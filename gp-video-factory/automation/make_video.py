#!/usr/bin/env python3
"""
GLOBAL POWER — make_video.py (v2 — post-Video-1 hardening pass)

Automates steps 2, 3, and 4 of producing a video from an already-written
script.

CHANGES IN THIS VERSION (fixing issues found in the first real render):
  1. Duration safety net: the final video is now HARD-TRIMMED to the total
     narration duration (plus a tiny deliberate tail) as the very last
     step, regardless of anything upstream. Video can no longer run long.
  2. Multi-shot B-roll: each scene is now built from several short clips
     (~12s each, documentary pace) instead of one clip stretched/looped
     for the whole scene.
  3. Smarter Pexels selection: no longer just picks the longest available
     clip. Scores by search relevance (Pexels' own ranking) + duration
     fit + avoids clips already used earlier in the same video.
  4. -stream_loop -1 is now CONDITIONAL: only applied when the clip is
     actually shorter than the shot it needs to fill. A long clip is just
     trimmed, never redundantly looped.
  5. Audio true-peak ceiling: the finished final_video.mp4 itself (not
     just an intermediate WAV) is passed through a peak limiter as the
     last processing step, targeting well under 0 dBTP.
  6. Normalized frame rate (30fps) on every shot, to avoid concat issues
     between clips of different source frame rates.

WHAT YOU STILL DO MANUALLY:
  - Install Piper locally / this runs inside GitHub Actions with Piper
    installed via pip (see the workflow file).
  - Get a free Pexels API key.
  - Upload the final MP4 to YouTube yourself.

REQUIREMENTS:
    pip install piper-tts==1.4.2 faster-whisper==1.1.0 requests==2.32.3 pillow==11.0.0
    ffmpeg must be installed and on PATH.

USAGE:
    python3 make_video.py --script episode-01.md \
                           --voice-model /path/to/en_US-ljspeech-high.onnx \
                           --pexels-key YOUR_PEXELS_KEY \
                           --title "America Built a Weapon That Hit 20,000 Chinese Companies"
"""

import argparse
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ==========================================================================
# CONFIG DEFAULTS
# ==========================================================================

WORKDIR = Path("./work")
OUTDIR = Path("./output")
VIDEO_WIDTH, VIDEO_HEIGHT = 1920, 1080
VIDEO_FPS = 30
WHISPER_MODEL_SIZE = "base"

SHOT_TARGET_SECONDS = 12      # aim for a new shot roughly every ~12s (documentary pace)
MAX_SHOTS_PER_SCENE = 6
MIN_SHOT_SECONDS = 4          # never split below this, avoids frantic cutting
FINAL_TAIL_PADDING = 0.3      # tiny deliberate margin after the last word, not a full clip
AUDIO_PEAK_LIMIT = 0.75       # calibrated from real render #2 measurement:
                               # limit=0.83 (nominal -1.62 dBFS) produced an
                               # ACTUAL measured true peak of -0.6 dBTP, i.e.
                               # ~1.02 dB of inter-sample overshoot (alimiter
                               # limits sample peaks, not oversampled true
                               # peaks, so some overshoot is expected). This
                               # value (-2.52 dB nominal / 0.75 linear) is
                               # calibrated to land the ACTUAL true peak of
                               # the finished file at approximately -1.5 dBTP.
                               # If a future real render measures meaningfully
                               # off target, adjust this constant by the same
                               # residual difference (in dB, converted back to
                               # linear via 10**(db/20)) rather than guessing.

# Several sub-topic queries per scene, cycled across that scene's shots, so
# a long scene pulls from more than one visual idea instead of one clip
# stretched to fill it. The globe/map resource is deliberately tied to the
# "ownership network" concepts (Part 2) rather than used as generic filler.
SCENE_QUERIES = {
    "COLD OPEN": ["us capitol washington dramatic", "washington dc government building night"],
    "PART 1": ["semiconductor manufacturing factory", "computer chips close up technology", "china factory manufacturing workers"],
    "PART 2": ["global network map connections", "world map data nodes glowing", "corporate ownership structure diagram"],
    "PART 3": ["tungsten mining industrial", "chemical manufacturing plant", "industrial production factory china"],
    "PART 4": ["diplomats handshake meeting", "trade negotiation summit table", "international summit flags"],
    "PART 5": ["clock countdown ticking macro", "cargo containers shipping port", "customs trade port cranes"],
    "CLOSE": ["world map global trade routes", "financial markets stock exchange global"],
}
DEFAULT_QUERIES = ["geopolitics global economy abstract"]


# ==========================================================================
# STEP 0 — parse the script into scenes
# ==========================================================================

def parse_scenes(script_path: Path):
    text = script_path.read_text(encoding="utf-8")
    skip_prefixes = ("SOURCES", "NOTES FOR THE FACT-CHECK")

    scenes = []
    current_heading = None
    current_lines = []

    for line in text.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_heading and not current_heading.upper().startswith(skip_prefixes):
                body = "\n".join(current_lines).strip()
                if body:
                    scenes.append((current_heading, body))
            current_heading = m.group(1).strip()
            current_lines = []
            continue
        if current_heading is not None:
            current_lines.append(line)

    if current_heading and not current_heading.upper().startswith(skip_prefixes):
        body = "\n".join(current_lines).strip()
        if body:
            scenes.append((current_heading, body))

    return scenes


def scene_queries(heading: str):
    for key, queries in SCENE_QUERIES.items():
        if heading.upper().startswith(key):
            return queries
    return DEFAULT_QUERIES


# ==========================================================================
# STEP 1 — local Piper call
# ==========================================================================

def synthesize_audio(text: str, voice_model: Path, out_wav: Path):
    print(f"  [voice] synthesizing -> {out_wav.name}")
    proc = subprocess.run(
        ["piper", "--model", str(voice_model), "--output_file", str(out_wav)],
        input=text.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"piper failed: {proc.stderr.decode(errors='ignore')}")


def get_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(proc.stdout.strip())


# ==========================================================================
# STEP 2 — fetch B-roll from Pexels with a relevance/variety-aware heuristic
# ==========================================================================

def search_pexels(query: str, pexels_key: str, per_page: int = 8):
    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": pexels_key},
        params={"query": query, "per_page": per_page, "orientation": "landscape"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("videos", [])


def pick_best_clip(results, shot_duration: float, used_ids: set):
    """Heuristic selection (no AI needed):
    1. Respect Pexels' own relevance ranking as the primary signal (the
       order results come back in).
    2. Penalize clips whose duration is a poor fit (too short = lots of
       looping; wildly longer than needed = usually generic filler).
    3. Prefer clips not already used earlier in this video, but don't
       fail the whole scene if every candidate has been used before.
    """
    if not results:
        return None

    def score(rank, video):
        dur = video.get("duration", 0) or 0
        if dur <= 0:
            duration_penalty = 50
        elif dur < shot_duration:
            duration_penalty = (shot_duration - dur) * 2  # will need looping
        elif dur > shot_duration * 6:
            duration_penalty = 15  # probably generic ambient filler, mild penalty
        else:
            duration_penalty = 0
        reuse_penalty = 100 if video["id"] in used_ids else 0
        return rank * 3 + duration_penalty + reuse_penalty

    ranked = sorted(enumerate(results), key=lambda pair: score(pair[0], pair[1]))
    return ranked[0][1]


def fetch_pexels_shot(queries, shot_index: int, shot_duration: float,
                       pexels_key: str, out_path: Path, used_ids: set):
    query = queries[shot_index % len(queries)]
    print(f"  [visuals] shot {shot_index + 1}: searching Pexels for '{query}'")
    results = search_pexels(query, pexels_key)

    if not results:
        # Fall back to the generic default query rather than crashing the run.
        print(f"    no results for '{query}', falling back to default query")
        results = search_pexels(DEFAULT_QUERIES[0], pexels_key)
    if not results:
        raise RuntimeError(f"No Pexels results at all for '{query}' or the fallback query")

    chosen = pick_best_clip(results, shot_duration, used_ids)
    used_ids.add(chosen["id"])

    files = sorted(
        chosen["video_files"],
        key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
        reverse=True,
    )
    video_url = files[0]["link"]

    r = requests.get(video_url, stream=True, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 16):
            f.write(chunk)

    return chosen.get("duration", 0)


# ==========================================================================
# STEP 3a — build one silent, exact-duration, normalized "shot"
# ==========================================================================

def build_silent_shot(raw_clip: Path, source_duration: float, shot_duration: float, out_mp4: Path):
    """Scales/crops to the standard frame, normalizes to VIDEO_FPS, and
    trims to EXACTLY shot_duration. Only loops if the source clip is
    actually shorter than what's needed — a long clip is just trimmed,
    never redundantly looped."""
    needs_loop = source_duration > 0 and source_duration < shot_duration
    cmd = ["ffmpeg", "-y"]
    if needs_loop:
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-i", str(raw_clip),
        "-an",
        "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
               f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS}",
        "-t", f"{shot_duration:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def split_duration(total: float, n: int):
    """n shot durations that sum EXACTLY to total (last one absorbs rounding)."""
    base = total / n
    durs = [base] * n
    durs[-1] = total - sum(durs[:-1])
    return durs


def assemble_scene_broll(heading: str, duration: float, pexels_key: str,
                          work_prefix: Path, used_ids: set) -> Path:
    """Builds the scene's full-duration SILENT video track out of several
    shots pulled from different queries, so no single clip fills more
    than ~SHOT_TARGET_SECONDS of screen time."""
    queries = scene_queries(heading)

    n_shots = max(1, min(MAX_SHOTS_PER_SCENE, round(duration / SHOT_TARGET_SECONDS)))
    if duration / max(n_shots, 1) < MIN_SHOT_SECONDS and n_shots > 1:
        n_shots = max(1, int(duration // MIN_SHOT_SECONDS))
    shot_durations = split_duration(duration, n_shots)

    print(f"  [broll] {heading}: {n_shots} shot(s) totaling {duration:.1f}s "
          f"({', '.join(f'{d:.1f}s' for d in shot_durations)})")

    shot_files = []
    for i, shot_dur in enumerate(shot_durations):
        raw = work_prefix.with_name(work_prefix.name + f"_shot{i:02}_raw.mp4")
        shot_out = work_prefix.with_name(work_prefix.name + f"_shot{i:02}.mp4")
        source_dur = fetch_pexels_shot(queries, i, shot_dur, pexels_key, raw, used_ids)
        build_silent_shot(raw, source_dur, shot_dur, shot_out)
        shot_files.append(shot_out)

    broll_out = work_prefix.with_name(work_prefix.name + "_broll.mp4")
    if len(shot_files) == 1:
        shot_files[0].rename(broll_out)
    else:
        concat_list = work_prefix.with_name(work_prefix.name + "_concat.txt")
        with open(concat_list, "w") as f:
            for sf in shot_files:
                f.write(f"file '{sf.resolve()}'\n")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list), "-c", "copy", str(broll_out),
        ], check=True, capture_output=True)

    return broll_out


# ==========================================================================
# STEP 3b — mux a scene's silent B-roll with its narration audio
# ==========================================================================

def mux_scene(broll_silent: Path, audio_wav: Path, out_mp4: Path, duration: float):
    print(f"  [assemble] muxing scene -> {out_mp4.name} ({duration:.1f}s)")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(broll_silent),
        "-i", str(audio_wav),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac",
        "-t", f"{duration:.3f}",
        "-shortest",
        str(out_mp4),
    ], check=True, capture_output=True)


def concatenate_scenes(scene_files, out_path: Path):
    print(f"  [assemble] concatenating {len(scene_files)} scenes -> {out_path.name}")
    concat_list = WORKDIR / "concat_list.txt"
    with open(concat_list, "w") as f:
        for sf in scene_files:
            f.write(f"file '{sf.resolve()}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(out_path),
    ], check=True, capture_output=True)


# ==========================================================================
# STEP 3c — subtitles via local Whisper, burned into the video
# ==========================================================================

def generate_subtitles(video_path: Path, srt_path: Path):
    print("  [subtitles] transcribing with local Whisper (this can take a bit)...")
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video_path), word_timestamps=False)

    def fmt(t):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{s:06.3f}".replace(".", ",")

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n{fmt(seg.start)} --> {fmt(seg.end)}\n{seg.text.strip()}\n\n")


def burn_subtitles(video_in: Path, srt_path: Path, video_out: Path):
    print(f"  [subtitles] burning into video -> {video_out.name}")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_in),
        "-vf", f"subtitles={srt_path}:force_style='FontSize=22,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2'",
        "-c:a", "copy",
        str(video_out),
    ], check=True, capture_output=True)


# ==========================================================================
# STEP 3d (NEW) — final mastering pass: HARD duration cap + audio peak ceiling
# ==========================================================================

def final_master(video_in: Path, target_duration: float, video_out: Path):
    """The last word on both open problems from Video 1:
    - Duration: no matter what happened upstream, the file on disk after
      this step can never exceed target_duration (+ the tiny deliberate
      tail padding already folded into target_duration by the caller).
    - Audio: runs the actual output audio through a peak limiter so the
      finished final_video.mp4 itself — not just an intermediate WAV —
      stays under the true-peak ceiling.
    Video is stream-copied (fast, no quality loss); only audio is
    re-encoded, since only audio needs the limiter applied.
    """
    print(f"  [master] final hard-trim to {target_duration:.2f}s + peak limiter -> {video_out.name}")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_in),
        "-t", f"{target_duration:.3f}",
        "-af", f"alimiter=limit={AUDIO_PEAK_LIMIT}:attack=5:release=50:level=disabled",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        str(video_out),
    ], check=True, capture_output=True)


# ==========================================================================
# STEP 4 — thumbnails
# ==========================================================================

def extract_frame(video_path: Path, timestamp: float, out_png: Path):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(timestamp), "-i", str(video_path),
        "-frames:v", "1", str(out_png),
    ], check=True, capture_output=True)


def make_thumbnail(frame_png: Path, title: str, out_png: Path, style: str):
    img = Image.open(frame_png).convert("RGB")
    img = img.resize((1280, 720))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except OSError:
        font = ImageFont.load_default()

    band_top = 720 - 260 if style in ("bottom", "highcontrast") else 40
    band = Image.new("RGBA", (1280, 260), (0, 0, 0, 160))
    img.paste(Image.alpha_composite(img.crop((0, band_top, 1280, band_top + 260)).convert("RGBA"), band).convert("RGB"),
              (0, band_top))

    words = title.upper().split()
    lines, current = [], ""
    for w in words:
        test = (current + " " + w).strip()
        if draw.textlength(test, font=font) > 1180:
            lines.append(current)
            current = w
        else:
            current = test
    if current:
        lines.append(current)
    lines = lines[:3]

    y = band_top + 20
    color = "#FFFFFF" if style not in ("alert", "highcontrast") else "#FFD400"
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (1280 - w) / 2
        if style == "highcontrast":
            draw.rectangle([x - 12, y - 8, x + w + 12, y + 78], fill="#CC0000")
        draw.text((x, y), line, font=font, fill=color,
                   stroke_width=3, stroke_fill="#000000")
        y += 80

    img.save(out_png)


# ==========================================================================
# OPTIMIZATION — automatic Short (vertical, ~30-45s) from the Cold Open scene
# ==========================================================================

def build_short(broll_silent: Path, audio_wav: Path, channel_name: str, out_mp4: Path):
    print("  [short] generating vertical Short from the Cold Open scene...")

    duration = get_duration(audio_wav)
    short_srt = WORKDIR / "short_subtitles.srt"
    generate_subtitles(audio_wav, short_srt)

    cta_start = max(duration - 2.5, 0)
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(broll_silent),
        "-i", str(audio_wav),
        "-vf",
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"subtitles={short_srt}:force_style='FontSize=30,PrimaryColour=&HFFFFFF&,"
        f"OutlineColour=&H000000&,BorderStyle=1,Outline=3,Alignment=2,MarginV=200',"
        f"drawtext=text='Full story\\: {channel_name}':fontsize=42:fontcolor=white:"
        f"box=1:boxcolor=black@0.6:boxborderw=15:x=(w-text_w)/2:y=h-260:"
        f"enable='gte(t,{cta_start})'",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-c:a", "aac",
        "-af", f"alimiter=limit={AUDIO_PEAK_LIMIT}:attack=5:release=50:level=disabled",
        "-t", f"{duration:.3f}",
        "-shortest",
        str(out_mp4),
    ], check=True, capture_output=True)


# ==========================================================================
# MAIN PIPELINE
# ==========================================================================

def main():
    ap = argparse.ArgumentParser(description="Automate GLOBAL POWER video assembly (steps 2-4)")
    ap.add_argument("--script", required=True, type=Path)
    ap.add_argument("--voice-model", required=True, type=Path)
    ap.add_argument("--pexels-key", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--channel-name", default="GLOBAL POWER")
    ap.add_argument("--skip-subtitles", action="store_true")
    ap.add_argument("--skip-short", action="store_true")
    args = ap.parse_args()

    missing = [t for t in ("piper", "ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        print(f"\nERROR: no se encontraron estos programas en el PATH: {missing}\n", file=sys.stderr)
        sys.exit(1)

    if not args.pexels_key or not args.pexels_key.strip():
        print("\nERROR: no llegó una API key de Pexels válida (PEXELS_API_KEY).\n", file=sys.stderr)
        sys.exit(1)

    WORKDIR.mkdir(exist_ok=True)
    OUTDIR.mkdir(exist_ok=True)

    print("Parsing script into scenes...")
    scenes = parse_scenes(args.script)
    print(f"Found {len(scenes)} scenes.")

    used_clip_ids = set()
    scene_mp4s = []
    total_narration_duration = 0.0
    scene1_broll = scene1_audio = None

    for i, (heading, body) in enumerate(scenes, start=1):
        print(f"\nScene {i}/{len(scenes)}: {heading}")
        audio_wav = WORKDIR / f"scene_{i:02}_audio.wav"
        scene_mp4 = WORKDIR / f"scene_{i:02}_final.mp4"

        synthesize_audio(body, args.voice_model, audio_wav)
        duration = get_duration(audio_wav)
        total_narration_duration += duration

        broll = assemble_scene_broll(heading, duration, args.pexels_key,
                                      WORKDIR / f"scene_{i:02}", used_clip_ids)
        mux_scene(broll, audio_wav, scene_mp4, duration)

        scene_mp4s.append(scene_mp4)
        if i == 1:
            scene1_broll, scene1_audio = broll, audio_wav

    if not args.skip_short:
        print("\nGenerating vertical Short from the Cold Open...")
        short_out = OUTDIR / "short_video.mp4"
        build_short(scene1_broll, scene1_audio, args.channel_name, short_out)
        print(f"  -> {short_out}")

    raw_final = WORKDIR / "final_no_subs.mp4"
    concatenate_scenes(scene_mp4s, raw_final)

    subtitled = WORKDIR / "final_subtitled.mp4"
    srt_path = OUTDIR / "subtitles.srt"
    if args.skip_subtitles:
        subtitled = raw_final
    else:
        generate_subtitles(raw_final, srt_path)
        burn_subtitles(raw_final, srt_path, subtitled)

    # SAFETY NET (layer 3): hard-trim to the real total narration duration
    # plus a tiny deliberate tail, and apply the audio peak ceiling to the
    # ACTUAL finished file — regardless of anything upstream.
    final_video = OUTDIR / "final_video.mp4"
    target_duration = total_narration_duration + FINAL_TAIL_PADDING
    final_master(subtitled, target_duration, final_video)

    print("\nGenerating thumbnails...")
    frame_png = WORKDIR / "thumb_frame.png"
    approx_ts = get_duration(WORKDIR / "scene_01_audio.wav") + 2
    extract_frame(raw_final, approx_ts, frame_png)

    for i, style in enumerate(["bottom", "top", "alert", "highcontrast"], start=1):
        out_png = OUTDIR / f"thumbnail_{i}.png"
        make_thumbnail(frame_png, args.title, out_png, style)
        print(f"  -> {out_png}")

    actual_final_duration = get_duration(final_video)
    print(f"\nDone.")
    print(f"Narration total: {total_narration_duration:.2f}s")
    print(f"Final video duration: {actual_final_duration:.2f}s")
    print(f"Difference: {actual_final_duration - total_narration_duration:.2f}s")
    print(f"Unique Pexels clips used: {len(used_clip_ids)}")
    print(f"Final video: {final_video}")
    print(f"Short: {OUTDIR / 'short_video.mp4' if not args.skip_short else '(skipped)'}")
    print(f"Subtitles: {srt_path if not args.skip_subtitles else '(skipped)'}")
    print(f"Thumbnails: {OUTDIR}/thumbnail_1.png through thumbnail_4.png")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\nERROR running external tool: {e}\nstderr: {e.stderr.decode(errors='ignore') if e.stderr else ''}", file=sys.stderr)
        sys.exit(1)
