from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import effective_settings, settings
from app.immich import ImmichClient
from app.jobs import job_queue
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
    db.set_setting("replacement_mode", replacement_mode)
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
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"settings": effective_settings(), "job": job},
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
    output_path = safe_job_file(job["output_path"])
    current_settings = effective_settings()

    if current_settings.replacement_mode == "disabled":
        db.update_job(
            asset_id,
            state="accepted",
            error=None,
            logs=(job["logs"] or "") + "\nAccepted: compressed output kept locally; upload is disabled.",
        )
        return RedirectResponse(f"/jobs/{asset_id}", status_code=303)

    if current_settings.dry_run:
        db.update_job(
            asset_id,
            state="copy-dry-run",
            error=None,
            logs=(job["logs"] or "") + "\nDry run: would upload a compressed copy and copy Immich metadata.",
        )
        return RedirectResponse(f"/jobs/{asset_id}", status_code=303)

    try:
        client = ImmichClient(current_settings)
        asset = client.find_asset_by_id(asset_id)
        uploaded = client.upload_asset_copy(asset, output_path)
        target_asset_id = uploaded["id"]
        db.update_job(asset_id, target_asset_id=target_asset_id, state="copying")
        client.copy_asset_metadata(asset_id, target_asset_id)
        if current_settings.replacement_mode == "upload-trash":
            client.trash_asset(asset_id)
            state = "copied-and-trashed"
            log_line = (
                f"\nAccepted: uploaded compressed asset {target_asset_id}, copied Immich metadata, "
                "and trashed the original asset."
            )
        else:
            state = "copied"
            log_line = f"\nAccepted: uploaded compressed asset {target_asset_id} and copied Immich metadata."
        db.update_job(
            asset_id,
            target_asset_id=target_asset_id,
            state=state,
            error=None,
            logs=(job["logs"] or "") + log_line,
        )
    except Exception as exc:
        db.update_job(asset_id, state="copy-failed", error=str(exc))
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)


@app.post("/jobs/{asset_id}/reject")
def reject_job(asset_id: str):
    if not db.get_job(asset_id):
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job(asset_id, state="rejected")
    return RedirectResponse(f"/jobs/{asset_id}", status_code=303)
