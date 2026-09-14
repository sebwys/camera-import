# camera-import

Pull SD cards, verify integrity, and sort footage into a camera/location archive, then delete the local staging dump. Also pulls DCIM photos into event folders and repairs missing metadata sidecars from the original cards. Built for a cloud-synced archive on macOS.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
export CAMERA_MEDIA_BASE="$HOME/Documents/Media"    # default if unset
```

Requires macOS `rsync`. The only dependency is `xxhash` for fast checksums.

## Data flow, video

```
SD card (/Volumes/<card>)
    |  camera_import.py pull
    |  rsync + xxHash checksums + manifest
    v
<media>/The Dump/<timestamp>_<card>/
    |  camera_import.py sort
    |  interactive: select clips, type a location, copy video + sidecars
    |  re-verify destination hashes against the dump's checksums.json
    |  rm -rf the dump folder
    v
<media>/The Footage/Sony SLOG-3/Iceland/
    Sony FX300814.MP4            video
    Sony FX300814M01.XML         camera metadata
    Sony FX300814T01.JPG         thumbnail
```

The dump is deleted only after every destination file verifies. If anything is unsorted or any hash mismatches, the dump is kept.

## Data flow, images

```
SD card or iPhone (/Volumes/<card> with DCIM/)
    |  camera_import.py images
    |  interactive: pick card, type an event name
    |  hash sources, rsync, verify destination
    v
<media>/The Images/<Event>/
```

Photos go straight to their destination, no staging. Source images are never deleted from the card.

## Commands

```bash
.venv/bin/python camera_import.py                   # interactive menu
.venv/bin/python camera_import.py pull              # card -> The Dump
.venv/bin/python camera_import.py sort [dump]       # The Dump -> The Footage, verify, cleanup
.venv/bin/python camera_import.py images            # DCIM -> The Images/<Event>/
.venv/bin/python camera_import.py verify <dump>     # recheck a dump's stored hashes
.venv/bin/python camera_import.py status            # reconcile every dump vs The Footage
.venv/bin/python camera_import.py backfill [--apply|--accept]
.venv/bin/python test_camera_import.py              # 75 tests, no pytest needed
```

## Backfill

Footage sorted by an older version of this tool has the `.MP4` but is missing its `M01.XML` metadata and `T01.JPG` thumbnail. Those sidecars were never copied, so the only source is the original card.

`backfill` matches each incomplete video to a clip on a mounted card and copies the sidecars the card has but the archive lacks:

- Matching is on filename and exact byte size, never name alone, because Sony reuses clip numbers after a card format.
- Sony only. DJI and iPhone keep same-stem sidecars beside the video and were never affected.
- Read-only by default. `--apply` copies and hash-verifies each sidecar. Video files are never modified.
- Idempotent. Only missing or 0-byte sidecars are copied, so re-running with another card picks up where you left off.

When the remaining gaps have no card left to fill them, `backfill --accept` records them in `<media>/.sidecar_baseline.json` and the tool reports the archive complete. Anything later found missing a sidecar that is not in that baseline is flagged as unaccounted.

## Sony FX30 filename pattern

| file | example |
|------|---------|
| video | `Sony FX300814.MP4` |
| camera XML | `Sony FX300814M01.XML` |
| thumbnail | `Sony FX300814T01.JPG` in `PRIVATE/M4ROOT/THMBNL/` |

The video stem does not match the XML stem. Naive stem matching silently drops every Sony sidecar. The sidecar logic strips the `M01`/`T01` suffix, matches case-insensitively, and looks in the cross-folder THMBNL directory. See `find_sony_sidecars()` and its tests.

## Cameras

| camera | detection | folder | sidecars |
|--------|-----------|--------|----------|
| Sony FX30 | `PRIVATE/M4ROOT` path | Sony SLOG-3/ | XML, JPG (cross-folder) |
| DJI | `DJI_` filename prefix | DJI DLOG-M/ | SRT, LRF, XMP, DNG |
| iPhone | MOV in DCIM | Apple ProRes-Log/ | AAE, XMP, HEIC, JPG |

Folder names for a camera are in the `CAMERAS` dict in `camera_import.py`.

## Archive layout

```
<media>/
|-- The Dump/                          staging, deleted after a verified sort
|   `-- <timestamp>_<card>/
|       |-- PRIVATE/M4ROOT/CLIP/       exact card structure preserved
|       |-- PRIVATE/M4ROOT/THMBNL/
|       |-- checksums.json             xxHash per file
|       `-- manifest.json              pull metadata
|-- The Footage/<Camera>/<Location>/
`-- The Images/<Event>/
```

Folders are created on demand.

## Cloud-synced archives

The archive folder is expected to live under a cloud-synced directory. Three things follow from that.

Ghost files. macOS `cp -p` uses `fcopyfile()`, which can time out on synced directories and leave 0-byte ghosts. The script treats zero-byte destinations as "needs copy" and uses `rsync -a` everywhere.

Eviction. The sync client may offload files from local disk. Evicted files still report full size via `stat().st_size` but `du` shows 0. The script reads `stat`, not `du`.

| state | `stat().st_size` | `du` | treated as |
|-------|------------------|------|------------|
| ghost | 0 | 0 | unsorted, re-copy |
| evicted | full | 0 | sorted, valid |

Permissions. SD cards mount as exFAT, which macOS maps to `700`. The sync indexer needs `644`. After every `rsync -a` the script runs `chmod 644` so the file is visible and syncable.

## Tests

```bash
.venv/bin/python test_camera_import.py
```

75 tests, standalone runner. They build fake cards and archives in temp directories and exercise pull, sort, verify, images and backfill end to end, including the Sony cross-folder sidecar case.

## License

MIT.
