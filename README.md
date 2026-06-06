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
- queue selected or all unprocessed videos without duplicate processing
- cancel individual jobs or cancel active work and clear the entire queue

Accepted videos are uploaded as new Immich assets, then Immich-side details are copied from the original asset with `copyAsset`. The original asset id and copied asset id are both tracked in this app.

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

Copy `docker-compose.example.yml` into your Immich compose folder and adjust the volume
paths and GPU/device mappings if you want hardware encoding inside the container.

Then run:

```bash
docker compose up -d immich-compress
```

### Standalone Docker Compose

You can run Immich Compress from its own folder without editing Immich's compose file. The
workflow downloads originals through the Immich API, so the container only needs network
access to Immich and persistent `/data`.

For TrueNAS, use the published-image example so the app data stays under `/mnt/Apps/AppData/immich-compress` and you do not need the source repo on the server:

```bash
cp docker-compose.published.example.yml docker-compose.yml
docker compose up -d
```

Open `http://your-truenas-ip:8097`.

Open Settings and configure:

- the Immich URL reachable from the container, for example `http://192.168.1.50:2283`
- an Immich API key
- a preset and encoder detected from the bundled HandBrakeCLI
- concurrency and optional 4K upscaling behavior
- review or automatic replacement mode

If you want to build locally from source instead, use `docker-compose.example.yml` and run:

```bash
docker compose up -d --build
```

## Settings

Connection, preset, encoder, concurrency, upscaling, and workflow mode are configured from the Settings page and
stored in `/data/immich-compress.sqlite`. HandBrake presets and encoders are detected from
the bundled CLI and presented as described dropdowns. Environment variables remain optional
bootstrap fallbacks for existing or automated deployments, but fresh installs have no
hard-coded preset or encoder.

Tool path variables such as `HANDBRAKE_CLI` and `EXIFTOOL` remain available for advanced
deployments. The commands are already included in the published Docker image.

## GPU encoding

The app ships with HandBrakeCLI, but hardware encoding still needs the host GPU passed into the container.

### NVIDIA NVENC

To use an NVENC encoder, the Docker host must have:

- NVIDIA driver installed
- NVIDIA Container Toolkit installed/configured
- GPU access enabled for this container

For Docker Compose deployments that support it, add:

```yaml
services:
  immich-compress:
    gpus: all
    environment:
      NVIDIA_DRIVER_CAPABILITIES: "compute,video,utility"
```

If the logs show `Cannot load libnvidia-encode.so.1` or `Cannot load libcuda.so.1`, the container does not have GPU access yet.

On TrueNAS Scale, enable GPU passthrough/allocation for the Immich Compress app/container in the app settings. If using a custom Compose app, uncomment `gpus: all` in `docker-compose.published.example.yml` if your TrueNAS Docker setup supports it. Otherwise use the TrueNAS UI GPU allocation controls.

### Intel Quick Sync / VAAPI

For Intel hardware encoding, pass `/dev/dri` into the container and choose a QSV encoder in Settings:

```yaml
services:
  immich-compress:
    devices:
      - /dev/dri:/dev/dri
```

If QSV is not available, try `vaapi_h265` if your HandBrake build and host GPU support it.

### CPU fallback

For testing without GPU passthrough, choose a software encoder such as `x265` in Settings.
This will be slower, but it confirms the rest of the workflow is healthy.

## Accepting reviewed files

The app does not modify the downloaded working copy under `/data/work/<asset-id>/`. That file is only a temporary local copy used for encoding and review.

In `review` mode, each compressed job exposes two actions:

- `Accept`: uploads the compressed file, copies Immich-side information, trashes the original asset, marks the job complete, and deletes local working copies.
- `Reject`: discards the result, marks the job rejected, and deletes local working copies.

In `auto` mode, the Accept workflow runs automatically after compression succeeds.

```text
POST /api/assets
PUT /api/assets/copy
```

The original asset id remains cached in this app as `asset_id`, and the new uploaded asset id is stored as `target_asset_id`.

`copyAsset` copies Immich-side data such as albums, favorites, shared links, sidecars, and stacks. ExifTool is still used before upload so metadata embedded inside the MP4 migrates with the actual file.

## Importing already-compressed files

For videos you already compressed manually, use the manual import utility. It looks for files whose stem ends in the legacy suffix, such as `20250503_210902-hbed.mp4`, searches Immich for the original asset stem, records that asset as `processed` in the sidecar database, and can rename the local file back to `20250503_210902.mp4`.

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
- writes temporary outputs using the original filename in the job work folder
- keeps the stored pixel matrix dimensions by default
- optionally bounds upscaled output to 3840x2160
- neutralizes rotation metadata before HandBrake encoding
- restores metadata with ExifTool using an args file
- removes ExifTool `_original` artifacts
