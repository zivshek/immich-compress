from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from queue import Empty, Queue

from app import db
from app.compression import compress_video
from app.config import effective_settings
from app.immich import ImmichClient


ASSET_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LEGACY_PROCESSED_SUFFIX = "-hbed"
RETRYABLE_STATES = {"failed", "rejected", "canceled"}


class JobQueue:
    def __init__(self) -> None:
        self.queue: Queue[str] = Queue()
        self.started = False
        self.lock = threading.Lock()
        self.queued_ids: set[str] = set()
        self.active: dict[str, threading.Event] = {}
        self.run_total = 0
        self.run_completed = 0

    def start(self) -> None:
        if self.started:
            return
        for job in db.list_jobs_by_states({"pending", "compressing", "copying"}):
            mark_canceled(job["asset_id"], "Canceled after the app restarted.")
        self.started = True
        for index in range(max(1, effective_settings().max_concurrent_jobs)):
            thread = threading.Thread(target=self._worker, name=f"compress-worker-{index}", daemon=True)
            thread.start()

    def enqueue(self, asset_id_or_url: str) -> str:
        asset_id = parse_asset_id(asset_id_or_url)
        client = ImmichClient()
        asset = client.find_asset_by_id(asset_id)
        self.enqueue_asset(asset)
        return asset_id

    def enqueue_asset(self, asset: dict) -> bool:
        asset_id = asset["id"]
        with self.lock:
            if asset_id in self.queued_ids or asset_id in self.active:
                return False
            job = db.get_job_for_asset(asset_id)
            if job and job["state"] not in RETRYABLE_STATES:
                return False
            if not self.queued_ids and not self.active:
                self.run_total = 0
                self.run_completed = 0
            db.upsert_job(asset_id, asset.get("originalFileName") or asset_id)
            self.queued_ids.add(asset_id)
            self.run_total += 1
            self.queue.put(asset_id)
        return True

    def retry(self, asset_id: str) -> bool:
        job = db.get_job(asset_id)
        if not job or job["state"] not in RETRYABLE_STATES:
            return False
        asset = ImmichClient().find_asset_by_id(asset_id)
        cleanup_work_dir(asset_id)
        db.update_job(
            asset_id,
            original_path=None,
            output_path=None,
            target_asset_id=None,
            original_size=None,
            compressed_size=None,
            saved_bytes=None,
            progress_stage="Retrying",
            progress_percent=0,
            process_started_at=None,
            error=None,
            logs=((job["logs"] or "") + "\nRetry requested.").strip(),
        )
        return self.enqueue_asset(asset)

    def snapshot(self) -> dict[str, object] | None:
        with self.lock:
            active_ids = list(self.active)
            queued_ids = list(self.queued_ids)
            total = self.run_total
            completed = self.run_completed
        if not active_ids and not queued_ids:
            return None
        active_jobs = [job for asset_id in active_ids if (job := db.get_job(asset_id))]
        queued_jobs = [job for asset_id in queued_ids if (job := db.get_job(asset_id))]
        active_progress = sum((job["progress_percent"] or 0) / 100 for job in active_jobs)
        percent = ((completed + active_progress) / total * 100) if total else 0
        return {
            "total": total,
            "completed": completed,
            "percent": percent,
            "active": active_jobs,
            "queued": queued_jobs,
        }

    def cancel_job(self, asset_id: str) -> bool:
        with self.lock:
            cancel_event = self.active.get(asset_id)
            if cancel_event:
                cancel_event.set()
                return True
            if asset_id in self.queued_ids:
                self.queued_ids.remove(asset_id)
                self.run_completed += 1
                mark_canceled(asset_id)
                return True
        return False

    def cancel_all(self) -> None:
        with self.lock:
            for cancel_event in self.active.values():
                cancel_event.set()
            queued_ids = set(self.queued_ids)
            self.queued_ids.clear()
            self.run_total = len(self.active)
            self.run_completed = 0
        for asset_id in queued_ids:
            mark_canceled(asset_id)
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Empty:
                break

    def _worker(self) -> None:
        while True:
            asset_id = self.queue.get()
            activated = False
            try:
                with self.lock:
                    if asset_id not in self.queued_ids:
                        continue
                    self.queued_ids.remove(asset_id)
                    cancel_event = threading.Event()
                    self.active[asset_id] = cancel_event
                    activated = True
                job = db.get_job(asset_id)
                if job and job["state"] == "pending":
                    try:
                        process_asset(asset_id, cancel_event)
                    except Exception as exc:
                        db.update_job(asset_id, state="failed", error=str(exc))
            finally:
                with self.lock:
                    self.active.pop(asset_id, None)
                    if activated:
                        self.run_completed += 1
                self.queue.task_done()


def parse_asset_id(value: str) -> str:
    match = ASSET_ID_RE.search(value.strip())
    if not match:
        raise ValueError("Paste a valid Immich asset ID or asset URL")
    return match.group(0)


def process_asset(asset_id: str, cancel_event: threading.Event) -> None:
    config = effective_settings()
    client = ImmichClient(config)
    asset = client.find_asset_by_id(asset_id)
    raise_if_canceled(cancel_event)
    original_name = asset.get("originalFileName") or f"{asset_id}.mp4"
    db.update_job(asset_id, process_started_at=db.utc_now())
    if is_legacy_processed_filename(original_name):
        current_size = asset.get("originalFileSize") or asset.get("fileSizeInByte")
        db.upsert_job(asset_id, original_name, "processed")
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

    db.upsert_job(asset_id, original_name, "compressing")

    work_dir = config.data_dir / "work" / asset_id
    input_path = work_dir / original_name
    output_dir = work_dir / "compressed"
    try:
        client.download_original(asset_id, input_path, cancel_event.is_set)
        raise_if_canceled(cancel_event)
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

        result = compress_video(
            input_path,
            output_dir,
            config,
            progress,
            cancel_event.is_set,
        )
        raise_if_canceled(cancel_event)
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
        raise_if_canceled(cancel_event)
        if config.replacement_mode == "auto":
            try:
                upload_copy(asset_id, trash_original=True)
            except Exception as exc:
                db.update_job(asset_id, state="copy-failed", error=str(exc))
    except InterruptedError:
        mark_canceled(asset_id)
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


def raise_if_canceled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Job canceled")


def mark_canceled(asset_id: str, message: str = "Canceled and deleted local work files.") -> None:
    cleanup_work_dir(asset_id)
    job = db.get_job(asset_id)
    if not job:
        return
    db.update_job(
        asset_id,
        state="canceled",
        original_path=None,
        output_path=None,
        progress_stage="Canceled",
        error=None,
        logs=(job["logs"] or "") + f"\n{message}",
    )


def is_legacy_processed_filename(file_name: str) -> bool:
    return Path(file_name).stem.lower().endswith(LEGACY_PROCESSED_SUFFIX)


job_queue = JobQueue()
