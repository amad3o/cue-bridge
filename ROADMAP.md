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
5. **README is out of date.** It lists Tailwind and shadcn/ui, which aren't installed, and has no setup or run instructions.
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

- [ ] Add the official Python MCP SDK (`mcp`, `FastMCP`) and a `mcp_server.py` entry point using stdio.
- [ ] Tools, all with compact, paginated output:
  - `load_rekordbox_library(path)`: parses and caches, returns a summary (track count, cue totals by type).
  - `search_tracks(query, has_cues, limit, offset)`: filter and page through tracks.
  - `get_track_cues(track_id)`: full cue detail for one track.
  - `find_tracks_without_cues()` and basic library stats.
- [ ] Clear tool descriptions and error messages that tell the assistant what to do next (for example "load a library first").
- [ ] Document how to register the server in Claude Desktop and Claude Code.
- [ ] Tests that call the tool functions directly with sample fixtures.

### 3. Serato support (the riskiest part)

- [ ] Decide on the Serato input: read cues from audio-file tags (`Serato Markers2`, using mutagen or a custom GEOB decoder). Use crates and `database V2` only to list tracks.
- [ ] Collect small real Serato-tagged fixtures for tests.
- [ ] Write a real `serato_parser` that returns `Track[]` in the same normalized shape.
- [ ] Tool: `load_serato_library(path)` or `read_serato_cues(folder_or_file)`.

### 4. Compare

- [ ] Match tracks across the two systems by file path, falling back to artist and title (and duration).
- [ ] Tool: `compare_libraries()`, returning a summary plus paginated detail of missing, mismatched and conflicting cues.
- [ ] Tool: `find_unmatched_tracks()`, listing tracks that couldn't be matched.

### 5. Convert and export (the actual "bridge")

- [ ] Cue color and slot mapping between the two systems.
- [ ] Serato to Rekordbox: generate a Rekordbox XML with `POSITION_MARK`s. This writes a new file and leaves the user's library alone.
- [ ] Rekordbox to Serato: write cues into Serato tags in audio files.
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

- [ ] Update the README: what CueBridge is, the MCP setup, example prompts, and a link to this roadmap.
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

### Later

- Support other platforms (Traktor NML, Engine DJ).
- Cue editing through tools (rename, recolor, move).
- Batch and multi-library operations.
- MCP resources and prompts (for example a "migrate my library" guided prompt).

## Suggested next step

Section 1: extract a path-based service layer from `main.py`. It's a small refactor, and the MCP server (and later the web endpoints) depend on it. Then build the read-only Rekordbox tools (section 2), which give a working MCP server before Serato is started. The UI (section 7) waits until the text workflow is complete.
