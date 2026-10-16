import hashlib
from datetime import date, timedelta

import pytest

from clearcare_app.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "private" / "records.sqlite3")


def patient(store, name="Sam Taylor"):
    return store.save_patient(name, "1985-05-12", "sam@example.com", "en")


def draft(store, patient_id=None, needs_review=False, text="A plain explanation."):
    cid = store.new_consultation(patient_id or patient(store), "Follow-up")
    c = store.save_source(cid, "Doctor's original note.", "Follow-up", 0)
    vid = store.add_version(cid, c["revision"], "en", text, {}, {"review": []}, needs_review)
    return cid, vid


def test_same_names_have_distinct_records_and_isolated_history(store):
    first, second = patient(store), patient(store)
    consultation, _ = draft(store, first)
    assert len(store.patients("Sam Taylor")) == 2
    assert store.patient(first)["reference"] != store.patient(second)["reference"]
    assert store.consultations(first)[0]["id"] == consultation
    assert store.consultations(second) == []


def test_persistence_survives_reopening(store):
    pid = patient(store)
    draft(store, pid)
    reopened = Store(store.path)
    assert reopened.patient(pid)["name"] == "Sam Taylor"
    assert len(reopened.consultations(pid)) == 1


def test_search_is_literal_and_parameterized(store):
    patient(store)
    assert len(store.patients("sam")) == 1
    assert store.patients("' OR 1=1 --") == []
    assert store.patients("%") == []


@pytest.mark.parametrize("name,born,email", [
    ("", "1985-05-12", ""), ("Sam", "not a date", ""),
    ("Sam", (date.today() + timedelta(days=1)).isoformat(), ""),
    ("Sam", "1985-05-12", "bad@example.com\nBcc: other@example.com"),
])
def test_invalid_patient_fields(store, name, born, email):
    with pytest.raises(ValueError):
        store.save_patient(name, born, email, "en")


def test_stale_input_cannot_overwrite_newer_notes(store):
    cid = store.new_consultation(patient(store))
    store.save_source(cid, "First session's edit", "Visit", 0)
    with pytest.raises(Conflict):
        store.save_source(cid, "Stale edit", "Visit", 0)
    assert store.consultation(cid)["source"] == "First session's edit"


def test_generation_cannot_save_against_changed_input(store):
    cid, _ = draft(store)
    c = store.consultation(cid)
    store.save_source(cid, "Updated source", "Follow-up", c["revision"])
    with pytest.raises(Conflict):
        store.add_version(cid, c["revision"], "en", "Old result", {}, {})


def test_draft_exports_and_links_require_approval(store):
    _, vid = draft(store)
    for action in (lambda: store.approved(vid), lambda: store.share(vid),
                   lambda: store.put_artifact(vid, "audio", b"data"), lambda: store.delivery(vid, "email")):
        with pytest.raises(ValueError):
            action()


def test_approval_requires_explicit_uncertainty_acknowledgment(store):
    _, vid = draft(store, needs_review=True)
    with pytest.raises(ValueError):
        store.approve(vid, "Dr Morgan")
    store.approve(vid, "Dr Morgan", acknowledge=True)
    assert store.approved(vid)["reviewer"] == "Dr Morgan"


def test_approval_rejects_stale_source_and_missing_reviewer(store):
    cid, vid = draft(store)
    with pytest.raises(ValueError):
        store.approve(vid, "")
    c = store.consultation(cid)
    store.save_source(cid, "New source", c["title"], c["revision"])
    with pytest.raises(Conflict):
        store.approve(vid, "Dr Morgan")


def test_edited_versions_preserve_approved_snapshot_and_language(store):
    cid, vid = draft(store)
    store.approve(vid, "Dr Morgan")
    original = store.version(vid)
    edited = store.add_version(cid, original["source_revision"], "en", "Doctor's corrected wording", {}, {},
                               True, "doctor_edit", vid)
    assert store.version(edited)["status"] == "draft"
    assert store.approved(vid)["text"] == "A plain explanation."
    assert [v["number"] for v in store.versions(cid)] == [2, 1]
    assert store.versions(cid, "fr") == []
    with pytest.raises(Conflict):
        store.reject(vid)


def test_share_is_hashed_scoped_expiring_and_revocable(store):
    _, vid = draft(store)
    store.approve(vid, "Dr Morgan")
    token = store.share(vid)
    assert store.shared_version(token)["id"] == vid
    assert store.shared_version("invalid") is None
    with store.connection() as db:
        row = db.execute("SELECT token_hash FROM shares").fetchone()
        assert row[0] == hashlib.sha256(token.encode()).hexdigest() and row[0] != token
    store.revoke_shares(vid)
    assert store.shared_version(token) is None
    expired = store.share(vid)
    with store.connection(write=True) as db:
        db.execute("UPDATE shares SET expires_at='2000-01-01T00:00:00+00:00'")
    assert store.shared_version(expired) is None


def test_artifacts_belong_to_exact_version(store):
    cid, vid = draft(store)
    store.approve(vid, "Dr Morgan")
    store.put_artifact(vid, "audio", b"first-version-audio")
    v = store.version(vid)
    new = store.add_version(cid, v["source_revision"], "fr", "Version française", {}, {})
    store.approve(new, "Dr Morgan")
    assert store.artifact(new, "audio") is None
    assert store.artifact(vid, "audio") == b"first-version-audio"
