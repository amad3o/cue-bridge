# CueBridge Roadmap

CueBridge is moving from a web UI to an **MCP server**. The goal is for an AI assistant such as Claude to read, compare and convert DJ cue points between Rekordbox and Serato by calling tools against files on the user's own machine.

The parsing, matching and conversion logic stays in plain Python. The MCP server is a thin layer over it.

**Text first, UI later.** Every tool returns a clear text summary the assistant can show in chat, such as "12 tracks change, 31 cues added, 2 conflicts", with a short diff of the first few items. The React UI is deferred. Once the text workflow is complete, an optional review page that the MCP server opens (section 7) can be added where the text summary falls short.

## Completed

**Core (Python)**

- Rekordbox parser normalizes cues:
  - unique IDs built from `TrackID`, cue type, slot and time
  - hot cue, memory and loop types, with a null `cueIndex` for memory cues
  - loop end time (`endMs`) and hex color
  - `filePath` decoded from `Location`
  - fade and load markers skipped, and bad marks no longer crash the parse
- Pydantic models (`cuebridge/models.py`): `Cue`, `Track`.
- Path-based service layer (`cuebridge/services/rekordbox_service.py`) with validation, domain errors, and an mtime-based library cache. The HTTP endpoints call it, and so will the MCP server.
- Missing or blank titles, artists and cue names are `None`, not placeholder text. The UI chooses what to show.
- `RekordboxParseError` for XML that isn't a Rekordbox export.
- pytest suite for the parser and the upload endpoint.
- Root `.gitignore`, and cache files untracked.

**Web UI (prototype, on hold until the text workflow is complete)**

- FastAPI `POST /upload/rekordbox` with validation (`.xml` only, 100 MB limit, clear 400/413 errors) and `POST /upload/serato` (a stub that parses no tracks).
- React upload screen, sortable and searchable `TrackTable`, and `CueCard` / `CueComparison` components. Shared TypeScript types match the API.

## Known issues

1. **Serato parser is a stub.** It decodes the bytes as UTF-8, which is garbage for binary data.
   - Serato stores cues in binary `Serato Markers2` / `Markers_` ID3/GEOB tags inside the audio files themselves.
   - Crates are `.crate` files and the library is a `database V2` file. These are binary too, and only hold track lists.
2. **Missing names default to the string `"Unknown"`**, which is indistinguishable from real data.
3. **Large libraries need a plan for AI output.** Returning every track would flood the assistant's context. Tools need summaries, filters and pagination. The legacy API's silent 100-track cap is not a model to copy.
4. **No tests for Serato yet,** and no sample Serato fixtures.
5. ~~**README is out of date.** It lists Tailwind and shadcn/ui, which aren't installed, and has no setup or run instructions.~~ Fixed: rewritten around the MCP server, with setup, registration and tool docs.
6. **Housekeeping.** `requirements.txt` has no pinned Python version, and a `.DS_Store` may still be tracked.
7. **UI bugs to fix when the UI is revived (section 7).**
   - The arrow-key listener is global, so it fires while typing in the search box.
   - Row keys aren't stable, sort direction never flips, and selection isn't reset when the filter changes.
   - The API URL is hardcoded to `localhost:8000`.

## To do

### 1. Restructure the core for reuse

- [x] Move logic out of `main.py` into a service layer that takes **file paths** (not uploads) and returns models. The HTTP endpoints and the MCP server both call it.
- [x] Move shared models out of the web layer, and decide on a package layout. The `app` package is now `cuebridge` (a generic top-level name like `app` can't be installed alongside other packages, which the packaging item in section 6 needs):
  ```
  backend/cuebridge/
    models.py      shared Pydantic models
    parsers/       file formats to models
    services/      path-based logic: validation, caching
    web/main.py    FastAPI app (run with: uvicorn cuebridge.web.main:app)
    (mcp_server.py goes here in section 2; avoid naming a package `mcp`, which would shadow the SDK)
  ```
- [x] Make "unknown" explicit: use `None` for missing title or artist, not the string `"Unknown"`.
- [x] Add a library cache so a large XML is parsed once per session, not on every tool call. `get_rekordbox_library(path)` keys on the resolved path, re-parses when the file's mtime or size changes, and keeps the last 4 libraries.

### 2. MCP server, read-only (Rekordbox)

- [x] Add the official Python MCP SDK (`mcp` 2.x, where `FastMCP` is now `MCPServer`) and a `cuebridge/mcp_server.py` entry point using stdio: `python -m cuebridge.mcp_server`.
- [x] Tools, all with compact, paginated output:
  - `load_rekordbox_library(path)`: parses and caches, returns a summary (track count, cue totals by type).
  - `search_tracks(query, has_cues, limit, offset)`: filter and page through tracks.
  - `get_track_cues(track_id)`: full cue detail for one track.
  - `find_tracks_without_cues()` and basic library stats.
  - `set_default_cue(output_path, dry_run=True)`: adds a hot cue A at 0:00 to every track with no cue or loop, in the loaded file (see below). It re-parses its own output to check that only the intended tracks changed. This is the first write tool. Because it only creates a new file, it uses a simple `dry_run` flag. The plan/apply/undo design in section 5 is still needed for edits to audio-file tags.
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
- Files also carry a legacy `Serato Markers_` frame with the same cues in an older format, plus `Overview`, `Analysis`, `Autotags`, `BeatGrid` frames and artwork. A writer must keep all of them and keep the legacy frame consistent.
- 241 files already have Serato cues, most synced earlier by Lexicon from Rekordbox memory cues.
- **Timing differs between the programs.** Rekordbox minus Serato: 0 ms for WAV; about 5-6 ms for MP3 with a LAME header; about -26 ms (one MP3 frame) for three MP3s with an Xing/Info frame but no LAME tag. Small sample, so the rule is a hypothesis.
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
- [x] Per-format timing offset (`services/cue_timing.py`): WAV 0 ms, MP3 -5.5 ms (LAME header or no Xing/Info frame), MP3 with an Xing/Info frame but no LAME tag left uncorrected and flagged. Round half up. Predicts the 95 real Lexicon-synced cues on measured files to within 1 ms (67 exact).
- [ ] Collect small real Serato-tagged fixtures for tests (done for one MP3; need a WAV with cues).
- [ ] Write the legacy `Serato Markers_` frame consistently, or confirm Serato ignores it. Not written yet: files carry both, so a synced file's two copies of the cues now disagree. Test one file in Serato.
- [ ] Tool: `load_serato_library(path)` / `read_serato_cues(...)` to read and compare Serato cues without writing.
- [x] Tools `sync_cues_to_serato` (dry run by default, `expected_files` must match the dry run) and `undo_serato_sync`.
- [ ] Confirm in Serato that a file we wrote shows the right cues. Only the user can do this.

### 4. Compare

- [ ] Match tracks across the two systems by file path, falling back to artist and title (and duration).
- [ ] Tool: `compare_libraries()`, returning a summary plus paginated detail of missing, mismatched and conflicting cues.
- [ ] Tool: `find_unmatched_tracks()`, listing tracks that couldn't be matched.

### 5. Convert and export (the actual "bridge")

- [ ] Cue color and slot mapping between the two systems.
- [ ] Serato to Rekordbox: generate a Rekordbox XML with `POSITION_MARK`s. This writes a new file and leaves the user's library alone.
- [x] Rekordbox to Serato: write cues into Serato tags in audio files (see section 3).
- [ ] **Safety before any write to audio files:**
  - every write tool defaults to `dry_run=True` and returns a preview
  - a dry run saves a pending plan and returns a plan ID with a text summary and diff
  - a separate `apply_plan(plan_id)` tool performs the write, and only for a plan that was previewed first
  - plans expire after a short time and are invalidated if the source files change, so a stale plan can't be applied
  - automatic backup before modifying a file, and an `undo_plan(plan_id)` tool that restores it
  - a per-plan limit on files modified
  - note: an assistant can call `apply_plan` itself, so the real check is the MCP client's per-call approval prompt. The tool description should tell the assistant to show the summary and get the user's explicit go-ahead first. A UI Apply button (section 7) would be a stronger check, since the user's click doesn't pass through the assistant.
- [ ] Tools: `export_rekordbox_xml(...)`, `plan_serato_cue_write(...)` (creates a pending plan and returns its ID and summary), `apply_plan(plan_id)`, `undo_plan(plan_id)`.

### 6. Polish and quality

- [x] Update the README: what CueBridge is, the MCP setup, example prompts, and a link to this roadmap. Keep it current as tools are added.
- [ ] Add CI and lint, and a one-command dev setup.
- [ ] Pin a Python version.
- [ ] Packaging for local install (`pipx` or `uvx`), so users can run the server with one command.

### 7. Optional review UI, opened by the MCP server (after the text workflow is complete)

Only start this once sections 1–5 work end to end. Decide then, based on how often the text summaries fall short.

- [ ] Point the FastAPI endpoints at the service layer. They read the same cached library as the MCP tools.
- [ ] Generate the TypeScript types from the backend's OpenAPI schema (`openapi-typescript`), so `types/cue.ts` stops being a hand-copied duplicate of `models.py`.
- [ ] Replace upload with session-based views, such as `/review/{plan_id}`, that load a pending comparison or conversion plan from the server.
- [ ] Side-by-side comparison view: wire up `CueComparison`, and flag missing and mismatched cues.
- [ ] Track detail view that uses `CueCard`.
- [ ] Preview-and-apply screen for write operations: shows exactly which files and cues change, with an Apply button that triggers the write. This is the stronger confirmation step, because the user's click doesn't pass through the assistant.
- [ ] MCP tool `open_review_ui(...)`: starts the web server if it isn't running, and returns the URL (and opens the browser if possible).
- [ ] Fix the UI bugs listed above (search-box arrow keys, stable row keys, sort direction, API base URL from an env variable).
- [ ] Bind the server to `127.0.0.1` only, and protect apply actions with a one-time token in the URL, so no other local page or process can trigger a write.

### Not yet verified

Things that are built and tested in code but have never been checked in the real application. Most need the user, because only they can run Rekordbox and Serato.

- [ ] **Serato accepts a file CueBridge wrote.** The writer reproduces Serato's tag layout byte for byte (1,541 real tags) and verifies each write by reading it back, but no file has been opened in Serato. Test on a copy of one track: sync, quit Serato, reopen, and check the cues show in the right slots, colors and positions.
- [ ] **The legacy `Serato Markers_` frame.** Files carry a second, older copy of the cues that the sync does not update, so a synced file's two copies disagree. Find out from the Serato test above whether it matters. If it does, decode and rewrite that frame too.
- [ ] **Importing an edited Rekordbox XML back into Rekordbox.** Does it add the hot cues to tracks already in the collection, or only add new tracks? Affects `set_default_cue` and `duplicate_memory_cues_to_hot_cues`. Back up Rekordbox first. If it does not update existing tracks, the XML tools are only useful as input for the Serato sync.
- [ ] **The Rekordbox XML marker comment** (`<!-- Edited by CueBridge. -->`) is accepted by Rekordbox's importer. It is valid XML, but untested.
- [ ] **MP3 timing for files with an Xing/Info frame but no LAME tag.** Left uncorrected and flagged, based on three files that differed by about 26 ms. Measure more files, or confirm the cause, before correcting them.
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

Check one synced file in Serato itself: copy a track with the `FODDER 001` kind of difference, point a Rekordbox cue at it, sync, quit Serato, then open the copy and see whether the cues show. That is the only test the codec and writer cannot do for themselves, and it also answers whether the legacy `Serato Markers_` frame needs writing. After that: matching tracks whose files moved (section 4), then the optional review UI (section 7).
