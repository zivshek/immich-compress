import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


PROCESSED_SUFFIX = "-hbed"
SUPPORTED_VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm", ".wmv",
}


def configure_console_output():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


configure_console_output()


def color(text, code):
    return f"\033[{code}m{text}\033[0m"


def green(text):
    return color(text, "92")


def red(text):
    return color(text, "91")


def is_processed_video(path):
    stem, ext = os.path.splitext(os.path.basename(path))
    return stem.lower().endswith(PROCESSED_SUFFIX) and ext.lower() in SUPPORTED_VIDEO_EXTENSIONS


def iter_processed_files(folder, recursive):
    if recursive:
        for root, _, files in os.walk(folder):
            for file_name in files:
                path = os.path.join(root, file_name)
                if is_processed_video(path):
                    yield path
    else:
        for file_name in os.listdir(folder):
            path = os.path.join(folder, file_name)
            if os.path.isfile(path) and is_processed_video(path):
                yield path


def original_stem_from_processed(processed_file):
    stem = os.path.splitext(os.path.basename(processed_file))[0]
    return stem[:-len(PROCESSED_SUFFIX)]


def load_env_file(env_file):
    if env_file and not os.path.isabs(env_file):
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), env_file)
    if not env_file or not os.path.exists(env_file):
        return {}

    values = {}
    with open(env_file, "r", encoding="utf-8-sig") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def api_url(server, path):
    return server.rstrip("/") + "/api/" + path.lstrip("/")


def immich_request(args, method, path, body=None):
    if not args.server:
        raise RuntimeError("Immich server is missing. Set IMMICH_SERVER in immich.env or pass --server.")
    if not args.api_key:
        raise RuntimeError("Immich API key is missing. Set IMMICH_API_KEY in immich.env or pass --api-key.")

    data = None
    headers = {
        "x-api-key": args.api_key,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(api_url(args.server, path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Immich API {method} {path} failed with {error.code}: {details}") from error


def search_original_asset(processed_file, args):
    original_stem = original_stem_from_processed(processed_file)
    matches = search_untrashed_assets_by_stem(original_stem, args)

    if not matches:
        return None, f"matching untrashed original asset not found on Immich for stem '{original_stem}'"
    if len(matches) > 1:
        names = ", ".join(f"{item.get('originalFileName')} ({item.get('id')})" for item in matches)
        return None, f"multiple matching original assets found on Immich: {names}"

    return matches[0], None


def search_uploaded_asset(processed_file, args):
    processed_name = os.path.basename(processed_file)
    processed_stem = os.path.splitext(processed_name)[0]
    matches = [
        item for item in search_untrashed_assets_by_stem(processed_stem, args)
        if item.get("originalFileName") == processed_name
    ]

    if not matches:
        return None, f"uploaded processed asset not found on Immich for filename '{processed_name}'"
    if len(matches) > 1:
        names = ", ".join(f"{item.get('originalFileName')} ({item.get('id')})" for item in matches)
        return None, f"multiple uploaded processed assets found on Immich: {names}"

    return matches[0], None


def search_untrashed_assets_by_stem(stem, args):
    response = immich_request(args, "POST", "search/metadata", {
        "originalFileName": stem,
        "size": 50,
    })
    items = response.get("assets", {}).get("items", []) if isinstance(response, dict) else []
    matches = []
    for item in items:
        original_name = item.get("originalFileName", "")
        item_stem, ext = os.path.splitext(original_name)
        if item_stem == stem and ext.lower() in SUPPORTED_VIDEO_EXTENSIONS:
            if not item.get("isTrashed"):
                matches.append(item)
    return matches


def get_album_cache_for_assets(asset_ids, args):
    target_ids = set(asset_ids)
    albums = immich_request(args, "GET", "albums") or []
    album_cache = {asset_id: [] for asset_id in target_ids}
    for album in albums:
        if not album.get("assetCount"):
            continue
        album_id = album.get("id")
        if not album_id:
            continue
        album_detail = immich_request(args, "GET", f"albums/{album_id}")
        assets = album_detail.get("assets", []) if isinstance(album_detail, dict) else []
        matching_asset_ids = {asset.get("id") for asset in assets} & target_ids
        if not matching_asset_ids:
            continue
        cached_album = {
            "id": album_id,
            "albumName": album.get("albumName") or album_detail.get("albumName") or album_id,
        }
        for asset_id in matching_asset_ids:
            album_cache[asset_id].append(cached_album)
    return album_cache


def add_asset_to_albums(asset_id, albums, args):
    if not albums:
        print("  Original asset is not in any albums")
        return
    album_names = ", ".join(album["albumName"] for album in albums)
    if args.dry_run:
        print(f"  Dry run: would add uploaded asset to {len(albums)} album(s): {album_names}")
        return
    for album in albums:
        immich_request(args, "PUT", f"albums/{album['id']}/assets", {
            "ids": [asset_id],
        })
    print(f"  Added uploaded asset to {len(albums)} album(s): {album_names}")


def trash_immich_asset(asset_id, args):
    if args.dry_run:
        print(f"  Dry run: would trash Immich asset {asset_id}")
        return
    immich_request(args, "DELETE", "assets", {
        "ids": [asset_id],
        "force": False,
    })


def link_or_copy_file(source, destination):
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy"


def upload_files(immich_go, processed_files, args):
    if not processed_files:
        return True, 0

    with tempfile.TemporaryDirectory(prefix="immich-upload-", dir=args.processed_folder) as temp_dir:
        print(f"\nBatch upload staging folder: {temp_dir}")
        for processed_file in processed_files:
            staged_file = os.path.join(temp_dir, os.path.basename(processed_file))
            staging_method = link_or_copy_file(processed_file, staged_file)
            print(f"  Staged via {staging_method}: {processed_file}")

        command = [
            immich_go,
            "--on-errors", "continue",
        ]
        if args.config:
            command.extend(["--config", args.config])
        if args.log_level:
            command.extend(["--log-level", args.log_level])
        if args.dry_run:
            command.append("--dry-run")

        command.extend(["upload"])
        if args.server:
            command.extend(["--server", args.server])
        if args.api_key:
            command.extend(["--api-key", args.api_key])
        if args.no_ui:
            command.append("--no-ui")
        for tag in args.tag:
            command.extend(["--tag", tag])
        if args.into_album:
            command.extend(["from-folder", "--into-album", args.into_album, temp_dir])
        else:
            command.extend(["from-folder", temp_dir])

        print("\nBatch uploading with immich-go...")
        result = subprocess.run(command, text=True, capture_output=True, errors="replace")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())
        return result.returncode == 0, result.returncode


def ensure_unique_basenames(processed_files):
    seen = {}
    duplicates = {}
    for processed_file in processed_files:
        basename = os.path.basename(processed_file).lower()
        if basename in seen:
            duplicates.setdefault(basename, [seen[basename]]).append(processed_file)
        else:
            seen[basename] = processed_file
    if not duplicates:
        return None

    details = []
    for basename, paths in duplicates.items():
        details.append(f"{basename}: {', '.join(paths)}")
    return "duplicate processed filenames are not supported for batch upload: " + "; ".join(details)


def prepare_item(processed_file, args):
    print(f"\nProcessing file: {processed_file}")

    print("  Step 1: locating matching original asset on Immich...")
    original_asset, error = search_original_asset(processed_file, args)
    if error:
        return None, error
    print(f"  Original asset: {original_asset.get('originalFileName')} ({original_asset.get('id')})")

    return {
        "processed_file": processed_file,
        "original_asset": original_asset,
        "albums": [],
    }, None


def apply_album_cache(item, album_cache):
    original_asset = item["original_asset"]
    original_albums = album_cache.get(original_asset["id"], [])
    item["albums"] = original_albums
    if original_albums:
        album_names = ", ".join(album["albumName"] for album in original_albums)
        print(f"  Original asset is in {len(original_albums)} album(s): {album_names}")
    else:
        print("  Original asset is not in any albums")


def finish_item(item, args):
    processed_file = item["processed_file"]
    original_asset = item["original_asset"]
    original_albums = item["albums"]

    print(f"\nFinishing file: {processed_file}")
    print("  Step 4: finding uploaded asset and adding it to original albums...")
    if args.dry_run:
        add_asset_to_albums(None, original_albums, args)
    else:
        uploaded_asset, error = search_uploaded_asset(processed_file, args)
        if error:
            return False, error
        print(f"  Uploaded asset: {uploaded_asset.get('originalFileName')} ({uploaded_asset.get('id')})")
        add_asset_to_albums(uploaded_asset["id"], original_albums, args)

    print("  Step 5: trashing original asset on Immich...")
    trash_immich_asset(original_asset["id"], args)
    if not args.dry_run:
        print(f"  Trashed original Immich asset: {original_asset.get('originalFileName')}")

    if args.dry_run:
        return True, "dry run: would upload, copy album memberships, and trash original Immich asset"
    return True, "uploaded, copied album memberships, and trashed original Immich asset"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Upload processed -hbed videos to Immich, copy original album memberships, "
            "and trash matching original Immich assets."
        )
    )
    parser.add_argument("processed_folder", help="Folder containing processed *-hbed videos")
    parser.add_argument("--recursive", action="store_true", help="Process subfolders recursively")
    parser.add_argument("--immich-go", default=r"C:\dev\immich-go\immich-go.exe", help="Path to immich-go.exe")
    parser.add_argument("--env-file", default="immich.env", help="Env file containing IMMICH_SERVER and IMMICH_API_KEY")
    parser.add_argument("--config", help="immich-go config file")
    parser.add_argument("--server", help="Immich server URL")
    parser.add_argument("--api-key", help="Immich API key")
    parser.add_argument("--no-ui", action="store_true", help="Pass --no-ui to immich-go")
    parser.add_argument("--tag", action="append", default=[], help="Tag to add during upload; may be repeated")
    parser.add_argument("--into-album", help="Album name to upload into")
    parser.add_argument("--log-level", default="INFO", help="immich-go log level")
    parser.add_argument("--dry-run", action="store_true", help="Run immich-go dry-run and do not trash originals")
    args = parser.parse_args()

    env_values = load_env_file(args.env_file)
    if not args.server:
        args.server = env_values.get("IMMICH_SERVER") or env_values.get("server")
    if not args.api_key:
        args.api_key = env_values.get("IMMICH_API_KEY") or env_values.get("api-key") or env_values.get("api_key")

    args.processed_folder = os.path.abspath(args.processed_folder)

    if not os.path.isfile(args.immich_go):
        print(red(f"immich-go not found: {args.immich_go}"))
        sys.exit(1)
    if not os.path.isdir(args.processed_folder):
        print(red(f"Processed folder not found: {args.processed_folder}"))
        sys.exit(1)
    processed_files = sorted(iter_processed_files(args.processed_folder, args.recursive))
    print(f"Found {len(processed_files)} processed file(s) in {args.processed_folder}")

    results = []
    duplicate_error = ensure_unique_basenames(processed_files)
    if duplicate_error:
        for processed_file in processed_files:
            results.append((processed_file, False, duplicate_error))
        print_results(results)
        sys.exit(1)

    prepared_items = []
    for processed_file in processed_files:
        try:
            item, error = prepare_item(processed_file, args)
            if error:
                results.append((processed_file, False, error))
            else:
                prepared_items.append(item)
        except Exception as exc:
            results.append((processed_file, False, str(exc)))

    if prepared_items:
        try:
            print("\nStep 2: finding original album memberships...")
            album_cache = get_album_cache_for_assets(
                [item["original_asset"]["id"] for item in prepared_items],
                args,
            )
            for item in prepared_items:
                apply_album_cache(item, album_cache)

            print("\nStep 3: uploading prepared files in one batch...")
            uploaded, return_code = upload_files(
                args.immich_go,
                [item["processed_file"] for item in prepared_items],
                args,
            )
            if not uploaded:
                for item in prepared_items:
                    results.append((
                        item["processed_file"],
                        False,
                        f"immich-go batch upload failed with exit code {return_code}",
                    ))
            else:
                for item in prepared_items:
                    try:
                        success, message = finish_item(item, args)
                    except Exception as exc:
                        success, message = False, str(exc)
                    results.append((item["processed_file"], success, message))
        except Exception as exc:
            for item in prepared_items:
                results.append((item["processed_file"], False, str(exc)))

    print_results(results)
    if any(not success for _, success, _ in results):
        sys.exit(1)


def print_results(results):

    print("\nProcessing results:")
    success_count = 0
    failure_count = 0
    for processed_file, success, message in results:
        line = f"{processed_file} -> {message}"
        if success:
            success_count += 1
            print(green(f"SUCCESS: {line}"))
        else:
            failure_count += 1
            print(red(f"FAILURE: {line}"))

    print(f"\nSummary: {success_count} successful, {failure_count} failed")


if __name__ == "__main__":
    main()
