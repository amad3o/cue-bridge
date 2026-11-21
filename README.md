# CueBridge

CueBridge is a local-first tool for managing a DJ library across **Apple Music**, **Rekordbox** and **Serato**. It's an **MCP server**, so an AI assistant such as Claude can read the libraries, compare them, and carefully make the changes that keep them in step. It is a personal tool, built around one setup: Apple Music holds the tracks and playlists, Rekordbox holds the cues and USB sets, and Serato plays from the Apple Music playlists.

Everything runs on your own machine, and your library files are never uploaded anywhere.

## Status

Early development, used on one real library. What works today:

- **Cues, Rekordbox to Serato.** Reads Rekordbox's cues from its live database (no export) and copies the hot and memory cues into the Serato tags of your MP3 and WAV files (both of Serato's cue frames, with the timing difference between the programs corrected), with Rekordbox winning, a journal and undo.
- **Apple Music, live.** Reads tracks and playlists straight from the open Music app, with no export.
- **Comparing the libraries.** Matches tracks across Apple Music and Rekordbox (by path, file name, then title and artist), and shows which tracks and playlists differ, which files moved, and which Apple Music files are listed twice.
- **Playlists into Rekordbox.** Adds new tracks to your existing Rekordbox playlists by editing Rekordbox's database (Rekordbox closed; backup, verification and undo), and prepares new tracks and playlists for Rekordbox's XML import.
- **Checking everything is in sync.** `check_sync_status` gives one read-only report across the three apps and names the tool that fixes each difference.
- **Repairing Rekordbox after Apple Music renames files.** Repoints moved files, reports stale duplicate rows, and removes them while keeping their cues, each with a dry run, backup, verification and undo.
- **Rekordbox XML.** Reads a Rekordbox export (tracks, cues, playlists). The only XML editing left is the playlist export, which refuses an export older than Rekordbox's database. Cue edits go to the database.

Not built yet: reading loops from Rekordbox's database (hot cues A to H and 17 colours are verified), the other colours, comparing cues between the programs, metadata and playlist-hygiene reports, and M4A/AIFF cue support. See [ROADMAP.md](ROADMAP.md) for completed work, known issues and what's next.

## Setup

Requires Python 3.10 or newer. The tests run on 3.10 to 3.14 (checked locally and in CI); development is on 3.13 (see `.python-version`).

```bash
make setup      # creates backend/.venv and installs the dependencies and dev tools
make check      # ruff, then the tests (what CI runs); `make test` and `make lint` run one each
```

Or by hand:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # optional: run the tests
```

`make setup-db` also installs the optional Rekordbox database packages (it needs `brew install sqlcipher` first; see [Updating Rekordbox playlists directly](#updating-rekordbox-playlists-directly)).

### Export your Rekordbox library

In Rekordbox, choose **File → Export Collection in xml format** and save the file somewhere you can find it. CueBridge can read that XML file, but it is no longer needed for the cue sync or the sync check: those read Rekordbox's live database (cues, beat grids and all). The export is a fallback, and what the two XML editing tools work on. An iTunes `Library.xml` won't work.

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
| `export_playlists_for_rekordbox(playlists, all_playlists=False, output_path, folder_name, dry_run=True)` | Adds Apple Music playlists to the Rekordbox XML (or to a separate file if `output_path` is given) so Rekordbox imports them in one step, replacing the drag and drop. By default only playlists that differ from Rekordbox are included. Refused if the export is older than Rekordbox's database (`allow_stale_export=true` overrides). See [Getting playlists into Rekordbox](#getting-playlists-into-rekordbox). |
| `sync_playlists_to_rekordbox(playlists, dry_run=True, expected_additions, database_path)` | **Edits Rekordbox's database.** Adds tracks that are in an Apple Music playlist but missing from the same Rekordbox playlist, at the end of the existing playlist (this is how an existing playlist can be updated; XML import can't). Needs no Rekordbox export. Needs [the optional database packages](#updating-rekordbox-playlists-directly). |
| `undo_rekordbox_playlist_sync(journal, dry_run=True)` | Removes exactly the playlist entries a sync added. |
| `fix_moved_files_in_rekordbox(dry_run=True, expected_fixes, database_path)` | Points Rekordbox tracks whose file was renamed or moved at the file Apple Music has (database; path and file name only). |
| `undo_rekordbox_file_fix(journal, dry_run=True)` | Puts the old paths back. |
| `create_review_playlists(dry_run=True, expected_tracks)` | Adds two temporary playlists to Rekordbox holding the stale duplicate rows (safe to remove; check cues first), so you can remove them in Rekordbox itself. Only adds playlists. |
| `undo_rekordbox_review_playlists(journal, dry_run=True)` | Removes those two playlists (never tracks). |
| `clean_up_stale_duplicates(dry_run=True, expected_removals, expected_cue_copies)` | Database write. Removes stale duplicate rows that hold nothing their twin lacks (with the cues, analysis-file records, mixer settings and playlist entries that belong to them), and copies cues a broken row has to its twin (keeping that row). Journal holds every removed row. |
| `undo_stale_duplicate_cleanup(journal, dry_run=True)` | Takes the copied cues off and puts the removed rows back. |
| `check_sync_status(rekordbox_xml, reload_apple_music, database_path)` | One read-only report: are Apple Music, Rekordbox and Serato in sync? Tracks and playlists (from Rekordbox's live database), missing files and stale duplicates, and Serato cues against Rekordbox's. Names the tool that fixes each thing. Changes nothing. |
| `report_stale_duplicates(database_path, limit)` | Read-only. For broken Rekordbox rows whose real file Rekordbox already has under another row: cues and playlists on each, and whether the broken row is safe to remove or needs a merge. |
| `set_default_cue(dry_run=True, expected_cues, playlisted_only=True, database_path)` | **Edits Rekordbox's database.** Adds a hot cue A at 0:00 to every playlisted track with no cue or loop. Tracks in no playlist (thousands of sample-pack files) are left alone unless `playlisted_only=false`. Rekordbox must be closed to write. |
| `duplicate_memory_cues_to_hot_cues(dry_run=True, expected_cues, playlisted_only=True, database_path)` | **Edits Rekordbox's database.** Copies every memory cue to a hot cue at the same position, in the lowest free slots (A-H). Memory cues are kept. A memory cue that already has a hot cue at the same time is skipped, so running it twice changes nothing. Cues beyond 8 are reported and not copied; a track with a cue in a slot CueBridge can't read is skipped and reported. |
| `undo_rekordbox_cue_additions(journal, dry_run=True)` | Takes the cues those two tools added off again. |
| `compare_cues(show="differences", limit, offset, cue_source="auto", include_memory_cues=True, use_apple_music_paths=True, playlisted_only=True)` | **Read-only.** Compares Rekordbox's cues with the cues in Serato's tags, per track: cues only in Rekordbox, cues only in Serato (a sync would remove them from tracks that have Rekordbox cues), and cues in both that differ in position (ms), colour or name. Uses the sync's own rules, so a matching track is one the sync would leave alone. MP3 and WAV; loops are counted, not compared. |
| `audit_music_library(report, show, limit, offset, playlisted_only, recent_days, low_bit_rate_kbps, short_seconds, many_playlists)` | **Read-only** audits of the loaded Apple Music library. `report` is `duplicates` (same title and artist, same file, same size and length), `metadata_gaps` (no artist, album, genre, BPM, year; Unknown Artist folders; very short tracks), `playlists` (biggest, tracks in no or many playlists, empty, overlap), `formats` (by format, bit rate, sample rate; low-bit-rate and largest files), `questions` (size, recently added, per genre and year) or `missing_files` (checks the disk). Every section is counted; `show` picks which one is listed. Only playlisted tracks by default, except `playlists` and `questions`. |
| `sync_cues_to_serato(dry_run=True, expected_files, track_ids, include_memory_cues=True, use_apple_music_paths=True, cue_source="auto")` | **Edits your audio files.** Copies Rekordbox's hot cues (and memory cues, in free slots) into each file's Serato cue tag. Rekordbox wins. Reads the cues from Rekordbox's live database (`cue_source="auto"`; `"xml"` uses the loaded export instead). If an Apple Music library is loaded, a file that moved is found there. |
| `undo_serato_sync(journal, dry_run=True)` | Puts back the original cue bytes recorded by a sync. |

Results are paged (at most 100 per call) so large libraries don't flood the assistant's context.

### Safety of the cue-writing tools (`set_default_cue`, `duplicate_memory_cues_to_hot_cues`)

These tools **write to Rekordbox's own database** (`master.db`), like the playlist and file-repair tools, so no XML export is needed. Needs [the optional database packages](#updating-rekordbox-playlists-directly).

- **Dry run by default.** With `dry_run=true` they report the number of tracks and cues and a few examples, and write nothing (Rekordbox may stay open). Applying needs `dry_run=false` and `expected_cues` equal to the dry run's count.
- **Rekordbox must be closed** to write.
- **Backup, then verify.** The database files are copied and the copy is checked first. After the write the database is read back, and if anything other than the planned cues differs (tracks, playlists, cues) the backup is put back.
- **Journal and undo.** Each run records the cues it added; `undo_rekordbox_cue_additions` removes exactly those.
- **Refuses a changed database.** If the database changed between the dry run and the write, nothing is written.
- **Rekordbox-shaped rows.** A new cue is written to both places Rekordbox keeps cues (`djmdCue` and the track's JSON copy in `contentCue`), modelled on rows Rekordbox saved itself. Rekordbox opening the result is checked on a copy of the real database but not yet in the application.

## Example prompts

> Load /path/to/rekordbox.xml and tell me how many tracks have no cues.

> Which tracks by "Daft Punk" have a loop?

> Do a dry run of set_default_cue and show me what would change. (Then quit Rekordbox and apply it.)

> Load my Apple Music library and my Rekordbox export, and tell me which playlists differ.

> Do a dry run of sync_playlists_to_rekordbox. (Then quit Rekordbox and apply it.)

> Do a dry run of sync_cues_to_serato and show me which files would change.

> Check that Apple Music, Rekordbox and Serato are in sync. (Read-only; it tells you which tool fixes each difference.)

> Report stale duplicates in Rekordbox, then clean them up. (Dry run first; Rekordbox must be closed to apply.)

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
                   Apple Music (file and live), the playlist export's XML writing, Rekordbox's database (reading,
                   writing, adding cues, and its cues as Track/Cue), the three-way sync check
  mcp_server.py    the MCP server (a thin layer over services/)
```
