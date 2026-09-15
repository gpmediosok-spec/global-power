#!/usr/bin/env python3
"""
GLOBAL POWER — make_video.py

Automates steps 2, 3, and 4 of producing a video from an already-written
script (step 1, writing the script, is done — see global-power-ep1-script.md).

WHAT THIS SCRIPT DOES, END TO END:
  1. Parses the script markdown into scenes (one per "## " section).
  2. Calls your LOCAL Piper install to synthesize narration audio per scene.
  3. Searches Pexels for a matching stock video clip per scene (step 2:
     visuals, automated).
  4. Assembles each scene (clip + narration) and concatenates them into
     one final video with ffmpeg (step 3: editing, automated).
  5. Runs Whisper locally to generate subtitles and burns them in.
  6. Generates 3 thumbnail variants with title text overlaid (step 4:
     thumbnail, automated).

WHAT YOU STILL DO MANUALLY (step 1, per your request):
  - Install Piper locally: https://github.com/OHF-Voice/piper1-gpl
    (`pip install piper-tts==1.4.2`) and download the voice model once:
    https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/ljspeech/high
  - Get a free Pexels API key: https://www.pexels.com/api/ (used for visuals)
  - Upload the final MP4 to YouTube yourself (Phase 3 will automate this
    part too, once the pipeline is proven).

REQUIREMENTS (install once):
    pip install piper-tts==1.4.2 faster-whisper==1.1.0 requests==2.32.3 pillow==11.0.0
    ffmpeg must be installed and on PATH (ffmpeg.org/download.html)

USAGE:
    python3 make_video.py --script global-power-ep1-script.md \
                           --voice-model /path/to/en_US-ljspeech-high.onnx \
                           --pexels-key YOUR_PEXELS_KEY \
                           --title "America Built a Weapon That Hit 20,000 Chinese Companies"

Output lands in ./output/: final_video.mp4, subtitles.srt, thumbnail_1.png,
thumbnail_2.png, thumbnail_3.png.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ==========================================================================
# CONFIG DEFAULTS — override via CLI flags, see argparse section below
# ==========================================================================

WORKDIR = Path("./work")
OUTDIR = Path("./output")
VIDEO_WIDTH, VIDEO_HEIGHT = 1920, 1080
WHISPER_MODEL_SIZE = "base"

# Manual keyword hints per scene heading, used to search Pexels. Falls back
# to a generic geopolitics/tech query if a heading isn't in this map.
SCENE_KEYWORDS = {
    "COLD OPEN": "government building silhouette dramatic",
    "PART 1": "computer chip factory technology",
    "PART 2": "network connections data map",
    "PART 3": "industrial factory gas pipes",
    "PART 4": "handshake diplomats meeting",
    "PART 5": "clock ticking countdown",
    "CLOSE": "world map global trade",
}
DEFAULT_KEYWORD = "geopolitics global economy abstract"


# ==========================================================================
# STEP 0 — parse the script into scenes
# ==========================================================================

def parse_scenes(script_path: Path):
    """Split the markdown script into (heading, narration_text) scenes.
    Skips the front-matter, SOURCES, and NOTES sections."""
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


def scene_keyword(heading: str) -> str:
    for key, kw in SCENE_KEYWORDS.items():
        if heading.upper().startswith(key):
            return kw
    return DEFAULT_KEYWORD


# ==========================================================================
# STEP 1 (assumed done by the user) — just a helper to call local Piper
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


def get_audio_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(proc.stdout.strip())


# ==========================================================================
# STEP 2 (automated) — fetch a matching stock video clip from Pexels
# ==========================================================================

def fetch_pexels_clip(keyword: str, pexels_key: str, out_path: Path, min_duration: float):
    print(f"  [visuals] searching Pexels for: '{keyword}'")
    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": pexels_key},
        params={"query": keyword, "per_page": 5, "orientation": "landscape"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("videos", [])
    if not results:
        raise RuntimeError(f"No Pexels results for '{keyword}'")

    # Prefer a clip at least as long as the narration; otherwise take the
    # longest available (we'll loop it with ffmpeg if still too short).
    results.sort(key=lambda v: v.get("duration", 0), reverse=True)
    chosen = results[0]

    # Pick the highest-resolution landscape file link available.
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


# ==========================================================================
# STEP 3 (automated) — assemble each scene, then concatenate with ffmpeg
# ==========================================================================

def build_scene_clip(raw_clip: Path, audio_wav: Path, out_mp4: Path, duration: float):
    print(f"  [assemble] building scene -> {out_mp4.name} ({duration:.1f}s)")
    # -stream_loop -1 lets ffmpeg loop the source clip if it's shorter than
    # the narration; -shortest then trims everything to the audio length.
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(raw_clip),
        "-i", str(audio_wav),
        "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
               f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-c:a", "aac",
        "-t", str(duration),
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
        "-i", str(concat_list),
        "-c", "copy",
        str(out_path),
    ], check=True, capture_output=True)


# ==========================================================================
# STEP 3b (automated) — subtitles via local Whisper, burned into the video
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
# STEP 4 (automated) — thumbnail generation with Pillow
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

    # Darken a band behind the text for legibility.
    band_top = 720 - 260 if style in ("bottom", "highcontrast") else 40
    band = Image.new("RGBA", (1280, 260), (0, 0, 0, 160))
    img.paste(Image.alpha_composite(img.crop((0, band_top, 1280, band_top + 260)).convert("RGBA"), band).convert("RGB"),
              (0, band_top))

    # Wrap title text manually into up to 3 lines.
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

def build_short(raw_clip: Path, audio_wav: Path, channel_name: str, out_mp4: Path):
    """Turns the Cold Open scene into a vertical Short with big burned
    captions and a call-to-action at the end. Shorts are the top-of-funnel
    for discovery, so this reuses the strongest hook (the cold open) rather
    than a random scene."""
    print("  [short] generating vertical Short from the Cold Open scene...")

    duration = get_audio_duration(audio_wav)
    short_srt = WORKDIR / "short_subtitles.srt"
    generate_subtitles(audio_wav, short_srt)

    cta_start = max(duration - 2.5, 0)
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(raw_clip),
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
        "-t", str(duration),
        "-shortest",
        str(out_mp4),
    ], check=True, capture_output=True)


# ==========================================================================
# MAIN PIPELINE
# ==========================================================================

def main():
    ap = argparse.ArgumentParser(description="Automate GLOBAL POWER video assembly (steps 2-4)")
    ap.add_argument("--script", required=True, type=Path, help="Path to the script markdown file")
    ap.add_argument("--voice-model", required=True, type=Path, help="Path to the Piper .onnx voice model")
    ap.add_argument("--pexels-key", required=True, help="Free Pexels API key")
    ap.add_argument("--title", required=True, help="Video title, used for the thumbnail")
    ap.add_argument("--channel-name", default="GLOBAL POWER", help="Shown in the Short's call-to-action overlay")
    ap.add_argument("--skip-subtitles", action="store_true", help="Skip Whisper subtitle burn-in (faster)")
    ap.add_argument("--skip-short", action="store_true", help="Skip generating the vertical Short")
    args = ap.parse_args()

    import shutil
    missing = [t for t in ("piper", "ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        print(f"\nERROR: no se encontraron estos programas en el PATH: {missing}\n"
              f"Si esto corre en GitHub Actions, revisá que los pasos de instalación\n"
              f"previos (pip install / apt-get install) hayan terminado sin error.\n",
              file=sys.stderr)
        sys.exit(1)

    if not args.pexels_key or not args.pexels_key.strip():
        print(
            "\nERROR: no llegó una API key de Pexels válida.\n"
            "Revisá que en GitHub, en Settings -> Secrets and variables -> Actions,\n"
            "exista un secret llamado exactamente PEXELS_API_KEY con tu key pegada ahí.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    WORKDIR.mkdir(exist_ok=True)
    OUTDIR.mkdir(exist_ok=True)

    print("Parsing script into scenes...")
    scenes = parse_scenes(args.script)
    print(f"Found {len(scenes)} scenes.")

    scene_mp4s = []
    for i, (heading, body) in enumerate(scenes, start=1):
        print(f"\nScene {i}/{len(scenes)}: {heading}")
        audio_wav = WORKDIR / f"scene_{i:02}_audio.wav"
        raw_clip = WORKDIR / f"scene_{i:02}_raw.mp4"
        scene_mp4 = WORKDIR / f"scene_{i:02}_final.mp4"

        synthesize_audio(body, args.voice_model, audio_wav)
        duration = get_audio_duration(audio_wav)

        keyword = scene_keyword(heading)
        fetch_pexels_clip(keyword, args.pexels_key, raw_clip, duration)
        build_scene_clip(raw_clip, audio_wav, scene_mp4, duration)

        scene_mp4s.append(scene_mp4)
        if i == 1:
            # Keep the Cold Open's raw clip/audio around for the Short.
            scene1_raw_clip, scene1_audio = raw_clip, audio_wav

    if not args.skip_short:
        print("\nGenerating vertical Short from the Cold Open...")
        short_out = OUTDIR / "short_video.mp4"
        build_short(scene1_raw_clip, scene1_audio, args.channel_name, short_out)
        print(f"  -> {short_out}")

    raw_final = WORKDIR / "final_no_subs.mp4"
    concatenate_scenes(scene_mp4s, raw_final)

    final_video = OUTDIR / "final_video.mp4"
    srt_path = OUTDIR / "subtitles.srt"

    if args.skip_subtitles:
        raw_final.rename(final_video)
    else:
        generate_subtitles(raw_final, srt_path)
        burn_subtitles(raw_final, srt_path, final_video)

    print("\nGenerating thumbnails...")
    frame_png = WORKDIR / "thumb_frame.png"
    # Grab from the PRE-subtitle video so a caption line never accidentally
    # lands baked into the thumbnail image. If subtitles were skipped,
    # raw_final was already renamed into final_video, so fall back to that.
    approx_ts = get_audio_duration(WORKDIR / "scene_01_audio.wav") + 2
    frame_source = raw_final if raw_final.exists() else final_video
    extract_frame(frame_source, approx_ts, frame_png)

    for i, style in enumerate(["bottom", "top", "alert", "highcontrast"], start=1):
        out_png = OUTDIR / f"thumbnail_{i}.png"
        make_thumbnail(frame_png, args.title, out_png, style)
        print(f"  -> {out_png}")

    print(f"\nDone. Final video: {final_video}")
    print(f"Short: {OUTDIR / 'short_video.mp4' if not args.skip_short else '(skipped)'}")
    print(f"Subtitles: {srt_path if not args.skip_subtitles else '(skipped)'}")
    print(f"Thumbnails: {OUTDIR}/thumbnail_1.png through thumbnail_4.png")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\nERROR running external tool: {e}\nstderr: {e.stderr.decode(errors='ignore') if e.stderr else ''}", file=sys.stderr)
        sys.exit(1)
