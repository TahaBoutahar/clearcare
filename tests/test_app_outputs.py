import pytest

from clearcare_app.outputs import patient_url, pdf_bytes, send_email
from clearcare_app.store import Store


@pytest.fixture
def approved(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    pid = store.save_patient("Sam Taylor", "1985-05-12", "sam@example.com", "en")
    cid = store.new_consultation(pid)
    vid = store.add_version(cid, 0, "en", "Your doctor's explanation.\n\nVotre médecin. شرح باللغة العربية.", {}, {})
    store.approve(vid, "Dr Morgan")
    return store, vid


def test_unicode_pdf_exports_and_is_cached(approved):
    store, vid = approved
    data = pdf_bytes(store, vid)
    assert data.startswith(b"%PDF-") and len(data) > 2000
    assert store.artifact(vid, "pdf") == data


def test_draft_cannot_be_exported(approved):
    store, vid = approved
    v = store.version(vid)
    draft = store.add_version(v["consultation_id"], 0, "en", "Unreviewed", {}, {})
    with pytest.raises(ValueError):
        pdf_bytes(store, draft)


def test_patient_url_rejects_unsafe_and_non_base_urls(monkeypatch):
    for url in ("javascript:alert(1)", "http://clinic.example.com", "https://app.example.com/?patient=1"):
        monkeypatch.setenv("CLEARCARE_PUBLIC_URL", url)
        with pytest.raises(ValueError):
            patient_url("token")
    monkeypatch.setenv("CLEARCARE_PUBLIC_URL", "https://app.example.com/clearcare")
    assert patient_url("token") == "https://app.example.com/clearcare/?share=token"


def test_email_sends_exact_approved_text_and_logs_success(monkeypatch, approved):
    store, vid = approved
    messages = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, **kwargs): pass
        def send_message(self, message):
            messages.append(message)
            return {}
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    monkeypatch.setattr("clearcare_app.outputs.smtplib.SMTP", SMTP)
    send_email(store, vid, "sam@example.com")
    assert store.approved(vid)["text"] in messages[0].get_body(preferencelist=("plain",)).get_content()
    assert len(list(messages[0].iter_attachments())) == 1
    assert store.deliveries(vid)[0]["status"] == "sent"


def test_failed_email_is_not_logged_as_sent(monkeypatch, approved):
    store, vid = approved
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    def failed(*args, **kwargs): raise OSError("failed")
    monkeypatch.setattr("clearcare_app.outputs.smtplib.SMTP", failed)
    with pytest.raises(RuntimeError):
        send_email(store, vid, "sam@example.com")
    assert store.deliveries(vid)[0]["status"] == "failed"
