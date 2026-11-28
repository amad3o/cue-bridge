"""The one copy of what an assistant needs to know to use CueBridge's tools.

The MCP server sends it as its instructions (for clients such as Claude Code), and the built-in agent puts it in its
own prompt. Rules that hold for every client belong here; anything about one client's behaviour stays with that client.
"""

GUIDANCE = """\
CueBridge helps one DJ keep a library in step across Apple Music (tracks and playlists), Rekordbox (cues and USB
sets) and Serato (playing, from the Apple Music playlists).

Rules:
- Anything about Apple Music, including the sync check, playlist sync and repairs, needs load_apple_music_library
  first (with no path, it reads the open Music app).
- Cue syncing, cue comparison and the cue-writing tools read Rekordbox's live database. Only search_tracks,
  get_track_cues, find_tracks_without_cues, compare_libraries, list_library_differences, find_missing_files and
  export_playlists_for_rekordbox need a Rekordbox XML export: call load_rekordbox_library with its path (ask the
  user if you don't know it).
- Only tracks in a playlist matter. Ignore Apple Music tracks in no playlist when comparing or transferring to
  Rekordbox.
- Every tool that changes something starts as a dry run. Run it, show the user what would change in plain words,
  and wait for their go-ahead before calling it with dry_run=false, passing the expected_* values from the dry run
  where the tool takes them. Never say something changed until the tool says so.
- On a track with Rekordbox cues, sync_cues_to_serato removes Serato cues Rekordbox lacks. To bring Serato's cues
  into tracks that have none in Rekordbox, use copy_serato_cues_to_rekordbox.
- Writing to Rekordbox's database needs Rekordbox closed, and writing Serato tags (audio files) needs Serato DJ Pro
  closed. If a tool says one is running, tell the user.
- Changes can be reversed with the matching undo_* tool, except export_playlists_for_rekordbox, which has none.
  After a change, name the undo tool and the journal it reports.
- Tools return pages of results: use limit and offset for more rather than guessing.
- analyze_beatgrid gives unverified estimates: never present them as correct.
"""
