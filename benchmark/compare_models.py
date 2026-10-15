"""
Compare models on speed and accuracy: same cases, all models at the same time.

    python -m benchmark.compare_models -p amd                        # every text model of the provider
    python -m benchmark.compare_models -p amd -m DeepSeek-V4.1-Flash Qwen3.8-Flash-Next

Each model runs the benchmark on the same few core cases (the "what it is for" lookup is skipped,
it is not scored). The table shows accuracy, time per prescription and tokens, best first.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from benchmark.run import RESULTS_DIR, PROJECT_DIR

CASES = ["amox-simple", "paracetamol-prn", "angine-full", "unclear-dose", "english-latin", "methotrexate-weekly"]
# Models made for images or documents are left out.
SKIP = ("vision", "minicpm-v-4.7-1b", "mineru")


def list_models(provider: str) -> list[str]:
    sys.path.insert(0, str(PROJECT_DIR))
    from doctor_cli import load_settings
    load_settings()
    import ai_pipeline as pipeline
    pipeline.configure(provider=provider)
    models = [m.id for m in pipeline._get_llm_client().models.list().data]
    return [m for m in models if not any(s in m.lower() for s in SKIP)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare models on speed and accuracy.")
    parser.add_argument("-p", "--provider", default="amd")
    parser.add_argument("-m", "--models", nargs="*", help="models to compare (default: all text models)")
    parser.add_argument("--cases", nargs="*", default=CASES, help="core case ids")
    args = parser.parse_args()

    models = args.models or list_models(args.provider)
    print(f"Comparing {len(models)} models on {len(args.cases)} cases: {', '.join(models)}\n", flush=True)
    started = time.time()
    procs = {}
    for model in models:
        cmd = [sys.executable, "-m", "benchmark.run", "-p", args.provider, "-m", model, "--cases", "cases.jsonl", "--no-purpose"]
        for case in args.cases:
            cmd += ["--case", case]
        log = open(RESULTS_DIR / f".compare_{model}.log", "w")
        procs[model] = (subprocess.Popen(cmd, cwd=PROJECT_DIR, stdout=log, stderr=subprocess.STDOUT), log)
        time.sleep(1)  # stagger the start, as AMD asks

    for model, (proc, log) in procs.items():
        proc.wait()
        log.close()
        print(f"  finished: {model}", flush=True)

    rows = []
    for model in models:
        tag = model.split("/")[-1]
        files = sorted(p for p in RESULTS_DIR.glob("*_cases.json") if p.stat().st_mtime >= started)
        report = None
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data["meta"]["model"] == model:
                report = data
        if not report:
            log = (RESULTS_DIR / f".compare_{model}.log").read_text(encoding="utf-8")[-300:]
            rows.append({"model": tag, "error": log.strip().splitlines()[-1] if log.strip() else "no result"})
            continue
        s = report["summary"]
        times = [r["time_total"] for r in report["results"] if r["time_total"] is not None]
        fields = sum(v["passed"] for v in s["by_field"].values()), sum(v["total"] for v in s["by_field"].values())
        rows.append({
            "model": tag, "checks": s["checks"]["rate"], "cases": f"{s['cases_fully_correct']}/{s['cases']}",
            "fields": f"{fields[0]}/{fields[1]}", "median_s": statistics.median(times) if times else None,
            "max_s": max(times) if times else None, "errors": sum(1 for r in report["results"] if r.get("error")),
            "file": report["meta"].get("file", ""),
        })

    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: (-r["checks"], r["median_s"] or 1e9))
    print(f"\n{'model':<26} {'checks':>7} {'cases':>6} {'fields':>7} {'median s':>9} {'slowest s':>10}")
    for r in ok:
        print(f"{r['model']:<26} {r['checks']:>7.0%} {r['cases']:>6} {r['fields']:>7} {r['median_s']:>9.1f} {r['max_s']:>10.1f}")
    for r in rows:
        if "error" in r:
            print(f"{r['model']:<26} FAILED: {r['error'][:90]}")


if __name__ == "__main__":
    main()
