# Immich Compress

Immich Compress is a sidecar app for compressing Immich videos with HandBrakeCLI while keeping the compression and metadata behavior from the original `hbed.py` workflow.

It is intentionally separate from Immich so you can maintain and deploy it without carrying an Immich source fork.

## Current status

This repo is an early scaffold. It can:

- serve a small web UI on port `8097`
- store job state in SQLite under `/data`
- accept an Immich asset ID or URL
- download the original asset through the Immich API
- compress it with HandBrakeCLI using the migrated `hbed.py` behavior
- copy metadata with ExifTool
- leave the compressed output in review state

Replacement/upload behavior from `upload_processed_to_immich.py` is not fully automated yet. The first implementation keeps replacement in review mode to avoid destructive surprises.

## Development

```powershell
cd F:\dev\immich-compress
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\uvicorn app.main:app --reload --port 8097
```

Open `http://localhost:8097`.

## Docker

The Docker image ships with the media tooling used by the app:

- `HandBrakeCLI`
- `exiftool`
- `ffmpeg`
- `ffprobe`

Copy `docker-compose.example.yml` into your Immich compose folder and adjust:

- `IMMICH_COMPRESS_API_KEY`
- `UPLOAD_LOCATION`
- `HANDBRAKE_ENCODER`
- GPU/device mappings if you want hardware encoding inside the container

Then run:

```bash
docker compose up -d immich-compress
```

### Standalone Docker Compose

You can run Immich Compress from its own folder without editing Immich's compose file. The current review-mode workflow downloads originals through the Immich API, so it only needs an API key and persistent `/data`.

Create an Immich API key, then create a `.env` file next to your compose file:

```bash
IMMICH_COMPRESS_API_KEY=your-api-key
```

For TrueNAS, use the published-image example so the app data stays under `/mnt/Apps/AppData/immich-compress` and you do not need the source repo on the server:

```bash
cp docker-compose.published.example.yml docker-compose.yml
docker compose up -d
```

Open `http://your-truenas-ip:8097`.

Set `IMMICH_URL` to your Immich LAN URL, for example `http://192.168.1.50:2283`.

If you want to build locally from source instead, use `docker-compose.example.yml` and run:

```bash
docker compose up -d --build
```

## Important settings

- `IMMICH_URL`: Immich server URL from inside Docker, usually `http://immich-server:2283`
- `IMMICH_API_KEY`: API key created in Immich
- `PROCESSED_SUFFIX`: default `-hbed`
- `HANDBRAKE_PRESET`: default `Fast 2160p60 4K HEVC`
- `HANDBRAKE_ENCODER`: default `nvenc_h265`
- `HANDBRAKE_CLI`: default `HandBrakeCLI`, already available in the Docker image
- `EXIFTOOL`: default `exiftool`, already available in the Docker image
- `DRY_RUN`: default `true`
- `REPLACEMENT_MODE`: default `review`

## GPU encoding

The app ships with HandBrakeCLI, but hardware encoding still needs the host GPU passed into the container.

### NVIDIA NVENC

The default encoder is `nvenc_h265`, matching the original script. For this to work, the Docker host must have:

- NVIDIA driver installed
- NVIDIA Container Toolkit installed/configured
- GPU access enabled for this container

For Docker Compose deployments that support it, add:

```yaml
services:
  immich-compress:
    gpus: all
    environment:
      HANDBRAKE_ENCODER: "nvenc_h265"
      NVIDIA_DRIVER_CAPABILITIES: "compute,video,utility"
```

If the logs show `Cannot load libnvidia-encode.so.1` or `Cannot load libcuda.so.1`, the container does not have GPU access yet.

On TrueNAS Scale, enable GPU passthrough/allocation for the Immich Compress app/container in the app settings. If using a custom Compose app, uncomment `gpus: all` in `docker-compose.published.example.yml` if your TrueNAS Docker setup supports it. Otherwise use the TrueNAS UI GPU allocation controls.

### Intel Quick Sync / VAAPI

For Intel hardware encoding, pass `/dev/dri` into the container and change the encoder:

```yaml
services:
  immich-compress:
    devices:
      - /dev/dri:/dev/dri
    environment:
      HANDBRAKE_ENCODER: "qsv_h265"
```

If QSV is not available, try `vaapi_h265` if your HandBrake build and host GPU support it.

### CPU fallback

For testing without GPU passthrough, set:

```yaml
HANDBRAKE_ENCODER: "x265"
```

This will be slower, but it confirms the rest of the workflow is healthy.

## Importing already-compressed files

For videos you already compressed manually, use the manual import utility. It looks for files whose stem ends in the configured suffix, such as `20250503_210902-hbed.mp4`, searches Immich for the original asset stem, records that asset as `processed` in the sidecar database, and can rename the local file back to `20250503_210902.mp4`.

Start with a dry run:

```powershell
immich-compress-import-manual "D:\path\to\processed-videos" --recursive --rename
```

Apply the database import and rename:

```powershell
immich-compress-import-manual "D:\path\to\processed-videos" --recursive --rename --apply
```

The utility refuses to rename if the target filename already exists. That means `20250503_210902-hbed.mp4` will not overwrite an existing `20250503_210902.mp4`.

You can override the suffix:

```powershell
immich-compress-import-manual "D:\path\to\processed-videos" --suffix "-hbed" --rename --apply
```

## Migration notes

The migrated compression logic preserves the important behavior from `hbed.py`:

- supports the same video extensions
- writes outputs with the configured processed suffix
- keeps the stored pixel matrix dimensions by default
- optionally bounds upscaled output to 3840x2160
- neutralizes rotation metadata before HandBrake encoding
- restores metadata with ExifTool using an args file
- removes ExifTool `_original` artifacts
