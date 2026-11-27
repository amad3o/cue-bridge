# CueBridge

CueBridge keeps a DJ library in step across **Apple Music** (tracks and playlists), **Rekordbox** (cues and USB sets) and **Serato** (playing). It's an **MCP server**, so an AI assistant can read the libraries, compare them and carefully make changes. You can use it from Claude Code or from its own terminal agent. It's a personal tool, built around one setup.

Your files stay on your machine, but whatever the assistant is shown (track names, file paths, playlists, cues) goes to its provider. The built-in agent uses **OpenAI**.

## What it does

Early development, used on one real library. There are 28 tools; results are paged (at most 100 per call).

| Area | Tools |
| --- | --- |
| Check everything | `check_sync_status` (read-only; names the tool that fixes each difference) |
| Read and search | `load_apple_music_library` (live from the Music app, or an XML), `get_playlist_tracks`, `load_rekordbox_library`, `search_tracks`, `get_track_cues`, `find_tracks_without_cues` (the last three read the XML export) |
| Compare and audit | `compare_libraries`, `list_library_differences`, `find_missing_files`, `compare_cues`, `audit_music_library` |
| Cues to Serato | `sync_cues_to_serato` writes Rekordbox's cues into MP3 and WAV tags; Rekordbox wins |
| Cues in Rekordbox | `set_default_cue` (hot cue A at 0:00 on tracks with no cue), `duplicate_memory_cues_to_hot_cues` |
| Playlists | `sync_playlists_to_rekordbox` (updates existing playlists), `export_playlists_for_rekordbox` (XML import, for new tracks and playlists) |
| Repairs | `fix_moved_files_in_rekordbox`, `report_stale_duplicates`, `create_review_playlists`, `clean_up_stale_duplicates` |
| Undo | six `undo_*` tools, one for each writer above except the XML export |
| Experimental | `analyze_beatgrid`: read-only BPM and first-beat estimate, not accurate enough yet to write grids from |

Not built: Serato to Rekordbox cues, M4A, AIFF and FLAC cues (skipped), tag editing and Serato crates. The [roadmap](docs/ROADMAP.md) has what's done, what's unverified in the real apps and what's next.

## Setup

Needs Python 3.10 or newer, plus Node 20 or newer for the built-in agent's screen.

```bash
make setup      # .venv, dependencies and dev tools
make check      # ruff, Python tests and screen tests
```

Optional extras:

- **Rekordbox database** (needed to write to it, and to read it live): run `brew install sqlcipher`, then `make setup-db`.
- **Beatgrid analysis:** `.venv/bin/pip install -e ".[beatgrid]"`. `make eval-beatgrid ARGS="<music folder> --limit 100"` scores it against Serato's grids.

The Rekordbox XML export (**File → Export Collection in xml format**) is only needed for the XML tools and as a fallback.

### Use it from Claude Code

From the repository root:

```bash
claude mcp add --scope local cuebridge -e "PYTHONPATH=$(pwd)/src" -- "$(pwd)/.venv/bin/python" -m cuebridge.mcp_server
```

Any MCP client can run the same stdio command. Claude Desktop is untested.

### Use the built-in agent

Set `OPENAI_API_KEY` in your shell or in `.env` (ignored by git, but Dropbox will sync it). `CUEBRIDGE_MODEL` optionally picks the model. Then run `make agent`. Type at the `>` prompt; the up arrow recalls earlier requests, and `exit` quits.

## Example prompts

> Are Apple Music, Rekordbox and Serato in sync?

> Load my Apple Music library and tell me which tracks are in no playlist.

> Do a dry run of sync_cues_to_serato and show me which files would change.

## Safety

The tools that change something follow one pattern:

- **Dry run by default.** Nothing is written until `dry_run=false`.
- **Guarded writes.** Edits to Rekordbox's database and Serato's tags require Rekordbox or Serato DJ Pro (respectively) to be closed, keep a backup or the original bytes, write a journal, read the result back, and can be undone. Most also require the counts or files from the dry run.
- **The XML export** only edits the XML file you import into Rekordbox. It has no undo tool.
- **Approval.** The built-in agent refuses a real change unless the same call's dry run happened before your latest message. It then asks Yes or No, starting on No, and shows what that dry run said. This is enforced in Python (`agent/runner.py`, `agent/previews.py`); neither the model nor the screen can skip it. In other clients, you rely on that client's permission prompts.

Rekordbox opening a database with cues added by the two cue tools hasn't been checked in the app yet.

## How it works

There are three processes:

```
┌─ Screen (Node, Ink) ─┐        ┌─ Agent (Python) ─────┐        ┌─ MCP server (Python) ─┐
│ src/ui/              │ JSON   │ agent/runner.py      │  MCP   │ mcp_server.py         │
│ draws, takes keys    │◄──────►│ model loop, history, │◄──────►│ tools/ → services/    │
│                      │ lines  │ approval rule        │ stdio  │ does the real work    │
└──────────────────────┘        └──────────┬───────────┘        └───────────┬───────────┘
                                           │ HTTPS                          │
                                      OpenAI API                 Music, Rekordbox, audio files
```

**The model chooses, the agent gates, the server acts and the screen displays.** With Claude Code, it replaces the screen and the agent; the server is the same.

### From prompt to answer

1. The screen sends your message to the agent as one line of JSON (`agent/protocol.py` lists every message).
2. The agent calls the model with its prompt (the shared rules in `instructions.py`, which the MCP server also sends, plus two of its own), the conversation (including earlier tool results) and every tool's name, description and argument schema.
3. The model picks a tool. There is no router in our code: it chooses from the **docstrings** and **type hints**.
4. The agent pauses every tool not marked read-only, then reads the arguments. Dry runs continue silently. A real change (`dry_run=false`) is refused unless its dry run came before your latest message; otherwise it asks you. Unreadable arguments are refused.
5. The server runs it: a thin function in `tools/` calls the logic in `services/`. The result goes back to the model.
6. The model calls another tool or answers (at most 40 model turns per run). The reply arrives all at once, with no streaming.

The screen turns events into state (`lib/state.ts`) and draws it: a spinner, a line per tool with its arguments, time and ✓ or ✗, the approval panel, and the reply as Markdown.

### Current limitations of the agent

- Apart from the dry-run result in the approval panel, the screen never receives tool results, so what you read in a reply is the model's retelling.
- That panel shows what the dry run said at the time, shortened. The tools' own expected counts are what catch a library that changed since.
- If a turn fails, it's left out of the history, so the model forgets any write made in it (and any dry run, which then has to be repeated).
- History is never trimmed or saved. SDK tracing is off.

### Where to look

| Problem | Look in |
| --- | --- |
| Wrong tool or arguments | tool docstrings, type hints, `instructions.py` |
| Approval behaviour | `agent/runner.py` and the tool's annotations |
| Wrong result | `tools/`, then `services/` |
| Wrong display | `src/ui/lib/state.ts` and `src/ui/components/` |

### Design choices

- **Read live, keep no copy** of the library. Each fact has one owner: Apple Music for tracks and playlists, Rekordbox for cues.
- **Only playlisted tracks matter** when comparing or transferring to Rekordbox.
- **Edit the database, not the XML**, because an XML import can create a playlist but not update one.
- **Never guess.** Tracks are matched by path, then file name, then title, artist and duration; unclear matches are only reported.
- **MP3 timing** differs between Rekordbox and Serato depending on the file's headers, so the cue sync corrects it per file.

## Layout

```
src/cuebridge/   tools/ (MCP tools), services/ (logic, by area), agent/, mcp_server.py, models.py
src/ui/          the agent's screen (Ink, TypeScript)
tests/           mirrors src/cuebridge; never touches the real Rekordbox database
evals/           beatgrid accuracy against Serato's grids
docs/ROADMAP.md  status and next steps
```
