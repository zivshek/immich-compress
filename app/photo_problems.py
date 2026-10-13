from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import requests
from PIL import Image

from app import photo_problem_db as store
from app.config import effective_settings
from app.immich import ImmichClient, check_bulk_results


def is_jpeg(header: bytes) -> bool:
    return header.startswith(b"\xff\xd8\xff")


def photo_problem_view_asset_id(row) -> str | None:
    """Only link an original while the repair record says it can still exist."""
    if row["status"] in {"repairing", "scan-error"}:
        return None
    if row["target_asset_id"]:
        return row["target_asset_id"]
    if row["manifest"]:
        try:
            manifest = json.loads(row["manifest"])
        except (TypeError, json.JSONDecodeError):
            return None
        if manifest.get("target_id"):
            return manifest["target_id"]
        if manifest.get("stage") != "prepared":
            return None
    return row["asset_id"]


def safe_name(value: str) -> str:
    # Handle both POSIX and Windows paths, regardless of the deployment OS.
    name = value.replace("\\", "/").split("/")[-1]
    if name in {"", ".", ".."}:
        raise ValueError("Invalid original filename")
    return name


def repair_directory(asset_id: str, root: Path) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9-]+", asset_id):
        raise ValueError("Invalid asset ID")
    return root / "photo-repair" / asset_id


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha1").hexdigest()


def verify_download(path: Path, asset: dict) -> str:
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError("Downloaded original is empty")
    checksum = asset.get("checksum")
    if not checksum:
        raise RuntimeError("Immich did not supply a checksum; cannot verify the backup")
    try:
        expected = base64.b64decode(checksum, validate=True).hex()
    except ValueError as exc:
        raise RuntimeError("Unsupported Immich checksum") from exc
    actual = digest(path)
    if len(expected) != 40 or actual != expected:
        raise RuntimeError("Backup checksum does not match the Immich original")
    with path.open("r+b") as file:
        os.fsync(file.fileno())
    return actual


def verify_jpeg(path: Path) -> None:
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise RuntimeError("The original is not a JPEG; no repair performed")
        image.verify()
    with Image.open(path) as image:
        image.load()


def metadata_values(asset: dict) -> dict:
    values = {key: asset[key] for key in ("isFavorite", "visibility") if key in asset}
    # Older Immich versions use isArchived instead of visibility.
    if "visibility" not in values and "isArchived" in asset:
        values["isArchived"] = asset["isArchived"]
    exif = asset.get("exifInfo") or {}
    for key in ("description", "dateTimeOriginal", "rating"):
        if exif.get(key) is not None:
            values[key] = exif[key]
    if values.get("rating") == 0:
        values.pop("rating")
    if exif.get("latitude") is not None and exif.get("longitude") is not None:
        values.update(latitude=exif["latitude"], longitude=exif["longitude"])
    return values


def prepare_backup(client: ImmichClient, asset_id: str) -> dict:
    asset = client.find_asset_by_id(asset_id)
    if "livePhotoVideoId" not in asset:
        raise RuntimeError("Cannot verify Live Photo pairing; original was not changed")
    if asset.get("id") != asset_id or asset.get("type") != "IMAGE":
        raise RuntimeError("Asset identity does not match the photo selected for repair")
    store.update(
        asset_id,
        original_file_name=asset.get("originalFileName") or asset_id,
        live_photo_video_id=asset["livePhotoVideoId"],
        live_photo_verified=1,
    )
    if asset.get("isExternal"):
        raise RuntimeError("External-library originals must be repaired in their source library")
    if asset.get("isTrashed"):
        raise RuntimeError("Original is in the trash; restore it before repairing")
    if Path(asset.get("originalFileName") or "").suffix.lower() != ".heic":
        raise RuntimeError("Original no longer has a .heic extension")
    # These associations cannot be restored by copyAsset after a checksum-identical
    # original is deleted. Fail before deletion rather than silently lose them.
    if asset.get("stack") or asset.get("stackId"):
        raise RuntimeError("Unstack this photo before repair; its stack cannot be safely restored")
    directory = repair_directory(asset_id, client.config.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    photo = directory / safe_name(asset["originalFileName"])
    client.download_original(asset_id, photo)
    photo_hash = verify_download(photo, asset)
    verify_jpeg(photo)
    corrected = photo.with_suffix(".jpg")
    shutil.copyfile(photo, corrected)
    with corrected.open("r+b") as file:
        os.fsync(file.fileno())
    video_id = asset.get("livePhotoVideoId")
    video = None
    files = {"photo": {"path": str(corrected), "sha1": photo_hash}}
    if video_id:
        video = client.find_asset_by_id(video_id)
        if video.get("type") != "VIDEO" or video.get("isTrashed"):
            raise RuntimeError("Live Photo motion asset is missing or invalid")
        video_dir = directory / "motion"
        video_path = video_dir / safe_name(video.get("originalFileName") or f"{video_id}.mov")
        client.download_original(video_id, video_path)
        files["video"] = {"path": str(video_path), "sha1": verify_download(video_path, video)}
    albums = client.album_memberships([asset_id])[asset_id]
    shared_links = [
        link["id"]
        for link in client.request("GET", "shared-links")
        if not link.get("album")
        and not link.get("albumId")
        and any(item.get("id") == asset_id for item in link.get("assets", []))
    ]
    return {
        "server": client.config.immich_url.rstrip("/"),
        "asset": asset,
        "video": video,
        "files": files,
        "albums": albums,
        "shared_links": shared_links,
        "stage": "prepared",
    }


def validate_backup(client: ImmichClient, asset_id: str, manifest: dict) -> None:
    if manifest["server"] != client.config.immich_url.rstrip("/"):
        raise RuntimeError("Immich server changed; restore the original connection before retrying")
    directory = repair_directory(asset_id, client.config.data_dir).resolve()
    for item in manifest["files"].values():
        path = Path(item["path"]).resolve()
        if directory not in path.parents or digest(path) != item["sha1"]:
            raise RuntimeError("Repair backup is missing, changed, or outside its directory")
    verify_jpeg(Path(manifest["files"]["photo"]["path"]))


def upload_photo(client: ImmichClient, manifest: dict, video_id: str | None) -> str:
    asset = manifest["asset"]
    path = Path(manifest["files"]["photo"]["path"])
    # Permanent deletion runs in an Immich background queue. Its checksum may
    # remain reserved briefly. Never treat a duplicate of the old HEIC as success.
    for attempt in range(30):
        try:
            result = client.upload_asset_copy(asset, path, live_photo_video_id=video_id)
            target_id = result["id"]
            if target_id != asset["id"]:
                target = client.find_asset_by_id(target_id)
                if Path(target.get("originalFileName") or "").suffix.lower() not in {
                    ".jpg",
                    ".jpeg",
                }:
                    raise RuntimeError("Upload matched another mislabeled asset; backup retained")
                if target.get("isTrashed"):
                    raise RuntimeError("Upload matched a trashed asset; backup retained")
                if target.get("checksum") != asset["checksum"]:
                    raise RuntimeError(
                        "Uploaded JPEG checksum verification failed; backup retained"
                    )
                return target_id
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in {409, 500}:
                raise
        if attempt < 29:
            time.sleep(2)
    raise RuntimeError("Original checksum is still reserved. Retry after Immich finishes deletion.")


def repair_photo(client: ImmichClient, asset_id: str) -> None:
    row = store.get(asset_id)
    if not row or row["status"] == "repaired":
        return
    if row["source_server"] and row["source_server"] != client.config.immich_url.rstrip("/"):
        raise RuntimeError("This entry belongs to another API connection. Scan Library again.")
    manifest = json.loads(row["manifest"]) if row["manifest"] else prepare_backup(client, asset_id)
    validate_backup(client, asset_id, manifest)
    store.save_manifest(asset_id, manifest)
    asset = manifest["asset"]
    video = manifest["video"]
    if manifest["stage"] == "prepared":
        # Persist intent first: a lost HTTP response or restart can safely retry
        # detachment and deletion without downloading a now-deleted original.
        manifest["stage"] = "delete-requested"
        store.save_manifest(asset_id, manifest)
    if manifest["stage"] == "delete-requested":
        try:
            current = client.find_asset_by_id(asset_id)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in {400, 404}:
                raise
            current = None
        if current is not None:
            if current.get("checksum") != asset.get("checksum"):
                raise RuntimeError("Original changed since backup; deletion stopped")
            if current.get("livePhotoVideoId") not in {None, (video or {}).get("id")}:
                raise RuntimeError("Live Photo pairing changed since backup; deletion stopped")
            if video:
                client.update_asset(asset_id, {"livePhotoVideoId": None})
                detached = client.find_asset_by_id(asset_id)
                if detached.get("livePhotoVideoId"):
                    raise RuntimeError("Could not detach motion; original was not deleted")
            client.delete_asset_permanently(asset_id)
        manifest["stage"] = "deleted"
        store.save_manifest(asset_id, manifest)
    if manifest["stage"] == "deleted":
        video_id = None
        if video:
            uploaded = client.upload_asset_copy(video, Path(manifest["files"]["video"]["path"]))
            video_id = uploaded["id"]
            detail = client.find_asset_by_id(video_id)
            if detail.get("type") != "VIDEO" or detail.get("isTrashed"):
                raise RuntimeError("Re-uploaded motion is not available; backup retained")
            if detail.get("checksum") != video["checksum"]:
                raise RuntimeError(
                    "Re-uploaded motion checksum verification failed; backup retained"
                )
        manifest["video_target_id"] = video_id
        store.save_manifest(asset_id, manifest)
        target_id = upload_photo(client, manifest, video_id)
        manifest.update(stage="uploaded", target_id=target_id)
        store.save_manifest(asset_id, manifest)
        store.update(asset_id, target_asset_id=target_id)
    target_id = manifest["target_id"]
    values = metadata_values(asset)
    if manifest.get("video_target_id"):
        values["livePhotoVideoId"] = manifest["video_target_id"]
    if values:
        client.update_asset(target_id, values)
    for album in manifest["albums"]:
        client.add_asset_to_album(target_id, album["id"])
    for link_id in manifest.get("shared_links", []):
        check_bulk_results(
            client.request(
                "PUT",
                f"shared-links/{link_id}/assets",
                json={"assetIds": [target_id]},
            )
        )
    for tag in asset.get("tags", []):
        check_bulk_results(
            client.request(
                "PUT",
                f"tags/{tag['id']}/assets",
                json={"ids": [target_id]},
            )
        )
    restored = client.find_asset_by_id(target_id)
    if restored.get("livePhotoVideoId") != manifest.get("video_target_id"):
        raise RuntimeError("Live Photo pairing verification failed; backup retained")
    manifest["stage"] = "complete"
    store.save_manifest(asset_id, manifest)
    store.update(asset_id, status="repaired", target_asset_id=target_id, error=None)
    # Keep originals and manifest for recovery, even after successful replacement.


class PhotoProblemWorker:
    """One scan or repair batch at a time, with persistent per-photo recovery."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.running = False
        self.mode = ""
        self.scanned = 0
        self.found = 0
        self.current_file = ""
        self.last_error = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                key: getattr(self, key)
                for key in (
                    "running",
                    "mode",
                    "scanned",
                    "found",
                    "current_file",
                    "last_error",
                )
            }

    def start(self, asset_ids: list[str] | None = None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.mode = "scan" if asset_ids is None else "repair"
            self.scanned = self.found = 0
            self.current_file = self.last_error = ""
            self.cancel_event.clear()
        if asset_ids is not None:
            asset_ids = list(dict.fromkeys(asset_ids))
        threading.Thread(
            target=self._run, args=(asset_ids,), daemon=True, name="photo-problems"
        ).start()
        return True

    def clear(self) -> bool:
        with self.lock:
            if self.running:
                return False
            store.clear_scan_cache()
            return True

    def _set(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def _run(self, asset_ids: list[str] | None):
        try:
            client = ImmichClient(effective_settings())
            if asset_ids is None:
                self._scan(client)
            else:
                for asset_id in asset_ids:
                    if self.cancel_event.is_set():
                        break
                    row = store.get(asset_id)
                    if not row or row["status"] not in {"problem", "failed"}:
                        continue
                    self._set(current_file=row["original_file_name"])
                    store.update(asset_id, status="repairing", error=None)
                    try:
                        repair_photo(client, asset_id)
                    except Exception as exc:  # noqa: BLE001 -- persist failures at the job boundary
                        store.update(asset_id, status="failed", error=str(exc))
                        self._set(last_error=str(exc))
                    self._set(scanned=self.scanned + 1)
        except Exception as exc:  # noqa: BLE001 -- expose background worker failures in the UI
            self._set(last_error=str(exc))
        finally:
            self._set(running=False, current_file="")

    def _scan(self, client: ImmichClient):
        page = 1
        seen_ids: set[str] = set()
        server = client.config.immich_url.rstrip("/")
        while not self.cancel_event.is_set():
            assets = client.search_photos(page=page)
            if not assets:
                break
            for asset in assets:
                if self.cancel_event.is_set():
                    break
                if asset.get("isTrashed"):
                    continue
                if Path(asset.get("originalFileName") or "").suffix.lower() != ".heic":
                    continue
                seen_ids.add(asset["id"])
                row = store.get(asset["id"])
                if row and (row["manifest"] or row["status"] in {"repairing", "repaired"}):
                    continue
                if row and row["status"] == "clean" and row["source_server"] == server:
                    continue
                self._set(current_file=asset.get("originalFileName") or asset["id"])
                try:
                    detail = client.find_asset_by_id(asset["id"])
                    if detail.get("id") != asset["id"] or detail.get("type") != "IMAGE":
                        raise RuntimeError("Search record does not match the image asset details")
                    asset = detail
                    if asset.get("isTrashed"):
                        raise RuntimeError("Asset is now in trash; reset or refresh the scan")
                    if "livePhotoVideoId" not in asset:
                        raise RuntimeError(
                            "Asset details omit Live Photo pairing; repair unavailable"
                        )
                    header = client.original_header(asset["id"])
                    if not header:
                        raise RuntimeError("Empty original response")
                    if is_jpeg(header):
                        store.record(asset, "problem", server=server)
                        self._set(found=self.found + 1)
                    elif len(header) >= 12 and header[4:8] == b"ftyp":
                        store.record(asset, "clean", server=server)
                    else:
                        raise RuntimeError(
                            "Unknown file signature; automatic JPEG repair unavailable"
                        )
                except Exception as exc:  # noqa: BLE001 -- one bad asset must not abort the scan
                    store.record(asset, "scan-error", str(exc), server=server)
                    self._set(last_error=str(exc))
                self._set(scanned=self.scanned + 1)
            if len(assets) < 100:
                break
            page += 1
        if not self.cancel_event.is_set():
            store.finish_scan(seen_ids)


photo_problem_worker = PhotoProblemWorker()
