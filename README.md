# CueBridge

CueBridge is a local-first tool for managing a DJ library across **Apple Music**, **Rekordbox** and **Serato**. It's an **MCP server**, so an AI assistant such as Claude can read the libraries, compare them, and carefully make the changes that keep them in step. It is a personal tool, built around one setup: Apple Music holds the tracks and playlists, Rekordbox holds the cues and USB sets, and Serato plays from the Apple Music playlists.

Everything runs on your own machine, and your library files are never uploaded anywhere. What an AI assistant sees is a different matter: the tools return track names, file paths, playlists and cues to whichever assistant is connected, so that data goes to that assistant's provider. That applies to Claude Code and Claude Desktop, and to the built-in agent below, which uses OpenAI.

## Status

Early development, used on one real library. What works today:

- **Cues, Rekordbox to Serato.** Reads Rekordbox's cues from its live database (no export) and copies the hot and memory cues into the Serato tags of your MP3 and WAV files (both of Serato's cue frames, with the timing difference between the programs corrected), with Rekordbox winning, a journal and undo.
- **Apple Music, live.** Reads tracks and playlists straight from the open Music app, with no export.
- **Comparing the libraries.** Matches tracks across Apple Music and Rekordbox (by path, file name, then title and artist), and shows which tracks and playlists differ, which files moved, and which Apple Music files are listed twice.
- **Comparing cues.** `compare_cues` is a read-only check of what Rekordbox and Serato hold per track: cues in only one of them, and differences in position, colour and name.
- **Auditing the Apple Music library.** `audit_music_library` is read-only and reports duplicates, metadata gaps, playlist hygiene, formats and quality, missing files, and library questions.
- **Playlists into Rekordbox.** Adds new tracks to your existing Rekordbox playlists by editing Rekordbox's database (Rekordbox closed; backup, verification and undo), and prepares new tracks and playlists for Rekordbox's XML import.
- **Checking everything is in sync.** `check_sync_status` gives one read-only report across the three apps and names the tool that fixes each difference.
- **Repairing Rekordbox after Apple Music renames files.** Repoints moved files, reports stale duplicate rows, and removes them while keeping their cues, each with a dry run, backup, verification and undo.
- **Rekordbox XML.** Reads a Rekordbox export (tracks, cues, playlists). The only XML editing left is the playlist export, which refuses an export older than Rekordbox's database. Cue edits go to the database.
- **Its own agent.** `make agent` opens a terminal chat (OpenAI Agents SDK) that uses all the tools, and asks you before anything is really changed. See [Or use the built-in agent](#or-use-the-built-in-agent).

Not built yet: Serato to Rekordbox cues, cue support for M4A, AIFF and FLAC files (they are skipped today), fixing tags inside the files, Serato crates, and a snapshot of the small important files. Loops on pads other than H and memory loops are still unchecked against Rekordbox. See [ROADMAP.md](docs/ROADMAP.md) for completed work, known issues and what's next.

## Setup

Requires Python 3.10 or newer. The tests run on 3.10 to 3.14 (checked locally and in CI); development is on 3.13 (see `.python-version`).

```bash
make setup      # creates .venv and installs the dependencies and dev tools
make check      # ruff, then the Python and screen tests (what CI runs); `make test`, `make test-ui` and `make lint` run one each
```

The built-in agent's screen (`src/ui/`) is a small Node app and needs Node 20 or newer; `make agent` installs its packages the first time (`make setup-ui` does it by hand and tells Dropbox to leave `src/ui/node_modules` alone). The MCP server needs no Node.

Or by hand:

```bash
# from the repo root
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # optional: run the tests
```

`make setup-db` also installs the optional Rekordbox database packages (the `rekordbox-db` extra in `pyproject.toml`; it needs `brew install sqlcipher` first; see [Updating Rekordbox playlists directly](#updating-rekordbox-playlists-directly)).

### Export your Rekordbox library

In Rekordbox, choose **File → Export Collection in xml format** and save the file somewhere you can find it. CueBridge can read that XML file, but it is no longer needed for the cue sync or the sync check: those read Rekordbox's live database (cues, beat grids and all). The export is a fallback, and what the two XML editing tools work on. An iTunes `Library.xml` won't work.

### Register the server with Claude Code

From the repository root:

```bash
claude mcp add --scope local cuebridge \
  -e "PYTHONPATH=$(pwd)/src" \
  -- "$(pwd)/.venv/bin/python" -m cuebridge.mcp_server
```

Start a new session (or run `/mcp`) and `cuebridge` should show as connected. The server uses stdio, so any MCP client can run the same command: `python -m cuebridge.mcp_server` with `src/` on `PYTHONPATH`. For example, Claude Desktop reads the same command, arguments and environment from its `mcpServers` config.

### Or use the built-in agent

CueBridge also has its own terminal chat, so you don't need Claude Code or Claude Desktop. The screen is an [Ink](https://github.com/vadimdemedes/ink) app (`src/ui/`). It starts the Python agent (`src/cuebridge/agent/`, built on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)), which starts the same MCP server, so it has every tool above. The two talk over stdin and stdout, one JSON object per line (described at the top of `src/cuebridge/agent/protocol.py`).

1. Put your key in `.env` (git ignores this file):

   ```
   OPENAI_API_KEY=sk-...
   # CUEBRIDGE_MODEL=       optional; the SDK's default model is used if this is empty
   ```

   The repository is in Dropbox, which will sync that file. If that is not what you want, set `OPENAI_API_KEY` in your shell instead; the environment is read first.

2. Start it from the repository root:

   ```bash
   make agent
   ```

   Type a request at the `you>` prompt (an empty line or Ctrl-D quits; the up arrow recalls earlier requests). Opening the Music app first lets it read your Apple Music library. While it works you see a spinner and a line for each tool it calls (`→ name(arguments)`, then `✓` with the time, or a red `✗` with the reason if the tool failed). Replies are shown as formatted markdown.

**Approval.** Reading and dry runs go straight through. A call that would really change something (a writing tool with `dry_run=false`) stops and shows a "Tool Approval Required" block with the tool and its arguments and a Yes / No choice. The choice starts on No, so Enter alone refuses; move with the arrow keys and press Enter, or press `y`, to approve. Any other key is a no. That check is in `src/cuebridge/agent/runner.py`, not in the model's hands and not in the screen's: the screen can only answer, and a wrong id, a malformed answer, anything but a real `true`, or the screen closing all count as a no. Anything unclear (for example, arguments it cannot read) is asked about too. If you refuse, nothing changes and the model is told so.

**Privacy.** Every tool result the model needs (track names, file paths, playlists, cues) is sent to OpenAI. Tracing is switched off, because the SDK would otherwise send traces to OpenAI too. Your audio files and the libraries themselves stay on your machine. To keep library data off OpenAI's servers, use the MCP server with Claude instead, or leave the agent alone.

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
# from the repo root
pytest                        # all tests
pytest tests/server/test_mcp_server.py::TestSetDefaultCue   # one class
```

Layout:

```
src/cuebridge/
  models.py        shared Pydantic models (Track, Cue)
  paths.py         file:// locations to paths
  services/        the logic, grouped by app:
    rekordbox/     the XML parser, loading and caching, the live database (reading, writing, adding cues, its cues as Track/Cue), the playlist export's XML writing
    apple_music/   reading Apple Music, live from the app or from a file, and its XML parser
    serato/        the tag and marker codecs, and the cue sync into Serato's tags
    compare/       matching tracks, finding differences, the cue comparison, the three-way sync check
    reports/       searches and the read-only reports
    cues/          cue rules and timing
  agent/           the built-in agent: runner.py (OpenAI Agents SDK, headless, and its approval gate),
                   protocol.py (the JSON-lines conversation with the screen)
  tools/           the MCP tools, one module per area: libraries, reports, playlists, repairs, cues, serato
                   (common.py holds the server, the loaded-library state and the error translation)
  mcp_server.py    the entry point: imports the tools (which registers them) and starts the server
src/ui/            the agent's screen (Ink, TypeScript), tests beside the code:
                   App.tsx, components/ (entries, approval panel, spinner, prompt),
                   agent/ (connection.ts starts the Python agent; protocol.ts is the JSON lines),
                   lib/ (state.ts turns events into what is shown; format.ts, markdown.ts)
tests/             mirrors src/cuebridge: agent/, apple_music/, rekordbox/, serato/, cues/, library/, server/; helpers/ and fixtures/
docs/ROADMAP.md    completed work, known issues and what's next

```
