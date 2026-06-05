from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from app import db
from app.compression import SUPPORTED_VIDEO_EXTENSIONS
from app.config import effective_settings
from app.immich import ImmichClient


@dataclass(frozen=True)
class ImportCandidate:
    processed_path: Path
    original_name: str
    original_stem: str
    target_path: Path


def is_processed_file(path: Path, suffix: str) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        and path.stem.lower().endswith(suffix.lower())
    )


def candidate_for(path: Path, suffix: str) -> ImportCandidate:
    original_stem = path.stem[: -len(suffix)]
    original_name = original_stem + path.suffix
    return ImportCandidate(
        processed_path=path,
        original_name=original_name,
        original_stem=original_stem,
        target_path=path.with_name(original_name),
    )


def iter_candidates(folder: Path, suffix: str, recursive: bool) -> list[ImportCandidate]:
    paths = folder.rglob("*") if recursive else folder.iterdir()
    return [candidate_for(path, suffix) for path in paths if is_processed_file(path, suffix)]


def find_original_asset(client: ImmichClient, candidate: ImportCandidate) -> tuple[dict | None, str | None]:
    matches = []
    for item in client.search_assets_by_stem(candidate.original_stem):
        original_name = item.get("originalFileName", "")
        if Path(original_name).stem == candidate.original_stem:
            matches.append(item)

    if not matches:
        return None, "matching untrashed Immich original asset not found"
    if len(matches) > 1:
        names = ", ".join(f"{item.get('originalFileName')} ({item.get('id')})" for item in matches)
        return None, f"multiple matching Immich assets found: {names}"
    return matches[0], None


def import_candidate(
    client: ImmichClient,
    candidate: ImportCandidate,
    *,
    rename: bool,
    apply: bool,
) -> tuple[bool, str]:
    asset, error = find_original_asset(client, candidate)
    if error:
        return False, error

    if rename and candidate.target_path.exists():
        return False, f"rename target already exists: {candidate.target_path}"

    if apply:
        db.upsert_job(asset["id"], candidate.original_name, "processed")
        db.update_job(
            asset["id"],
            original_path=str(candidate.target_path if rename else candidate.processed_path),
            output_path=str(candidate.target_path if rename else candidate.processed_path),
            compressed_size=candidate.processed_path.stat().st_size,
            saved_bytes=None,
            error=None,
            logs="Imported existing manually compressed file.",
        )
        if rename:
            candidate.processed_path.rename(candidate.target_path)
    action = "imported"
    if rename:
        action += " and renamed"
    if not apply:
        action = "dry run: would " + action
    return True, f"{action} for asset {asset['id']}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import already-compressed videos into the sidecar database. "
            "Files are matched to Immich originals by removing the processed suffix from the filename."
        )
    )
    parser.add_argument("folder", type=Path, help="Folder containing processed videos")
    parser.add_argument("--recursive", action="store_true", help="Scan subfolders")
    parser.add_argument("--suffix", help="Processed suffix to remove, defaults to configured suffix")
    parser.add_argument("--rename", action="store_true", help="Rename files by removing the processed suffix")
    parser.add_argument("--apply", action="store_true", help="Write database rows and perform renames")
    args = parser.parse_args()

    config = effective_settings()
    suffix = args.suffix or config.processed_suffix
    db.init_db()
    client = ImmichClient(config)

    candidates = iter_candidates(args.folder, suffix, args.recursive)
    print(f"Found {len(candidates)} processed file(s) with suffix '{suffix}'")
    success_count = 0
    failure_count = 0
    for candidate in candidates:
        success, message = import_candidate(
            client,
            candidate,
            rename=args.rename,
            apply=args.apply,
        )
        label = "OK" if success else "FAIL"
        print(f"{label}: {candidate.processed_path} -> {message}")
        success_count += int(success)
        failure_count += int(not success)

    print(f"Summary: {success_count} ready/imported, {failure_count} failed")


if __name__ == "__main__":
    main()
