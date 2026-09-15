# GLOBAL POWER — Fábrica de videos automática (GitHub, gratis, sin tarjeta)

Esto corre 100% solo, en una computadora que te presta GitHub gratis (no
la tuya). Vos hacés 5 pasos de clics una sola vez para dejarlo armado, y
después, para cada video nuevo, apretás un solo botón y esperás.

## PASO 1 — Crear cuenta de GitHub (gratis, sin tarjeta)

Andá a **https://github.com/signup**, creá tu cuenta con tu email. No pide
tarjeta en ningún momento para esto.

## PASO 2 — Crear el repositorio y subir esta carpeta

1. Ya logueado, clic en el botón verde **"New"** (o el ícono "+" arriba a
   la derecha → "New repository").
2. Nombre: `global-power` (o el que quieras).
3. Marcá **Public** (así los minutos de GitHub Actions son ilimitados y
   gratis; si lo dejás Private también funciona, con 2.000 minutos/mes
   gratis, de sobra para varios videos).
4. Clic en **"Create repository"**.
5. En la página del repo recién creado, buscá el link que dice
   **"uploading an existing file"** (aparece en el medio de la página, en
   las instrucciones de "Quick setup").
6. Arrastrá **toda la carpeta** `gp-video-factory` (la que te doy en el
   zip) directo al navegador, en esa pantalla. GitHub mantiene las
   subcarpetas automáticamente.
7. Abajo, clic en **"Commit changes"**.

## PASO 3 — Conseguir la API key gratis de Pexels (para las imágenes/videos)

1. Andá a **https://www.pexels.com/api/** → "Get Started" → registrate
   gratis (sin tarjeta) → te muestra tu API key al instante.
2. Copiala.

## PASO 4 — Pegar esa key en GitHub (una sola vez, sin código)

1. En tu repositorio de GitHub, andá a **Settings** (pestaña arriba).
2. Menú de la izquierda → **Secrets and variables** → **Actions**.
3. Clic en **"New repository secret"**.
4. Nombre: `PEXELS_API_KEY` (exactamente así).
5. Valor: pegás la key que copiaste del paso 3.
6. Clic en **"Add secret"**.

## PASO 5 — Generar el video (esto es lo que vas a repetir para cada video nuevo)

1. Pestaña **"Actions"** arriba del repositorio.
2. En la lista de la izquierda, clic en **"make-video"**.
3. Botón **"Run workflow"** (arriba a la derecha de la lista de
   ejecuciones).
4. Se abre un cuadro chico — dejalo con los valores que ya vienen
   puestos (o cambiá el título si querés otro).
5. Clic en el botón verde **"Run workflow"**.
6. Esperá. Vas a ver un círculo amarillo girando — cuando se pone tilde
   verde, terminó (tarda entre 5 y 20 minutos).
7. Clic en esa ejecución terminada → abajo de todo de esa página hay una
   sección **"Artifacts"** → clic en **"video-listo"** → se baja un .zip
   a tu computadora.

Adentro de ese .zip vas a tener:
- `final_video.mp4` — el video largo terminado, listo para subir a YouTube
- `short_video.mp4` — un Short vertical de ~30-45s hecho automáticamente
  con el gancho inicial del guion, subtítulos grandes y un cartel al final
  invitando a ver el video completo
- `subtitles.srt` — subtítulos del video largo, por si querés subirlos aparte
- `thumbnail_1.png` a `thumbnail_4.png` — 4 miniaturas distintas (la 4 es
  de alto contraste, con caja roja detrás del texto)
- `video1-descripcion-youtube.md` — el título, la descripción y los tags
  ya redactados, para copiar y pegar tal cual en YouTube

## ⚠️ PASO OBLIGATORIO al subir a YouTube (política vigente, reforzada en 2026)

YouTube exige declarar cuando un video usa voz sintética/IA. Al subir:

1. En el paso "Elementos de video" del asistente de subida, buscá la
   pregunta **"¿Tu contenido es realista y fue generado o alterado
   sintéticamente?"**
2. Marcá **Sí** y elegí la categoría de voz generada sintéticamente.

Esto no perjudica la monetización por sí solo — lo que sí la arriesga es
NO declararlo y que YouTube lo detecte después, o subir guiones genéricos
sin tesis propia. Nuestro guion ya tiene análisis propio, así que vamos
bien en ese punto.

## Para el próximo video

Solo necesitás: escribir el guion nuevo (yo te lo armo cuando quieras,
verificando los datos primero como hicimos con este), subirlo a la
carpeta `scripts/` del mismo repositorio (arrastrando el archivo, igual
que en el Paso 2), y repetir el Paso 5 con la ruta del guion nuevo en el
campo `script_path`. No hay que tocar nada más.

## Si algo falla

Clic en la ejecución que falló (aparece con una X roja) → clic en el paso
que falló para ver el detalle en texto. Pegámelo acá y lo reviso.
