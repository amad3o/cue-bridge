"""Score the beatgrid analyzer against the grids Serato already wrote into the audio files.

Serato's grid is a reference, not the truth, so a miss means "disagrees with Serato". Run it on demand (it decodes audio):

    make eval-beatgrid ARGS="/Volumes/LaCie/Music --limit 100"

BPM is scored by ratio (exact, half/double, 3:2, 4:3, other). Phase is scored only where the BPM is exact or half/double,
as the distance of our first beat from Serato's folded to within half a beat. Results are cached per file and analyzer
version in a JSON file, so a rerun only analyses what changed.
"""

import argparse
import json
import random
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cuebridge.services.beatgrid import analyzer  # noqa: E402
from cuebridge.services.serato.tags import read_first_beat  # noqa: E402

SUFFIXES = {".wav", ".mp3"}
BPM_CLASSES = (("exact", 1.0), ("double", 2.0), ("half", 0.5), ("3:2", 1.5), ("2:3", 2 / 3), ("4:3", 4 / 3), ("3:4", 3 / 4))
BUCKETS = ((0.0, 0.3), (0.3, 0.6), (0.6, 0.8), (0.8, 1.01))


def bpm_class(ours: float, theirs: float) -> str:
    for name, ratio in BPM_CLASSES:
        if abs(ours / theirs - ratio) <= 0.004 * ratio:
            return name

    return "other"


def phase_error_ms(ours_ms: float, theirs_ms: float, bpm: float) -> float:
    period = 60000.0 / bpm
    return (ours_ms - theirs_ms + period / 2) % period - period / 2


def phase_class(error_ms: float, bpm: float) -> str:
    period = 60000.0 / bpm
    size = abs(error_ms)

    if size <= 15:
        return "within 15 ms"

    if size <= 50:
        return "within 50 ms"

    return "off-beat (near half a beat)" if size >= 0.35 * period else "other"


def score_file(path: str, mp3_offset_ms: float = 0.0) -> dict:
    entry = {"path": path}
    reference = read_first_beat(path)

    if not reference:
        return {**entry, "skip": "no Serato grid"}

    entry["serato"] = {"firstBeatMs": reference[0], "bpm": reference[1]}

    try:
        result = analyzer.analyze_file(path)
    except analyzer.AnalysisError as error:
        return {**entry, "skip": str(error)}

    entry["ours"] = {"firstBeatMs": result.first_beat_ms, "bpm": result.bpm, "confidence": result.confidence, "steady": result.steady}
    entry["bpmClass"] = bpm_class(result.bpm, reference[1])

    if entry["bpmClass"] in ("exact", "double", "half"):
        lead = mp3_offset_ms if path.lower().endswith(".mp3") else 0.0
        # fold by the faster of the two grids: when ours is half Serato's tempo, our beats are a subset of Serato's and are right
        # wherever they fall on one of its beats; when ours is double, every one of our beats must be on a Serato beat or half-way
        fast = max(result.bpm, reference[1])
        error = phase_error_ms(result.first_beat_ms + lead, reference[0], fast)
        entry["phaseErrorMs"] = round(error, 1)
        entry["phaseClass"] = phase_class(error, fast)

    return entry


def summarise(entries: list[dict]) -> str:
    scored = [e for e in entries if "bpmClass" in e]
    lines = [f"{len(scored)} scored, {len(entries) - len(scored)} skipped ({dict(Counter(e['skip'] for e in entries if 'skip' in e))})"]

    if not scored:
        return lines[0]

    lines.append("BPM against Serato: " + ", ".join(f"{k} {v}" for k, v in Counter(e["bpmClass"] for e in scored).most_common()))
    exact = [e for e in scored if e["bpmClass"] == "exact"]
    octave_ok = sum(e["bpmClass"] in ("exact", "double", "half") for e in scored)
    lines.append(f"  exact or half/double: {octave_ok}/{len(scored)}; exact: {len(exact)}/{len(scored)}")
    phased = [e for e in scored if "phaseClass" in e]
    lines.append("Phase where the BPM agrees: " + ", ".join(f"{k} {v}" for k, v in Counter(e["phaseClass"] for e in phased).most_common()))
    lines.append("By confidence (BPM exact | phase within 15 ms, of the files in the bucket):")

    for low, high in BUCKETS:
        bucket = [e for e in scored if low <= e["ours"]["confidence"] < high]

        if bucket:
            ok_bpm = sum(e["bpmClass"] == "exact" for e in bucket)
            ok_phase = sum(e.get("phaseClass") == "within 15 ms" for e in bucket)
            lines.append(f"  {low:.1f}-{min(high, 1.0):.1f}: {len(bucket):4} files | BPM exact {ok_bpm:4} | phase ok {ok_phase:4}")

    good = [e for e in scored if e["ours"]["confidence"] >= 0.6 and e["ours"]["steady"]]
    lines.append(
        f"Files the tool would call usable (steady, confidence >= 0.6): {len(good)}; of those BPM exact {sum(e['bpmClass'] == 'exact' for e in good)}, "
        f"phase within 15 ms {sum(e.get('phaseClass') == 'within 15 ms' for e in good)}, "
        f"wrong tempo ratio (not exact or half/double) {sum(e['bpmClass'] not in ('exact', 'double', 'half') for e in good)}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="folder with audio files that Serato has analysed")
    parser.add_argument("--limit", type=int, default=100, help="how many files to score (a seeded random sample)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cache", type=Path, default=Path("evals/results/beatgrid_vs_serato.json"))
    parser.add_argument("--version", default=analyzer.VERSION, help="cache key: results are reused only for the same analyzer version")
    parser.add_argument("--mp3-offset-ms", type=float, default=0.0, help="added to our first beat for MP3s before comparing (the decoder lead, about 43)")
    parser.add_argument("--show-worst", type=int, default=0, help="list this many of the worst misses")
    args = parser.parse_args()

    files = sorted(p for p in args.root.rglob("*") if p.suffix.lower() in SUFFIXES and not p.name.startswith("._"))
    random.Random(args.seed).shuffle(files)
    files = [str(p) for p in files[: args.limit]]
    cache = json.loads(args.cache.read_text()) if args.cache.exists() else {}
    key = f"{args.version}|mp3+{args.mp3_offset_ms:g}"
    done = cache.get(key, {})
    todo = [f for f in files if f not in done]
    print(f"{len(files)} files, {len(files) - len(todo)} already scored for {key}", flush=True)

    with ProcessPoolExecutor(args.workers) as pool:
        for count, entry in enumerate(pool.map(score_file, todo, [args.mp3_offset_ms] * len(todo)), 1):
            done[entry["path"]] = entry

            if count % 20 == 0:
                print(f"  {count}/{len(todo)}", flush=True)

    cache[key] = done
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    args.cache.write_text(json.dumps(cache, indent=1))
    entries = [done[f] for f in files]
    print(summarise(entries))

    if args.show_worst:
        bad = [e for e in entries if "bpmClass" in e and (e["bpmClass"] == "other" or e.get("phaseClass", "").startswith(("off", "other")))]
        print(f"\nMisses ({len(bad)}), first {args.show_worst}:")

        for e in bad[: args.show_worst]:
            name, ours, theirs = Path(e["path"]).name[:40], e["ours"]["bpm"], e["serato"]["bpm"]
            print(f"  {name:40} ours {ours:8.3f} serato {theirs:8.3f} {e['bpmClass']:6} phase {e.get('phaseErrorMs')}")


if __name__ == "__main__":
    main()
