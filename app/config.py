from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    return int(value)


def normalize_mode(value: str) -> str:
    if value == "auto" or value == "upload-trash":
        return "auto"
    return "review"


@dataclass(frozen=True)
class Settings:
    immich_url: str = os.environ.get("IMMICH_URL", "")
    immich_api_key: str = os.environ.get("IMMICH_API_KEY", "")
    data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
    upload_root: Path = Path(os.environ.get("IMMICH_UPLOAD_ROOT", "/immich-upload"))
    ffmpeg: str = os.environ.get("FFMPEG", "ffmpeg")
    ffprobe: str = os.environ.get("FFPROBE", "ffprobe")
    exiftool: str = os.environ.get("EXIFTOOL", "exiftool")
    av1_ffmpeg: str = os.environ.get("AV1_FFMPEG", "/opt/av1/bin/ffmpeg")
    video_crf: int = env_int("VIDEO_CRF", 28)
    video_taken_before: str = os.environ.get("VIDEO_TAKEN_BEFORE", "")
    poll_interval_seconds: int = env_int("POLL_INTERVAL_SECONDS", 300)
    auto_process_new_uploads: bool = env_bool("AUTO_PROCESS_NEW_UPLOADS", False)
    max_concurrent_jobs: int = env_int("MAX_CONCURRENT_JOBS", 1)
    replacement_mode: str = normalize_mode(os.environ.get("REPLACEMENT_MODE", "review"))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "immich-compress.sqlite"


settings = Settings()


def effective_settings() -> Settings:
    """Return settings with UI-saved database overrides applied."""
    from app import db

    return replace(
        settings,
        immich_url=db.get_setting("immich_url", settings.immich_url),
        immich_api_key=db.get_setting("immich_api_key", settings.immich_api_key),
        video_crf=int(db.get_setting("video_crf", str(settings.video_crf))),
        video_taken_before=db.get_setting("video_taken_before", settings.video_taken_before),
        max_concurrent_jobs=max(
            1,
            int(db.get_setting("max_concurrent_jobs", str(settings.max_concurrent_jobs))),
        ),
        replacement_mode=normalize_mode(db.get_setting("replacement_mode", settings.replacement_mode)),
    )
