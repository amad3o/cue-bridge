"""Beatgrid analysis on synthetic tracks whose BPM and first beat are known."""

import numpy as np
import pytest

pytest.importorskip("librosa")
soundfile = pytest.importorskip("soundfile")

from cuebridge.services.beatgrid import analyzer  # noqa: E402

RATE = 22050


def kick(length: int = 4000) -> np.ndarray:
    t = np.arange(length) / RATE
    return np.sin(2 * np.pi * (110 * np.exp(-t * 18) + 40) * t) * np.exp(-t * 28)


def hat(length: int = 900, seed: int = 1) -> np.ndarray:
    noise = np.random.default_rng(seed).standard_normal(length)
    return np.diff(noise, prepend=0) * np.exp(-np.arange(length) / RATE * 220) * 0.25


def render(onsets_s, seconds: float, with_hats: bool = False) -> np.ndarray:
    out = np.zeros(int(seconds * RATE) + 8000)
    sound = kick()

    for onset in onsets_s:
        i = int(round(onset * RATE))
        out[i : i + len(sound)] += sound

        if with_hats:
            half = i + int(round(60 / 128 / 2 * RATE))
            out[half : half + 900] += hat()

    return (out / max(1.0, np.max(np.abs(out))) * 0.8)[: int(seconds * RATE)]


def write(tmp_path, samples, name="track.wav"):
    path = tmp_path / name
    soundfile.write(str(path), samples, RATE)
    return path


def constant_track(bpm: float, offset_s: float, seconds: float = 40.0, with_hats: bool = False) -> np.ndarray:
    period = 60.0 / bpm
    return render(offset_s + period * np.arange(int((seconds - offset_s) / period)), seconds, with_hats)


@pytest.mark.parametrize("bpm,offset", [(128.0, 0.2), (124.0, 0.0), (120.0, 0.37), (174.0, 0.05)])
def test_finds_bpm_and_first_beat(tmp_path, bpm, offset):
    result = analyzer.analyze_file(write(tmp_path, constant_track(bpm, offset)))

    assert result.bpm == pytest.approx(bpm, abs=0.05)
    assert result.steady
    assert result.confidence > 0.8
    # the first grid beat is within one beat of the start: compare modulo the period
    period_ms = 60000.0 / bpm
    error = (result.first_beat_ms - offset * 1000.0 + period_ms / 2) % period_ms - period_ms / 2
    assert abs(error) < 12.0


@pytest.mark.parametrize("bpm", [75.0, 85.0, 140.0, 150.0])
def test_does_not_pick_half_or_double_tempo(tmp_path, bpm):
    result = analyzer.analyze_file(write(tmp_path, constant_track(bpm, 0.1)))

    assert result.bpm == pytest.approx(bpm, abs=0.1)


def test_off_beat_hats_do_not_confuse_it(tmp_path):
    result = analyzer.analyze_file(write(tmp_path, constant_track(128.0, 0.15, with_hats=True)))

    assert result.bpm == pytest.approx(128.0, abs=0.05)
    assert result.steady


def test_a_tempo_ramp_is_not_steady(tmp_path):
    # 120 BPM speeding up to 134 over the track: no constant grid fits
    onsets, now, seconds = [], 0.2, 50.0

    while now < seconds - 1:
        onsets.append(now)
        now += 60.0 / (120.0 + 14.0 * now / seconds)

    result = analyzer.analyze_file(write(tmp_path, render(onsets, seconds)))

    assert not result.steady


def test_noise_is_not_trusted(tmp_path):
    noise = np.random.default_rng(3).standard_normal(RATE * 30) * 0.1
    result = analyzer.analyze_file(write(tmp_path, noise))

    assert not result.steady or result.confidence < 0.5


def test_silence_is_refused(tmp_path):
    with pytest.raises(analyzer.AnalysisError, match="silence"):
        analyzer.analyze_file(write(tmp_path, np.zeros(RATE * 20)))


def test_a_short_file_is_refused(tmp_path):
    with pytest.raises(analyzer.AnalysisError, match="too short"):
        analyzer.analyze_file(write(tmp_path, constant_track(128.0, 0.0, seconds=5.0)))


def test_an_unreadable_file_is_refused(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not audio")

    with pytest.raises(analyzer.AnalysisError, match="couldn't read"):
        analyzer.analyze_file(path)


def test_the_first_beat_is_never_before_the_start_of_the_file(tmp_path):
    result = analyzer.analyze_file(write(tmp_path, constant_track(124.0, 0.0)))

    assert 0.0 <= result.first_beat_ms < 60000.0 / 124.0


@pytest.mark.parametrize("first,period,expected", [(-0.0007, 0.5, 0.0), (-0.1, 0.5, 0.4), (0.7, 0.5, 0.2), (0.2, 0.5, 0.2)])
def test_inside_the_file(first, period, expected):
    assert analyzer._inside_the_file(first, period) == pytest.approx(expected)


def test_the_file_is_not_modified(tmp_path):
    path = write(tmp_path, constant_track(128.0, 0.0))
    before = path.read_bytes()
    analyzer.analyze_file(path)

    assert path.read_bytes() == before
