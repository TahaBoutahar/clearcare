"""
worker.py — the translation step for the dashboard, kept running between requests.

    .venv/bin/python worker.py

Reads one JSON request per line on stdin and writes one JSON answer per line on stdout:
    in:  {"prescription": str, "extraction": dict, "english": str, "lang": "ary" | "fr" | ..., "judge": bool}
    out: {"language", "language_name", "native", "dir", "text", "arabizi" (Darija only), "model",
          "is_right_language", "kept_in_english", "sentences_translated", "rating"}  or  {"error": str}
    in:  {"languages": true}  ->  {"languages": [{"code", "name", "native", "dir"}, ...]}
The prescription is cleaned of personal details again here before the judge sees it.
"""

from __future__ import annotations

import json
import sys

import translation_pipeline as tp  # sets up the project path and the pipeline
import judge
from languages import LANGUAGES


def handle(req: dict) -> dict:
    if req.get("languages"):
        return {"languages": [{"code": c, "name": L["name"], "native": L["native"], "dir": L["dir"]}
                              for c, L in LANGUAGES.items()]}
    lang = req.get("lang") or "ary"
    out = tp.to_language(req["extraction"], lang)
    if lang == "ary":
        out["arabizi"] = tp.to_arabizi(out["text"])
    if req.get("judge", True):
        cleaned, _ = tp.keep_medical_only(req["prescription"])
        out["rating"] = judge.rate(cleaned, req["english"], out["text"], out["language_name"])
    return out


def main() -> None:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # library prints must not break the JSON lines
    print(json.dumps({"ready": True}), file=real_stdout, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            answer = handle(json.loads(line))
        except Exception as exc:  # reported to the dashboard, the worker keeps running
            answer = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(answer, ensure_ascii=False, default=str), file=real_stdout, flush=True)


if __name__ == "__main__":
    main()
