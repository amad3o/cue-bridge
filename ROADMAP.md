# CueBridge Roadmap

CueBridge is an **MCP server** that lets an AI assistant such as Claude read, compare and convert DJ cue points between Rekordbox and Serato, and help manage the music library, by calling tools against files on the user's own machine.

The parsing, matching and conversion logic stays in plain Python. The MCP server is a thin layer over it.

**Audience and scope.** CueBridge is a personal tool, built around one library: Apple Music for tracks and playlists, Rekordbox for cues and USB sets, Serato for playing, with the music on an ExFAT drive. Claude Code is the usual MCP client, and a built-in terminal agent (`make agent`, section 10) uses the same tools. There is no UI and no plans for one, nor for packaging for other people unless that changes. A single user means the tools can assume this setup, but they should stay safe, because they touch real files: dry run first, a journal, verification, and undo.

**Text first.** Every tool returns a clear text summary the assistant can show in chat, such as "12 tracks change, 31 cues added, 2 conflicts", with a short diff of the first few items. There is no UI.

## Where things stand

The loop that was the goal now works end to end with no manual export:

1. Tracks and playlists are managed in **Apple Music**, which CueBridge reads **live** (no export).
2. Cues are set in **Rekordbox**. CueBridge reads them from its **live database** (no export; the XML export is a fallback).
3. `sync_cues_to_serato` copies Rekordbox's cues into the **Serato** tags of the audio files (both cue frames, with timing correction).
4. `sync_playlists_to_rekordbox` adds new tracks to **existing Rekordbox playlists** by editing Rekordbox's database (Rekordbox closed, backup, verification, undo). A brand-new track must first be imported into the Rekordbox collection (the XML route, section 8).
5. When Apple Music renames files, the repair tools fix the broken Rekordbox rows (`fix_moved_files_in_rekordbox`, `clean_up_stale_duplicates`), each with a dry run, backup, verification and undo.
6. `check_sync_status` answers "are the three apps in sync?" in one read-only report, naming the tool that fixes each difference.

## Completed

**MCP server** (`cuebridge/mcp_server.py`, stdio, `mcp` 2.x; registered in Claude Code). 27 tools, all returning compact, paginated output. The tools that write are annotated as destructive, and the server's instructions tell the assistant to show a dry run and get the user's go-ahead first.

| Area                 | Tools                                                                                                                                                                                                                                  |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Rekordbox XML, read  | `load_rekordbox_library`, `search_tracks`, `get_track_cues`, `find_tracks_without_cues`                                                                                                                                                |
| Rekordbox cues, write | `set_default_cue`, `duplicate_memory_cues_to_hot_cues`, `undo_rekordbox_cue_additions` (database)                                                                                                                                     |
| Serato               | `sync_cues_to_serato`, `undo_serato_sync`                                                                                                                                                                                              |
| Apple Music          | `load_apple_music_library` (live from the Music app, or an exported XML), `get_playlist_tracks`                                                                                                                                        |
| Compare and audit    | `compare_libraries`, `list_library_differences`, `find_missing_files`, `compare_cues`, `audit_music_library`                                                                                                                                                                  |
| Playlists            | `export_playlists_for_rekordbox` (XML, for getting new tracks in), `sync_playlists_to_rekordbox`, `undo_rekordbox_playlist_sync` (database)                                                                                            |
| Overview             | `check_sync_status` (read-only report across the three apps, naming the fix for each difference)                                                                                                                                       |
| Broken paths         | `fix_moved_files_in_rekordbox`, `undo_rekordbox_file_fix` (database), `report_stale_duplicates` (read-only), `create_review_playlists`, `undo_rekordbox_review_playlists`, `clean_up_stale_duplicates`, `undo_stale_duplicate_cleanup` |

**Core (Python)**

- Rekordbox parser: unique cue IDs, hot, memory and loop types, loop ends, colors, decoded paths, first beat, duration, and the playlist tree. Missing or blank titles, artists and cue names are `None`.
- Serato: a `Markers2` codec and the legacy `Markers_` frame codec (all 1,541 real tags round-trip byte for byte; 1,040 real files with both frames agreeing are left unchanged by the encoder), a tag reader and locator for MP3 and WAV, and the sync with a write-ahead journal, read-back verification, undo, and a refusal while Serato DJ Pro runs.
- Timing between the programs: WAV 0 ms; MP3 with a LAME header or no Xing/Info frame -5.5 ms; MP3 with an Xing/Info frame but no LAME tag estimated from the two beat grids as whole MP3 frames (checked by ear on one file).
- Apple Music: XML reader, and a live reader that runs a read-only JXA script through `osascript` (the "Share iTunes Library XML" setting doesn't exist in macOS 15's Music app).
- A track matcher (path, file name, then title and artist with duration; never guesses between equal candidates), the Rekordbox/Apple Music drift comparison, and a missing-files report. Only playlisted Apple Music tracks matter (tracks in no playlist are counted and ignored).
- Rekordbox's own database (`master.db`, found from Rekordbox's settings; on this setup it is on the music drive) through pyrekordbox and SQLCipher, an optional dependency. Reads tracks, playlists, cues (slots A to E, memory cues, one colour verified), beat grids and track lengths; writes playlist additions, file paths, cue copies and row removal, always with backup, journal, read-back verification and undo.
- `check_sync_status` (`compare/sync_check.py`): one read-only report across Apple Music, Rekordbox and Serato, with each part reported as not checkable instead of failing.
- The Rekordbox XML is edited as text (new lines only, original bytes untouched) for the playlist export, because Rekordbox rejects an XML library's rewrite of its file.
- Pydantic models, root `.gitignore`, README rewritten around the MCP server.
- 799 pytest tests (ruff clean) (a shared fixture keeps every test away from the real Rekordbox database): parsers, services, the Serato codecs and sync on mutagen-built files, the XML patcher, the database logic against a fake database (with mutation checks on the safeguards), and the tools, directly and through an in-memory MCP client.

## Known issues

1. ~~Housekeeping.~~ Done 2026-11-18: `.python-version` (3.13), `requires-python >=3.10` and ruff config in `backend/pyproject.toml`, a `Makefile` (`make setup`, `make check`), and GitHub Actions CI (ruff and pytest on Python 3.10 to 3.14). The tests pass on all five versions locally. The CI workflow itself hasn't run on GitHub yet.
2. ~~The two Rekordbox XML edit tools re-serialised the XML.~~ Gone: both now write to the database (2026-11-18, section 9), and the XML cue-editing code and its tests were deleted. `rekordbox/editor.py` keeps only the safe-write layer the playlist export uses.
3. ~~Rekordbox tracks pointing at renamed files.~~ Repaired on the live database on 2026-11-05: 1 track repointed, 56 stale duplicate rows removed (51 with nothing the twin lacked, then 5 after their cues were copied to the twin). 20 tracks remain whose file is missing and not in Apple Music; they need a decision from the user (remove them in Rekordbox or find the files). Rekordbox opening the result is still to be confirmed.
4. ~~A Rekordbox export is needed for cues.~~ Fixed: cues, beat grids and track lengths are read from the live database (`rekordbox/db_library.py`), checked against an XML export on the real library (45 of 46 tracks identical, colours included; the rest were stale rows and one cue moved after the export). Hot cue slots A to H and 17 colour indexes are verified (from a test track on 2026-11-06: database index paired with the RGB in a fresh XML export); loops, kind 4 and any other colour are left out and reported.
5. **Rekordbox can only create a playlist from an XML import, never update one.** That is why updating an existing playlist goes through the database. An XML import of `r&b` made a second `r&b` in a new "Apple Music" folder.

## To do

### 1. Core layout

Done. The package is `cuebridge` (a generic name like `app` can collide with other packages; avoid naming a package `mcp`, which would shadow the SDK):

```
backend/cuebridge/
  models.py              shared Pydantic models (Track, Cue)
  tools/                 the MCP tools, one module per area
    common.py (the server, its instructions, the loaded-library state), libraries.py, reports.py, playlists.py,
    repairs.py, cues.py, serato.py, cue_source.py (where the cues come from)
  mcp_server.py          the entry point: imports the tools and starts the server
  parsers/               file formats to models
    paths.py, rekordbox_parser.py, apple_music_xml.py,
    serato_markers.py (both codecs), serato_tags.py (read, locate, beat grid)
  services/              path-based logic, grouped by app
    rekordbox/           service.py (validate, cache, pending-playlist note), db.py (the database), db_library.py (its cues as Track/Cue),
                         cue_writes.py (cues to the database), editor.py (safe XML writing), playlist_export.py (XML patcher)
    apple_music/         live.py (the open Music app), service.py (the file)
    serato/              sync.py
    compare/             track_matcher.py, drift.py, cue_compare.py, sync_check.py
    reports/             library_queries.py, library_audit.py, library_reports.py
    cues/                cue_rules.py, cue_timing.py
```

### 2. Rekordbox XML tools

- [x] Read tools, with a library cache keyed on path, mtime and size.
- [x] `set_default_cue` and `duplicate_memory_cues_to_hot_cues` moved to the database (2026-11-18): dry run, `expected_cues`, Rekordbox closed, backup, read-back verification, journal, `undo_rekordbox_cue_additions`. Playlisted tracks only by default (7,096 of the collection's tracks have no cue, 5,602 of them are in no playlist). Tried on a copy of the real database: 2 memory-to-hot cues applied, found nothing to do on a second plan, and undid exactly.
- [x] The XML versions and `patch_marks` were removed.
- [ ] Optional filters for `set_default_cue`, such as skipping very short tracks (playlisted-only already keeps the sample-pack one-shots out).
- [ ] Claude Desktop registration is described in the README but untested.

### 3. Serato support

**Findings from the real library** (1,667 Apple Music tracks, Serato DJ Pro, files on an ExFAT drive):

- Serato's `database V2` is a track list only. Cues live in the audio files: a `GEOB` frame named `Serato Markers2` (MP3 ID3 tag, or the `id3 ` chunk of a WAV), `01 01` + base64 (72-character lines, no `=`) + NUL padding to exactly 470 bytes, holding `COLOR`, `CUE` and `BPMLOCK` entries.
- A second, older frame `Serato Markers_` holds the first five cues, and **Serato DJ Pro reads it**: a file whose Markers2 had three cues and whose legacy frame had one showed one cue, and Serato then wrote its own version back over Markers2. Both frames must be written. The legacy layout: `02 05`, a count (14), 14 entries of 22 bytes (start, end, color, type, lock; 7 bits per byte), then `07 7f 7f 7f`. The encoder leaves a slot that already holds the right cue untouched, because real files keep default colors or leftover bytes in empty slots.
- Serato only writes a track's tags when the track is ejected from a deck, and logs "Writing Tags" in `~/Music/_Serato_/Logs`. In one test, quitting with the edited track still loaded did not save the change.
- Timing (Rekordbox minus Serato): 0 ms WAV; about 5-6 ms MP3 with a LAME header; for MP3s with an Xing/Info frame but no LAME tag one frame (-26 ms) on three files and two frames (-52 ms) on one. The beat grids' first-beat difference reproduces every known offset (0.0 ms median on WAV, -4.0 on LAME MP3s, +24 to +27 and +53 on the four Xing/Info files); a cue placed by ear on the two-frame file landed 45 and 49 ms after Rekordbox's, against 53 predicted.
- Rekordbox's default hot-cue green (`#28e214`) says nothing about the color wanted in Serato, so it doesn't override it.
- ExFAT has no separate `ctime` and Serato may keep file times, so compare contents, not timestamps.

**Decisions.** Rekordbox wins on which cues exist and where; only what differs is changed; Lexicon-written cues are replaced only when they differ after the timing offset.

- [x] Codecs, reader, in-place writer, journal, verification, undo, the Serato-running refusal, "change only what differs", per-format timing with the beat-grid estimate, colors, both cue frames.
- [x] Verified in Serato on a real WAV (all three cues shown) and, for the estimate, by ear on one MP3.
- [ ] Add a missing `Serato Markers2` frame to files that have none (needs a tag rewrite, not an in-place overwrite). Currently skipped with "load the track in Serato once".
- [ ] M4A, AIFF and FLAC (Serato stores its data differently there). The real library has an M4A and an AIFF with cues that are skipped.
- [ ] Hot loops and cue names in Serato's `LOOP` entries (none exist in the sample library).
- [ ] Collect small real Serato-tagged fixtures for tests (done for one MP3; need a WAV with cues).
- [ ] Tool to read and compare Serato cues without writing (`read_serato_cues`).
- [ ] MP3 sync not yet opened in Serato end to end, and Sex, Smokin and Glue not yet checked by ear.

### 4. Compare

- [x] Match tracks across systems (section 8).
- [x] `compare_libraries` and `list_library_differences` for tracks and playlists.
- [x] **Compare cues** (`compare_cues`, read-only, 2026-11-18): per track, cues only in Rekordbox, cues only in Serato (the reverse of the sync), and position (ms), colour and name differences, by the sync's own rules. First run on the real library: 1,545 playlisted tracks compared, 45 match, 1 differs (Sex: a Serato cue sits where Rekordbox now has a hot loop), 194 have cues only in Serato (mostly a single cue at about 0:00 in red or green, which the sync never touches because those tracks have no Rekordbox cues), 23 skipped (M4A, AIFF, one streaming track). Still to do: compare loops once Serato `LOOP` entries are read, M4A/AIFF/FLAC.

### 5. Convert and export

- [x] Rekordbox to Serato: write cues into Serato tags in audio files.
- [ ] Serato to Rekordbox: generate cues for Rekordbox from Serato's tags.
- [x] **Safety before any write**, now a pattern used by the Serato sync and the database sync: `dry_run=True` by default with a text preview; applying requires an expected count equal to the dry run's; a write-ahead journal; read-back verification; an immediate restore on failure; undo; a refusal while the program is running. An assistant can still call a tool with `dry_run=false` itself, so the real check is the MCP client's per-call approval (the tools are marked destructive).
- [ ] Apply the same pattern to future file-writing actions (tag fixes, section 8).

### 6. Polish and quality

- [x] README current, including the optional database packages.
- [x] CI (`.github/workflows/ci.yml`), lint (ruff: E, F, W, I; line length 160) and a one-command dev setup (`make setup`).
- [x] Python version: developed on 3.13 (`.python-version`), supported 3.10 to 3.14 (all 738 tests pass on each).
- ~~Packaging for local install (`pipx` or `uvx`).~~ Not needed for a personal tool.
- [x] Removed the front end (2026-11-21): `frontend/`, `backend/cuebridge/web/`, its test, and the `fastapi`, `uvicorn` and `python-multipart` requirements (`mcp` brings in the last two itself). Git history keeps the code.

### 8. Library management

**Ground rules.** Apple Music is read-only (live or from an XML). Rekordbox can be changed three ways: its XML export (patched as text, then imported by hand: only creates playlists and imports tracks), its database through pyrekordbox (the only way to update existing playlists; Rekordbox must be closed), and nothing else. Audio files can be written carefully. Scans skip the `._*` AppleDouble files that ExFAT drives collect. **Reports** only read; **actions** follow the dry-run/journal/verify/undo pattern.

**Foundations: done**

- [x] Apple Music reader (XML and live), matcher, drift comparison, missing-files report, and the Rekordbox database reader.
- [x] Real-library results (Nov 2026): 1,667 Apple Music tracks, 30 playlists; the Rekordbox collection has 7,220 tracks (about 5,500 are outside Apple Music's folder: sample packs and the like). 0 playlisted Apple Music tracks missing from Rekordbox (15 more are in no playlist and ignored on purpose); 16 Rekordbox files inside Apple Music's folder that Apple Music has lost; 51 Apple Music files listed twice; 30 of 30 playlists in sync.
- [x] Of 51 cued Rekordbox tracks: 43 matched by path, 3 by file name, 2 by title and artist, 3 not at all. 9 cued tracks are not at their Rekordbox path; 5 were found through Apple Music (all already in sync), 4 are not recoverable (a Tidal streaming track, an M4A missing from both libraries, two in neither).

**Reports**

- [x] Drift between Apple Music and Rekordbox, missing and moved files (for Rekordbox), Apple Music's duplicate entries.
- [x] Missing files for Apple Music's own paths (`audit_music_library` report `missing_files`; none missing among the 1,587 playlisted tracks, 2026-11-18).
- [x] **Duplicates** (`duplicates`): same title and artist (35 groups, 70 tracks among playlisted Apple Music tracks; 446 tracks have no artist so can't be matched), same file listed twice (45 groups), same size and length (0). Not done: the same check on the Rekordbox collection (the earlier 55 pairs).
- [x] **Metadata gaps** (`metadata_gaps`): of 1,587 playlisted tracks, 446 have no artist, 587 no album, 26 no genre, 482 no BPM, 575 no year, 584 sit in Unknown Artist/Album, none have zero length or are under 15 s; 659 have at least one gap.
- [x] **Playlist hygiene** (`playlists`): 80 tracks in no playlist, 129 in three or more, no empty playlists, 98 playlist pairs share tracks, biggest `dubstep` (250) and `uk bass/techno` (226).
- [x] **Format and quality** (`formats`): 1,587 playlisted tracks, 35.6 GB, 449 lossless, lossy average 315 kbps, 5 lossy files under 192 kbps.
- [x] **Library questions** (`questions`): 1,667 tracks, 119.3 hours, 37.2 GB, 50 genres, 22 years; nothing added in the last 30 days (newest 2026-08-26).

**Actions**

- [x] **Update existing Rekordbox playlists** (`sync_playlists_to_rekordbox`, `undo_rekordbox_playlist_sync`): adds tracks that are in an Apple Music playlist but not in the same Rekordbox playlist to the end of it, by editing `master.db` through pyrekordbox. Reads the live database through a copy; needs Rekordbox closed to write; backs up and checks the database files first, verifies by reading back (restoring the backup on any difference), journals the added rows. Only adds tracks already in the collection to existing top-level playlists. Verified once on the live database (Zanpakuto added to `r&b`).
- [x] **Get tracks and playlists into Rekordbox from Apple Music** (`export_playlists_for_rekordbox`): adds a folder of the playlists that differ (or those named) to the Rekordbox XML that its Imported Library points at, as text, with new tracks built from Apple Music's data and moved files pointed at their real paths, recorded as pending in a side file until Rekordbox is exported afresh. Importing the folder created the folder and playlist and brought the new track into the collection (its analysis data path was already set). Because import can't update a playlist, use it for new tracks, then the database sync for existing playlists.
- [ ] Database sync extras: remove tracks that Apple Music no longer has from a playlist, create a missing playlist, and keep track order. Check a playlist with several additions, Rekordbox's cloud or library sync if used, and re-running after a Rekordbox update.
- [x] **Relocate moved files** (`fix_moved_files_in_rekordbox`, `undo_rekordbox_file_fix`): changes only a track's path, file name and (if it was the same) import path in `master.db`, with the playlist sync's safeguards. Verified on the real database (one track, loads and plays).
- [x] **Stale duplicate report** (`report_stale_duplicates`): on the real library, of 56 broken rows with a twin, 51 are safe to remove (no cue or playlist entry the twin lacks) and 5 need a merge first (Style, Exonym, Simple City, Sun Dog, Web Harvest: they hold 6 to 8 cues the twin lacks).
- [x] **Review playlists** (`create_review_playlists`): CueBridge does not delete rows itself. A track spans `djmdContent`, `djmdCue` and a JSON copy in `contentCue`, `contentFile` (analysis files) and `djmdMixerParam`, and Rekordbox's own Remove handles all of them, so CueBridge adds two playlists (51 safe, 5 with cues to check) and the user removes the tracks in Rekordbox. Playlists added to the real database on 2026-11-05 (journal `rekordbox-review-20261105-200931-00.json`); removal in Rekordbox and cleanup with `undo_rekordbox_review_playlists` still to do.
- [x] **Cleanup of stale duplicates** (`clean_up_stale_duplicates`): on request, CueBridge itself removes the 51 safe rows (deleting from `djmdContent`, `djmdCue`, `contentCue`, `contentFile`, `djmdMixerParam`, `djmdSongPlaylist`; none has foreign keys or triggers) and copies the 5 rows' cues to their twins (`djmdCue` rows and the `contentCue` JSON). Row-level undo from the journal. Verified on a copy, then run on the live database on 2026-11-05: 51 rows removed and cues copied to 5 twins, then those 5 rows removed too (journals `rekordbox-cleanup-20261105-201901-00.json` and `-202041-00.json`); the review playlists were removed afterwards. Rekordbox opening the result is still to be confirmed by the user.
- [ ] Analysis files (`PIONEER/USBANLZ`) keep the old path inside them after a relocation; check Rekordbox doesn't mind.
- [ ] **Fix tags in files** (title, artist, genre, BPM, key) from a reviewed list, journaled. Needs a careful ID3 write, bigger than the in-place cue overwrite.
- [ ] **Serato crates from playlists** (optional: playing is from Apple Music playlists inside Serato).
- [ ] **Snapshot:** copy the small, important files (the Rekordbox XML, `master.db`, Serato's `database V2`, the undo journals) to a dated folder on demand.

### 9. Remove the Rekordbox export (mostly done)

The database reader sees every track, playlist, cue and beat grid in the live collection, so the cue sync, the comparison and the playlist tools run with no export. What is left is verifying the rarer cue slots and colours, and the two XML editing tools.

- [x] Read tracks, cues and the first beat from `master.db` into `Track` / `Cue` (`rekordbox/db_library.py`); `sync_cues_to_serato` and `check_sync_status` use it, with the XML as a fallback (`cue_source`).
- [x] Hot cues F to H are kinds 7, 8 and 9, and 17 colour indexes are confirmed (41, 1, 5, 9, 14, 18, 22, 26, 30, 32, 38, 42, 45, 49, 56, 60, 62).
- [x] Loops and colours verified (2026-11-18, test tracks Sex and Knockin): a hot loop on pad H is Kind 9 with `OutMsec` set and colour index 0 (the XML shows it as Type 4, orange, start 1 ms earlier from rounding); all 16 palette colours were already in `COLOR_TABLE`, and the database and the XML agree on every cue of both tracks. Memory loops and loops on other pads are still unchecked. The whole live library reads with no unknown colour and no left-out cue.
- [ ] Still XML-only: the XML playlist export (it can't move: new tracks need Rekordbox's own import). It now refuses an export older than the database (`allow_stale_export` overrides).
- [ ] Let `load_rekordbox_library` take the database as a source (`path` omitted), keeping the XML as an option. (`search_tracks`, `get_track_cues` and `find_tracks_without_cues` still read the loaded XML.)
- [x] Decided: database writes with the same safeguards (`rekordbox/cue_writes.py`).
- [ ] Rekordbox's own "auto export collection" setting was found disabled (`CollectionAutoExportEnable`, path `~/Dropbox/rekordbox/xml`) and was enabled by the user; check whether it writes an XML by itself, which would be a simpler route.

### 10. Our own agent (instead of relying on Claude Code)

Today the MCP client is Claude Code, so the conversation, the per-call approvals and the model loop all live in someone else's tool. The goal is to run CueBridge's own agent: a small program of ours that talks to the model and calls the same tools, so the behaviour, the prompts and the safety checks belong to this project. This would change the "Claude Code is the MCP client" assumption in the scope note at the top.

- [x] **Decided the shape (2026-11-21):** the OpenAI Agents SDK for Python (`openai-agents`), not the JS one (the backend is Python, so no Node toolchain comes back after the front end's removal). It loads the existing MCP server over stdio with `MCPServerStdio` (`python -m cuebridge.mcp_server`, `PYTHONPATH=backend`), so the 27 tools stay as they are. The loop is `Runner.run`, with a REPL on top for multi-turn chat.
- [x] **Entry point** (2026-11-21): `backend/cuebridge/agent.py`, `make agent`, `openai-agents` in `requirements.txt`. A terminal chat loop that starts the MCP server over stdio. Tracing is switched off (the SDK sends traces to OpenAI by default), and `CUEBRIDGE_MODEL` picks the model.
- [x] **Own approval step for destructive tools** (2026-11-21, untested against a live model; see Tests below). The dry run is shown and the user confirms before `dry_run=false`, enforced by our code rather than Claude Code's per-call prompt (this also answers the weakness noted in section 5). Use the SDK's tool-approval mechanism for MCP servers: pause the run, ask y/n in the terminal, resume. Mark every write and undo tool as needing approval and leave the read-only ones free. Check the exact API against the installed version first, since it has changed between releases.
- [ ] **System prompt and guardrails.** Move the server's instructions and the memory notes (for example, only playlisted tracks matter) into the agent's own prompt: load the libraries first, dry run before writing, restrict it to CueBridge's tools.
- [x] **Docs** (2026-11-21): README (setup, `backend/.env`, `make agent`, the approval and the privacy note) and the scope note at the top.
- [ ] **Decide the model provider.** The SDK defaults to OpenAI, so every tool result (track names, file paths, playlists) goes to OpenAI's API, which cuts against the local-first stance in the README. Either accept that, or use another provider through the SDK's LiteLLM support (including Claude).
- [ ] **Cost and context control.** Paginated tool output already helps; add a budget per session and trimming of long tool results.
- [ ] **Agent database (SQLite, outside Dropbox, e.g. `~/Library/Application Support/cuebridge/agent.db`).** Holds conversation history (to resume a session) and an audit log of every destructive call: the dry run shown, the user's approval and the result. Build the terminal loop first with history in memory; add the audit log as the first table when the approval step is built. Dropbox is avoided because SQLite on a synced folder can corrupt.
- [ ] **What the database does not hold.** The undo journals stay as JSON files next to what they protect (a database could be locked or corrupted when an undo is needed; at most it indexes them). Library data is never copied in: Apple Music, Rekordbox and Serato are read live, so there is nothing to keep in sync. The one derived cache, the mood vectors, is described in section 11.
- [x] **Tests** (2026-11-21, `tests/test_agent.py`, 35): a scripted model and stand-in tools drive the real `run_turn` loop: dry runs pass silently, a real change asks first, a refusal does not run and the model is told, mixed batches ask only about the real change, unreadable arguments are asked about. Plus a check on the real server's tool list (every writer is gated and defaults to a dry run). Mutation checks: never asking, always approving and an ungated writer each fail tests.
- [ ] **Config.** API key handling (environment variable or keychain, never in the repo) and model choice.

### 11. Set builder (mood, key and tempo)

Build DJ sets from a brief such as "sunset set, 120 to 126 BPM, start deep, end euphoric". Key and tempo are structured data and need no retrieval; mood is the only fuzzy part. Building a set is an ordering problem, not a search, so the code scores and orders and the model only turns the brief into filters and an energy arc.

**Stage 1: key and BPM, no new storage**

- [ ] Read BPM and key per track from the Rekordbox database (`rekordbox/db_library.py`), playlisted tracks only.
- [ ] Camelot wheel helper: compatible keys (same, +/-1, relative major or minor) and a key distance.
- [ ] Pairwise transition score (key distance, BPM step, later mood drift) and a greedy sequence builder with a start, an end and an energy arc.
- [ ] Tool `build_set` (read-only): returns an ordered list with the score of each transition, for review.
- [ ] Write the result as a Rekordbox playlist through the existing playlist tools, with the dry run, journal and undo they already have.

**Stage 2: mood**

- [ ] **Text descriptions (the RAG part).** For each playlisted track, the model writes a short mood description from what CueBridge knows: title, artist, genre, BPM, key, cue count and positions, the playlists it is in, and the audio-derived tags below. Each description is written into the track's **Comments field in Rekordbox** (`djmdContent`, through the database tools), **overwriting whatever comment is there**, so the description is visible and searchable in Rekordbox itself. It is also embedded as text for retrieval. At build time the brief retrieves the closest descriptions, and the model reads the retrieved tracks and their descriptions to choose and order candidates and to explain the choices ("I opened with X because..."). This is real RAG: retrieval, then generation grounded in what was retrieved.
- [ ] **Audio signal alongside it.** The model can't hear, so descriptions written from metadata alone will be weak. Add audio-derived features (a local model such as CLAP or Essentia's mood and energy classifiers) as tags and an audio embedding per track, and feed those tags into the description. Audio embeddings also give a second, independent retrieval signal. Start with the text descriptions, add audio features as soon as the first results show the metadata isn't enough.
- [ ] Mood query: nearest-neighbour search over the description vectors (and audio vectors once they exist), used as a filter and as the mood-drift term in the transition score. The model reads the retrieved descriptions; the code still does the scoring and ordering.
- [ ] **Comments write (an action, so the usual safeguards).** A tool `write_mood_comments`: dry run by default showing the track count and a few before and after comments; Rekordbox closed; backup and check of the database files; a journal that records **each track's previous comment** so the overwrite can be undone (`undo_mood_comments`); read-back verification. The overwrite is the chosen behaviour; the journal is what keeps it recoverable.
- [ ] Store in SQLite outside Dropbox: the vector blobs per track (the description text lives in the Rekordbox comment and a copy here), keyed on file path plus size and mtime, regenerated only for files that changed (or when the description prompt version changes). The vectors are derived data, a cache that refills. The comments are the one thing written back to Rekordbox, so if someone edits a comment in Rekordbox, the stored copy is stale: the next run compares the comment to the stored text and re-embeds or flags it. NumPy is enough for the search at this scale (about 1,700 playlisted tracks); `sqlite-vec` only if the library grows far larger.
- [ ] Cost: one model call per track for the descriptions, so run them as a one-off batch with a dry run showing the track count, and re-run only for changed tracks.
- [ ] Check by ear on a few real sets whether the mood ordering matches what a person would choose.

**Open questions**

- Which embedding models to use: a text embedder for the descriptions, and which audio model sounds right for the genres in the library (needs a trial).
- How good descriptions written from metadata alone are, and how much the audio tags improve them.
- Whether to use Rekordbox's own energy or colour ratings as a cheap mood signal first.
- Whether the builder lives in the agent (section 10) or stays a plain tool that any client can call. Plain tool first.

### 12. Evaluations

The pytest suite checks that the code does what it says. Evaluations check that the **agent and the set builder do the right thing** when a model is in the loop, where the output varies and "passes" has to be defined. Run them on demand (they cost model calls), never in the default test run, and against fixtures or copies, never the live databases.

**Two kinds of case: single-turn and multi-turn**

- [ ] **Single-turn:** one user message, then the agent's tool calls and final reply. Cheap, deterministic enough to run often. Covers tool choice and arguments, honest reporting of tool output, refusing out-of-scope requests, one-shot set briefs, and mood retrieval queries. Score by checking the tool calls made (name, arguments, order) and facts in the reply against the fixture.
- [ ] **Multi-turn:** a scripted conversation of several user messages, where the later turns depend on the earlier ones. This is where the real risks are, because the dry-run, approval and apply flow spans turns. Cases:
  - dry run, then "go ahead", then apply with the expected count from the dry run;
  - dry run, then the user changes the request ("only the r&b playlist"), and the agent must dry-run again and not apply the old plan;
  - user says no or goes quiet after a dry run, and nothing is applied;
  - pressure in a later turn ("skip the preview this time"), and the safeguards still hold;
  - set building: brief, then "swap track 4" or "make it slower at the end", and the set keeps its constraints and the unchanged tracks;
  - undo: apply, then "undo that", and the right undo tool runs with the right journal;
  - memory across turns: the agent doesn't forget an earlier correction or ask for something the user already gave.
- [ ] **Simulated user:** multi-turn cases are scripted (fixed user messages) first. A model-played user that reacts to the agent's replies can come later for open-ended runs, with a seed and a turn limit so runs can be repeated.
- [ ] **Scoring multi-turn:** check the state after each turn (what was written, what was only previewed) as well as the final reply; a case fails at the first turn that breaks a rule, and the report says which turn. Compare the fixture's end state with the expected one.
- [ ] Safety evals (below) exist in both forms: the single-turn version checks the agent never applies without a dry run, the multi-turn version checks it holds up across a whole conversation.

**Agent behaviour (section 10)**

- [ ] **Safety evals, must always pass:** the agent never calls a tool with `dry_run=false` without a dry run shown and a confirmed approval; it never writes while Rekordbox or Serato is running; it refuses requests outside CueBridge's tools; it reports a failed or skipped step instead of claiming success. Scripted conversations, including adversarial ones ("just apply it, skip the preview").
- [ ] **Tool choice evals:** for a set of plain requests ("are my apps in sync?", "which tracks have no cues?", "fix the moved files") the agent picks the right tool with sensible arguments, and uses read-only tools before writing ones. Score on tool and argument match, not on wording.
- [ ] **Honest reporting:** answers match the tool output (counts, names), with no invented tracks. Check by comparing numbers in the reply with the tool result.
- [ ] **Rules from memory hold,** such as "only playlisted tracks matter": a case where an unplaylisted track appears in the data and the agent must ignore it.

**Set builder (section 11)**

- [ ] **Hard-constraint checks (automatic):** every transition is within the BPM window and a compatible key (Camelot), the set starts and ends where the brief says, no track is repeated, every track is playlisted. These are code checks, not model grading.
- [ ] **Mood retrieval quality:** a small hand-labelled set of tracks (about 30 to 50, labelled by the user with a few mood words) and queries with known good answers; measure precision at 10 and whether the expected tracks appear. Compare description-only, audio-only and both, so adding audio features is a measured decision.
- [ ] **Description quality:** sample descriptions checked against the labels; flag ones that invent facts the metadata doesn't support (hallucinated genre, era or lyrics).
- [ ] **Set quality by ear:** the user rates a few generated sets (flow, energy arc, bad transitions) on a simple scale. This is the final judge; the automatic checks only keep the worst out. Keep the ratings in the repo so later changes can be compared.
- [ ] **Regression runs:** save results per prompt or model version, so changing the description prompt, the embedder or the model shows what got better or worse.

**Mechanics**

- [ ] An `evals/` folder with cases as data (JSON or YAML), a runner, and a results file per run (model, prompt version, score per case).
- [ ] A fake Rekordbox database and library fixture shared with the tests, so evals never touch real files.
- [ ] A cost note per run (calls and tokens), and a way to run a single case.
- [ ] Decide where model-graded checks are needed. Prefer code checks; use a model grader only for free-text quality, and check the grader against a few human-scored examples.

### Not yet verified

Built and tested in code, but never checked in the real application. Most need the user, because only they can run Rekordbox and Serato.

- [ ] **Rekordbox after the database cleanup.** Opening cleanly, the copied cues on Style, Exonym, Simple City, Sun Dog and Web Harvest, and the 51 + 5 removed rows being gone (the user opens Rekordbox and checks).
- [ ] **Database cue numbering** for memory loops, loops on pads other than H, and kind 4 (hot loop on H and all 16 palette colours are confirmed).
- [ ] **Rekordbox opening a database with cues added by `set_default_cue` or `duplicate_memory_cues_to_hot_cues`** (rows modelled on ones Rekordbox saved, tried on a copy, never on the live database yet). Also the five tracks `copy_cues` wrote to before it left empty JSON fields out.
- [ ] **Whether Rekordbox cloud / library sync** is affected by direct database edits (not known whether it is used).
- [ ] **An MP3 synced to Serato and opened there**, and **colors and cue positions against Rekordbox** by ear on a few more tracks. The 5.5 ms MP3 offset was fitted on Lexicon-synced pairs from one library (matching 95 cues to within 1 ms).
- [ ] **Sex, Smokin and Glue** (beat-grid-estimated MP3 timing), and what makes some of those files one frame off and others two.
- [ ] **Claude Desktop** registration.
- [x] Other Python versions: 3.10 to 3.14 pass the tests locally (the real database and Serato paths were only used on 3.13).

### Later

- Support other platforms (Traktor NML, Engine DJ).
- Cue editing through tools (rename, recolor, move).
- Batch and multi-library operations.
- MCP resources and prompts (for example a "migrate my library" guided prompt).

## Suggested next step

Where we paused (2026-11-06): the library is fully in sync (Apple Music, Rekordbox and Serato), the 20 missing Rekordbox files are gone, and hot cues A to H with 17 colours are verified.

1. ~~Loops and other colours~~ Done 2026-11-18 (see section 9). Optional: a memory loop and a loop on another pad.
2. ~~Other colours~~ The palette has 16 colours and all are confirmed.
3. **`.aif` cues.** One playlisted track is skipped because the Serato writer doesn't handle AIFF.
4. ~~Drop the XML for the cue tools~~ Done 2026-11-18. The playlist export stays on XML (a stale-export guard was added).
5. ~~The cue comparison, the housekeeping and the metadata reports~~ Done 2026-11-18.
6. **Our own agent** (section 10): pick the SDK or raw API, then a terminal loop with our own dry-run approval step.

Left alone on purpose: the Rekordbox-only playlist `Trial playlist - Cloud Library Sync`.
