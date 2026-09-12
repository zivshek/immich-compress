from __future__ import annotations

import shutil
import threading
from pathlib import Path

from app import ios_problem_db
from app.config import effective_settings
from app.immich import ImmichClient
from app.ios_repair import analyze_ios_compatibility, probe_media


class IosProblemScanner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.cancel_event = threading.Event()
        self.scanned = 0
        self.found = 0
        self.skipped = 0
        self.total: int | None = None
        self.current_file = ""
        self.last_error = ""

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.cancel_event.clear()
            self.scanned = 0
            self.found = 0
            self.skipped = 0
            self.total = None
            self.current_file = ""
            self.last_error = ""
        thread = threading.Thread(target=self._run, name="ios-problem-scanner", daemon=True)
        thread.start()
        return True

    def cancel(self) -> None:
        self.cancel_event.set()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            total = self.total
            scanned = self.scanned
            percent = (scanned / total * 100) if total else None
            return {
                "running": self.running,
                "scanned": scanned,
                "found": self.found,
                "skipped": self.skipped,
                "total": total,
                "percent": percent,
                "current_file": self.current_file,
                "last_error": self.last_error,
            }

    def _run(self) -> None:
        config = effective_settings()
        scan_root = config.data_dir / "ios-scan"
        try:
            scan_root.mkdir(parents=True, exist_ok=True)
            client = ImmichClient(config)
            page = 1
            while not self.cancel_event.is_set():
                videos, total = client.search_videos(page=page, size=100)
                if total is not None:
                    self._set(total=total)
                if not videos:
                    break
                for asset in videos:
                    if self.cancel_event.is_set():
                        break
                    self._scan_asset(client, asset, scan_root)
                if len(videos) < 100:
                    break
                page += 1
        except Exception as exc:
            self._set(last_error=str(exc))
        finally:
            shutil.rmtree(scan_root, ignore_errors=True)
            with self.lock:
                self.running = False
                self.current_file = ""

    def _scan_asset(self, client: ImmichClient, asset: dict, scan_root: Path) -> None:
        asset_id = asset["id"]
        original_name = asset.get("originalFileName") or f"{asset_id}.mp4"
        self._set(current_file=original_name)
        if ios_problem_db.has_row(asset_id):
            self._increment(scanned=1, skipped=1)
            return
        asset_dir = scan_root / asset_id
        input_path = asset_dir / original_name
        try:
            client.download_original(asset_id, input_path, self.cancel_event.is_set)
            probe = probe_media(input_path, client.config)
            analysis = analyze_ios_compatibility(probe)
            if analysis.needs_repair:
                ios_problem_db.upsert_problem(asset, probe, analysis)
                self._increment(found=1)
            else:
                ios_problem_db.mark_not_problem(asset_id)
        except InterruptedError:
            raise
        except Exception as exc:
            ios_problem_db.upsert_scan_error(asset, str(exc))
            self._set(last_error=f"{original_name}: {exc}")
        finally:
            shutil.rmtree(asset_dir, ignore_errors=True)
            self._increment(scanned=1)

    def _set(self, **values: object) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def _increment(self, *, scanned: int = 0, found: int = 0, skipped: int = 0) -> None:
        with self.lock:
            self.scanned += scanned
            self.found += found
            self.skipped += skipped


ios_problem_scanner = IosProblemScanner()
