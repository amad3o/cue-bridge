# CueBridge

CueBridge is a local-first tool for working with DJ cue points. It's an **MCP server**, so an AI assistant such as Claude can read your Rekordbox library, answer questions about your cues, and write edited copies of it. The goal is to move and compare cue points between **Rekordbox** and **Serato**.

Everything runs on your own machine, and your library files are never uploaded anywhere.

## Status

Early development. What works today:

- Reading a Rekordbox XML export: tracks, hot cues, memory cues and loops, with colors and file paths.
- Searching and paging through the library, and finding tracks with no cues.
- Writing an **edited copy** of the XML with a default hot cue added to tracks that have none.

- Copying Rekordbox cues into the Serato tags of your MP3 and WAV files, with Rekordbox winning, a journal and undo.
- Reading an Apple Music export (tracks and playlists), matching its tracks to Rekordbox's, finding files that moved, and comparing the two libraries (tracks and playlists).

Not built yet: metadata and playlist-hygiene reports, writing playlists back, and M4A/AIFF support. See [ROADMAP.md](ROADMAP.md) for completed work, known issues and what's next.

## Setup

Requires Python 3.10 or newer (the dependencies' minimum). Only tested on 3.13.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # optional: run the tests
```

### Export your Rekordbox library

In Rekordbox, choose **File → Export Collection in xml format** and save the file somewhere you can find it. CueBridge reads that XML file. Only that export contains cue points. An iTunes `Library.xml` won't work.

### Register the server with Claude Code

From the repository root:

```bash
claude mcp add --scope local cuebridge \
  -e "PYTHONPATH=$(pwd)/backend" \
  -- "$(pwd)/backend/.venv/bin/python" -m cuebridge.mcp_server
```

Start a new session (or run `/mcp`) and `cuebridge` should show as connected. The server uses stdio, so any MCP client can run the same command: `python -m cuebridge.mcp_server` with `backend/` on `PYTHONPATH`. For example, Claude Desktop reads the same command, arguments and environment from its `mcpServers` config.

## Tools

| Tool | What it does |
|---|---|
| `load_rekordbox_library(path)` | Loads the XML export and returns a summary: track count, cue counts by type, tracks with no cues. |
| `search_tracks(query, has_cues, limit, offset)` | Lists tracks a page at a time. `query` words must all appear in the title, artist or file name. |
| `get_track_cues(track_id)` | One track with all its cues: type, pad slot, name, time, loop end and color. |
| `find_tracks_without_cues(limit, offset)` | Tracks that have no cues. |
| `load_apple_music_library(path)` | Loads an Apple Music library exported as XML (**File → Library → Export Library**) and summarises it: tracks, formats, metadata gaps and every playlist with its size. |
| `get_playlist_tracks(name, limit, offset)` | The tracks of one Apple Music playlist, in order, a page at a time. |
| `find_missing_files(only_with_cues=True, limit, offset)` | Rekordbox tracks whose file isn't where Rekordbox says, and where Apple Music has it (matched by path, then file name, then title and artist; an unclear match is never guessed). Needs both libraries loaded. |
| `compare_libraries()` | Compares the loaded Rekordbox and Apple Music libraries: tracks only in one (Apple Music tracks that are in no playlist are counted and ignored, since only playlisted tracks are meant for Rekordbox), Apple Music files listed twice, and every playlist that differs and how. Needs both loaded. |
| `list_library_differences(kind, playlist, music_folder_only=True, include_unplaylisted=False, limit, offset)` | The tracks behind that comparison (`in_apple_music_only`, `in_rekordbox_only`, `duplicate_entries`), for the whole library or one playlist. |
| `export_playlists_for_rekordbox(playlists, all_playlists=False, output_path, folder_name, dry_run=True)` | Writes Apple Music playlists into a small Rekordbox XML (default `cuebridge-playlists.xml` next to your export) that Rekordbox imports in one step, replacing the drag and drop. By default only playlists that differ from Rekordbox are included. See [Getting playlists into Rekordbox](#getting-playlists-into-rekordbox). |
| `set_default_cue(output_path, dry_run=True)` | Adds a hot cue A at 0:00 to every track with no cue or loop, in the loaded XML file (a backup is saved first). |
| `duplicate_memory_cues_to_hot_cues(output_path, dry_run=True)` | Copies every memory cue to a hot cue at the same position, in the lowest free slots (A-H), in the loaded XML file. Memory cues are kept. A memory cue that already has a hot cue at the same time is skipped, so running it twice changes nothing. Cues beyond 8 are reported and not copied. |

| `sync_cues_to_serato(dry_run=True, expected_files, track_ids, include_memory_cues=True, use_apple_music_paths=True)` | **Edits your audio files.** Copies the loaded Rekordbox library's hot cues (and memory cues, in free slots) into each file's Serato cue tag. Rekordbox wins. If an Apple Music library is loaded, a file that moved is found there. |
| `undo_serato_sync(journal, dry_run=True)` | Puts back the original cue bytes recorded by a sync. |

Results are paged (at most 100 per call) so large libraries don't flood the assistant's context.

### Safety of the writing tools (`set_default_cue`, `duplicate_memory_cues_to_hot_cues`)

These two tools **edit your `rekordbox.xml` in place**. That file is an export, and Rekordbox itself is unaffected, but you can lose cues you added to it. So:

- **Dry run by default.** With `dry_run=true` they report what would change and whether a backup would be made, and write nothing. Ask the assistant to show you that first.
- **Backup first.** The first edit to a file that came from Rekordbox saves a timestamped copy next to it (`rekordbox.xml.backup-20261103-121017`) and checks it is identical. Edited files carry a marker comment (`<!-- Edited by CueBridge. -->`), so later edits to the same file don't pile up more backups. A fresh export from Rekordbox is backed up again.
- **Atomic write.** The new XML goes to a temporary file that is swapped in, and file permissions are kept. An interrupted write can't leave a half-written file.
- **Refuses stale plans.** If the file changed between planning and writing, for example because you re-exported from Rekordbox, nothing is written.
- **Verified before saving.** The tools re-parse their own output and stop if anything other than the intended cues changed.
- **Repeatable.** Running a tool twice changes nothing the second time, and the two tools can be run in either order, so their edits add up in the same file.
- **Optional copy.** Pass `output_path` (ending in `.xml`) to save the edited XML elsewhere and leave the loaded file alone. CueBridge only ever replaces a file at that path if it wrote it.

To undo an edit, copy the backup back over `rekordbox.xml`.

Back up your Rekordbox library before importing an edited file. How Rekordbox treats cues on tracks already in your collection when it imports an XML hasn't been tested yet, so the edited file is only proven to be valid XML that CueBridge itself reads back correctly.

### Getting playlists into Rekordbox

Rekordbox can't be told to add playlists from outside, so `export_playlists_for_rekordbox` writes the next best thing: one small XML file holding a folder of the playlists that need to get in. Tracks Rekordbox already has are copied as they are (cues and beat grids included); tracks it doesn't have are added from Apple Music's data; a track whose file moved gets Apple Music's current path. The tool never edits your Rekordbox export or Rekordbox's database, and it only replaces a file it wrote itself.

The loop, whenever you add music or playlists in Apple Music:

1. Export your Apple Music library again (**File → Library → Export Library**) and your Rekordbox collection (**File → Export Collection in xml format**).
2. Ask Claude to load both, then to do a dry run of the playlist export, and to write it.
3. In Rekordbox, once: **Preferences → Advanced → Database → rekordbox xml → Imported Library**, choose `cuebridge-playlists.xml`, and show the *rekordbox xml* item in the sidebar. Each time: reload it, expand it, right-click the folder (default name *Apple Music*) and choose **Import Playlist**.

Not yet tested: how Rekordbox treats a playlist that already exists (a copy, or a merge) and whether importing a track it already has makes a duplicate. Try it with one small playlist first.

### Syncing cues to Serato

`sync_cues_to_serato` is the one tool that changes your music files, so it is the most careful.

- **Rekordbox wins, but only changes what differs.** For every track that has cues in Rekordbox, Serato's hot cues are made to match: Rekordbox hot cues keep their slots, memory cues fill the free ones, and a Serato cue that is not in Rekordbox is removed. A cue that already agrees (same slot, within 1 ms, same color and name where Rekordbox has them) is left byte for byte as it is, so colors you already have in Serato survive. Tracks with no cues in Rekordbox are never touched.
- **Timing.** Rekordbox and Serato place MP3 cues about 5.5 ms apart, and WAV cues exactly the same. The correction is applied by file type. For MP3s with an Xing/Info frame but no LAME tag the shift is one or two whole MP3 frames (26 or 52 ms) later in Serato; it is estimated from the two programs' beat grids, and those files are listed in the dry run as estimated. Without usable grids no correction is applied. An existing Serato cue within 30 ms of the estimate is trusted, so positions you fixed by hand in Serato are kept.
- **Colors.** Rekordbox's default hot-cue green (`#28e214`) counts as "no color chosen", so it doesn't repaint your Serato colors. A color you picked in Rekordbox does win.
- **Both of Serato's cue frames.** Serato keeps the cues in `Markers2` (470 bytes) and in an older `Markers_` frame (318 bytes) that holds the first five. Serato DJ Pro reads the older one, so both are written and kept in step.
- **In place, and tiny.** Only those fixed-size frames are overwritten. Nothing else in the file moves, and the file's modified time is kept, as Serato does.
- **Journal and undo.** Before any file is touched, the original bytes of every file are saved to `~/.cuebridge/undo/` (set `CUEBRIDGE_HOME` to move it). `undo_serato_sync` restores them. A file whose tag was changed again afterwards is left alone.
- **Verified after every write.** The file is read back, and the bytes outside the tag must hash identically to before. A file that fails is restored immediately.
- **Dry run first, and the count must match.** `dry_run` is the default. To apply, pass `expected_files` equal to the number the dry run reported, or nothing is written.
- **Close Serato first.** Serato keeps cues in memory and can overwrite a change when a track is ejected or it quits, so the tool refuses while Serato DJ Pro is running.
- **MP3 and WAV, files that already have a Serato tag.** Files with no tag yet (load them in Serato once), M4A, AIFF and FLAC are skipped and listed with the reason.

A synced WAV has been opened in Serato and shows the new cues; MP3s are checked only by reading the files back. Sync one track first and look at it in Serato.

## Example prompts

> Load /path/to/rekordbox.xml and tell me how many tracks have no cues.

> Which tracks by "Daft Punk" have a loop?

> Do a dry run of set_default_cue and show me what would change.

## Development

```bash
cd backend
pytest                        # all tests
pytest tests/test_mcp_server.py::TestSetDefaultCue   # one class
```

Layout:

```
backend/cuebridge/
  models.py        shared Pydantic models (Track, Cue)
  parsers/         file formats to models
  services/        path-based logic: validation, caching, queries, XML editing
  mcp_server.py    the MCP server (a thin layer over services/)
  web/main.py      FastAPI app used by the prototype web UI
frontend/          React + TypeScript prototype UI
```

### Prototype web UI (on hold)

An earlier version had a web UI for uploading and browsing a Rekordbox XML. It still runs but isn't being extended until the MCP workflow is complete (see the roadmap).

```bash
# terminal 1, in backend/ with the virtualenv active
uvicorn cuebridge.web.main:app --reload --port 8000

# terminal 2, in frontend/
npm install
npm run dev                   # then open http://localhost:5173
```
