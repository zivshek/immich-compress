from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import effective_settings, normalize_mode, settings
from app.immich import ImmichClient
from app.jobs import job_queue, mark_processed, reject_job as reject_work_job, upload_copy
from app.tools import tool_statuses


app = FastAPI(title="Immich Compress")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def safe_job_file(path_value: str | None) -> Path:
    if not path_value:
        raise HTTPException(status_code=404, detail="File is not available")
    path = Path(path_value).resolve()
    data_dir = effective_settings().data_dir.resolve()
    if data_dir not in path.parents and path != data_dir:
        raise HTTPException(status_code=403, detail="File is outside the app data directory")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File does not exist")
    return path


def asset_info_for_page(asset: dict) -> dict[str, object]:
    exif = asset.get("exifInfo") or {}
    city_parts = [
        exif.get("city"),
        exif.get("state"),
        exif.get("country"),
    ]
    location_name = ", ".join(part for part in city_parts if part)
    latitude = exif.get("latitude")
    longitude = exif.get("longitude")
    coordinates = ""
    if latitude is not None and longitude is not None:
        coordinates = f"{latitude}, {longitude}"

    camera = " ".join(part for part in [exif.get("make"), exif.get("model")] if part)
    dimensions = " x ".join(
        str(part)
        for part in [
            exif.get("exifImageWidth") or asset.get("exifInfo", {}).get("exifImageWidth"),
            exif.get("exifImageHeight") or asset.get("exifInfo", {}).get("exifImageHeight"),
        ]
        if part
    )
    size = asset.get("originalFileSize") or asset.get("fileSizeInByte")
    file_line_parts = [dimensions, format_bytes(size) if size else ""]
    file_line = "  ".join(part for part in file_line_parts if part)
    lens = exif.get("lensModel") or ""
    lens_line_parts = [
        f"f/{exif.get('fNumber')}" if exif.get("fNumber") else "",
        f"{exif.get('focalLength')} mm" if exif.get("focalLength") else "",
    ]
    return {
        "date_time": asset.get("localDateTime") or asset.get("fileCreatedAt") or "",
        "location": location_name or coordinates,
        "coordinates": coordinates,
        "camera": camera,
        "camera_settings": "  ".join(
            part
            for part in [
                exif.get("exposureTime"),
                f"ISO {exif.get('iso')}" if exif.get("iso") else "",
            ]
            if part
        ),
        "lens": lens,
        "lens_settings": "  ".join(part for part in lens_line_parts if part),
        "duration": asset.get("duration") or "",
        "file_name": asset.get("originalFileName") or "",
        "file_summary": file_line,
        "original_file_size": format_bytes(size) if size else "",
    }


def format_bytes(value: object) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f} {units[unit]}"


def asset_size_bytes(asset: dict) -> object | None:
    exif = asset.get("exifInfo") or {}
    return (
        asset.get("originalFileSize")
        or asset.get("fileSizeInByte")
        or exif.get("fileSizeInByte")
        or exif.get("fileSize")
    )


def asset_resolution(asset: dict) -> str:
    exif = asset.get("exifInfo") or {}
    width = (
        asset.get("width")
        or exif.get("exifImageWidth")
        or exif.get("imageWidth")
    )
    height = (
        asset.get("height")
        or exif.get("exifImageHeight")
        or exif.get("imageHeight")
    )
    if width and height:
        return f"{width} x {height}"
    return "-"


def enrich_video_assets(videos: list[dict]) -> list[dict]:
    def fetch(video: dict) -> dict:
        try:
            detail = ImmichClient().find_asset_by_id(video["id"])
            return {**video, **detail}
        except Exception:
            return video

    with ThreadPoolExecutor(max_workers=min(5, len(videos) or 1)) as executor:
        return list(executor.map(fetch, videos))


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    job_queue.start()


@app.get("/")
def dashboard(request: Request):
    jobs = db.list_jobs(25)
    current_settings = effective_settings()
    client = ImmichClient()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "settings": current_settings,
            "jobs": jobs,
            "stats": db.get_dashboard_stats(),
            "immich_connected": client.ping() if current_settings.immich_api_key else False,
            "tools": tool_statuses(current_settings),
        },
    )


@app.get("/settings")
def settings_page(request: Request):
    current_settings = effective_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": current_settings,
            "message": None,
        },
    )


@app.post("/settings")
def save_settings(
    request: Request,
    handbrake_preset: str = Form(...),
    handbrake_encoder: str = Form(...),
    replacement_mode: str = Form(...),
):
    db.set_setting("handbrake_preset", handbrake_preset)
    db.set_setting("handbrake_encoder", handbrake_encoder)
    db.set_setting("replacement_mode", normalize_mode(replacement_mode))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": effective_settings(),
            "message": "Settings saved. New jobs will use these values.",
        },
    )


@app.get("/jobs")
def jobs_page(request: Request):
    queue_status = job_queue.snapshot()
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"settings": effective_settings(), "jobs": db.list_jobs(200), "queue": queue_status},
    )


@app.get("/videos")
def videos_page(request: Request, page: int = Query(1, ge=1)):
    client = ImmichClient()
    page_size = 10
    videos, total = client.search_videos(page=page, size=page_size)
    videos = enrich_video_assets(videos)
    jobs_by_asset = db.list_jobs_for_assets([video["id"] for video in videos])
    rows = []
    for video in videos:
        job = jobs_by_asset.get(video["id"])
        is_copied_asset = bool(job and job["target_asset_id"] == video["id"])
        original_size = job["original_size"] if job and job["original_size"] is not None else None
        if original_size is None and not is_copied_asset:
            original_size = asset_size_bytes(video)
        compressed_size = job["compressed_size"] if job and job["compressed_size"] is not None else None
        rows.append(
            {
                "asset": video,
                "job": job,
                "state": job["state"] if job else "unprocessed",
                "size": format_bytes(original_size),
                "compressed_size": format_bytes(compressed_size),
                "resolution": asset_resolution(video),
                "date": video.get("localDateTime") or video.get("fileCreatedAt") or "",
            }
        )
    return templates.TemplateResponse(
        request,
        "videos.html",
        {
            "settings": effective_settings(),
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_next": len(videos) == page_size,
            "has_previous": page > 1,
        },
    )


@app.post("/jobs/process-asset")
def process_asset(asset: str = Form(...)):
    job_queue.enqueue(asset)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/videos/process-selected")
def process_selected(asset_ids: list[str] = Form(default=[])):
    if not asset_ids:
        return RedirectResponse("/videos", status_code=303)
    client = ImmichClient()
    for asset_id in asset_ids:
        asset = client.find_asset_by_id(asset_id)
        job_queue.enqueue_asset(asset)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/videos/process-all")
def process_all_videos():
    client = ImmichClient()
    all_assets: list[dict] = []
    page = 1
    while True:
        videos, _ = client.search_videos(page=page, size=100)
        if not videos:
            break
        all_assets.extend(videos)
        if len(videos) < 100:
            break
        page += 1

    for asset in all_assets:
        job_queue.enqueue_asset(asset)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/cancel-all")
def cancel_all_jobs():
    job_queue.cancel_all()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{asset_id}/cancel")
def cancel_job(asset_id: str):
    job_queue.cancel_job(asset_id)
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.get("/jobs/{asset_id}")
def job_detail(request: Request, asset_id: str):
    job = db.get_job(asset_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    asset_info = {}
    try:
        client = ImmichClient()
        asset = client.find_asset_by_id(asset_id)
        asset_info = asset_info_for_page(asset)
    except Exception:
        asset_info = {}
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "settings": effective_settings(),
            "job": job,
            "asset_info": asset_info,
            "can_cancel": job["state"] in {"pending", "compressing"},
        },
    )


@app.get("/jobs/{asset_id}/files/{kind}")
def job_file(asset_id: str, kind: str):
    job = db.get_job(asset_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if kind == "original":
        path = safe_job_file(job["original_path"])
    elif kind == "compressed":
        path = safe_job_file(job["output_path"])
    else:
        raise HTTPException(status_code=404, detail="File kind not found")
    return FileResponse(path, filename=path.name)


@app.post("/jobs/{asset_id}/accept")
def accept_job(asset_id: str):
    job = db.get_job(asset_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        upload_copy(asset_id, trash_original=True)
    except Exception as exc:
        db.update_job(asset_id, state="copy-failed", error=str(exc))
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.post("/jobs/{asset_id}/mark-processed")
def mark_processed_job(asset_id: str):
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        mark_processed(asset_id)
    except Exception as exc:
        db.update_job(asset_id, state="failed", error=str(exc))
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.post("/jobs/{asset_id}/reject")
def reject_job(asset_id: str):
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    reject_work_job(asset_id)
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)
