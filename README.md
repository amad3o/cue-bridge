# CueBridge

CueBridge is a local-first tool for managing a DJ library across **Apple Music**, **Rekordbox** and **Serato**. It's an **MCP server**, so an AI assistant such as Claude can read the libraries, compare them, and carefully make the changes that keep them in step. It is a personal tool, built around one setup: Apple Music holds the tracks and playlists, Rekordbox holds the cues and USB sets, and Serato plays from the Apple Music playlists.

Everything runs on your own machine, and your library files are never uploaded anywhere.

## Status

Early development, used on one real library. What works today:

- **Cues, Rekordbox to Serato.** Copies Rekordbox's hot and memory cues into the Serato tags of your MP3 and WAV files (both of Serato's cue frames, with the timing difference between the programs corrected), with Rekordbox winning, a journal and undo.
- **Apple Music, live.** Reads tracks and playlists straight from the open Music app, with no export.
- **Comparing the libraries.** Matches tracks across Apple Music and Rekordbox (by path, file name, then title and artist), and shows which tracks and playlists differ, which files moved, and which Apple Music files are listed twice.
- **Playlists into Rekordbox.** Adds new tracks to your existing Rekordbox playlists by editing Rekordbox's database (Rekordbox closed; backup, verification and undo), and prepares new tracks and playlists for Rekordbox's XML import.
- **Rekordbox XML.** Reads a Rekordbox export (tracks, cues, playlists), and has two editing tools for it (see the warning under their safety section).

Not built yet: reading cues from Rekordbox's database (so no export is needed), comparing cues between the programs, metadata and playlist-hygiene reports, and M4A/AIFF cue support. See [ROADMAP.md](ROADMAP.md) for completed work, known issues and what's next.

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
| `load_apple_music_library(path=None)` | With no path, reads the **open Music app directly** (no export; about 20-30 seconds; read-only; Music must already be running and macOS asks for permission the first time). With a path, reads an XML exported from Music (**File → Library → Export Library**). Summarises the library: tracks, formats, metadata gaps and every playlist with its size. Call it again to pick up changes. |
| `get_playlist_tracks(name, limit, offset)` | The tracks of one Apple Music playlist, in order, a page at a time. |
| `find_missing_files(only_with_cues=True, limit, offset)` | Rekordbox tracks whose file isn't where Rekordbox says, and where Apple Music has it (matched by path, then file name, then title and artist; an unclear match is never guessed). Needs both libraries loaded. |
| `compare_libraries()` | Compares the loaded Rekordbox and Apple Music libraries: tracks only in one (Apple Music tracks that are in no playlist are counted and ignored, since only playlisted tracks are meant for Rekordbox), Apple Music files listed twice, and every playlist that differs and how. Needs both loaded. |
| `list_library_differences(kind, playlist, music_folder_only=True, include_unplaylisted=False, limit, offset)` | The tracks behind that comparison (`in_apple_music_only`, `in_rekordbox_only`, `duplicate_entries`), for the whole library or one playlist. |
| `export_playlists_for_rekordbox(playlists, all_playlists=False, output_path, folder_name, dry_run=True)` | Adds Apple Music playlists to the Rekordbox XML (or to a separate file if `output_path` is given) so Rekordbox imports them in one step, replacing the drag and drop. By default only playlists that differ from Rekordbox are included. See [Getting playlists into Rekordbox](#getting-playlists-into-rekordbox). |
| `sync_playlists_to_rekordbox(playlists, dry_run=True, expected_additions, database_path)` | **Edits Rekordbox's database.** Adds tracks that are in an Apple Music playlist but missing from the same Rekordbox playlist, at the end of the existing playlist (this is how an existing playlist can be updated; XML import can't). Needs no Rekordbox export. Needs [the optional database packages](#updating-rekordbox-playlists-directly). |
| `undo_rekordbox_playlist_sync(journal, dry_run=True)` | Removes exactly the playlist entries a sync added. |
| `fix_moved_files_in_rekordbox(dry_run=True, expected_fixes, database_path)` | Points Rekordbox tracks whose file was renamed or moved at the file Apple Music has (database; path and file name only). |
| `undo_rekordbox_file_fix(journal, dry_run=True)` | Puts the old paths back. |
| `report_stale_duplicates(database_path, limit)` | Read-only. For broken Rekordbox rows whose real file Rekordbox already has under another row: cues and playlists on each, and whether the broken row is safe to remove or needs a merge. |
| `set_default_cue(output_path, dry_run=True)` | Adds a hot cue A at 0:00 to every track with no cue or loop, in the loaded XML file (a backup is saved first). |
| `duplicate_memory_cues_to_hot_cues(output_path, dry_run=True)` | Copies every memory cue to a hot cue at the same position, in the lowest free slots (A-H), in the loaded XML file. Memory cues are kept. A memory cue that already has a hot cue at the same time is skipped, so running it twice changes nothing. Cues beyond 8 are reported and not copied. |

| `sync_cues_to_serato(dry_run=True, expected_files, track_ids, include_memory_cues=True, use_apple_music_paths=True)` | **Edits your audio files.** Copies the loaded Rekordbox library's hot cues (and memory cues, in free slots) into each file's Serato cue tag. Rekordbox wins. If an Apple Music library is loaded, a file that moved is found there. |
| `undo_serato_sync(journal, dry_run=True)` | Puts back the original cue bytes recorded by a sync. |

Results are paged (at most 100 per call) so large libraries don't flood the assistant's context.

### Safety of the writing tools (`set_default_cue`, `duplicate_memory_cues_to_hot_cues`)

These two tools **edit your `rekordbox.xml` in place**. That file is an export, and Rekordbox itself is unaffected, but you can lose cues you added to it.

They insert only the new cue lines as text, in Rekordbox's own style, and every other byte of the file stays as it was (declaration, CRLF line endings, escaping, spacing; no marker comment). This is the same approach as the playlist export, because Rekordbox refused an XML library's rewrite of its file. Whether Rekordbox accepts an *edited* export has not been checked in the real application yet.

So:

- **Dry run by default.** With `dry_run=true` they report what would change and whether a backup would be made, and write nothing. Ask the assistant to show you that first.
- **Backup first.** Every in-place edit saves a timestamped copy of the file as it was (`rekordbox.xml.backup-20261103-121017`) and checks it is identical. An edit that would change nothing writes nothing and makes no backup. With `output_path` a new file is created and an existing file is never overwritten.
- **Atomic write.** The new XML goes to a temporary file that is swapped in, and file permissions are kept. An interrupted write can't leave a half-written file.
- **Refuses stale plans.** If the file changed between planning and writing, for example because you re-exported from Rekordbox, nothing is written.
- **Verified before saving.** The tools re-parse their own output and stop if anything other than the intended cues changed.
- **Repeatable.** Running a tool twice changes nothing the second time, and the two tools can be run in either order, so their edits add up in the same file.
- **Optional copy.** Pass `output_path` (ending in `.xml`) to save the edited XML elsewhere and leave the loaded file alone. CueBridge only ever replaces a file at that path if it wrote it.

To undo an edit, copy the backup back over `rekordbox.xml`.

Back up your Rekordbox library before importing an edited file. How Rekordbox treats cues on tracks already in your collection when it imports an XML hasn't been tested yet, so the edited file is only proven to be valid XML that CueBridge itself reads back correctly.

### Getting playlists into Rekordbox

Rekordbox can't be told to add playlists from outside, so `export_playlists_for_rekordbox` prepares the next best thing: a folder of the playlists that need to get in, written **into the Rekordbox XML that Rekordbox's Imported Library setting already points at**, so nothing has to be re-pointed. Tracks Rekordbox already has are left as they are (cues and beat grids included); tracks it doesn't have are added from Apple Music's data; a track whose file moved gets Apple Music's current path. Rekordbox's own database is never touched.

It is an edit of your export, so it works like the other editing tools: dry run by default, a timestamped backup of a fresh export first, an atomic write, and a refusal if the file changed since it was planned. Unlike the other editing tools it does not re-serialise the XML: Rekordbox refused an XML library's rewrite of its file (different declaration, line endings, escaping and spacing, and marker comments), so only the new lines are inserted, in Rekordbox's own style, and every other byte stays as it was. A check confirms that taking the additions back out gives your original file. What is pending is recorded in `rekordbox.xml.cuebridge.json` next to the file (tied to the file's checksum, so a fresh export makes it stale). Running it again replaces its own folder. Pass `output_path` to write a separate small XML instead and leave your export alone.

The loop, whenever you add music or playlists in Apple Music:

1. Have a current Rekordbox export (**File → Export Collection in xml format**). Apple Music needs no export: with Music open, Claude reads it directly.
2. Ask Claude to load both, do a dry run of the playlist export, then write it.
3. In Rekordbox, once: **Preferences → Advanced → Database → rekordbox xml → Imported Library**, choose your export, and show the *rekordbox xml* item in the sidebar. Each time: reload it, expand it, right-click the folder (default name *Apple Music*) and choose **Import Playlist**.

What Rekordbox really has is only known from an export, so the added folder and tracks are marked as pending: the loader leaves them out until you export Rekordbox afresh, so the comparison keeps showing what still needs importing.

**Not yet verified in Rekordbox.** The file is valid XML and re-parses to exactly what was planned, but no playlist has been imported from it yet. Check with one small change first (a track added to a small playlist).

### Updating Rekordbox playlists directly

Rekordbox can only create a playlist from an XML import; it never updates one that exists. To add a track to a playlist you already have, CueBridge edits Rekordbox's own database (`master.db`, found from Rekordbox's settings; on your setup it lives on the music drive, not in your home folder). This is the riskiest thing CueBridge does, so it is narrow and layered:

- **What it does.** It only adds a track that is already in your collection to the end of an existing top-level playlist with the same name. It never removes tracks, creates playlists, or touches tracks, files, cues, analysis or other playlists. Tracks Rekordbox doesn't have, and playlists it doesn't have, are reported (import a new track first, for example with `export_playlists_for_rekordbox`).
- **Rekordbox must be closed to write.** Planning (the dry run) reads a copy of the database, so Rekordbox may stay open for that.
- **Backup first.** The database files are copied to `~/.cuebridge/db-backups/<time>/` and the copy is checked byte for byte and by reading it back. (Rekordbox also keeps its own dated backups next to `master.db`.)
- **Rekordbox's own bookkeeping.** The write goes through pyrekordbox's methods, which keep row IDs, timestamps and sync counters right.
- **Checked afterwards.** The database is read again; every playlist must be exactly as planned and the track list unchanged, otherwise the backup is put back.
- **Undo.** `undo_rekordbox_playlist_sync` removes exactly the rows a run added, if they are still there unchanged.
- **Dry run first, and the count must match** (`expected_additions`), as with the Serato sync.

**Fixing renamed files.** `fix_moved_files_in_rekordbox` uses the same safeguards (dry run, Rekordbox closed, backup, read-back check, journal and undo, count must match) to change the path and file name of a track whose file is missing, to the file Apple Music has for it. A track is left alone, and reported, when Rekordbox already has another row for the real file (a duplicate left behind by the rename; repointing would make a second one, and the older row may hold cues or playlist entries), or when Apple Music has no matching file. The analysis files under `PIONEER/USBANLZ` are not touched, and Rekordbox opening a repointed track has not been checked yet.

It is not an officially supported way to edit Rekordbox, and a Rekordbox update could change the database format. It needs two optional packages:

```bash
brew install sqlcipher
cd backend && source .venv/bin/activate
P="$(brew --prefix sqlcipher)"
CFLAGS="-I$P/include -I$P/include/sqlcipher" LDFLAGS="-L$P/lib" SQLCIPHER_PATH="$P" \
  pip install -r requirements-rekordbox-db.txt
```

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

> Load my Apple Music library and my Rekordbox export, and tell me which playlists differ.

> Do a dry run of sync_playlists_to_rekordbox. (Then quit Rekordbox and apply it.)

> Do a dry run of sync_cues_to_serato and show me which files would change.

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
  parsers/         file formats to models (Rekordbox XML, Apple Music XML, Serato tags)
  services/        path-based logic: loading and caching, queries and comparison, the Serato sync,
                   Apple Music (file and live), Rekordbox XML edits, Rekordbox's database
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
