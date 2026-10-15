"""
ai_pipeline.py — ClearCare: medical prescription explainer pipeline (AMD Developer Hackathon ACT III).

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
import unicodedata
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

AMD_BASE_URL = os.getenv("AMD_BASE_URL") or "https://developer.amd.com.cn/radeon/api/v1"
AMD_MODEL = os.getenv("AMD_MODEL") or "DeepSeek-V4.1-Flash"

# Provider -> (base URL, API key env var or None, default model).
# All are OpenAI-compatible, so the same SDK client works for each.
_PROVIDERS: dict[str, tuple[str, str | None, str]] = {
    "amd": (AMD_BASE_URL, "AMD_API_KEY", AMD_MODEL),
    "ollama": (
        os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434/v1",
        None,  # local, no key needed
        os.getenv("OLLAMA_MODEL") or "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q4_K_M",
    ),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        os.getenv("OPENROUTER_MODEL") or "meta-llama/llama-3.3-70b-instruct:free",
    ),
}

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "amd").strip().lower()
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
_TTS_LANGUAGE_CODES = {"english": "en", "french": "fr", "arabic": "ar", "spanish": "es", "german": "de",
                       "portuguese": "pt", "italian": "it", "dutch": "nl", "turkish": "tr"}

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
    # On "429 too many requests" the SDK waits (exponential backoff, honours Retry-After) and retries.
    return OpenAI(api_key=api_key, base_url=base_url, max_retries=6, timeout=120)


# --------------------------------------------------------------------------- #
# Daily spending guard for paid providers (AMD: a daily budget shared by everyone using the key)
# --------------------------------------------------------------------------- #

USAGE_PATH = Path(__file__).parent / ".cache" / "ai_usage.json"
_usage_lock = threading.Lock()


def _budget_day() -> str:
    """AMD's day resets at 17:00 Asia/Shanghai (10:00 in Morocco)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(hours=17)
    return now.strftime("%Y-%m-%d")


def daily_limit(provider: str) -> int:
    """Token limit per day for a provider (0 = no limit). Set e.g. AMD_DAILY_TOKEN_LIMIT=100000."""
    try:
        return int(os.getenv(f"{provider.upper()}_DAILY_TOKEN_LIMIT") or 0)
    except ValueError:
        return 0


def tokens_used_today(provider: str) -> int:
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    return int(data.get(_budget_day(), {}).get(provider, 0))


def record_usage(provider: str, tokens: int) -> None:
    if not tokens:
        return
    with _usage_lock:
        try:
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        day = data.setdefault(_budget_day(), {})
        day[provider] = int(day.get(provider, 0)) + int(tokens)
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")


def budget_left(provider: str) -> int | None:
    """Tokens left today, or None when there is no limit."""
    limit = daily_limit(provider)
    return None if not limit else max(0, limit - tokens_used_today(provider))


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

    provider, client = _pick_client(kwargs)

    try:
        try:
            response = client.chat.completions.create(**kwargs)
        except BadRequestError:
            if not json_mode:
                raise
            # Some endpoints don't support response_format; the prompt still asks for JSON.
            kwargs.pop("response_format")
            response = client.chat.completions.create(**kwargs)
    except PipelineError:
        raise
    except Exception as exc:  # network / auth / rate-limit errors from the SDK
        raise PipelineError(f"Sorry, I could not reach the AI service ({LLM_PROVIDER}). Why: {exc}") from exc

    if _PROVIDERS[provider][1] is not None:  # paid / keyed providers are counted against the daily budget
        record_usage(provider, getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
    content = (response.choices[0].message.content or "").strip()
    # Reasoning models sometimes inline their chain of thought; strip it.
    content = _THINK_RE.sub("", content).strip()
    if not content:
        raise PipelineError(f"Sorry, the AI service ({LLM_PROVIDER}) sent back an empty answer. Please try again.")
    return content


def _chat_stream(system_prompt: str, user_prompt: str, json_mode: bool = False, max_tokens: int | None = None):
    """Like _chat(), but yields the answer piece by piece as the model writes it."""
    kwargs = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    provider, client = _pick_client(kwargs)

    try:
        try:
            stream = client.chat.completions.create(**kwargs)
        except BadRequestError:
            # Some endpoints don't support response_format or usage in streams; the prompt still asks for JSON.
            kwargs.pop("response_format", None)
            kwargs.pop("stream_options", None)
            stream = client.chat.completions.create(**kwargs)
        tokens = 0
        for chunk in stream:
            if getattr(chunk, "usage", None):
                tokens = chunk.usage.total_tokens or 0
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Sorry, I could not reach the AI service ({LLM_PROVIDER}). Why: {exc}") from exc
    if _PROVIDERS[provider][1] is not None:
        record_usage(provider, tokens)


def _pick_client(kwargs: dict):
    """The configured client, or the fallback provider when today's budget is used up."""
    provider, client = LLM_PROVIDER, None
    if budget_left(provider) == 0:
        fallback = (os.getenv(f"{provider.upper()}_FALLBACK") or "").strip().lower()
        if fallback in _PROVIDERS and fallback != provider and budget_left(fallback) != 0:
            logger.warning("Daily %s budget used up: using %s for this call", provider, fallback)
            base_url, key_env, model = _PROVIDERS[fallback]
            client = OpenAI(api_key=os.getenv(key_env) if key_env else "not-needed", base_url=base_url)
            provider, kwargs["model"] = fallback, model
        else:
            raise PipelineError(
                f"Sorry, today's {provider.upper()} budget is used up ({tokens_used_today(provider)} tokens). "
                "It refills at 10:00 Morocco time; you can also use the free local model (-p ollama).")
    return provider, client or _get_llm_client()


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
  "per_week": number of times per WEEK, only for weekly medicines (e.g. "1 fois par semaine"),
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
"unreadable": [{"text": a part you could not understand, "why": short reason}] (never list text you already used above)}

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


_DRUG_PURPOSE_SYSTEM_PROMPT = """You are a careful pharmacist. Say what a medicine is used for, to a patient who may not read well.

First decide honestly if you KNOW this medicine. You know it only if you are certain of its active
ingredient. If the name is unknown to you, looks misspelled or misread, could be several different
medicines, or you would be guessing in any way, you do NOT know it. Saying you do not know is the
right answer then; a wrong answer can hurt the patient.

Return compact JSON only:
{"known": true or false,
 "active_ingredient": "the generic name, or null if you do not know it",
 "purpose": "what it is used for, fitting the sentence 'This medicine is ___.', max 8 words, or null"}

Use simple everyday words a child understands (say "infection", "pain", "fever", "sugar in your blood",
"blood pressure", "allergies", "stomach acid"). No advice, warnings or instructions.
Never give a vague purpose such as "to help you feel better": if you do not know the real use, purpose is null.

Example for ibuprofen: {"known":true,"active_ingredient":"ibuprofen","purpose":"for pain, swelling and fever"}
Example for Kardegic: {"known":true,"active_ingredient":"aspirin","purpose":"to stop blood clots"}
Example for Zorvatex: {"known":false,"active_ingredient":null,"purpose":null}"""


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
    level "risk":   shown to the doctor only, and the doctor must confirm (high-risk medicine).
    """
    target.setdefault("issues", []).append({"level": level, "what": what.strip(), "why": why.strip().rstrip(".")})


def _letters(text: str) -> str:
    """"co-vid" / "COVID-19" -> "covid" / "covid": for loose name matching."""
    return re.sub(r"[^a-z]", "", text.lower())


def _normalize_med(med) -> dict | None:
    """One medicine of the model's answer in the public shape, or None if it has no name."""
    if not isinstance(med, dict):
        return None
    name = _as_str(med.get("name")) or _as_str(med.get("generic"))
    if not name:
        return None
    times = {t.lower() for t in _as_list(med.get("times"))} & set(_TIME_ORDER)
    return {
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
        "times_per_week": _as_int(med.get("per_week")),
        "weekday": None,
        "times_per_day_range": None, "single_dose": False,
        "purpose": None,  # filled in by _add_drug_info()
        "issues": [{"level": "unsure", "what": "part of this medicine's instructions", "why": why.rstrip(".")}
                   for why in _as_list(med.get("unclear"))],
    }


def _normalize_dosing(data: dict) -> dict:
    """Map the model's compact keys onto the public `structured` shape (models drift on types)."""
    medicines = [m for m in map(_normalize_med, data.get("meds") or data.get("medicines") or []) if m]
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
    return f"purpose-v2::{LLM_MODEL}::{name.lower()}"


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


# Purposes that say nothing: treated as "I do not know what it is for".
_VAGUE_PURPOSE_RE = re.compile(r"feel better|sick or hurt|your health|general|various|many (?:things|conditions)|"
                               r"different (?:things|problems)|^for (?:you|treatment)$|help (?:you|your body)$", re.IGNORECASE)


def _fetch_drug_purpose(med: dict) -> dict | None:
    """One tiny LLM call: does the AI really know this medicine, and what is it for?
    Returns {"known": bool, "active": str|None, "purpose": str|None}, or None if the call failed."""
    label = med["name_as_written"] if not med.get("common_name") else f"{med['name_as_written']} ({med['common_name']})"
    try:
        data = _parse_json_object(_chat(_DRUG_PURPOSE_SYSTEM_PROMPT, f"Medicine: {label}", max_tokens=90, json_mode=True))
    except (PipelineError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Could not look up %s: %s", label, exc)
        return None
    active = _as_str(data.get("active_ingredient"))
    purpose = _as_str(data.get("purpose"))
    if purpose:  # the model sometimes repeats the sentence it is slotted into
        purpose = re.sub(r"^\s*(this medicine is)\s*", "", purpose, flags=re.IGNORECASE).strip().rstrip(".")
        if _VAGUE_PURPOSE_RE.search(purpose):
            purpose = None
    known = data.get("known") in (True, "true", "True", "yes") and bool(active)  # no active ingredient = not known
    return {"known": known, "active": active if known else None, "purpose": purpose if known else None}


MEDICINE_NAMES_PATH = Path(os.getenv("MEDICINE_NAMES", Path(__file__).parent / "data" / "medicine_names.txt"))


@lru_cache(maxsize=1)
def _known_names() -> frozenset:
    try:
        lines = MEDICINE_NAMES_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return frozenset()
    return frozenset(l.strip() for l in lines if l.strip() and not l.startswith("#"))


def _name_key(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z]", "", text)


def name_is_known(med: dict) -> bool:
    """Is the written name (not the AI's guess) a medicine on our list? Small models will happily
    "recognise" a made-up name, so this is decided by the list, not by the model."""
    names = _known_names()
    if not names:  # no list available: fall back to the model's own answer
        return True
    words = short_name(med).split()
    candidates = [" ".join(words), words[0] if words else ""] + [w for w in words if len(w) >= 4]
    return any(_name_key(c) in names for c in candidates if len(_name_key(c)) >= 4)


def _apply_purpose(med: dict, info: dict | None) -> None:
    """Say plainly what we do not know, instead of guessing."""
    if not name_is_known(med):
        med["unknown_medicine"] = True
        _flag(med, "unsure", "this medicine's name",
              "I do not recognise this medicine; the name may be misread or misspelled")
        return
    if info is None:
        _flag(med, "note", "what it is for", "I could not look up what this medicine is used for")
        return
    if isinstance(info, str):  # older cache entries
        info = {"known": True, "active": None, "purpose": info}
    if not info["known"]:  # a real name, but the AI does not know it well enough to say what it is for
        med["purpose_unknown"] = True
        return
    med["purpose"] = _plain(info["purpose"])
    if not med["purpose"]:
        med["purpose_unknown"] = True


def _iter_drug_info(extraction: dict, started: dict | None = None):
    """Fill in what each medicine is for, yielding each medicine's index as it is done.

    Cached drugs are yielded immediately; the rest are fetched in parallel and yielded in the
    order they finish, so a caller can show each one as soon as it is ready. `started` holds
    lookups already running ({cache key: future}), begun while the prescription was still being read.
    """
    started = started or {}
    meds = extraction["medicines"]
    with _cache_lock:
        cache = _load_drug_cache()

    missing: dict[str, list[int]] = {}
    for i, med in enumerate(meds):
        if med.get("no_name"):
            yield i
            continue
        key = _drug_key(med)
        if key in cache:
            _apply_purpose(med, cache[key])
            yield i
        else:
            missing.setdefault(key, []).append(i)
    if not missing:
        return

    logger.info("Looking up %d drug(s): %s", len(missing), ", ".join(k.split("::")[-1] for k in missing))
    with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
        futures = {started.get(key) or pool.submit(_fetch_drug_purpose, meds[idx[0]]): key
                   for key, idx in missing.items()}
        for future in as_completed(futures):
            key = futures[future]
            info = future.result()
            if info is not None:
                with _cache_lock:
                    _save_drug_cache({**_load_drug_cache(), key: info})
            for i in missing[key]:
                _apply_purpose(meds[i], info)
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


# Where a new medicine starts inside a line: after ". " / "; " / ", ", a word followed by a strength
# or a colon ("Spasfon :", "Zorbitrax 20 mg", "Doliprane 1000 :"), or a medicine name from our list.
_NEXT_MED_RE = re.compile(
    r"(?:(?<=[.;,])|(?<=[.;,]\s)|(?<=[.;,]\s\s))(?=([A-Za-zÀ-ÿ][\w-]{2,})"
    r"(\s+\d+(?:[.,]\d+)?\s*(?:mg|g|ml|µg|mcg|ui)\b|(?:\s+\d+(?:[.,]\d+)?)?\s*:)?)",
    re.IGNORECASE,
)


def _medicine_parts(line: str) -> list[str]:
    """A line cut where each medicine starts, for lines with several medicines (often dictated)."""
    names, cuts = _known_names(), [0]
    for m in _NEXT_MED_RE.finditer(line):
        if m.start() and (m.group(2) or _name_key(m.group(1)) in names):
            cuts.append(m.start())
    return [line[a:b] for a, b in zip(cuts, cuts[1:] + [len(line)])]


def _source_line(med: dict, source_text: str) -> str | None:
    """The part of the prescription about this medicine: its line, or its part of a line that
    holds several medicines (matched on the first word of its name, accents ignored)."""
    first_word = _name_key(med["name_as_written"].split()[0])
    if not first_word:
        return None
    line = next((line for line in source_text.splitlines() if first_word in _name_key(line)), None)
    if line is None:
        return None
    return next((part for part in _medicine_parts(line) if first_word in _name_key(part)), line)


def name_on_prescription(med: dict, source_text: str) -> bool:
    """Is the medicine's name really written on the prescription? A name that is not there was
    made up (models sometimes copy the example from their instructions) and is never shown."""
    if med.get("no_name"):
        return True
    words = [_name_key(w) for w in med["name_as_written"].split()]
    words = [w for w in words if len(w) >= 3 and not w.isdigit()]
    text = _name_key(source_text)
    # The first 5 letters are enough, so small spelling differences ("Doliprane"/"Doliprene") still match.
    return any(w[:5] in text for w in words[:2]) if words else True


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
_DURATION_RE = re.compile(r"\b(?:pendant|durant|qsp|for|x)\s*(\d+|un|une|deux|trois|quatre|cinq|six|sept|huit|dix|quinze|vingt|trente)\s*(jours?|semaines?|mois|days?|weeks?|months?)\b", re.IGNORECASE)
_LONG_TERM_RE = re.compile(r"\b(au long cours|à vie|a vie|long terme|traitement chronique|à poursuivre|ne pas arr[êe]ter|long[- ]term|for life|ongoing)\b", re.IGNORECASE)


# What the line must contain for each food instruction to be kept.
_MEAL = r"(?:les?\s+|la\s+|du\s+)?(?:repas|manger|d[ée]jeuner|d[îi]ner|petit[- ]d[ée]j\w*|meals?|food|eating|breakfast|lunch|dinner)"
_FOOD_EVIDENCE = {
    "with food": re.compile(r"repas|manger|meal|food|eating", re.IGNORECASE),
    "before food": re.compile(r"\b(?:avant|before)\s+" + _MEAL + r"|\bac\b", re.IGNORECASE),
    "after food": re.compile(r"\b(?:apr[èe]s|after)\s+" + _MEAL + r"|\bpc\b", re.IGNORECASE),
    "empty stomach": re.compile(r"jeun|empty stomach|fasting|\b(?:avant|before)\s+" + _MEAL, re.IGNORECASE),
}
_PRN_RE = re.compile(r"\b(si besoin|si n[ée]cessaire|en cas de|si|prn|as needed|if needed|when needed)\b\s*(.*)", re.IGNORECASE)
_PRN_REASONS = [("douleur", "pain"), ("fi[èe]vre", "fever"), ("mal de gorge", "a sore throat"), ("toux", "a cough"),
                ("naus[ée]e", "nausea"), ("allergie", "allergy"), ("insomnie", "trouble sleeping"),
                ("pain", "pain"), ("fever", "fever")]
_TIME_EVIDENCE = [("morning", r"\bmatin\b|\bmorning\b"), ("noon", r"\bmidi\b|\bnoon\b|\blunch\b"),
                  ("evening", r"\bsoir\b|\bevening\b"), ("bedtime", r"\bcoucher\b|\bbedtime\b|\bhs\b")]
_PER_DAY_RE = re.compile(r"(?:x\s*(\d+)\s*/\s*j|(\d+)\s*(?:fois|x)\s*(?:par|/)\s*j|(\d+)\s*times? (?:a|per) day)", re.IGNORECASE)


_NUM = r"(\d+|" + "|".join(w for w in ("un", "une", "one", "deux", "two", "trois", "three", "quatre", "four",
                                         "cinq", "five", "six", "huit", "eight", "dix", "ten")) + r")"
_PER_DAY_WORDS_RE = re.compile(_NUM + r"\s*(?:fois|x|times?)\s*(?:par|/|a|per)\s*(?:j\b|jour|day)", re.IGNORECASE)
_PER_WEEK_RE = re.compile(r"(?:" + _NUM + r"\s*(?:fois|x|times?)?\s*(?:par|/|a|per)\s*(?:semaine|sem\b|week)|hebdomadaire|weekly|once a week)",
                          re.IGNORECASE)
_EVERY_HOURS_RE = re.compile(r"toutes les\s*(\d+)\s*h|\bq\s?(\d+)\s?h\b|every\s*(\d+)\s*h", re.IGNORECASE)
_MAX_PER_DAY_RE = re.compile(r"max(?:imum)?\.?\s*" + _NUM + r"\s*([a-zéèû.]+)?\s*(?:/|par|per|a)\s*(?:j\b|jour|day)", re.IGNORECASE)
_UNIT_WORDS = [(r"cp|comprim|tab", "tablet"), (r"g[ée]l|cap", "capsule"), (r"sach", "sachet"), (r"bouff|puff", "puff"),
               (r"past|lozenge", "lozenge"), (r"goutt|drop", "drop")]
_WEEKDAYS = [(r"\blundi|\bmonday", "Monday"), (r"\bmardi|\btuesday", "Tuesday"), (r"\bmercredi|\bwednesday", "Wednesday"),
             (r"\bjeudi|\bthursday", "Thursday"), (r"\bvendredi|\bfriday", "Friday"), (r"\bsamedi|\bsaturday", "Saturday"),
             (r"\bdimanche|\bsunday", "Sunday")]
_LATIN_PER_DAY = {"qd": 1, "od": 1, "bid": 2, "tid": 3, "qid": 4}


_PER_DAY_RANGE_RE = re.compile(_NUM + r"\s*(?:à|a|-|to|ou|or)\s*" + _NUM +
                               r"\s*(?:fois|x|times?)\s*(?:par|/|a|per)\s*(?:j\b|jour|day|24\s*h)", re.IGNORECASE)
_ONE_UNIT_PER_DAY_RE = re.compile(r"\b(?:1|un|une|one)\s*(?:cp|comprim\w*|g[ée]l\w*|sach\w*|tab\w*|cap\w*|pill\w*|"
                                  r"ovule\w*|suppo\w*|ampoule\w*|patch\w*|application\w*)\s*(?:par|/|a|per)\s*(?:j\b|jour|day|24\s*h)",
                                  re.IGNORECASE)
_PER_24H_RE = re.compile(_NUM + r"\s*(?:fois|x|times?)\s*(?:par|/|per|toutes les|every)\s*24\s*h", re.IGNORECASE)
_SINGLE_DOSE_RE = re.compile(r"prise unique|dose unique|en une (?:seule )?prise|une seule fois|single dose|one[- ]off|\bstat\b",
                             re.IGNORECASE)


def per_day_range_in_line(line: str) -> tuple[int, int] | None:
    """"1 à 4 fois par 24 heures" -> (1, 4)."""
    if m := _PER_DAY_RANGE_RE.search(line):
        low, high = _number_value(m.group(1)), _number_value(m.group(2))
        if low and high and low < high:
            return low, high
    return None


def _per_day_in_line(line: str) -> int | None:
    """Times per day as written: "x 2/j", "3 fois par jour", "trois fois par jour", "TID", "2 fois par 24 h",
    "1 comprimé par jour" (= once a day)."""
    if m := _PER_24H_RE.search(line):
        return _number_value(m.group(1))
    if _ONE_UNIT_PER_DAY_RE.search(line):
        return 1
    if m := _PER_DAY_RE.search(line):
        return int(next(g for g in m.groups() if g))
    if m := _PER_DAY_WORDS_RE.search(line):
        return _number_value(m.group(1))
    if m := re.search(r"\b(qd|od|bid|tid|qid)\b", line, re.IGNORECASE):
        return _LATIN_PER_DAY[m.group(1).lower()]
    return None


# Phrases the code already understands; the model sometimes lists them as "unreadable" anyway.
_UNDERSTOOD_RE = re.compile(r"\bqsp\b|long cours|long terme|[àa] jeun|repas|si besoin|si douleur|si fi[èe]vre|matin|soir|coucher|"
                            r"\brepos\b|\brest\b|boire|hydrat|r[ée]gime|marche", re.IGNORECASE)


def _drop_understood_unreadable(extraction: dict, source_text: str) -> None:
    """Turn "unreadable" items the code does understand into doctor-only notes."""
    kept, notes = [], []
    for issue in extraction["issues"]:
        text = issue["what"].strip('"')
        if issue["level"] == "unsure" and _UNDERSTOOD_RE.search(text) and text.lower() in source_text.lower():
            notes.append({"level": "note", "what": issue["what"],
                          "why": f"the AI marked it unreadable ({_lower_first(issue['why'])}), but it is understood"})
        else:
            kept.append(issue)
    extraction["issues"] = kept + notes


# A "name" that is really just a dose: "5 comprimés", "1 comprimé 3 fois par jour", "2 gélules le matin".
_NO_NAME_RE = re.compile(r"(?:\d+(?:[.,/]\d+)?|un|une|deux|trois)\s*(?:cp|comprim\w*|g[ée]l\w*|sach\w*|tab\w*|cap\w*|pills?|"
                         r"gouttes?|drops?|ml|bouff\w*|puffs?|ampoules?|ovules?|injections?)\b.*", re.IGNORECASE)


def _name_on_line(line: str) -> str | None:
    """A medicine name from our list written on this line, with its strength ("Amoxicilline 1 g")."""
    names = _known_names()
    for m in re.finditer(r"[A-Za-zÀ-ÿ][\w-]{3,}", line):
        if _name_key(m.group(0)) in names:
            strength = re.match(r"\s+\d+(?:[.,]\d+)?\s*(?:mg|g|ml|µg|mcg|ui)\b", line[m.end():], re.IGNORECASE)
            return m.group(0) + (strength.group(0) if strength else "")
    return None


def _check_against_line(med: dict, source_text: str) -> None:
    """Small models drop or invent details. Re-read the medicine's own line:
    - remove a food instruction the line does not mention;
    - fill in "when needed", times of day, times per day, duration and "à sucer" if the model missed them.
    """
    if _NO_NAME_RE.fullmatch(med["name_as_written"].strip()):
        # The AI wrote the dose where the name goes. Find the line with that dose and read the name there.
        dose = med["name_as_written"].strip()
        line = next((l for l in source_text.splitlines() if _name_key(dose) and _name_key(dose) in _name_key(l)),
                    source_text.splitlines()[0] if source_text.strip() else "")
        if name := _name_on_line(line):
            _flag(med, "note", "medicine name", f'the AI wrote "{dose}" as the name; "{name}" is read from the prescription')
            med["name_as_written"], med["common_name"] = name, None
        else:
            med["name_as_written"] = "Medicine (name not written)"
            med["common_name"] = None
            med["no_name"] = True
            _flag(med, "unsure", "which medicine this is", "the medicine's name is not written on the prescription")
    else:
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

    written_times = [t for t, pattern in _TIME_EVIDENCE if re.search(pattern, line, re.IGNORECASE)]
    if invented := [t for t in med.get("times_of_day") or [] if t not in written_times]:
        _flag(med, "note", "time of day", f"removed {', '.join(invented)}: it is not on the prescription")
        med["times_of_day"] = [t for t in med["times_of_day"] if t in written_times]
    if not med.get("times_of_day"):
        med["times_of_day"] = written_times
        if med["times_of_day"]:
            _flag(med, "note", "time of day", f"added {', '.join(med['times_of_day'])} from the prescription (the AI had missed it)")

    # The AI's "per week" and "every N hours" need the words on the line ("par 24 heures" is per DAY).
    if med.get("times_per_week") and not _PER_WEEK_RE.search(line):
        _flag(med, "note", "weekly", f'removed "{med["times_per_week"]} time(s) a week": the prescription does not say weekly')
        med["times_per_week"], med["weekday"] = None, None
    if med.get("every_hours") and not _EVERY_HOURS_RE.search(line):
        _flag(med, "note", "hours between doses", f'removed "every {med["every_hours"]} hours": it is not written that way')
        med["every_hours"] = None

    # Weekly medicines (e.g. methotrexate): a daily reading here would be dangerous.
    if m := _PER_WEEK_RE.search(line):
        per_week = _number_value(m.group(1)) if m.group(1) else 1
        if med.get("times_per_week") != per_week or med.get("times_per_day"):
            _flag(med, "note", "weekly", f'read "{m.group(0).strip()}" as {per_week} time(s) a week')
        med["times_per_week"], med["times_per_day"] = per_week, None
        if day := next((en for fr, en in _WEEKDAYS if re.search(fr, line, re.IGNORECASE)), None):
            med["weekday"] = day

    if rng := per_day_range_in_line(line):
        med["times_per_day_range"] = rng
        med["times_per_day"] = None
        _flag(med, "note", "times per day", f"read {rng[0]} to {rng[1]} times a day from the prescription")

    if _SINGLE_DOSE_RE.search(line):
        med["single_dose"] = True
        med["times_per_day"], med["times_of_day"] = None, med.get("times_of_day") or []
        _flag(med, "note", "single dose", f'read "{_SINGLE_DOSE_RE.search(line).group(0)}" as one single dose')

    written_per_day = None if med.get("times_per_day_range") else _per_day_in_line(line)
    if med.get("times_per_day") and med["times_per_day"] != written_per_day \
            and med["times_per_day"] != len(med.get("times_of_day") or []):
        _flag(med, "note", "times per day", f'removed "{med["times_per_day"]} times a day": it is not on the prescription')
        med["times_per_day"] = None
    if not med.get("times_per_day") and written_per_day and not med.get("times_per_week"):
        med["times_per_day"] = written_per_day
        _flag(med, "note", "times per day", f"added {written_per_day} times a day from the prescription (the AI had missed it)")

    if not med.get("every_hours") and (m := _EVERY_HOURS_RE.search(line)):
        med["every_hours"] = int(next(g for g in m.groups() if g))
        _flag(med, "note", "hours between doses", f'added "every {med["every_hours"]} hours" from "{m.group(0)}" (the AI had missed it)')

    if not med.get("max_per_day") and (m := _MAX_PER_DAY_RE.search(line)):
        number = _number_value(m.group(1))
        unit = next((en for pattern, en in _UNIT_WORDS if m.group(2) and re.match(pattern, m.group(2), re.IGNORECASE)), None)
        med["max_per_day"] = _with_unit(str(number), f"1 {unit}" if unit else med.get("amount_per_dose"))
        _flag(med, "note", "maximum per day", f'added "{med["max_per_day"]}" from "{m.group(0)}" (the AI had missed it)')

    if med.get("duration") and "long" in med["duration"].lower() and not _LONG_TERM_RE.search(line):
        _flag(med, "note", "duration", f'removed "{med["duration"]}": the prescription does not say it is long-term')
        med["duration"] = None

    if med.get("route") and re.search(r"\bsuck", med["route"], re.IGNORECASE) and re.search(r"sucer", line, re.IGNORECASE):
        med["route"] = "Suck it"  # only what is written: "à sucer" (the AI likes to add "slowly")
    if not med.get("route") and re.search(r"[àa] sucer|sucer", line, re.IGNORECASE):
        med["route"] = "Suck it"
        _flag(med, "note", "how to use", 'added "suck it" from "à sucer" (the AI had missed it)')

    before = med.get("duration")
    _fill_duration(med, line)
    if med.get("duration") != before:
        _flag(med, "note", "duration", f'set to "{med["duration"]}" from the prescription (the AI had {"missed it" if not before else repr(before)})')


def _fill_duration(med: dict, line: str) -> None:
    """Small models sometimes drop "QSP 3 mois" or "au long cours"; read it from the line itself."""
    if _LONG_TERM_RE.search(line):
        med["duration"] = "long term"  # "do not stop" matters more than a refill period
    elif not med.get("duration") and (m := _DURATION_RE.search(line)):
        number, unit = str(_number_value(m.group(1))), _UNITS_EN[m.group(2).lower()]
        if number == "1" and unit == "months":
            unit = "month"
        med["duration"] = f"{number} {unit}"


_NUMBER_WORDS = {
    "un": 1, "une": 1, "one": 1, "once": 1, "deux": 2, "two": 2, "twice": 2, "trois": 3, "three": 3,
    "quatre": 4, "four": 4, "cinq": 5, "five": 5, "six": 6, "sept": 7, "seven": 7, "huit": 8, "eight": 8,
    "neuf": 9, "nine": 9, "dix": 10, "ten": 10, "douze": 12, "twelve": 12, "quinze": 15, "fifteen": 15,
    "vingt": 20, "twenty": 20, "trente": 30, "thirty": 30,
    "qd": 1, "od": 1, "bid": 2, "tid": 3, "qid": 4,
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)


def _number_value(token: str) -> int | None:
    token = token.lower()
    return int(token) if token.isdigit() else _NUMBER_WORDS.get(token)


def numbers_on_prescription(text: str) -> set[str]:
    """Digits on the prescription, plus numbers written as words ("cinq jours") or Latin
    abbreviations ("TID" = 3), so they count as written by the doctor."""
    return _numbers(text) | {str(_NUMBER_WORDS[w.lower()]) for w in _NUMBER_WORD_RE.findall(text)}


# Medicines where a misunderstanding can seriously harm the patient. Matched on the name as
# written or the generic name. The doctor must always confirm these explicitly.
_HIGH_RISK = [
    (r"m[ée]thotrexate|novatrex|imeth", "methotrexate is taken once a WEEK; taking it daily is dangerous"),
    (r"warfarin|coumadine|sintrom|ac[ée]nocoumarol|previscan|fluindione|apixaban|eliquis|rivaroxaban|xarelto|dabigatran|pradaxa",
     "blood thinner: a wrong dose can cause bleeding or clots"),
    (r"h[ée]parine|heparin|enoxaparin|lovenox|innohep|tinzaparin", "blood thinner injection"),
    (r"insulin|insuline|lantus|levemir|novorapid|humalog|toujeo|tresiba|apidra|actrapid|insulatard", "insulin: a wrong dose can cause dangerously low sugar"),
    (r"morphin|oxycodon|fentanyl|tramadol|cod[ée]ine|m[ée]thadone|buprenorphin|tapentadol", "opioid painkiller"),
    (r"lithium|t[ée]ralithe", "lithium: a small dose change can be toxic"),
    (r"digoxin|digoxine", "digoxin: a small dose change can be toxic"),
    (r"colchicin", "colchicine: too much is toxic"),
]
_CHILD_RE = re.compile(r"\b(enfant|nourrisson|b[ée]b[ée]|p[ée]diatrique|dose[- ]poids|child|infant|baby|\d+\s*kg)\b", re.IGNORECASE)


def _flag_high_risk(med: dict, source_text: str) -> None:
    names = f"{med['name_as_written']} {med.get('common_name') or ''}"
    for pattern, why in _HIGH_RISK:
        if re.search(pattern, names, re.IGNORECASE):
            _flag(med, "risk", "high-risk medicine", why)
            break
    else:
        if med.get("times_per_week"):
            _flag(med, "risk", "weekly medicine", "taken once a week; a daily reading would be dangerous")
    line = _source_line(med, source_text) or ""
    if _CHILD_RE.search(line):
        _flag(med, "risk", "child's dose", "doses for children depend on age and weight")


def check_medicine(med: dict, source_text: str, source_numbers: set[str] | None = None) -> dict:
    """All the checks of one medicine against its line of the prescription. Safe to show afterwards."""
    if source_numbers is None:
        source_numbers = numbers_on_prescription(source_text)
    if not _NO_NAME_RE.fullmatch(med["name_as_written"].strip()) and not name_on_prescription(med, source_text):
        med["not_on_prescription"] = True
        med["_checked"] = True
        return med
    for issue in med.get("issues", []):
        if _numbers(issue["why"]) - source_numbers:
            issue["why"] = "I could not read or understand this part of the prescription"
    _fix_form(med, source_text)
    _check_against_line(med, source_text)
    for field, label in (
        ("strength", "strength"),
        ("amount_per_dose", "how much to take"),
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
            _flag(med, "unsure", label if label.startswith("how") else f"the {label}",
                  f'I read "{value}", but the number {", ".join(sorted(unknown))} is not on the prescription, so I removed it')

    # Times per day must agree with the listed moments of the day, when both are given.
    n, moments = med.get("times_per_day"), med.get("times_of_day")
    if n and moments and n != len(moments):
        _flag(med, "unsure", "how many times a day to take it",
              f"the prescription seems to say {n} times a day, but names {len(moments)} time(s) of the day")

    # Things the patient needs that the prescription does not give.
    _flag_high_risk(med, source_text)
    flagged = {i["what"] for i in med.get("issues", [])}
    if not med.get("amount_per_dose") and "how much to take" not in flagged:
        _flag(med, "unsure", "how much to take", "the amount to take each time is not written on the prescription")
    if not (med["only_when_needed"] or med.get("times_per_day") or med.get("times_of_day")
            or med.get("single_dose") or med.get("times_per_day_range")
            or med.get("every_hours") or med.get("times_per_week")):
        _flag(med, "unsure", "how often to take it", "how many times a day is not written on the prescription")

    med["_checked"] = True
    return med


def _safety_check(extraction: dict, source_text: str) -> dict:
    """Check the model's reading against the original prescription and record every doubt.

    Numbers the prescription does not contain are removed; missing doses and frequencies,
    automatic corrections and dropped inventions are all recorded as issues with a reason,
    so nothing is fixed or removed silently.
    """
    source_numbers = numbers_on_prescription(source_text)
    _drop_understood_unreadable(extraction, source_text)
    for issue in extraction["issues"] + [i for m in extraction["medicines"] for i in m.get("issues", [])]:
        if _numbers(issue["why"]) - source_numbers:  # e.g. "it could be 500 mg" when 500 is not written
            issue["why"] = "I could not read or understand this part of the prescription"
    for med in extraction["medicines"]:
        if not med.get("_checked"):
            check_medicine(med, source_text, source_numbers)
    invented = [m for m in extraction["medicines"] if m.get("not_on_prescription")]
    for med in invented:
        _flag(extraction, "risk", "medicine", f'the AI listed "{med["name_as_written"]}", which is not on the prescription, '
              "so I removed it: please check that no medicine is missing")
    extraction["medicines"] = [m for m in extraction["medicines"] if not m.get("not_on_prescription")]
    extraction["invented_medicines"] = len(invented)

    # Text copied from the example in the AI's instructions, when the prescription says nothing like it.
    for field, value, evidence in _EXAMPLE_COPIES:
        items = extraction[field]
        if any(_letters(i) == _letters(value) for i in items) and not re.search(evidence, source_text, re.IGNORECASE):
            extraction[field] = [i for i in items if _letters(i) != _letters(value)]
            _flag(extraction, "note", field.replace("_", " ").replace("conditions", "illness"),
                  f'removed "{value}": it is not on the prescription')

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
        if _ADVICE_RE.search(line):
            _flag(extraction, "note", "doctor's instruction", f'removed "{line}": general advice that is not on the prescription')
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


# What the example in _DOSING_SYSTEM_PROMPT says, and the words a prescription would need to say the same.
_EXAMPLE_COPIES = [
    ("conditions", "throat infection", r"angine|gorge|throat|pharyn|amygdal|tonsil"),
    ("doctor_instructions", "Rest.", r"\brepos|\brest\b|repose"),
]

_FOLLOW_UP_RE = re.compile(
    r"\b(revoir|revenir|contr[ôo]le|rendez[- ]vous|rdv|consultation|reconsulter|suivi|retour|"
    r"follow[- ]?up|see (me|the doctor|your doctor) again|come back|check[- ]?up|review|"
    r"dans \d+\s*(jours?|semaines?|mois)|in \d+\s*(days?|weeks?|months?))\b",
    re.IGNORECASE,
)
_FOOD_INSTRUCTION_RE = re.compile(r"(take (it |them )?)?(with|during|before|after) (food|meals?|eating)", re.IGNORECASE)
_ADVICE_RE = re.compile(
    r"recommended dose|do not exceed|as directed|consult (your|a|the) (doctor|pharmacist)|if (symptoms|signs|it) persists?|"
    r"keep out of reach|read the (leaflet|label)|ask your (doctor|pharmacist)",
    re.IGNORECASE,
)
_REVISIT_RE = re.compile(r"\b(revisit|return|come back|see (your|the) doctor|follow[- ]?up|check[- ]?up)\b", re.IGNORECASE)
_DOSE_IN_TEXT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(mg|g|ml|µg|mcg|tablets?|capsules?|cp|gél|sachets?|drops?|puffs?)\b", re.IGNORECASE)


def _read_dosing(text: str, retries: int = 1) -> dict:
    """Dosing call + safety check. Drug facts are not filled in yet."""
    source, removed = _medical_part(text)
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
            _safety_check(extraction, source)
            if extraction["invented_medicines"] and attempt < retries:
                logger.warning("The AI listed a medicine that is not on the prescription: reading again")
                continue
            return extraction
        reason = "I could not find any medicine or illness in this text"
        if extraction["issues"]:
            reason += " (" + "; ".join(f"{i['what']}: {i['why']}" for i in extraction["issues"]) + ")"
        logger.warning("Model found no medicines or illness (attempt %d)", attempt + 1)

    raise PipelineError(
        f"Sorry, I could not understand this prescription. Why: {reason}. "
        "Please check the text, or ask a doctor or pharmacist to explain it."
    )


def _medical_part(text: str) -> tuple[str, list[str]]:
    """The prescription without personal details (removed here, on this computer, before any AI call)."""
    if not text or not text.strip():
        raise PipelineError("Sorry, there is nothing to explain: the prescription is empty.")
    source, removed = keep_medical_only(text)
    if not source:
        raise PipelineError(
            "Sorry, I could not find any medical information in this text. Why: it only seems to contain "
            "personal details (" + ", ".join(removed) + "), which this tool does not need."
        )
    return source, removed


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


class _MedsInStream:
    """Picks each complete medicine out of the model's JSON while it is still being written.

    Feed it the pieces of text as they arrive; it returns the objects of the "meds" list that are
    finished (closing brace received). Braces inside strings are ignored.
    """

    def __init__(self):
        self.text, self.pos = "", 0
        self.in_list = self.done = self.in_str = self.escape = False
        self.depth, self.start = 0, None

    def feed(self, piece: str) -> list[dict]:
        self.text += piece
        found = []
        if not self.in_list:
            m = re.search(r'"(?:meds|medicines)"\s*:\s*\[', self.text)
            if not m:
                return found
            self.in_list, self.pos = True, m.end()
        while self.pos < len(self.text) and not self.done:
            ch = self.text[self.pos]
            if self.in_str:
                if self.escape:
                    self.escape = False
                elif ch == "\\":
                    self.escape = True
                elif ch == '"':
                    self.in_str = False
            elif ch == '"':
                self.in_str = True
            elif ch == "{":
                if self.depth == 0:
                    self.start = self.pos
                self.depth += 1
            elif ch == "}":
                self.depth -= 1
                if self.depth == 0 and self.start is not None:
                    try:
                        found.append(json.loads(self.text[self.start:self.pos + 1]))
                    except json.JSONDecodeError:
                        pass  # the full answer is checked again at the end
                    self.start = None
            elif ch == "]" and self.depth == 0:
                self.done = True
            self.pos += 1
        return found


def _ready_medicines(items, source, source_numbers, shown, started, cache, pool):
    """Check each finished medicine of the live answer and yield it to show, unless it was made up."""
    for item in items:
        med = _normalize_med(item)
        if not med:
            continue
        check_medicine(med, source, source_numbers)
        if med.get("not_on_prescription"):
            continue  # never shown; the whole reading is checked again at the end
        shown.append(med)
        yield {"type": "medicine_ready", "index": len(shown) - 1, "medicine": med}
        # Start looking up what it is for right away, while the model writes the next one.
        key = _drug_key(med)
        if not med.get("no_name") and key not in cache and key not in started:
            started[key] = pool.submit(_fetch_drug_purpose, med)


def stream_prescription(text: str):
    """Same as extract_prescription(), but yields results as soon as each part is ready.

    Like a game world loading block by block, each medicine is shown as soon as the model has
    finished writing it AND it has passed all its checks; nothing half-written or unchecked is shown.

    Yields, in order:
        {"type": "medicine_ready", "index": int, "medicine": dict}  # how to take one medicine, checked
        {"type": "dosing", "prescription": dict,                   # everything read: illness, instructions...
         "replaces_stream": bool}                                  # True: the streamed medicines changed, show these instead
        {"type": "medicine", "index": int, "medicine": dict}       # once per medicine, when its purpose arrives
        {"type": "done", "prescription": dict, "text": str,        # complete explanation for the patient
         "review": [str], "needs_review": bool}                    # for the doctor: risks, doubts and corrections
    """
    source, removed = _medical_part(text)
    source_numbers = numbers_on_prescription(source)
    shown: list[dict] = []
    started: dict = {}
    pool = ThreadPoolExecutor(max_workers=4)
    with _cache_lock:
        cache = _load_drug_cache()
    try:
        parser, raw = _MedsInStream(), ""
        try:
            pieces = _chat_stream(_DOSING_SYSTEM_PROMPT, f"Prescription:\n{source}", max_tokens=1200, json_mode=True)
            for piece in pieces:
                raw += piece
                yield from _ready_medicines(parser.feed(piece), source, source_numbers, shown, started, cache, pool)
        except PipelineError as exc:
            # The live answer broke off (e.g. the AI service timed out): read again the normal way, with retries.
            logger.warning("Streaming failed (%s): reading again without streaming", exc)
            raw = ""

        extraction = None
        try:
            extraction = _normalize_dosing(_parse_json_object(_THINK_RE.sub("", raw)))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse streamed JSON: %s", exc)
        if extraction and (extraction["medicines"] or extraction["conditions"]):
            # Keep the medicines already checked and shown; a medicine is the same if its name is.
            ready = {m["name_as_written"]: m for m in shown}
            extraction["medicines"] = [ready.get(m["name_as_written"], m) for m in extraction["medicines"]]
            extraction["privacy_removed"] = removed
            _safety_check(extraction, source)
            if extraction["invented_medicines"]:
                logger.warning("The AI listed a medicine that is not on the prescription: reading again")
                extraction = _read_dosing(text)
        else:
            extraction = _read_dosing(text)  # one more try the normal way
        replaced = [id(m) for m in extraction["medicines"]] != [id(m) for m in shown]
        yield {"type": "dosing", "prescription": extraction, "replaces_stream": replaced and bool(shown)}
        for i in _iter_drug_info(extraction, started):
            yield {"type": "medicine", "index": i, "medicine": extraction["medicines"][i]}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
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


_PILL_RE = re.compile(r"\b(tablet|capsule|pill|tab|cap|comprim[ée]|g[ée]lule)(s?)\b", re.IGNORECASE)


def short_name(med: dict) -> str:
    """The medicine's name without its strength: "Amoxicilline 1 g" -> "Amoxicilline",
    "Clamoxyl sirop 250mg/5ml" -> "Clamoxyl sirop", "Vitamine D3 1000 UI" -> "Vitamine D3"."""
    words = []
    for word in med["name_as_written"].split():
        if re.match(r"\d", word):
            break
        words.append(word)
    name = " ".join(words) or med["name_as_written"]
    return name[:1].upper() + name[1:]


def _pills(text: str) -> str:
    """Tablets and capsules are simply "pills" for the patient."""
    return _PILL_RE.sub(lambda m: "pill" + ("s" if m.group(2) or m.group(1).lower().endswith("s") else ""), text)


def _dose_with_name(amount: str, name: str) -> str:
    """"1 tablet" -> "1 Amoxicilline pill"; "2 puffs" -> "2 puffs of Ventoline"; "5 ml" -> "5 ml of Clamoxyl sirop"."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?|\d+\s*/\s*\d+|½|half|one|two)\s+(.+)$", amount, re.IGNORECASE)
    if not m:
        return f"{amount} of {name}"
    number, unit = m.group(1), m.group(2).strip()
    if _PILL_RE.fullmatch(unit) or _PILL_RE.match(unit):
        plural = number not in ("1", "one") and "/" not in number and number not in ("½", "half")
        return f"{number} {name} pill{'s' if plural else ''}"
    return f"{number} {unit} of {name}"


def _how_sentences(med: dict) -> list[str]:
    """How to take it, as short sentences: how much, when, food, how long, limits."""
    amount = med.get("amount_per_dose")
    first = f"Take {_dose_with_name(amount, short_name(med))}" if amount else "Take it"

    if med["only_when_needed"]:
        need = med.get("needed_for")
        first += f" only when you have {need}" if need else " only when you need it"
        if med.get("times_per_day"):
            first += f", up to {med['times_per_day']} times a day" if med["times_per_day"] > 1 else ", up to once a day"
    else:
        moments = med.get("times_of_day") or []
        n = med.get("times_per_day")
        if med.get("times_per_week"):  # weekly first: "in the morning" alone would read as every day
            w = med["times_per_week"]
            first += " once a week" if w == 1 else f" {w} times a week"
            if med.get("weekday"):
                first += f", on {med['weekday']}"
            if moments:
                first += ", " + _join_or([_TIME_WORDS[m] for m in moments], "and")
        elif moments:
            first += " " + _join_or([_TIME_WORDS[m] for m in moments], "and")
        elif med.get("single_dose"):
            first += " only once (one single dose)"
        elif med.get("times_per_day_range"):
            low, high = med["times_per_day_range"]
            first += f" {low} to {high} times a day"
        elif med.get("times_per_week"):
            w = med["times_per_week"]
            first += " once a week" if w == 1 else f" {w} times a week"
            if med.get("weekday"):
                first += f", on {med['weekday']}"
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
        sentences.append(f"Do not take more than {_pills(med['max_per_day'])} in one day.")
    if long_term:
        sentences.append("This is a long-term treatment.")
    return sentences


def _purpose_sentences(med: dict) -> list[str]:
    if med.get("purpose"):
        return [_sentence(f"{short_name(med)} is {_lower_first(med['purpose'])}")]
    if med.get("purpose_unknown"):
        return [f"I do not know what {short_name(med)} is for. Please ask your doctor or pharmacist."]
    return []


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
        body = _purpose_sentences(med) + (_sorry_sentences(med) if med.get("unknown_medicine") else [])
    else:
        body = _purpose_sentences(med) + _how_sentences(med) + _sorry_sentences(med)
    return [f"{number}. {med['name_as_written']}", *(f"   {s}" for s in body)]


def _daily_schedule(medicines: list[dict]) -> list[str]:
    """One line per moment of the day. Empty if any regular medicine has no known time."""
    regular = [m for m in medicines if not m["only_when_needed"]]
    if not regular or any(not m.get("times_of_day") or m.get("times_per_week") for m in regular):
        return []  # a weekly medicine in a "each day" schedule would read as daily

    slots: dict[str, list[str]] = {t: [] for t in _TIME_ORDER}
    as_needed: list[str] = []
    for med in medicines:
        label = med["name_as_written"]
        if med.get("amount_per_dose"):
            label += f" ({_pills(med['amount_per_dose'])})"
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
    return any(i["level"] in ("unsure", "risk") for i in _all_issues(extraction))


def _all_issues(extraction: dict) -> list[dict]:
    issues = [dict(i, source=med["name_as_written"]) for med in extraction["medicines"] for i in med.get("issues", [])]
    return issues + [dict(i, source="Prescription") for i in extraction.get("issues", [])]


def render_review(extraction: dict) -> list[str]:
    """For the doctor (not the patient): what was not understood, what was corrected, and why."""
    issues = _all_issues(extraction)
    unsure = [i for i in issues if i["level"] == "unsure"]
    notes = [i for i in issues if i["level"] == "note"]
    risks = [i for i in issues if i["level"] == "risk"]

    out = ["For the doctor: please review"]
    if removed := extraction.get("privacy_removed"):
        out.append(f"   PRIVACY    Removed before sending to the AI: {', '.join(removed)}. This tool only needs the medical part.")
    if risks:
        out.append("   CHECK BEFORE GIVING THIS TO THE PATIENT: it contains a high-risk medicine.")
    if unsure:
        out.append(f"   CHECK BEFORE GIVING THIS TO THE PATIENT: I am not sure about {len(unsure)} thing{'s' if len(unsure) != 1 else ''}.")
    elif not risks:
        out.append("   Everything on the prescription was understood.")
    for i in risks:
        out.append(f"   HIGH RISK  {i['source']}: {i['what']} ({_lower_first(i['why'])}).")
    for i in unsure:
        out.append(f"   NOT SURE   {i['source']}: {i['what']} ({_lower_first(i['why'])}).")
    for i in notes:
        out.append(f"   CORRECTED  {i['source']}: {i['what']} ({_lower_first(i['why'])}).")
    if any(m.get("purpose") for m in extraction["medicines"]):
        out.append('   AI-ADDED   The lines saying what each medicine is for are not on the prescription: please check them.')
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
