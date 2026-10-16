from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from clearcare_app.agent import Agent
from clearcare_app.store import Store

ROOT = Path(__file__).resolve().parent.parent


def button(app, label):
    return next(b for b in app.button if b.label == label)


def text(app, label):
    return next(t for t in app.text_input if t.label == label)


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARCARE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLEARCARE_APP_PASSWORD", raising=False)
    # Prevent an unrelated local .env from affecting this isolated test.
    monkeypatch.setattr("clearcare_app.ui.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr("clearcare_app.ui.st.get_option", lambda key: "127.0.0.1")
    return tmp_path


def test_patient_consultation_generation_approval_and_history(app_env, monkeypatch):
    calls = []
    def generate(self, source, language, on_event=None):
        calls.append((source, language))
        return {"text": "Your doctor says you have a throat infection.", "structured": {},
                "report": {"review": ["Original agent review"]}, "needs_review": False}
    monkeypatch.setattr(Agent, "generate", generate)
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    assert not app.exception
    assert not app.sidebar.text_input
    button(app, "Add patient").click().run()
    text(app, "Full name").set_value("Sam Taylor")
    app.date_input[0].set_value(date(1985, 5, 12))
    text(app, "Email (optional)").set_value("sam@example.com")
    # AppTest reruns the full script for dialog submissions; keep its opener active.
    button(app, "Add patient").click()
    button(app, "Save patient").click().run()
    assert app.title[0].value == "Sam Taylor"
    button(app, "New consultation").click().run()
    text(app, "Consultation title").set_value("Throat infection").run()
    next(t for t in app.text_area if t.label == "Condition, prescription and advice").set_value("Diagnostic: Angine.").run()
    assert calls == []
    button(app, "Create explanation").click().run()
    assert len(calls) == 1 and not app.exception
    app.run()
    assert len(calls) == 1  # ordinary reruns must not create paid model calls
    button(app, "← Patient history").click().run()
    button(app, "Open").click().run()
    assert next(t for t in app.text_area if t.label == "Condition, prescription and advice").value == "Diagnostic: Angine."
    next(t for t in app.text_area if t.label == "Patient wording").set_value("Doctor-edited explanation.")
    button(app, "Save as a new draft").click().run()
    assert len(calls) == 1
    next(c for c in app.checkbox if c.label.startswith("I reviewed this exact")).check().run()
    next(c for c in app.checkbox if c.label.startswith("I checked the flagged")).check().run()
    assert button(app, "Approve & give to patient").disabled
    text(app, "Reviewing doctor").set_value("Dr Morgan").run()
    button(app, "Approve & give to patient").click().run()
    assert not app.exception
    store = Store(app_env / "clearcare.sqlite3")
    pid = store.patients()[0]["id"]
    cid = store.consultations(pid)[0]["id"]
    version = store.version(store.versions(cid)[0]["id"])
    assert version["status"] == "approved"
    assert version["text"] == "Doctor-edited explanation."
    assert len(store.versions(cid)) == 2
    assert store.version(store.versions(cid)[1]["id"])["text"] == "Your doctor says you have a throat infection."
    # Start a fresh session: patient and consultation records survive.
    fresh = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    button(fresh, "Open patient").click().run()
    assert "Throat infection" in " ".join(m.value for m in fresh.markdown)


def test_share_view_does_not_render_clinician_navigation(app_env):
    store = Store(app_env / "clearcare.sqlite3")
    pid = store.save_patient("Private Patient", "1985-05-12", "", "en")
    cid = store.new_consultation(pid)
    vid = store.add_version(cid, 0, "en", "Only this approved explanation", {}, {})
    store.approve(vid, "Dr Morgan")
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20)
    app.query_params["share"] = store.share(vid)
    app.run()
    assert not app.exception
    assert not app.text_input and not app.sidebar.button
    assert "Only this approved explanation" in " ".join(m.value for m in app.markdown)
    assert "Private Patient" not in " ".join(m.value for m in app.markdown)


def test_password_gate_precedes_patient_records(app_env, monkeypatch):
    monkeypatch.setenv("CLEARCARE_APP_PASSWORD", "test-workspace-password")
    app = AppTest.from_file(str(ROOT / "streamlit_app.py")).run()
    assert text(app, "Workspace password")
    assert not any(b.label == "Add patient" for b in app.button)
    text(app, "Workspace password").set_value("wrong")
    button(app, "Sign in").click().run()
    assert app.error[0].value == "The password is incorrect."
    text(app, "Workspace password").set_value("test-workspace-password")
    button(app, "Sign in").click().run()
    assert any(b.label == "Add patient" for b in app.button)
