from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import effective_settings, normalize_compression_mode, normalize_mode, settings
from app.immich import ImmichClient
from app.jobs import (
    job_queue,
    mark_processed,
    reject_job as reject_work_job,
    upload_copy,
)
from app.tools import HandBrakeOption, handbrake_encoders, handbrake_presets, tool_statuses


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


def find_next_unprocessed_video_page(
    client: ImmichClient,
    page_size: int = 10,
    scan_size: int = 100,
) -> int | None:
    scanned = 0
    scan_page = 1
    while True:
        videos, total = client.search_videos(page=scan_page, size=scan_size)
        if not videos:
            return None
        jobs_by_asset = db.list_jobs_for_assets([video["id"] for video in videos])
        for index, video in enumerate(videos):
            if video["id"] not in jobs_by_asset:
                return ((scanned + index) // page_size) + 1
        scanned += len(videos)
        if len(videos) < scan_size or (total is not None and scanned >= total):
            return None
        scan_page += 1


def remembered_video_page(request: Request) -> int:
    try:
        return max(1, int(request.cookies.get("immich_compress_video_page", "1")))
    except ValueError:
        return 1


def videos_url(page: int, search: str = "") -> str:
    query = {"page": max(1, page)}
    if search:
        query["search"] = search
    return f"/videos?{urlencode(query)}"


def include_current_option(
    options: list[HandBrakeOption],
    current: str,
) -> list[HandBrakeOption]:
    if current and all(option.value != current for option in options):
        return [
            HandBrakeOption(current, "Configured value; not reported by this HandBrake installation."),
            *options,
        ]
    return options


def settings_page_context(message: str | None = None) -> dict[str, object]:
    current = effective_settings()
    return {
        "settings": current,
        "message": message,
        "presets": include_current_option(handbrake_presets(current), current.handbrake_preset),
        "encoders": include_current_option(handbrake_encoders(current), current.handbrake_encoder),
    }


def processing_is_configured() -> bool:
    current = effective_settings()
    compression_configured = current.compression_mode == "perceptual-av1" or bool(
        current.handbrake_preset and current.handbrake_encoder
    )
    return bool(current.immich_url and current.immich_api_key and compression_configured)


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
    return templates.TemplateResponse(
        request,
        "settings.html",
        settings_page_context(),
    )


@app.post("/settings")
def save_settings(
    request: Request,
    immich_url: str = Form(...),
    immich_api_key: str = Form(...),
    compression_mode: str = Form(...),
    video_score: int = Form(...),
    min_savings_percent: int = Form(...),
    handbrake_preset: str = Form(default=""),
    handbrake_encoder: str = Form(default=""),
    video_taken_before: str = Form(default=""),
    max_concurrent_jobs: int = Form(...),
    upscale_to_4k: str | None = Form(default=None),
    replacement_mode: str = Form(...),
):
    immich_url = immich_url.strip().rstrip("/")
    immich_api_key = immich_api_key.strip()
    video_taken_before = video_taken_before.strip()
    current = effective_settings()
    saved_api_key = immich_api_key or current.immich_api_key
    submitted = replace(
        current,
        immich_url=immich_url,
        immich_api_key=saved_api_key,
        compression_mode=normalize_compression_mode(compression_mode),
        video_score=video_score,
        min_savings_percent=min_savings_percent,
        handbrake_preset=handbrake_preset,
        handbrake_encoder=handbrake_encoder,
        video_taken_before=video_taken_before,
        max_concurrent_jobs=max_concurrent_jobs,
        upscale_to_4k=upscale_to_4k == "true",
        replacement_mode=normalize_mode(replacement_mode),
    )
    presets = include_current_option(handbrake_presets(current), current.handbrake_preset)
    encoders = include_current_option(handbrake_encoders(current), current.handbrake_encoder)
    errors = []
    if not immich_url:
        errors.append("Immich URL is required.")
    elif not immich_url.startswith(("http://", "https://")):
        errors.append("Immich URL must begin with http:// or https://.")
    if not saved_api_key:
        errors.append("Immich API key is required.")
    if video_taken_before:
        try:
            datetime.fromisoformat(video_taken_before.replace("Z", "+00:00"))
        except ValueError:
            errors.append("Video cutoff must be a valid date and time.")
    if compression_mode not in {"handbrake", "perceptual-av1"}:
        errors.append("Choose a supported compression strategy.")
    if compression_mode == "handbrake":
        if handbrake_preset not in {option.value for option in presets}:
            errors.append("Choose a preset reported by HandBrakeCLI.")
        if handbrake_encoder not in {option.value for option in encoders}:
            errors.append("Choose an encoder reported by HandBrakeCLI.")
    if not 1 <= video_score <= 100:
        errors.append("Video VMAF target must be between 1 and 100.")
    if not 1 <= min_savings_percent <= 99:
        errors.append("Minimum savings must be between 1 and 99 percent.")
    if not 1 <= max_concurrent_jobs <= 8:
        errors.append("Concurrent jobs must be between 1 and 8.")
    if errors:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "settings": submitted,
                "message": " ".join(errors),
                "presets": presets,
                "encoders": encoders,
            },
            status_code=400,
        )

    db.set_setting("immich_url", immich_url)
    if immich_api_key:
        db.set_setting("immich_api_key", immich_api_key)
    db.set_setting("compression_mode", normalize_compression_mode(compression_mode))
    db.set_setting("video_score", str(video_score))
    db.set_setting("min_savings_percent", str(min_savings_percent))
    db.set_setting("handbrake_preset", handbrake_preset)
    db.set_setting("handbrake_encoder", handbrake_encoder)
    db.set_setting("video_taken_before", video_taken_before)
    db.set_setting("max_concurrent_jobs", str(max_concurrent_jobs))
    db.set_setting("upscale_to_4k", str(upscale_to_4k == "true"))
    db.set_setting("replacement_mode", normalize_mode(replacement_mode))
    return templates.TemplateResponse(
        request,
        "settings.html",
        settings_page_context("Settings saved. New jobs will use these values."),
    )


@app.get("/jobs")
def jobs_page(request: Request, page: int = Query(default=1, ge=1)):
    queue_status = job_queue.snapshot()
    page_size = 25
    total = db.count_jobs()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "settings": effective_settings(),
            "jobs": db.list_jobs(page_size, (page - 1) * page_size),
            "queue": queue_status,
            "page": page,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page < total_pages,
        },
    )


@app.get("/videos")
def videos_page(
    request: Request,
    page: int | None = Query(default=None, ge=1),
    search: str = Query(default=""),
):
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
    client = ImmichClient()
    page_size = 10
    search = search.strip()
    if page is None:
        if search:
            return RedirectResponse(videos_url(1, search), status_code=303)
        target_page = find_next_unprocessed_video_page(client, page_size)
        return RedirectResponse(
            videos_url(target_page or remembered_video_page(request)),
            status_code=303,
        )

    videos, total = client.search_videos(page=page, size=page_size, file_name=search)
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
    response = templates.TemplateResponse(
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
            "search": search,
            "previous_url": videos_url(page - 1, search),
            "next_url": videos_url(page + 1, search),
        },
    )
    response.set_cookie(
        "immich_compress_video_page",
        str(page),
        max_age=31536000,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/jobs/process-asset")
def process_asset(asset: str = Form(...)):
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
    job_queue.enqueue(asset)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/videos/process-selected")
def process_selected(
    asset_ids: list[str] = Form(default=[]),
    page: int = Form(default=1),
    search: str = Form(default=""),
):
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
    redirect_url = videos_url(page, search.strip())
    if not asset_ids:
        return RedirectResponse(redirect_url, status_code=303)
    client = ImmichClient()
    for asset_id in asset_ids:
        asset = client.find_asset_by_id(asset_id)
        job_queue.enqueue_asset(asset)
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/videos/mark-processed")
def mark_selected_processed(
    asset_ids: list[str] = Form(default=[]),
    page: int = Form(default=1),
    search: str = Form(default=""),
):
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
    redirect_url = videos_url(page, search.strip())
    client = ImmichClient()
    for asset_id in asset_ids:
        asset = client.find_asset_by_id(asset_id)
        db.mark_asset_as_processed(asset)
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/videos/process-all")
def process_all_videos():
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
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


@app.post("/jobs/{asset_id}/retry")
def retry_job(asset_id: str):
    if not processing_is_configured():
        return RedirectResponse("/settings", status_code=303)
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    job_queue.retry(asset_id)
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
            "can_retry": job["state"] in {"failed", "rejected", "canceled"},
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
