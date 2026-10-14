"""
translator.py — the patient explanation in English, French or Arabic.

Safe by design:
    - Everything that carries a dose (amount, times, food, duration, limits) is built from fixed
      sentence templates in each language. The AI never re-words a dose.
    - Only short free text is translated by the AI: what each medicine is for, the illness, the
      doctor's own instructions, "when needed" reasons and apologies. A translation must contain
      exactly the same numbers as the English; otherwise the English is kept and reported.
    - Medicine names are never translated.

The templates must be checked once by a native-speaking health professional before real use.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import ai_pipeline as pipeline

LANGUAGES = {"en": "English", "fr": "Français", "ar": "العربية"}
RTL = {"ar"}
TTS_LANG = {"en": "english", "fr": "french", "ar": "arabic"}

CACHE_PATH = Path(__file__).resolve().parent / "data" / "translations.json"
_cache_lock = threading.Lock()


def _de(name: str) -> str:
    return f"d'{name}" if re.match(r"[aeiouyhAEIOUYHÉéÈè]", name) else f"de {name}"


def _ar_dose(n: str, form: tuple, name: str, one: str, of: str) -> str:
    """Arabic: "one pill" and "two pills" are said differently from 3+."""
    singular, plural, dual = form
    if n == "1":
        return f"{singular} {one} {of} {name}"
    if n == "2":
        return f"{dual} {of} {name}"
    return f"{n} {plural} {of} {name}"



# --------------------------------------------------------------------------- #
# Fixed templates (dose sentences and labels)
# --------------------------------------------------------------------------- #

T: dict[str, dict] = {
    "en": {
        "Your illness": "Your illness", "Your medicines": "Your medicines", "Also": "Also",
        "When to take them each day": "When to take them each day",
        "Sorry, I could not understand everything": "Sorry, I could not understand everything",
        "intro": "Here is how to take your medicines.",
        "disclaimer": "If you are not sure about anything, ask your doctor or pharmacist.",
        "ask_about_this": "Please ask your doctor or pharmacist about this.",
        "slot": {"morning": "Morning", "noon": "Noon", "evening": "Evening", "bedtime": "Bedtime"},
        "if_needed_slot": "Only if needed",
        "checked": lambda date: f"Checked by your doctor on {date}.",
    },
    "fr": {
        "Your illness": "Votre maladie", "Your medicines": "Vos médicaments", "Also": "Aussi",
        "When to take them each day": "Quand les prendre chaque jour",
        "Sorry, I could not understand everything": "Désolé, je n'ai pas tout compris",
        "intro": "Voici comment prendre vos médicaments.",
        "disclaimer": "Si vous avez un doute, demandez à votre médecin ou à votre pharmacien.",
        "ask_about_this": "Demandez à votre médecin ou à votre pharmacien.",
        "take": "Prenez", "take_it": "Prenez-le",
        "time": {"morning": "le matin", "noon": "à midi", "evening": "le soir", "bedtime": "au coucher"},
        "slot": {"morning": "Matin", "noon": "Midi", "evening": "Soir", "bedtime": "Coucher"},
        "if_needed_slot": "Seulement si besoin", "and": "et",
        "once_a_day": "une fois par jour", "n_a_day": lambda n: f"{n} fois par jour",
        "once_a_week": "une fois par semaine", "n_a_week": lambda n: f"{n} fois par semaine",
        "on_day": lambda d: f"le {d}",
        "days": {"Monday": "lundi", "Tuesday": "mardi", "Wednesday": "mercredi", "Thursday": "jeudi",
                 "Friday": "vendredi", "Saturday": "samedi", "Sunday": "dimanche"},
        "only_when": lambda need: f"seulement en cas de {need}", "only_when_needed": "seulement si besoin",
        "up_to": lambda n: "jusqu'à une fois par jour" if n == 1 else f"jusqu'à {n} fois par jour",
        "food": {"with food": "pendant le repas", "before food": "avant de manger",
                 "after food": "après avoir mangé", "empty stomach": "à jeun, avant de manger quoi que ce soit"},
        "for_duration": lambda d: f"pendant {d}",
        "units": {"day": ("jour", "jours"), "week": ("semaine", "semaines"), "month": ("mois", "mois")},
        "wait_hours": lambda h: f"Attendez au moins {h} heures avant d'en reprendre.",
        "max_day": lambda m: f"Ne prenez pas plus de {m} par jour.",
        "long_term": "C'est un traitement de longue durée.",
        "forms": {"pill": ("comprimé", "comprimés"), "sachet": ("sachet", "sachets"), "puff": ("bouffée", "bouffées"),
                  "drop": ("goutte", "gouttes"), "lozenge": ("pastille", "pastilles"), "ml": ("ml", "ml"),
                  "spoon": ("cuillère", "cuillères")},
        "dose": lambda n, form, name: f"{n} {form[0] if n in ('1', '1/2', '½') else form[1]} {_de(name)}",
        "checked": lambda date: f"Vérifié par votre médecin le {date}.",
    },
    "ar": {
        "Your illness": "مرضك", "Your medicines": "أدويتك", "Also": "أيضا",
        "When to take them each day": "متى تأخذها كل يوم",
        "Sorry, I could not understand everything": "عذرا، لم أفهم كل شيء",
        "intro": "إليك كيف تأخذ أدويتك.",
        "disclaimer": "إذا كان لديك أي شك، اسأل طبيبك أو الصيدلي.",
        "ask_about_this": "اسأل طبيبك أو الصيدلي.",
        "take": "خذ", "take_it": "خذه",
        "time": {"morning": "في الصباح", "noon": "عند الظهر", "evening": "في المساء", "bedtime": "قبل النوم"},
        "slot": {"morning": "الصباح", "noon": "الظهر", "evening": "المساء", "bedtime": "قبل النوم"},
        "if_needed_slot": "عند الحاجة فقط", "and": "و",
        "once_a_day": "مرة واحدة في اليوم", "n_a_day": lambda n: "مرتين في اليوم" if n == 2 else f"{n} مرات في اليوم",
        "once_a_week": "مرة واحدة في الأسبوع", "n_a_week": lambda n: f"{n} مرات في الأسبوع",
        "on_day": lambda d: f"يوم {d}",
        "days": {"Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء", "Thursday": "الخميس",
                 "Friday": "الجمعة", "Saturday": "السبت", "Sunday": "الأحد"},
        "only_when": lambda need: f"فقط في حالة {need}", "only_when_needed": "فقط عند الحاجة",
        "up_to": lambda n: "حتى مرة واحدة في اليوم" if n == 1 else ("حتى مرتين في اليوم" if n == 2 else f"حتى {n} مرات في اليوم"),
        "food": {"with food": "مع الأكل", "before food": "قبل الأكل", "after food": "بعد الأكل",
                 "empty stomach": "على معدة فارغة، قبل أن تأكل أي شيء"},
        "for_duration": lambda d: f"لمدة {d}",
        "units": {"day": ("يوم", "أيام"), "week": ("أسبوع", "أسابيع"), "month": ("شهر", "أشهر")},
        "wait_hours": lambda h: f"انتظر {h} ساعات على الأقل قبل أن تأخذه مرة أخرى.",
        "max_day": lambda m: f"لا تأخذ أكثر من {m} في اليوم.",
        "long_term": "هذا علاج طويل المدى.",
        "forms": {"pill": ("قرص", "أقراص", "قرصين"), "sachet": ("كيس", "أكياس", "كيسين"), "puff": ("بخة", "بخات", "بختين"),
                  "drop": ("قطرة", "قطرات", "قطرتين"), "lozenge": ("قرص للمص", "أقراص للمص", "قرصين للمص"),
                  "ml": ("مل", "مل", "2 مل"), "spoon": ("ملعقة", "ملاعق", "ملعقتين")},
        "dose": lambda n, form, name: _ar_dose(n, form, name, "واحد", "من"),
        "checked": lambda date: f"راجعه طبيبك بتاريخ {date}.",
    },
}

_FORM_WORDS = [("pill", r"tablet|capsule|pill|tab|cap|comprim|g[ée]l"), ("sachet", r"sachet"), ("puff", r"puff|bouff"),
               ("drop", r"drop|goutte"), ("lozenge", r"lozenge|pastille"), ("ml", r"\bml\b"), ("spoon", r"spoon|cuill")]


def _sep(lang: str) -> str:
    return "، " if lang in RTL else ", "


def _join(items: list[str], word: str, lang: str) -> str:
    if len(items) == 1:
        return items[0]
    if lang in RTL:
        return "، ".join(items[:-1]) + f" {word}" + items[-1]
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def _quantity(amount: str | None):
    """"2 tablets" -> ("2", "pill"); None when the amount cannot be templated."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?|\d+\s*/\s*\d+|½)\s+(.+)$", amount or "")
    if not m:
        return None
    form = next((f for f, pattern in _FORM_WORDS if re.search(pattern, m.group(2).lower())), None)
    return (m.group(1).replace(" ", ""), form) if form else None


def _forms(lang: str, form: str) -> tuple:
    f = T[lang]["forms"][form]
    return f if len(f) == 3 else (f[0], f[1], f[1])


def _amount_text(amount: str | None, lang: str) -> str | None:
    """"3 tablets" -> "3 comprimés" (limits and the daily schedule)."""
    if lang == "en":
        return pipeline._pills(amount) if amount else None
    q = _quantity(amount)
    if not q:
        return None
    n, form = q
    singular, plural, dual = _forms(lang, form)
    if lang in RTL and n == "2":
        return dual
    return f"{n} {singular if n in ('1', '1/2', '½') else plural}"


def _duration(duration: str, lang: str) -> str | None:
    m = re.match(r"^\s*(\d+)\s*(day|week|month)s?\s*$", duration or "", re.IGNORECASE)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    singular, plural = T[lang]["units"][unit]
    return f"{n} {singular if n == 1 else plural}"


# --------------------------------------------------------------------------- #
# Free-text translation (AI), with the number check
# --------------------------------------------------------------------------- #

_PROMPT = """You translate short patient-facing medical sentences from English to {language}.
Rules:
- Simple, everyday words for a patient who may not read well.
- Keep every number exactly as written. Keep medicine names exactly as written (do not translate them).
- Do not add or remove information. Do not add advice.
Return compact JSON only: {{"translations": [one translation per input sentence, same order]}}"""

_LANGUAGE_NAMES = {"fr": "French", "ar": "Modern Standard Arabic"}


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def translate_phrases(phrases: list[str], lang: str) -> tuple[dict[str, str], list[str]]:
    """Translate free-text phrases. Returns ({english: translation}, [phrases kept in English])."""
    phrases = list(dict.fromkeys(p for p in phrases if p and p.strip()))
    if lang == "en" or not phrases:
        return {p: p for p in phrases}, []
    with _cache_lock:
        cache = _load_cache()
    key = lambda p: f"{lang}::{pipeline.LLM_MODEL}::{p}"
    todo = [p for p in phrases if key(p) not in cache]
    if todo:
        try:
            raw = pipeline._chat(_PROMPT.format(language=_LANGUAGE_NAMES[lang]),
                                 json.dumps({"sentences": todo}, ensure_ascii=False), max_tokens=1500, json_mode=True)
            got = pipeline._as_list(pipeline._parse_json_object(raw).get("translations"))
        except Exception:  # model unreachable or unreadable answer: keep English
            got = []
        new = {}
        for english, translated in zip(todo, got):
            # Same numbers or it is not used: a translation must never change a dose.
            if pipeline._numbers(english) == pipeline._numbers(translated) and translated.strip():
                new[key(english)] = translated.strip()
        if new:
            with _cache_lock:
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_text(json.dumps({**_load_cache(), **new}, ensure_ascii=False, indent=1), encoding="utf-8")
            cache.update(new)
    result, kept = {}, []
    for p in phrases:
        if key(p) in cache:
            result[p] = cache[key(p)]
        else:
            result[p], kept = p, kept + [p]
    return result, kept


# --------------------------------------------------------------------------- #
# Building the patient sheet
# --------------------------------------------------------------------------- #

def _issue_line(issue: dict) -> str:
    what = issue["what"][:1].upper() + issue["what"][1:]
    return f"{what}: {pipeline._lower_first(issue['why'])}."


def _how_sentences(med: dict, lang: str, tr: dict) -> list[str]:
    if lang == "en":
        return pipeline._how_sentences(med)
    t = T[lang]
    name = pipeline.short_name(med)
    q = _quantity(med.get("amount_per_dose"))
    if q:
        first = f"{t['take']} {t['dose'](q[0], _forms(lang, q[1]), name)}"
    elif med.get("amount_per_dose"):
        first = f"{t['take']} {tr.get(med['amount_per_dose'], med['amount_per_dose'])}"
    else:
        first = t["take_it"]

    if med["only_when_needed"]:
        need = med.get("needed_for")
        first += " " + (t["only_when"](tr.get(need, need)) if need else t["only_when_needed"])
        if med.get("times_per_day"):
            first += _sep(lang) + t["up_to"](med["times_per_day"])
    elif med.get("times_of_day"):
        first += " " + _join([t["time"][m] for m in med["times_of_day"]], t["and"], lang)
    elif med.get("times_per_week"):
        w = med["times_per_week"]
        first += " " + (t["once_a_week"] if w == 1 else t["n_a_week"](w))
        if med.get("weekday"):
            first += _sep(lang) + t["on_day"](t["days"].get(med["weekday"], med["weekday"]))
    elif med.get("times_per_day"):
        n = med["times_per_day"]
        first += " " + (t["once_a_day"] if n == 1 else t["n_a_day"](n))

    extras = []
    if med.get("food"):
        extras.append(t["food"][med["food"]])
    long_term = bool(med.get("duration")) and "long" in med["duration"].lower()
    if med.get("duration") and not long_term:
        extras.append(t["for_duration"](_duration(med["duration"], lang) or tr.get(med["duration"], med["duration"])))
    sentences = [] if first == t["take_it"] and not extras else [_sep(lang).join([first, *extras]) + "."]

    if med.get("route") and med["route"].lower() not in ("by mouth", "oral", "orally", "po"):
        sentences.append(tr.get(med["route"], med["route"]).rstrip(".") + ".")
    if med.get("every_hours"):
        sentences.append(t["wait_hours"](med["every_hours"]))
    if med.get("max_per_day"):
        sentences.append(t["max_day"](_amount_text(med["max_per_day"], lang) or tr.get(med["max_per_day"], med["max_per_day"])))
    if long_term:
        sentences.append(t["long_term"])
    return sentences


def _free_text(extraction: dict) -> list[str]:
    """Every English phrase the AI must translate."""
    phrases = [pipeline._sentence(c) for c in extraction.get("conditions", [])]
    for med in extraction["medicines"]:
        if med.get("purpose"):
            phrases.append(med["purpose"])
        phrases += pipeline._sorry_sentences(med)
        for field in ("needed_for", "route", "duration"):
            if med.get(field):
                phrases.append(med[field])
        for field in ("amount_per_dose", "max_per_day"):
            if med.get(field) and not _quantity(med[field]):
                phrases.append(med[field])
    for line in extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else []):
        phrases.append(pipeline._sentence(line))
    phrases += [_issue_line(i) for i in extraction.get("issues", []) if i["level"] == "unsure"]
    return phrases


def render(extraction: dict, lang: str) -> dict:
    """The patient sheet in `lang`, as cards (for the screen) and as plain text (to share, print, speak).

    {"lang", "rtl", "illness": [..], "intro", "cards": [{"n", "name", "tag", "lines", "sorry", "doubt", "risk"}],
     "schedule": [..], "also": [..], "unclear": [..], "labels": {..}, "disclaimer", "text", "kept_in_english": [..]}
    """
    if lang not in LANGUAGES:
        lang = "en"
    t = T[lang]
    tr, kept = translate_phrases(_free_text(extraction), lang)

    illness = [tr[pipeline._sentence(c)] for c in extraction.get("conditions", [])]
    cards = []
    for n, med in enumerate(extraction["medicines"], 1):
        sorry = pipeline._sorry_sentences(med)
        cards.append({
            "n": n, "name": med["name_as_written"],
            "tag": tr.get(med["purpose"], med["purpose"]).rstrip(".") if med.get("purpose") else "",
            "lines": _how_sentences(med, lang, tr),
            "sorry": tr[sorry[0]] if sorry else "",
            "doubt": bool(sorry), "risk": any(i["level"] == "risk" for i in med.get("issues", [])),
        })

    schedule = []
    meds = extraction["medicines"]
    regular = [m for m in meds if not m["only_when_needed"]]
    if regular and all(m.get("times_of_day") for m in regular):
        for slot in pipeline._TIME_ORDER:
            items = []
            for m in regular:
                if slot in m["times_of_day"]:
                    amount = _amount_text(m.get("amount_per_dose"), lang)
                    items.append(m["name_as_written"] + (f" ({amount})" if amount else ""))
            if items:
                schedule.append(f"{t['slot'][slot]}: " + _sep(lang).join(items))
        if needed := [m["name_as_written"] for m in meds if m["only_when_needed"]]:
            schedule.append(f"{t['if_needed_slot']}: " + _sep(lang).join(needed))

    doctor = extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else [])
    also = [tr[pipeline._sentence(line)] for line in doctor]
    unclear = [tr[_issue_line(i)] for i in extraction.get("issues", []) if i["level"] == "unsure"]

    # Plain text, for sharing, printing and the spoken version.
    out: list[str] = []
    if illness:
        out += [t["Your illness"], *illness, ""]
    if cards:
        out += [t["Your medicines"], t["intro"], ""]
    for c in cards:
        out += [f"{c['n']}. {c['name']}" + (f" — {c['tag']}" if c["tag"] else ""), *c["lines"],
                *([c["sorry"]] if c["sorry"] else []), ""]
    if schedule:
        out += [t["When to take them each day"], *schedule, ""]
    if also:
        out += [t["Also"], *also, ""]
    if unclear:
        out += [t["Sorry, I could not understand everything"], *unclear, t["ask_about_this"], ""]
    out.append(t["disclaimer"])

    return {"lang": lang, "rtl": lang in RTL, "illness": illness, "intro": t["intro"], "cards": cards,
            "schedule": schedule, "also": also, "unclear": unclear, "disclaimer": t["disclaimer"],
            "labels": {k: t[k] for k in ("Your illness", "Your medicines", "Also", "When to take them each day",
                                          "Sorry, I could not understand everything")},
            "ask_about_this": t["ask_about_this"], "text": "\n".join(out).strip(), "kept_in_english": kept}


def stamp(lang: str, date: str) -> str:
    return T.get(lang, T["en"])["checked"](date)
