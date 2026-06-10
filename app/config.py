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


def normalize_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_compression_mode(value: str) -> str:
    return "perceptual-av1" if value == "perceptual-av1" else "handbrake"


@dataclass(frozen=True)
class Settings:
    immich_url: str = os.environ.get("IMMICH_URL", "")
    immich_api_key: str = os.environ.get("IMMICH_API_KEY", "")
    data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
    upload_root: Path = Path(os.environ.get("IMMICH_UPLOAD_ROOT", "/immich-upload"))
    handbrake_cli: str = os.environ.get("HANDBRAKE_CLI", "HandBrakeCLI")
    ffmpeg: str = os.environ.get("FFMPEG", "ffmpeg")
    ffprobe: str = os.environ.get("FFPROBE", "ffprobe")
    exiftool: str = os.environ.get("EXIFTOOL", "exiftool")
    ab_av1: str = os.environ.get("AB_AV1", "ab-av1")
    perceptual_ffmpeg: str = os.environ.get("PERCEPTUAL_FFMPEG", "/opt/ab-av1/bin/ffmpeg")
    vmaf_model_dir: Path = Path(
        os.environ.get("VMAF_MODEL_DIR", "/opt/ab-av1/share/vmaf/model")
    )
    compression_mode: str = normalize_compression_mode(
        os.environ.get("COMPRESSION_MODE", "perceptual-av1")
    )
    video_score: int = env_int("VIDEO_SCORE", 95)
    handbrake_preset: str = os.environ.get("HANDBRAKE_PRESET", "")
    handbrake_encoder: str = os.environ.get("HANDBRAKE_ENCODER", "")
    video_taken_before: str = os.environ.get("VIDEO_TAKEN_BEFORE", "")
    poll_interval_seconds: int = env_int("POLL_INTERVAL_SECONDS", 300)
    auto_process_new_uploads: bool = env_bool("AUTO_PROCESS_NEW_UPLOADS", False)
    max_concurrent_jobs: int = env_int("MAX_CONCURRENT_JOBS", 1)
    upscale_to_4k: bool = env_bool("UPSCALE_TO_4K", False)
    min_savings_percent: int = env_int("MIN_SAVINGS_PERCENT", 20)
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
        compression_mode=normalize_compression_mode(
            db.get_setting("compression_mode", settings.compression_mode)
        ),
        video_score=int(db.get_setting("video_score", str(settings.video_score))),
        min_savings_percent=int(
            db.get_setting("min_savings_percent", str(settings.min_savings_percent))
        ),
        handbrake_preset=db.get_setting("handbrake_preset", settings.handbrake_preset),
        handbrake_encoder=db.get_setting("handbrake_encoder", settings.handbrake_encoder),
        video_taken_before=db.get_setting("video_taken_before", settings.video_taken_before),
        max_concurrent_jobs=max(
            1,
            int(db.get_setting("max_concurrent_jobs", str(settings.max_concurrent_jobs))),
        ),
        upscale_to_4k=normalize_bool(
            db.get_setting("upscale_to_4k", str(settings.upscale_to_4k))
        ),
        replacement_mode=normalize_mode(db.get_setting("replacement_mode", settings.replacement_mode)),
    )
