import pytest

pytest.importorskip("librosa")
soundfile = pytest.importorskip("soundfile")

from test_analyzer import constant_track, write  # noqa: E402

from cuebridge import mcp_server  # noqa: E402
from cuebridge.services.rekordbox import db as rekordbox_db  # noqa: E402
from cuebridge.services.rekordbox.db import DbContent, DbPlaylist, DbSnapshot  # noqa: E402
from cuebridge.tools import beatgrid as tool  # noqa: E402


def test_analyses_the_files_it_is_given(tmp_path):
    good = write(tmp_path, constant_track(126.0, 0.1), "good.wav")
    short = write(tmp_path, constant_track(126.0, 0.0, seconds=4.0), "short.wav")
    result = mcp_server.analyze_beatgrid(paths=[str(good), str(short), str(tmp_path / "missing.wav")])

    assert result["analysed"] == 3 and result["usable"] == 1 and result["couldNotAnalyse"] == 2
    first = result["tracks"][0]
    assert first["bpm"] == pytest.approx(126.0, abs=0.05) and first["verdict"] == "usable"
    assert "too short" in result["tracks"][1]["error"]
    assert "Nothing was written" in result["summary"]


def test_paging_over_the_files(tmp_path):
    paths = [str(write(tmp_path, constant_track(120.0 + i, 0.0, seconds=12.0), f"{i}.wav")) for i in range(3)]
    page = mcp_server.analyze_beatgrid(paths=paths, limit=2)

    assert page["analysed"] == 2 and page["hasMore"] and "offset=2" in page["summary"]
    assert mcp_server.analyze_beatgrid(paths=paths, limit=2, offset=2)["analysed"] == 1


def test_a_steady_but_unclear_track_is_left_alone():
    class Result:
        steady, confidence = True, 0.3

    assert tool._verdict(Result()).startswith("leave alone")


def test_takes_playlisted_tracks_without_a_grid_from_the_database(tmp_path, monkeypatch):
    audio = write(tmp_path, constant_track(124.0, 0.2), "a.wav")
    other = write(tmp_path, constant_track(124.0, 0.2), "b.wav")
    snapshot = DbSnapshot(
        (
            DbContent("1", "In playlist", "X", str(audio), 40000, analysis_path="/a/1"),
            DbContent("2", "In no playlist", "X", str(other), 40000, analysis_path="/a/2"),
        ),
        (DbPlaylist("p", "p", None, False, False, (), ("s1",), ("1",)),),
    )
    monkeypatch.setattr(rekordbox_db, "read_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(rekordbox_db, "default_backend", lambda: None)
    monkeypatch.setattr(tool, "db_target", lambda p: tmp_path / "master.db")
    monkeypatch.setattr(tool.db_library, "read_first_beat_ms", lambda analysis: 210 if analysis == "/a/1" else None)

    default = mcp_server.analyze_beatgrid()
    assert default["toAnalyse"] == 0 and default["skippedForAlreadyHavingAGrid"] == 1 and default["skippedForBeingInNoPlaylist"] == 1

    compared = mcp_server.analyze_beatgrid(include_with_grid=True)
    assert compared["analysed"] == 1
    track = compared["tracks"][0]
    assert track["track"] == "In playlist" and track["rekordboxFirstBeatMs"] == 210
    assert abs(track["offsetFromRekordboxMs"]) < 15
