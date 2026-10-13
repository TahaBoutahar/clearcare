"""
ai_pipeline.py — Medical prescription explainer pipeline (AMD Developer Hackathon ACT III).

Flow:
    voice -> transcribe_voice (faster-whisper) -> text
    text  -> extract_prescription:
               1. dosing call (LLM, short compact JSON)
               2. safety check (every number must appear in the original prescription)
               3. what each medicine is for, one line per drug (LLM, parallel, cached in .cache/)
          -> plain-English restatement rendered in Python (doses are never re-worded by the LLM;
             no advice is added — only what the prescription says)
    (optional) -> text_to_audio (gTTS) -> .mp3 file

Why structured extraction instead of "please rewrite this simply":
    The LLM only reads the prescription and fills fields. The final text is built in code from
    those fields, so a dose cannot drift while the model "rephrases", and anything the model
    could not read or that does not match the original is shown to the patient as
    "check with your pharmacist" instead of being guessed.

Environment variables:
    LLM_PROVIDER       (optional)  "amd" (default), "ollama" (free, local) or "openrouter" (free tier)
    LLM_MODEL          (optional)  override the model name for the chosen provider

    AMD_API_KEY        (required for amd)  API key for the AMD OpenAI-compatible endpoint
    AMD_BASE_URL       (optional)  defaults to https://developer.amd.com.cn/radeon/api/v1
    AMD_MODEL          (optional)  defaults to amd/DeepSeek-V4-Flash

    OLLAMA_BASE_URL    (optional)  defaults to http://localhost:11434/v1
    OLLAMA_MODEL       (optional)  defaults to the Hermes-3 8B model already pulled locally

    OPENROUTER_API_KEY (required for openrouter)  free key from https://openrouter.ai/keys
    OPENROUTER_MODEL   (optional)  defaults to a ":free" model

    WHISPER_MODEL_SIZE (optional)  faster-whisper model size, defaults to "small"
    WHISPER_DEVICE     (optional)  "cpu" / "cuda" / "auto", defaults to "auto"
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from openai import BadRequestError, OpenAI

from privacy import keep_medical_only

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

AMD_BASE_URL = os.getenv("AMD_BASE_URL", "https://developer.amd.com.cn/radeon/api/v1")
AMD_MODEL = os.getenv("AMD_MODEL", "amd/DeepSeek-V4-Flash")

# Provider -> (base URL, API key env var or None, default model).
# All are OpenAI-compatible, so the same SDK client works for each.
_PROVIDERS: dict[str, tuple[str, str | None, str]] = {
    "amd": (AMD_BASE_URL, "AMD_API_KEY", AMD_MODEL),
    "ollama": (
        os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        None,  # local, no key needed
        os.getenv("OLLAMA_MODEL", "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q4_K_M"),
    ),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    ),
}

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "amd").strip().lower()
if LLM_PROVIDER not in _PROVIDERS:
    raise ValueError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Use one of: {', '.join(_PROVIDERS)}")
LLM_MODEL = os.getenv("LLM_MODEL") or _PROVIDERS[LLM_PROVIDER][2]



def configure(provider: str | None = None, model: str | None = None) -> None:
    """Switch provider and/or model at runtime (e.g. from command-line flags)."""
    global LLM_PROVIDER, LLM_MODEL
    if provider:
        provider = provider.strip().lower()
        if provider not in _PROVIDERS:
            raise ValueError(f"Unknown provider '{provider}'. Use one of: {', '.join(_PROVIDERS)}")
        LLM_PROVIDER = provider
    LLM_MODEL = model or (os.getenv("LLM_MODEL") if not provider else None) or _PROVIDERS[LLM_PROVIDER][2]
    _get_llm_client.cache_clear()


WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")

# gTTS language codes for the (currently optional) audio output.
_TTS_LANGUAGE_CODES = {"english": "en", "french": "fr", "arabic": "ar", "spanish": "es", "german": "de"}

DISCLAIMER = "If you are not sure about anything, ask your doctor or pharmacist."


class PipelineError(RuntimeError):
    """Raised when a pipeline step fails in a way the caller should handle."""


# --------------------------------------------------------------------------- #
# Clients (lazy, cached)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _get_llm_client() -> OpenAI:
    base_url, key_env, _ = _PROVIDERS[LLM_PROVIDER]
    if key_env is None:
        return OpenAI(api_key="not-needed", base_url=base_url)
    api_key = os.getenv(key_env)
    if not api_key:
        raise PipelineError(f"{key_env} environment variable is not set (LLM_PROVIDER={LLM_PROVIDER}).")
    return OpenAI(api_key=api_key, base_url=base_url)


@lru_cache(maxsize=1)
def _get_whisper_model():
    # Imported lazily so text-only usage doesn't pay the faster-whisper import cost.
    from faster_whisper import WhisperModel

    compute_type = "int8" if WHISPER_DEVICE in ("cpu", "auto") else "float16"
    return WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=compute_type)


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _chat(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """Single chat-completion call to the configured model, returning cleaned text."""
    kwargs = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens  # hard cap so a rambling model can't stall the request
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        try:
            response = _get_llm_client().chat.completions.create(**kwargs)
        except BadRequestError:
            if not json_mode:
                raise
            # Some endpoints don't support response_format; the prompt still asks for JSON.
            kwargs.pop("response_format")
            response = _get_llm_client().chat.completions.create(**kwargs)
    except PipelineError:
        raise
    except Exception as exc:  # network / auth / rate-limit errors from the SDK
        raise PipelineError(f"Sorry, I could not reach the AI service ({LLM_PROVIDER}). Why: {exc}") from exc

    content = (response.choices[0].message.content or "").strip()
    # Reasoning models sometimes inline their chain of thought; strip it.
    content = _THINK_RE.sub("", content).strip()
    if not content:
        raise PipelineError(f"Sorry, the AI service ({LLM_PROVIDER}) sent back an empty answer. Please try again.")
    return content


# --------------------------------------------------------------------------- #
# Step 1 — Speech to text
# --------------------------------------------------------------------------- #

def transcribe_voice(audio_file_path: str) -> str:
    """Transcribe an audio file (wav/mp3/m4a/ogg/...) to text with faster-whisper.

    Language is auto-detected, so doctors can dictate in French, Arabic, English, etc.
    """
    path = Path(audio_file_path)
    if not path.is_file():
        raise PipelineError(f"Audio file not found: {audio_file_path}")

    segments, _info = _get_whisper_model().transcribe(
        str(path),
        beam_size=5,
        vad_filter=True,  # drop silences / background noise
    )
    text = " ".join(segment.text.strip() for segment in segments)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise PipelineError("No speech detected in the audio file.")
    return text


# --------------------------------------------------------------------------- #
# Step 2 — Structured extraction (LLM)
#
# The output only restates the prescription in simple words: no advice, tips or warnings.
# Speed: generation time is ~all of the latency (~15 tok/s on a local 8B model), so the
# model is asked to write as little as possible:
#   a) one short call that reads the prescription (compact keys, unknown keys omitted);
#   b) a one-line "what this medicine is for" per drug, fetched in parallel and cached on
#      disk — repeat drugs cost nothing.
# --------------------------------------------------------------------------- #

_DOSING_SYSTEM_PROMPT = """You are an experienced clinical pharmacist. Read a doctor's prescription and extract it into compact JSON.

The prescription can be in any language (often French) and may be a voice transcript with no punctuation.

Abbreviations:
- French: cp = tablet, gél = capsule, sach = sachet, gtt = drops, bouffée = puff, pastille = lozenge, c. à c. = teaspoon, c. à s. = tablespoon, /j = per day, x2/j = 2 times a day, matin = morning, midi = noon, soir = evening, au coucher = bedtime, à jeun = empty stomach, au milieu / au cours des repas = with food, avant les repas = before food, après les repas = after food, si besoin / si douleur = only when needed, QSP = treatment length, au long cours = long term, à renouveler = refill.
- Latin: qd/od = once a day, bid = 2 times a day, tid = 3, qid = 4, q6h = every 6 hours, hs = bedtime, prn = only when needed, ac = before food, pc = after food, po = by mouth, sl = under the tongue, sc = under the skin, im = into a muscle.

JSON keys (OMIT any key that is not stated in the prescription):
{"meds": [{
  "name": medicine name and strength exactly as written,
  "generic": generic name in English, lowercase,
  "form": tablet | capsule | sachet | lozenge | syrup | drops | inhaler | cream | injection | suppository | other,
  "strength": e.g. "1 g",
  "amount": amount per dose in English, e.g. "1 tablet",
  "per_day": number of times per day,
  "times": subset of ["morning", "noon", "evening", "bedtime"],
  "every_h": hours between doses,
  "food": with food | before food | after food | empty stomach,
  "duration": e.g. "7 days", "3 months", "long term",
  "prn": true if only when needed,
  "prn_for": in simple words, e.g. "pain or fever",
  "max_day": e.g. "4 tablets",
  "route": only if not swallowed, as a short instruction, e.g. "suck it slowly", "breathe it in", "put it on the skin",
  "unclear": [why something in this line is unclear, e.g. "the dose is not written", "the word after 1 cp cannot be read"]
}],
"diagnosis": [illnesses written by the doctor, in simple everyday English, e.g. "throat infection", "high blood pressure", "type 2 diabetes"],
"instructions": [other doctor instructions, as very short, simple ENGLISH sentences (max 8 words)],
"follow_up": when to see the doctor again, plain English sentence,
"unreadable": [{"text": a part you could not understand, "why": short reason}]}

Rules:
- Copy every number EXACTLY. Never calculate, convert or guess a dose, frequency or duration.
- Only write what the prescription says. Do not add any advice of your own.
- Never guess. If you are not sure about something, leave it out and explain why in "unclear" or "unreadable".
- Only list medicines that are in the prescription. Ignore names of doctor/patient, addresses, dates.
- EVERY medicine goes in "meds", even when several are written on one line. Never put a medicine or a dose in "instructions".
- A medicine is something the patient takes, uses or applies. An illness, disease or symptom (covid, grippe, angine, diabète, HTA, asthme, fièvre, infection...) is NEVER a medicine: put it in "diagnosis". If the text only names an illness, "meds" is [].
- Translate instructions to English ("Éviter l'alcool" -> "Do not drink alcohol.", "Revoir dans 15 jours" -> follow_up "See your doctor again in 15 days.").
- Output compact JSON only.

Example input:
Angine. Ibuprofène 400 mg : 1 cp x 3/j après les repas pendant 5 jours. Repos. Contrôle dans 1 semaine.
Example output:
{"meds":[{"name":"Ibuprofène 400 mg","generic":"ibuprofen","form":"tablet","strength":"400 mg","amount":"1 tablet","per_day":3,"food":"after food","duration":"5 days"}],"diagnosis":["throat infection"],"instructions":["Rest."],"follow_up":"See your doctor again in 1 week."}"""


_DRUG_PURPOSE_SYSTEM_PROMPT = """Say what a medicine is used for, to a patient who may not read well.
Use simple everyday words a child understands. No medical words (say "infection", "pain", "fever",
"sugar in your blood", "blood pressure", "allergies", "stomach acid").

Your text is inserted into the sentence "This medicine is ___." so it must fit it, max 8 words.
Return compact JSON only: {"purpose": "..."}
Do not give any advice, warnings or instructions. If you are not sure, return {"purpose": null}.

Example for ibuprofen: {"purpose":"for pain, swelling and fever"}
Example for amlodipine: {"purpose":"to lower your blood pressure"}"""


_TIME_ORDER = ["morning", "noon", "evening", "bedtime"]

DRUG_INFO_CACHE_PATH = Path(os.getenv("DRUG_INFO_CACHE", Path(__file__).parent / ".cache" / "drug_info.json"))
_cache_lock = threading.Lock()


def _parse_json_object(raw: str) -> dict:
    """Parse a JSON object out of a model reply, tolerating code fences or extra text."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(raw[start : end + 1])


_EMPTY_VALUES = ("", "null", "none", "n/a", "nothing", "-", "unknown", "not stated", "not specified", "not mentioned")


def _as_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower().rstrip(".") in _EMPTY_VALUES else text


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [s for s in (_as_str(v) for v in value) if s]


def _as_food(value) -> str | None:
    """Map free-text food timing ("during meals", "à jeun", ...) onto the 4 known values."""
    text = (_as_str(value) or "").lower()
    if not text:
        return None
    if "empty" in text or "jeun" in text or "fasting" in text:
        return "empty stomach"
    if "before" in text or "avant" in text:
        return "before food"
    if "after" in text or "après" in text or "apres" in text:
        return "after food"
    if any(w in text for w in ("with", "during", "meal", "food", "repas")):
        return "with food"
    return None


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _clean_reason(value) -> str | None:
    """"if fever" / "for pain" -> "fever" / "pain" (the renderer adds its own "for")."""
    text = _as_str(value)
    return re.sub(r"^(if|when|for|in case of|si|en cas de)\s+", "", text, flags=re.IGNORECASE) if text else None


def _with_unit(max_value: str | None, amount: str | None) -> str | None:
    """"3" + "1 tablet" -> "3 tablets", so the patient knows 3 of what."""
    if not max_value or not re.fullmatch(r"\d+", max_value.strip()) or not amount:
        return max_value
    unit = re.sub(r"^[\d.,/½\s]+", "", amount).strip()
    if not unit:
        return max_value
    if max_value.strip() != "1" and not unit.endswith("s"):
        unit += "s"
    return f"{max_value.strip()} {unit}"


def _flag(target: dict, level: str, what: str, why: str) -> None:
    """Record something the patient or doctor should know about.

    level "unsure": shown to the patient (with an apology) and to the doctor.
    level "note":   shown to the doctor only (an automatic correction).
    """
    target.setdefault("issues", []).append({"level": level, "what": what.strip(), "why": why.strip().rstrip(".")})


def _letters(text: str) -> str:
    """"co-vid" / "COVID-19" -> "covid" / "covid": for loose name matching."""
    return re.sub(r"[^a-z]", "", text.lower())


def _normalize_dosing(data: dict) -> dict:
    """Map the model's compact keys onto the public `structured` shape (models drift on types)."""
    medicines = []
    for med in data.get("meds") or data.get("medicines") or []:
        if not isinstance(med, dict):
            continue
        name = _as_str(med.get("name")) or _as_str(med.get("generic"))
        if not name:
            continue
        times = {t.lower() for t in _as_list(med.get("times"))} & set(_TIME_ORDER)
        medicines.append({
            "name_as_written": name,
            "common_name": (_as_str(med.get("generic")) or "").lower() or None,
            "form": _as_str(med.get("form")),
            "strength": _as_str(med.get("strength")),
            "amount_per_dose": _as_str(med.get("amount")),
            "times_per_day": _as_int(med.get("per_day")),
            "times_of_day": sorted(times, key=_TIME_ORDER.index),
            "every_hours": _as_int(med.get("every_h")),
            "food": _as_food(med.get("food")),
            "duration": _as_str(med.get("duration")),
            "only_when_needed": med.get("prn") in (True, "true", "True", "yes"),
            "needed_for": _plain(_clean_reason(med.get("prn_for"))),
            "max_per_day": _with_unit(_as_str(med.get("max_day")), _as_str(med.get("amount"))),
            "route": _as_str(med.get("route")),
            "purpose": None,  # filled in by _add_drug_info()
            "issues": [{"level": "unsure", "what": "part of this medicine's instructions", "why": why.rstrip(".")}
                       for why in _as_list(med.get("unclear"))],
        })
    conditions = list(dict.fromkeys(_as_list(data.get("diagnosis"))))
    # Small models sometimes list the illness as a medicine or as unreadable too; drop those.
    illness_keys = {_letters(c) for c in conditions}
    medicines = [m for m in medicines if _letters(m["name_as_written"]) not in illness_keys]
    extraction = {
        "conditions": conditions,
        "medicines": medicines,
        "doctor_instructions": _as_list(data.get("instructions")),
        "follow_up": _as_str(data.get("follow_up")),
        "issues": [],
    }
    for item in data.get("unreadable") or []:
        text, why = (item.get("text"), item.get("why")) if isinstance(item, dict) else (item, None)
        text = _as_str(text)
        if not text or any(_letters(text) and (_letters(text) in k or k in _letters(text)) for k in illness_keys):
            continue
        _flag(extraction, "unsure", f'"{text}"', _as_str(why) or "I could not read or understand this part of the prescription")
    return extraction


def _drug_key(med: dict) -> str:
    """Cache key: generic name if known, else the first word as written. Scoped per model."""
    name = med.get("common_name") or med["name_as_written"].split()[0]
    return f"purpose-v1::{LLM_MODEL}::{name.lower()}"


def _load_drug_cache() -> dict:
    try:
        return json.loads(DRUG_INFO_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_drug_cache(cache: dict) -> None:
    DRUG_INFO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRUG_INFO_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DRUG_INFO_CACHE_PATH)


# Medical words small models still use, and the everyday words patients understand.
_PLAIN_WORDS = [
    (r"\bbacterial infections?\b", "infections"),
    (r"\bviral infections?\b", "infections caused by a virus"),
    (r"\bhypertension\b", "high blood pressure"),
    (r"\bhyperglyc(a)?emia\b", "high sugar in the blood"),
    (r"\bacid reflux\b", "acid coming up from the stomach"),
    (r"\bsymptoms\b", "signs"),
    (r"\bshortness of breath\b", "trouble breathing"),
    (r"\b(respiratory distress|breathing difficult(y|ies)|difficulty breathing)\b", "trouble breathing"),
    (r"\bmedications?\b", "medicine"),
]


def _plain(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, word in _PLAIN_WORDS:
        text = re.sub(pattern, word, text, flags=re.IGNORECASE)
    return text


def _fetch_drug_purpose(med: dict) -> str | None:
    """One tiny LLM call: what this medicine is used for, in a few simple words."""
    label = med.get("common_name") or med["name_as_written"]
    try:
        data = _parse_json_object(_chat(_DRUG_PURPOSE_SYSTEM_PROMPT, f"Medicine: {label}", max_tokens=60, json_mode=True))
    except (PipelineError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Could not get the purpose of %s: %s", label, exc)
        return None
    purpose = _as_str(data.get("purpose"))
    if purpose:  # the model sometimes repeats the sentence it is slotted into
        purpose = re.sub(r"^\s*(this medicine is)\s*", "", purpose, flags=re.IGNORECASE).strip().rstrip(".")
    return purpose or None


def _iter_drug_info(extraction: dict):
    """Fill in what each medicine is for, yielding each medicine's index as it is done.

    Cached drugs are yielded immediately; the rest are fetched in parallel and yielded in the
    order they finish, so a caller can show each one as soon as it is ready.
    """
    meds = extraction["medicines"]
    with _cache_lock:
        cache = _load_drug_cache()

    missing: dict[str, list[int]] = {}
    for i, med in enumerate(meds):
        key = _drug_key(med)
        if key in cache:
            med["purpose"] = _plain(cache[key])
            yield i
        else:
            missing.setdefault(key, []).append(i)
    if not missing:
        return

    logger.info("Looking up %d drug(s): %s", len(missing), ", ".join(k.split("::")[-1] for k in missing))
    with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
        futures = {pool.submit(_fetch_drug_purpose, meds[idx[0]]): key for key, idx in missing.items()}
        for future in as_completed(futures):
            key = futures[future]
            purpose = future.result()
            if purpose:
                with _cache_lock:
                    _save_drug_cache({**_load_drug_cache(), key: purpose})
            for i in missing[key]:
                meds[i]["purpose"] = _plain(purpose)
                if not purpose:
                    _flag(meds[i], "note", "what it is for", "I could not look up what this medicine is used for")
                yield i


def _add_drug_info(extraction: dict) -> None:
    for _ in _iter_drug_info(extraction):
        pass


# --------------------------------------------------------------------------- #
# Step 3 — Safety check (deterministic)
# --------------------------------------------------------------------------- #

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    """All numbers in `text`, normalized so "1,0", "1.0" and "1" compare equal."""
    found = set()
    for n in _NUMBER_RE.findall(text or ""):
        n = n.replace(",", ".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        found.add(n)
    return found


# Dose-form abbreviations as written on prescriptions -> the plain-English word.
_FORM_PATTERNS = {
    "tablet": re.compile(r"\b(cp|cps|comprim[ée]s?|tab|tabs|tablets?)\b", re.IGNORECASE),
    "capsule": re.compile(r"\b(g[ée]l|g[ée]lules?|caps?|capsules?)\b", re.IGNORECASE),
    "sachet": re.compile(r"\b(sach|sachets?)\b", re.IGNORECASE),
}
_FORM_WORD_RE = re.compile(r"\b(tablet|capsule|sachet)(s?)\b", re.IGNORECASE)


def _source_line(med: dict, source_text: str) -> str | None:
    """The prescription line that mentions this medicine (matched on the first word of its name)."""
    first_word = med["name_as_written"].split()[0].lower()
    return next((line for line in source_text.splitlines() if first_word in line.lower()), None)


def _fix_form(med: dict, source_text: str) -> None:
    """Small models mix up "cp" (tablet) and "gél" (capsule); trust what is actually written."""
    line = _source_line(med, source_text)
    if not line:
        return
    written = [form for form, pattern in _FORM_PATTERNS.items() if pattern.search(line)]
    if len(written) != 1:
        return
    form = written[0]
    med["form"] = form
    for field in ("amount_per_dose", "max_per_day"):
        if med.get(field):
            fixed = _FORM_WORD_RE.sub(lambda m: form + m.group(2), med[field])
            if fixed != med[field]:
                _flag(med, "note", f"form ({field.replace('_', ' ')})", f'the AI wrote "{med[field]}", corrected to "{fixed}" as written on the prescription')
                med[field] = fixed


_UNITS_EN = {"jour": "day", "jours": "days", "semaine": "week", "semaines": "weeks", "mois": "months",
             "day": "day", "days": "days", "week": "week", "weeks": "weeks", "month": "month", "months": "months"}
_DURATION_RE = re.compile(r"\b(?:pendant|durant|qsp|for|x)\s*(\d+)\s*(jours?|semaines?|mois|days?|weeks?|months?)\b", re.IGNORECASE)
_LONG_TERM_RE = re.compile(r"\b(au long cours|à vie|a vie|long terme|traitement chronique|à poursuivre|ne pas arr[êe]ter|long[- ]term|for life|ongoing)\b", re.IGNORECASE)


# What the line must contain for each food instruction to be kept.
_FOOD_EVIDENCE = {
    "with food": re.compile(r"repas|manger|meal|food|eating", re.IGNORECASE),
    "before food": re.compile(r"avant|before|\bac\b", re.IGNORECASE),
    "after food": re.compile(r"apr[èe]s|after|\bpc\b", re.IGNORECASE),
    "empty stomach": re.compile(r"jeun|empty|fasting|avant", re.IGNORECASE),
}
_PRN_RE = re.compile(r"\b(si besoin|si n[ée]cessaire|en cas de|si|prn|as needed|if needed|when needed)\b\s*(.*)", re.IGNORECASE)
_PRN_REASONS = [("douleur", "pain"), ("fi[èe]vre", "fever"), ("mal de gorge", "a sore throat"), ("toux", "a cough"),
                ("naus[ée]e", "nausea"), ("allergie", "allergy"), ("insomnie", "trouble sleeping"),
                ("pain", "pain"), ("fever", "fever")]
_TIME_EVIDENCE = [("morning", r"\bmatin\b|\bmorning\b"), ("noon", r"\bmidi\b|\bnoon\b|\blunch\b"),
                  ("evening", r"\bsoir\b|\bevening\b"), ("bedtime", r"\bcoucher\b|\bbedtime\b|\bhs\b")]
_PER_DAY_RE = re.compile(r"(?:x\s*(\d+)\s*/\s*j|(\d+)\s*(?:fois|x)\s*(?:par|/)\s*j|(\d+)\s*times? (?:a|per) day)", re.IGNORECASE)


def _check_against_line(med: dict, source_text: str) -> None:
    """Small models drop or invent details. Re-read the medicine's own line:
    - remove a food instruction the line does not mention;
    - fill in "when needed", times of day, times per day, duration and "à sucer" if the model missed them.
    """
    line = _source_line(med, source_text)
    if not line:
        _flag(med, "unsure", "this medicine",
              "I could not find this name in the prescription text, so I may have misread it")
        return

    if med.get("food") and not _FOOD_EVIDENCE[med["food"]].search(line):
        _flag(med, "note", "food", f'removed "{med["food"]}": the prescription does not mention meals for this medicine')
        med["food"] = None

    if med["only_when_needed"] and not med.get("needed_for") and (m := _PRN_RE.search(line)):
        reasons = [word for pattern, word in _PRN_REASONS if re.search(pattern, m.group(2).lower())]
        if reasons:
            med["needed_for"] = _join_or(list(dict.fromkeys(reasons)))
            _flag(med, "note", "when needed", f'added "{med["needed_for"]}" from "{m.group(0).strip()}" (the AI had missed it)')
    elif not med["only_when_needed"] and (m := _PRN_RE.search(line)):
        rest = m.group(2).lower()
        reasons = [word for pattern, word in _PRN_REASONS if re.search(pattern, rest)]
        if reasons or m.group(1).lower() != "si":  # "si" alone is too vague ("si possible"...)
            med["only_when_needed"] = True
            med["needed_for"] = med.get("needed_for") or (_join_or(list(dict.fromkeys(reasons))) if reasons else None)
            _flag(med, "note", "when needed", f'added "only when needed" from "{m.group(0).strip()}" (the AI had missed it)')

    if not med.get("times_of_day"):
        med["times_of_day"] = [t for t, pattern in _TIME_EVIDENCE if re.search(pattern, line, re.IGNORECASE)]
        if med["times_of_day"]:
            _flag(med, "note", "time of day", f"added {', '.join(med['times_of_day'])} from the prescription (the AI had missed it)")
    if not med.get("times_per_day") and (m := _PER_DAY_RE.search(line)):
        med["times_per_day"] = int(next(g for g in m.groups() if g))
        _flag(med, "note", "times per day", f'added {med["times_per_day"]} times a day from "{m.group(0)}" (the AI had missed it)')

    if not med.get("route") and re.search(r"[àa] sucer|sucer", line, re.IGNORECASE):
        med["route"] = "Suck it slowly"
        _flag(med, "note", "how to use", 'added "suck it slowly" from "à sucer" (the AI had missed it)')

    before = med.get("duration")
    _fill_duration(med, line)
    if med.get("duration") != before:
        _flag(med, "note", "duration", f'set to "{med["duration"]}" from the prescription (the AI had {"missed it" if not before else repr(before)})')


def _fill_duration(med: dict, line: str) -> None:
    """Small models sometimes drop "QSP 3 mois" or "au long cours"; read it from the line itself."""
    if _LONG_TERM_RE.search(line):
        med["duration"] = "long term"  # "do not stop" matters more than a refill period
    elif not med.get("duration") and (m := _DURATION_RE.search(line)):
        number, unit = m.group(1), _UNITS_EN[m.group(2).lower()]
        if number == "1" and unit == "months":
            unit = "month"
        med["duration"] = f"{number} {unit}"


def _safety_check(extraction: dict, source_text: str) -> dict:
    """Check the model's reading against the original prescription and record every doubt.

    Numbers the prescription does not contain are removed; missing doses and frequencies,
    automatic corrections and dropped inventions are all recorded as issues with a reason,
    so nothing is fixed or removed silently.
    """
    source_numbers = _numbers(source_text)
    for med in extraction["medicines"]:
        _fix_form(med, source_text)
        _check_against_line(med, source_text)
        for field, label in (
            ("strength", "strength"),
            ("amount_per_dose", "amount to take"),
            ("every_hours", "time between doses"),
            ("duration", "length of treatment"),
            ("max_per_day", "maximum per day"),
        ):
            value = med.get(field)
            if value is None:
                continue
            unknown = _numbers(str(value)) - source_numbers
            if unknown:
                med[field] = None
                _flag(med, "unsure", f"the {label}",
                      f'I read "{value}", but the number {", ".join(sorted(unknown))} is not on the prescription, so I removed it')

        # Times per day must agree with the listed moments of the day, when both are given.
        n, moments = med.get("times_per_day"), med.get("times_of_day")
        if n and moments and n != len(moments):
            _flag(med, "unsure", "how many times a day to take it",
                  f"the prescription seems to say {n} times a day, but names {len(moments)} time(s) of the day")

        # Things the patient needs that the prescription does not give.
        if not med.get("amount_per_dose"):
            _flag(med, "unsure", "how much to take", "the amount to take each time is not written on the prescription")
        if not (med["only_when_needed"] or med.get("times_per_day") or med.get("times_of_day") or med.get("every_hours")):
            _flag(med, "unsure", "how often to take it", "how many times a day is not written on the prescription")

    # Small models copy the follow-up from the prompt example; keep it only if the prescription has one.
    if extraction["follow_up"] and not _FOLLOW_UP_RE.search(source_text):
        _flag(extraction, "note", "follow-up visit", f'removed "{extraction["follow_up"]}": it is not on the prescription')
        extraction["follow_up"] = None

    checked = []
    for line in extraction["doctor_instructions"]:
        line = _plain(line)
        if extraction["follow_up"] and _REVISIT_RE.search(line):
            continue  # already shown as the follow-up visit
        if _FOOD_INSTRUCTION_RE.fullmatch(line.strip().rstrip(".")) and any(m.get("food") for m in extraction["medicines"]):
            _flag(extraction, "note", "doctor's instruction", f'removed "{line}": it repeats a medicine\'s own food instruction')
            continue
        if unknown := _numbers(line) - source_numbers:
            _flag(extraction, "note", "doctor's instruction",
                  f'removed "{line}": the number {", ".join(sorted(unknown))} is not on the prescription')
        elif _DOSE_IN_TEXT_RE.search(line):
            _flag(extraction, "unsure", f'"{line}"',
                  "this looks like a medicine dose, but I could not tell which medicine it belongs to")
        else:
            checked.append(line)
    extraction["doctor_instructions"] = checked
    return extraction


_FOLLOW_UP_RE = re.compile(
    r"\b(revoir|revenir|contr[ôo]le|rendez[- ]vous|rdv|consultation|reconsulter|suivi|retour|"
    r"follow[- ]?up|see (me|the doctor|your doctor) again|come back|check[- ]?up|review|"
    r"dans \d+\s*(jours?|semaines?|mois)|in \d+\s*(days?|weeks?|months?))\b",
    re.IGNORECASE,
)
_FOOD_INSTRUCTION_RE = re.compile(r"(take (it |them )?)?(with|during|before|after) (food|meals?|eating)", re.IGNORECASE)
_REVISIT_RE = re.compile(r"\b(revisit|return|come back|see (your|the) doctor|follow[- ]?up|check[- ]?up)\b", re.IGNORECASE)
_DOSE_IN_TEXT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(mg|g|ml|µg|mcg|tablets?|capsules?|cp|gél|sachets?|drops?|puffs?)\b", re.IGNORECASE)


def _read_dosing(text: str, retries: int = 1) -> dict:
    """Dosing call + safety check. Drug facts are not filled in yet."""
    if not text or not text.strip():
        raise PipelineError("Sorry, there is nothing to explain: the prescription is empty.")
    # Personal details are removed here, on this computer, before anything is sent to the AI.
    source, removed = keep_medical_only(text)
    if not source:
        raise PipelineError(
            "Sorry, I could not find any medical information in this text. Why: it only seems to contain "
            "personal details (" + ", ".join(removed) + "), which this tool does not need."
        )

    reason = "unknown"
    for attempt in range(retries + 1):
        raw = _chat(_DOSING_SYSTEM_PROMPT, f"Prescription:\n{source}", max_tokens=1200, json_mode=True)
        try:
            extraction = _normalize_dosing(_parse_json_object(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            reason = "the AI's answer was not in a form I could read"
            logger.warning("Could not parse model JSON (attempt %d): %s", attempt + 1, exc)
            continue
        if extraction["medicines"] or extraction["conditions"]:
            extraction["privacy_removed"] = removed
            return _safety_check(extraction, source)
        reason = "I could not find any medicine or illness in this text"
        if extraction["issues"]:
            reason += " (" + "; ".join(f"{i['what']}: {i['why']}" for i in extraction["issues"]) + ")"
        logger.warning("Model found no medicines or illness (attempt %d)", attempt + 1)

    raise PipelineError(
        f"Sorry, I could not understand this prescription. Why: {reason}. "
        "Please check the text, or ask a doctor or pharmacist to explain it."
    )


def extract_prescription(text: str) -> dict:
    """Read a prescription and return a structured, safety-checked dict.

    Personal details are removed locally first (see privacy.py); "privacy_removed" lists what kind.
    Shape: {"conditions": [...], "medicines": [...], "doctor_instructions": [...], "follow_up": str|None,
            "issues": [...]} — see _normalize_dosing() for the medicine fields. Each medicine also has
            "issues". An issue is {"level": "unsure" | "note", "what": str, "why": str}.
    Useful for the backend/UI (e.g. one card per medicine, a daily schedule view).
    """
    extraction = _read_dosing(text)
    _add_drug_info(extraction)
    return extraction


def stream_prescription(text: str):
    """Same as extract_prescription(), but yields results as soon as each part is ready.

    Yields, in order:
        {"type": "dosing", "prescription": dict}               # illness + how to take each medicine
        {"type": "medicine", "index": int, "medicine": dict}   # once per medicine, when its purpose arrives
        {"type": "done", "prescription": dict, "text": str,    # complete explanation for the patient
         "review": [str], "needs_review": bool}                # for the doctor: doubts and corrections
    """
    extraction = _read_dosing(text)
    yield {"type": "dosing", "prescription": extraction}
    for i in _iter_drug_info(extraction):
        yield {"type": "medicine", "index": i, "medicine": extraction["medicines"][i]}
    yield {"type": "done", "prescription": extraction, "text": render_explanation(extraction),
           "review": render_review(extraction), "needs_review": needs_review(extraction)}


# --------------------------------------------------------------------------- #
# Step 4 — Render the patient-friendly explanation (deterministic)
#
# Only restates the prescription, for patients who may not read well: short sentences,
# everyday words, one idea per sentence, no advice. The medicine name is kept as written on
# the prescription so it matches the box.
# --------------------------------------------------------------------------- #

HEADINGS = {
    "Your illness",
    "Your medicines",
    "How to take them",
    "What they are for",
    "When to take them each day",
    "Your doctor also says",
    "Sorry, I could not understand everything",
    "For the doctor: please review",
}

_FOOD_WORDS = {
    "with food": "with food",
    "before food": "before eating",
    "after food": "after eating",
    "empty stomach": "before you eat anything",
}
_TIME_WORDS = {"morning": "in the morning", "noon": "at noon", "evening": "in the evening", "bedtime": "at bedtime"}


def _sentence(text: str) -> str:
    """Capitalize and end with a single period."""
    text = text.strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _lower_first(text: str) -> str:
    text = text.strip().rstrip(".")
    if re.match(r"I\b", text):
        return text
    return text[:1].lower() + text[1:]


def _join_or(items: list[str], word: str = "or") -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _how_sentences(med: dict) -> list[str]:
    """How to take it, as short sentences: how much, when, food, how long, limits."""
    amount = med.get("amount_per_dose")
    first = f"Take {amount}" if amount else "Take it"

    if med["only_when_needed"]:
        need = med.get("needed_for")
        first += f" only when you have {need}" if need else " only when you need it"
        if med.get("times_per_day"):
            first += f", up to {med['times_per_day']} times a day" if med["times_per_day"] > 1 else ", up to once a day"
    else:
        moments = med.get("times_of_day") or []
        n = med.get("times_per_day")
        if moments:
            first += " " + _join_or([_TIME_WORDS[m] for m in moments], "and")
        elif n:
            first += " once a day" if n == 1 else f" {n} times a day"

    extras = []
    if med.get("food"):
        extras.append(_FOOD_WORDS[med["food"]])
    long_term = bool(med.get("duration")) and "long" in med["duration"].lower()
    if med.get("duration") and not long_term:
        extras.append(f"for {med['duration']}")
    # "Take it." alone says nothing: the apology below explains what is missing.
    sentences = [] if first == "Take it" and not extras else [_sentence(", ".join([first, *extras]))]

    if med.get("route") and med["route"].lower() not in ("by mouth", "oral", "orally", "po"):
        sentences.append(_sentence(med["route"]))
    if med.get("every_hours"):
        sentences.append(f"Wait at least {med['every_hours']} hours before you take another one.")
    if med.get("max_per_day"):
        sentences.append(f"Do not take more than {med['max_per_day']} in one day.")
    if long_term:
        sentences.append("This is a long-term treatment.")
    return sentences


def _purpose_sentences(med: dict) -> list[str]:
    return [_sentence(f"This medicine is {_lower_first(med['purpose'])}")] if med.get("purpose") else []


def _sorry_sentences(med: dict) -> list[str]:
    """One apology per medicine for everything we are not sure about, with the reasons."""
    unsure = [i for i in med.get("issues", []) if i["level"] == "unsure"]
    if not unsure:
        return []
    generic = "part of this medicine's instructions"
    specific = [i for i in unsure if i["what"] != generic]
    whats = list(dict.fromkeys(i["what"] for i in specific))
    # Our own checks give clear reasons; the AI's free-text reasons are only used when there are none.
    whys = list(dict.fromkeys(_lower_first(i["why"]) for i in (specific or unsure)))
    about = _join_or(whats, "and") if whats else generic
    return [f"Sorry, I am not sure about {about}. Why: {'; '.join(whys)}. Please ask your doctor or pharmacist."]


def render_medicine(number: int, med: dict, part: str = "all") -> list[str]:
    """Lines for one medicine. part: "all", "dosing" (how to take it) or "details" (what it is for)."""
    if part == "dosing":
        body = _how_sentences(med) + _sorry_sentences(med)
    elif part == "details":
        body = _purpose_sentences(med)
    else:
        body = _purpose_sentences(med) + _how_sentences(med) + _sorry_sentences(med)
    return [f"{number}. {med['name_as_written']}", *(f"   {s}" for s in body)]


def _daily_schedule(medicines: list[dict]) -> list[str]:
    """One line per moment of the day. Empty if any regular medicine has no known time."""
    regular = [m for m in medicines if not m["only_when_needed"]]
    if not regular or any(not m.get("times_of_day") for m in regular):
        return []

    slots: dict[str, list[str]] = {t: [] for t in _TIME_ORDER}
    as_needed: list[str] = []
    for med in medicines:
        label = med["name_as_written"]
        if med.get("amount_per_dose"):
            label += f" ({med['amount_per_dose']})"
        if med["only_when_needed"]:
            as_needed.append(label)
        else:
            for moment in med["times_of_day"]:
                slots[moment].append(label)

    lines = [f"   {t.capitalize()}: " + ", ".join(items) for t, items in slots.items() if items]
    if as_needed:
        lines.append("   Only if needed: " + ", ".join(as_needed))
    return lines


def render_conditions(conditions: list[str]) -> list[str]:
    """The illness(es) the doctor wrote, in plain words."""
    if not conditions:
        return []
    return ["Your illness", f"   {_sentence(_join_or(conditions, 'and'))}"]


def render_footer(extraction: dict) -> list[str]:
    """Daily schedule, the doctor's other instructions, unreadable parts and the disclaimer."""
    out: list[str] = []
    if schedule := _daily_schedule(extraction["medicines"]):
        out += ["When to take them each day", *schedule, ""]

    doctor_lines = extraction["doctor_instructions"] + ([extraction["follow_up"]] if extraction["follow_up"] else [])
    if doctor_lines:
        out += ["Your doctor also says", *(f"   {_sentence(line)}" for line in doctor_lines), ""]

    unsure = [i for i in extraction.get("issues", []) if i["level"] == "unsure"]
    if unsure:
        out += ["Sorry, I could not understand everything",
                *(f"   {i['what'][:1].upper() + i['what'][1:]}: {_lower_first(i['why'])}." for i in unsure),
                "   Please ask your doctor or pharmacist about this.", ""]

    out.append(DISCLAIMER)
    return out


def needs_review(extraction: dict) -> bool:
    """True when something on the prescription was not understood with certainty."""
    return any(i["level"] == "unsure" for i in _all_issues(extraction))


def _all_issues(extraction: dict) -> list[dict]:
    issues = [dict(i, source=med["name_as_written"]) for med in extraction["medicines"] for i in med.get("issues", [])]
    return issues + [dict(i, source="Prescription") for i in extraction.get("issues", [])]


def render_review(extraction: dict) -> list[str]:
    """For the doctor (not the patient): what was not understood, what was corrected, and why."""
    issues = _all_issues(extraction)
    unsure = [i for i in issues if i["level"] == "unsure"]
    notes = [i for i in issues if i["level"] == "note"]

    out = ["For the doctor: please review"]
    if removed := extraction.get("privacy_removed"):
        out.append(f"   PRIVACY    Removed before sending to the AI: {', '.join(removed)}. This tool only needs the medical part.")
    if unsure:
        out.append(f"   CHECK BEFORE GIVING THIS TO THE PATIENT: I am not sure about {len(unsure)} thing{'s' if len(unsure) != 1 else ''}.")
    else:
        out.append("   Everything on the prescription was understood.")
    for i in unsure:
        out.append(f"   NOT SURE   {i['source']}: {i['what']} ({_lower_first(i['why'])}).")
    for i in notes:
        out.append(f"   CORRECTED  {i['source']}: {i['what']} ({_lower_first(i['why'])}).")
    if any(m.get("purpose") for m in extraction["medicines"]):
        out.append('   AI-ADDED   The lines "This medicine is ..." are not on the prescription: please check them.')
    return out


def render_explanation(extraction: dict) -> str:
    """Turn the structured extraction into the plain-English text shown to the patient."""
    out: list[str] = []
    if illness := render_conditions(extraction.get("conditions", [])):
        out += [*illness, ""]

    meds = extraction["medicines"]
    if meds:
        count = len(meds)
        out += ["Your medicines", f"You have {count} medicine{'s' if count != 1 else ''}.", ""]
    for i, med in enumerate(meds, 1):
        out += [*render_medicine(i, med), ""]
    out += render_footer(extraction)
    return "\n".join(out)


def simplify_prescription(text: str) -> str:
    """Turn a raw prescription into a patient-friendly English explanation."""
    return render_explanation(extract_prescription(text))


# --------------------------------------------------------------------------- #
# Optional — Text to speech (off by default in process_prescription)
# --------------------------------------------------------------------------- #

def _clean_for_speech(text: str) -> str:
    """Remove symbols so the TTS voice doesn't read them out."""
    text = re.sub(r"[*_#`>|!]+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)  # icons
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{2,}", "\n", text).strip()


def text_to_audio(text: str, language: str, output_path: str) -> str:
    """Synthesize `text` to an MP3 file with gTTS and return its path."""
    from gtts import gTTS  # lazy import

    if not text or not text.strip():
        raise PipelineError("Text for audio is empty.")

    lang_code = _TTS_LANGUAGE_CODES.get((language or "english").strip().lower(), "en")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        gTTS(text=_clean_for_speech(text), lang=lang_code, slow=False).save(str(out))
    except Exception as exc:
        raise PipelineError(f"Text-to-speech failed: {exc}") from exc
    return str(out)


# --------------------------------------------------------------------------- #
# Master function
# --------------------------------------------------------------------------- #

def process_prescription(
    input_text: str | None = None,
    audio_path: str | None = None,
    output_dir: str = "outputs",
    generate_audio: bool = False,
) -> dict:
    """Run the full pipeline.

    Provide either `input_text` or `audio_path` (text wins if both are given).
    Audio output is off for now; pass `generate_audio=True` to turn it back on.

    Returns:
        {
            "simplified_english": str,      # text to show the patient
            "structured": dict,             # per-medicine fields, for building UI cards
            "audio_file_path": str | None,  # None unless generate_audio=True
        }
    """
    if input_text and input_text.strip():
        source_text = input_text.strip()
    elif audio_path:
        logger.info("Transcribing audio...")
        source_text = transcribe_voice(audio_path)
    else:
        raise PipelineError("Provide either input_text or audio_path.")

    logger.info("Reading prescription (%s)...", LLM_MODEL)
    structured = extract_prescription(source_text)
    simplified = render_explanation(structured)

    audio_file_path = None
    if generate_audio:
        audio_file = Path(output_dir) / f"prescription_{uuid.uuid4().hex[:8]}.mp3"
        logger.info("Generating audio...")
        audio_file_path = text_to_audio(simplified, "english", str(audio_file))

    return {
        "simplified_english": simplified,
        "needs_review": needs_review(structured),       # True: a person must check before the patient gets it
        "review": "\n".join(render_review(structured)),  # for the doctor: doubts, corrections and why
        "structured": structured,
        "audio_file_path": audio_file_path,
    }


# --------------------------------------------------------------------------- #
# Quick manual test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # hide per-request HTTP logs

    # The header is personal on purpose: it is removed locally before anything reaches the AI.
    sample_french_prescription = """Dr. Karim Benali — Médecine générale
Patient : Mme Fatima Z., 54 ans
Date : 10/10/2026

1) Amoxicilline 1 g : 1 cp x 2/j pendant 7 jours, au milieu des repas.
2) Paracétamol 1 g : 1 cp toutes les 6 h si douleur ou fièvre, max 4 cp/j.
3) Oméprazole 20 mg : 1 gél le matin à jeun pendant 14 jours.
4) Metformine 850 mg : 1 cp matin et soir au cours des repas. À renouveler 3 mois.

Éviter l'alcool. Revoir dans 15 jours."""

    key_env = _PROVIDERS[LLM_PROVIDER][1]
    if key_env and not os.getenv(key_env):
        print(f"{key_env} is not set. Run:  export {key_env}=your_key")
        print("Or try the free local model:  LLM_PROVIDER=ollama python ai_pipeline.py")
        sys.exit(1)

    # Usage: python ai_pipeline.py ["your own prescription text"]
    text = " ".join(sys.argv[1:]) or sample_french_prescription
    print(f"Provider: {LLM_PROVIDER} | Model: {LLM_MODEL}\n", flush=True)
    result = process_prescription(input_text=text)

    print(result["simplified_english"])
