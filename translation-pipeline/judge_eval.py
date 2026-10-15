"""
judge_eval.py — judge the judge with planted errors.

    python judge_eval.py            # 5 correct explanations x 5 versions = 25 ratings

Takes explanations the benchmark checked as correct, plants ONE known mistake in each copy,
and asks the judge to rate every version without saying which is which:

    clean            unchanged                              -> judge should give >= 4
    wrong_dose       a dose number changed (3 -> 5)          -> faithful should drop
    wrong_time       morning <-> evening swapped             -> faithful should drop
    missing_medicine one medicine removed                    -> complete should drop
    added_advice     advice the doctor never wrote           -> no_added_advice should drop

Measures: errors caught, false alarms on clean ones, whether the RIGHT criterion dropped, and
whether the judge's reason actually points at the planted mistake (if not, its reason is made up).
Results: data/judge_eval_<time>.json
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import translation_pipeline as tp  # sets up the project path and the pipeline
import judge

BENCH = tp.PROJECT / "benchmark"
ADVICE = "Avoid alcohol while you take it, and stop taking it if you feel dizzy."
EXPECTED_CRITERION = {"wrong_dose": "faithful", "wrong_time": "faithful", "wrong_duration": "faithful",
                      "wrong_food": "faithful", "prn_removed": "faithful", "limit_removed": "complete",
                      "missing_medicine": "complete", "added_advice": "no_added_advice"}
CAUGHT_MAX = 2  # harsh: an error only counts as caught if the score is 2 or less
PARALLEL = 2    # ratings at the same time (AMD allows 20 requests a minute)


def _bases(n: int = 5) -> list[dict]:
    """Correct explanations from the newest full core benchmark run, with 2+ medicines when possible."""
    reports = sorted(p for p in (BENCH / "results").glob("*_cases.json"))
    for path in reversed(reports):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["summary"]["cases"] < 20:
            continue
        good = [r for r in data["results"] if r["output"] and all(c["ok"] for c in r["checks"])
                and re.search(r"\b\d+ (?:pill|tablet|capsule|sachet|puff)s?\b", r["output"])]
        good.sort(key=lambda r: -len(r["structured"]["medicines"]))
        return good[:n]
    raise SystemExit("No full core benchmark run found in benchmark/results/")


def _cleaned(text: str) -> str:
    return tp.keep_medical_only(text)[0]


# --------------------------------------------------------------------------- #
# Planting one known mistake. Each returns (new text, the words that were changed) or None.
# --------------------------------------------------------------------------- #

def wrong_dose(text: str):
    m = re.search(r"\b(Take )(\d+)( \S+ (?:pill|tablet|capsule|sachet|puff)s?\b|\s(?:pills?|sachets?|puffs?)\b)", text)
    if not m:
        m = re.search(r"\b(Take )(\d+)(\b)", text)
    if not m:
        return None
    new = "5" if m.group(2) != "5" else "3"
    return text[:m.start(2)] + new + text[m.end(2):], f"{m.group(2)} -> {new}"


def wrong_time(text: str):
    if "in the morning" in text:
        return text.replace("in the morning", "in the evening", 1), "morning -> evening"
    if "in the evening" in text:
        return text.replace("in the evening", "in the morning", 1), "evening -> morning"
    if re.search(r"\b2 times a day\b", text):
        return re.sub(r"\b2 times a day\b", "once a day", text, count=1), "2 times a day -> once a day"
    return None


def missing_medicine(text: str):
    blocks = re.split(r"\n(?=\d+\. )", text)
    meds = [i for i, b in enumerate(blocks) if re.match(r"\d+\. ", b)]
    if len(meds) < 2:
        return None
    i = meds[-1]
    name = blocks[i].splitlines()[0]
    body = blocks[i].split("\n\n", 1)
    rest = ("\n\n" + body[1]) if len(body) > 1 else ""
    return "\n".join(blocks[:i] + ([rest.lstrip("\n")] if rest.strip() else []) + blocks[i + 1:]), f"removed {name}"


def added_advice(text: str):
    m = re.search(r"(\n   Take [^\n]+)", text)
    if not m:
        return None
    return text[:m.end()] + f"\n   {ADVICE}" + text[m.end():], f'added "{ADVICE}"'


def wrong_duration(text: str):
    m = re.search(r"\bfor (\d+) (days?|weeks?|months?)\b", text)
    if not m:
        return None
    new = str(int(m.group(1)) + 2)
    return text[:m.start(1)] + new + text[m.end(1):], f"{m.group(1)} {m.group(2)} -> {new} {m.group(2)}"


def wrong_food(text: str):
    swaps = [("with food", "before eating"), ("before you eat anything", "with food"), ("after eating", "before eating")]
    for old, new in swaps:
        if old in text:
            return text.replace(old, new, 1), f"{old} -> {new}"
    return None


def prn_removed(text: str):
    m = re.search(r" only when you have [^,.]+", text)
    if not m:
        return None
    return text[:m.start()] + text[m.end():], f'removed "{m.group(0).strip()}"'


def limit_removed(text: str):
    m = re.search(r"\n\s*Do not take more than [^\n]+", text)
    if not m:
        return None
    return text[:m.start()] + text[m.end():], f'removed "{m.group(0).strip()}"'


PLANTS = {"wrong_dose": wrong_dose, "wrong_time": wrong_time, "wrong_duration": wrong_duration,
          "wrong_food": wrong_food, "prn_removed": prn_removed, "limit_removed": limit_removed,
          "missing_medicine": missing_medicine, "added_advice": added_advice}


def _mentions(reason_text: str, kind: str, change: str) -> bool:
    """Does the judge's reasoning point at the planted mistake?"""
    t = reason_text.lower()
    if kind == "wrong_dose":
        old, new = re.findall(r"\d+", change)[:2]
        return new in t or "dose" in t or "amount" in t or "number" in t
    if kind == "wrong_time":
        return any(w in t for w in ("morning", "evening", "time of day", "timing", "once a day", "times a day"))
    if kind == "missing_medicine":
        name = change.replace("removed ", "").split(". ", 1)[-1].split()[0].lower()
        return name in t or "missing" in t or "not included" in t or "omit" in t
    if kind == "wrong_duration":
        return re.findall(r"\d+", change)[-1] in t or "duration" in t or "days" in t or "weeks" in t or "months" in t
    if kind == "wrong_food":
        return any(w in t for w in ("food", "eat", "meal", "stomach", "repas", "jeun"))
    if kind == "prn_removed":
        return any(w in t for w in ("when needed", "only when", "if needed", "as needed", "fever", "pain", "si "))
    if kind == "limit_removed":
        return any(w in t for w in ("more than", "maximum", "max", "limit"))
    if kind == "added_advice":
        return any(w in t for w in ("alcohol", "dizzy", "advice", "warning", "not written", "not in the prescription"))
    return False


def main() -> None:
    bases = _bases()
    print(f"Judging the judge · {len(bases)} correct explanations · judge {judge._client()[1]} / {judge._client()[2]}\n", flush=True)
    jobs = []
    for base in bases:
        rx = _cleaned(base["input"])
        versions = [("clean", base["output"], "")]
        for kind, plant in PLANTS.items():
            if planted := plant(base["output"]):
                versions.append((kind, *planted))
        jobs += [(base, rx, kind, text, change) for kind, text, change in versions]

    rows = []
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        ratings = pool.map(lambda job: judge.rate(job[1], job[3]), jobs)
        for (base, rx, kind, text, change), r in zip(jobs, ratings):
            reasons = " ".join(list((r.get("reasons") or {}).values()) + [r.get("verdict", "")] + r.get("problems", []))
            expected = EXPECTED_CRITERION.get(kind)
            crit = r["criteria"].get(expected) if expected else None
            row = {
                "case": base["id"], "kind": kind, "change": change, "score": r["score"], "criteria": r["criteria"],
                "reasons": r.get("reasons"), "quotes": r.get("quotes"), "unverified": r.get("unverified", []),
                "verdict": r.get("verdict"),
                "flagged": r["score"] is not None and r["score"] < judge.PASS_SCORE,
                "right_criterion": expected is not None and crit is not None and crit <= CAUGHT_MAX,
                "reason_points_at_it": kind != "clean" and _mentions(reasons, kind, change),
            }
            # Harsh: caught = low score AND the right criterion AND a reason that names the planted error.
            row["caught"] = (kind != "clean" and r["score"] is not None and r["score"] <= CAUGHT_MAX
                             and row["right_criterion"] and row["reason_points_at_it"])
            rows.append(row)
            tag = "clean" if kind == "clean" else kind
            print(f"{base['id']:<22} {tag:<17} judge {r['score']}/5   "
                  + (f"false alarm={'YES' if row['flagged'] else 'no'}" if kind == "clean" else
                     f"CAUGHT={'yes' if row['caught'] else 'NO '}  low={'yes' if row['flagged'] else 'no '}  "
                     f"right criterion={'yes' if row['right_criterion'] else 'no '}  names the error={'yes' if row['reason_points_at_it'] else 'NO '}")
                  + (f"  made-up reasons: {', '.join(row['unverified'])}" if row["unverified"] else ""),
                  flush=True)

    planted = [r for r in rows if r["kind"] != "clean" and r["score"] is not None]
    clean = [r for r in rows if r["kind"] == "clean" and r["score"] is not None]
    by_kind = {k: {"caught": sum(r["caught"] for r in planted if r["kind"] == k),
                   "low_score": sum(r["flagged"] for r in planted if r["kind"] == k),
                   "total": sum(r["kind"] == k for r in planted)} for k in PLANTS}
    every = [r for r in rows if r["score"] is not None]
    summary = {
        "planted_errors": len(planted),
        "caught": sum(r["caught"] for r in planted),
        "low_score_only": sum(r["flagged"] for r in planted),
        "made_up_reasons": sum(len(r["unverified"]) for r in every),
        "ratings_with_made_up_reasons": sum(bool(r["unverified"]) for r in every),
        "right_criterion": sum(r["right_criterion"] for r in planted),
        "reason_points_at_it": sum(r["reason_points_at_it"] for r in planted),
        "clean": len(clean), "false_alarms": sum(r["flagged"] for r in clean),
        "by_kind": by_kind,
    }
    out = tp.HERE / "data" / f"judge_eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"judge": judge._client()[2], "summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    p = summary["planted_errors"] or 1
    print(f"\nPlanted errors CAUGHT (score <= {CAUGHT_MAX}, right criterion, names the error): "
          f"{summary['caught']}/{summary['planted_errors']}  ({round(100 * summary['caught'] / p)}%)")
    print(f"Planted errors that at least got a low score (< {judge.PASS_SCORE}): {summary['low_score_only']}/{summary['planted_errors']}")
    for k, v in by_kind.items():
        print(f"   {k:<17} caught {v['caught']}/{v['total']}   low score {v['low_score']}/{v['total']}")
    print(f"Right criterion lowered:             {summary['right_criterion']}/{summary['planted_errors']}")
    print(f"Reason actually points at the error: {summary['reason_points_at_it']}/{summary['planted_errors']}")
    print(f"False alarms on clean explanations:  {summary['false_alarms']}/{summary['clean']}")
    print(f"Made-up reasons (quote not in the text): {summary['made_up_reasons']} in "
          f"{summary['ratings_with_made_up_reasons']}/{len(every)} ratings")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
