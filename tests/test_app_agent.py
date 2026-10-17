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
    judged = []
    def rate(*args):
        judged.append(args)
        return {"passed": True, "score": 5}
    translation = SimpleNamespace(keep_medical_only=lambda source: ("clean source", []))
    monkeypatch.setattr(agent, "_modules", lambda: (SimpleNamespace(stream_prescription=stream), translation,
                                                     SimpleNamespace(rate=rate)))
    events = []
    result = agent.Agent().generate("Doctor source", on_event=events.append)
    assert calls == ["Doctor source"]
    assert result["text"] == done["text"] and result["structured"] == done["prescription"]
    assert len(events) == 2
    # The second model also checks English sheets.
    assert judged == [("clean source", "Original output")] and result["report"]["rating"]["score"] == 5


def test_english_sheet_failing_the_second_model_needs_review(monkeypatch):
    done = {"type": "done", "text": "English output", "review": [], "prescription": {}, "needs_review": False}
    monkeypatch.setattr(agent, "_modules", lambda: (
        SimpleNamespace(stream_prescription=lambda s: iter([done])),
        SimpleNamespace(keep_medical_only=lambda source: ("clean", [])),
        SimpleNamespace(rate=lambda *a: {"passed": False, "score": 2})))
    assert agent.Agent().generate("source")["needs_review"]


def test_agent_refusal_keeps_its_reason(monkeypatch):
    class PipelineError(Exception):
        pass
    def stream(source):
        raise PipelineError("Sorry, I could not understand this prescription. Why: it is a greeting.")
        yield
    monkeypatch.setattr(agent, "_modules", lambda: (
        SimpleNamespace(stream_prescription=stream, PipelineError=PipelineError), None, None))
    with pytest.raises(agent.AgentError, match="Why: it is a greeting"):
        agent.Agent().generate("Bonjour")


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
    assert agent.Agent().audio("نص بالدارجة", "ary") == b"mp3"
    assert calls[-1] == ("نص بالدارجة", "arabic")
    with pytest.raises(ValueError):
        agent.Agent().audio("Unsupported", "unknown")


def test_language_metadata_loads_without_agent_credentials():
    assert "ary" in agent.languages() and agent.languages()["ar"]["dir"] == "rtl"


def test_transcription_passes_the_spoken_language(monkeypatch):
    calls = []
    def transcribe_voice(path, language=None):
        calls.append((Path(path).read_bytes(), language))
        return "Amoxicilline 1 g"
    monkeypatch.setattr(agent, "_modules", lambda: (SimpleNamespace(transcribe_voice=transcribe_voice), None, None))
    assert agent.Agent().transcribe(b"wav", "fr") == "Amoxicilline 1 g"
    assert calls == [(b"wav", "fr")]
