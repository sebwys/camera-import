# camera-import

Copies camera SD cards into a sorted footage archive on macOS, checking each clip against its pull checksum.

I built it so footage moves from card to a cloud synced archive without silent losses. Pull hashes the copy in the dump, and sort rehashes each clip and its sidecars in the archive before it deletes the dump.

## How it works

| command | what it does |
|---|---|
| `pull` | card -> `The Dump/<timestamp>_<card>/`, with xxHash checksums |
| `sort [dump]` | copy chosen clips to `The Footage/<Camera>/<Location>/`, verify, delete the dump |
| `images` | DCIM photos -> `The Images/<Event>/`, verified, card untouched |
| `verify <dump>` | recheck a dump's hashes |
| `status` | sorted and unsorted clips per dump |
| `backfill` | report missing Sony sidecars, copy and verify them from the card with `--apply`. Videos are never touched |

## Decisions

- I delete a dump only when every clip in it is sorted and every copy matches its pull checksum.
- I find Sony sidecars by suffix, `M01.XML` beside the video and `T01.JPG` in `PRIVATE/M4ROOT/THMBNL/`, because their stems differ from the video stem.
- Pull and backfill match clips on name and exact size, because Sony reuses clip numbers after a card format.
- I copy with `rsync -a` and `chmod 644`, because `cp -p` can leave empty ghost files in synced folders and exFAT cards mount as 700, which the sync client skips.
- `backfill --accept` marks every current gap as final, so a new gap stands out as unaccounted. Run `--apply` first, while the cards are still around.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python test_camera_import.py
export CAMERA_MEDIA_BASE="$HOME/Documents/Media"
.venv/bin/python camera_import.py
```

The archive root is `CAMERA_MEDIA_BASE`, default `~/Documents/Media`, and the menu creates its folders on start, so set it first. With no arguments the tool opens a menu. The 75 tests run on fake cards in temp folders.

## Layout

| path | role |
|---|---|
| `camera_import.py` | the tool. Folder names and sidecar rules are in `CAMERAS`, detection in `detect_camera()` |
| `test_camera_import.py` | tests, standalone runner |
| `requirements.txt` | `xxhash`, the only dependency |

## Limits

- macOS only: cards are found under `/Volumes`.
- Cameras are told apart by path and name: `PRIVATE` or `M4ROOT` in the path is Sony, a `DJI_` prefix is DJI, and any other `.MOV` is filed as iPhone. Other video is skipped at sort and its dump is kept.
- A new clip that reuses an archived clip's name with a different size is treated as already sorted. Its dump is kept, so nothing is lost, but the clip has to be filed by hand.
- Photos left in a dump are deleted with it, so run `images` first.

## License

MIT.
