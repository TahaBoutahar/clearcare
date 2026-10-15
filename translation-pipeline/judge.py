"""
judge.py — a second model that checks the final outputs and rates them from 0 to 5.

It always runs at the end, reads the original prescription (personal details already removed),
the English explanation and, if there is one, the Darija version, and returns a score with
reasons. It never changes the text: it only rates, and a low score asks the doctor to look.

Which model judges (set in the project's .env or the environment):
    JUDGE_PROVIDER   amd | ollama | openrouter   (default: same provider as the pipeline)
    JUDGE_MODEL      model name                  (default: that provider's default model)
A judge should ideally be a different model from the one that wrote the text.
"""

from __future__ import annotations

import json
import os
import re

import ai_pipeline as pipeline
from openai import OpenAI

PASS_SCORE = 4  # below this, the doctor is asked to look closely

_PROMPT = """You are the strictest clinical pharmacist checking an explanation written for a patient.
A wrong explanation can hurt someone, so you look for ANY mistake and you never give the benefit of the doubt.
You get: the ORIGINAL prescription, the ENGLISH explanation, and sometimes a TRANSLATION of it for the patient
(the language is named).

Compare the explanation with the prescription, detail by detail, and score each criterion from 0 to 5:
- faithful: every amount, number, time of day, frequency, food rule, duration, maximum, "only when needed"
  and way of use matches the prescription EXACTLY. One single wrong or changed detail -> 0.
  A detail that is present but vague or partly wrong -> at most 2.
- complete: every medicine, dose, time, duration and doctor's instruction of the prescription is in the
  explanation. One missing medicine -> 0. Any other missing detail -> at most 2.
- no_added_advice: the explanation only restates the prescription. Any sentence of advice, warning or
  diagnosis that the doctor did not write -> at most 1 ("what the medicine is for" is allowed).
- clear: short, simple, everyday sentences. Any confusing, repeated or broken sentence -> at most 2.
- translation: (only if there is a translation) it says EXACTLY the same as the English: every
  number, time, frequency and duration. Anything dropped, added, changed, untranslated or garbled -> 0.

For EVERY criterion below 5 you must:
  - say WHY in one short sentence, and
  - give "quote": the exact words copied from the explanation or the translation that are wrong
    (or, for something missing, the exact words copied from the prescription that are not explained).
Only report mistakes you can quote. Do not invent problems.

Judge the EXPLANATION, not the prescription: if the prescription itself does not give some information
and the explanation honestly says it is not written, that is correct.

French prescription words (read them correctly before you judge):
  "au milieu des repas", "au cours des repas", "pendant le repas" = DURING the meal = "with food";
  "avant les repas" = before eating; "après les repas" = after eating; "entre les repas" = between meals;
  "à jeun" = on an empty stomach; "si ...", "en cas de ..." = ONLY when needed (for that reason);
  "à sucer" = suck it (that is the way of use, not advice); "cp" = tablet; "j" = day.
These lines are part of the tool itself and are NOT added advice: "If you are not sure about anything,
ask your doctor or pharmacist." and the "Sorry, I am not sure about ... Please ask your doctor or pharmacist." lines.

The overall score is the LOWEST of all criterion scores.

Return compact JSON only:
{"faithful": {"score": n, "why": "...", "quote": "..."}, "complete": {"score": n, "why": "...", "quote": "..."},
 "no_added_advice": {"score": n, "why": "...", "quote": "..."}, "clear": {"score": n, "why": "...", "quote": "..."},
 "translation": {"score": n, "why": "...", "quote": "..."}, "score": n, "verdict": "one short sentence"}"""

CRITERIA = ["faithful", "complete", "no_added_advice", "clear", "translation"]


def _client() -> tuple[OpenAI, str, str]:
    provider = (os.getenv("JUDGE_PROVIDER") or pipeline.LLM_PROVIDER).strip().lower()
    if provider not in pipeline._PROVIDERS:
        raise ValueError(f"Unknown JUDGE_PROVIDER '{provider}'")
    base_url, key_env, default_model = pipeline._PROVIDERS[provider]
    model = os.getenv("JUDGE_MODEL") or default_model
    key = "not-needed" if key_env is None else os.getenv(key_env)
    if not key:
        raise ValueError(f"{key_env} is not set for the judge")
    return OpenAI(api_key=key, base_url=base_url, max_retries=6, timeout=120), provider, model


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'«»“”.,;:!?()\-]", " ", (text or "").lower())).strip()


def _score(value) -> int | None:
    try:
        return max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def rate(prescription: str, english: str, darija: str | None = None, language: str | None = None) -> dict:
    """{"score": 0-5 or None, "criteria": {...}, "problems": [...], "verdict", "judge", "passed"}"""
    try:
        client, provider, model = _client()
        if pipeline.budget_left(provider) == 0:
            raise RuntimeError(f"today's {provider.upper()} budget is used up")
        message = f"ORIGINAL PRESCRIPTION:\n{prescription}\n\nENGLISH EXPLANATION:\n{english}"
        if darija:
            message += f"\n\nTRANSLATION ({language or 'Moroccan Darija'}):\n{darija}"
        response = client.chat.completions.create(
            model=model, temperature=0, max_tokens=600, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _PROMPT}, {"role": "user", "content": message}],
        )
        if pipeline._PROVIDERS[provider][1] is not None:  # count paid calls against the daily budget
            pipeline.record_usage(provider, getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
        data = pipeline._parse_json_object(response.choices[0].message.content or "")
    except Exception as exc:  # the judge failing must never block the result; it is reported instead
        return {"score": None, "criteria": {}, "problems": [], "verdict": f"The judge could not rate this: {exc}",
                "judge": os.getenv("JUDGE_MODEL") or "?", "passed": False}

    criteria, reasons, quotes, unverified = {}, {}, {}, []
    sources = _normalise(" ".join(filter(None, [prescription, english, darija])))
    for c in CRITERIA:
        item = data.get(c)
        if isinstance(item, dict):  # {"score": n, "why": "...", "quote": "..."}
            criteria[c], reasons[c] = _score(item.get("score")), pipeline._as_str(item.get("why")) or ""
            quotes[c] = pipeline._as_str(item.get("quote")) or ""
        else:                       # a bare number (older answer shape)
            criteria[c], reasons[c], quotes[c] = _score(item), "", ""
        # A deduction must quote words that really exist; otherwise the reason is made up.
        if criteria[c] is not None and criteria[c] < 5 and (c != "translation" or darija):
            if not quotes[c] or _normalise(quotes[c]) not in sources:
                unverified.append(c)
    problems = pipeline._as_list(data.get("problems")) + [f"{c}: {reasons[c]}" for c in CRITERIA
                                                          if reasons.get(c) and criteria.get(c) is not None and criteria[c] < 4]
    if not darija:  # nothing to judge: not a 0, and no complaint about a "missing" translation
        criteria["translation"], reasons["translation"] = None, ""
        problems = [p for p in problems if not re.search(r"darija|translat", p, re.IGNORECASE)]
    # Harsh: the overall score is the lowest criterion, whatever the judge wrote as overall.
    given = [v for v in criteria.values() if v is not None]
    score = _score(data.get("score"))
    if given:
        score = min(given + ([score] if score is not None else []))
    return {
        "score": score, "criteria": criteria, "reasons": reasons, "quotes": quotes, "unverified": unverified,
        "problems": problems,
        "verdict": pipeline._as_str(data.get("verdict")) or "",
        "judge": f"{provider} · {model}", "same_model_as_writer": model == pipeline.LLM_MODEL,
        "passed": score is not None and score >= PASS_SCORE,
    }


def format_rating(r: dict) -> list[str]:
    stars = "★" * (r["score"] or 0) + "☆" * (5 - (r["score"] or 0)) if r["score"] is not None else "—"
    out = [f"Rating: {r['score'] if r['score'] is not None else '?'}/5  {stars}   (judge: {r['judge']})"]
    if r.get("same_model_as_writer"):
        out.append("  Note: the judge is the same model that wrote the text; a different model is a stronger check.")
    for c, v in r["criteria"].items():
        if v is not None:
            why = (r.get("reasons") or {}).get(c) or "(no reason given)"
            quote = (r.get("quotes") or {}).get(c)
            flag = "  [UNVERIFIED: the quoted words are not in the text]" if c in (r.get("unverified") or []) else ""
            out.append(f"  {c.replace('_', ' '):<16} {v}/5  — {why}" + (f'  «{quote}»' if quote and v < 5 else "") + flag)
    if r["verdict"]:
        out.append(f"  Overall: {r['verdict']}")
    if not r["passed"]:
        out.append(f"  CHECK BEFORE GIVING THIS TO THE PATIENT: rated below {PASS_SCORE}/5.")
    return out
