from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class ToolStatus:
    name: str
    command: str
    available: bool
    version: str


@dataclass(frozen=True)
class HandBrakeOption:
    value: str
    description: str


def check_command(name: str, command: str, *version_args: str) -> ToolStatus:
    resolved = shutil.which(command) or command
    try:
        result = subprocess.run(
            [resolved, *version_args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolStatus(name=name, command=command, available=False, version=str(exc))

    output = (result.stdout or result.stderr or "").strip().splitlines()
    version = output[0] if output else f"exit code {result.returncode}"
    return ToolStatus(
        name=name,
        command=command,
        available=result.returncode == 0,
        version=version,
    )


def tool_statuses(config: Settings) -> list[ToolStatus]:
    return [
        check_command("ab-av1", config.ab_av1, "--version"),
        check_command("Perceptual FFmpeg", config.perceptual_ffmpeg, "-version"),
        check_command("HandBrakeCLI", config.handbrake_cli, "--version"),
        check_command("ExifTool", config.exiftool, "-ver"),
        check_command("FFmpeg", config.ffmpeg, "-version"),
        check_command("FFprobe", config.ffprobe, "-version"),
    ]


def handbrake_presets(config: Settings) -> list[HandBrakeOption]:
    output = run_handbrake(config.handbrake_cli, "--preset-list")
    if not output:
        return []
    try:
        data = decode_first_json(output)
        options: list[HandBrakeOption] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                name = value.get("PresetName")
                if isinstance(name, str) and not value.get("Folder"):
                    description = value.get("PresetDescription")
                    options.append(
                        HandBrakeOption(
                            name,
                            description if isinstance(description, str) else describe_preset(name),
                        )
                    )
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(data)
    except ValueError:
        options = parse_text_preset_list(output)
    return list({option.value: option for option in options}.values())


def handbrake_encoders(config: Settings) -> list[HandBrakeOption]:
    output = run_handbrake(config.handbrake_cli, "--help")
    if not output:
        return []
    match = re.search(
        r"--encoder\s+<[^>]+>[^\n]*\n(.+?)(?=\n\s*--encoder-preset)",
        output,
        re.DOTALL,
    )
    if not match:
        return []
    values = re.findall(r"^\s+([a-zA-Z0-9_]+)\s*$", match.group(1), re.MULTILINE)
    if not values:
        options_match = re.search(r"Options:\s*(.+)", match.group(1), re.DOTALL)
        if options_match:
            values = re.findall(r"[a-zA-Z0-9_]+", options_match.group(1))
    return [
        HandBrakeOption(value, describe_encoder(value))
        for value in dict.fromkeys(values)
        if value.lower() != "options"
    ]


def parse_text_preset_list(output: str) -> list[HandBrakeOption]:
    options: list[HandBrakeOption] = []
    preset_indent: int | None = None
    current_name: str | None = None
    current_description: list[str] = []

    def append_current() -> None:
        if not current_name:
            return
        description = " ".join(current_description) or describe_preset(current_name)
        options.append(HandBrakeOption(current_name, description))

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if line.endswith("/"):
            append_current()
            current_name = None
            current_description = []
            preset_indent = indent + 4
            continue
        if preset_indent is None or line.startswith("["):
            continue
        if indent == preset_indent:
            append_current()
            current_name = line
            current_description = []
        elif current_name and indent > preset_indent:
            current_description.append(line)
    append_current()
    return options


def decode_first_json(output: str) -> object:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", output):
        try:
            data, _ = decoder.raw_decode(output[match.start() :])
            return data
        except json.JSONDecodeError:
            continue
    raise ValueError("HandBrake output did not contain JSON")


def run_handbrake(command: str, *args: str) -> str:
    resolved = shutil.which(command) or command
    try:
        result = subprocess.run(
            [resolved, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(part for part in [result.stdout, result.stderr] if part)


def describe_preset(name: str) -> str:
    lowered = name.lower()
    if "production" in lowered:
        return "High-quality intermediate intended for editing; creates large files."
    if "social" in lowered or "vimeo" in lowered or "youtube" in lowered:
        return "Tuned for the named online publishing destination."
    if "fast" in lowered or "very fast" in lowered:
        return "Faster encoding with a larger file or lower efficiency."
    if "hq" in lowered or "super hq" in lowered:
        return "Higher quality and compression efficiency, with slower encoding."
    return "Built-in HandBrake preset defining quality, audio, and container defaults."


def describe_encoder(value: str) -> str:
    lowered = value.lower()
    if "av1" in lowered:
        codec = "AV1"
    elif "265" in lowered or "hevc" in lowered:
        codec = "H.265/HEVC"
    elif "264" in lowered:
        codec = "H.264"
    elif "vp9" in lowered:
        codec = "VP9"
    elif "vp8" in lowered:
        codec = "VP8"
    elif "ffv1" in lowered:
        codec = "FFV1 lossless"
    elif "mpeg4" in lowered:
        codec = "MPEG-4"
    elif "mpeg2" in lowered:
        codec = "MPEG-2"
    elif "theora" in lowered:
        codec = "Theora"
    else:
        codec = value
    if "nvenc" in lowered:
        return f"{codec} using NVIDIA NVENC hardware; requires NVIDIA GPU access."
    if "qsv" in lowered:
        return f"{codec} using Intel Quick Sync hardware; requires Intel GPU access."
    if "vce" in lowered:
        return f"{codec} using AMD VCE hardware; requires AMD GPU access."
    if lowered.startswith("mf_"):
        return f"{codec} using Microsoft Media Foundation hardware."
    if "videotoolbox" in lowered:
        return f"{codec} using Apple VideoToolbox hardware."
    if "vaapi" in lowered:
        return f"{codec} using VA-API hardware; requires /dev/dri access."
    return f"{codec} software encoding on the CPU; slower but broadly compatible."
