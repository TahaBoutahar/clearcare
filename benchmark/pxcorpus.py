"""
Turn PxCorpus (real spoken French drug prescriptions, CC BY 4.0) into a benchmark test set.

    python -m benchmark.pxcorpus            # reads data/PxCorpus/, writes benchmark/pxcorpus.jsonl

Source: Kocabiyikoglu et al., "A spoken drug prescription dataset in French for spoken
language understanding", LREC 2022. https://zenodo.org/records/6482587
Only the transcripts and labels are used (no audio).

The model only ever receives the transcript (seq.in). The human annotations (seq.out: one
label per word, e.g. B-dos_val, B-rhythm_tdte, B-dur_ut) are turned into expected answers
used by the scorer alone. A field is only checked when the annotation states it without
ambiguity (e.g. one amount, times of day we can name, one duration).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "data" / "PxCorpus"
OUT = Path(__file__).resolve().parent / "pxcorpus.jsonl"

WORD_NUMBERS = {"un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "six": "6",
                "sept": "7", "huit": "8", "neuf": "9", "dix": "10", "quinze": "15", "vingt": "20", "trente": "30"}
UNITS = {"comprimé": "tablet", "comprimés": "tablet", "gélule": "capsule", "gélules": "capsule",
         "capsule": "capsule", "capsules": "capsule", "sachet": "sachet", "sachets": "sachet",
         "goutte": "drop", "gouttes": "drop", "bouffée": "puff", "bouffées": "puff"}
TIMES = {"matin": "morning", "matins": "morning", "midi": "noon", "soir": "evening", "soirs": "evening"}
DURATION_UNITS = {"jour": "days", "jours": "days", "semaine": "weeks", "semaines": "weeks", "mois": "months"}


def _spans(tokens: list[str], tags: list[str]) -> list[tuple[str, str]]:
    """BIO tags -> [(slot, text)]."""
    out, current = [], None
    for token, tag in zip(tokens, tags):
        if tag.startswith("B-"):
            current = [tag[2:], [token]]
            out.append(current)
        elif tag.startswith("I-") and current and current[0] == tag[2:]:
            current[1].append(token)
        else:
            current = None
    return [(slot, " ".join(words)) for slot, words in out]


def _number(text: str) -> str | None:
    text = text.strip().lower().replace(",", ".")
    return text if text.replace(".", "", 1).isdigit() else WORD_NUMBERS.get(text)


def expected_from_labels(spans: list[tuple[str, str]], text: str) -> dict:
    """What a correct reading must contain, built only from the human annotation.
    A field is left out whenever the annotation is ambiguous."""
    get = lambda slot: [v for s, v in spans if s == slot]
    exp: dict = {}
    lower = text.lower()

    # One amount, with a unit, that is a per-dose amount (not "3 comprimés par jour" = daily total).
    vals, units = get("dos_val"), get("dos_uf")
    if len(vals) == 1 and len(units) == 1 and not re.search(r"\b(par|/)\s*(jour|j)\b", lower):
        number, unit = _number(vals[0]), UNITS.get(units[0].split()[0].lower())
        if number and unit:
            exp["amount"] = f"{number} {unit}{'s' if number != '1' else ''}"

    # Times of day: annotators sometimes label "midi" as a clock time (rhythm_hour), so look at both.
    time_words = get("rhythm_tdte") + [h for h in get("rhythm_hour") if h.lower().strip("/") in TIMES]
    times = [TIMES.get(t.lower().strip("/")) for t in time_words]
    if times and all(times):
        exp["times_of_day"] = sorted(set(times), key=["morning", "noon", "evening"].index)

    per_day = {_number(v) for v in get("rhythm_perday")}
    if len(per_day) == 1 and (n := next(iter(per_day))) and n.isdigit():
        exp["times_per_day"] = int(n)

    for val_slot, unit_slot in (("dur_val", "dur_ut"), ("qsp_val", "qsp_ut")):
        vals, units = get(val_slot), get(unit_slot)
        if len(vals) == 1 and len(units) == 1 and (n := _number(vals[0])) and units[0].lower() in DURATION_UNITS:
            exp["duration"] = f"{n} {DURATION_UNITS[units[0].lower()]}"
            break

    if get("dos_cond"):
        exp["prn"] = True
    if get("fasting"):
        exp["food"] = ["empty stomach", "before food"]

    every = [(_number(v), u.lower()) for v, u in zip(get("freq_int_v1"), get("freq_int_v1_ut"))]
    if len(every) == 1 and every[0][0] and every[0][1].startswith("heure"):
        exp["every_hours"] = int(float(every[0][0]))
    return exp


def build() -> list[dict]:
    lines = lambda name: (SOURCE / name).read_text(encoding="utf-8").splitlines()
    cases = []
    for i, (text, tags, intent) in enumerate(zip(lines("seq.in"), lines("seq.out"), lines("seq.label")), 1):
        spans = _spans(text.split(), tags.split())
        case = {"id": f"px-{i:04d}", "category": f"PxCorpus · {intent}", "input": text}
        if intent != "medical_prescription":
            # Dialogue turns ("non", "remplacer par ...", remarks): no expected reading,
            # refusing them is fine; only the safety checks apply.
            case["allow_error"] = True
            cases.append(case)
            continue
        drugs = [v for s, v in spans if s in ("drug", "inn")]
        fields = expected_from_labels(spans, text)
        if len(drugs) == 1:
            case["meds"] = [{"name": drugs[0], **fields}]
        elif not drugs:
            case["meds"] = [{"name": None, **fields}]  # dose spoken without the drug name
        else:
            case["meds"] = [{"name": d} for d in drugs]  # which dose goes with which drug is ambiguous
        cases.append(case)
    return cases


def main() -> None:
    cases = build()
    with OUT.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    checked = sum(len([k for k in m if k != "name"]) for c in cases for m in c.get("meds", []))
    print(f"Wrote {len(cases)} cases ({checked} expected fields) to {OUT}")


if __name__ == "__main__":
    main()
