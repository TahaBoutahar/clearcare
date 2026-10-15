"""
translation_pipeline.py — bridge from ClearCare's explainer (English) to the patient's language:
Moroccan Darija, Modern Standard Arabic, French, Spanish, Portuguese, Italian, German, Dutch, Turkish.

    python translation_pipeline.py -f ../examples/angine.txt
    python translation_pipeline.py "Amoxicilline 1 g : 1 cp matin et soir pendant 8 jours" --arabizi
    python translation_pipeline.py -f ../examples/angine.txt --lang fr

Pipeline:
    prescription
      -> ClearCare explainer (../ai_pipeline.py): personal details removed, read, checked,
         plain-English explanation                                     [unchanged]
      -> this bridge:
           1. dose sentences (amount, times, food, duration, limits) from fixed Darija templates:
              no model ever re-words a dose
           2. short free-text sentences only (what a medicine is for, the illness, the doctor's
              instructions, apologies) go to darija_translator.translate_to_darija(), one by one
           3. every translated sentence must keep the same numbers and medicine names, otherwise
              the English sentence is kept and reported
           4. reports which model translated; Helsinki opus-mt-en-ar gives Modern Standard Arabic,
              not Darija, and is flagged as such
           5. optional Arabizi (Latin letters), converted word by word

darija_translator.py is the translation module (darija_med), used as is.
Local only until it is validated; the Darija templates must be checked by a native speaker.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

from doctor_cli import load_settings  # noqa: E402  (loads the project's .env, then the pipeline)

load_settings(PROJECT / ".env")
import ai_pipeline as pipeline  # noqa: E402
import darija_translator as darija  # noqa: E402
import judge  # noqa: E402
from languages import LANGUAGES  # noqa: E402
from privacy import keep_medical_only  # noqa: E402

logging.getLogger("darija_translator").setLevel(logging.WARNING)
for noisy in ("httpx", "httpx2", "openai", "transformers"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

CACHE_PATH = HERE / "data" / "darija_cache.json"
_cache_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Fixed templates for every language: languages.py. Doses never go through a model.
# --------------------------------------------------------------------------- #

T = LANGUAGES["ary"]  # kept for older callers
_FORM_WORDS = [("capsule", r"capsule|g[ée]l"), ("pill", r"tablet|pill|tab|comprim"), ("sachet", r"sachet"),
               ("puff", r"puff|bouff"), ("drop", r"drop|goutte"), ("lozenge", r"lozenge|pastille"), ("ml", r"\bml\b"),
               ("spoon", r"spoon|cuill")]


def _quantity(amount: str | None):
    """"2 tablets" -> ("2", "pill"); None when it cannot be templated."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?|\d+\s*/\s*\d+|½)\s+(.+)$", amount or "")
    if not m:
        return None
    form = next((f for f, pattern in _FORM_WORDS if re.search(pattern, m.group(2).lower())), None)
    return (m.group(1).replace(" ", ""), form) if form else None


def _amount(amount: str | None, L: dict = T) -> str | None:
    q = _quantity(amount)
    return L["amount"](*q) if q else None


def _duration(duration: str, L: dict = T) -> str | None:
    m = re.match(r"^\s*(\d+)\s*(day|week|month)s?\s*$", duration or "", re.IGNORECASE)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if "unit_count" in L:
        return L["unit_count"](n, unit)
    singular, plural = L["units"][unit][:2]
    return f"{n} {singular if n == 1 else plural}"


def _join(items: list[str], L: dict = T) -> str:
    return items[0] if len(items) == 1 else L["sep"].join(items[:-1]) + L["and"] + items[-1]


# --------------------------------------------------------------------------- #
# Free text -> the patient's language, with the safety checks
#
# TRANSLATOR=amd (default): the AI model on AMD translates all short sentences of a sheet in one
#   request, for every language (Darija included).
# TRANSLATOR=local: darija_translator.py on this computer (Darija only; needs torch).
# --------------------------------------------------------------------------- #

import os  # noqa: E402

TRANSLATOR = (os.getenv("TRANSLATOR") or "amd").strip().lower()

_TRANSLATE_PROMPT = """You translate short sentences of a medicine explanation for a patient into {language}.
Rules:
- Translate the meaning exactly. Add nothing, remove nothing, no advice, no explanation.
- Keep every medicine name exactly as written, in Latin letters.
- Keep every number as the same digits (0-9).
- Use short, everyday words a patient who does not read well understands.
Return JSON only: {{"1": "translation of sentence 1", "2": "...", ...}}"""


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def model_in_use(lang: str = "ary") -> str | None:
    if TRANSLATOR == "local" and lang == "ary":
        loaded = darija._translation_pipeline
        return loaded[2] if loaded else None
    return f"{pipeline.LLM_PROVIDER} · {pipeline.LLM_MODEL}"


def _ask_model(sentences: list[str], lang: str) -> dict[str, str]:
    """All sentences in one request; returns {english: translation} for those it answered."""
    if TRANSLATOR == "local" and lang == "ary":
        return {e: darija.translate_to_darija(e, source_lang="en") for e in sentences}
    numbered = "\n".join(f"{i}. {e}" for i, e in enumerate(sentences, 1))
    raw = pipeline._chat(_TRANSLATE_PROMPT.format(language=LANGUAGES[lang]["model_name"]), numbered,
                         json_mode=True, max_tokens=200 + 120 * len(sentences))
    data = pipeline._parse_json_object(raw)
    return {e: str(data[str(i)]).strip() for i, e in enumerate(sentences, 1) if data.get(str(i))}


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def translate_sentences(sentences: list[str], names: list[str], lang: str = "ary") -> tuple[dict, list[dict]]:
    """English sentences -> {english: translation}. A sentence that fails a check stays in English and is reported."""
    sentences = list(dict.fromkeys(s for s in sentences if s and s.strip()))
    if TRANSLATOR == "local" and lang == "ary":
        darija._load_translation_model()  # so we know which model (Darija or the MSA fallback) will answer
    model = model_in_use(lang) or "none"
    with _cache_lock:
        cache = _load_cache()
    keys = {e: f"{lang}::{model}::{e}" for e in sentences}  # per language and model
    todo = [e for e in sentences if keys[e] not in cache]
    answers, error = {}, None
    if todo:
        try:
            answers = _ask_model(todo, lang)
        except Exception as exc:  # the translation failing must never block the sheet: English is kept
            error = f"the translation service failed ({exc})"
    result, problems, new = {}, [], {}
    for english in sentences:
        translated = cache.get(keys[english], answers.get(english))
        why = None
        if translated is None:
            why = error or "the model gave no translation"
        else:
            translated = translated.translate(_ARABIC_DIGITS)
            if translated.startswith("[ERROR]"):
                why = translated
            elif pipeline._numbers(english) != pipeline._numbers(translated):
                why = f"numbers changed ({sorted(pipeline._numbers(english))} -> {sorted(pipeline._numbers(translated))})"
            elif lost := [n for n in names if n.lower() in english.lower() and n.lower() not in translated.lower()]:
                why = f"medicine name changed: {', '.join(lost)}"
            elif junk := _looks_broken(english, translated, names, lang):
                why = junk
        if why:
            result[english] = english
            problems.append({"sentence": english, "translation": translated, "why": why})
        else:
            result[english] = translated.strip()
            new[keys[english]] = translated.strip()
    if new:
        with _cache_lock:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps({**_load_cache(), **new}, ensure_ascii=False, indent=1), encoding="utf-8")
    return result, problems


def _looks_broken(english: str, translated: str, names: list[str], lang: str = "ary") -> str | None:
    """Catch garbage such as "(ب) (((((())", or an answer in the wrong script: the number and name checks cannot see it."""
    text = translated
    for name in names:  # medicine names are Latin on purpose
        text = re.sub(re.escape(name), "", text, flags=re.IGNORECASE)
    letters = [c for c in text if c.isalpha()]
    arabic = sum("؀" <= c <= "ۿ" for c in letters)
    rtl = LANGUAGES[lang]["dir"] == "rtl"
    if not letters or (rtl and arabic / len(letters) < 0.7):
        return "the translation is mostly not Arabic"
    if not rtl and arabic / len(letters) > 0.1:
        return f"the translation is not in {LANGUAGES[lang]['name']}"
    if not rtl and translated.strip().lower() == english.strip().lower() and len(english.split()) > 2:
        return "the sentence was not translated"
    if re.search(r"([()\[\]{}<>|*#_~^=+-])\1|[()]{3,}", translated):
        return "the translation contains broken symbols"
    ratio = len(translated.strip()) / max(len(english.strip()), 1)
    if ratio < 0.3 or ratio > 3:
        return f"the translation is {'much shorter' if ratio < 0.3 else 'much longer'} than the English"
    return None


def _free_text(extraction: dict) -> list[str]:
    phrases = [pipeline._sentence(c) for c in extraction.get("conditions", [])]
    for med in extraction["medicines"]:
        if med.get("purpose"):
            phrases.append(pipeline._sentence(med["purpose"]))
        phrases += pipeline._sorry_sentences(med)
        for field in ("needed_for", "route"):
            if med.get(field):
                phrases.append(med[field])
        for field in ("amount_per_dose", "max_per_day", "duration"):
            if med.get(field) and not (_quantity(med[field]) if field != "duration" else
                                       (_duration(med[field]) or "long" in med[field].lower())):
                phrases.append(med[field])
    for line in extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else []):
        phrases.append(pipeline._sentence(line))
    for issue in extraction.get("issues", []):
        if issue["level"] == "unsure":
            phrases.append(_issue_line(issue))
    return phrases


def _issue_line(issue: dict) -> str:
    what = issue["what"][:1].upper() + issue["what"][1:]
    return f"{what}: {pipeline._lower_first(issue['why'])}."


# --------------------------------------------------------------------------- #
# The patient sheet in any language
# --------------------------------------------------------------------------- #

def _how(med: dict, tr: dict, L: dict = T) -> list[str]:
    """How to take it, from the fixed templates. Mirrors ai_pipeline._how_sentences()."""
    name = pipeline.short_name(med)
    q = _quantity(med.get("amount_per_dose"))
    if q:
        dose = L["dose"](q[0], q[1], name)
    elif med.get("amount_per_dose"):
        dose = tr.get(med["amount_per_dose"], med["amount_per_dose"])
    else:
        dose = None

    when, extras = [], []
    if med["only_when_needed"]:
        need = med.get("needed_for")
        when.append(L["only_when"](tr.get(need, need).rstrip(".!؟。 ")) if need else L["only_when_needed"])
        if med.get("times_per_day"):
            extras.append(L["up_to"](med["times_per_day"]))
    else:
        moments = med.get("times_of_day") or []
        if med.get("times_per_week"):  # weekly first: "in the morning" alone would read as every day
            when.append(L["n_a_week"](med["times_per_week"]))
            if med.get("weekday"):
                extras.append(L["on_day"](L["days"].get(med["weekday"], med["weekday"])))
            if moments:
                extras.append(_join([L["time"][m] for m in moments], L))
        elif moments:
            when.append(_join([L["time"][m] for m in moments], L))
        elif med.get("single_dose"):
            when.append(L["single_dose"])
        elif med.get("times_per_day_range"):
            when.append(L["range_a_day"](*med["times_per_day_range"]))
        elif med.get("times_per_day"):
            when.append(L["n_a_day"](med["times_per_day"]))

    if med.get("food"):
        extras.append(L["food"][med["food"]])
    long_term = bool(med.get("duration")) and "long" in med["duration"].lower()
    if med.get("duration") and not long_term:
        extras.append(L["for_duration"](_duration(med["duration"], L) or tr.get(med["duration"], med["duration"])))

    out = []
    if dose or when or extras:
        take = L["take"] if dose else L["take_it"]
        out.append(L["line"](take, dose or "", " ".join(when), extras).rstrip(".") + ".")
    if med.get("route") and med["route"].lower() not in ("by mouth", "oral", "orally", "po"):
        route = tr.get(med["route"], med["route"]).strip().rstrip(".")
        out.append(route[:1].upper() + route[1:] + ".")
    if med.get("every_hours"):
        out.append(L["wait_hours"](med["every_hours"]))
    if med.get("max_per_day"):
        out.append(L["max_day"](_amount(med["max_per_day"], L) or tr.get(med["max_per_day"], med["max_per_day"])))
    if long_term:
        out.append(L["long_term"])
    return out


def to_language(extraction: dict, lang: str = "ary") -> dict:
    """The patient sheet in one language, plus a safety report."""
    if lang not in LANGUAGES:
        raise ValueError(f"Unknown language '{lang}'. Choose from: {', '.join(LANGUAGES)}")
    L = LANGUAGES[lang]
    names = [pipeline.short_name(m) for m in extraction["medicines"]]
    tr, problems = translate_sentences(_free_text(extraction), names, lang)
    model = model_in_use(lang)
    sep = L["sep"]

    lines: list[str] = []
    if extraction.get("conditions"):
        lines += [L["Your illness"], *(tr[pipeline._sentence(c)] for c in extraction["conditions"]), ""]
    meds = extraction["medicines"]
    if meds:
        lines += [L["Your medicines"], L["intro"], ""]
    for n, med in enumerate(meds, 1):
        purpose = tr[pipeline._sentence(med["purpose"])] if med.get("purpose") else ""
        lines.append(f"{n}. {med['name_as_written']}" + (f" — {purpose}" if purpose else ""))
        lines += _how(med, tr, L)
        lines += [tr[s] for s in pipeline._sorry_sentences(med)]
        lines.append("")

    regular = [m for m in meds if not m["only_when_needed"]]
    if regular and all(m.get("times_of_day") and not m.get("times_per_week") for m in regular):
        lines.append(L["When to take them each day"])
        for slot in pipeline._TIME_ORDER:
            items = [m["name_as_written"] + (f" ({a})" if (a := _amount(m.get("amount_per_dose"), L)) else "")
                     for m in regular if slot in m["times_of_day"]]
            if items:
                lines.append(f"{L['slot'][slot]}: " + sep.join(items))
        if needed := [m["name_as_written"] + (f" ({a})" if (a := _amount(m.get("amount_per_dose"), L)) else "")
                      for m in meds if m["only_when_needed"]]:
            lines.append(f"{L['if_needed_slot']}: " + sep.join(needed))
        lines.append("")

    doctor = extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else [])
    if doctor:
        lines += [L["Also"], *(tr[pipeline._sentence(l)] for l in doctor), ""]
    unsure = [i for i in extraction.get("issues", []) if i["level"] == "unsure"]
    if unsure:
        lines += [L["Sorry, I could not understand everything"], *(tr[_issue_line(i)] for i in unsure),
                  L["ask_about_this"], ""]
    lines.append(L["disclaimer"])

    local = TRANSLATOR == "local" and lang == "ary"
    return {
        "language": lang, "language_name": L["name"], "native": L["native"], "dir": L["dir"],
        "text": "\n".join(lines).strip(),
        "model": model,
        # The local fallback model writes Modern Standard Arabic, not Darija.
        "is_right_language": not local or model == darija.DARIJA_MODEL,
        "kept_in_english": problems,
        "sentences_translated": len(tr) - len(problems),
    }


def to_darija(extraction: dict) -> dict:
    """The Darija sheet (older shape: "darija", "is_darija")."""
    out = to_language(extraction, "ary")
    return {**out, "darija": out["text"], "is_darija": out["is_right_language"]}


# --------------------------------------------------------------------------- #
# Arabizi (Latin letters), word by word
# --------------------------------------------------------------------------- #

_ARABIC_WORD = re.compile(r"[؀-ۿ]+")


def to_arabizi(text: str) -> str:
    """Like darija_translator.arabic_to_arabizi, but whole words only: short words such as
    "مع" or "من" are no longer replaced inside longer words."""
    def convert(match: re.Match) -> str:
        word = match.group(0)
        if word in darija.DARIJA_WORD_MAP:
            return darija.DARIJA_WORD_MAP[word]
        return "".join(darija.ARABIZI_MAP.get(ch, ch) for ch in word)
    return _ARABIC_WORD.sub(convert, text).replace("،", ",")


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #

def process(prescription: str, lang: str | None = "ary", arabizi: bool = False) -> dict:
    """Prescription -> checked English -> the patient's language (lang None: English only) -> judge."""
    extraction = pipeline.extract_prescription(prescription)
    english = pipeline.render_explanation(extraction)
    result = {"english": english, "needs_review": pipeline.needs_review(extraction),
              "review": pipeline.render_review(extraction)}
    if lang:
        result["translation"] = to_language(extraction, lang)
        if arabizi and lang == "ary":
            result["arabizi"] = to_arabizi(result["translation"]["text"])
    # Always last: a second model rates the final outputs from 0 to 5 (it never changes them).
    cleaned, _ = keep_medical_only(prescription)
    t = result.get("translation")
    result["rating"] = judge.rate(cleaned, english, t["text"] if t else None, t["language_name"] if t else None)
    result["needs_review"] = result["needs_review"] or not result["rating"]["passed"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Prescription -> plain English -> the patient's language.")
    parser.add_argument("text", nargs="*", help="prescription text")
    parser.add_argument("-f", "--file", type=Path, help="prescription in a text file")
    parser.add_argument("-p", "--provider", choices=sorted(pipeline._PROVIDERS), help="AI for reading the prescription")
    parser.add_argument("-l", "--lang", default="ary", choices=sorted(LANGUAGES) + ["en"],
                        help="language of the patient sheet (default: ary = Moroccan Darija; en = English only)")
    parser.add_argument("--arabizi", action="store_true", help="also show Darija in Latin letters")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    if args.provider:
        pipeline.configure(provider=args.provider)
    text = args.file.read_text(encoding="utf-8") if args.file else " ".join(args.text)
    if not text.strip():
        parser.error("give a prescription (text or -f file)")

    result = process(text, lang=None if args.lang == "en" else args.lang, arabizi=args.arabizi)
    print("=== English (ClearCare) ===\n" + result["english"])
    t = result.get("translation")
    if t:
        print(f"\n=== {t['language_name']} ({t['native']}) ===\n" + t["text"])
        if result.get("arabizi"):
            print("\n=== Arabizi ===\n" + result["arabizi"])
    print("\n=== Safety report ===")
    if t:
        print(f"Translation model: {t['model']}" + ("" if t["is_right_language"] else
              "  <-- NOT Darija: this fallback writes Modern Standard Arabic"))
        print(f"Free-text sentences translated: {t['sentences_translated']}")
        for p in t["kept_in_english"]:
            print(f"KEPT IN ENGLISH: {p['sentence']!r} ({p['why']})")
    print("\n".join(result["review"]))
    print("\n=== Second-model check ===\n" + "\n".join(judge.format_rating(result["rating"])))


if __name__ == "__main__":
    main()
