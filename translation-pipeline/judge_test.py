"""
judge_test.py — does the second-model judge spot real mistakes?

    python judge_test.py            # 25 PxCorpus prescriptions already checked by the benchmark

Takes prescriptions the benchmark has already scored against the human annotations, runs the
full chain (English explanation -> Darija -> judge 0-5) on them, and compares: when the
benchmark found a real error, did the judge give a low score?
Results: data/judge_test_<time>.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import translation_pipeline as tp  # first: it sets up the project path, the pipeline and the Darija module
import judge

BENCH = tp.PROJECT / "benchmark"
SAMPLE = 25
SEED = 25


def benchmark_verdicts() -> dict[str, dict]:
    """Case id -> benchmark result, from the PxCorpus run (saved progress or a finished report)."""
    verdicts = {}
    progress = BENCH / "results" / ".progress_pxcorpus_ollama.jsonl"
    if progress.is_file():
        for line in progress.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)["result"]
            verdicts[r["id"]] = r
    for report in sorted((BENCH / "results").glob("*_pxcorpus*.json")):
        for r in json.loads(report.read_text(encoding="utf-8"))["results"]:
            verdicts[r["id"]] = r
    return verdicts


def main() -> None:
    parser = argparse.ArgumentParser(description="Does the judge spot real mistakes?")
    parser.add_argument("-n", type=int, default=SAMPLE, help="how many prescriptions (random, fixed seed)")
    parser.add_argument("--ids", nargs="*", help="these PxCorpus case ids instead of a random sample")
    parser.add_argument("--show", action="store_true", help="print each explanation and the judge's reasons")
    args = parser.parse_args()
    cases = {json.loads(l)["id"]: json.loads(l) for l in (BENCH / "pxcorpus.jsonl").read_text(encoding="utf-8").splitlines()}
    verdicts = benchmark_verdicts()
    # Real prescriptions the benchmark could check (it had expected fields for them).
    pool = [cid for cid, r in verdicts.items()
            if cases[cid]["category"].endswith("medical_prescription") and r["checks"] and not r.get("error")]
    chosen = args.ids or random.Random(SEED).sample(sorted(pool), min(args.n, len(pool)))
    print(f"Judge test · {len(chosen)} PxCorpus prescriptions · judge: {judge._client()[1]} / {judge._client()[2]}\n", flush=True)

    rows = []
    for n, cid in enumerate(chosen, 1):
        case, bench = cases[cid], verdicts[cid]
        failed = [c["name"] for c in bench["checks"] if not c["ok"]]
        start = time.monotonic()
        try:
            out = tp.process(case["input"])
            rating = out["rating"]
            kept = [k["why"] for k in out.get("kept_in_english", [])]
        except Exception as exc:
            rating, kept, out = {"score": None, "problems": [str(exc)], "criteria": {}}, [], {}
        rows.append({
            "id": cid, "input": case["input"], "benchmark_ok": not failed, "benchmark_failed": failed,
            "score": rating.get("score"), "criteria": rating.get("criteria"), "problems": rating.get("problems"),
            "verdict": rating.get("verdict"), "english": out.get("english"), "darija": out.get("darija"),
            "kept_in_english": kept, "translation_model": out.get("model"), "seconds": round(time.monotonic() - start, 1),
        })
        mark = "OK " if not failed else "ERR"
        print(f"[{n:2}/{len(chosen)}] {cid}  benchmark {mark}  judge {rating.get('score')}/5  "
              f"{'; '.join(failed)[:60]}", flush=True)
        if args.show:
            body = [l for l in (out.get("english") or "").splitlines() if l.strip() and not l.startswith(("Your", "You have", "If you"))]
            print(f"   PRESCRIPTION: {case['input']}")
            print("   ENGLISH:     " + "\n                ".join(body))
            if out.get("darija"):
                dbody = [l for l in out["darija"].splitlines() if l.strip()][:8]
                print("   DARIJA:      " + "\n                ".join(dbody))
            print("\n".join("   " + l for l in judge.format_rating(rating)) + "\n", flush=True)

    scored = [r for r in rows if r["score"] is not None]
    bad = [r for r in scored if not r["benchmark_ok"]]
    good = [r for r in scored if r["benchmark_ok"]]
    caught = [r for r in bad if r["score"] < judge.PASS_SCORE]
    false_alarms = [r for r in good if r["score"] < judge.PASS_SCORE]
    summary = {
        "prescriptions": len(rows), "rated": len(scored),
        "average_score": round(statistics.mean(r["score"] for r in scored), 2) if scored else None,
        "score_counts": {s: sum(r["score"] == s for r in scored) for s in range(6)},
        "benchmark_errors": len(bad), "errors_caught_by_judge": len(caught),
        "benchmark_correct": len(good), "false_alarms": len(false_alarms),
        "translation_kept_in_english": sum(len(r["kept_in_english"]) for r in rows),
    }
    out_file = tp.HERE / "data" / f"judge_test_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.parent.mkdir(exist_ok=True)
    out_file.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nAverage judge score: {summary['average_score']}/5  ·  scores: {summary['score_counts']}")
    print(f"Real errors (benchmark): {len(bad)}  ·  caught by the judge (<{judge.PASS_SCORE}/5): {len(caught)}")
    print(f"Correct ones (benchmark): {len(good)}  ·  judge false alarms: {len(false_alarms)}")
    print(f"Saved: {out_file}")


if __name__ == "__main__":
    main()
