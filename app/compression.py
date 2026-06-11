from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None


@dataclass(frozen=True)
class CompressionResult:
    output_path: Path
    original_size: int
    compressed_size: int
    saved_bytes: int
    logs: str


def is_supported_video(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def get_av1_output_path(input_path: Path, output_dir: Path | None = None) -> Path:
    if output_dir:
        return output_dir / f"{input_path.stem}.mp4"
    return input_path.with_name(f"{input_path.stem}{TEMP_OUTPUT_SUFFIX}.mp4")


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
        color_primaries=video_stream.get("color_primaries") or video_stream.get("color_primaries"),
        color_transfer=video_stream.get("color_transfer") or video_stream.get("color_trc") or video_stream.get("color_trc"),
        color_space=video_stream.get("color_space") or video_stream.get("colorspace"),
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


def compress_video(
    input_path: Path,
    output_dir: Path | None = None,
    config: Settings = settings,
    progress_callback: Callable[[str, float | None, str | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> CompressionResult:
    return compress_with_perceptual_av1(
        input_path, output_dir, config, progress_callback, cancel_requested
    )


def build_av1_command(
    input_path: Path,
    video_only_path: Path,
    video_info: VideoInfo,
    config: Settings,
) -> list[str]:
    return [
        config.ab_av1,
        "auto-encode",
        "--input",
        str(input_path),
        "--output",
        str(video_only_path),
        "--video-only",
        "--crf",
        "28",
        "--preset",
        "6",
        "--enc-input",
        "noautorotate",
    ]


def compress_with_perceptual_av1(
    input_path: Path,
    output_dir: Path | None = None,
    config: Settings = settings,
    progress_callback: Callable[[str, float | None, str | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> CompressionResult:
    if not is_supported_video(input_path):
        raise RuntimeError(f"Unsupported video extension: {input_path.suffix}")
    output_path = get_av1_output_path(input_path, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path == input_path:
        raise RuntimeError("Output path is the same as input path")
    if output_path.exists():
        raise RuntimeError(f"Output already exists: {output_path}")

    original_size = input_path.stat().st_size
    video_info = probe_video(input_path, config)
    log_lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ab-av1-", dir=output_path.parent) as directory:
        temp_dir = Path(directory)
        video_only_path = temp_dir / "video-only-av1.mp4"
        env = os.environ.copy()
        env["AB_AV1_TEMP_DIR"] = str(temp_dir / "samples")
        env["XDG_CACHE_HOME"] = str(temp_dir / "cache")
        Path(env["AB_AV1_TEMP_DIR"]).mkdir()
        Path(env["XDG_CACHE_HOME"]).mkdir()
        env["PATH"] = os.pathsep.join(
            [str(Path(config.perceptual_ffmpeg).parent), env.get("PATH", "")]
        )
        library_path = str(Path(config.perceptual_ffmpeg).parent.parent / "lib")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            [library_path, env.get("LD_LIBRARY_PATH", "")]
        )
        if progress_callback:
            progress_callback(
                "Analyzing",
                0,
                "Encoding AV1 with fixed CRF 28 and preserving original audio, chapters, and metadata.",
            )
        run_streaming_command(
            build_av1_command(input_path, video_only_path, video_info, config),
            env,
            log_lines,
            progress_callback,
            cancel_requested,
            "Analyzing",
        )
        if not video_only_path.is_file() or video_only_path.stat().st_size == 0:
            raise RuntimeError("ab-av1 output file does not exist or is empty")

        if progress_callback:
            progress_callback("Remuxing", 100, "Restoring original audio, chapters, and metadata")
        cmd: list[str] = [
            config.perceptual_ffmpeg,
            "-hide_banner",
            "-y",
            "-noautorotate",
            "-i",
            str(video_only_path),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
        ]

        # Preserve color metadata (if present in source) by setting stream metadata
        if video_info.color_primaries:
            cmd += ["-metadata:s:v:0", f"color_primaries={video_info.color_primaries}"]
        if video_info.color_transfer:
            cmd += ["-metadata:s:v:0", f"color_trc={video_info.color_transfer}"]
        if video_info.color_space:
            cmd += ["-metadata:s:v:0", f"colorspace={video_info.color_space}"]

        cmd += [
            "-c",
            "copy",
            "-tag:v",
            "av01",
            "-movflags",
            "+faststart+use_metadata_tags",
            str(output_path),
        ]

        remux = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
        )
        if remux.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(f"AV1 remux failed: {remux.stderr[-4000:]}")

    if progress_callback:
        progress_callback("Metadata", 100, "Copying metadata with ExifTool")
    try:
        copy_metadata(input_path, output_path, config)
        Path(str(output_path) + "_original").unlink(missing_ok=True)
        validate_metadata(input_path, output_path, config)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    compressed_size = output_path.stat().st_size
    savings_percent = (
        (original_size - compressed_size) / original_size * 100 if original_size else 0
    )
    if savings_percent < config.min_savings_percent:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"AV1 output saved only {savings_percent:.1f}%; required "
            f"{config.min_savings_percent}%"
        )
    if progress_callback:
        progress_callback(
            "Complete",
            100,
            f"Compressed to {compressed_size / 1048576:.1f} MB; saved "
            f"{(original_size - compressed_size) / 1048576:.1f} MB ({savings_percent:.1f}%).",
        )
    return CompressionResult(
        output_path=output_path,
        original_size=original_size,
        compressed_size=compressed_size,
        saved_bytes=original_size - compressed_size,
        logs="\n".join(log_lines),
    )


def run_streaming_command(
    command: list[str],
    env: dict[str, str],
    log_lines: list[str],
    progress_callback: Callable[[str, float | None, str | None], None] | None,
    cancel_requested: Callable[[], bool] | None,
    stage: str,
) -> None:
    resolved = shutil.which(command[0], path=env.get("PATH")) or command[0]
    process = subprocess.Popen(
        [resolved, *command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            if cancel_requested and cancel_requested():
                process.terminate()
                raise InterruptedError("Job canceled")
            line = raw_line.strip()
            if not line:
                continue
            log_lines.append(line)
            percent = parse_command_percent(line)
            if progress_callback:
                progress_callback(stage, percent, compact_log_line(line))
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if process.returncode != 0:
        raise RuntimeError(
            f"{Path(command[0]).name} failed with exit code {process.returncode}\n"
            f"{chr(10).join(log_lines)[-4000:]}"
        )


def parse_command_percent(line: str) -> float | None:
    match = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)%", line)
    if not match:
        return None
    return min(100, float(match.group(1)))


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


def validate_metadata(
    original_path: Path,
    compressed_path: Path,
    config: Settings = settings,
) -> None:
    for tag in ("Rotation", "GPSCoordinates", "Model", "CreateDate"):
        source_value = read_metadata_tag(original_path, tag, config)
        if source_value and not read_metadata_tag(compressed_path, tag, config):
            raise RuntimeError(f"Metadata validation failed: output is missing {tag}")


def read_metadata_tag(path: Path, tag: str, config: Settings) -> str:
    result = subprocess.run(
        [config.exiftool, "-s3", f"-{tag}", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ExifTool could not read {tag}: {result.stderr}")
    return result.stdout.strip()
