#!/usr/bin/env python3
# tests for camera_import.py
# runs entirely in temp directories - no real cards needed.

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import xxhash

import camera_import as ci


def make_fake_file(path, content=b"test video data"):
    """Create a file with known content for hash tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ============================================================
# hash + checksum tests
# ============================================================

def test_hash_file_xxhash():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "x.mp4"
        f.write_bytes(b"hello")
        assert ci.hash_file(f) == xxhash.xxh3_128(b"hello").hexdigest()


def test_hash_file_md5():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "x.mp4"
        f.write_bytes(b"hello")
        assert ci.hash_file(f, algorithm="md5") == hashlib.md5(b"hello").hexdigest()


def test_compute_checksums_uses_xxhash():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "clip.mp4", b"hello xxhash world")
        checksums = ci.compute_checksums(tmp)
        assert checksums["_algorithm"] == "xxh3_128"
        assert checksums["clip.mp4"] == xxhash.xxh3_128(b"hello xxhash world").hexdigest()


def test_compute_checksums_skips_hidden():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "clip.mp4")
        make_fake_file(tmp / ".DS_Store")
        make_fake_file(tmp / "._metadata")
        checksums = ci.compute_checksums(tmp)
        file_keys = [k for k in checksums if k != "_algorithm"]
        assert file_keys == ["clip.mp4"]


def test_compute_checksums_nested():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4")
        make_fake_file(tmp / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001M01.XML")
        checksums = ci.compute_checksums(tmp)
        assert "PRIVATE/M4ROOT/CLIP/C0001.MP4" in checksums
        assert "PRIVATE/M4ROOT/CLIP/C0001M01.XML" in checksums


# ============================================================
# verify_dump (recheck stored hashes on the dump itself)
# ============================================================

def test_verify_dump_xxhash():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = b"verify me with xxhash"
        make_fake_file(tmp / "clip.mp4", data)
        (tmp / "checksums.json").write_text(json.dumps({
            "_algorithm": "xxh3_128",
            "clip.mp4": xxhash.xxh3_128(data).hexdigest(),
        }))
        assert ci.verify_dump(tmp) is True


def test_verify_dump_legacy_md5():
    """Dumps written before the _algorithm marker existed used MD5."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = b"legacy md5 content"
        make_fake_file(tmp / "old_clip.mp4", data)
        (tmp / "checksums.json").write_text(json.dumps({
            "old_clip.mp4": hashlib.md5(data).hexdigest(),
        }))
        assert ci.verify_dump(tmp) is True


def test_verify_dump_detects_corruption():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "clip.mp4", b"original")
        (tmp / "checksums.json").write_text(json.dumps({
            "_algorithm": "xxh3_128",
            "clip.mp4": "deadbeef" * 4,
        }))
        assert ci.verify_dump(tmp) is False


def test_verify_dump_detects_missing():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "checksums.json").write_text(json.dumps({
            "_algorithm": "xxh3_128",
            "gone.mp4": "abcd" * 8,
        }))
        assert ci.verify_dump(tmp) is False


# ============================================================
# camera detection
# ============================================================

def test_detect_sony():
    assert ci.detect_camera(Path("/dump/PRIVATE/M4ROOT/CLIP/C0001.MP4")) == "SONY"


def test_detect_dji():
    assert ci.detect_camera(Path("/dump/DJI_0001.MP4")) == "DJI"


def test_detect_iphone_dcim():
    assert ci.detect_camera(Path("/dump/DCIM/100APPLE/IMG_0001.MOV")) == "IPHONE"


def test_detect_iphone_standalone_mov():
    assert ci.detect_camera(Path("/tmp/random.mov")) == "IPHONE"


def test_detect_unknown():
    assert ci.detect_camera(Path("/tmp/random.mp4")) == "UNKNOWN"


# ============================================================
# sidecar discovery - Sony FX30 pattern across folders
# ============================================================

def test_find_sony_sidecars_xml_and_thumbnail():
    """Sony video <stem>.MP4 has XML <stem>M01.XML next to it and
    thumbnail <stem>T01.JPG in PRIVATE/M4ROOT/THMBNL/."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = dump / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", b"video")
        make_fake_file(clip / "Sony FX300814M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG", b"jpg data")
        # a different clip's sidecars should not match
        make_fake_file(clip / "Sony FX300999M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300999T01.JPG", b"jpg")

        sidecars = ci.find_sony_sidecars(clip / "Sony FX300814.MP4", dump)
        names = {s.name for s in sidecars}
        assert names == {"Sony FX300814M01.XML", "Sony FX300814T01.JPG"}


def test_find_sony_sidecars_case_insensitive_match():
    """File extensions can be upper or lower case. Matching ignores case."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = dump / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", b"video")
        make_fake_file(clip / "Sony FX300814M01.xml", b"<xml/>")  # lowercase
        make_fake_file(thmbnl / "Sony FX300814T01.jpg", b"jpg")  # lowercase

        sidecars = ci.find_sony_sidecars(clip / "Sony FX300814.MP4", dump)
        # real names on disk returned (case preserved)
        names = {s.name for s in sidecars}
        assert "Sony FX300814M01.xml" in names
        assert "Sony FX300814T01.jpg" in names


def test_find_sony_sidecars_no_thmbnl_folder():
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        make_fake_file(clip / "Sony FX300814.MP4", b"video")
        make_fake_file(clip / "Sony FX300814M01.XML", b"<xml/>")

        sidecars = ci.find_sony_sidecars(clip / "Sony FX300814.MP4", dump)
        names = {s.name for s in sidecars}
        assert names == {"Sony FX300814M01.XML"}


def test_find_sony_sidecars_no_match_when_no_M01():
    """If XML doesn't have M01 suffix, it's not a sidecar."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        make_fake_file(clip / "Sony FX300814.MP4", b"video")
        make_fake_file(clip / "Sony FX300814.XML", b"<xml/>")  # no M01

        sidecars = ci.find_sony_sidecars(clip / "Sony FX300814.MP4", dump)
        assert sidecars == []


def test_find_sidecars_routes_sony_to_sony_logic():
    """find_sidecars(camera_type='SONY') should use the Sony logic that looks across folders."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = dump / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4")
        make_fake_file(clip / "Sony FX300814M01.XML")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG")

        sidecars = ci.find_sidecars(clip / "Sony FX300814.MP4", "SONY", dump_root=dump)
        names = {s.name for s in sidecars}
        assert names == {"Sony FX300814M01.XML", "Sony FX300814T01.JPG"}


# ============================================================
# sidecar discovery - DJI / iPhone extension match
# ============================================================

def test_find_extension_sidecars_dji():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "DJI_0001.MP4")
        make_fake_file(tmp / "DJI_0001.SRT")
        make_fake_file(tmp / "DJI_0001.LRF")
        make_fake_file(tmp / "DJI_0001.TXT")  # not a sidecar

        sidecars = ci.find_extension_sidecars(tmp / "DJI_0001.MP4", "DJI")
        names = {s.name for s in sidecars}
        assert "DJI_0001.SRT" in names
        assert "DJI_0001.LRF" in names
        assert "DJI_0001.TXT" not in names


def test_find_extension_sidecars_iphone():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "IMG_0001.MOV")
        make_fake_file(tmp / "IMG_0001.AAE")
        make_fake_file(tmp / "IMG_0001.HEIC")

        sidecars = ci.find_extension_sidecars(tmp / "IMG_0001.MOV", "IPHONE")
        names = {s.name for s in sidecars}
        assert "IMG_0001.AAE" in names
        assert "IMG_0001.HEIC" in names


def test_find_extension_sidecars_skips_self():
    """Don't return the video file itself as a sidecar."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "DJI_0001.MP4")
        make_fake_file(tmp / "DJI_0001.JPG")  # JPG isn't in DJI's sidecar list

        sidecars = ci.find_extension_sidecars(tmp / "DJI_0001.MP4", "DJI")
        names = {s.name for s in sidecars}
        assert "DJI_0001.MP4" not in names


# ============================================================
# selection parsing
# ============================================================

def test_parse_selection_single():
    assert ci.parse_selection("3", 10) == [2]


def test_parse_selection_range():
    assert ci.parse_selection("1-3", 10) == [0, 1, 2]


def test_parse_selection_mixed():
    assert ci.parse_selection("1-3,7,9", 10) == [0, 1, 2, 6, 8]


def test_parse_selection_out_of_range():
    assert ci.parse_selection("15", 10) == []


def test_parse_selection_invalid():
    assert ci.parse_selection("abc", 10) == []


# ============================================================
# path building
# ============================================================

def test_build_dest_path_sony():
    dest = ci.build_dest_path("SONY", "Argentina")
    assert str(dest).endswith("Sony SLOG-3/Argentina")


def test_build_dest_path_dji():
    dest = ci.build_dest_path("DJI", "Chile")
    assert str(dest).endswith("DJI DLOG-M/Chile")


def test_build_dest_path_iphone():
    dest = ci.build_dest_path("IPHONE", "Australia")
    assert str(dest).endswith("Apple ProRes-Log/Australia")


def test_camera_folder_names_match_disk():
    assert ci.CAMERAS["SONY"]["folder"] == "Sony SLOG-3"
    assert ci.CAMERAS["DJI"]["folder"] == "DJI DLOG-M"
    assert ci.CAMERAS["IPHONE"]["folder"] == "Apple ProRes-Log"


# ============================================================
# select_country / select_event
# ============================================================

def test_select_country_returns_typed_name():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        (footage / "Sony SLOG-3" / "Argentina").mkdir(parents=True)
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            with mock.patch("builtins.input", return_value="Argentina"):
                assert ci.select_country() == "Argentina"


def test_select_country_accepts_new():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        footage.mkdir()
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            with mock.patch("builtins.input", return_value="Bolivia"):
                assert ci.select_country() == "Bolivia"


def test_select_country_warns_on_camera_body_and_reprompts():
    """Typing a camera body name (e.g. 'Mavic Mini') triggers a warning.
    Answering 'n' loops back so the user can type a real location."""
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        footage.mkdir()
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            with mock.patch("builtins.input", side_effect=["Mavic Mini", "n", "Iceland"]):
                assert ci.select_country() == "Iceland"


def test_select_country_accepts_camera_body_with_override():
    """y at the override prompt forces the camera body name through."""
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        footage.mkdir()
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            with mock.patch("builtins.input", side_effect=["Mavic Mini", "y"]):
                assert ci.select_country() == "Mavic Mini"


def test_select_country_does_not_warn_on_event_name():
    """Event names like 'Graduation' contain no camera body keywords."""
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        footage.mkdir()
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            with mock.patch("builtins.input", return_value="Graduation"):
                assert ci.select_country() == "Graduation"


def test_select_event_returns_typed_name():
    with tempfile.TemporaryDirectory() as tmp:
        images = Path(tmp) / "The Images"
        (images / "Family - Christmas '26").mkdir(parents=True)
        with mock.patch.object(ci, "THE_IMAGES", images):
            with mock.patch("builtins.input", return_value="Family - Christmas '26"):
                assert ci.select_event() == "Family - Christmas '26"


# ============================================================
# file counting + footage indexing
# ============================================================

def test_count_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        make_fake_file(tmp / "a.mp4")
        make_fake_file(tmp / "sub" / "b.mov")
        make_fake_file(tmp / ".hidden")
        assert ci.count_files(tmp) == 2
        assert ci.count_files(tmp, exclude_hidden=False) == 3


def test_build_footage_size_index_empty():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(ci, "THE_FOOTAGE", Path(tmp) / "nonexistent"):
            assert ci.build_footage_size_index() == set()


def test_build_footage_size_index_keys_by_name_and_size():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        make_fake_file(footage / "Sony SLOG-3" / "Japan" / "C0001.MP4", b"abc")
        make_fake_file(footage / "DJI DLOG-M" / "Chile" / "DJI_0001.MP4", b"defgh")
        make_fake_file(footage / "Sony SLOG-3" / "Japan" / "C0002.MP4", b"")  # 0 bytes
        make_fake_file(footage / "Sony SLOG-3" / "Japan" / "C0001.XML", b"<xml/>")

        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            idx = ci.build_footage_size_index()

        assert ("C0001.MP4", 3) in idx
        assert ("DJI_0001.MP4", 5) in idx
        assert ("C0002.MP4", 0) not in idx
        assert not any(name == "C0001.XML" for name, _ in idx)
        assert len(idx) == 2


def test_build_footage_index_skips_zero_byte():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        make_fake_file(footage / "Sony SLOG-3" / "Japan" / "real.MP4", b"x")
        make_fake_file(footage / "Sony SLOG-3" / "Japan" / "ghost.MP4", b"")
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            idx = ci.build_footage_index()
        assert "real.MP4" in idx
        assert "ghost.MP4" not in idx


# ============================================================
# find_card_excludes - dedup during pull
# ============================================================

def test_find_card_excludes_no_dupes():
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4", b"new clip")
        patterns, skipped, bytes_ = ci.find_card_excludes(card, set())
        assert patterns == []
        assert skipped == 0
        assert bytes_ == 0


def test_find_card_excludes_skips_dupe_and_sony_sidecars():
    """Dupe video AND its M01.XML + T01.JPG thumbnail get excluded."""
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        clip = card / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300001.MP4", b"sorted already")
        make_fake_file(clip / "Sony FX300001M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300001T01.JPG", b"thumb")
        # a new clip - should not be excluded
        make_fake_file(clip / "Sony FX300099.MP4", b"new clip")
        make_fake_file(clip / "Sony FX300099M01.XML", b"<xml/>")

        size_index = {("Sony FX300001.MP4", len(b"sorted already"))}
        patterns, skipped, bytes_ = ci.find_card_excludes(card, size_index)

        assert "/PRIVATE/M4ROOT/CLIP/Sony FX300001.MP4" in patterns
        assert "/PRIVATE/M4ROOT/CLIP/Sony FX300001M01.XML" in patterns
        assert "/PRIVATE/M4ROOT/THMBNL/Sony FX300001T01.JPG" in patterns
        assert "/PRIVATE/M4ROOT/CLIP/Sony FX300099.MP4" not in patterns
        assert "/PRIVATE/M4ROOT/CLIP/Sony FX300099M01.XML" not in patterns
        assert skipped == 1
        assert bytes_ == len(b"sorted already")


def test_find_card_excludes_size_mismatch_kept():
    """Sony filename wrap: same name, different size = NOT a dupe."""
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4", b"new shoot, different size")
        size_index = {("C0001.MP4", 5)}
        patterns, skipped, _ = ci.find_card_excludes(card, size_index)
        assert patterns == []
        assert skipped == 0


# ============================================================
# find_dcim_cards + find_image_files
# ============================================================

def test_find_image_files_finds_common_formats():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "DCIM" / "100APPLE"
        make_fake_file(d / "IMG_0001.JPG", b"jpg")
        make_fake_file(d / "IMG_0002.HEIC", b"heic")
        make_fake_file(d / "IMG_0003.DNG", b"dng")
        make_fake_file(d / "IMG_0004.MOV", b"mov")  # video, not image
        make_fake_file(d / ".hidden.jpg", b"hidden")

        images = ci.find_image_files(d)
        names = {i.name for i in images}
        assert names == {"IMG_0001.JPG", "IMG_0002.HEIC", "IMG_0003.DNG"}


def test_image_extensions_include_raw_formats():
    """RAW formats from major camera brands should be recognized."""
    for ext in [".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng"]:
        assert ext in ci.IMAGE_EXTENSIONS


# ============================================================
# verify_sorted_against_dump - the check after sort
# ============================================================

def test_verify_sorted_against_dump_passes_when_hashes_match():
    """Sony video + sidecars copied to footage, all hashes match."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dump = tmp / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = dump / "PRIVATE" / "M4ROOT" / "THMBNL"
        footage = tmp / "The Footage" / "Sony SLOG-3" / "Iceland"

        video_data = b"sony video bytes"
        xml_data = b"<xml/>"
        thm_data = b"thumb"

        make_fake_file(clip / "Sony FX300001.MP4", video_data)
        make_fake_file(clip / "Sony FX300001M01.XML", xml_data)
        make_fake_file(thmbnl / "Sony FX300001T01.JPG", thm_data)

        # write dump's checksums.json
        checksums = ci.compute_checksums(dump)
        (dump / "checksums.json").write_text(json.dumps(checksums))

        # copy to footage (simulating sort)
        make_fake_file(footage / "Sony FX300001.MP4", video_data)
        make_fake_file(footage / "Sony FX300001M01.XML", xml_data)
        make_fake_file(footage / "Sony FX300001T01.JPG", thm_data)

        sorted_list = [
            (clip / "Sony FX300001.MP4", footage / "Sony FX300001.MP4", "SONY"),
        ]
        with mock.patch.object(ci, "THE_FOOTAGE", tmp / "The Footage"):
            assert ci.verify_sorted_against_dump(dump, sorted_list) is True


def test_verify_sorted_against_dump_fails_on_corrupt_destination():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dump = tmp / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        footage = tmp / "The Footage" / "Sony SLOG-3" / "Iceland"

        make_fake_file(clip / "Sony FX300001.MP4", b"good video")
        checksums = ci.compute_checksums(dump)
        (dump / "checksums.json").write_text(json.dumps(checksums))

        # sorted to footage but corrupted
        make_fake_file(footage / "Sony FX300001.MP4", b"corrupted")

        sorted_list = [
            (clip / "Sony FX300001.MP4", footage / "Sony FX300001.MP4", "SONY"),
        ]
        with mock.patch.object(ci, "THE_FOOTAGE", tmp / "The Footage"):
            assert ci.verify_sorted_against_dump(dump, sorted_list) is False


def test_verify_sorted_against_dump_fails_on_missing_sidecar():
    """If a sidecar wasn't copied to footage, verification fails."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dump = tmp / "dump"
        clip = dump / "PRIVATE" / "M4ROOT" / "CLIP"
        footage = tmp / "The Footage" / "Sony SLOG-3" / "Iceland"

        make_fake_file(clip / "Sony FX300001.MP4", b"video")
        make_fake_file(clip / "Sony FX300001M01.XML", b"<xml/>")
        checksums = ci.compute_checksums(dump)
        (dump / "checksums.json").write_text(json.dumps(checksums))

        # video sorted, XML missing from footage
        make_fake_file(footage / "Sony FX300001.MP4", b"video")

        sorted_list = [
            (clip / "Sony FX300001.MP4", footage / "Sony FX300001.MP4", "SONY"),
        ]
        with mock.patch.object(ci, "THE_FOOTAGE", tmp / "The Footage"):
            assert ci.verify_sorted_against_dump(dump, sorted_list) is False


# ============================================================
# copy_one_file - iCloud ghost handling
# ============================================================

def test_copy_one_file_creates_destination():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src.mp4"
        src.write_bytes(b"hello")
        dest = tmp / "dest" / "src.mp4"
        dest.parent.mkdir()
        assert ci.copy_one_file(src, dest) is True
        assert dest.exists()
        assert dest.read_bytes() == b"hello"


def test_copy_one_file_overwrites_ghost():
    """An empty destination (iCloud ghost) gets overwritten."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src.mp4"
        src.write_bytes(b"real data")
        dest = tmp / "dest" / "src.mp4"
        dest.parent.mkdir()
        dest.write_bytes(b"")  # ghost
        assert ci.copy_one_file(src, dest) is True
        assert dest.read_bytes() == b"real data"


def test_copy_one_file_skips_existing_real_file():
    """If destination already exists with content, don't copy it again."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src.mp4"
        src.write_bytes(b"new data")
        dest = tmp / "dest" / "src.mp4"
        dest.parent.mkdir()
        dest.write_bytes(b"existing data")
        assert ci.copy_one_file(src, dest) is True
        assert dest.read_bytes() == b"existing data"  # unchanged


# ============================================================
# duplicate pull guard
# ============================================================

def test_find_existing_dump_returns_none_when_no_dumps():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(ci, "THE_DUMP", Path(tmp) / "nope"):
            assert ci.find_existing_dump_for_card("SD256", 1311) is None


def test_find_existing_dump_returns_none_for_different_card():
    """Card name doesn't match any existing dump -> no duplicate."""
    with tempfile.TemporaryDirectory() as tmp:
        the_dump = Path(tmp) / "The Dump"
        d = the_dump / "2026-05-18_120000_SD512"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "card_name": "SD512",
            "source_file_count": 100,
        }))
        with mock.patch.object(ci, "THE_DUMP", the_dump):
            assert ci.find_existing_dump_for_card("SD256", 100) is None


def test_find_existing_dump_returns_none_for_different_count():
    """Same card name but different source count -> card has new content, not dupe."""
    with tempfile.TemporaryDirectory() as tmp:
        the_dump = Path(tmp) / "The Dump"
        d = the_dump / "2026-05-18_120000_SD256"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "card_name": "SD256",
            "source_file_count": 100,
        }))
        with mock.patch.object(ci, "THE_DUMP", the_dump):
            assert ci.find_existing_dump_for_card("SD256", 999) is None


def test_find_existing_dump_finds_exact_match():
    """Same card name + same source count = duplicate."""
    with tempfile.TemporaryDirectory() as tmp:
        the_dump = Path(tmp) / "The Dump"
        d = the_dump / "2026-05-18_120000_SD256"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "card_name": "SD256",
            "source_file_count": 1311,
        }))
        with mock.patch.object(ci, "THE_DUMP", the_dump):
            result = ci.find_existing_dump_for_card("SD256", 1311)
        assert result == d


def test_find_existing_dump_ignores_incomplete():
    """A dump without manifest.json (interrupted pull) doesn't count as duplicate."""
    with tempfile.TemporaryDirectory() as tmp:
        the_dump = Path(tmp) / "The Dump"
        d = the_dump / "2026-05-18_120000_SD256"
        d.mkdir(parents=True)
        # no manifest.json - interrupted pull
        with mock.patch.object(ci, "THE_DUMP", the_dump):
            assert ci.find_existing_dump_for_card("SD256", 1311) is None


def test_pull_card_skips_when_duplicate_detected_and_user_declines():
    """Duplicate pull detected, user answers 'n' -> returns existing path, no new dump."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_dump = media / "The Dump"
        the_dump.mkdir(parents=True)

        # fake card with 1 file
        card = tmp / "SD256"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4", b"v1")
        card_count = ci.count_files(card)

        # existing complete dump for this card with matching count
        existing_dump = the_dump / "2026-05-18_120000_SD256"
        existing_dump.mkdir(parents=True)
        (existing_dump / "manifest.json").write_text(json.dumps({
            "card_name": "SD256",
            "source_file_count": card_count,
        }))

        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_DUMP", the_dump), \
             mock.patch.object(ci, "THE_FOOTAGE", media / "The Footage"), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"), \
             mock.patch("builtins.input", return_value="n"):
            result = ci.pull_card(card)

        assert result == existing_dump
        # no new dump folder should have been created (only the existing one)
        dumps = [d for d in the_dump.iterdir() if d.is_dir()]
        assert len(dumps) == 1
        assert dumps[0] == existing_dump


def test_pull_card_force_overrides_duplicate_guard():
    """force=True bypasses the duplicate check entirely (no prompt)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_dump = media / "The Dump"
        the_dump.mkdir(parents=True)

        card = tmp / "SD256"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4", b"v1")
        card_count = ci.count_files(card)

        existing_dump = the_dump / "2026-05-18_120000_SD256"
        existing_dump.mkdir(parents=True)
        (existing_dump / "manifest.json").write_text(json.dumps({
            "card_name": "SD256",
            "source_file_count": card_count,
        }))

        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_DUMP", the_dump), \
             mock.patch.object(ci, "THE_FOOTAGE", media / "The Footage"), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"):
            # no input mock - if prompt fires the test will hang/error
            result = ci.pull_card(card, force=True)

        # should create a new dump in addition to existing one
        dumps = sorted([d for d in the_dump.iterdir() if d.is_dir()])
        assert len(dumps) == 2
        assert result != existing_dump


# ============================================================
# backfill - recover missing sidecars from the original cards
# ============================================================

def test_strip_sony_suffix():
    assert ci._strip_sony_suffix("Sony FX300814M01") == "Sony FX300814"
    assert ci._strip_sony_suffix("Sony FX300814T01") == "Sony FX300814"
    assert ci._strip_sony_suffix("Sony FX300814") == "Sony FX300814"


def test_camera_from_footage_path():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            assert ci._camera_from_footage_path(
                footage / "Sony SLOG-3" / "Iceland" / "x.MP4") == "SONY"
            assert ci._camera_from_footage_path(
                footage / "DJI DLOG-M" / "Chile" / "y.MP4") == "DJI"


def test_missing_sidecars_beside_complete_sony():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        make_fake_file(iceland / "Sony FX300814.MP4", b"v")
        make_fake_file(iceland / "Sony FX300814M01.XML", b"<xml/>")
        make_fake_file(iceland / "Sony FX300814T01.JPG", b"jpg")
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            assert ci._missing_sidecars_beside(iceland / "Sony FX300814.MP4") == (False, False)


def test_missing_sidecars_beside_missing_both():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        make_fake_file(iceland / "Sony FX300814.MP4", b"v")
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            assert ci._missing_sidecars_beside(iceland / "Sony FX300814.MP4") == (True, True)


def test_missing_sidecars_beside_non_sony_not_flagged():
    """DJI / iPhone are never flagged. Their sidecars share the video stem and
    are optional."""
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        chile = footage / "DJI DLOG-M" / "Chile"
        make_fake_file(chile / "DJI_0001.MP4", b"v")  # no .srt beside it
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            assert ci._missing_sidecars_beside(chile / "DJI_0001.MP4") == (False, False)


def test_audit_footage_sidecars_flags_only_incomplete_sony():
    with tempfile.TemporaryDirectory() as tmp:
        footage = Path(tmp) / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        make_fake_file(iceland / "Sony FX300001.MP4", b"incomplete")  # no sidecars
        make_fake_file(iceland / "Sony FX300002.MP4", b"complete")
        make_fake_file(iceland / "Sony FX300002M01.XML", b"<xml/>")
        make_fake_file(iceland / "Sony FX300002T01.JPG", b"jpg")
        make_fake_file(footage / "DJI DLOG-M" / "Chile" / "DJI_0001.MP4", b"dji")  # not flagged
        with mock.patch.object(ci, "THE_FOOTAGE", footage):
            records = ci.audit_footage_sidecars()
        names = {r[1] for r in records}
        assert names == {"Sony FX300001.MP4"}


def test_index_card_clips_keys_by_name_and_size():
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "SD256"
        clip = card / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", b"sony video bytes")
        make_fake_file(clip / "Sony FX300814M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG", b"jpg")
        idx = ci.index_card_clips([card])
        key = ("Sony FX300814.MP4", len(b"sony video bytes"))
        assert key in idx
        _card_name, sidecars = idx[key]
        names = {s.name for s in sidecars}
        assert names == {"Sony FX300814M01.XML", "Sony FX300814T01.JPG"}


def test_run_backfill_audit_only_copies_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        footage = tmp / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        data = b"sony video bytes"
        make_fake_file(iceland / "Sony FX300814.MP4", data)
        card = tmp / "SD256"
        clip = card / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", data)
        make_fake_file(clip / "Sony FX300814M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG", b"jpg")
        with mock.patch.object(ci, "THE_FOOTAGE", footage), \
             mock.patch.object(ci, "LOG_FILE", tmp / "log.txt"), \
             mock.patch.object(ci, "SIDECAR_BASELINE", tmp / ".sidecar_baseline.json"), \
             mock.patch.object(ci, "find_mounted_cards", return_value=[card]):
            ci.run_backfill(apply=False)
        assert not (iceland / "Sony FX300814M01.XML").exists()
        assert not (iceland / "Sony FX300814T01.JPG").exists()


def test_run_backfill_apply_copies_and_verifies():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        footage = tmp / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        data = b"sony video bytes"
        make_fake_file(iceland / "Sony FX300814.MP4", data)
        card = tmp / "SD256"
        clip = card / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", data)            # same name + size
        make_fake_file(clip / "Sony FX300814M01.XML", b"<xml/>")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG", b"jpgbytes")
        with mock.patch.object(ci, "THE_FOOTAGE", footage), \
             mock.patch.object(ci, "LOG_FILE", tmp / "log.txt"), \
             mock.patch.object(ci, "SIDECAR_BASELINE", tmp / ".sidecar_baseline.json"), \
             mock.patch.object(ci, "find_mounted_cards", return_value=[card]):
            ci.run_backfill(apply=True)
        assert (iceland / "Sony FX300814M01.XML").read_bytes() == b"<xml/>"
        assert (iceland / "Sony FX300814T01.JPG").read_bytes() == b"jpgbytes"
        assert (iceland / "Sony FX300814.MP4").read_bytes() == data  # video untouched


def test_run_backfill_size_mismatch_not_recovered():
    """Name matches but size differs (Sony reuses clip numbers) -> do not recover."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        footage = tmp / "The Footage"
        iceland = footage / "Sony SLOG-3" / "Iceland"
        make_fake_file(iceland / "Sony FX300814.MP4", b"AAAA")       # 4 bytes
        card = tmp / "SD256"
        clip = card / "PRIVATE" / "M4ROOT" / "CLIP"
        thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
        make_fake_file(clip / "Sony FX300814.MP4", b"BBBBBBBB")      # 8 bytes: different clip
        make_fake_file(clip / "Sony FX300814M01.XML", b"<wrong/>")
        make_fake_file(thmbnl / "Sony FX300814T01.JPG", b"wrong")
        with mock.patch.object(ci, "THE_FOOTAGE", footage), \
             mock.patch.object(ci, "LOG_FILE", tmp / "log.txt"), \
             mock.patch.object(ci, "SIDECAR_BASELINE", tmp / ".sidecar_baseline.json"), \
             mock.patch.object(ci, "find_mounted_cards", return_value=[card]):
            ci.run_backfill(apply=True)
        assert not (iceland / "Sony FX300814M01.XML").exists()
        assert not (iceland / "Sony FX300814T01.JPG").exists()


def test_load_accepted_baseline_empty_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(ci, "SIDECAR_BASELINE", Path(tmp) / "nope.json"):
            assert ci.load_accepted_baseline() == set()


def test_run_backfill_accept_writes_baseline():
    """--accept records videos that are incomplete now so they read as final."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        footage = tmp / "The Footage"
        make_fake_file(footage / "Sony SLOG-3" / "Iceland" / "Sony FX300459.MP4", b"v")  # no sidecars
        baseline = tmp / ".sidecar_baseline.json"
        with mock.patch.object(ci, "THE_FOOTAGE", footage), \
             mock.patch.object(ci, "LOG_FILE", tmp / "log.txt"), \
             mock.patch.object(ci, "SIDECAR_BASELINE", baseline), \
             mock.patch.object(ci, "find_mounted_cards", return_value=[]):
            ci.run_backfill(accept=True)
            assert baseline.exists()
            assert "Sony SLOG-3/Iceland/Sony FX300459.MP4" in ci.load_accepted_baseline()


# ============================================================
# end to end: full Sony pull + sort + verify + cleanup
# ============================================================

def test_full_sony_flow_with_sidecars():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_dump = media / "The Dump"
        the_footage = media / "The Footage"

        # fake Sony card with one video + XML + thumbnail
        card = tmp / "card"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4",
                       b"sony video bytes goes here")
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001M01.XML",
                       b"<NonRealTimeMeta>...</NonRealTimeMeta>")
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "THMBNL" / "Sony FX300001T01.JPG",
                       b"\xff\xd8\xff\xe0jpg-bytes")
        make_fake_file(card / "PRIVATE" / "DATABASE" / "DATABASE.BIN",
                       b"sony bookkeeping")

        # patch paths
        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_DUMP", the_dump), \
             mock.patch.object(ci, "THE_FOOTAGE", the_footage), \
             mock.patch.object(ci, "THE_IMAGES", media / "The Images"), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"):

            the_dump.mkdir(parents=True)
            the_footage.mkdir(parents=True)

            # 1. pull
            dump_path = ci.pull_card(card)
            assert dump_path.exists()
            assert (dump_path / "checksums.json").exists()
            assert (dump_path / "manifest.json").exists()
            # video, XML, thumbnail all came through
            assert (dump_path / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4").exists()
            assert (dump_path / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001M01.XML").exists()
            assert (dump_path / "PRIVATE" / "M4ROOT" / "THMBNL" / "Sony FX300001T01.JPG").exists()

            # 2. sort - select all (A), country "Iceland"
            with mock.patch("builtins.input", side_effect=["A", "Iceland"]):
                ci.sort_dump(dump_path)

            # 3. sorted files exist in The Footage with proper structure
            iceland = the_footage / "Sony SLOG-3" / "Iceland"
            assert (iceland / "Sony FX300001.MP4").exists()
            assert (iceland / "Sony FX300001M01.XML").exists()
            assert (iceland / "Sony FX300001T01.JPG").exists()

            # 4. dump folder deleted by the cleanup after sort
            assert not dump_path.exists()


def test_full_sony_flow_keeps_dump_if_unsorted_remain():
    """If user quits sort without finishing, dump must NOT be deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_dump = media / "The Dump"
        the_footage = media / "The Footage"

        card = tmp / "card"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4", b"v1")
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001M01.XML", b"<xml/>")
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300002.MP4", b"v2")
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300002M01.XML", b"<xml/>")

        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_DUMP", the_dump), \
             mock.patch.object(ci, "THE_FOOTAGE", the_footage), \
             mock.patch.object(ci, "THE_IMAGES", media / "The Images"), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"):

            the_dump.mkdir(parents=True)
            the_footage.mkdir(parents=True)

            dump_path = ci.pull_card(card)

            # sort only video 1, then quit
            with mock.patch("builtins.input", side_effect=["1", "Iceland", "Q"]):
                ci.sort_dump(dump_path)

            # video 1 sorted, video 2 still in dump -> dump must remain
            assert (the_footage / "Sony SLOG-3" / "Iceland" / "Sony FX300001.MP4").exists()
            assert dump_path.exists()
            assert (dump_path / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300002.MP4").exists()


def test_full_sony_flow_keeps_dump_if_verification_fails():
    """If the sorted file is corrupt vs the dump checksum, dump must NOT be deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_dump = media / "The Dump"
        the_footage = media / "The Footage"

        card = tmp / "card"
        make_fake_file(card / "PRIVATE" / "M4ROOT" / "CLIP" / "Sony FX300001.MP4", b"v1")

        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_DUMP", the_dump), \
             mock.patch.object(ci, "THE_FOOTAGE", the_footage), \
             mock.patch.object(ci, "THE_IMAGES", media / "The Images"), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"):

            the_dump.mkdir(parents=True)
            the_footage.mkdir(parents=True)

            dump_path = ci.pull_card(card)

            iceland = the_footage / "Sony SLOG-3" / "Iceland"
            iceland.mkdir(parents=True, exist_ok=True)

            # create a corrupted destination first so sort's copy_one_file
            # sees the file already exists with content and skips it
            (iceland / "Sony FX300001.MP4").write_bytes(b"CORRUPTED")

            with mock.patch("builtins.input", side_effect=["A", "Iceland"]):
                ci.sort_dump(dump_path)

            # dump must remain because hash on destination != stored hash
            assert dump_path.exists()


# ============================================================
# end to end: DCIM image pull
# ============================================================

def test_dcim_image_pull_flow():
    """Fake DCIM card -> images command -> The Images/<Event>/ -> verified."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        media = tmp / "Documents" / "Media"
        the_images = media / "The Images"

        card = tmp / "iPhone"
        dcim = card / "DCIM" / "100APPLE"
        make_fake_file(dcim / "IMG_0001.HEIC", b"heic body 1")
        make_fake_file(dcim / "IMG_0002.JPG", b"jpg body 2")
        make_fake_file(dcim / "IMG_0003.MOV", b"video - should NOT come along")

        with mock.patch.object(ci, "MEDIA_BASE", media), \
             mock.patch.object(ci, "THE_IMAGES", the_images), \
             mock.patch.object(ci, "LOG_FILE", media / "import_log.txt"):

            the_images.mkdir(parents=True)
            ci.pull_images_from_card(card, "Family - Christmas '26")

            event_dir = the_images / "Family - Christmas '26"
            assert (event_dir / "IMG_0001.HEIC").exists()
            assert (event_dir / "IMG_0002.JPG").exists()
            # video should not be in The Images
            assert not (event_dir / "IMG_0003.MOV").exists()
            # content preserved
            assert (event_dir / "IMG_0001.HEIC").read_bytes() == b"heic body 1"


# ============================================================
# test runner
# ============================================================

if __name__ == "__main__":
    tests = [
        # hashing
        test_hash_file_xxhash,
        test_hash_file_md5,
        test_compute_checksums_uses_xxhash,
        test_compute_checksums_skips_hidden,
        test_compute_checksums_nested,
        # verify_dump
        test_verify_dump_xxhash,
        test_verify_dump_legacy_md5,
        test_verify_dump_detects_corruption,
        test_verify_dump_detects_missing,
        # camera detection
        test_detect_sony,
        test_detect_dji,
        test_detect_iphone_dcim,
        test_detect_iphone_standalone_mov,
        test_detect_unknown,
        # Sony sidecars
        test_find_sony_sidecars_xml_and_thumbnail,
        test_find_sony_sidecars_case_insensitive_match,
        test_find_sony_sidecars_no_thmbnl_folder,
        test_find_sony_sidecars_no_match_when_no_M01,
        test_find_sidecars_routes_sony_to_sony_logic,
        # DJI/iPhone extension sidecars
        test_find_extension_sidecars_dji,
        test_find_extension_sidecars_iphone,
        test_find_extension_sidecars_skips_self,
        # selection parsing
        test_parse_selection_single,
        test_parse_selection_range,
        test_parse_selection_mixed,
        test_parse_selection_out_of_range,
        test_parse_selection_invalid,
        # path building
        test_build_dest_path_sony,
        test_build_dest_path_dji,
        test_build_dest_path_iphone,
        test_camera_folder_names_match_disk,
        # selection prompts
        test_select_country_returns_typed_name,
        test_select_country_accepts_new,
        test_select_country_warns_on_camera_body_and_reprompts,
        test_select_country_accepts_camera_body_with_override,
        test_select_country_does_not_warn_on_event_name,
        test_select_event_returns_typed_name,
        # file counting + footage indexing
        test_count_files,
        test_build_footage_size_index_empty,
        test_build_footage_size_index_keys_by_name_and_size,
        test_build_footage_index_skips_zero_byte,
        # dedup at pull
        test_find_card_excludes_no_dupes,
        test_find_card_excludes_skips_dupe_and_sony_sidecars,
        test_find_card_excludes_size_mismatch_kept,
        # DCIM + images
        test_find_image_files_finds_common_formats,
        test_image_extensions_include_raw_formats,
        # verify_sorted_against_dump
        test_verify_sorted_against_dump_passes_when_hashes_match,
        test_verify_sorted_against_dump_fails_on_corrupt_destination,
        test_verify_sorted_against_dump_fails_on_missing_sidecar,
        # copy_one_file
        test_copy_one_file_creates_destination,
        test_copy_one_file_overwrites_ghost,
        test_copy_one_file_skips_existing_real_file,
        # duplicate pull guard
        test_find_existing_dump_returns_none_when_no_dumps,
        test_find_existing_dump_returns_none_for_different_card,
        test_find_existing_dump_returns_none_for_different_count,
        test_find_existing_dump_finds_exact_match,
        test_find_existing_dump_ignores_incomplete,
        test_pull_card_skips_when_duplicate_detected_and_user_declines,
        test_pull_card_force_overrides_duplicate_guard,
        # backfill
        test_strip_sony_suffix,
        test_camera_from_footage_path,
        test_missing_sidecars_beside_complete_sony,
        test_missing_sidecars_beside_missing_both,
        test_missing_sidecars_beside_non_sony_not_flagged,
        test_audit_footage_sidecars_flags_only_incomplete_sony,
        test_index_card_clips_keys_by_name_and_size,
        test_run_backfill_audit_only_copies_nothing,
        test_run_backfill_apply_copies_and_verifies,
        test_run_backfill_size_mismatch_not_recovered,
        test_load_accepted_baseline_empty_when_absent,
        test_run_backfill_accept_writes_baseline,
        # end to end
        test_full_sony_flow_with_sidecars,
        test_full_sony_flow_keeps_dump_if_unsorted_remain,
        test_full_sony_flow_keeps_dump_if_verification_fails,
        test_dcim_image_pull_flow,
    ]

    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
