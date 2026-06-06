from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Settings, settings


TEMP_OUTPUT_SUFFIX = "-compressed"


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


def get_output_path(input_path: Path, output_dir: Path | None = None) -> Path:
    if output_dir:
        return output_dir / input_path.name
    return input_path.with_name(f"{input_path.stem}{TEMP_OUTPUT_SUFFIX}{input_path.suffix}")


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
    progress_callback: Callable[[str, float | None, str | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> CompressionResult:
    if not config.handbrake_preset or not config.handbrake_encoder:
        raise RuntimeError("Choose a HandBrake preset and encoder in Settings before processing videos")
    if not is_supported_video(input_path):
        raise RuntimeError(f"Unsupported video extension: {input_path.suffix}")

    output_path = get_output_path(input_path, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path == input_path:
        raise RuntimeError("Output path is the same as input path")
    if output_path.exists():
        raise RuntimeError(f"Output already exists: {output_path}")

    original_size = input_path.stat().st_size
    if progress_callback:
        progress_callback("Probing", 0, f"Original file size: {original_size / 1048576:.1f} MB")
    video_info = probe_video(input_path, config)
    if config.upscale_to_4k:
        target_w, target_h = get_4k_dimensions(video_info.width, video_info.height)
    else:
        target_w, target_h = video_info.width, video_info.height

    input_for_handbrake = input_path
    temp_input: Path | None = None
    try:
        if video_info.rotation % 360 != 0:
            if progress_callback:
                progress_callback("Preparing", None, f"Neutralizing rotation metadata: {video_info.rotation}")
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
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            errors="replace",
        )

        def stop_when_canceled() -> None:
            while process.poll() is None:
                if cancel_requested and cancel_requested():
                    process.terminate()
                    return
                time.sleep(0.25)

        if cancel_requested:
            threading.Thread(target=stop_when_canceled, daemon=True).start()
        log_lines: list[str] = []
        last_stage = ""
        last_percent = -1.0
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel_requested and cancel_requested():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                output_path.unlink(missing_ok=True)
                raise InterruptedError("Job canceled")
            line = raw_line.strip()
            if not line:
                continue
            log_lines.append(line)
            progress = parse_handbrake_progress(line)
            if progress:
                stage, percent = progress
                if progress_callback and (stage != last_stage or percent - last_percent >= 1):
                    progress_callback(stage, percent, compact_log_line(line))
                    last_stage = stage
                    last_percent = percent
            elif progress_callback and should_log_handbrake_line(line):
                progress_callback(last_stage or "Encoding", None, compact_log_line(line))

        process.wait()
        logs = "\n".join(log_lines)
        if cancel_requested and cancel_requested():
            output_path.unlink(missing_ok=True)
            raise InterruptedError("Job canceled")
        if process.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(f"HandBrake failed with exit code {process.returncode}\n{logs[-4000:]}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("HandBrake output file does not exist or is empty")

        if progress_callback:
            progress_callback("Metadata", 100, "Copying metadata with ExifTool")
        copy_metadata(input_path, output_path, config)
        artifact = Path(str(output_path) + "_original")
        artifact.unlink(missing_ok=True)

        compressed_size = output_path.stat().st_size
        if progress_callback:
            saved = original_size - compressed_size
            pct = (saved / original_size * 100) if original_size else 0
            progress_callback(
                "Complete",
                100,
                f"Compressed to {compressed_size / 1048576:.1f} MB; saved {saved / 1048576:.1f} MB ({pct:.1f}%).",
            )
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


def parse_handbrake_progress(line: str) -> tuple[str, float] | None:
    if "Scanning title" in line and "%" in line:
        percent = percent_before_marker(line)
        if percent is not None:
            return "Scanning", percent
    if "Encoding: task" in line and "%" in line:
        percent = percent_before_marker(line)
        if percent is not None:
            return "Encoding", percent
    return None


def percent_before_marker(line: str) -> float | None:
    try:
        return float(line.split("%", 1)[0].split(",")[-1].strip())
    except ValueError:
        return None


def should_log_handbrake_line(line: str) -> bool:
    interesting = (
        "Using preset:",
        "encoder:",
        "quality:",
        "Output geometry",
        "Starting Task",
        "Encode done",
        "HandBrake has exited",
        "ERROR:",
        "Cannot load",
        "Failure",
    )
    return any(marker in line for marker in interesting)


def compact_log_line(line: str, max_length: int = 220) -> str:
    if len(line) <= max_length:
        return line
    return line[: max_length - 3] + "..."


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
