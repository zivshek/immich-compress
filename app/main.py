from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import effective_settings, normalize_mode, settings
from app.immich import ImmichClient
from app.jobs import job_queue, trash_original_asset, upload_copy
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


def asset_info_for_page(asset: dict, album_names: list[str]) -> dict[str, str]:
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
    size = asset.get("originalFileSize") or asset.get("fileSizeInByte")
    return {
        "date_time": asset.get("localDateTime") or asset.get("fileCreatedAt") or "",
        "location": location_name or coordinates,
        "coordinates": coordinates,
        "camera": camera,
        "albums": ", ".join(album_names),
        "duration": asset.get("duration") or "",
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
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"settings": effective_settings(), "jobs": db.list_jobs(200)},
    )


@app.post("/jobs/process-asset")
def process_asset(asset: str = Form(...)):
    job_queue.enqueue(asset)
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs/{asset_id}")
def job_detail(request: Request, asset_id: str):
    job = db.get_job(asset_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    asset_info = {}
    try:
        client = ImmichClient()
        asset = client.find_asset_by_id(asset_id)
        asset_info = asset_info_for_page(asset, client.album_names_for_asset(asset_id))
    except Exception:
        asset_info = {}
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"settings": effective_settings(), "job": job, "asset_info": asset_info},
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
def upload_job(asset_id: str):
    job = db.get_job(asset_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    current_settings = effective_settings()

    try:
        upload_copy(asset_id, trash_original=current_settings.replacement_mode == "auto")
    except Exception as exc:
        db.update_job(asset_id, state="copy-failed", error=str(exc))
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.post("/jobs/{asset_id}/trash-original")
def trash_original_job(asset_id: str):
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        trash_original_asset(asset_id)
    except Exception as exc:
        db.update_job(asset_id, state="trash-failed", error=str(exc))
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.post("/jobs/{asset_id}/reject")
def reject_job(asset_id: str):
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job(asset_id, state="rejected")
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)
