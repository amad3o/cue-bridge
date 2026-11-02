# CueBridge Roadmap

## Completed

**Backend (FastAPI)**

- `POST /upload/rekordbox` parses Rekordbox XML and returns `filename`, `tracksFound` and the first 100 tracks. Each track has its normalized cues.
- CORS is set up for the Vite dev server at `localhost:5173`.
- `POST /upload/serato` accepts a file and returns its size and a 1000-byte text preview. This is a stub. It parses no tracks.
- Rekordbox parser normalizes cues:
  - unique IDs built from `TrackID`, cue type, slot and time
  - hot cue, memory and loop types, with a null `cueIndex` for memory cues
  - loop end time (`endMs`) and hex color
  - `filePath` decoded from `Location`
  - fade and load markers skipped, and bad marks no longer crash the upload
- pytest suite for the parser and the upload endpoint.
- Pydantic response models (`app/models.py`) and upload validation: `.xml` only, 100 MB limit, and clear 400/413 errors for empty, malformed or non-Rekordbox files.
- Root `.gitignore`, and cache files untracked.

**Frontend (React + TS + Vite)**

- Rekordbox XML upload, by file picker or drag-and-drop, with loading and error states.
- `TrackTable`:
  - sortable by title, artist or cue count
  - up/down arrow keyboard row selection
  - cue-time summary column
- Search box and a "only tracks with cues" filter.
- Shared `NormalizedCue` and `Track` types (updated for `endMs`, `trackId`, `filePath` and nullable `cueIndex`/`color`), plus `formatCueTime`.
- `CueCard` shows loop end, slot or "Memory", and cue color.
- Serato upload button that logs the preview and shows a size summary.
- `CueCard` and `CueComparison` components exist but nothing renders them yet.

## Issues and bugs

1. ~~**Cue ID collisions.** The ID is `rekordbox-{trackName}-{Num}`, so two tracks with the same title produce duplicate IDs. Use the track's `TrackID` or `Location` instead.~~ Fixed.
2. ~~**Rekordbox cue types are all labeled "hotcue".**~~ Fixed.
   - In Rekordbox XML, `Num=-1` is a memory cue. `Type` distinguishes cues from loops (`Type=4`, with an `End` attribute).
   - Loops and memory cues are mis-typed, and `End` is ignored.
   - `Num=-1` also gives `cueIndex` -1.
3. ~~**Cue `color` is always `None`.** The `Red`, `Green` and `Blue` attributes are ignored.~~ Fixed.
4. ~~**`filePath` is never populated.** The `Location` attribute is ignored. Without it you can't match tracks between Rekordbox and Serato.~~ Fixed.
5. **API truncates to 100 tracks** (`parsed_tracks[:100]`) with no pagination. Large libraries silently lose data.
6. **Parser has no error handling.**
   - ~~Malformed XML raises `ParseError`, which returns a 500.~~ Fixed: returns a 400.
   - ~~Missing or non-numeric values raise as well.~~ Fixed in the parser.
   - ~~There is no upload size or type validation.~~ Fixed: `.xml` only, 100 MB max.
   - Missing names default to the string `"Unknown"`, which is indistinguishable from real data.
7. **The Serato "parser" only decodes bytes as UTF-8.**
   - Serato stores cues in binary `Serato Markers2` / `Markers_` ID3/GEOB tags inside the audio files themselves.
   - Crates are `.crate` files and the library is a `database V2` file. These are binary too, so the preview is garbage.
8. **TrackTable bugs.**
   - Row keys use the title plus the index, so they aren't stable.
   - Sort arrows only show on the active column and never flip direction.
   - The arrow-key listener is global, so it fires while typing in the search box and blocks cursor movement.
   - The selection index isn't reset when the filter changes.
   - There is no scroll-into-view.
   - Time formatting is duplicated inline instead of using `formatCueTime`.
9. **Hardcoded `http://localhost:8000` in the API services.** Move it to an env var or a Vite proxy. The `allow_credentials` and origin settings are also dev-only.
10. **The README is out of date.**
    - It lists Tailwind and shadcn/ui, but neither is installed. Everything uses inline styles.
    - It doesn't mention Serato, the setup steps or how to run the app.
    - It says Pydantic, but no models are defined.
11. **Housekeeping.**
    - `requirements.txt` has no pinned Python version.
    - There are no frontend tests, and no tests for the API endpoints.
    - `RekordboxUploader` handles both uploaders plus filtering, so it's too big.
    - The Serato response type declares `tracks: []`.

## To do

### Near term: finish the MVP (Rekordbox to UI)

- [x] Commit the parser fix. Add `.gitignore` entries for `__pycache__` and `.DS_Store`, then `git rm --cached` them.
- [x] Fix cue IDs, cue types (hot, memory, loop), loop end time, color and file path in the Rekordbox parser.
- [x] Add Pydantic response models. This matches what the README promises.
- [x] Add error handling and validation for uploads.
- [ ] Add track detail view or expandable rows that use `CueCard`.
- [ ] Remove the 100-track cap by adding pagination or virtualized rows.
- [ ] Update the README and add run instructions.

### Serato support (the core feature)

- [ ] Decide on the Serato input. Reading cues from audio-file tags (`Serato Markers2`, using mutagen or a custom GEOB decoder) is the realistic route. The `database V2` and crate files only hold track lists.
- [ ] Write a real `serato_parser` that returns `Track[]` in the same normalized shape.
- [ ] Match tracks across the two systems by file path, falling back to artist and title (and duration).
- [ ] Wire up `CueComparison` to show Rekordbox and Serato cues side by side, with missing and mismatched cues flagged.

### Conversion and export (the actual "bridge")

- [ ] Rekordbox to Serato: write cues into Serato tags.
- [ ] Serato to Rekordbox: generate a Rekordbox XML with `POSITION_MARK`s.
- [ ] Cue color and slot mapping between the two systems.
- [ ] A dry-run preview and a backup before any write to the user's files.
- [ ] Download or export endpoints.

### Polish and quality

- [ ] Adopt Tailwind and shadcn/ui as the README says, or drop them from the README.
- [ ] Split `RekordboxUploader` into smaller components and hooks, and add a state/data layer.
- [ ] Fix the TrackTable issues above (sort direction, input focus, stable keys).
- [ ] Add backend tests (pytest, with sample XML fixtures) and frontend tests.
- [ ] Add CI and lint, and a one-command dev setup (Makefile or script).
- [ ] Add Docker or packaging for local-first distribution.

### Later

- Support other platforms (Traktor NML, Engine DJ).
- Waveform column (the "Wave" placeholder is `|||`).
- Batch and multi-library operations.
- Cue editing in the UI.

## Suggested next step

Fix the search-box arrow-key bug and move the API URL to an env variable. Then decide how to approach Serato, since it's the riskiest unknown.
