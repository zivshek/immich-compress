from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.compression import (
    get_stream_rotation,
    read_metadata_tag,
    run_streaming_command,
)
from app.config import Settings, settings


IOS_REPAIR_OUTPUT_SUFFIX = "-av1"
HLG_TRANSFER = "arib-std-b67"
BT2020_PRIMARIES = "bt2020"


@dataclass(frozen=True)
class MediaProbe:
    format_name: str | None
    video_codec: str | None
    video_profile: str | None
    pixel_format: str | None
    width: int
    height: int
    rotation: int
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    audio_codecs: tuple[str, ...]
    handler_name: str | None


@dataclass(frozen=True)
class IosCompatibilityAnalysis:
    needs_repair: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class IosRepairResult:
    output_path: Path
    original_size: int
    processed_size: int
    size_delta_bytes: int
    logs: str


def get_ios_repair_output_path(input_path: Path, output_dir: Path | None = None) -> Path:
    if output_dir:
        return output_dir / f"{input_path.stem}.mp4"
    return input_path.with_name(f"{input_path.stem}{IOS_REPAIR_OUTPUT_SUFFIX}.mp4")


def probe_media(path: Path, config: Settings = settings) -> MediaProbe:
    result = subprocess.run(
        [
            config.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
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
    audio_codecs = tuple(
        stream.get("codec_name")
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "audio" and stream.get("codec_name")
    )
    tags = video_stream.get("tags") or {}
    return MediaProbe(
        format_name=(data.get("format") or {}).get("format_name"),
        video_codec=video_stream.get("codec_name"),
        video_profile=video_stream.get("profile"),
        pixel_format=video_stream.get("pix_fmt"),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        rotation=get_stream_rotation(video_stream),
        color_primaries=video_stream.get("color_primaries"),
        color_transfer=video_stream.get("color_transfer") or video_stream.get("color_trc"),
        color_space=video_stream.get("color_space") or video_stream.get("colorspace"),
        audio_codecs=audio_codecs,
        handler_name=tags.get("handler_name"),
    )


def analyze_ios_compatibility(probe: MediaProbe) -> IosCompatibilityAnalysis:
    codec = (probe.video_codec or "").lower()
    profile = (probe.video_profile or "").lower()
    pixel_format = (probe.pixel_format or "").lower()
    color_transfer = (probe.color_transfer or "").lower()
    color_primaries = (probe.color_primaries or "").lower()

    is_problematic_vp9_hlg = (
        codec == "vp9"
        and "profile 2" in profile
        and pixel_format == "yuv420p10le"
        and color_primaries == BT2020_PRIMARIES
        and color_transfer == HLG_TRANSFER
    )
    reasons = (
        ("VP9 Profile 2 yuv420p10le BT.2020/HLG HDR is unreliable on iOS",)
        if is_problematic_vp9_hlg
        else ()
    )
    return IosCompatibilityAnalysis(is_problematic_vp9_hlg, reasons)


def build_ios_repair_command(
    input_path: Path,
    output_path: Path,
    probe: MediaProbe,
    config: Settings,
) -> list[str]:
    encoder = (config.ios_repair_encoder or "").lower()
    if "av1" in encoder:
        codec_args = ["-pix_fmt", "yuv420p"]
        tag_args = ["-tag:v", "av01"]
    elif "hevc" in encoder:
        codec_args = ["-profile:v", "main", "-pix_fmt", "yuv420p"]
        tag_args = ["-tag:v", "hvc1"]
    else:
        codec_args = ["-profile:v", "high", "-pix_fmt", "yuv420p"]
        tag_args = []

    return [
        config.ffmpeg,
        "-hide_banner",
        "-y",
        "-noautorotate",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-vf",
        build_tonemap_filter(probe),
        "-c:v",
        config.ios_repair_encoder,
        "-preset",
        "p5",
        "-rc",
        "vbr",
        "-cq",
        str(config.video_crf),
        "-b:v",
        "0",
        *codec_args,
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        "-c:a",
        "copy",
        *tag_args,
        "-movflags",
        "+faststart+use_metadata_tags",
        str(output_path),
    ]


def build_tonemap_filter(probe: MediaProbe) -> str:
    color_transfer = (probe.color_transfer or "").lower()
    color_primaries = (probe.color_primaries or "").lower()
    if color_transfer == HLG_TRANSFER or color_primaries == BT2020_PRIMARIES:
        return (
            "zscale=transfer=linear:npl=100,"
            "format=gbrpf32le,"
            "tonemap=tonemap=hable:desat=0,"
            "zscale=primaries=bt709:transfer=bt709:matrix=bt709:range=tv,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            "format=yuv420p"
        )
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"


def repair_video_for_ios(
    input_path: Path,
    output_dir: Path | None = None,
    config: Settings = settings,
    progress_callback: Callable[[str, float | None, str | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> IosRepairResult:
    probe = probe_media(input_path, config)
    analysis = analyze_ios_compatibility(probe)
    if not analysis.needs_repair:
        raise RuntimeError("Video already appears compatible; repair is not required")

    output_path = get_ios_repair_output_path(input_path, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path == input_path:
        raise RuntimeError("Output path is the same as input path")
    if output_path.exists():
        raise RuntimeError(f"Output already exists: {output_path}")

    original_size = input_path.stat().st_size
    log_lines = ["iOS repair reasons: " + "; ".join(analysis.reasons)]
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(Path(config.ffmpeg).parent), env.get("PATH", "")])

    if progress_callback:
        progress_callback(
            "Repairing",
            0,
            f"Transcoding to AV1 MP4 with {config.ios_repair_encoder} at CRF {config.video_crf}, "
            "tone-mapped to SDR BT.709.",
        )
    try:
        run_streaming_command(
            build_ios_repair_command(input_path, output_path, probe, config),
            env,
            log_lines,
            progress_callback,
            cancel_requested,
            "Repairing",
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("Repair output file does not exist or is empty")
        if progress_callback:
            progress_callback("Metadata", 100, "Copying applicable metadata with ExifTool")
        copy_metadata_for_ios_repair(input_path, output_path, config)
        Path(str(output_path) + "_original").unlink(missing_ok=True)
        validate_ios_repair_metadata(input_path, output_path, config)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise

    processed_size = output_path.stat().st_size
    if progress_callback:
        progress_callback(
            "Complete",
            100,
            f"Repaired to AV1 MP4: {processed_size / 1048576:.1f} MB.",
        )
    return IosRepairResult(
        output_path=output_path,
        original_size=original_size,
        processed_size=processed_size,
        size_delta_bytes=original_size - processed_size,
        logs="\n".join(log_lines),
    )


def copy_metadata_for_ios_repair(
    original_path: Path,
    repaired_path: Path,
    config: Settings = settings,
) -> None:
    args_file: str | None = None
    excluded_color_tags = (
        "ColorPrimaries",
        "TransferCharacteristics",
        "MatrixCoefficients",
        "VideoFullRangeFlag",
    )
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="immich-ios-repair-exiftool-",
            suffix=".args",
            delete=False,
        ) as file:
            args_file = file.name
            file.write("-api\n")
            file.write("ExtractEmbedded=1\n")
            file.write("-TagsFromFile\n")
            file.write(f"{original_path}\n")
            for tag in excluded_color_tags:
                file.write(f"--{tag}\n")
            file.write("-all\n")
            file.write("-all:all\n")
            file.write("-Rotation<Rotation\n")
            file.write(f"{repaired_path}\n")

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


def validate_ios_repair_metadata(
    original_path: Path,
    repaired_path: Path,
    config: Settings = settings,
) -> None:
    for tag in ("Rotation", "GPSCoordinates", "Model", "CreateDate"):
        source_value = read_metadata_tag(original_path, tag, config)
        if source_value and not read_metadata_tag(repaired_path, tag, config):
            raise RuntimeError(f"Metadata validation failed: output is missing {tag}")


def ffmpeg_has_filter(filter_name: str, config: Settings = settings) -> bool:
    resolved = shutil.which(config.ffmpeg) or config.ffmpeg
    result = subprocess.run(
        [resolved, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.returncode == 0 and filter_name in result.stdout
