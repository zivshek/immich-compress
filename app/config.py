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
    immich_url: str = os.environ.get("IMMICH_URL", "http://immich-server:2283")
    immich_api_key: str = os.environ.get("IMMICH_API_KEY", "")
    data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
    upload_root: Path = Path(os.environ.get("IMMICH_UPLOAD_ROOT", "/immich-upload"))
    handbrake_cli: str = os.environ.get("HANDBRAKE_CLI", "HandBrakeCLI")
    ffmpeg: str = os.environ.get("FFMPEG", "ffmpeg")
    ffprobe: str = os.environ.get("FFPROBE", "ffprobe")
    exiftool: str = os.environ.get("EXIFTOOL", "exiftool")
    handbrake_preset: str = os.environ.get("HANDBRAKE_PRESET", "Fast 2160p60 4K HEVC")
    handbrake_encoder: str = os.environ.get("HANDBRAKE_ENCODER", "nvenc_h265")
    poll_interval_seconds: int = env_int("POLL_INTERVAL_SECONDS", 300)
    auto_process_new_uploads: bool = env_bool("AUTO_PROCESS_NEW_UPLOADS", False)
    max_concurrent_jobs: int = env_int("MAX_CONCURRENT_JOBS", 1)
    upscale_to_4k: bool = env_bool("UPSCALE_TO_4K", False)
    min_savings_percent: int = env_int("MIN_SAVINGS_PERCENT", 5)
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
        handbrake_preset=db.get_setting("handbrake_preset", settings.handbrake_preset),
        handbrake_encoder=db.get_setting("handbrake_encoder", settings.handbrake_encoder),
        replacement_mode=normalize_mode(db.get_setting("replacement_mode", settings.replacement_mode)),
    )
