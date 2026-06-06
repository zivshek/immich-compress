from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from queue import Queue
from dataclasses import dataclass

from app import db
from app.compression import compress_with_handbrake
from app.config import effective_settings, settings
from app.immich import ImmichClient


ASSET_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LEGACY_PROCESSED_SUFFIX = "-hbed"


@dataclass(frozen=True)
class QueuedJob:
    asset_id: str
    batch_id: str | None = None


class JobQueue:
    def __init__(self) -> None:
        self.queue: Queue[QueuedJob] = Queue()
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for index in range(max(1, settings.max_concurrent_jobs)):
            thread = threading.Thread(target=self._worker, name=f"compress-worker-{index}", daemon=True)
            thread.start()

    def enqueue(self, asset_id_or_url: str, batch_id: str | None = None) -> str:
        asset_id = parse_asset_id(asset_id_or_url)
        client = ImmichClient()
        asset = client.find_asset_by_id(asset_id)
        db.upsert_job(asset_id, asset.get("originalFileName") or asset_id, batch_id=batch_id)
        self.queue.put(QueuedJob(asset_id=asset_id, batch_id=batch_id))
        return asset_id

    def enqueue_asset(self, asset: dict, batch_id: str | None = None) -> str:
        asset_id = asset["id"]
        db.upsert_job(asset_id, asset.get("originalFileName") or asset_id, batch_id=batch_id)
        self.queue.put(QueuedJob(asset_id=asset_id, batch_id=batch_id))
        return asset_id

    def _worker(self) -> None:
        while True:
            queued = self.queue.get()
            try:
                process_asset(queued.asset_id, queued.batch_id)
            finally:
                self.queue.task_done()


def parse_asset_id(value: str) -> str:
    match = ASSET_ID_RE.search(value.strip())
    if not match:
        raise ValueError("Paste a valid Immich asset ID or asset URL")
    return match.group(0)


def process_asset(asset_id: str, batch_id: str | None = None) -> None:
    config = effective_settings()
    client = ImmichClient(config)
    asset = client.find_asset_by_id(asset_id)
    original_name = asset.get("originalFileName") or f"{asset_id}.mp4"
    if is_legacy_processed_filename(original_name):
        current_size = asset.get("originalFileSize") or asset.get("fileSizeInByte")
        db.upsert_job(asset_id, original_name, "processed", batch_id=batch_id)
        db.update_job(
            asset_id,
            compressed_size=current_size,
            progress_stage="Already processed",
            progress_percent=100,
            logs=(
                f"Skipped compression because {original_name} already ends with "
                f"{LEGACY_PROCESSED_SUFFIX}."
            ),
            error=None,
        )
        return

    db.upsert_job(asset_id, original_name, "compressing", batch_id=batch_id)

    work_dir = config.data_dir / "work" / asset_id
    input_path = work_dir / original_name
    output_dir = work_dir / "compressed"
    try:
        client.download_original(asset_id, input_path)
        original_size = input_path.stat().st_size
        db.update_job(
            asset_id,
            original_path=str(input_path),
            original_size=original_size,
            progress_stage="Downloaded",
            progress_percent=0,
            logs=f"Downloaded original: {original_size / 1048576:.1f} MB",
        )

        def progress(stage: str, percent: float | None, line: str | None) -> None:
            values: dict[str, object] = {"progress_stage": stage}
            if percent is not None:
                values["progress_percent"] = max(0, min(100, percent))
            db.update_job(asset_id, **values)
            if line:
                db.append_job_log(asset_id, line)

        result = compress_with_handbrake(input_path, output_dir, config, progress)
        db.update_job(
            asset_id,
            state="review",
            original_path=str(input_path),
            output_path=str(result.output_path),
            original_size=result.original_size,
            compressed_size=result.compressed_size,
            saved_bytes=result.saved_bytes,
            progress_stage="Review",
            progress_percent=100,
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
    cleanup_work_dir(asset_id)
    db.update_job(asset_id, original_path=None, output_path=None)
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
    cleanup_work_dir(asset_id)
    db.update_job(asset_id, original_path=None, output_path=None)


def mark_processed(asset_id: str) -> None:
    job = db.get_job(asset_id)
    if not job:
        raise RuntimeError("Job not found")
    db.update_job(
        asset_id,
        state="processed",
        progress_stage="Already processed",
        progress_percent=100,
        error=None,
        logs=(job["logs"] or "") + "\nMarked as already processed.",
    )
    cleanup_work_dir(asset_id)
    db.update_job(asset_id, original_path=None, output_path=None)


def reject_job(asset_id: str) -> None:
    job = db.get_job(asset_id)
    if not job:
        raise RuntimeError("Job not found")
    cleanup_work_dir(asset_id)
    db.update_job(
        asset_id,
        state="rejected",
        original_path=None,
        output_path=None,
        progress_stage="Rejected",
        error=None,
        logs=(job["logs"] or "") + "\nRejected and deleted local work files.",
    )


def cleanup_work_dir(asset_id: str) -> None:
    work_dir = effective_settings().data_dir / "work" / asset_id
    if work_dir.exists():
        shutil.rmtree(work_dir)


def is_legacy_processed_filename(file_name: str) -> bool:
    return Path(file_name).stem.lower().endswith(LEGACY_PROCESSED_SUFFIX)


job_queue = JobQueue()
