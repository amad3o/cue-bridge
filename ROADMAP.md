# CueBridge Roadmap

CueBridge is moving from a web UI to an **MCP server**. The goal is for an AI assistant such as Claude to read, compare and convert DJ cue points between Rekordbox and Serato, and to help manage the music library, by calling tools against files on the user's own machine.

The parsing, matching and conversion logic stays in plain Python. The MCP server is a thin layer over it.

**Audience and scope.** CueBridge is a personal tool, built around one library: Apple Music for tracks and playlists, Rekordbox for cues and USB sets, Serato for playing, with the music on an ExFAT drive. Claude Code is the MCP client. There are no plans for a command line, a standalone UI, or packaging for other people unless that changes. The scope is growing from syncing cues to **managing the music library as a whole** (section 8). A single user means the tools can assume this setup, but they should stay safe, because they touch real files: dry run first, a journal, verification, and undo.

**Text first, UI later.** Every tool returns a clear text summary the assistant can show in chat, such as "12 tracks change, 31 cues added, 2 conflicts", with a short diff of the first few items. The React UI is deferred. Once the text workflow is complete, an optional review page that the MCP server opens (section 7) can be added where the text summary falls short.

## Completed

**MCP server** (`cuebridge/mcp_server.py`, stdio, `mcp` 2.x; registered in Claude Code)

- Read tools with compact, paginated output: `load_rekordbox_library`, `search_tracks`, `get_track_cues`, `find_tracks_without_cues`.
- Rekordbox XML edit tools: `set_default_cue` and `duplicate_memory_cues_to_hot_cues`. Dry run by default, a verified backup of a fresh export, atomic writes, refusal if the file changed since planning.
- Serato tools: `sync_cues_to_serato` and `undo_serato_sync`. Rekordbox wins, but only what differs is changed. In-place overwrite of Serato's 470-byte cue tag, a write-ahead journal, read-back verification, undo, and a refusal while Serato DJ Pro is running.
- Tools that write are annotated as destructive, and the server's instructions tell the assistant to show a dry run and get the user's go-ahead first.

**Core (Python)**

- Rekordbox parser: unique cue IDs, hot, memory and loop types, loop ends, colors, decoded file paths. Missing or blank titles, artists and cue names are `None`.
- Service layer that takes file paths: validation, domain errors, an mtime-based library cache, queries, the Rekordbox XML editor, the memory-to-hot slot rules (`cue_rules.py`) and the Rekordbox-to-Serato timing offsets (`cue_timing.py`).
- Serato `Markers2` codec and a tag reader/locator for MP3 and WAV. All 1,541 tags in the real library decode, re-encode byte for byte, and are located correctly.
- Pydantic models, `RekordboxParseError`, root `.gitignore`, README rewritten around the MCP server.
- 400 pytest tests: parsers, fixtures, services, the editor, the Serato codec and sync (on mutagen-built MP3 and WAV files), and the tools, both directly and through an in-memory MCP client.

**Web UI (prototype, on hold)**

- FastAPI `POST /upload/rekordbox` with validation, and a React upload screen with a sortable, searchable `TrackTable` plus `CueCard` / `CueComparison` components.

## Known issues

1. ~~Dead code from the first prototype.~~ Removed: the Serato stub, its endpoint and the React button.
2. ~~Missing names default to the string "Unknown".~~ Fixed: they are `None`.
3. ~~Large libraries need a plan for AI output.~~ Done: tools are paginated and capped.
4. ~~No tests for Serato.~~ Done. A real WAV with cues would still be a useful extra fixture.
5. ~~README is out of date.~~ Fixed.
6. **Housekeeping.** `requirements.txt` doesn't pin a Python version. There is no CI or lint.
7. **Matching by path breaks when files move.** 6 of the 8 cued tracks whose Rekordbox paths no longer exist were found elsewhere by title and artist. The Serato sync skips such tracks today ("the file wasn't found"); the matcher in section 8 fixes that.
8. **UI bugs to fix if the UI is revived (section 7).**
   - The arrow-key listener is global, so it fires while typing in the search box.
   - Row keys aren't stable, sort direction never flips, and selection isn't reset when the filter changes.
   - The API URL is hardcoded to `localhost:8000`.

## To do

### 1. Restructure the core for reuse

- [x] Move logic out of `main.py` into a service layer that takes **file paths** (not uploads) and returns models. The HTTP endpoints and the MCP server both call it.
- [x] Move shared models out of the web layer, and decide on a package layout. The `app` package is now `cuebridge`, since a generic top-level name like `app` can collide with other packages:
  ```
  backend/cuebridge/
    models.py            shared Pydantic models (Track, Cue)
    mcp_server.py        the MCP server: tools and their instructions
    parsers/             file formats to models
      rekordbox_parser.py, serato_markers.py (codec), serato_tags.py (read, locate)
    services/            path-based logic
      rekordbox_service.py (validate, cache), library_queries.py, rekordbox_editor.py,
      cue_rules.py, cue_timing.py, serato_sync.py (plan, apply, undo)
    web/main.py          FastAPI app for the prototype UI
  ```
  (Avoid naming a package `mcp`, which would shadow the SDK.)
- [x] Make "unknown" explicit: use `None` for missing title or artist, not the string `"Unknown"`.
- [x] Add a library cache so a large XML is parsed once per session, not on every tool call. `get_rekordbox_library(path)` keys on the resolved path, re-parses when the file's mtime or size changes, and keeps the last 4 libraries.

### 2. MCP server and Rekordbox tools

- [x] Add the official Python MCP SDK (`mcp` 2.x, where `FastMCP` is now `MCPServer`) and a `cuebridge/mcp_server.py` entry point using stdio: `python -m cuebridge.mcp_server`.
- [x] Tools, all with compact, paginated output:
  - `load_rekordbox_library(path)`: parses and caches, returns a summary (track count, cue totals by type).
  - `search_tracks(query, has_cues, limit, offset)`: filter and page through tracks.
  - `get_track_cues(track_id)`: full cue detail for one track.
  - `find_tracks_without_cues()` and basic library stats.
  - `set_default_cue(output_path, dry_run=True)`: adds a hot cue A at 0:00 to every track with no cue or loop, in the loaded file (see below). It re-parses its own output to check that only the intended tracks changed. This was the first write tool. It edits an export file, which Rekordbox can regenerate, so it uses a `dry_run` flag and a backup; the audio-file writes in section 3 got a journal and undo as well.
  - `duplicate_memory_cues_to_hot_cues(output_path, dry_run=True)`: copies each memory cue to a hot cue at the same position, in the lowest free slot (8 slots, A-H), in the loaded file. The memory cues stay. The slot rules live in `services/cue_rules.py`, so the Serato writer can use the same ones. On the real library it adds 129 hot cues to 47 tracks, with at most 5 per track, so no overflow.
  - **Edits go into the loaded file itself** (or `output_path` if given). The first edit of a fresh Rekordbox export saves a verified, timestamped backup next to it. Edited files carry a marker comment, so later edits make no more backups and a re-export is backed up again. Writes are atomic and keep permissions, and are refused if the file changed since planning. Both tools build on the same file, so edits add up, and running them again is a no-op. Because these tools edit a file in place, they are annotated as destructive.
- [x] Clear tool descriptions and error messages that tell the assistant what to do next (for example "load a library first").
- [x] Document how to register the server in Claude Code (in the README). The Claude Desktop config is described but untested.
- [x] Tests that call the tool functions directly with sample fixtures, and through the MCP protocol with an in-memory client.
- [ ] Open question: confirm how Rekordbox treats cues on tracks already in the collection when it imports an XML. `set_default_cue` writes a valid file, but it hasn't been tested by importing it into Rekordbox.
- [ ] Optional filters for `set_default_cue`, such as skipping very short tracks. A real export includes Pioneer sampler one-shots (`NOISE`, `SINEWAVE`), which get a cue too.

### 3. Serato support (the riskiest part)

**Findings from a real library** (1,667 Apple Music tracks, Serato DJ Pro, files on an ExFAT drive):

- Serato's `database V2` is a track list only (path, title, artist, BPM, key). It has no cues. Paths are stored without the leading `/`. Serato keeps one `_Serato_` folder per drive, so the library on the music drive is the real one.
- Cues live in the audio files: a `GEOB` frame named `Serato Markers2` in the ID3 tag (MP3), or in the `id3 ` chunk (WAV). Its layout: `01 01` + base64 text (wrapped at 72 characters, no `=` padding) + NUL padding to exactly 470 bytes. The base64 decodes to `01 01`, then entries (`name\0`, 4-byte big-endian length, payload), then a final NUL. Entries seen: `COLOR`, `CUE`, `BPMLOCK`. A `CUE` payload is 13 bytes: reserved, slot (0-based), position in ms (4 bytes big-endian), reserved, RGB (3 bytes), 2 reserved, name + NUL.
- All 1,541 real tags decode and **re-encode byte for byte**, which is the test the codec has to keep passing.
- Files also carry a legacy `Serato Markers_` frame, plus `Overview`, `Analysis`, `Autotags`, `BeatGrid` frames and artwork. **Serato DJ Pro reads the legacy frame**: a file whose Markers2 had three cues and whose legacy frame had one showed one cue, and Serato then wrote its own version back over Markers2. So both must be written. Layout: `02 05`, a 4-byte count (14), 14 entries of 22 bytes, then `07 7f 7f 7f`. An entry is start (5 bytes), end (5), `00`, `7f`x5, color (4), type, lock, with numbers at 7 bits per byte. The first five entries are the cue slots (type `01` = cue); the rest are loops. The encoder leaves a slot that already holds the right cue untouched, because some real files keep default colors or leftover bytes in empty slots. It leaves all 1,040 real files where Serato's two frames agree unchanged.
- 241 files already have Serato cues, most synced earlier by Lexicon from Rekordbox memory cues.
- **Timing differs between the programs.** Rekordbox minus Serato: 0 ms for WAV; about 5-6 ms for MP3 with a LAME header; for MP3s with an Xing/Info frame but no LAME tag, -26 ms (one MP3 frame) on three files and -52 ms (two frames) on one. The beat grids' first-beat difference (Serato minus Rekordbox) reproduces every known offset: 0.0 ms median on WAV, -4.0 on LAME MP3s, +24 to +27 and +53 on the four Xing/Info files. A cue placed by ear in Serato on the two-frame file landed 45 and 49 ms after Rekordbox's, against 53 predicted.
- ExFAT has no separate `ctime`, and Serato may keep file times, so a change to a tag can't be detected from timestamps. Compare contents.

**Decisions**

- **Rekordbox wins.** When the cues differ, the Serato tag is rewritten from Rekordbox. Cues are never merged.
- The Lexicon-written cues are replaced only when they differ from Rekordbox after the timing offset is applied.

- [x] Decide on the Serato input: read cues from audio-file tags; use `database V2` only to list tracks.
- [x] Decode `Markers2` and prove the format against real data (read side).
- [x] Codec (`parsers/serato_markers.py`) and a read-only tag reader (`parsers/serato_tags.py`, MP3 and WAV via mutagen) with tests. Validated on the real library: all 1,541 tags decode and re-encode byte for byte, covering 365 cues in 241 files, with no cue needing the raw-bytes fallback. A cue the codec can't reproduce exactly is kept verbatim as a `RawEntry`, so a rewrite can never silently alter it.
- [x] Tag writer (`services/serato_sync.py`): overwrites the fixed 470-byte payload in place (located by `locate_markers2`, checked against mutagen on all 1,541 real files), so nothing else in the file moves. A write-ahead journal in `~/.cuebridge/undo/` stores the original bytes, every write is read back (the rest of the file must hash identically), failures are restored at once, and `undo_sync` reverses a run. File mtime is preserved. Refuses while Serato DJ Pro is running.
- [x] "Change only what differs": a Serato cue within 1 ms of the computed one (30 ms where timing is unverified) keeps its exact bytes and color; colors come from Rekordbox, then the existing slot, then a default. On the real library 38 of 39 cued files already match; only `FODDER 001` (3 cues in Rekordbox, 1 in Serato) would change.
- [ ] Add a missing `Serato Markers2` frame to files that have none (needs a tag rewrite, not an in-place overwrite). Currently skipped with "load the track in Serato once".
- [ ] M4A, AIFF and FLAC (Serato stores its data differently there).
- [ ] Hot loops and cue names in Serato's `LOOP` entries (none exist in the sample library).
- [x] Per-format timing offset (`services/cue_timing.py`): WAV 0 ms, MP3 -5.5 ms (LAME header or no Xing/Info frame). MP3 with an Xing/Info frame but no LAME tag: estimated from the two beat grids as a whole number of MP3 frames (`refine_with_beat_grids`), otherwise left uncorrected and flagged. Round half up. Predicts the 95 real Lexicon-synced cues on measured files to within 1 ms (67 exact).
- [x] Rekordbox's default hot-cue green (`#28e214`) no longer overrides Serato colors, and a cue within tolerance keeps Serato's exact position even when its color changes.
- [ ] Collect small real Serato-tagged fixtures for tests (done for one MP3; need a WAV with cues).
- [x] Write the legacy `Serato Markers_` frame in step with Markers2 (codec, locator, journal with several regions per file, undo, verification). Serato only showed the new cues once this was done.
- [ ] Tool: `load_serato_library(path)` / `read_serato_cues(...)` to read and compare Serato cues without writing.
- [x] Tools `sync_cues_to_serato` (dry run by default, `expected_files` must match the dry run) and `undo_serato_sync`.
- [x] Confirmed in Serato: a real WAV ("03 Stalker") written by the sync shows all three cues.

### 4. Compare

- [ ] Match tracks across the systems by file path, falling back to artist and title (and duration). This is the shared matcher in section 8, and it also lets the Serato sync find files whose paths moved.
- [ ] Tool: `compare_libraries()`, returning a summary plus paginated detail of missing, mismatched and conflicting cues.
- [ ] Tool: `find_unmatched_tracks()`, listing tracks that couldn't be matched.

### 5. Convert and export (the actual "bridge")

- [ ] Cue color and slot mapping between the two systems.
- [ ] Serato to Rekordbox: generate a Rekordbox XML with `POSITION_MARK`s. This writes a new file and leaves the user's library alone.
- [x] Rekordbox to Serato: write cues into Serato tags in audio files (see section 3).
- [x] **Safety before any write to audio files**, built for the Serato sync in a simpler shape than first planned (no plan IDs):
  - `dry_run=True` by default, with a text preview of what changes and why files are skipped
  - applying requires `expected_files` equal to the dry run's count, so an approval is tied to what was previewed
  - a write-ahead journal of the original bytes, read-back verification, an immediate restore of any file that fails, and `undo_serato_sync`
  - a refusal while Serato DJ Pro is running
  - note: an assistant can still call the tool with `dry_run=false` itself, so the real check is the MCP client's per-call approval prompt (the tools are marked destructive). A UI Apply button (section 7) would be stronger.
- [ ] Apply the same pattern to future file-writing actions (tag fixes, section 8): preview, expected count, journal, verification, undo.

### 6. Polish and quality

- [x] Update the README: what CueBridge is, the MCP setup, example prompts, and a link to this roadmap. Keep it current as tools are added.
- [x] Remove the dead Serato stub, its endpoint and the React upload.
- [ ] Add CI and lint, and a one-command dev setup.
- [ ] Pin a Python version.
- ~~Packaging for local install (`pipx` or `uvx`).~~ Not needed for a personal tool.

### 7. Optional review UI, opened by the MCP server (after the text workflow is complete)

Lowest priority. Only worth building if the text summaries in chat turn out not to be enough for reviewing changes, since this is a personal tool and Claude Code is the client.

- [ ] Point the FastAPI endpoints at the service layer. They read the same cached library as the MCP tools.
- [ ] Generate the TypeScript types from the backend's OpenAPI schema (`openapi-typescript`), so `types/cue.ts` stops being a hand-copied duplicate of `models.py`.
- [ ] Replace upload with session-based views, such as `/review/{plan_id}`, that load a pending comparison or conversion plan from the server.
- [ ] Side-by-side comparison view: wire up `CueComparison`, and flag missing and mismatched cues.
- [ ] Track detail view that uses `CueCard`.
- [ ] Preview-and-apply screen for write operations: shows exactly which files and cues change, with an Apply button that triggers the write. This is the stronger confirmation step, because the user's click doesn't pass through the assistant.
- [ ] MCP tool `open_review_ui(...)`: starts the web server if it isn't running, and returns the URL (and opens the browser if possible).
- [ ] Fix the UI bugs listed above (search-box arrow keys, stable row keys, sort direction, API base URL from an env variable).
- [ ] Bind the server to `127.0.0.1` only, and protect apply actions with a one-time token in the URL, so no other local page or process can trigger a write.

### 8. Library management tools

- [x] **Update existing Rekordbox playlists** (`sync_playlists_to_rekordbox`, `undo_rekordbox_playlist_sync`, `services/rekordbox_db.py`): adds tracks that are in an Apple Music playlist but not in the same Rekordbox playlist to the end of it, by editing `master.db` through pyrekordbox (optional dependency). Reads the live database through a copy, so no Rekordbox export is needed for it; requires Rekordbox to be closed to write; backup, journal, read-back verification and undo. Only adds tracks already in the collection to existing top-level playlists. Not built: removing tracks, creating playlists, reading cues from the database, and using the database as the source for the other tools (so the XML export is no longer needed at all).

Ideas grounded in what the real library showed (1,667 Apple Music tracks, 30 playlists, 1,177 tracks in the Rekordbox collection, files mostly MP3 and WAV on an ExFAT drive). **Reports** only read. **Actions** change something and follow the same pattern as the Serato sync: dry run first, a journal of what changed, verification, undo.

What can be written, for planning: Apple Music's database is read-only for us (we read an exported XML). Rekordbox can only be changed through an XML we write and then import. Audio files can be written, carefully. Scans must skip the `._*` AppleDouble files that ExFAT drives collect.

**Foundations**

- [x] Apple Music XML reader (`parsers/apple_music_xml.py`, `plistlib`): tracks (name, artist, album, genre, BPM, year, bit rate, size, date added, location) and user playlists with their order and folders. Tools `load_apple_music_library` and `get_playlist_tracks`. Reads an exported XML, or (no path) the open Music app directly through a read-only AppleScript/JXA script (`services/apple_music_live.py`), so no export is needed: on the real library it returns all 1,667 tracks and 30 playlists in about 20-30 seconds and matches the XML export, with the live paths being the more accurate (74 differ only in folder-name case, 3 are files that no longer exist). Music's "Share iTunes Library XML" setting is not available in macOS 15's Music app, which is why the script is used.
- [x] A track matcher (`services/track_matcher.py`): path, then file name, then title and artist with duration to break ties; Unicode, case and punctuation normalised; an unclear match is reported as ambiguous, never guessed. On the real library, of 51 cued Rekordbox tracks: 43 matched by path, 3 by file name, 2 by title and artist, 3 not at all. Still to do: the other direction (Apple Music tracks to Rekordbox) for the drift reports.

**Reports (read-only)**

- [x] **Drift between Apple Music and Rekordbox** (`compare_libraries`, `list_library_differences`, `services/drift.py`): tracks and playlists present in one and not the other. The stale July 2025 export showed 601 Apple Music tracks missing from Rekordbox and 3 of 30 playlists covered. The fresh export (Nov 2026) shows 0 playlisted Apple Music tracks missing from Rekordbox (15 more are in no playlist and are ignored on purpose: only playlisted tracks are meant for Rekordbox), 16 Rekordbox files inside Apple Music's folder that Apple Music doesn't have (the other ~5,540 Rekordbox-only tracks are outside it: sample packs and the like), and all 30 playlists in sync. Rekordbox's playlists are now parsed.
- [x] **Missing and moved files** for Rekordbox (`find_missing_files`), and the sync uses Apple Music's path for a moved file. Real library: 9 cued tracks not at their path; 5 found through Apple Music (all already in sync), 4 not recoverable (one is a Tidal streaming track, one is an M4A missing from both libraries, two are in neither). Still to do: the same check for Apple Music's own paths.
- [ ] **Duplicates:** the same title and artist (55 pairs in the Rekordbox collection), the same file in several folders, and near-duplicates by duration and size. Done so far: Apple Music lists 51 files twice (the same file under two track IDs), shown by `list_library_differences("duplicate_entries")`.
- [ ] **Metadata gaps:** no artist (418 of 1,177 in Rekordbox), no BPM or key, zero length, "Unknown Artist / Unknown Album" folders, sampler one-shots mixed into the library.
- [ ] **Playlist hygiene:** tracks in no playlist, tracks in many, empty playlists, overlap between genre playlists, the biggest playlists.
- [ ] **Format and quality:** counts by format, bit rate and sample rate; low-bit-rate MP3s; very large files.
- [ ] **Cue audit across both programs:** tracks with cues in one and not the other, Serato cues that Rekordbox lacks (the reverse of the sync), cue counts and colors.
- [ ] **Library questions:** what was added recently, tracks per genre or year, the library's size.

**Actions**

- [x] **Playlists into Rekordbox** (`export_playlists_for_rekordbox`, `services/playlist_export.py`): adds a folder of the playlists that differ (or those named) to the Rekordbox XML that Rekordbox's Imported Library points at (backup first; or a separate file with `output_path`), with the folder and new tracks marked as pending until Rekordbox is re-exported, copying Rekordbox's own entries for tracks it has and building entries for new ones, so one right-click replaces the drag and drop. Rekordbox 7.2.19 only creates a playlist from an XML import and never updates an existing one (an import of `r&b` made a second `r&b` in a new Apple Music folder), so updating an existing playlist is done through the database instead (`sync_playlists_to_rekordbox`, below). The XML route stays useful for getting a new track into the collection. Found while testing it: 53 Rekordbox tracks point at files Apple Music has since renamed (for example `15 Linguine Limbs.mp3` is now `1-15 Linguine Limbs.mp3`), so they are broken in Rekordbox today; the export points them at the real files.
- [ ] **Relocate moved files** in a Rekordbox XML copy by rewriting each `Location`.
- [ ] **Fix tags in files** (title, artist, genre, BPM, key) from a reviewed list, journaled. Needs a careful ID3 write, which is bigger than the in-place cue overwrite.
- [ ] **Serato crates from playlists** (optional: you play from Apple Music playlists inside Serato).
- [ ] **Snapshot:** copy the small, important files (the Rekordbox XML, Serato's `database V2`, the undo journals) to a dated folder on demand.

**Suggested order:** the Apple Music reader and matcher, then the drift and missing-file reports, since that is the problem seen first-hand. Then duplicates, metadata and playlist reports. Actions come last, one at a time.

### Not yet verified

- [x] **`sync_playlists_to_rekordbox` on the real database.** Verified once on the live `master.db` (Rekordbox 7.2.19): "Zanpakuto" was added to the real `r&b` playlist; the read-back check passed, the other 29 playlists and all 7,220 tracks were unchanged, and Rekordbox opened and showed the track in the playlist. Still to check: a playlist with several additions, adding a track to a playlist that has cues on its tracks, Rekordbox's cloud or library sync if used, and re-running after a Rekordbox update.

- [ ] **Importing the playlist folder into Rekordbox 7. Not verified.** What has been learned on the real library: with the original export set as the Imported Library, Rekordbox shows the `rekordbox xml` sidebar item (after a restart), but with the file as first edited (an XML library's re-serialisation: single-quoted declaration, two comment lines, LF instead of CRLF, `&apos;` unescaped, `<X />` spacing, attributes on one line) the item did not appear, so Rekordbox rejects that format. The playlist export now patches the text instead (13 changed lines on a 9 MB file, CRLF and header intact, no markers; pending state kept in a side file). Not yet known whether Rekordbox accepts that file, which part of the old format it objected to, whether Import Playlist adds the playlist or skips one that exists, whether a track Rekordbox doesn't have is created and analysed, and whether cues survive. **The other two editing tools (`set_default_cue`, `duplicate_memory_cues_to_hot_cues`) still re-serialise the XML and add a marker comment, so Rekordbox may reject their output too; they should move to the same text patching.**

Things that are built and tested in code but have never been checked in the real application. Most need the user, because only they can run Rekordbox and Serato.

- [x] **Serato accepts a file CueBridge wrote** (and needs the legacy frame written too). Checked on one WAV. Still to check: an MP3, and colors and cue positions against Rekordbox by ear.
- [ ] **Importing an edited Rekordbox XML back into Rekordbox.** Does it add the hot cues to tracks already in the collection, or only add new tracks? Affects `set_default_cue` and `duplicate_memory_cues_to_hot_cues`. Back up Rekordbox first. If it does not update existing tracks, the XML tools are only useful as input for the Serato sync.
- [ ] **The Rekordbox XML marker comment** (`<!-- Edited by CueBridge. -->`) is accepted by Rekordbox's importer. It is valid XML, but untested.
- [ ] **MP3 timing for files with an Xing/Info frame but no LAME tag.** Now estimated from beat grids (1 or 2 frames), checked by ear on one file (Drop feat Frisco: within about 6 ms). Check the other three (Sex, Smokin, Glue) by ear, and look for what makes some files one frame and others two.
- [ ] **The MP3 offset of 5.5 ms** was fitted on Lexicon-synced pairs from one library. It matches 95 cues to within 1 ms, but confirm by ear or with a beat grid on a few tracks.
- [ ] **Claude Desktop.** The README describes registering the server there, but only Claude Code has been tried.
- [ ] **Other Python versions.** Tested on 3.13 only; the dependencies declare 3.10 or newer.
- [ ] **First run on the real library.** Dry run, then a real sync of one or two tracks (Serato closed), then the rest. The journal makes it reversible.

### Later

- Support other platforms (Traktor NML, Engine DJ).
- Cue editing through tools (rename, recolor, move).
- Batch and multi-library operations.
- MCP resources and prompts (for example a "migrate my library" guided prompt).

## Suggested next step

1. Export a fresh `rekordbox.xml`, then build the Apple Music reader, the matcher, and the drift and missing-file reports (section 8).
