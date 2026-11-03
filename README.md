# CueBridge

CueBridge is a local-first tool for working with DJ cue points. It's an **MCP server**, so an AI assistant such as Claude can read your Rekordbox library, answer questions about your cues, and write edited copies of it. The goal is to move and compare cue points between **Rekordbox** and **Serato**.

Everything runs on your own machine, and your library files are never uploaded anywhere.

## Status

Early development. What works today:

- Reading a Rekordbox XML export: tracks, hot cues, memory cues and loops, with colors and file paths.
- Searching and paging through the library, and finding tracks with no cues.
- Writing an **edited copy** of the XML with a default hot cue added to tracks that have none.

Not built yet: reading Serato, comparing the two libraries, and converting between them. See [ROADMAP.md](ROADMAP.md) for completed work, known issues and what's next.

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
| `set_default_cue(output_path, dry_run=True)` | Adds a hot cue A at 0:00 to every track with no cue or loop, in the loaded XML file (a backup is saved first). |
| `duplicate_memory_cues_to_hot_cues(output_path, dry_run=True)` | Copies every memory cue to a hot cue at the same position, in the lowest free slots (A-H), in the loaded XML file. Memory cues are kept. A memory cue that already has a hot cue at the same time is skipped, so running it twice changes nothing. Cues beyond 8 are reported and not copied. |

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
