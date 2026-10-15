"""
Benchmark: run the pipeline on labelled prescriptions and measure how well it does.

    python -m benchmark.run                       # all cases, provider from .env
    python -m benchmark.run -p ollama --limit 5   # quick check
    python -m benchmark.run --cold                # empty "what it is for" cache: worst-case speed

Measures, for every case and overall:
    - field accuracy: amount, times per day, time of day, food, duration, when needed, max per day, hours between doses
    - medicines found (recall) and medicines invented (precision); illness detection
    - review flag: raised when something is uncertain, not raised when everything is clear
    - privacy: personal details that reached the AI or the patient text (must be 0)
    - unsupported numbers: numbers in the patient text that are not on the prescription (must be 0)
    - required / forbidden wording, expected refusals
    - speed (time to dosing, total) and reading level of the patient text

Results are written to benchmark/results/<time>_<provider>_<set>.json (read by the AMD Full stack dashboard).
Progress is saved after every case; if a run stops, the same command continues where it stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable

PROJECT_DIR = Path(__file__).resolve().parent.parent
CASES_FILE = Path(__file__).resolve().parent / "cases.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

sys.path.insert(0, str(PROJECT_DIR))
from doctor_cli import load_settings  # noqa: E402

load_settings()
import ai_pipeline as pipeline  # noqa: E402
from privacy import keep_medical_only  # noqa: E402

FIELDS = ["amount", "times_per_day", "times_of_day", "food", "duration", "prn", "max_per_day", "every_hours"]
_FIELD_MAP = {  # benchmark name -> pipeline field
    "amount": "amount_per_dose", "times_per_day": "times_per_day", "times_of_day": "times_of_day", "food": "food",
    "duration": "duration", "prn": "only_when_needed", "max_per_day": "max_per_day", "every_hours": "every_hours",
}


# --------------------------------------------------------------------------- #
# Normalisation, so "1 tablet" == "1 tablets" == "1 cp" and "8 days" == "8 jours"
# --------------------------------------------------------------------------- #

_UNITS = [
    ("tablet", r"tablet|comprim|\bcp\b|\btab"), ("capsule", r"capsule|g[ée]l|\bcap"), ("sachet", r"sachet"),
    ("puff", r"puff|bouff"), ("lozenge", r"lozenge|pastille"), ("spoon", r"spoon|c\. ?[àa] ?s"), ("drop", r"drop|goutte"),
    ("ml", r"\bml\b|millilit"),
]


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", _strip_accents(text.lower()))


def _quantity(value) -> tuple | None:
    if value is None:
        return None
    text = str(value).lower()
    if re.search(r"\b1\s*/\s*2\b|½|\bhalf\b|\bdemi", text):
        number = "0.5"
    else:
        m = re.search(r"\d+(?:[.,]\d+)?", text)
        number = m.group(0).replace(",", ".") if m else None
    unit = next((u for u, p in _UNITS if re.search(p, text)), None)
    return (number, unit)


def _duration(value) -> str | None:
    if value is None:
        return None
    text = str(value).lower()
    if "long" in text:
        return "long term"
    m = re.search(r"(\d+)\s*(day|jour|week|semaine|month|mois)", text)
    if not m:
        return text
    unit = {"jour": "day", "semaine": "week", "mois": "month"}.get(m.group(2), m.group(2))
    return f"{m.group(1)} {unit}"


def _same(field: str, expected, got) -> bool:
    if field in ("amount", "max_per_day"):
        return _quantity(expected) == _quantity(got)
    if field == "duration":
        return _duration(expected) == _duration(got)
    if field == "times_of_day":
        return sorted(expected or []) == sorted(got or [])
    if field == "food" and isinstance(expected, list):
        return got in expected
    if field == "prn":
        return bool(expected) == bool(got)
    return expected == got


# --------------------------------------------------------------------------- #
# Text measures
# --------------------------------------------------------------------------- #

def _syllables(word: str) -> int:
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups) - (1 if word.endswith("e") and len(groups) > 1 else 0)
    return max(count, 1)


def reading_grade(text: str) -> float | None:
    """Flesch-Kincaid grade level (school years needed). Lower is easier."""
    sentences = [s for s in re.split(r"[.!?\n]+", text) if re.search(r"[A-Za-z]", s)]
    words = re.findall(r"[A-Za-z']+", text)
    if not sentences or not words:
        return None
    syllables = sum(_syllables(w) for w in words)
    return round(0.39 * len(words) / len(sentences) + 11.8 * syllables / len(words) - 15.59, 1)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


def unsupported_numbers(output: str, source: str) -> list[str]:
    """Numbers in the patient text that are not on the prescription (list numbering excluded)."""
    body = re.sub(r"^\s*\d+\.\s", "", output, flags=re.MULTILINE)
    body = re.sub(r"Checked by your doctor on .*", "", body)
    allowed = pipeline.numbers_on_prescription(source) | {"1"}  # words like "cinq" or "TID" count; "in one day"
    return sorted(_numbers(body) - allowed)


# --------------------------------------------------------------------------- #
# Running one case
# --------------------------------------------------------------------------- #

def run_case(case: dict) -> dict:
    sent_to_ai, removed = keep_medical_only(case["input"])
    result = {
        "id": case["id"], "category": case.get("category", ""), "input": case["input"],
        "sent_to_ai": sent_to_ai, "privacy_removed": removed,
        "checks": [], "error": None, "output": "", "review": [], "structured": None,
        "time_to_first": None, "time_to_dosing": None, "time_total": None,
    }
    checks = result["checks"]

    def check(group: str, name: str, ok: bool, expected=None, got=None):
        checks.append({"group": group, "name": name, "ok": bool(ok), "expected": expected, "got": got})

    start = time.monotonic()
    done = None
    try:
        for event in pipeline.stream_prescription(case["input"]):
            if event["type"] == "medicine_ready" and result["time_to_first"] is None:
                result["time_to_first"] = round(time.monotonic() - start, 2)  # first checked medicine on screen
            elif event["type"] == "dosing":
                result["time_to_dosing"] = round(time.monotonic() - start, 2)
            elif event["type"] == "done":
                done = event
    except pipeline.PipelineError as exc:
        result["error"] = str(exc)
    result["time_total"] = round(time.monotonic() - start, 2)

    # Privacy: personal details must never reach the AI or the patient text.
    for secret in case.get("private", []):
        check("privacy", f'"{secret}" not sent to the AI', secret.lower() not in sent_to_ai.lower(), "removed",
              "leaked" if secret.lower() in sent_to_ai.lower() else "removed")

    if case.get("expect_error"):
        check("refusal", "refuses and explains why", done is None and bool(result["error"]) and "Sorry" in result["error"],
              "a 'Sorry ... Why' message", result["error"] or "explained it anyway")
        return result
    if done is None:
        if not case.get("allow_error"):  # dialogue turns ("non", remarks) may be refused
            check("refusal", "explains a real prescription", False, "an explanation", result["error"])
        return result

    output, structured = done["text"], done["prescription"]
    result.update(output=output, review=done["review"], structured=structured,
                  needs_review=done["needs_review"], reading_grade=reading_grade(output))

    for secret in case.get("private", []):
        check("privacy", f'"{secret}" not in patient text', secret.lower() not in output.lower(), "absent",
              "present" if secret.lower() in output.lower() else "absent")

    # Medicines: each expected one must be found, with the right fields; none may be invented.
    found = structured["medicines"]
    matched: set[int] = set()
    for exp in case.get("meds", []):
        if exp["name"] is None:  # dose spoken without the drug's name: take the first medicine found
            idx = next((i for i in range(len(found)) if i not in matched), None)
            exp = {**exp, "name": "(unnamed)"}
        else:
            key = _letters(exp["name"])[:6]
            idx = next((i for i, m in enumerate(found)
                        if i not in matched and _letters(m["name_as_written"]).startswith(key[:5])), None)
        check("medicines", f"{exp['name']}: found", idx is not None, exp["name"],
              found[idx]["name_as_written"] if idx is not None else "missing")
        if idx is None:
            for field in FIELDS:
                if field in exp:
                    check("fields", f"{exp['name']}: {field}", False, exp[field], "medicine missing")
            continue
        matched.add(idx)
        med = found[idx]
        for field in FIELDS:
            if field in exp:
                got = med.get(_FIELD_MAP[field])
                check("fields", f"{exp['name']}: {field}", _same(field, exp[field], got), exp[field], got)
    if "meds" in case:
        extra = [m["name_as_written"] for i, m in enumerate(found) if i not in matched]
        check("medicines", "no invented medicine", not extra, "none", extra or "none")

    for pattern in case.get("conditions", []):
        conditions = structured.get("conditions", [])
        check("illness", f"illness /{pattern}/", any(re.search(pattern, c, re.IGNORECASE) for c in conditions), pattern, conditions)

    if "expect_review" in case:
        check("review", "asks the doctor to check" if case["expect_review"] else "no needless review flag",
              done["needs_review"] == case["expect_review"], case["expect_review"], done["needs_review"])

    bad_numbers = unsupported_numbers(output, sent_to_ai)
    check("safety", "no number that is not on the prescription", not bad_numbers, [], bad_numbers)

    for phrase in case.get("required_output", []):
        check("wording", f'says "{phrase}"', phrase.lower() in output.lower(), phrase, "present" if phrase.lower() in output.lower() else "missing")
    for phrase in case.get("forbidden_output", []):
        check("wording", f'never says "{phrase}"', phrase.lower() not in output.lower(), "absent", "present" if phrase.lower() in output.lower() else "absent")

    return result


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def _rate(checks: list[dict]) -> dict:
    total = len(checks)
    passed = sum(c["ok"] for c in checks)
    return {"passed": passed, "total": total, "rate": round(passed / total, 3) if total else None}


def summarize(results: list[dict]) -> dict:
    all_checks = [c for r in results for c in r["checks"]]
    by_group = {g: _rate([c for c in all_checks if c["group"] == g]) for g in dict.fromkeys(c["group"] for c in all_checks)}
    field_checks = [c for c in all_checks if c["group"] == "fields"]
    by_field = {f: _rate([c for c in field_checks if c["name"].endswith(f": {f}")]) for f in FIELDS}
    by_field = {f: v for f, v in by_field.items() if v["total"]}
    by_category = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(all(c["ok"] for c in r["checks"]))

    timed = [r for r in results if r["time_to_dosing"] is not None]
    dosing = [r["time_to_dosing"] for r in timed]
    first = [r["time_to_first"] for r in results if r.get("time_to_first") is not None]
    totals = [r["time_total"] for r in timed]
    grades = [r["reading_grade"] for r in results if r.get("reading_grade") is not None]
    words = [len(r["output"].split()) for r in results if r["output"]]

    def stats(values):
        if not values:
            return None
        values = sorted(values)
        return {"mean": round(statistics.mean(values), 2), "median": round(statistics.median(values), 2),
                "p95": values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))], "max": values[-1]}

    review = [c for c in all_checks if c["group"] == "review"]
    return {
        "cases": len(results),
        "cases_fully_correct": sum(all(c["ok"] for c in r["checks"]) for r in results),
        "checks": _rate(all_checks),
        "by_group": by_group,
        "by_field": by_field,
        "by_category": {k: {"passed": sum(v), "total": len(v)} for k, v in by_category.items()},
        "privacy_leaks": sum(not c["ok"] for c in all_checks if c["group"] == "privacy"),
        "unsupported_numbers": sum(not c["ok"] for c in all_checks if c["name"] == "no number that is not on the prescription"),
        "review_flag": {
            "missed": sum(not c["ok"] and c["expected"] is True for c in review),
            "needless": sum(not c["ok"] and c["expected"] is False for c in review),
        },
        "time_to_first_s": stats(first),
        "time_to_dosing_s": stats(dosing),
        "time_total_s": stats(totals),
        "reading_grade": {"mean": round(statistics.mean(grades), 1), "max": max(grades)} if grades else None,
        "words_per_explanation": round(statistics.mean(words)) if words else None,
    }


def run_benchmark(cases: list[dict], cold: bool = False,
                  on_progress: Callable[[int, int, dict], None] | None = None, dataset: str = CASES_FILE.name,
                  no_purpose: bool = False) -> dict:
    """Run all cases and return {"meta", "summary", "results"}; also saved to benchmark/results/."""
    if cold:  # empty cache: every "what it is for" lookup goes to the AI
        pipeline.DRUG_INFO_CACHE_PATH = Path(tempfile.mkdtemp()) / "drug_info.json"
    if no_purpose:  # the "what it is for" line is not scored; skipping it makes big runs faster
        pipeline._fetch_drug_purpose = lambda med: None
    # Every finished case is appended to a progress file, so a stopped run can continue later
    # (same test set, provider and model) instead of starting again.
    model_tag = re.sub(r"[^A-Za-z0-9.]+", "-", pipeline.LLM_MODEL.split("/")[-1])[:40]
    progress_file = RESULTS_DIR / f".progress_{Path(dataset).stem}_{pipeline.LLM_PROVIDER}_{model_tag}{'_cold' if cold else ''}.jsonl"
    done: dict[str, dict] = {}
    started = datetime.now()
    if progress_file.is_file():
        for line in progress_file.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("model") == pipeline.LLM_MODEL:
                started = datetime.fromisoformat(row["started"])
                done[row["result"]["id"]] = row["result"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    with progress_file.open("a", encoding="utf-8") as progress:
        for i, case in enumerate(cases, 1):
            if case["id"] in done:  # finished in an earlier, interrupted run
                results.append(done[case["id"]])
                continue
            result = run_case(case)
            results.append(result)
            progress.write(json.dumps({"started": started.isoformat(timespec="seconds"), "model": pipeline.LLM_MODEL,
                                       "result": result}, ensure_ascii=False, default=str) + "\n")
            progress.flush()
            if on_progress:
                on_progress(i, len(cases), result)
    report = {
        "meta": {
            "started": started.isoformat(timespec="seconds"),
            "duration_s": round((datetime.now() - started).total_seconds(), 1),
            "provider": pipeline.LLM_PROVIDER, "model": pipeline.LLM_MODEL, "cold_cache": cold, "no_purpose": no_purpose,
            "cases_file": dataset,
        },
        "summary": summarize(results),
        "results": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = Path(dataset).stem
    model_part = "" if pipeline.LLM_PROVIDER == "ollama" else f"_{model_tag}"
    path = RESULTS_DIR / f"{started:%Y%m%d_%H%M%S}_{pipeline.LLM_PROVIDER}{model_part}_{tag}{'_cold' if cold else ''}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    progress_file.unlink(missing_ok=True)  # complete: the saved report replaces the progress file
    report["meta"]["file"] = path.name
    report["meta"]["resumed_cases"] = len(done)
    return report


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def datasets() -> list[str]:
    """Test sets available in benchmark/ (cases.jsonl first)."""
    files = sorted(p.name for p in CASES_FILE.parent.glob("*.jsonl"))
    return sorted(files, key=lambda f: f != CASES_FILE.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure ClearCare on labelled cases.")
    parser.add_argument("-p", "--provider", choices=sorted(pipeline._PROVIDERS))
    parser.add_argument("-m", "--model")
    parser.add_argument("--limit", type=int, help="only run the first N cases")
    parser.add_argument("--case", action="append", help="only run this case id (repeatable)")
    parser.add_argument("--cold", action="store_true", help="start with an empty cache (worst-case speed)")
    parser.add_argument("--no-purpose", action="store_true", help="skip the 'what it is for' lookup (not scored; faster)")
    parser.add_argument("--cases", default=CASES_FILE.name,
                        help="test set in benchmark/ (cases.jsonl, realworld.jsonl, random_seed7.jsonl, ...)")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.ERROR)
    if args.provider or args.model:
        pipeline.configure(provider=args.provider, model=args.model)

    cases = load_cases(CASES_FILE.parent / args.cases)
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.limit:
        cases = cases[: args.limit]

    def progress(i, n, r):
        failed = [c["name"] for c in r["checks"] if not c["ok"]]
        status = "PASS" if not failed else f"FAIL ({len(failed)}): " + "; ".join(failed[:3])
        print(f"[{i}/{n}] {r['id']:<24} {r['time_total']:>5.1f}s  {status}", flush=True)

    print(f"Benchmark · {args.cases} · {pipeline.LLM_PROVIDER} / {pipeline.LLM_MODEL} · {len(cases)} cases", flush=True)
    print("Stopped runs continue where they stopped: just run the same command again.\n", flush=True)
    report = run_benchmark(cases, cold=args.cold, on_progress=progress, dataset=args.cases, no_purpose=args.no_purpose)
    s = report["summary"]
    print(f"\nChecks passed:        {s['checks']['passed']}/{s['checks']['total']} ({s['checks']['rate']:.0%})")
    print(f"Cases fully correct:  {s['cases_fully_correct']}/{s['cases']}")
    for f, v in s["by_field"].items():
        print(f"  field {f:<14} {v['passed']}/{v['total']}")
    print(f"Privacy leaks:        {s['privacy_leaks']}")
    print(f"Unsupported numbers:  {s['unsupported_numbers']}")
    print(f"Review flag:          missed {s['review_flag']['missed']}, needless {s['review_flag']['needless']}")
    if s["time_to_dosing_s"]:
        if s.get("time_to_first_s"):
            print(f"First medicine shown: median {s['time_to_first_s']['median']}s")
        print(f"Time to dosing:       median {s['time_to_dosing_s']['median']}s · total median {s['time_total_s']['median']}s")
    if s["reading_grade"]:
        print(f"Reading level:        grade {s['reading_grade']['mean']} (school years)")
    print(f"\nSaved: {RESULTS_DIR / report['meta']['file']}")


if __name__ == "__main__":
    main()
