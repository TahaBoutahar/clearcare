"""
Generate random prescriptions with their correct answers, for testing at scale.

    python -m benchmark.generate --seed 7 -n 40      # writes benchmark/random_seed7.jsonl

Each prescription mixes 1-3 real medicines with random amounts, timing, food, duration and
"when needed", written in a random style (clean French, abbreviated, sloppy lowercase without
accents, "1-0-1" notation, English/Latin, voice-like with numbers as words), with optional
diagnosis, doctor's notes and a personal header. The generator knows what it wrote, so the
expected answers are exact. The same seed always gives the same cases.
"""

from __future__ import annotations

import argparse
import json
import random
import unicodedata
from pathlib import Path

# (name as written, generic, strength, form)
DRUGS = [
    ("Amoxicilline", "amoxicillin", "1 g", "tablet"), ("Augmentin", "amoxicillin clavulanate", "1 g", "sachet"),
    ("Doliprane", "paracetamol", "1000 mg", "tablet"), ("Paracétamol", "paracetamol", "500 mg", "capsule"),
    ("Ibuprofène", "ibuprofen", "400 mg", "tablet"), ("Spasfon", "phloroglucinol", "80 mg", "tablet"),
    ("Oméprazole", "omeprazole", "20 mg", "capsule"), ("Ésoméprazole", "esomeprazole", "40 mg", "tablet"),
    ("Metformine", "metformin", "850 mg", "tablet"), ("Amlodipine", "amlodipine", "5 mg", "tablet"),
    ("Cétirizine", "cetirizine", "10 mg", "tablet"), ("Desloratadine", "desloratadine", "5 mg", "tablet"),
    ("Azithromycine", "azithromycin", "250 mg", "tablet"), ("Ciprofloxacine", "ciprofloxacin", "500 mg", "tablet"),
    ("Smecta", "diosmectite", "3 g", "sachet"), ("Motilium", "domperidone", "10 mg", "tablet"),
    ("Vitamine D3", "cholecalciferol", "1000 UI", "capsule"), ("Atorvastatine", "atorvastatin", "20 mg", "tablet"),
    ("Losartan", "losartan", "50 mg", "tablet"), ("Fluconazole", "fluconazole", "150 mg", "capsule"),
]
FORM_WORDS = {  # style -> form -> (singular, plural)
    "fr": {"tablet": ("comprimé", "comprimés"), "capsule": ("gélule", "gélules"), "sachet": ("sachet", "sachets")},
    "abbr": {"tablet": ("cp", "cp"), "capsule": ("gél", "gél"), "sachet": ("sach", "sach")},
    "en": {"tablet": ("tab", "tabs"), "capsule": ("cap", "caps"), "sachet": ("sachet", "sachets")},
}
FR_NUMBERS = {1: "un", 2: "deux", 3: "trois", 4: "quatre", 5: "cinq", 6: "six", 7: "sept", 8: "huit", 10: "dix", 14: "quatorze"}
TIMES_FR = {"morning": "matin", "noon": "midi", "evening": "soir", "bedtime": "au coucher"}
DIAGNOSES = [("Angine", r"throat|angin|tonsil"), ("Grippe", r"flu|influenza|grippe"), ("HTA", r"blood pressure|hypertension"),
             ("Gastro-entérite", r"gastro|stomach|diarr"), ("Otite", r"ear|otitis"), ("Infection urinaire", r"urin|bladder")]
NOTES = ["Repos.", "Boire beaucoup d'eau.", "Revoir dans 15 jours.", "Contrôle dans 1 mois.", ""]
NAMES = ["Fatima Zahra Bennis", "Youssef El Amrani", "Khadija Ouali", "Omar Tazi", "Salma Idrissi"]


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _medicine(rng: random.Random, style: str) -> tuple[str, dict]:
    name, generic, strength, form = rng.choice(DRUGS)
    n = rng.choice([1, 1, 1, 2])
    kind = rng.choice(["times", "per_day", "per_day", "prn", "every_h"])
    if style == "notation":
        kind = "times"
    if style == "en" and kind == "times":
        kind = "per_day"
    food = rng.choice([None, None, "with food", "empty stomach", "after food"])
    duration = rng.choice([None, ("days", rng.choice([3, 5, 7, 8, 10, 14])), ("months", rng.choice([1, 3])), "long term"])
    expected = {"name": _strip_accents(name).lower(), "amount": f"{n} {form}{'s' if n > 1 else ''}"}

    sing, plur = FORM_WORDS["en" if style == "en" else "abbr" if style in ("abbr", "notation") else "fr"][form]
    unit = sing if n == 1 else plur
    num = FR_NUMBERS[n] if style == "voice" else str(n)
    parts = [f"{num} {unit}"]

    if kind == "times":
        times = sorted(rng.sample(["morning", "noon", "evening", "bedtime"], rng.choice([1, 2, 2, 3])),
                       key=["morning", "noon", "evening", "bedtime"].index)
        expected["times_of_day"] = times
        if style == "notation":
            slots = ["morning", "noon", "evening"]
            times = [t for t in times if t in slots] or ["morning"]
            expected["times_of_day"] = times
            parts = ["-".join(str(n) if s in times else "0" for s in slots)]
            expected.pop("amount")  # "1-0-1" gives the count, not the form
        else:
            words = [TIMES_FR[t] for t in times]
            parts.append(("le " if len(words) == 1 and words[0] != "au coucher" else "") + " et ".join(words))
    elif kind == "per_day":
        k = rng.choice([2, 3, 4])
        expected["times_per_day"] = k
        parts.append({"fr": f"{k} fois par jour", "abbr": f"x {k}/j", "en": {2: "BID", 3: "TID", 4: "QID"}[k],
                      "sloppy": f"{k}x/j", "voice": f"{FR_NUMBERS[k]} fois par jour"}[style])
    elif kind == "prn":
        expected["prn"] = True
        reason = rng.choice(["douleur", "fièvre"])
        parts.append({"en": "PRN pain" if reason == "douleur" else "PRN fever"}.get(style, f"si {reason}"))
        if rng.random() < 0.6:
            m = rng.choice([3, 4])
            expected["max_per_day"] = f"{m} {form}s"
            parts.append({"en": f"max {m} {plur}/day"}.get(style, f"max {m} {FORM_WORDS['abbr'][form][1]}/j"))
        duration = None
    else:  # every N hours
        h = rng.choice([6, 8, 12])
        expected["every_hours"] = h
        parts.append({"en": f"q{h}h"}.get(style, f"toutes les {h} h"))

    if food and style != "notation" and kind != "prn":
        expected["food"] = [food, "before food"] if food == "empty stomach" else food
        parts.append({"with food": {"en": "with meals"}.get(style, "au milieu des repas"),
                      "empty stomach": {"en": "on an empty stomach"}.get(style, "à jeun"),
                      "after food": {"en": "after meals"}.get(style, "après les repas")}[food])
    if duration == "long term":
        expected["duration"] = "long term"
        parts.append({"en": "long term"}.get(style, "au long cours"))
    elif duration:
        unit_en, d = duration
        expected["duration"] = f"{d} {unit_en}"
        fr_unit = "jours" if unit_en == "days" else "mois"
        parts.append({"en": f"x {d} {unit_en}", "abbr": f"pdt {d}{'j' if unit_en == 'days' else ' mois'}",
                      "voice": f"pendant {FR_NUMBERS.get(d, d)} {fr_unit}"}.get(style, f"pendant {d} {fr_unit}"))

    label = generic.capitalize() if style == "en" else name
    if style == "en":
        expected["name"] = generic.split()[0]
    sep = " " if style in ("voice", "sloppy", "en") else ", "
    text = f"{label} {strength}{' :' if style in ('fr', 'notation') else ''} " + sep.join(parts)
    if style == "sloppy":
        text = _strip_accents(text).lower()
    elif style == "abbr":
        text = text.upper() if rng.random() < 0.5 else text
    if style not in ("voice",):
        text += "."
    return text, expected


def make_case(rng: random.Random, i: int) -> dict:
    style = rng.choice(["fr", "fr", "abbr", "sloppy", "notation", "en", "voice"])
    lines, meds, private = [], [], []
    if rng.random() < 0.3 and style != "en":
        name = rng.choice(NAMES)
        phone = f"06 {rng.randint(10, 99)} {rng.randint(10, 99)} {rng.randint(10, 99)} {rng.randint(10, 99)}"
        lines += [f"Patient : {name}", f"Tél : {phone}"]
        private += [name.split()[0], name.split()[-1], phone]
    case = {"id": f"rand-{i:03d}", "category": f"random · {style}"}
    if rng.random() < 0.5:
        diag, pattern = rng.choice(DIAGNOSES)
        lines.append(f"Diagnostic : {diag}" if style != "sloppy" else f"diag: {_strip_accents(diag).lower()}")
        case["conditions"] = [pattern]
    used = set()
    for _ in range(rng.choice([1, 1, 2, 2, 3])):
        text, expected = _medicine(rng, style)
        if expected["name"] in used:
            continue
        used.add(expected["name"])
        lines.append(text)
        meds.append(expected)
    if note := rng.choice(NOTES):
        lines.append(note)
    joiner = " " if style == "voice" else "\n"
    case["input"] = joiner.join(lines)
    case["meds"] = meds
    if private:
        case["private"] = private
    return case


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate random prescriptions with expected answers.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("-n", type=int, default=40)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    path = Path(__file__).resolve().parent / f"random_seed{args.seed}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i in range(1, args.n + 1):
            f.write(json.dumps(make_case(rng, i), ensure_ascii=False) + "\n")
    print(f"Wrote {args.n} cases to {path}")


if __name__ == "__main__":
    main()
