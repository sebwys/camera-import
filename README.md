# camera-import

Copies camera SD cards into a sorted, checksum verified footage archive on macOS.

I built it so footage moves from card to archive without silent losses. Every file is hashed on pull and checked again at its destination.

## How it works

| command | what it does |
|---|---|
| `pull` | card -> `The Dump/<timestamp>_<card>/`, with xxHash checksums |
| `sort [dump]` | copy chosen clips to `The Footage/<Camera>/<Location>/`, verify, delete the dump |
| `images` | DCIM photos -> `The Images/<Event>/`, verified, card untouched |
| `verify <dump>` | recheck a dump's hashes |
| `status` | sorted and unsorted clips per dump |
| `backfill` | report missing Sony sidecars, copy them from the card with `--apply` |

## Decisions

- I delete a dump only when every clip in it is sorted and every copy matches its pull checksum.
- I find Sony sidecars by suffix, `M01.XML` beside the video and `T01.JPG` in `PRIVATE/M4ROOT/THMBNL/`, because their stems differ from the video stem.
- Pull and backfill match clips on name and exact size, because Sony reuses clip numbers after a card format.
- I copy with `rsync -a` and `chmod 644`, because `cp -p` can leave zero byte ghosts in synced folders and exFAT cards mount as 700, which the sync client skips.
- `backfill --accept` records gaps no card can fill, so a new gap stands out as unaccounted.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python camera_import.py
.venv/bin/python test_camera_import.py
```

With no arguments it opens a menu. The archive root is `CAMERA_MEDIA_BASE`, default `~/Documents/Media`. The 75 tests run on fake cards in temp folders.

## Layout

| path | role |
|---|---|
| `camera_import.py` | the tool, camera rules in `CAMERAS` |
| `test_camera_import.py` | tests, standalone runner |
| `requirements.txt` | `xxhash`, the only dependency |

## Limits

- macOS only: cards are found under `/Volumes`.
- Sony FX30, DJI and iPhone only. Other video is skipped at sort.
- Photos left in a dump are deleted with it, so run `images` first.

## License

MIT.
