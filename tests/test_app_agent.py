from pathlib import Path
from types import SimpleNamespace

import pytest

from clearcare_app import agent


def test_generation_uses_existing_stream_without_changing_provider(monkeypatch):
    calls = []
    done = {"type": "done", "text": "Original output", "review": ["Original review"],
            "prescription": {"medicines": []}, "needs_review": False}
    def stream(source):
        calls.append(source)
        yield {"type": "dosing", "prescription": {}}
        yield done
    monkeypatch.setattr(agent, "_modules", lambda: (SimpleNamespace(stream_prescription=stream), None, None))
    events = []
    result = agent.Agent().generate("Doctor source", on_event=events.append)
    assert calls == ["Doctor source"]
    assert result["text"] == done["text"] and result["structured"] == done["prescription"]
    assert len(events) == 2


def test_incomplete_stream_is_not_treated_as_saved_output(monkeypatch):
    monkeypatch.setattr(agent, "_modules", lambda: (SimpleNamespace(stream_prescription=lambda s: iter([])), None, None))
    with pytest.raises(RuntimeError):
        agent.Agent().generate("source")


def test_translation_uses_existing_checks_and_reports_fallback(monkeypatch):
    done = {"type": "done", "text": "English output", "review": [], "prescription": {}, "needs_review": False}
    translation = SimpleNamespace(
        to_language=lambda extraction, code: {"text": "Texte français", "language_name": "French",
                    "kept_in_english": [{"why": "not translated"}], "is_right_language": True},
        keep_medical_only=lambda source: ("clean source", []))
    judged = []
    def rate(*args):
        judged.append(args)
        return {"passed": True, "score": 5}
    monkeypatch.setattr(agent, "_modules", lambda: (
        SimpleNamespace(stream_prescription=lambda s: iter([done])), translation, SimpleNamespace(rate=rate)))
    result = agent.Agent().generate("source", "fr")
    assert result["text"] == "Texte français" and result["needs_review"]
    assert judged == [("clean source", "English output", "Texte français", "French")]


def test_audio_maps_codes_and_never_silently_uses_english(monkeypatch):
    calls = []
    def speak(text, language, path):
        calls.append((text, language))
        Path(path).write_bytes(b"mp3")
        return path
    monkeypatch.setattr(agent, "_modules", lambda: (SimpleNamespace(text_to_audio=speak), None, None))
    assert agent.Agent().audio("Bonjour", "fr") == b"mp3"
    assert calls == [("Bonjour", "french")]
    with pytest.raises(ValueError, match="explanation model is not connected"):
        agent.Agent().audio("نص بالدارجة", "ary")
    assert calls == [("Bonjour", "french")]
    with pytest.raises(ValueError):
        agent.Agent().audio("Unsupported", "unknown")


def test_language_metadata_loads_without_agent_credentials():
    assert "ary" in agent.languages() and agent.languages()["ar"]["dir"] == "rtl"
