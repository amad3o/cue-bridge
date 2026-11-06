# CueBridge Roadmap

CueBridge is an **MCP server** that lets an AI assistant such as Claude read, compare and convert DJ cue points between Rekordbox and Serato, and help manage the music library, by calling tools against files on the user's own machine.

The parsing, matching and conversion logic stays in plain Python. The MCP server is a thin layer over it.

**Audience and scope.** CueBridge is a personal tool, built around one library: Apple Music for tracks and playlists, Rekordbox for cues and USB sets, Serato for playing, with the music on an ExFAT drive. Claude Code is the MCP client. There are no plans for a command line, a standalone UI, or packaging for other people unless that changes. A single user means the tools can assume this setup, but they should stay safe, because they touch real files: dry run first, a journal, verification, and undo.

**Text first, UI later.** Every tool returns a clear text summary the assistant can show in chat, such as "12 tracks change, 31 cues added, 2 conflicts", with a short diff of the first few items. The React UI is deferred (section 7).

## Where things stand

The loop that was the goal now works end to end, with one remaining export:

1. Tracks and playlists are managed in **Apple Music**, which CueBridge reads **live** (no export).
2. Cues are set in **Rekordbox**. CueBridge reads its **XML export** for cues (the one remaining manual export, see section 9).
3. `sync_cues_to_serato` copies Rekordbox's cues into the **Serato** tags of the audio files (both cue frames, with timing correction).
4. `sync_playlists_to_rekordbox` adds new tracks to **existing Rekordbox playlists** by editing Rekordbox's database (Rekordbox closed, backup, verification, undo). A brand-new track must first be imported into the Rekordbox collection (the XML route, section 8).

## Completed

**MCP server** (`cuebridge/mcp_server.py`, stdio, `mcp` 2.x; registered in Claude Code). 18 tools, all returning compact, paginated output. The tools that write are annotated as destructive, and the server's instructions tell the assistant to show a dry run and get the user's go-ahead first.

| Area | Tools |
|---|---|
| Rekordbox XML, read | `load_rekordbox_library`, `search_tracks`, `get_track_cues`, `find_tracks_without_cues` |
| Rekordbox XML, write | `set_default_cue`, `duplicate_memory_cues_to_hot_cues` (see known issue 2) |
| Serato | `sync_cues_to_serato`, `undo_serato_sync` |
| Apple Music | `load_apple_music_library` (live from the Music app, or an exported XML), `get_playlist_tracks` |
| Compare | `compare_libraries`, `list_library_differences`, `find_missing_files` |
| Playlists | `export_playlists_for_rekordbox` (XML, for getting new tracks in), `sync_playlists_to_rekordbox`, `undo_rekordbox_playlist_sync` (database) |
| Broken paths | `fix_moved_files_in_rekordbox`, `undo_rekordbox_file_fix` (database) |

**Core (Python)**

- Rekordbox parser: unique cue IDs, hot, memory and loop types, loop ends, colors, decoded paths, first beat, duration, and the playlist tree. Missing or blank titles, artists and cue names are `None`.
- Serato: a `Markers2` codec and the legacy `Markers_` frame codec (all 1,541 real tags round-trip byte for byte; 1,040 real files with both frames agreeing are left unchanged by the encoder), a tag reader and locator for MP3 and WAV, and the sync with a write-ahead journal, read-back verification, undo, and a refusal while Serato DJ Pro runs.
- Timing between the programs: WAV 0 ms; MP3 with a LAME header or no Xing/Info frame -5.5 ms; MP3 with an Xing/Info frame but no LAME tag estimated from the two beat grids as whole MP3 frames (checked by ear on one file).
- Apple Music: XML reader, and a live reader that runs a read-only JXA script through `osascript` (the "Share iTunes Library XML" setting doesn't exist in macOS 15's Music app).
- A track matcher (path, file name, then title and artist with duration; never guesses between equal candidates), the Rekordbox/Apple Music drift comparison, and a missing-files report. Only playlisted Apple Music tracks matter (tracks in no playlist are counted and ignored).
- Rekordbox's own database (`master.db`, found from Rekordbox's settings; on this setup it is on the music drive) through pyrekordbox and SQLCipher, an optional dependency.
- The Rekordbox XML is edited as text (new lines only, original bytes untouched) for the playlist export, because Rekordbox rejects an XML library's rewrite of its file.
- Pydantic models, root `.gitignore`, README rewritten around the MCP server.
- 607 pytest tests: parsers, services, the Serato codecs and sync on mutagen-built files, the XML patcher, the database logic against a fake database (with mutation checks on the safeguards), and the tools, directly and through an in-memory MCP client.

**Web UI (prototype, on hold)**

- FastAPI `POST /upload/rekordbox` with validation, and a React upload screen with a sortable, searchable `TrackTable` plus `CueCard` / `CueComparison` components.

## Known issues

1. **Housekeeping.** `requirements.txt` doesn't pin a Python version. There is no CI or lint.
2. ~~The two Rekordbox XML edit tools re-serialised the XML.~~ Fixed: they now insert the new cue lines as text and keep every other byte (no marker comment). Every in-place edit now makes a backup, and a copy is never saved over an existing file. Rekordbox accepting an edited export is still unverified (see "Not yet verified").
3. **Rekordbox tracks pointing at renamed files.** The live database has 77 missing files (7,220 tracks). Read through the live Apple Music library: only 1 can be repointed (`fix_moved_files_in_rekordbox` did that, Floor (Rowka Remix), and it plays in Rekordbox; a first run repointed 5, but 4 turned out to be duplicates that differed only in case or accents, which the check now catches and the run was undone); 56 already have a second Rekordbox row for the real file (stale duplicates, which may hold cues or playlist entries, so they need a decision, not a repoint); 20 have no matching file in Apple Music.
4. **A Rekordbox export is still needed for cues and for the comparison with Rekordbox's collection** (section 9).
5. **Rekordbox can only create a playlist from an XML import, never update one.** That is why updating an existing playlist goes through the database. An XML import of `r&b` made a second `r&b` in a new "Apple Music" folder.
6. **UI bugs to fix if the UI is revived (section 7).** The arrow-key listener is global (it fires while typing in the search box), row keys aren't stable, sort direction never flips, selection isn't reset when the filter changes, and the API URL is hardcoded to `localhost:8000`.

## To do

### 1. Core layout

Done. The package is `cuebridge` (a generic name like `app` can collide with other packages; avoid naming a package `mcp`, which would shadow the SDK):

```
backend/cuebridge/
  models.py              shared Pydantic models (Track, Cue)
  mcp_server.py          the MCP server: tools and their instructions
  parsers/               file formats to models
    paths.py, rekordbox_parser.py, apple_music_xml.py,
    serato_markers.py (both codecs), serato_tags.py (read, locate, beat grid)
  services/              path-based logic
    rekordbox_service.py (validate, cache, pending-playlist note), library_queries.py,
    rekordbox_editor.py (XML edits), cue_rules.py, cue_timing.py,
    serato_sync.py, apple_music_service.py, apple_music_live.py,
    track_matcher.py, drift.py, library_reports.py,
    playlist_export.py (XML patcher), rekordbox_db.py (database)
  web/main.py            FastAPI app for the prototype UI
```

### 2. Rekordbox XML tools

- [x] Read tools, with a library cache keyed on path, mtime and size.
- [x] `set_default_cue` and `duplicate_memory_cues_to_hot_cues`: dry run by default, a verified backup of a fresh export, atomic writes, refusal if the file changed since planning. (On the real library the second adds 129 hot cues to 47 tracks, at most 5 per track.)
- [x] **Moved both to text patching** (`patch_marks`), marker comment dropped, side-file note kept in step with the file.
- [ ] Optional filters for `set_default_cue`, such as skipping very short tracks. A real export includes Pioneer sampler one-shots (`NOISE`, `SINEWAVE`), which get a cue too.
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
- [ ] **Compare cues**: tracks with cues in one program and not the other, Serato cues Rekordbox lacks (the reverse of the sync), counts and colors.

### 5. Convert and export

- [x] Rekordbox to Serato: write cues into Serato tags in audio files.
- [ ] Serato to Rekordbox: generate cues for Rekordbox from Serato's tags.
- [x] **Safety before any write**, now a pattern used by the Serato sync and the database sync: `dry_run=True` by default with a text preview; applying requires an expected count equal to the dry run's; a write-ahead journal; read-back verification; an immediate restore on failure; undo; a refusal while the program is running. An assistant can still call a tool with `dry_run=false` itself, so the real check is the MCP client's per-call approval (the tools are marked destructive).
- [ ] Apply the same pattern to future file-writing actions (tag fixes, section 8).

### 6. Polish and quality

- [x] README current, including the optional database packages.
- [ ] Add CI and lint, and a one-command dev setup.
- [ ] Pin a Python version (tested on 3.13 only; dependencies declare 3.10 or newer).
- ~~Packaging for local install (`pipx` or `uvx`).~~ Not needed for a personal tool.

### 7. Optional review UI (lowest priority)

Only worth building if the text summaries in chat turn out not to be enough. Point the FastAPI endpoints at the service layer; generate TypeScript types from the OpenAPI schema; session-based review views and a preview-and-apply screen (a stronger confirmation than the assistant's, since the click doesn't pass through it); an `open_review_ui` tool; fix the UI bugs above; bind to `127.0.0.1` with a one-time token.

### 8. Library management

**Ground rules.** Apple Music is read-only (live or from an XML). Rekordbox can be changed three ways: its XML export (patched as text, then imported by hand: only creates playlists and imports tracks), its database through pyrekordbox (the only way to update existing playlists; Rekordbox must be closed), and nothing else. Audio files can be written carefully. Scans skip the `._*` AppleDouble files that ExFAT drives collect. **Reports** only read; **actions** follow the dry-run/journal/verify/undo pattern.

**Foundations: done**

- [x] Apple Music reader (XML and live), matcher, drift comparison, missing-files report, and the Rekordbox database reader.
- [x] Real-library results (Nov 2026): 1,667 Apple Music tracks, 30 playlists; the Rekordbox collection has 7,220 tracks (about 5,500 are outside Apple Music's folder: sample packs and the like). 0 playlisted Apple Music tracks missing from Rekordbox (15 more are in no playlist and ignored on purpose); 16 Rekordbox files inside Apple Music's folder that Apple Music has lost; 51 Apple Music files listed twice; 30 of 30 playlists in sync.
- [x] Of 51 cued Rekordbox tracks: 43 matched by path, 3 by file name, 2 by title and artist, 3 not at all. 9 cued tracks are not at their Rekordbox path; 5 were found through Apple Music (all already in sync), 4 are not recoverable (a Tidal streaming track, an M4A missing from both libraries, two in neither).

**Reports**

- [x] Drift between Apple Music and Rekordbox, missing and moved files (for Rekordbox), Apple Music's duplicate entries.
- [ ] Missing files for Apple Music's own paths.
- [ ] **Duplicates:** the same title and artist (55 pairs in the Rekordbox collection), the same file in several folders, near-duplicates by duration and size.
- [ ] **Metadata gaps:** no artist (463 of 1,667 in Apple Music), no BPM (500), zero length, "Unknown Artist / Unknown Album" folders, sample one-shots mixed into the library.
- [ ] **Playlist hygiene:** tracks in no playlist, tracks in many, empty playlists, overlap between genre playlists, the biggest playlists.
- [ ] **Format and quality:** counts by format, bit rate and sample rate; low-bit-rate MP3s; very large files.
- [ ] **Library questions:** what was added recently, tracks per genre or year, the library's size.

**Actions**

- [x] **Update existing Rekordbox playlists** (`sync_playlists_to_rekordbox`, `undo_rekordbox_playlist_sync`): adds tracks that are in an Apple Music playlist but not in the same Rekordbox playlist to the end of it, by editing `master.db` through pyrekordbox. Reads the live database through a copy; needs Rekordbox closed to write; backs up and checks the database files first, verifies by reading back (restoring the backup on any difference), journals the added rows. Only adds tracks already in the collection to existing top-level playlists. Verified once on the live database (Zanpakuto added to `r&b`).
- [x] **Get tracks and playlists into Rekordbox from Apple Music** (`export_playlists_for_rekordbox`): adds a folder of the playlists that differ (or those named) to the Rekordbox XML that its Imported Library points at, as text, with new tracks built from Apple Music's data and moved files pointed at their real paths, recorded as pending in a side file until Rekordbox is exported afresh. Importing the folder created the folder and playlist and brought the new track into the collection (its analysis data path was already set). Because import can't update a playlist, use it for new tracks, then the database sync for existing playlists.
- [ ] Database sync extras: remove tracks that Apple Music no longer has from a playlist, create a missing playlist, and keep track order. Check a playlist with several additions, Rekordbox's cloud or library sync if used, and re-running after a Rekordbox update.
- [x] **Relocate moved files** (`fix_moved_files_in_rekordbox`, `undo_rekordbox_file_fix`): changes only a track's path, file name and (if it was the same) import path in `master.db`, with the playlist sync's safeguards. Verified on the real database (one track, loads and plays).
- [ ] **Stale duplicate rows** (56 on the real library): decide how to merge cues and playlist entries from a broken row into the good one, then remove the broken row.
- [ ] Analysis files (`PIONEER/USBANLZ`) keep the old path inside them after a relocation; check Rekordbox doesn't mind.
- [ ] **Fix tags in files** (title, artist, genre, BPM, key) from a reviewed list, journaled. Needs a careful ID3 write, bigger than the in-place cue overwrite.
- [ ] **Serato crates from playlists** (optional: playing is from Apple Music playlists inside Serato).
- [ ] **Snapshot:** copy the small, important files (the Rekordbox XML, `master.db`, Serato's `database V2`, the undo journals) to a dated folder on demand.

### 9. Remove the Rekordbox export (next)

The database reader already sees every track and playlist in the live collection. Reading **cues** (and beat grids, for the timing estimate) from it too would let the cue sync, the comparison and the playlist tools all run with no export, so the loop needs no manual step.

- [ ] Read tracks, cues (hot, memory, loop, colors) and the first beat from `master.db` into the existing `Track` / `Cue` models, through a copy, as the playlist reader does.
- [ ] Let `load_rekordbox_library` take the database as a source (`path` omitted), keeping the XML as an option.
- [ ] Decide what replaces `set_default_cue` and `duplicate_memory_cues_to_hot_cues`: database writes with the same safeguards, or text-patched XML (known issue 2). Rekordbox must be closed for database writes.
- [ ] Rekordbox's own "auto export collection" setting was found disabled (`CollectionAutoExportEnable`, path `~/Dropbox/rekordbox/xml`) and was enabled by the user; check whether it writes an XML by itself, which would be a simpler route.

### Not yet verified

Built and tested in code, but never checked in the real application. Most need the user, because only they can run Rekordbox and Serato.

- [ ] **Rekordbox accepting a cue-edited XML** from `set_default_cue` and `duplicate_memory_cues_to_hot_cues` (now text-patched; known issue 2).
- [ ] **Whether Rekordbox cloud / library sync** is affected by direct database edits (not known whether it is used).
- [ ] **An MP3 synced to Serato and opened there**, and **colors and cue positions against Rekordbox** by ear on a few more tracks. The 5.5 ms MP3 offset was fitted on Lexicon-synced pairs from one library (matching 95 cues to within 1 ms).
- [ ] **Sex, Smokin and Glue** (beat-grid-estimated MP3 timing), and what makes some of those files one frame off and others two.
- [ ] **Claude Desktop** registration.
- [ ] **Other Python versions.**

### Later

- Support other platforms (Traktor NML, Engine DJ).
- Cue editing through tools (rename, recolor, move).
- Batch and multi-library operations.
- MCP resources and prompts (for example a "migrate my library" guided prompt).

## Suggested next step

Section 9: read cues from Rekordbox's database so nothing has to be exported, then move or replace the two XML edit tools. After that, the cue comparison (section 4) and the duplicates and metadata reports (section 8).
