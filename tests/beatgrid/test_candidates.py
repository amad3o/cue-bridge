from cuebridge.services.beatgrid import candidates
from cuebridge.services.rekordbox.db import DbContent, DbPlaylist, DbSnapshot


def content(id, title, path="/m/a.wav", analysis="/a/1"):
    return DbContent(id=id, title=title, artist=None, path=path, duration_ms=200000, analysis_path=analysis)


def playlist(*ids, smart=False, folder=False):
    return DbPlaylist("p", "p", None, folder, smart, (), tuple(f"s{i}" for i in ids), tuple(ids))


GRIDS = {"/a/with": 120}


def pick(snapshot, **kwargs):
    return candidates.select_candidates(snapshot, GRIDS.get, file_exists=lambda p: "gone" not in p, **kwargs)


def test_playlisted_tracks_without_a_grid_are_kept():
    snapshot = DbSnapshot((content("1", "A", analysis="/a/none"), content("2", "B", analysis="/a/none")), (playlist("1"),))
    chosen = pick(snapshot)

    assert [c.id for c in chosen.tracks] == ["1"]
    assert chosen.not_playlisted == 1


def test_smart_playlists_and_folders_do_not_count_as_playlisted():
    snapshot = DbSnapshot((content("1", "A", analysis="/a/none"),), (playlist("1", smart=True), playlist("1", folder=True)))

    assert pick(snapshot).tracks == []


def test_tracks_with_a_grid_are_skipped_unless_asked():
    snapshot = DbSnapshot((content("1", "A", analysis="/a/with"),), (playlist("1"),))

    assert pick(snapshot).tracks == []
    assert pick(snapshot).already_gridded == 1

    chosen = pick(snapshot, include_with_grid=True)
    assert [c.id for c in chosen.tracks] == ["1"]
    assert chosen.existing_first_beat_ms == {"1": 120}


def test_a_missing_file_is_counted_not_listed():
    snapshot = DbSnapshot((content("1", "A", path="/m/gone.wav", analysis="/a/none"), content("2", "B", path=None, analysis="/a/none")), (playlist("1", "2"),))
    chosen = pick(snapshot)

    assert chosen.tracks == [] and chosen.file_missing == 2


def test_everything_when_not_limited_to_playlists():
    snapshot = DbSnapshot((content("1", "A", analysis="/a/none"),), ())

    assert [c.id for c in pick(snapshot, playlisted_only=False).tracks] == ["1"]


def test_the_order_is_stable_for_paging():
    snapshot = DbSnapshot(tuple(content(str(i), t, analysis="/a/none") for i, t in [(1, "B"), (2, "A"), (3, "C")]), (playlist("1", "2", "3"),))

    assert [c.title for c in pick(snapshot).tracks] == ["A", "B", "C"]


def test_offset_from_existing_folds_to_half_a_beat():
    assert candidates.offset_from_existing(130.0, 100.0, 120.0) == 30.0
    assert candidates.offset_from_existing(100.0, 130.0, 120.0) == -30.0
    assert candidates.offset_from_existing(620.0, 100.0, 120.0) == 20.0  # a beat (500 ms) later: the same grid
