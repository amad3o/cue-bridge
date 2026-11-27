"""Beatgrid analysis: first beat, BPM, and how far to trust them. Read-only: it never touches the file.

The method (docs/ROADMAP.md, section 13): estimate a tempo from the onset strength inside a sane range, fit a constant grid
to it, then refine the grid on the audio itself (the sharpest attack near each grid beat, then a least-squares line through
those attacks). A track whose beats don't sit on one constant grid (live drums, tempo changes, a long beatless intro) is
flagged as not steady, and the caller should leave it alone rather than write a bad grid.

librosa and soundfile are an optional extra (`pip install -e ".[beatgrid]"`). The MCP server imports this module at
startup, so it must import without them: librosa is imported when a file is analysed, and numpy (which comes with it)
may be missing.
"""

from __future__ import annotations  # the np.ndarray annotations must not need numpy at import time

from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
except ImportError:  # only used after _load, which says how to install the extra
    np = None

# Bump when the method changes: the evaluation caches its scores per version.
VERSION = "8"
SAMPLE_RATE = 22050
MIN_BPM = 70.0
MAX_BPM = 180.0
PREFERRED_BPM = 125.0  # a mild pull towards dance tempos, to settle half/double ambiguity
MIN_SECONDS = 10.0
COARSE_HOP = 256  # samples; about 11.6 ms
FINE_HOP = 64  # samples; about 2.9 ms
FINE_FFT = 512
FLUX_LEAD_MS = -7.7  # the windowed flux peaks this much before the attack (negative: early); measured on synthetic tracks
SEARCH_WINDOW_S = 0.035  # how far from a grid beat an attack may be and still count as that beat
HIT_STRENGTH = 0.25  # an attack counts as a hit when its slope is this fraction of the track's strong attacks
SEGMENTS = 4
DRIFT_LIMIT_MS = 30.0


class AnalysisError(Exception):
    """The file can't be analysed (missing extra, unreadable, too short, or silent)."""


@dataclass(frozen=True)
class BeatgridResult:
    first_beat_ms: float  # the first grid beat, within one beat of the start of the file
    bpm: float
    confidence: float  # 0 to 1: the share of grid beats that land on an attack, reduced by how loosely they land
    steady: bool  # False when the beats don't sit on one constant grid
    spread_ms: float  # how far the grid sits from the beats, across four stretches of the track
    hit_fraction: float
    duration_s: float


def _load(path: Path):
    try:
        import librosa
    except ImportError:
        raise AnalysisError('librosa is not installed. Install the beatgrid extra: pip install -e ".[beatgrid]"') from None

    try:
        samples, _ = librosa.load(str(path), sr=SAMPLE_RATE, mono=True)
    except Exception as error:  # the decoders raise many kinds of error
        raise AnalysisError(f"couldn't read the audio: {error}") from None

    return librosa, samples


def _estimate_bpm(librosa, samples: np.ndarray) -> float:
    """A rough tempo from the autocorrelation of the onset strength, inside MIN_BPM..MAX_BPM."""
    env = librosa.onset.onset_strength(y=samples, sr=SAMPLE_RATE, hop_length=COARSE_HOP)
    env = env - env.mean()
    size = len(env)
    spectrum = np.fft.rfft(env, 2 * size)
    corr = np.fft.irfft(spectrum * np.conj(spectrum))[:size]
    frames_per_second = SAMPLE_RATE / COARSE_HOP

    lags = np.arange(1, size)
    bpms = 60.0 * frames_per_second / lags
    allowed = (bpms >= MIN_BPM) & (bpms <= MAX_BPM)

    if not allowed.any() or corr[0] <= 0:
        raise AnalysisError("no usable beat found (silence?)")

    # Comb score: a true beat period has strong autocorrelation at 1, 2, 3 and 4 beats; a 3:2 or 4:3 impostor does not.
    # Then a mild prior in octaves, so that 70 and 140 don't tie.
    def at(multiple: float) -> np.ndarray:
        # the best value within a frame either side: a peak between two frames would otherwise read low
        positions = np.arange(size)
        return np.max([np.interp(lags[allowed] * multiple + shift, positions, corr) for shift in (-1.0, 0.0, 1.0)], axis=0)

    comb = at(1) + 0.75 * at(2) + 0.5 * at(3) + 0.4 * at(4)
    prior = np.exp(-0.5 * (np.log2(bpms[allowed] / PREFERRED_BPM) / 1.0) ** 2)
    scores = comb * prior
    best = int(np.argmax(scores))
    lag = lags[allowed][best]
    # Parabolic interpolation of the autocorrelation peak for a sub-frame lag.
    if 1 < lag < size - 1:
        left, mid, right = corr[lag - 1], corr[lag], corr[lag + 1]
        denominator = left - 2 * mid + right
        shift = 0.5 * (left - right) / denominator if denominator else 0.0
        lag = lag + max(-1.0, min(1.0, shift))

    return float(60.0 * frames_per_second / lag)


def _bass_onsets(librosa, samples: np.ndarray) -> np.ndarray:
    """Onset strength of the bass band only (below about 200 Hz), where the kick is, so hats and off-beat parts don't pull the phase."""
    mel = librosa.feature.melspectrogram(y=samples, sr=SAMPLE_RATE, hop_length=COARSE_HOP, n_fft=2048, n_mels=24, fmin=30, fmax=200)
    # center=False: given a spectrogram, onset_strength would otherwise shift its result later by a guessed n_fft/2 (n_fft=2048)
    return librosa.onset.onset_strength(S=librosa.power_to_db(mel, ref=np.max), sr=SAMPLE_RATE, hop_length=COARSE_HOP, center=False)


def _coarse_grid(librosa, samples: np.ndarray, bpm: float) -> tuple[float, float]:
    """(bpm, first beat in seconds) of the constant grid that best fits the bass onsets, found by folding."""
    env = _bass_onsets(librosa, samples)
    frames_per_second = SAMPLE_RATE / COARSE_HOP
    times = np.arange(len(env)) / frames_per_second
    best = (-1.0, bpm, 0.0)

    for candidate in bpm * np.linspace(0.97, 1.03, 121):
        period = 60.0 / candidate
        bins = 48
        position = ((times % period) / period * bins).astype(int)
        profile = np.bincount(position, weights=env, minlength=bins)
        profile = np.convolve(np.r_[profile[-1:], profile, profile[:1]], [0.25, 0.5, 0.25], mode="valid")
        sharpness = profile.max() / (profile.mean() + 1e-12)

        if sharpness > best[0]:
            best = (sharpness, candidate, (int(np.argmax(profile)) + 0.5) / bins * period)

    return best[1], best[2]


def _attack_slope(librosa, samples: np.ndarray) -> np.ndarray:
    """Log-compressed spectral flux at about 3 ms resolution: how much new energy appears in any band (kick and hats alike)."""
    mel = librosa.feature.melspectrogram(y=samples, sr=SAMPLE_RATE, hop_length=FINE_HOP, n_fft=FINE_FFT, n_mels=40, fmin=30, fmax=9000)
    return librosa.onset.onset_strength(S=librosa.power_to_db(mel, ref=np.max), sr=SAMPLE_RATE, hop_length=FINE_HOP, center=False)


def _find_attacks(slope: np.ndarray, grid_s: np.ndarray, strong: float) -> tuple[np.ndarray, np.ndarray]:
    """For each grid beat, the time of the sharpest attack near it, and whether it is strong enough to count (a hit)."""
    rate = SAMPLE_RATE / FINE_HOP
    window = int(SEARCH_WINDOW_S * rate)
    times = np.zeros(len(grid_s))
    hits = np.zeros(len(grid_s), dtype=bool)

    for i, beat in enumerate(grid_s):
        centre = int(round(beat * rate))
        start, end = max(0, centre - window), min(len(slope), centre + window + 1)

        if end - start < 2:
            times[i] = beat
            continue

        peak = start + int(np.argmax(slope[start:end]))
        # parabolic interpolation around the peak for a finer time than one frame
        if 0 < peak < len(slope) - 1:
            left, mid, right = slope[peak - 1], slope[peak], slope[peak + 1]
            denominator = left - 2 * mid + right
            shift = 0.5 * (left - right) / denominator if denominator else 0.0
            peak_time = (peak + max(-0.5, min(0.5, shift))) / rate
        else:
            peak_time = peak / rate

        times[i] = peak_time - FLUX_LEAD_MS / 1000.0
        hits[i] = slope[peak] >= HIT_STRENGTH * strong

    return times, hits


def _fit(grid_s: np.ndarray, times: np.ndarray, hits: np.ndarray, period: float) -> tuple[float, float, np.ndarray]:
    """Least squares through the attacks that were hits: (period, first beat, residuals in seconds for those beats)."""
    index = np.arange(len(grid_s))[hits]

    if len(index) < 8:
        return period, float(grid_s[0]), np.array([])

    slope, intercept = np.polyfit(index, times[hits], 1)
    residuals = times[hits] - (intercept + slope * index)
    return float(slope), float(intercept), residuals


def _inside_the_file(first: float, period: float) -> float:
    """Move the first beat into 0..one beat: a tiny negative (fit noise on a beat at 0) is 0; a real one is a beat later."""
    if first < 0:
        first = 0.0 if first > -0.02 else first + period * np.ceil(-first / period)

    return float(first % period) if first >= period else float(first)


def analyze_file(path: str | Path) -> BeatgridResult:
    """Analyse one audio file. Raises AnalysisError when it can't be done."""
    librosa, samples = _load(Path(path))
    duration = len(samples) / SAMPLE_RATE

    if duration < MIN_SECONDS:
        raise AnalysisError(f"too short to analyse ({duration:.1f} s)")

    if float(np.max(np.abs(samples))) < 1e-4:
        raise AnalysisError("no usable beat found (silence)")

    bpm, first = _coarse_grid(librosa, samples, _estimate_bpm(librosa, samples))
    period = 60.0 / bpm
    slope = _attack_slope(librosa, samples)
    strong = float(np.percentile(slope[slope > 0], 99)) if np.any(slope > 0) else 0.0

    if strong <= 0:
        raise AnalysisError("no usable beat found (no attacks)")

    hits = np.zeros(1, dtype=bool)
    times = np.zeros(1)
    grid = np.array([first])
    residuals = np.array([])

    # Refine twice: find the attacks near the grid, fit a line through them, and look again around the new grid.
    for _ in range(2):
        count = int((duration - first) / period)
        grid = first + period * np.arange(max(count, 1))
        times, hits = _find_attacks(slope, grid, strong)
        period, first, residuals = _fit(grid, times, hits, period)

        if len(residuals) == 0:
            break

    first = _inside_the_file(first, period)
    count = int((duration - first) / period)
    grid = first + period * np.arange(max(count, 1))
    times, hits = _find_attacks(slope, grid, strong)
    hit_fraction = float(hits.mean())

    # Does one constant grid hold across the whole track? Compare four stretches of it.
    medians, stretch_hits = [], []
    for chunk in np.array_split(np.arange(len(grid)), SEGMENTS):
        chunk_hits = hits[chunk]
        stretch_hits.append(float(chunk_hits.mean()) if len(chunk) else 0.0)

        if chunk_hits.any():
            medians.append(float(np.median(times[chunk][chunk_hits] - grid[chunk][chunk_hits])))

    spread_ms = (max(medians) - min(medians)) * 1000.0 if len(medians) >= 2 else 0.0
    residual_ms = float(np.std(residuals)) * 1000.0 if len(residuals) else 99.0
    uneven = max(stretch_hits) >= 0.5 > min(stretch_hits)
    steady = spread_ms <= DRIFT_LIMIT_MS and not uneven and hit_fraction >= 0.5
    confidence = hit_fraction * (1.0 - min(1.0, residual_ms / 25.0))

    return BeatgridResult(
        first_beat_ms=round(first * 1000.0, 1),
        bpm=round(60.0 / period, 3),
        confidence=round(max(0.0, confidence), 3),
        steady=steady,
        spread_ms=round(spread_ms, 1),
        hit_fraction=round(hit_fraction, 3),
        duration_s=round(duration, 1),
    )
