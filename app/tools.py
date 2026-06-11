from __future__ import annotations

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
        check_command("AV1 FFmpeg", config.av1_ffmpeg, "-version"),
        check_command("ExifTool", config.exiftool, "-ver"),
        check_command("FFmpeg", config.ffmpeg, "-version"),
        check_command("FFprobe", config.ffprobe, "-version"),
    ]
