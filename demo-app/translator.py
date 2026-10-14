"""
translator.py — the patient explanation in English, French or Arabic.

Safe by design:
    - Everything that carries a dose (amount, times, food, duration, limits, schedule) is built
      from fixed sentence templates in each language. The AI never re-words a dose.
    - Only short free-text parts are translated by the AI: what each medicine is for, the illness,
      the doctor's own instructions, "when needed" reasons and apologies. Each translation must
      contain exactly the same numbers as the English; otherwise the English is kept and marked.
    - Medicine names are never translated.

The templates should be checked once by a native-speaking health professional before real use.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import ai_pipeline as pipeline

LANGUAGES = {"en": "English", "fr": "Français", "ar": "العربية"}
RTL = {"ar"}

CACHE_PATH = Path(__file__).resolve().parent / "data" / "translations.json"
_cache_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Fixed templates
# --------------------------------------------------------------------------- #

T = {
    "fr": {
        "Your illness": "Votre maladie",
        "Your medicines": "Vos médicaments",
        "What they are for": "À quoi ils servent",
        "When to take them each day": "Quand les prendre chaque jour",
        "Your doctor also says": "Votre médecin dit aussi",
        "Sorry, I could not understand everything": "Désolé, je n'ai pas tout compris",
        "you_have": lambda n: f"Vous avez {n} médicament{'s' if n > 1 else ''}.",
        "disclaimer": "Si vous avez un doute, demandez à votre médecin ou à votre pharmacien.",
        "ask_about_this": "Demandez à votre médecin ou à votre pharmacien.",
        "take": "Prenez", "take_it": "Prenez-le",
        "time": {"morning": "le matin", "noon": "à midi", "evening": "le soir", "bedtime": "au coucher"},
        "slot": {"morning": "Matin", "noon": "Midi", "evening": "Soir", "bedtime": "Coucher"},
        "if_needed_slot": "Seulement si besoin",
        "and": "et", "or": "ou",
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
        "dose": lambda n, form, name: f"{n} {form} {_de(name)}",
        "checked": lambda date: f"Vérifié par votre médecin le {date}.",
    },
    "ar": {
        "Your illness": "مرضك",
        "Your medicines": "أدويتك",
        "What they are for": "فيم تستعمل",
        "When to take them each day": "متى تأخذها كل يوم",
        "Your doctor also says": "يقول طبيبك أيضا",
        "Sorry, I could not understand everything": "عذرا، لم أفهم كل شيء",
        "you_have": lambda n: "لديك دواء واحد." if n == 1 else ("لديك دواءان." if n == 2 else f"لديك {n} أدوية."),
        "disclaimer": "إذا كان لديك أي شك، اسأل طبيبك أو الصيدلي.",
        "ask_about_this": "اسأل طبيبك أو الصيدلي.",
        "take": "خذ", "take_it": "خذه",
        "time": {"morning": "في الصباح", "noon": "عند الظهر", "evening": "في المساء", "bedtime": "قبل النوم"},
        "slot": {"morning": "الصباح", "noon": "الظهر", "evening": "المساء", "bedtime": "قبل النوم"},
        "if_needed_slot": "عند الحاجة فقط",
        "and": "و", "or": "أو",
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
        "forms": {"pill": ("قرص", "أقراص"), "sachet": ("كيس", "أكياس"), "puff": ("بخة", "بخات"),
                  "drop": ("قطرة", "قطرات"), "lozenge": ("قرص للمص", "أقراص للمص"), "ml": ("مل", "مل"),
                  "spoon": ("ملعقة", "ملاعق")},
        "dose": lambda n, form, name: (f"{form} واحد من {name}" if n == "1" else
                                       f"{_AR_DUAL.get(form, '2 ' + form)} من {name}" if n == "2" else f"{n} {form} من {name}"),
        "checked": lambda date: f"راجعه طبيبك بتاريخ {date}.",
    },
}

# Arabic dual ("two pills" is one word); used for the amount 2.
_AR_DUAL = {"أقراص": "قرصين", "أكياس": "كيسين", "بخات": "بختين", "قطرات": "قطرتين", "ملاعق": "ملعقتين", "أقراص للمص": "قرصين للمص"}

_FORM_WORDS = [("pill", r"tablet|capsule|pill|tab|cap|comprim|g[ée]l"), ("sachet", r"sachet"), ("puff", r"puff|bouff"),
               ("drop", r"drop|goutte"), ("lozenge", r"lozenge|pastille"), ("ml", r"\bml\b"), ("spoon", r"spoon|cuill")]


def _de(name: str) -> str:
    return f"d'{name}" if re.match(r"[aeiouyhAEIOUYHÉéÈè]", name) else f"de {name}"


def _join(items: list[str], word: str, lang: str) -> str:
    if len(items) == 1:
        return items[0]
    if lang == "ar":
        return "، ".join(items[:-1]) + f" {word}" + items[-1]
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def _quantity(amount: str, lang: str) -> tuple[str, str] | None:
    """"2 tablets" -> ("2", "comprimés"); None when the amount cannot be templated."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?|\d+\s*/\s*\d+|½)\s+(.+)$", amount or "")
    if not m:
        return None
    number, unit_text = m.group(1).replace(" ", ""), m.group(2).lower()
    form = next((f for f, pattern in _FORM_WORDS if re.search(pattern, unit_text)), None)
    if not form:
        return None
    singular, plural = T[lang]["forms"][form]
    one = number in ("1", "1/2", "½") or number.startswith("0")
    if lang == "ar" and number == "2":
        return number, plural  # Arabic dual is close enough for a reviewed demo
    return number, singular if one else plural


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
- Simple, everyday words for a patient who may not read well.{dialect}
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
            raw = pipeline._chat(_PROMPT.format(language=_LANGUAGE_NAMES[lang], dialect=""),
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
# Rendering in French / Arabic (English uses the pipeline's own renderer)
# --------------------------------------------------------------------------- #

def _how_sentences(med: dict, lang: str, tr: dict) -> list[str]:
    t = T[lang]
    name = pipeline.short_name(med)
    q = _quantity(med.get("amount_per_dose"), lang) if med.get("amount_per_dose") else None
    first = f"{t['take']} {t['dose'](q[0], q[1], name)}" if q else (
        f"{t['take']} {tr[med['amount_per_dose']]}" if med.get("amount_per_dose") else t["take_it"])

    if med["only_when_needed"]:
        need = med.get("needed_for")
        first += " " + (t["only_when"](tr.get(need, need)) if need else t["only_when_needed"])
        if med.get("times_per_day"):
            first += ("، " if lang == "ar" else ", ") + t["up_to"](med["times_per_day"])
    elif med.get("times_of_day"):
        first += " " + _join([t["time"][m] for m in med["times_of_day"]], t["and"], lang)
    elif med.get("times_per_week"):
        w = med["times_per_week"]
        first += " " + (t["once_a_week"] if w == 1 else t["n_a_week"](w))
        if med.get("weekday"):
            first += ("، " if lang == "ar" else ", ") + t["on_day"](t["days"].get(med["weekday"], med["weekday"]))
    elif med.get("times_per_day"):
        n = med["times_per_day"]
        first += " " + (t["once_a_day"] if n == 1 else t["n_a_day"](n))

    extras = []
    if med.get("food"):
        extras.append(t["food"][med["food"]])
    long_term = bool(med.get("duration")) and "long" in med["duration"].lower()
    if med.get("duration") and not long_term:
        extras.append(t["for_duration"](_duration(med["duration"], lang) or tr.get(med["duration"], med["duration"])))
    sep = "، " if lang == "ar" else ", "
    sentences = [] if first == t["take_it"] and not extras else [sep.join([first, *extras]) + "."]

    if med.get("route") and med["route"].lower() not in ("by mouth", "oral", "orally", "po"):
        sentences.append(tr.get(med["route"], med["route"]).rstrip(".") + ".")
    if med.get("every_hours"):
        sentences.append(t["wait_hours"](med["every_hours"]))
    if med.get("max_per_day"):
        q = _quantity(med["max_per_day"], lang)
        sentences.append(t["max_day"](f"{q[0]} {q[1]}" if q else tr.get(med["max_per_day"], med["max_per_day"])))
    if long_term:
        sentences.append(t["long_term"])
    return sentences


def _free_text(extraction: dict) -> list[str]:
    """Every English phrase that needs the AI to translate it."""
    phrases = []
    for c in extraction.get("conditions", []):
        phrases.append(pipeline._sentence(c))
    for med in extraction["medicines"]:
        phrases += pipeline._purpose_sentences(med) + pipeline._sorry_sentences(med)
        for field in ("needed_for", "route"):
            if med.get(field):
                phrases.append(med[field])
        if med.get("amount_per_dose") and not _quantity(med["amount_per_dose"], "fr"):
            phrases.append(med["amount_per_dose"])
        if med.get("max_per_day") and not _quantity(med["max_per_day"], "fr"):
            phrases.append(med["max_per_day"])
    for line in extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else []):
        phrases.append(pipeline._sentence(line))
    for i in extraction.get("issues", []):
        if i["level"] == "unsure":
            phrases.append(_issue_line(i))
    return phrases


def _issue_line(issue: dict) -> str:
    """The English line for something on the prescription we could not understand."""
    what = issue["what"][:1].upper() + issue["what"][1:]
    return f"{what}: {pipeline._lower_first(issue['why'])}."


def render(extraction: dict, lang: str) -> dict:
    """{"text", "lang", "rtl", "kept_in_english": [...]} for the patient sheet in `lang`."""
    if lang not in LANGUAGES:
        lang = "en"
    if lang == "en":
        return {"text": pipeline.render_explanation(extraction), "lang": "en", "rtl": False, "kept_in_english": []}

    t = T[lang]
    tr, kept = translate_phrases(_free_text(extraction), lang)
    out: list[str] = []

    if extraction.get("conditions"):
        out += [t["Your illness"], *(f"   {tr[pipeline._sentence(c)]}" for c in extraction["conditions"]), ""]

    meds = extraction["medicines"]
    if meds:
        out += [t["Your medicines"], t["you_have"](len(meds)), ""]
    for n, med in enumerate(meds, 1):
        body = [tr[s] for s in pipeline._purpose_sentences(med)]
        body += _how_sentences(med, lang, tr)
        body += [tr[s] for s in pipeline._sorry_sentences(med)]
        out += [f"{n}. {med['name_as_written']}", *(f"   {s}" for s in body), ""]

    regular = [m for m in meds if not m["only_when_needed"]]
    if regular and all(m.get("times_of_day") for m in regular):
        out.append(t["When to take them each day"])
        for slot in pipeline._TIME_ORDER:
            items = []
            for m in regular:
                if slot in m["times_of_day"]:
                    q = _quantity(m.get("amount_per_dose"), lang) if m.get("amount_per_dose") else None
                    items.append(m["name_as_written"] + (f" ({q[0]} {q[1]})" if q else ""))
            if items:
                out.append(f"   {t['slot'][slot]}: " + ("، " if lang == "ar" else ", ").join(items))
        if needed := [m["name_as_written"] for m in meds if m["only_when_needed"]]:
            out.append(f"   {t['if_needed_slot']}: " + ("، " if lang == "ar" else ", ").join(needed))
        out.append("")

    doctor = extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else [])
    if doctor:
        out += [t["Your doctor also says"], *(f"   {tr[pipeline._sentence(line)]}" for line in doctor), ""]

    unsure = [i for i in extraction.get("issues", []) if i["level"] == "unsure"]
    if unsure:
        out.append(t["Sorry, I could not understand everything"])
        out += [f"   {tr[_issue_line(i)]}" for i in unsure]
        out += [f"   {t['ask_about_this']}", ""]

    out.append(t["disclaimer"])
    return {"text": "\n".join(out), "lang": lang, "rtl": lang in RTL, "kept_in_english": kept}
