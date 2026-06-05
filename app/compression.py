from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, settings


SUPPORTED_VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    rotation: int


@dataclass(frozen=True)
class CompressionResult:
    output_path: Path
    original_size: int
    compressed_size: int
    saved_bytes: int
    logs: str


def is_supported_video(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def processed_name(original_name: str, suffix: str | None = None) -> str:
    suffix = suffix or settings.processed_suffix
    return f"{Path(original_name).stem}{suffix}.mp4"


def get_output_path(input_path: Path, output_dir: Path | None = None, suffix: str | None = None) -> Path:
    output_name = processed_name(input_path.name, suffix)
    return (output_dir or input_path.parent) / output_name


def probe_video(path: Path, config: Settings = settings) -> VideoInfo:
    result = subprocess.run(
        [
            config.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )
    data = json.loads(result.stdout)
    video_stream = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise RuntimeError(f"No video stream found in {path}")
    return VideoInfo(
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        rotation=get_stream_rotation(video_stream),
    )


def get_stream_rotation(video_stream: dict) -> int:
    tags = video_stream.get("tags") or {}
    try:
        if "rotate" in tags:
            return int(tags["rotate"])
        for side_data in video_stream.get("side_data_list") or []:
            if "rotation" in side_data:
                return int(side_data["rotation"])
    except (TypeError, ValueError):
        return 0
    return 0


def get_4k_dimensions(width: int, height: int) -> tuple[int, int]:
    long_edge = max(width, height)
    short_edge = min(width, height)
    scale = min(3840 / long_edge, 2160 / short_edge)
    target_w = int(round(width * scale / 2)) * 2
    target_h = int(round(height * scale / 2)) * 2
    return target_w, target_h


def make_rotation_neutral_input(input_path: Path, config: Settings = settings) -> Path:
    fd, temp_name = tempfile.mkstemp(
        prefix="hbed-neutral-",
        suffix=".mp4",
        dir=str(input_path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    temp_path.unlink(missing_ok=True)

    result = subprocess.run(
        [
            config.ffmpeg,
            "-y",
            "-display_rotation:v:0",
            "0",
            "-i",
            str(input_path),
            "-map",
            "0:v",
            "-map",
            "0:a?",
            "-c",
            "copy",
            str(temp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to prepare rotation-neutral input: {result.stderr}")
    return temp_path


def compress_with_handbrake(
    input_path: Path,
    output_dir: Path | None = None,
    config: Settings = settings,
) -> CompressionResult:
    if not is_supported_video(input_path):
        raise RuntimeError(f"Unsupported video extension: {input_path.suffix}")

    output_path = get_output_path(input_path, output_dir, config.processed_suffix)
    if output_path == input_path:
        raise RuntimeError("Output path is the same as input path")
    if output_path.exists():
        raise RuntimeError(f"Output already exists: {output_path}")

    original_size = input_path.stat().st_size
    video_info = probe_video(input_path, config)
    if config.upscale_to_4k:
        target_w, target_h = get_4k_dimensions(video_info.width, video_info.height)
    else:
        target_w, target_h = video_info.width, video_info.height

    input_for_handbrake = input_path
    temp_input: Path | None = None
    try:
        if video_info.rotation % 360 != 0:
            temp_input = make_rotation_neutral_input(input_path, config)
            input_for_handbrake = temp_input

        command = [
            config.handbrake_cli,
            "-i",
            str(input_for_handbrake),
            "-o",
            str(output_path),
            "--non-anamorphic",
            "--width",
            str(target_w),
            "--height",
            str(target_h),
            "-O",
            "--preset",
            config.handbrake_preset,
            "--encoder",
            config.handbrake_encoder,
        ]
        process = subprocess.run(command, capture_output=True, text=True, errors="replace")
        logs = (process.stdout or "") + (process.stderr or "")
        if process.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(f"HandBrake failed with exit code {process.returncode}\n{logs}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("HandBrake output file does not exist or is empty")

        copy_metadata(input_path, output_path, config)
        artifact = Path(str(output_path) + "_original")
        artifact.unlink(missing_ok=True)

        compressed_size = output_path.stat().st_size
        return CompressionResult(
            output_path=output_path,
            original_size=original_size,
            compressed_size=compressed_size,
            saved_bytes=original_size - compressed_size,
            logs=logs,
        )
    finally:
        if temp_input:
            temp_input.unlink(missing_ok=True)


def copy_metadata(original_path: Path, compressed_path: Path, config: Settings = settings) -> None:
    args_file: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="hbed-exiftool-",
            suffix=".args",
            delete=False,
        ) as file:
            args_file = file.name
            file.write("-TagsFromFile\n")
            file.write(f"{original_path}\n")
            file.write("-all\n")
            file.write("-all:all\n")
            file.write("-Rotation<Rotation\n")
            file.write(f"{compressed_path}\n")

        result = subprocess.run(
            [config.exiftool, "-charset", "filename=UTF8", "-@", args_file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    finally:
        if args_file:
            Path(args_file).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"ExifTool failed with exit code {result.returncode}: {result.stderr}")

