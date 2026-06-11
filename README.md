# Immich Compress

Immich Compress is a sidecar app for compressing existing Immich videos with SVT-AV1 while
preserving resolution, orientation, audio, chapters, and metadata.

It is intentionally separate from Immich so you can maintain and deploy it without carrying an Immich source fork.

## Current status

This repo is an early scaffold. It can:

- serve a small web UI on port `8097`
- store job state in SQLite under `/data`
- accept an Immich asset ID or URL
- download the original asset through the Immich API
- encode videos with SVT-AV1 in one pass at a configurable fixed CRF
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

- a dedicated FFmpeg build with SVT-AV1
- `exiftool`
- `ffmpeg`
- `ffprobe`

Copy `docker-compose.example.yml` into your Immich compose folder and adjust the volume
paths.

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
- an AV1 CRF quality setting
- concurrency
- review or automatic replacement mode

If you want to build locally from source instead, use `docker-compose.example.yml` and run:

```bash
docker compose up -d --build
```

## Settings

Connection, AV1 CRF, concurrency, and workflow mode are configured from the Settings page and stored in
`/data/immich-compress.sqlite`. Environment variables remain optional bootstrap fallbacks.

AV1 encoding defaults to CRF 28 and performs a single full encode without sampling or
comparison passes. Lower CRF values retain more quality and produce larger files; higher values
save more space. Select **Process All Unprocessed** on the Videos page to apply it to all existing videos.

Tool path variables such as `AV1_FFMPEG` and `EXIFTOOL` remain available for advanced deployments. The commands are
already included in the published Docker image.

## AV1 encoding

AV1 encoding uses CPU-based SVT-AV1 at the configured fixed CRF. No GPU passthrough is required.

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
