"""Thin application adapter around the repository's unmodified AI functions."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.RLock()


@lru_cache(maxsize=1)
def languages():
    # Loading the templates needs no model, network, torch or credentials.
    spec = importlib.util.spec_from_file_location("clearcare_language_templates", ROOT / "translation-pipeline/languages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {"en": {"name": "English", "native": "English", "dir": "ltr"}, **module.LANGUAGES}


@lru_cache(maxsize=1)
def _modules():
    for directory in (ROOT, ROOT / "translation-pipeline"):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    # doctor_cli loads the existing .env before the pipeline reads its configuration.
    cli = importlib.import_module("doctor_cli")
    return cli.pipeline, importlib.import_module("translation_pipeline"), importlib.import_module("judge")


class Agent:
    def generate(self, source, language="en", on_event=None):
        if language not in languages():
            raise ValueError("Choose a supported language.")
        if not source.strip():
            raise ValueError("Enter the doctor's notes first.")
        # The current agent has process-wide settings and caches. Do not switch them per user.
        with _LOCK:
            pipeline, translation, judge = _modules()
            done = None
            for event in pipeline.stream_prescription(source):
                if on_event:
                    on_event(event)
                if event["type"] == "done":
                    done = event
            if not done:
                raise RuntimeError("The agent did not complete. The input is saved; please retry.")
            report = {"review": done["review"], "english": done["text"]}
            text, needs_review = done["text"], done["needs_review"]
            if language != "en":
                translated = translation.to_language(done["prescription"], language)
                cleaned, _ = translation.keep_medical_only(source)
                rating = judge.rate(cleaned, done["text"], translated["text"], translated["language_name"])
                text = translated["text"]
                report.update(translation=translated, rating=rating)
                needs_review = (needs_review or bool(translated["kept_in_english"])
                                or not translated["is_right_language"] or not rating["passed"])
            return {"text": text, "structured": done["prescription"], "report": report, "needs_review": needs_review}

    def transcribe(self, data: bytes):
        with _LOCK, tempfile.TemporaryDirectory(prefix="clearcare-recording-") as directory:
            audio = Path(directory) / "recording.wav"
            audio.write_bytes(data)
            pipeline, _, _ = _modules()
            return pipeline.transcribe_voice(str(audio))

    def audio(self, text, language):
        # The existing function accepts language NAMES, not the app's language codes.
        names = {"en": "english", "fr": "french", "ar": "arabic", "es": "spanish", "pt": "portuguese",
                 "it": "italian", "de": "german", "nl": "dutch", "tr": "turkish", "ary": "arabic"}
        if language not in names:
            raise ValueError("Choose a supported speech language.")
        with _LOCK, tempfile.TemporaryDirectory(prefix="clearcare-speech-") as directory:
            pipeline, _, _ = _modules()
            path = pipeline.text_to_audio(text, names[language], str(Path(directory) / "explanation.mp3"))
            return Path(path).read_bytes()
