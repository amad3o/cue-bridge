import hashlib
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from apple_helpers import music_track, playlist, write_library
from cuebridge import mcp_server as server
from cuebridge.parsers.apple_music_xml import parse_apple_music_plist
from cuebridge.parsers.paths import location_to_path
from cuebridge.parsers.rekordbox_parser import parse_rekordbox_playlists
from cuebridge.services import apple_music_service, drift, playlist_export as pe, rekordbox_editor, rekordbox_service
from cuebridge.services.rekordbox_editor import OutputPathError
from cuebridge.services.rekordbox_service import clear_cache, get_rekordbox_library, parse_rekordbox_bytes
from test_drift import rb_playlist_xml, rb_track_xml, rb_xml
from apple_helpers import library_bytes

ON_DISK = {"/Music/a.mp3", "/Music/b.mp3", "/Music/d & co.mp3", "/Music/e.mp3", "/Music/new name.mp3"}


def exists(path: str) -> bool:
    return path in ON_DISK


def apple_tracks():
    return [
        music_track(1, "A", "X", "/Music/a.mp3"),
        music_track(2, "B", "X", "/Music/b.mp3"),
        music_track(3, "B", "X", "/Music/b.mp3"),  # the same file listed again
        music_track(4, "D", "Dee & Co", "/Music/d & co.mp3", BPM=128, Genre="house", Year=2024, Album="LP", Size=5_000_000),
        music_track(5, "E", "X", "/Music/e.mp3"),
    ]


def apple_playlists():
    return [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5, 3), playlist("OnlyApple", 1)]


@pytest.fixture
def world(tmp_path):
    tracks = (
        rb_track_xml(10, "A", "X", "/Music/a.mp3") + rb_track_xml(20, "B", "X", "/Music/b.mp3", cue=True)
        + rb_track_xml(30, "E", "X", "/Music/old name.mp3") + rb_track_xml(40, "Lost", "X", "/Music/lost.mp3")
    )
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(rb_xml(
        tracks,
        rb_playlist_xml("Set", 10, 20) + rb_playlist_xml("Warm", 20, 30, 40) + rb_playlist_xml("Extra", 10),
    ))
    clear_cache()
    music = parse_apple_music_plist(library_bytes(apple_tracks(), apple_playlists()))
    rekordbox = get_rekordbox_library(path)

    return music, rekordbox, drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)


def plan(world, **kwargs):
    music, rekordbox, result = world
    return pe.plan_export(rekordbox, music, result, exists=exists, **kwargs)


class TestPlanning:
    def test_by_default_only_playlists_that_differ_are_included(self, world):
        result = plan(world)

        assert [(p.name, p.reason) for p in result.playlists] == [
            ("Warm", "differs from Rekordbox"), ("OnlyApple", "not in Rekordbox yet"),
        ]
        assert result.in_sync == ["Set"]

    def test_tracks_keep_their_rekordbox_ids_and_new_ones_get_new_keys(self, world):
        result = plan(world)

        warm = result.playlists[0]
        assert warm.keys == ("20", str(pe.NEW_TRACK_ID_BASE + 4), "30")  # the duplicate entry of B is dropped
        assert result.playlists[1].keys == ("10",)
        assert result.existing_ids == ["20", "30", "10"]
        assert [(k, t.title) for k, t in result.new_tracks] == [(str(pe.NEW_TRACK_ID_BASE + 4), "D")]
        assert result.duplicates_removed == 1

    def test_a_moved_file_gets_apple_music_s_path(self, world):
        assert plan(world).relocated == {"30": "/Music/e.mp3"}  # Rekordbox has the old name, which is gone

    def test_named_playlists_ignore_case_and_the_sync_state(self, world):
        result = plan(world, names=["set"])

        assert [p.name for p in result.playlists] == ["Set"] and result.in_sync == []
        assert [p.reason for p in result.playlists] == ["requested"]

    def test_an_unknown_name_lists_the_real_ones(self, world):
        with pytest.raises(pe.ExportError, match="Warm"):
            plan(world, names=["nope"])

    def test_all_playlists(self, world):
        assert [p.name for p in plan(world, all_playlists=True).playlists] == ["Set", "Warm", "OnlyApple"]

    def test_a_new_track_whose_file_is_missing_is_left_out_and_listed(self, world):
        ON_DISK.discard("/Music/d & co.mp3")
        try:
            result = plan(world)
        finally:
            ON_DISK.add("/Music/d & co.mp3")

        assert result.playlists[0].keys == ("20", "30")
        assert result.skipped == [("Warm", "D", "file not found")]
        assert result.new_tracks == []

    def test_a_new_track_is_added_once_across_playlists(self, world):
        music, rekordbox, result = world
        music2 = parse_apple_music_plist(library_bytes(apple_tracks(), apple_playlists() + [playlist("Warm 2", 4)]))
        result2 = drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music2)

        out = pe.plan_export(rekordbox, music2, result2, exists=exists)

        assert len(out.new_tracks) == 1 and out.playlists[-1].keys == (str(pe.NEW_TRACK_ID_BASE + 4),)


class TestBuilding:
    def build(self, world, **kwargs):
        music, rekordbox, _ = world
        result = plan(world, **kwargs)
        data = pe.build_xml(result, pe.export_source_bytes(rekordbox))
        pe.verify_xml(data, result)
        return data, result

    def test_the_file_has_the_tracks_and_the_playlists(self, world):
        data, _ = self.build(world)

        tracks = {t.trackId: t for t in parse_rekordbox_bytes(data)}
        playlists = parse_rekordbox_playlists(data)

        assert set(tracks) == {"10", "20", "30", str(pe.NEW_TRACK_ID_BASE + 4)}
        assert [(p.name, p.folders) for p in playlists] == [("Warm", ("Apple Music",)), ("OnlyApple", ("Apple Music",))]
        assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n' + pe.WRITTEN_MARKER)

    def test_rekordbox_tracks_are_copied_with_their_cues(self, world):
        data, _ = self.build(world)

        copied = {t.trackId: t for t in parse_rekordbox_bytes(data)}["20"]
        original = world[1].tracks_by_id["20"]

        assert copied.cues == original.cues and len(copied.cues) == 1

    def test_a_moved_file_gets_its_new_location(self, world):
        data, _ = self.build(world)

        assert {t.trackId: t for t in parse_rekordbox_bytes(data)}["30"].filePath == "/Music/e.mp3"

    def test_a_new_track_is_built_from_apple_music_s_data(self, world):
        data, _ = self.build(world)

        new = {t.trackId: t for t in parse_rekordbox_bytes(data)}[str(pe.NEW_TRACK_ID_BASE + 4)]

        assert (new.trackTitle, new.artist, new.filePath) == ("D", "Dee & Co", "/Music/d & co.mp3")
        assert b'AverageBpm="128.00"' in data and b'Genre="house"' in data and b'Kind="MP3 File"' in data
        assert b"/Music/d%20%26%20co.mp3" in data  # special characters are percent-encoded

    def test_a_folder_name_can_be_chosen(self, world):
        music, rekordbox, _ = world
        data = pe.build_xml(plan(world), pe.export_source_bytes(rekordbox), folder_name="From Apple")

        assert parse_rekordbox_playlists(data)[0].folders == ("From Apple",)

    def test_verification_catches_a_wrong_file(self, world):
        data, result = self.build(world)
        other = plan(world, all_playlists=True)

        with pytest.raises(pe.ExportError):
            pe.verify_xml(data, other)


class TestOutputPath:
    def test_the_rekordbox_export_is_never_the_target(self, world):
        with pytest.raises(OutputPathError, match="Rekordbox export itself"):
            pe.check_output_path(world[1].path, world[1])

    def test_only_xml_in_an_existing_folder(self, world, tmp_path):
        with pytest.raises(OutputPathError, match=".xml"):
            pe.check_output_path(tmp_path / "x.txt", world[1])

        with pytest.raises(OutputPathError, match="does not exist"):
            pe.check_output_path(tmp_path / "nope" / "x.xml", world[1])

    def test_a_foreign_file_is_not_replaced_but_our_own_is(self, world, tmp_path):
        theirs, ours = tmp_path / "theirs.xml", tmp_path / "ours.xml"
        theirs.write_bytes(b"<DJ_PLAYLISTS/>")
        ours.write_bytes(b'<?xml version="1.0"?>\n' + pe.WRITTEN_MARKER + b"\n<DJ_PLAYLISTS/>")

        with pytest.raises(OutputPathError, match="wasn't written by CueBridge"):
            pe.check_output_path(theirs, world[1])

        assert pe.check_output_path(ours, world[1]) == ours

    def test_the_default_sits_next_to_the_rekordbox_export(self, world):
        assert pe.default_output_path(world[1]) == world[1].path.with_name("cuebridge-playlists.xml")


class TestTool:
    @pytest.fixture(autouse=True)
    def clean(self):
        clear_cache()
        apple_music_service.clear_cache()
        server.reset_state()
        yield
        clear_cache()
        apple_music_service.clear_cache()
        server.reset_state()

    @pytest.fixture
    def loaded(self, tmp_path, monkeypatch):
        a, b, e, d = (tmp_path / n for n in ("a.mp3", "b.mp3", "e.mp3", "d & co.mp3"))
        for f in (a, b, e, d):
            f.write_bytes(b"x")

        rb_path = tmp_path / "rekordbox.xml"
        rb_path.write_bytes(rb_xml(
            rb_track_xml(10, "A", "X", a) + rb_track_xml(20, "B", "X", b, cue=True) + rb_track_xml(30, "E", "X", e),
            rb_playlist_xml("Set", 10, 20) + rb_playlist_xml("Warm", 20, 30),
        ))
        apple = write_library(
            tmp_path / "Library.xml",
            [music_track(1, "A", "X", a), music_track(2, "B", "X", b), music_track(4, "D", "Dee", d), music_track(5, "E", "X", e)],
            [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5)],
        )
        server.load_rekordbox_library(str(rb_path))
        server.load_apple_music_library(str(apple))
        return tmp_path

    def test_a_dry_run_writes_nothing(self, loaded):
        export = loaded / "rekordbox.xml"
        before = export.read_bytes()

        result = server.export_playlists_for_rekordbox()

        assert result["dryRun"] is True and export.read_bytes() == before
        assert not list(loaded.glob("*.backup-*")) and not (loaded / "cuebridge-playlists.xml").exists()
        assert [p["name"] for p in result["playlists"]] == ["Warm"]
        assert result["newTracksForRekordbox"] == 1 and result["newTrackExamples"] == ["Dee - D"]
        assert result["alreadyInSync"] == ["Set"] and "Nothing has been written" in result["summary"]
        assert result["backupWillBeSaved"] is True and result["mode"] == "added to the Rekordbox XML"

    def test_by_default_the_playlists_go_into_the_rekordbox_xml_after_a_backup(self, loaded):
        export = loaded / "rekordbox.xml"
        original = export.read_bytes()

        result = server.export_playlists_for_rekordbox(dry_run=False)

        (backup,) = loaded.glob("rekordbox.xml.backup-*")
        assert backup.read_bytes() == original and result["backup"] == str(backup)
        data = export.read_bytes()
        assert b"<!--" not in data  # Rekordbox's file gets no markers; the state lives in a side file
        assert rekordbox_service.pending_note_path(export).is_file()
        assert not (loaded / "cuebridge-playlists.xml").exists()
        folder = [p for p in parse_rekordbox_playlists(data) if p.folders == ("Apple Music",)]
        assert [p.name for p in folder] == ["Warm"]
        assert "Import Playlist" in result["summary"]

    def test_the_existing_library_is_untouched_and_the_additions_are_pending(self, loaded):
        before = {t.trackId: t for t in get_rekordbox_library(loaded / "rekordbox.xml").tracks}
        playlists_before = [(p.name, p.track_ids) for p in get_rekordbox_library(loaded / "rekordbox.xml").playlists]

        server.export_playlists_for_rekordbox(dry_run=False)
        after = get_rekordbox_library(loaded / "rekordbox.xml")

        # What Rekordbox really has hasn't changed, so the new track and folder don't count yet...
        assert {t.trackId: t for t in after.tracks} == before
        assert [(p.name, p.track_ids) for p in after.playlists] == playlists_before
        assert after.pending_folder == "Apple Music"
        # ...but they are in the file for Rekordbox to import.
        raw = parse_rekordbox_bytes((loaded / "rekordbox.xml").read_bytes())
        assert len(raw) == len(before) + 1

        server.load_rekordbox_library(str(loaded / "rekordbox.xml"))
        assert server.compare_libraries()["playlists"]["differ"] == 1  # Warm still differs until imported

    def test_running_again_replaces_the_folder_and_makes_no_second_backup(self, loaded):
        server.export_playlists_for_rekordbox(dry_run=False)
        first = (loaded / "rekordbox.xml").read_bytes()

        again = server.export_playlists_for_rekordbox(dry_run=False)

        assert (loaded / "rekordbox.xml").read_bytes() == first
        assert again["backup"] is None and len(list(loaded.glob("rekordbox.xml.backup-*"))) == 1
        assert again["replacesEarlierFolder"] is True

    def test_a_different_folder_name_replaces_the_earlier_one(self, loaded):
        server.export_playlists_for_rekordbox(dry_run=False)

        server.export_playlists_for_rekordbox(folder_name="From Apple", dry_run=False)

        folders = {p.folders for p in parse_rekordbox_playlists((loaded / "rekordbox.xml").read_bytes()) if p.folders}
        assert folders == {("From Apple",)}

    def test_a_real_folder_with_that_name_is_never_touched(self, loaded):
        export = loaded / "rekordbox.xml"
        export.write_bytes(export.read_bytes().replace(
            b"</NODE></PLAYLISTS>", b'<NODE Type="0" Name="Apple Music" Count="0"></NODE></NODE></PLAYLISTS>'
        ))
        before = export.read_bytes()

        with pytest.raises(ToolError, match="didn't add"):
            server.export_playlists_for_rekordbox(dry_run=False)

        assert export.read_bytes() == before

    def test_a_file_that_changed_after_planning_is_not_overwritten(self, loaded):
        rekordbox = get_rekordbox_library(loaded / "rekordbox.xml")
        music = apple_music_service.get_apple_music_library(loaded / "Library.xml")
        result = drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)
        file_plan = pe.build_file_plan(pe.plan_export(rekordbox, music, result), rekordbox)
        (loaded / "rekordbox.xml").write_bytes(b"<DJ_PLAYLISTS><COLLECTION/></DJ_PLAYLISTS>")

        with pytest.raises(OutputPathError, match="changed since"):
            rekordbox_editor.write_plan(file_plan)

    def test_a_separate_file_leaves_the_rekordbox_export_alone(self, loaded):
        export = loaded / "rekordbox.xml"
        before = hashlib.sha256(export.read_bytes()).hexdigest()
        target = loaded / "cuebridge-playlists.xml"

        result = server.export_playlists_for_rekordbox(output_path=str(target), dry_run=False)

        assert target.is_file() and pe.WRITTEN_MARKER in target.read_bytes()
        assert [p.name for p in parse_rekordbox_playlists(target.read_bytes())] == ["Warm"]
        assert hashlib.sha256(export.read_bytes()).hexdigest() == before and not list(loaded.glob("*.backup-*"))
        assert result["mode"] == "separate file" and "Import Playlist" in result["summary"]

    def test_a_separate_file_is_replaced_when_run_again(self, loaded):
        target = str(loaded / "cuebridge-playlists.xml")
        server.export_playlists_for_rekordbox(output_path=target, dry_run=False)

        again = server.export_playlists_for_rekordbox(output_path=target, dry_run=False)

        assert again["wouldReplaceEarlierFile"] is True and (loaded / "cuebridge-playlists.xml").is_file()

    def test_it_will_not_replace_a_file_it_did_not_write(self, loaded):
        target = loaded / "mine.xml"
        target.write_text("<keep/>")

        with pytest.raises(ToolError, match="wasn't written by CueBridge"):
            server.export_playlists_for_rekordbox(output_path=str(target), dry_run=False)

        assert target.read_text() == "<keep/>"

    def test_nothing_to_export_when_everything_matches(self, loaded):
        in_sync = write_library(
            loaded / "Synced.xml",
            [music_track(1, "A", "X", loaded / "a.mp3"), music_track(2, "B", "X", loaded / "b.mp3")],
            [playlist("Set", 1, 2)],
        )
        server.load_apple_music_library(str(in_sync))

        result = server.export_playlists_for_rekordbox(dry_run=False)

        assert result["playlists"] == [] and result["summary"].startswith("Nothing to export")
        assert not (loaded / "cuebridge-playlists.xml").exists()

    def test_named_playlists_are_exported_even_when_in_sync(self, loaded):
        result = server.export_playlists_for_rekordbox(playlists=["Set"])

        assert [p["name"] for p in result["playlists"]] == ["Set"] and result["newTracksForRekordbox"] == 0

    def test_both_libraries_are_needed(self, tmp_path):
        with pytest.raises(ToolError, match="load_rekordbox_library"):
            server.export_playlists_for_rekordbox()


class TestStaleNote:
    @pytest.fixture(autouse=True)
    def clean(self):
        clear_cache()
        yield
        clear_cache()

    def test_a_fresh_export_over_the_file_makes_the_note_stale(self, tmp_path):
        path = tmp_path / "rekordbox.xml"
        original = rb_xml(rb_track_xml(10, "A", "X", "/Music/a.mp3"), rb_playlist_xml("Set", 10))
        path.write_bytes(original)
        written = original.replace(b"</NODE></PLAYLISTS>", b'<NODE Type="0" Name="Apple Music" Count="0"></NODE></NODE></PLAYLISTS>')
        path.write_bytes(written)
        rekordbox_service.write_pending_note(path, "Apple Music", written)
        clear_cache()

        assert get_rekordbox_library(path).pending_folder == "Apple Music"

        path.write_bytes(written + b"\n")  # Rekordbox exported again, or anything else touched the file
        clear_cache()

        library = get_rekordbox_library(path)
        assert library.pending_folder is None and any(p.folders == ("Apple Music",) or p.name == "Set" for p in library.playlists)

    def test_a_missing_or_broken_note_means_nothing_is_pending(self, tmp_path):
        path = tmp_path / "rekordbox.xml"
        path.write_bytes(rb_xml("", ""))

        assert get_rekordbox_library(path).pending_folder is None

        rekordbox_service.pending_note_path(path).write_text("not json")
        clear_cache()

        assert get_rekordbox_library(path).pending_folder is None


REAL_STYLE = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n<DJ_PLAYLISTS Version="1.0.0">\r\n'
    '  <PRODUCT Name="rekordbox" Version="7.2.19" Company="AlphaTheta"/>\r\n'
    '  <COLLECTION Entries="2">\r\n'
    '    <TRACK TrackID="10" Name="It&apos;s A &amp; B" Artist="X" Composer="" Album=""\r\n'
    '           Grouping="" Genre="" Kind="MP3 File" Size="1" TotalTime="5"\r\n'
    '           Location="file://localhost/Music/old.mp3"\r\n'
    '           Remixer="" Tonality="" Label="" Mix="">\r\n'
    '      <TEMPO Inizio="0.437" Bpm="137.00" Metro="4/4" Battito="2"/>\r\n'
    '    </TRACK>\r\n'
    '    <TRACK TrackID="20" Name="B" Artist="X" Location="file://localhost/Music/b.mp3"/>\r\n'
    '  </COLLECTION>\r\n'
    '  <PLAYLISTS>\r\n'
    '    <NODE Type="0" Name="ROOT" Count="2">\r\n'
    '      <NODE Name="CUE Analysis Playlist" Type="1" KeyType="0" Entries="0"/>\r\n'
    '      <NODE Name="Set" Type="1" KeyType="0" Entries="2">\r\n'
    '        <TRACK Key="10"/>\r\n'
    '        <TRACK Key="20"/>\r\n'
    '      </NODE>\r\n'
    '    </NODE>\r\n'
    '  </PLAYLISTS>\r\n'
    '</DJ_PLAYLISTS>\r\n'
).encode()


class TestPatchingKeepsTheFileAsItIs:
    """Rekordbox rejected an XML library's re-serialisation of its file, so only new lines may be added."""

    def make_plan(self, **kwargs):
        track = parse_apple_music_plist(library_bytes([
            music_track(7, "Don't & Stop", "A&B", "/Music/new.mp3", BPM=120),
        ])).tracks[0]
        plan = pe.ExportPlan(
            playlists=[pe.PlaylistPlan("Mix", ("10", str(pe.NEW_TRACK_ID_BASE + 7)), "requested")],
            existing_ids=["10"], new_tracks=[(str(pe.NEW_TRACK_ID_BASE + 7), track)], **kwargs,
        )
        return plan

    def test_every_original_byte_is_still_there(self):
        edited = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None)
        text, original = edited.decode(), REAL_STYLE.decode()

        assert edited.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n<DJ_PLAYLISTS Version="1.0.0">\r\n')
        assert b"<!--" not in edited and edited.count(b"\n") == edited.count(b"\r\n")  # no comments, CRLF throughout
        assert pe._strip_additions(text, "Apple Music", {}) == pe._strip_additions(original, None, {})
        assert "\r\n    <TRACK TrackID=\"20\" Name=\"B\"" in text and text.endswith("</DJ_PLAYLISTS>\r\n")

    def test_the_counts_are_kept_up_to_date(self):
        text = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None).decode()

        assert '<COLLECTION Entries="3">' in text and '<NODE Type="0" Name="ROOT" Count="3">' in text

    def test_new_lines_are_in_rekordbox_s_style(self):
        text = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None).decode()

        assert 'Name="Don&apos;t &amp; Stop" Artist="A&amp;B"' in text
        assert '/>\r\n  </COLLECTION>' in text and ' />' not in text
        assert '      <NODE Type="0" Name="Apple Music" Count="1">\r\n        <NODE Name="Mix" Type="1" KeyType="0" Entries="2">\r\n' in text
        assert '          <TRACK Key="10"/>\r\n' in text

    def test_the_result_is_valid_and_reads_back(self):
        edited = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None)

        tracks = {t.trackId: t for t in parse_rekordbox_bytes(edited)}
        folder = [p for p in parse_rekordbox_playlists(edited) if p.folders == ("Apple Music",)]

        assert tracks[str(pe.NEW_TRACK_ID_BASE + 7)].trackTitle == "Don't & Stop"
        assert [(p.name, p.track_ids) for p in folder] == [("Mix", ("10", str(pe.NEW_TRACK_ID_BASE + 7)))]

    def test_a_second_run_replaces_the_first_exactly(self):
        first = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None)

        again = pe.patch_xml(first, self.make_plan(), "Apple Music", "Apple Music")

        assert again == first

    def test_a_different_folder_name_replaces_the_old_folder(self):
        first = pe.patch_xml(REAL_STYLE, self.make_plan(), "Apple Music", None)

        second = pe.patch_xml(first, self.make_plan(), "From Apple", "Apple Music").decode()

        assert 'Name="Apple Music"' not in second and 'Name="From Apple"' in second
        assert second.count('TrackID="9') == 1 and '<NODE Type="0" Name="ROOT" Count="3">' in second

    def test_a_moved_file_only_changes_its_location(self):
        plan = self.make_plan(relocated={"10": "/Music/new place/it's.mp3"})

        text = pe.patch_xml(REAL_STYLE, plan, "Apple Music", None).decode()

        assert 'Location="file://localhost/Music/new%20place/it%27s.mp3"' in text
        assert 'Location="file://localhost/Music/old.mp3"' not in text
        assert pe._strip_additions(text, "Apple Music", plan.relocated) == pe._strip_additions(REAL_STYLE.decode(), None, plan.relocated)

    def test_locations_are_encoded_the_way_rekordbox_does_it(self):
        assert pe._location("/Music/A (b) [c] & d's, e!.mp3") == "file://localhost/Music/A%20(b)%20%5Bc%5D%20%26%20d%27s,%20e!.mp3"

    def test_a_real_folder_with_the_name_is_never_replaced(self):
        original = REAL_STYLE.replace(b"    </NODE>\r\n  </PLAYLISTS>", b'      <NODE Type="0" Name="Apple Music" Count="0"/>\r\n    </NODE>\r\n  </PLAYLISTS>')

        with pytest.raises(pe.ExportError, match="didn't add"):
            pe.patch_xml(original, self.make_plan(), "Apple Music", None)

    def test_a_file_without_a_collection_is_refused(self):
        with pytest.raises(pe.ExportError, match="COLLECTION"):
            pe.patch_xml(b"<DJ_PLAYLISTS></DJ_PLAYLISTS>", self.make_plan(), "Apple Music", None)

    def test_a_relocated_track_that_is_not_in_the_file_is_refused(self):
        with pytest.raises(pe.ExportError, match="wasn't found"):
            pe.patch_xml(REAL_STYLE, self.make_plan(relocated={"99": "/x.mp3"}), "Apple Music", None)
