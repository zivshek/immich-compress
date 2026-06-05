from __future__ import annotations

import re
import threading
from pathlib import Path
from queue import Queue

from app import db
from app.compression import compress_with_handbrake
from app.config import effective_settings, settings
from app.immich import ImmichClient


ASSET_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class JobQueue:
    def __init__(self) -> None:
        self.queue: Queue[str] = Queue()
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for index in range(max(1, settings.max_concurrent_jobs)):
            thread = threading.Thread(target=self._worker, name=f"compress-worker-{index}", daemon=True)
            thread.start()

    def enqueue(self, asset_id_or_url: str) -> str:
        asset_id = parse_asset_id(asset_id_or_url)
        client = ImmichClient()
        asset = client.find_asset_by_id(asset_id)
        db.upsert_job(asset_id, asset.get("originalFileName") or asset_id)
        self.queue.put(asset_id)
        return asset_id

    def _worker(self) -> None:
        while True:
            asset_id = self.queue.get()
            try:
                process_asset(asset_id)
            finally:
                self.queue.task_done()


def parse_asset_id(value: str) -> str:
    match = ASSET_ID_RE.search(value.strip())
    if not match:
        raise ValueError("Paste a valid Immich asset ID or asset URL")
    return match.group(0)


def process_asset(asset_id: str) -> None:
    config = effective_settings()
    client = ImmichClient(config)
    asset = client.find_asset_by_id(asset_id)
    original_name = asset.get("originalFileName") or f"{asset_id}.mp4"
    db.upsert_job(asset_id, original_name, "compressing")

    work_dir = config.data_dir / "work" / asset_id
    input_path = work_dir / original_name
    output_dir = work_dir / "compressed"
    try:
        client.download_original(asset_id, input_path)
        result = compress_with_handbrake(input_path, output_dir, config)
        db.update_job(
            asset_id,
            state="review",
            original_path=str(input_path),
            output_path=str(result.output_path),
            original_size=result.original_size,
            compressed_size=result.compressed_size,
            saved_bytes=result.saved_bytes,
            logs=result.logs[-10000:],
            error=None,
        )
        if config.replacement_mode == "auto":
            try:
                upload_copy(asset_id, trash_original=True)
            except Exception as exc:
                db.update_job(asset_id, state="copy-failed", error=str(exc))
    except Exception as exc:
        db.update_job(asset_id, state="failed", error=str(exc))


def upload_copy(asset_id: str, *, trash_original: bool = False) -> str:
    config = effective_settings()
    job = db.get_job(asset_id)
    if not job:
        raise RuntimeError("Job not found")
    if job["target_asset_id"]:
        target_asset_id = job["target_asset_id"]
    else:
        output_path = Path(job["output_path"])
        if not output_path.is_file():
            raise RuntimeError("Compressed output file is not available")
        client = ImmichClient(config)
        source_asset = client.find_asset_by_id(asset_id)
        uploaded = client.upload_asset_copy(source_asset, output_path)
        target_asset_id = uploaded["id"]
        db.update_job(asset_id, target_asset_id=target_asset_id, state="copying")
        client.copy_asset_metadata(asset_id, target_asset_id)

    if trash_original:
        refreshed_job = db.get_job(asset_id)
        db.update_job(
            asset_id,
            target_asset_id=target_asset_id,
            state="copied",
            error=None,
            logs=(refreshed_job["logs"] if refreshed_job else job["logs"] or "")
            + f"\nUploaded compressed asset {target_asset_id} and copied Immich metadata.",
        )
        trash_original_asset(asset_id)
        return target_asset_id

    db.update_job(
        asset_id,
        target_asset_id=target_asset_id,
        state="copied",
        error=None,
        logs=(job["logs"] or "") + f"\nUploaded compressed asset {target_asset_id} and copied Immich metadata.",
    )
    return target_asset_id


def trash_original_asset(asset_id: str) -> None:
    config = effective_settings()
    job = db.get_job(asset_id)
    if not job:
        raise RuntimeError("Job not found")
    if not job["target_asset_id"]:
        raise RuntimeError("Upload the compressed copy before trashing the original")

    client = ImmichClient(config)
    client.trash_asset(asset_id)
    db.update_job(
        asset_id,
        state="copied-and-trashed",
        error=None,
        logs=(job["logs"] or "")
        + f"\nTrashed original asset after uploading compressed asset {job['target_asset_id']}.",
    )


job_queue = JobQueue()
