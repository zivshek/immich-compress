import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import ffmpeg
from collections import Counter


SUPPORTED_VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm", ".wmv",
}


def configure_console_output():
    """Avoid crashing when the active Windows console cannot display a path."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


configure_console_output()


def get_file_size_mb(file_path):
    """Get the size of a file in megabytes."""
    size_bytes = os.path.getsize(file_path)
    size_mb = size_bytes / (1024 * 1024)
    return size_mb

def get_exiftool_path():
    """Find ExifTool from configuration, PATH, or the newest local installation."""
    candidates = [
        os.environ.get("EXIFTOOL_EXE"),
        shutil.which("exiftool"),
        shutil.which("exiftool.exe"),
    ]
    local_installs = glob.glob(r"C:\dev\exiftool-*_64\exiftool.exe")
    local_installs += glob.glob(r"C:\dev\exiftool-*_64\exiftool(-k).exe")
    candidates.extend(sorted(local_installs, key=os.path.getmtime, reverse=True))

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(
        "ExifTool was not found. Add it to PATH or set the EXIFTOOL_EXE environment variable."
    )

def get_video_resolution(file_path):
    """Get the resolution of a video file using ffmpeg-python."""
    try:
        probe = ffmpeg.probe(file_path)
        video_stream = next(
            (stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None
        )
        if video_stream:
            width = int(video_stream['width'])
            height = int(video_stream['height'])
            rotation = get_stream_rotation(video_stream)
            return width, height, rotation
        else:
            print(f"\033[91mERROR: No video stream found in {file_path}\033[0m")
            return None, None, None
    except ffmpeg.Error as e:
        print(f"\033[91mERROR: An exception occurred while getting video resolution: {e}\033[0m")
        return None, None, None

def get_stream_rotation(video_stream):
    """Extract rotation metadata from ffmpeg probe stream."""
    rotation = 0
    tags = video_stream.get('tags', {}) or {}
    try:
        if 'rotate' in tags:
            rotation = int(tags['rotate'])
        else:
            for side_data in video_stream.get('side_data_list', []) or []:
                if 'rotation' in side_data:
                    rotation = int(side_data['rotation'])
                    break
    except (ValueError, TypeError):
        rotation = 0
    return rotation


def get_4k_dimensions(width, height):
    """Scale the stored pixel matrix within 3840px long and 2160px short edges."""
    long_edge = max(width, height)
    short_edge = min(width, height)
    scale = min(3840 / long_edge, 2160 / short_edge)
    target_w = int(round(width * scale / 2)) * 2
    target_h = int(round(height * scale / 2)) * 2
    return target_w, target_h

def get_output_file(original_file, output_folder=None):
    base_name = f"{os.path.splitext(os.path.basename(original_file))[0]}-hbed.mp4"
    if output_folder:
        return os.path.join(output_folder, base_name)
    base, _ = os.path.splitext(original_file)
    return f"{base}-hbed.mp4"


def compress_with_handbrake(original_file, output_folder=None, upscale=False, verbose_output=False):
    original_size_mb = get_file_size_mb(original_file)
    print(f"Original file size: {original_size_mb:.2f} MB")

    output_file = get_output_file(original_file, output_folder)
    if output_file == original_file:
        return None, 0

    width, height, rotation = get_video_resolution(original_file)
    if width is None or height is None:
        return None, 0

    print(f"Original resolution: {width}x{height}")
    print(f"Rotation: {rotation}")
    print(f"Using preset: Fast 2160p60 4K HEVC")

    has_rotation = int(rotation) % 360 != 0

    if upscale:
        print("Upscaling stored pixel matrix to fit within 3840x2160")
        target_w, target_h = get_4k_dimensions(width, height)
    else:
        # Keep the original stored pixel matrix. Rotation is restored after
        # encoding so compatible clients display it in the intended orientation.
        target_w = width
        target_h = height

    print(f"Target encode dimensions: {target_w}x{target_h}")

    handbrake_command = [
        "C:\\HandBrakeCLI\\HandBrakeCLI.exe",
        "-i", original_file,
        "-o", output_file,
        "--non-anamorphic",
        "--width", str(target_w),
        "--height", str(target_h),
        "-O",
        "--preset", "Fast 2160p60 4K HEVC",
        "--encoder", "nvenc_h265"
    ]

    temporary_input_file = None
    if has_rotation:
        file_descriptor, temporary_input_file = tempfile.mkstemp(
            prefix="hbed-neutral-",
            suffix=".mp4",
            dir=os.path.dirname(original_file)
        )
        os.close(file_descriptor)
        os.remove(temporary_input_file)
        neutral_result = subprocess.run([
            "ffmpeg",
            "-y",
            "-display_rotation:v:0", "0",
            "-i", original_file,
            "-map", "0:v",
            "-map", "0:a?",
            "-c", "copy",
            temporary_input_file
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if neutral_result.returncode != 0:
            if os.path.exists(temporary_input_file):
                os.remove(temporary_input_file)
            print(f"\033[91mERROR: Failed to prepare metadata-neutral input: {neutral_result.stderr}\033[0m")
            return None, 0
        handbrake_command[handbrake_command.index("-i") + 1] = temporary_input_file

    print(f"Compressing {original_file} to {output_file}...")
    
    # Track progress stages
    current_stage = "Preparing"
    last_percentage = 0
    scanning_complete = False
    
    def print_progress_bar(percentage, stage="Processing"):
        """Print a progress bar with the current percentage"""
        bar_length = 40
        filled_length = int(bar_length * percentage / 100)
        bar = '#' * filled_length + '-' * (bar_length - filled_length)
        print(f"\r{stage}: [{bar}] {percentage:.1f}%", end='', flush=True)
    
    # Print output in real-time, but only show progress
    process = None
    try:
        # Run the process with real-time output streaming
        process = subprocess.Popen(
            handbrake_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                print()
                break
            
            if output:
                if verbose_output:
                    print(output.strip())
                
                # Check for completion markers first
                if "Encode done!" in output or "HandBrake has exited" in output:
                    if current_stage == "Encoding" and last_percentage > 0:
                        # Make sure we show 100% when complete
                        print_progress_bar(100.0, current_stage)
                    continue
                
                # Extract scanning percentage
                if "Scanning title" in output and "%" in output:
                    try:
                        percentage = float(output.split("%")[0].split(",")[-1].strip())
                        if percentage > last_percentage:
                            last_percentage = percentage
                            current_stage = "Scanning"
                            print_progress_bar(percentage, current_stage)
                    except ValueError:
                        pass
                
                # Extract encoding percentage
                elif "Encoding: task" in output and "%" in output:
                    try:
                        parts = output.split("%")[0].split(",")
                        percentage = float(parts[-1].strip())
                        
                        # Only when we see first encoding percentage, print a newline to separate from scanning
                        if not scanning_complete and "Encoding" in output:
                            scanning_complete = True
                            print_progress_bar(100, current_stage)
                            print()  # New line after scanning completes
                            last_percentage = 0  # Reset for encoding percentage
                        
                        if percentage > last_percentage:
                            last_percentage = percentage
                            current_stage = "Encoding"
                            print_progress_bar(percentage, current_stage)
                            
                            # Print ETA if available
                            if "ETA" in output:
                                eta = output.split("ETA")[1][:10].strip()
                                print(f"  (ETA: {eta})", end='', flush=True)
                    except ValueError:
                        pass
        
        # Make sure we finish with a newline
        print()
        
        # Wait for the process to fully complete
        process.wait()
        return_code = process.returncode
        
        if return_code == 0:
            # Verify the output file exists and has content
            if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                # Optionally dump ffprobe info for the output file when verbose to aid debugging
                try:
                    if verbose_output:
                        probe = subprocess.run([
                            "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", output_file
                        ], capture_output=True, text=True)
                        if probe.stdout:
                            print("ffprobe output for processed file:")
                            print(probe.stdout)
                except Exception:
                    pass

                compressed_size_mb = get_file_size_mb(output_file)
                size_saved_mb = original_size_mb - compressed_size_mb
                reduction_pct = (size_saved_mb / original_size_mb * 100) if original_size_mb else 0.0

                # Print the 'saved' part in green using ANSI escape codes
                print(f"original: {original_size_mb:.2f} MB, result: {compressed_size_mb:.2f} MB, \033[92msaved: {size_saved_mb:.2f} MB ({reduction_pct:.1f}% reduction)\033[0m")
                return output_file, size_saved_mb
            else:
                print(f"\033[91mERROR: HandBrake output file {output_file} does not exist or is empty\033[0m")
                return None, 0
        else:
            print(f"\033[91mERROR: HandBrake returned code {return_code}\033[0m")
            return None, 0
            
    except Exception as e:
        # Handle any errors in the output processing
        print(f"\033[91mERROR: An exception occurred while processing HandBrake output: {e}\033[0m")
        try:
            # Try to terminate the process if it's still running
            if process is not None and process.poll() is None:
                process.terminate()
        except:
            pass
        return None, 0
    finally:
        if temporary_input_file and os.path.exists(temporary_input_file):
            os.remove(temporary_input_file)

def apply_tags(original_file, compressed_file):
    """Copy metadata, including the original display rotation matrix."""
    print(f"Copying metadata...")
    args_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="hbed-exiftool-",
            suffix=".args",
            delete=False
        ) as file:
            args_file = file.name
            file.write("-TagsFromFile\n")
            file.write(f"{original_file}\n")
            file.write("-all\n")
            file.write("-all:all\n")
            file.write("-Rotation<Rotation\n")
            file.write(f"{compressed_file}\n")

        result = subprocess.run([
            get_exiftool_path(),
            "-charset", "filename=UTF8",
            "-@", args_file
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, errors="replace")
    finally:
        if args_file and os.path.exists(args_file):
            os.remove(args_file)

    if result.returncode == 0:
        print(f"Tags applied from {original_file} to {compressed_file}")
        return True
    else:
        if result.stderr:
            print(result.stderr)
        print(f"\033[91mError applying tags: {result.returncode}\033[0m")
        return False

def load_metadata_json(json_file):
    """Load global and optional per-file metadata injection settings."""
    with open(json_file, "r", encoding="utf-8-sig") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("Metadata JSON must be an object.")

    if "metadata" in data or "global" in data or "files" in data:
        global_metadata = data.get("metadata", data.get("global", {}))
        file_metadata = data.get("files", {})
    elif all(not isinstance(value, dict) for value in data.values()):
        global_metadata = data
        file_metadata = {}
    else:
        global_metadata = {}
        file_metadata = data

    if not isinstance(global_metadata, dict):
        raise ValueError("'metadata' must be an object.")
    if not isinstance(file_metadata, dict):
        raise ValueError("'files' must be an object.")

    return global_metadata, file_metadata

def metadata_for_file(file_path, global_metadata, file_metadata):
    """Return metadata for a file using full path, basename, or stem matches."""
    metadata = dict(global_metadata)
    absolute_path = os.path.abspath(file_path)
    candidates = [
        file_path,
        absolute_path,
        os.path.normpath(file_path),
        os.path.normpath(absolute_path),
        os.path.basename(file_path),
        os.path.splitext(os.path.basename(file_path))[0],
    ]

    for candidate in candidates:
        value = file_metadata.get(candidate)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"Metadata for {candidate} must be an object.")
            metadata.update(value)

    return metadata

def tag_assignments(tag, value):
    """Convert a JSON key/value pair into ExifTool assignment(s)."""
    tag_map = {
        "AndroidMake": ["Keys:AndroidMake"],
        "AndroidModel": ["Keys:AndroidModel"],
        "AndroidCaptureFPS": ["Keys:AndroidCaptureFPS"],
        "Make": ["UserData:Make", "XMP-tiff:Make"],
        "Model": ["UserData:Model", "XMP-tiff:Model"],
        "Software": ["UserData:Software", "XMP-tiff:Software"],
    }
    tags = tag_map.get(tag, [tag])

    if value is None:
        rendered_value = ""
    elif isinstance(value, bool):
        rendered_value = "true" if value else "false"
    elif isinstance(value, (list, tuple)):
        rendered_value = ", ".join(str(item) for item in value)
    else:
        rendered_value = str(value)

    return [f"-{mapped_tag}={rendered_value}" for mapped_tag in tags]

def inject_metadata(file_path, metadata):
    """Write manually specified metadata into an existing video file."""
    if not metadata:
        print(f"No metadata specified for {file_path}")
        return False

    print(f"Injecting metadata into {file_path}")
    args_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="hbed-exiftool-inject-",
            suffix=".args",
            delete=False
        ) as file:
            args_file = file.name
            for tag, value in metadata.items():
                for assignment in tag_assignments(tag, value):
                    file.write(f"{assignment}\n")
            file.write(f"{file_path}\n")

        result = subprocess.run([
            get_exiftool_path(),
            "-charset", "filename=UTF8",
            "-overwrite_original",
            "-@", args_file
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, errors="replace")
    finally:
        if args_file and os.path.exists(args_file):
            os.remove(args_file)

    if result.returncode == 0:
        return True

    if result.stderr:
        print(result.stderr)
    print(f"\033[91mError injecting metadata: {result.returncode}\033[0m")
    return False

def main():
    parser = argparse.ArgumentParser(description="Compress video files using HandBrake and copy metadata with ExifTool")
    parser.add_argument("folder_path", help="Path to the folder containing video files")
    parser.add_argument("--delete", action="store_true", help="Delete original files after successful compression (default: False)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Display detailed HandBrake output (default: False)")
    parser.add_argument("--upscale", action="store_true", help="Upscale videos to 4K")
    parser.add_argument("--metadata-only", action="store_true", help="Copy metadata to existing -hbed outputs without re-encoding")
    parser.add_argument("--inject-metadata", help="JSON file of metadata to inject into existing videos without re-encoding")
    parser.add_argument("--output-folder", help="Folder for -hbed outputs (default: same folder as input)")
    parser.add_argument("-r", "--recursive", action="store_true", help="Process folder recursively (default: False)")

    args = parser.parse_args()
    folder_path = args.folder_path
    delete_originals = args.delete
    verbose_output = args.verbose
    upscale = args.upscale
    metadata_only = args.metadata_only
    inject_metadata_file = args.inject_metadata
    output_folder = args.output_folder
    if output_folder:
        os.makedirs(output_folder, exist_ok=True)

    total_saved_size = 0
    
    files_to_process = []
    if args.recursive:
        for root, _, files in os.walk(folder_path):
            for f in files:
                files_to_process.append((root, f))
    else:
        for f in os.listdir(folder_path):
            if os.path.isfile(os.path.join(folder_path, f)):
                files_to_process.append((folder_path, f))

    print(f"Found {len(files_to_process)} file(s) to inspect in {folder_path}")

    global_injected_metadata = {}
    per_file_injected_metadata = {}
    if inject_metadata_file:
        global_injected_metadata, per_file_injected_metadata = load_metadata_json(inject_metadata_file)

    processed_count = 0
    skipped_count = 0
    for current_dir, file_name in files_to_process:
        original_file = os.path.join(current_dir, file_name)
        if os.path.splitext(file_name)[1].lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            print(f"Skipping unsupported file type: {original_file}")
            skipped_count += 1
            continue

        if inject_metadata_file:
            metadata = metadata_for_file(original_file, global_injected_metadata, per_file_injected_metadata)
            if metadata:
                if inject_metadata(original_file, metadata):
                    processed_count += 1
                else:
                    print(f"\033[91mFailed to inject metadata for {original_file}\033[0m")
            else:
                print(f"Skipping file without JSON metadata match: {original_file}")
                skipped_count += 1
            continue

        if "-hbed" in file_name.lower():
            print(f"Skipping already processed file: {original_file}")
            skipped_count += 1
            continue

        output_file = get_output_file(original_file, output_folder)
        if os.path.exists(output_file):
            if metadata_only:
                print(f"Copying metadata to existing output: {output_file}")
                if apply_tags(original_file, output_file):
                    processed_count += 1
                    artifact = output_file + "_original"
                    if os.path.exists(artifact):
                        os.remove(artifact)
                        print(f"{artifact} deleted.")
                else:
                    print(f"\033[91mFailed to copy metadata for {output_file}\033[0m")
            else:
                print(f"Skipping input with existing output: {original_file}")
            skipped_count += 1
            continue

        if metadata_only:
            print(f"Skipping input without existing output: {original_file}")
            skipped_count += 1
            continue

        print(f"Processing file: {original_file}")
        result = compress_with_handbrake(original_file, output_folder, upscale, verbose_output)
        if result and result[0]:
            processed_count += 1
            compressed_file, size_saved_mb = result
            total_saved_size += size_saved_mb
            tags_success = apply_tags(original_file, compressed_file)
            if tags_success:
                import time
                time.sleep(1)  # wait for ExifTool to release the file handle
                artifact = compressed_file + "_original"
                if os.path.exists(artifact):
                    os.remove(artifact)
                    print(f"{artifact} deleted.")
                if delete_originals:
                    time.sleep(1)  # wait for ExifTool to release the file handle
                    os.remove(original_file)
                    print(f"Original file {original_file} deleted.")
            else:
                print(f"\033[91mSkipping deletion of {original_file} due to tag application failure.\033[0m")
        else:
            print(f"\033[91mFailed to compress {original_file}\033[0m")

    print(f"Processed files: {processed_count}, skipped files: {skipped_count}")
    print(f"\033[92mCompressed total size: {total_saved_size:.2f} MB\033[0m")

if __name__ == "__main__":
    main()
