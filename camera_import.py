#!/usr/bin/env python3
# camera_import.py
# pull, sort, and verify camera footage into a structured archive.
#
# workflow (video):
#   1. pull    card -> <media>/The Dump/<ts>_<card>/
#   2. sort    interactive: pick a location per video, copy it plus sidecars to
#              The Footage/<Camera>/<Location>/, then re-verify destination
#              hashes against the dump checksums and delete the dump folder.
#              dump deletion is gated on the destination verify.
#   3. status  what is sorted vs unsorted across all dumps
#   4. verify  recheck a dump's internal hashes
#
# workflow (image / DCIM):
#   images     detect DCIM cards, prompt for an event name, pull JPG/HEIC/RAW
#              straight to The Images/<Event>/, verify on destination.
#
# repair:
#   backfill   footage sorted by an older version has the .MP4 but no M01.XML
#              and no T01.JPG. those sidecars only exist on the original card.
#              backfill matches footage videos to mounted-card clips by
#              (name, size) and copies the missing sidecars in. read-only by
#              default; --apply copies; --accept marks the gaps no card can
#              fill as final, so the archive reads complete.
#
# sidecar handling:
#   SONY    video file        Sony FX300814.MP4
#           camera metadata   Sony FX300814M01.XML   (CLIP/ same folder)
#           thumbnail         Sony FX300814T01.JPG   (THMBNL/ sibling folder)
#   DJI     video             DJI_0001.MP4
#           GPS telemetry     DJI_0001.SRT
#           low-res preview   DJI_0001.LRF
#   IPHONE  video             IMG_0001.MOV
#           edit metadata     IMG_0001.AAE

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import xxhash


# ============================================================
# Configuration
# ============================================================

# override with CAMERA_MEDIA_BASE
MEDIA_BASE = Path(os.environ.get("CAMERA_MEDIA_BASE", Path.home() / "Documents" / "Media")).expanduser()
THE_DUMP = MEDIA_BASE / "The Dump"
THE_FOOTAGE = MEDIA_BASE / "The Footage"
THE_IMAGES = MEDIA_BASE / "The Images"
LOG_FILE = MEDIA_BASE / "import_log.txt"

# Videos accepted as permanently sidecar-less (their original card is gone).
# backfill records them here so it stops flagging them and reports the archive
# complete. Anything missing a sidecar that is NOT listed here is a new problem.
SIDECAR_BASELINE = MEDIA_BASE / ".sidecar_baseline.json"

# Camera definitions.
#   folder            destination top-level folder under The Footage
#   sidecar_strategy  "sony"      - Sony FX30 cross-folder match (M01.XML + THMBNL/T01.JPG)
#                     "extension" - same-stem files matching the extension list
CAMERAS = {
    "SONY": {
        "folder": "Sony SLOG-3",
        "sidecar_strategy": "sony",
    },
    "DJI": {
        "folder": "DJI DLOG-M",
        "sidecar_strategy": "extension",
        "sidecar_extensions": [".srt", ".lrf", ".xmp", ".dng"],
    },
    "IPHONE": {
        "folder": "Apple ProRes-Log",
        "sidecar_strategy": "extension",
        "sidecar_extensions": [".aae", ".xmp", ".heic", ".jpg", ".jpeg"],
    },
}

# Substrings that signal a camera body, not a location. Guards select_country()
# against the Mavic Mini regression where a folder named after a camera model
# became a "location" in the picker. Curated to be distinctive (no false hits
# in real country/event names).
CAMERA_MODEL_KEYWORDS = (
    # DJI bodies
    "mavic", "phantom", "inspire", "osmo", "ronin",
    # GoPro
    "gopro",
    # Apple
    "iphone", "ipad",
    # Sony cinema bodies
    "fx30", "fx3", "fx6", "fx9",
    # cinema heavyweights
    "ursa", "komodo", "alexa",
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf"}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png",
    ".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".raw",
}

# Substring match against /Volumes/<name> for SD card detection
CARD_PATTERNS = ["SD512", "SD256", "SD128", "UNTITLED", "NO NAME",
                 "EOS_DIGITAL", "SONY", "DJI", "NIKON"]

HASH_CHUNK = 1024 * 1024  # 1 MiB
HASH_ALGORITHM = "xxh3_128"


# ============================================================
# Hash helpers
# ============================================================

def hash_file(path, algorithm=HASH_ALGORITHM):
    """Stream-hash a file. Default xxh3_128 (fast); 'md5' for legacy dumps."""
    if algorithm == "xxh3_128":
        h = xxhash.xxh3_128()
        block = HASH_CHUNK
    elif algorithm == "md5":
        import hashlib
        h = hashlib.md5()
        block = 65536
    else:
        raise ValueError(f"Unknown hash algorithm: {algorithm}")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_checksums(directory):
    """Hash every non-hidden file in a directory tree.
    Returns {relpath: hash, '_algorithm': name}. The algorithm marker lets
    verify_dump detect legacy MD5 dumps written before 2026.
    """
    directory = Path(directory)
    checksums = {"_algorithm": HASH_ALGORITHM}
    for f in sorted(directory.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            checksums[str(f.relative_to(directory))] = hash_file(f)
    return checksums


# ============================================================
# Camera + sidecar discovery
# ============================================================

def detect_camera(file_path):
    """Identify camera type from path + filename."""
    p = str(file_path)
    name = file_path.name.upper()
    if "M4ROOT" in p or "PRIVATE" in p:
        return "SONY"
    if name.startswith("DJI_"):
        return "DJI"
    if file_path.suffix.upper() == ".MOV":
        return "IPHONE"
    return "UNKNOWN"


def find_sidecars(video_path, camera_type, dump_root=None):
    """Return all sidecar/metadata files associated with a video.

    dump_root is the dump folder (or card root). Sony needs it because
    thumbnails live in PRIVATE/M4ROOT/THMBNL/, not next to the video.
    If omitted, we walk up from the video to find a PRIVATE/M4ROOT ancestor.
    """
    strategy = CAMERAS.get(camera_type, {}).get("sidecar_strategy")
    if strategy == "sony":
        root = Path(dump_root) if dump_root else _guess_sony_dump_root(video_path)
        return find_sony_sidecars(video_path, root)
    if strategy == "extension":
        return find_extension_sidecars(video_path, camera_type)
    return []


def _guess_sony_dump_root(video_path):
    """Walk upward from a Sony video file to find the dump root (parent of PRIVATE/)."""
    p = video_path.parent
    while p != p.parent:
        if (p / "PRIVATE" / "M4ROOT").exists():
            return p
        p = p.parent
    return video_path.parent


def find_sony_sidecars(video_path, dump_root):
    """Sony FX30 layout:
       <dump>/PRIVATE/M4ROOT/CLIP/<stem>.MP4           - video
       <dump>/PRIVATE/M4ROOT/CLIP/<stem>M01.XML        - per-clip metadata
       <dump>/PRIVATE/M4ROOT/THMBNL/<stem>T01.JPG      - thumbnail
    Case-insensitive: we iterate the real folder and match by upper-cased stem,
    so the on-disk filename (preserving its real case) is what gets returned.
    """
    sidecars = []
    stem_upper = video_path.stem.upper()  # e.g. "SONY FX300814"

    # XML lives next to the .MP4
    xml_target = f"{stem_upper}M01"
    try:
        siblings = list(video_path.parent.iterdir())
    except OSError:
        siblings = []
    for sibling in siblings:
        if (sibling.is_file()
                and sibling.stem.upper() == xml_target
                and sibling.suffix.upper() == ".XML"):
            sidecars.append(sibling)
            break

    # Thumbnail lives in THMBNL/
    thmbnl = Path(dump_root) / "PRIVATE" / "M4ROOT" / "THMBNL"
    if thmbnl.is_dir():
        thm_target = f"{stem_upper}T01"
        try:
            thm_siblings = list(thmbnl.iterdir())
        except OSError:
            thm_siblings = []
        for sibling in thm_siblings:
            if (sibling.is_file()
                    and sibling.stem.upper() == thm_target
                    and sibling.suffix.upper() == ".JPG"):
                sidecars.append(sibling)
                break

    return sidecars


def find_extension_sidecars(video_path, camera_type):
    """Same-stem siblings whose extension is in the camera's sidecar list."""
    extensions = {ext.lower() for ext in
                  CAMERAS.get(camera_type, {}).get("sidecar_extensions", [])}
    if not extensions:
        return []
    stem_upper = video_path.stem.upper()
    sidecars = []
    try:
        siblings = list(video_path.parent.iterdir())
    except OSError:
        return []
    for sibling in siblings:
        if (sibling.is_file()
                and not sibling.name.startswith(".")
                and sibling != video_path
                and sibling.stem.upper() == stem_upper
                and sibling.suffix.lower() in extensions):
            sidecars.append(sibling)
    return sidecars


def find_video_files(search_path):
    """Find every video file under search_path with its camera type and sidecars.
    Returns [(path, camera_type, [sidecars])]."""
    search_path = Path(search_path)
    results = []
    for f in sorted(search_path.rglob("*")):
        if (f.is_file()
                and not f.name.startswith(".")
                and f.suffix.lower() in VIDEO_EXTENSIONS):
            cam = detect_camera(f)
            scs = find_sidecars(f, cam, dump_root=search_path)
            results.append((f, cam, scs))
    return results


def find_image_files(search_path):
    """Find every image file under search_path (for DCIM photo pulls)."""
    search_path = Path(search_path)
    results = []
    for f in sorted(search_path.rglob("*")):
        if (f.is_file()
                and not f.name.startswith(".")
                and f.suffix.lower() in IMAGE_EXTENSIONS):
            results.append(f)
    return results


# ============================================================
# Card detection
# ============================================================

def find_mounted_cards():
    """Detect mounted SD cards / camera volumes (any kind)."""
    volumes = Path("/Volumes")
    if not volumes.exists():
        return []
    cards = []
    for vol in sorted(volumes.iterdir()):
        if vol.name.startswith("."):
            continue
        name_upper = vol.name.upper()
        if any(p in name_upper for p in CARD_PATTERNS):
            cards.append(vol)
            continue
        if (vol / "PRIVATE" / "M4ROOT").exists() or (vol / "DCIM").exists():
            cards.append(vol)
    return cards


def find_dcim_cards():
    """Detect mounted volumes with a DCIM folder (iPhone, Sony stills, Nikon...)."""
    volumes = Path("/Volumes")
    if not volumes.exists():
        return []
    return [v for v in sorted(volumes.iterdir())
            if not v.name.startswith(".") and (v / "DCIM").is_dir()]


# ============================================================
# File counting + footage indexing
# ============================================================

def count_files(path, exclude_hidden=True):
    """Count files under path. Excludes dotfiles by default."""
    path = Path(path)
    count = 0
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        if exclude_hidden and f.name.startswith("."):
            continue
        count += 1
    return count


def build_footage_index():
    """{video_filename: footage_path} for non-zero video files in The Footage.
    Zero-byte files are skipped (iCloud ghost files - treated as unsorted)."""
    index = {}
    if not THE_FOOTAGE.exists():
        return index
    for f in THE_FOOTAGE.rglob("*"):
        if (f.is_file()
                and not f.name.startswith(".")
                and f.suffix.lower() in VIDEO_EXTENSIONS):
            try:
                if f.stat().st_size > 0:
                    index[f.name] = f
            except OSError:
                continue
    return index


def build_footage_size_index():
    """{(filename, size)} for non-zero videos in The Footage. Used at pull
    time to dedup against already-sorted clips by (name, size) - needed
    because Sony reuses clip numbers after a card format."""
    index = set()
    if not THE_FOOTAGE.exists():
        return index
    for f in THE_FOOTAGE.rglob("*"):
        if (f.is_file()
                and not f.name.startswith(".")
                and f.suffix.lower() in VIDEO_EXTENSIONS):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size > 0:
                index.add((f.name, size))
    return index


def find_card_excludes(card_path, size_index):
    """Build rsync --exclude-from patterns for videos already in The Footage
    (matched by name+size) AND their sidecars. Returns
    (patterns, skipped_videos, skipped_bytes)."""
    card_path = Path(card_path)
    patterns = []
    skipped_videos = 0
    skipped_bytes = 0

    for f in card_path.rglob("*"):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if (f.name, size) not in size_index:
            continue

        patterns.append("/" + f.relative_to(card_path).as_posix())
        skipped_videos += 1
        skipped_bytes += size

        # Also exclude sidecars for the skipped video
        camera = detect_camera(f)
        for sc in find_sidecars(f, camera, dump_root=card_path):
            patterns.append("/" + sc.relative_to(card_path).as_posix())

    return patterns, skipped_videos, skipped_bytes


# ============================================================
# Pull workflow (card -> local dump)
# ============================================================

def find_existing_dump_for_card(card_name, source_count):
    """Return path to an existing COMPLETE dump for this card whose source
    file count matches - i.e., a pull that already captured the same content.
    Returns None if no duplicate found. Used to guard against double-pulling.
    """
    if not THE_DUMP.exists():
        return None
    for d in sorted(THE_DUMP.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not d.name.endswith(f"_{card_name}"):
            continue
        manifest_file = d / "manifest.json"
        if not manifest_file.exists():
            continue  # incomplete pull, ignore
        try:
            m = json.loads(manifest_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (m.get("card_name") == card_name
                and m.get("source_file_count") == source_count):
            return d
    return None


def pull_card(card_path, force=False):
    """Copy an entire card to The Dump/<ts>_<card>/. Verify count, compute
    xxHash checksums, write manifest.

    Guards against accidental duplicate pulls: if a completed dump already
    exists for this card with the same source-file count, prompts to skip
    (unless force=True).
    """
    card_path = Path(card_path)
    card_name = card_path.name

    print(f"\n{'=' * 60}")
    print(f"PULLING: {card_name}")
    print(f"{'=' * 60}")

    print("Counting source files...")
    source_count = count_files(card_path)
    print(f"  Source: {source_count} files on card")

    # Duplicate-pull guard
    if not force:
        existing = find_existing_dump_for_card(card_name, source_count)
        if existing is not None:
            print(f"\n  warning:  A complete dump for {card_name} already exists with the")
            print(f"     same file count ({source_count}): {existing.name}")
            print(f"     This would create a duplicate of the same card contents.")
            ans = input("  Pull anyway? (y/N): ").strip().lower()
            if ans != "y":
                print(f"  Skipped. Existing dump: {existing}")
                log(f"Skipped duplicate pull of {card_name} (existing: {existing.name})")
                return existing

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dump_name = f"{timestamp}_{card_name}"
    dump_path = THE_DUMP / dump_name

    dump_path.mkdir(parents=True, exist_ok=True)

    # Dedup against already-sorted clips
    size_index = build_footage_size_index()
    excludes, skipped_videos, skipped_bytes = find_card_excludes(card_path, size_index)
    exclude_file = None
    extra_args = []
    if excludes:
        fd, exclude_file = tempfile.mkstemp(prefix="rsync_excludes_", suffix=".txt")
        with os.fdopen(fd, "w") as ef:
            ef.write("\n".join(excludes) + "\n")
        extra_args = ["--exclude-from", exclude_file]
        gb = skipped_bytes / (1024 ** 3)
        print(f"  Skipping {skipped_videos} videos (+sidecars) already in The Footage ({gb:.2f} GiB)")

    print("Copying card contents...")
    rsync_cmd = [
        "rsync", "-ah", "--progress",
        "--exclude", ".Spotlight-V100",
        "--exclude", ".fseventsd",
        "--exclude", ".Trashes",
        "--exclude", ".DS_Store",
        "--exclude", "._*",
        *extra_args,
        f"{card_path}/", f"{dump_path}/",
    ]
    try:
        result = subprocess.run(rsync_cmd)
        if result.returncode != 0:
            print(f"WARNING: rsync exited with code {result.returncode}")
    finally:
        if exclude_file:
            Path(exclude_file).unlink(missing_ok=True)

    dest_count = count_files(dump_path)
    expected = source_count - len(excludes)
    print(f"\n  Source: {source_count} files")
    print(f"  Skipped (already in footage): {len(excludes)}")
    print(f"  Copied: {dest_count} files")
    if dest_count >= expected:
        print("  File count: VERIFIED")
    else:
        print(f"  WARNING: {expected - dest_count} files may be missing!")

    print("Generating checksums (xxh3_128)...")
    checksums = compute_checksums(dump_path)
    checksum_count = sum(1 for k in checksums if k != "_algorithm")
    with open(dump_path / "checksums.json", "w") as f:
        json.dump(checksums, f, indent=2)
    print(f"  {checksum_count} checksums saved.")

    videos = find_video_files(dump_path)
    camera_counts = {}
    for _, cam, _ in videos:
        camera_counts[cam] = camera_counts.get(cam, 0) + 1

    manifest = {
        "card_name": card_name,
        "pulled_at": timestamp,
        "source_path": str(card_path),
        "dump_path": str(dump_path),
        "source_file_count": source_count,
        "copied_file_count": dest_count,
        "video_count": len(videos),
        "cameras_detected": camera_counts,
        "checksum_count": checksum_count,
        "skipped_videos_already_in_footage": skipped_videos,
        "skipped_bytes_already_in_footage": skipped_bytes,
    }
    with open(dump_path / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("\nPull summary:")
    print(f"  Location: {dump_path}")
    print(f"  Videos: {len(videos)}")
    for cam, cnt in sorted(camera_counts.items()):
        print(f"    {cam}: {cnt}")

    log(f"Pulled {card_name} -> {dump_name} ({dest_count} files, {len(videos)} videos)")
    print("Pull complete.")
    return dump_path


# Backwards-compat alias - older docs and habits still use `dump`
dump_card = pull_card


# ============================================================
# Sort workflow (dump -> footage, then verify + mirror + cleanup)
# ============================================================

def parse_selection(text, max_count):
    """Parse '1-5,8,12' into zero-based indices. Returns [] on parse error."""
    indices = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                for i in range(int(a), int(b) + 1):
                    if 1 <= i <= max_count:
                        indices.add(i - 1)
            except ValueError:
                return []
        else:
            try:
                i = int(part)
                if 1 <= i <= max_count:
                    indices.add(i - 1)
            except ValueError:
                return []
    return sorted(indices)


def select_country():
    """Pick a destination country/event. Shows existing, accepts new. Guards
    against camera-body names like 'Mavic Mini' that would create a misclass-
    ified location bucket; you can still override with y."""
    existing = set()
    if THE_FOOTAGE.exists():
        for cam_dir in THE_FOOTAGE.iterdir():
            if cam_dir.is_dir() and not cam_dir.name.startswith("."):
                for country_dir in cam_dir.iterdir():
                    if country_dir.is_dir() and not country_dir.name.startswith("."):
                        existing.add(country_dir.name)
    if existing:
        print("  Existing locations:")
        for name in sorted(existing):
            print(f"    {name}")
        print()
    while True:
        country = input("  Country (or new name): ").strip()
        if not country:
            print("  Enter a country name.")
            continue
        low = country.lower()
        if any(kw in low for kw in CAMERA_MODEL_KEYWORDS):
            print(f"  warning: '{country}' looks like a camera body, not a location.")
            print("    The Footage is <Camera>/<Location>/ - type a country or event.")
            ans = input("    Use it anyway? (y/N): ").strip().lower()
            if ans != "y":
                continue
        return country


def build_dest_path(camera_type, country):
    """Build destination folder path: The Footage / <camera folder> / <country>."""
    return THE_FOOTAGE / CAMERAS[camera_type]["folder"] / country


def copy_one_file(src, dest_file):
    """rsync a single file. Handles iCloud-ghost destinations (0-byte stubs
    from prior failed copies). Returns True on success."""
    needs_copy = not dest_file.exists()
    if not needs_copy:
        try:
            needs_copy = dest_file.stat().st_size == 0
        except OSError:
            needs_copy = True

    if not needs_copy:
        return True  # already a real file

    result = subprocess.run(["rsync", "-a", str(src), str(dest_file)])
    if (result.returncode == 0
            and dest_file.exists()
            and dest_file.stat().st_size > 0):
        try:
            dest_file.chmod(0o644)  # iCloud's `bird` needs 644 to index
        except OSError:
            pass
        return True

    # Clean up ghost so the next attempt isn't blocked
    try:
        if dest_file.exists() and dest_file.stat().st_size == 0:
            dest_file.unlink()
    except OSError:
        pass
    return False


def sort_dump(dump_path):
    """Interactive sort: assign each video to a country, copy + sidecars.

    After the user finishes (or if everything was already sorted), runs the
    post-sort chain: verify -> mirror -> cleanup.
    """
    dump_path = Path(dump_path)
    if not dump_path.exists():
        print(f"Not found: {dump_path}")
        return

    all_videos = find_video_files(dump_path)
    if not all_videos:
        print("No video files found in this dump.")
        _post_sort_cleanup(dump_path)
        return

    footage_index = build_footage_index()
    already_sorted = [v for v in all_videos if v[0].name in footage_index]
    videos = [v for v in all_videos if v[0].name not in footage_index]

    print(f"\n{'=' * 60}")
    print(f"SORTING: {dump_path.name}")
    print(f"{'=' * 60}")
    print(f"Found {len(all_videos)} video files")
    if already_sorted:
        print(f"  Already sorted: {len(already_sorted)} (skipping)")
    if not videos:
        print("\nAll files in this dump are already sorted.")
        _post_sort_cleanup(dump_path)
        return

    print(f"  To sort: {len(videos)}\n")
    for i, (vpath, cam, scs) in enumerate(videos, 1):
        sc_txt = f" +{len(scs)} sidecars" if scs else ""
        print(f"  {i:3d}. [{cam:<7}] {vpath.name}{sc_txt}")

    while videos:
        print("\nCommands:")
        print("  Numbers (e.g. 1-5,8,12) - select files to sort")
        print("  A - select all")
        print("  Q - quit sorting (skip cleanup; dump kept)")

        choice = input("\nSelect: ").strip().upper()
        if choice == "Q":
            print("Quit - dump kept for resume.")
            return

        if choice == "A":
            selected_idx = list(range(len(videos)))
        else:
            selected_idx = parse_selection(choice, len(videos))
            if not selected_idx:
                print("Invalid selection.")
                continue

        selected = [videos[i] for i in selected_idx]
        print(f"\n{len(selected)} files selected. Choose destination:\n")
        country = select_country()

        copied = 0
        failed_idx = set()
        for sel_i, (vpath, cam, scs) in zip(selected_idx, selected):
            if cam == "UNKNOWN":
                print(f"  Skipping (unknown camera): {vpath.name}")
                failed_idx.add(sel_i)
                continue

            dest = build_dest_path(cam, country)
            dest.mkdir(parents=True, exist_ok=True)

            dest_file = dest / vpath.name
            if copy_one_file(vpath, dest_file):
                print(f"  -> {vpath.name}")
                copied += 1
            else:
                print(f"  !! {vpath.name} (copy failed)")
                failed_idx.add(sel_i)
                continue

            for sc in scs:
                sc_dest = dest / sc.name
                if copy_one_file(sc, sc_dest):
                    print(f"     + {sc.name}")
                else:
                    print(f"     !! {sc.name} (copy failed)")

        print(f"\n{copied} files copied to {country}")
        if failed_idx:
            print(f"{len(failed_idx)} files failed (kept in list for retry)")

        done = set(selected_idx) - failed_idx
        remaining = set(range(len(videos))) - done
        videos = [videos[i] for i in sorted(remaining)]

        if videos:
            print(f"\n{len(videos)} files remaining:")
            for i, (vpath, cam, scs) in enumerate(videos, 1):
                sc_txt = f" +{len(scs)} sidecars" if scs else ""
                print(f"  {i:3d}. [{cam:<7}] {vpath.name}{sc_txt}")

    print("\nAll files in dump are now sorted.")
    _post_sort_cleanup(dump_path)


def _post_sort_cleanup(dump_path):
    """Post-sort chain: verify destination hashes, then delete the dump.
    Deletion is gated on the verify passing."""
    print(f"\n{'-' * 60}")
    print(f"VERIFY -> CLEANUP: {dump_path.name}")
    print(f"{'-' * 60}")

    sorted_list, unsorted_list = reconcile_dump(dump_path)

    if unsorted_list:
        print(f"  {len(unsorted_list)} videos still unsorted - keeping dump.")
        log(f"Sort incomplete for {dump_path.name}: {len(unsorted_list)} unsorted")
        return

    if sorted_list:
        print(f"  Verifying {len(sorted_list)} videos + sidecars on destination...")
        if not verify_sorted_against_dump(dump_path, sorted_list):
            print("  Verification FAILED - keeping dump for inspection.")
            log(f"Verify FAILED for {dump_path.name} - dump kept")
            return
        print("  All sorted files verified.")
        log(f"Verify OK for {dump_path.name}: {len(sorted_list)} videos + sidecars match dump checksums")
    else:
        print("  No videos to verify.")

    # Soft warning: orphaned images in the dump that won't reach The Images
    image_files = find_image_files(dump_path)
    # Sony THMBNL/*T01.JPG were already sorted as video sidecars - exclude them
    orphans = [img for img in image_files
               if not (img.parent.name.upper() == "THMBNL"
                       or img.stem.upper().endswith("T01"))]
    if orphans:
        print(f"  NOTE: {len(orphans)} photo file(s) in dump will be deleted.")
        print(f"        Run `images` command before sort if you want to keep them.")

    print(f"  Deleting dump: {dump_path.name}")
    shutil.rmtree(dump_path)
    log(f"Cleaned up dump after verified sort: {dump_path.name}")
    print("Done.")


def verify_sorted_against_dump(dump_path, sorted_list):
    """Recompute each sorted file's hash on the footage destination and
    compare against the dump's stored hash. Also verifies each video's
    sidecars (XML, thumbnail). True iff every checked file matches."""
    dump_path = Path(dump_path)
    checksum_file = dump_path / "checksums.json"
    if not checksum_file.exists():
        print("  No checksums.json - cannot verify.")
        return False

    with open(checksum_file) as f:
        stored = json.load(f)
    algorithm = stored.pop("_algorithm", "md5")

    failures = []
    for dump_video, footage_video, camera in sorted_list:
        rel = str(dump_video.relative_to(dump_path))
        expected = stored.get(rel)
        if expected is None:
            failures.append(f"{dump_video.name}: not in checksums.json")
            continue
        try:
            actual = hash_file(footage_video, algorithm=algorithm)
        except OSError as e:
            failures.append(f"{footage_video.name}: read error: {e}")
            continue
        if actual != expected:
            failures.append(f"{footage_video.name}: hash mismatch")
            continue

        # Verify sidecars too
        for sc in find_sidecars(dump_video, camera, dump_root=dump_path):
            sc_rel = str(sc.relative_to(dump_path))
            sc_expected = stored.get(sc_rel)
            if sc_expected is None:
                continue  # sidecar wasn't checksummed; skip (shouldn't happen post-pull)
            sc_footage = footage_video.parent / sc.name
            if not sc_footage.exists():
                failures.append(f"{sc.name}: missing from footage")
                continue
            try:
                sc_actual = hash_file(sc_footage, algorithm=algorithm)
            except OSError as e:
                failures.append(f"{sc.name}: read error: {e}")
                continue
            if sc_actual != sc_expected:
                failures.append(f"{sc.name}: hash mismatch")

    if failures:
        for line in failures[:10]:
            print(f"    !! {line}")
        if len(failures) > 10:
            print(f"    !! ...and {len(failures) - 10} more")
        return False
    return True


# ============================================================
# Verify (re-check a dump's stored checksums)
# ============================================================

def verify_dump(dump_path):
    """Recompute every file's hash in a dump and compare to checksums.json."""
    dump_path = Path(dump_path)
    checksum_file = dump_path / "checksums.json"
    if not checksum_file.exists():
        print(f"No checksums.json in {dump_path}")
        return False

    print(f"Verifying: {dump_path.name}")
    with open(checksum_file) as f:
        stored = json.load(f)
    algorithm = stored.pop("_algorithm", "md5")
    if algorithm == "xxh3_128":
        print("  Algorithm: xxHash (XXH3-128)")
    else:
        print(f"  Algorithm: {algorithm.upper()} (legacy)")

    print(f"  Checking {len(stored)} files...")
    missing, mismatches = [], []
    for rel, expected in stored.items():
        full = dump_path / rel
        if not full.exists():
            missing.append(rel)
            continue
        try:
            actual = hash_file(full, algorithm=algorithm)
        except OSError as e:
            mismatches.append(f"{rel} (read error: {e})")
            continue
        if actual != expected:
            mismatches.append(rel)

    if not missing and not mismatches:
        print(f"  All {len(stored)} files verified OK.")
        return True
    if missing:
        print(f"  MISSING: {len(missing)} files")
        for m in missing[:10]:
            print(f"    - {m}")
    if mismatches:
        print(f"  CORRUPT: {len(mismatches)} files")
        for m in mismatches[:10]:
            print(f"    - {m}")
    return False


# ============================================================
# Reconcile / status
# ============================================================

def reconcile_dump(dump_path, footage_index=None):
    """Compare a dump's videos against The Footage by filename.
    Returns (sorted_list, unsorted_list)."""
    dump_path = Path(dump_path)
    if footage_index is None:
        footage_index = build_footage_index()
    videos = find_video_files(dump_path)
    sorted_list, unsorted_list = [], []
    for vpath, cam, scs in videos:
        if vpath.name in footage_index:
            sorted_list.append((vpath, footage_index[vpath.name], cam))
        else:
            unsorted_list.append((vpath, cam, scs))
    return sorted_list, unsorted_list


def print_status(dump_path=None):
    """Print reconciliation status for one dump or all dumps."""
    footage_index = build_footage_index()
    if dump_path:
        dumps = [Path(dump_path)]
    elif not THE_DUMP.exists():
        print("No dumps found.")
        return
    else:
        dumps = sorted([d for d in THE_DUMP.iterdir()
                        if d.is_dir() and not d.name.startswith(".")])

    if not dumps:
        print("No dumps found.")
        return

    total_sorted, total_unsorted = 0, 0
    for dump in dumps:
        sorted_files, unsorted_files = reconcile_dump(dump, footage_index)
        total = len(sorted_files) + len(unsorted_files)
        total_sorted += len(sorted_files)
        total_unsorted += len(unsorted_files)

        if total == 0:
            print(f"\n{dump.name}: no video files")
            continue

        print(f"\n{'=' * 60}")
        print(dump.name)
        print(f"{'=' * 60}")
        print(f"  Total videos: {total}")
        print(f"  Sorted:       {len(sorted_files)}")
        print(f"  Unsorted:     {len(unsorted_files)}")

        if sorted_files:
            dests = {}
            for _, dest, _ in sorted_files:
                loc = "/".join(dest.relative_to(THE_FOOTAGE).parts[:-1])
                dests[loc] = dests.get(loc, 0) + 1
            print("\n  Sorted destinations:")
            for loc, cnt in sorted(dests.items()):
                print(f"    {loc}: {cnt}")

        if unsorted_files:
            by_cam = {}
            for vpath, cam, _ in unsorted_files:
                by_cam.setdefault(cam, []).append(vpath.name)
            print("\n  Unsorted by camera:")
            for cam, names in sorted(by_cam.items()):
                names.sort()
                print(f"    {cam}: {len(names)} files")
                if len(names) <= 5:
                    for n in names:
                        print(f"      {n}")
                else:
                    print(f"      {names[0]} ... {names[-1]}")

    if len(dumps) > 1:
        print(f"\n{'=' * 60}")
        print(f"TOTAL across {len(dumps)} dumps")
        print(f"  Sorted:   {total_sorted}")
        print(f"  Unsorted: {total_unsorted}")


# ============================================================
# Images workflow (DCIM -> The Images/<Event>/)
# ============================================================

def select_event():
    """Pick a destination event folder under The Images. New names allowed."""
    existing = set()
    if THE_IMAGES.exists():
        for d in THE_IMAGES.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                existing.add(d.name)
    if existing:
        print("  Existing events:")
        for name in sorted(existing):
            print(f"    {name}")
        print()
    while True:
        event = input("  Event name (or new): ").strip()
        if event:
            return event
        print("  Enter an event name.")


def pull_images_from_card(card_path, event_name):
    """Pull DCIM images from one card to The Images/<event>/, verify, mirror."""
    card_path = Path(card_path)
    dcim = card_path / "DCIM"
    if not dcim.is_dir():
        print(f"  No DCIM folder on {card_path.name}")
        return 0

    images = find_image_files(dcim)
    print(f"\n  Found {len(images)} images on {card_path.name}/DCIM")
    if not images:
        return 0

    dest = THE_IMAGES / event_name
    dest.mkdir(parents=True, exist_ok=True)

    # Hash sources up-front (single read) so we can verify after copy
    print(f"  Hashing {len(images)} sources...")
    source_hashes = {}
    for img in images:
        try:
            source_hashes[img.name] = hash_file(img)
        except OSError as e:
            print(f"    !! cannot read {img.name}: {e}")

    print(f"  Copying to {dest}...")
    copied, failed = 0, 0
    for img in images:
        if copy_one_file(img, dest / img.name):
            copied += 1
        else:
            failed += 1
            print(f"    !! copy failed: {img.name}")
    print(f"  Copied: {copied}, failed: {failed}")

    print("  Verifying destination hashes...")
    mismatches = []
    for img in images:
        if img.name not in source_hashes:
            continue
        dest_file = dest / img.name
        if not dest_file.exists():
            mismatches.append(img.name)
            continue
        try:
            actual = hash_file(dest_file)
        except OSError:
            mismatches.append(img.name)
            continue
        if actual != source_hashes[img.name]:
            mismatches.append(img.name)

    if mismatches:
        print(f"  WARNING: {len(mismatches)} images failed verification:")
        for m in mismatches[:10]:
            print(f"    - {m}")
    else:
        print(f"  All {copied} images verified.")

    log(f"Pulled {copied} images from {card_path.name} -> {event_name}")
    return copied


def run_images_command():
    """Entry point for the `images` CLI verb."""
    cards = find_dcim_cards()
    if not cards:
        print("No DCIM cards detected.")
        return

    print(f"\nDetected {len(cards)} DCIM card(s):")
    for i, c in enumerate(cards, 1):
        print(f"  {i}. {c.name}")

    if len(cards) == 1:
        selected = cards
    else:
        sel = input("\nSelect card (number, or A for all): ").strip().upper()
        if sel == "A":
            selected = cards
        else:
            try:
                selected = [cards[int(sel) - 1]]
            except (ValueError, IndexError):
                print("Invalid selection.")
                return

    print("\nChoose event:")
    event = select_event()

    for card in selected:
        pull_images_from_card(card, event)


# ============================================================
# Backfill (recover missing sidecars from the original cards)
# ============================================================

def _strip_sony_suffix(stem):
    # drop a trailing sony suffix like M01 / T01 so a sidecar stem matches its
    # video stem
    return re.sub(r"(?i)[MT]\d+$", "", stem)


def _camera_from_footage_path(path):
    # in The Footage the camera is encoded in the top-level folder name, not in
    # the file path the way detect_camera expects on a card
    try:
        top = path.relative_to(THE_FOOTAGE).parts[0]
    except (ValueError, IndexError):
        return "UNKNOWN"
    for key, d in CAMERAS.items():
        if d["folder"] == top:
            return key
    return "UNKNOWN"


def _missing_sidecars_beside(video):
    """For a sorted footage video, return (miss_xml, miss_thumb). Sidecars sit
    in the same folder as the video once sorted.

    Only Sony is checked. The pre-fix bug was Sony-specific (cross-folder THMBNL
    plus the M01/T01 suffix). DJI and iPhone keep same-stem sidecars next to the
    video, were never affected, and their sidecars are optional, so non-Sony
    videos are reported complete."""
    cam = _camera_from_footage_path(video)
    if CAMERAS.get(cam, {}).get("sidecar_strategy") != "sony":
        return (False, False)
    vstem = video.stem.upper()
    try:
        sibs = [s for s in video.parent.iterdir()
                if s.is_file() and s.name != video.name]
    except OSError:
        sibs = []
    has_xml = any(s.suffix.upper() == ".XML"
                  and _strip_sony_suffix(s.stem).upper() == vstem for s in sibs)
    has_thumb = any(s.suffix.upper() in (".JPG", ".JPEG")
                    and _strip_sony_suffix(s.stem).upper() == vstem for s in sibs)
    return (not has_xml, not has_thumb)


def audit_footage_sidecars():
    """Walk The Footage and return records for videos missing a sidecar:
    (video_path, name, size, camera, miss_xml, miss_thumb)."""
    records = []
    if not THE_FOOTAGE.exists():
        return records
    for f in sorted(THE_FOOTAGE.rglob("*")):
        if (not f.is_file() or f.name.startswith(".")
                or f.suffix.lower() not in VIDEO_EXTENSIONS):
            continue
        miss_xml, miss_thumb = _missing_sidecars_beside(f)
        if miss_xml or miss_thumb:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            records.append((f, f.name, size, _camera_from_footage_path(f),
                            miss_xml, miss_thumb))
    return records


def index_card_clips(cards=None):
    """{(name, size): (card_name, [sidecar_paths])} for every video on the
    mounted cards. Keyed by (name, size) because Sony reuses clip numbers after
    a card format, so name alone would match the wrong clip."""
    if cards is None:
        cards = find_mounted_cards()
    index = {}
    for card in cards:
        for video, _cam, sidecars in find_video_files(card):
            try:
                size = video.stat().st_size
            except OSError:
                continue
            index[(video.name, size)] = (card.name, sidecars)
    return index


def load_accepted_baseline():
    """Relative footage paths the user has accepted as permanently sidecar-less."""
    try:
        return set(json.loads(SIDECAR_BASELINE.read_text()).get("accepted", []))
    except (OSError, json.JSONDecodeError):
        return set()


def save_accepted_baseline(relpaths):
    SIDECAR_BASELINE.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR_BASELINE.write_text(json.dumps(
        {"accepted_at": datetime.now().isoformat(timespec="seconds"),
         "accepted": sorted(relpaths)}, indent=2))


def run_backfill(apply=False, accept=False):
    """Recover missing Sony sidecars from mounted cards, or accept the gaps no
    card can fill as final.

    Footage videos are matched to card clips by (name, size); for each match we
    copy any sidecar the card has but the footage lacks. Sidecars no connected
    card can provide and that the user has accepted (--accept) are reported as
    final, not as problems, so the archive reads complete. Read-only unless
    apply or accept. Video files are never touched.
    """
    records = audit_footage_sidecars()

    def relpath(v):
        try:
            return v.relative_to(THE_FOOTAGE).as_posix()
        except ValueError:
            return str(v)

    incomplete = {relpath(v): v for v, *_ in records}

    # what a currently-mounted card can complete right now
    cards = find_mounted_cards()
    index = index_card_clips(cards) if cards else {}
    plan = []                       # (sidecar_src, dest_file)
    recoverable = set()             # relpaths a connected card can complete now
    for video, name, size, _cam, _mx, _mt in records:
        hit = index.get((name, size))
        if not hit:
            continue
        _card, sidecars = hit
        for sc in sidecars:
            dest_file = video.parent / sc.name
            need = not dest_file.exists()
            if not need:
                try:
                    need = dest_file.stat().st_size == 0
                except OSError:
                    need = True
            if need:
                plan.append((sc, dest_file))
                recoverable.add(relpath(video))

    accepted = load_accepted_baseline()
    n_accepted = sum(1 for r in incomplete if r in accepted and r not in recoverable)
    unaccounted = sorted(r for r in incomplete
                         if r not in accepted and r not in recoverable)

    print(f"\n{'=' * 60}")
    print("BACKFILL  -  sidecar status")
    print(f"{'=' * 60}")
    print(f"  videos missing a sidecar : {len(incomplete)}")
    print(f"    recoverable now (cards): {len(recoverable)}")
    print(f"    accepted as final      : {n_accepted}")
    print(f"    unaccounted            : {len(unaccounted)}")
    if cards:
        print(f"  cards mounted            : {', '.join(c.name for c in cards)}")

    # accept: snapshot every currently-incomplete video as final
    if accept:
        save_accepted_baseline(set(accepted) | set(incomplete))
        print(f"\nAccepted {len(incomplete)} videos as final; sidecars no card can provide "
              f"are now treated as complete.")
        if recoverable:
            print(f"  Note: {len(recoverable)} are still recoverable from a mounted card if "
                  f"you'd rather run `backfill --apply` first.")
        print(f"  Baseline: {SIDECAR_BASELINE}")
        log(f"Backfill: accepted {len(incomplete)} videos as final")
        return

    # all-clear: nothing recoverable and nothing unaccounted
    if not recoverable and not unaccounted:
        print("\nArchive complete: every video has its sidecars or is accepted as final.")
        return

    if not recoverable:   # only unaccounted remain, no card to help
        print(f"\n{len(unaccounted)} videos are missing sidecars that no mounted card can "
              f"provide.\nConnect the original card(s), or run `backfill --accept` to mark "
              f"them final.")
        return

    if not apply:
        for sc, dest_file in plan[:8]:
            print(f"    + {dest_file.parent.name}/{sc.name}")
        if len(plan) > 8:
            print(f"    ... and {len(plan) - 8} more")
        print("\nRead-only. Re-run with --apply to copy these into The Footage.")
        return

    print(f"\nCopying {len(plan)} sidecars (rsync + chmod 644 + verify)...")
    copied = failed = verified = 0
    for sc, dest_file in plan:
        try:
            src_hash = hash_file(sc)
        except OSError:
            src_hash = None
        if copy_one_file(sc, dest_file):
            copied += 1
            if src_hash is not None:
                try:
                    if hash_file(dest_file) == src_hash:
                        verified += 1
                    else:
                        print(f"    !! verify mismatch: {dest_file.name}")
                except OSError:
                    print(f"    !! verify read error: {dest_file.name}")
        else:
            failed += 1
            print(f"    !! copy failed: {dest_file.name}")

    print(f"\n  copied: {copied}, verified: {verified}, failed: {failed}")
    log(f"Backfill: copied {copied} sidecars ({verified} verified, {failed} failed)")


# ============================================================
# Logging
# ============================================================

def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.now()}: {msg}\n")


# ============================================================
# CLI
# ============================================================

def _list_dumps():
    if not THE_DUMP.exists():
        return []
    return sorted([d for d in THE_DUMP.iterdir()
                   if d.is_dir() and not d.name.startswith(".")])


def menu():
    for d in [THE_DUMP, THE_FOOTAGE, THE_IMAGES]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print("CAMERA IMPORT SYSTEM")
    print(f"{'=' * 60}")
    print(f"Media base: {MEDIA_BASE}")
    print()
    print("1. Pull new card (video)")
    print("2. Sort existing dump (verify, cleanup)")
    print("3. Pull DCIM images")
    print("4. Verify dump integrity")
    print("5. Reconcile dumps (status)")
    print("6. Backfill missing sidecars from cards")
    print("7. View import log")
    print("8. Exit")

    choice = input("\nChoice: ").strip()

    if choice == "1":
        cards = find_mounted_cards()
        if not cards:
            print("No camera cards detected.")
            return
        print(f"\nDetected {len(cards)} card(s):")
        for c in cards:
            print(f"  {c.name}")
        if input("\nPull all? (y/n): ").strip().lower() != "y":
            return
        for card in cards:
            dump_path = pull_card(card)
            if input("\nSort this dump now? (y/n): ").strip().lower() == "y":
                sort_dump(dump_path)

    elif choice == "2":
        dumps = _list_dumps()
        if not dumps:
            print("No dumps found.")
            return
        for i, d in enumerate(dumps, 1):
            info = ""
            manifest = d / "manifest.json"
            if manifest.exists():
                try:
                    m = json.loads(manifest.read_text())
                    info = f" ({m.get('video_count', '?')} videos)"
                except (OSError, json.JSONDecodeError):
                    pass
            print(f"  {i}. {d.name}{info}")
        sel = input("\nDump to sort (number): ").strip()
        try:
            target = dumps[int(sel) - 1]
        except (ValueError, IndexError):
            print("Invalid.")
            return
        sort_dump(target)

    elif choice == "3":
        run_images_command()

    elif choice == "4":
        dumps = _list_dumps()
        if not dumps:
            print("No dumps found.")
            return
        for i, d in enumerate(dumps, 1):
            print(f"  {i}. {d.name}")
        sel = input("\nDump to verify (number): ").strip()
        try:
            target = dumps[int(sel) - 1]
        except (ValueError, IndexError):
            print("Invalid.")
            return
        verify_dump(target)

    elif choice == "5":
        print_status()

    elif choice == "6":
        run_backfill(apply=False)
        if input("\nApply (copy recoverable sidecars from cards)? (y/N): ").strip().lower() == "y":
            run_backfill(apply=True)

    elif choice == "7":
        if LOG_FILE.exists():
            print(LOG_FILE.read_text())
        else:
            print("No import log yet.")

    elif choice == "8":
        print("Done.")
        sys.exit(0)


def main():
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if not args:
        while True:
            try:
                menu()
            except KeyboardInterrupt:
                print("\nExiting.")
                break
        return

    cmd = args[0].lower()
    rest = args[1:]

    if cmd in ("pull", "dump"):
        cards = find_mounted_cards()
        if not cards:
            print("No cards detected.")
            sys.exit(1)
        for card in cards:
            pull_card(card, force=force)

    elif cmd == "sort":
        dumps = _list_dumps()
        if not dumps:
            print("No dumps found.")
            sys.exit(1)
        target = (THE_DUMP / rest[0]) if rest else dumps[-1]
        sort_dump(target)

    elif cmd == "images":
        run_images_command()

    elif cmd == "verify":
        if not rest:
            print("Usage: camera_import.py verify <dump_folder_name>")
            sys.exit(1)
        verify_dump(THE_DUMP / rest[0])

    elif cmd == "status":
        if rest:
            print_status(THE_DUMP / rest[0])
        else:
            print_status()

    elif cmd == "backfill":
        run_backfill(apply="--apply" in rest, accept="--accept" in rest)

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: pull [--force], sort [dump], images, verify <dump>, "
              "status [dump], backfill [--apply|--accept]")
        sys.exit(1)


if __name__ == "__main__":
    main()
