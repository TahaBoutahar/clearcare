"""Local persistence with immutable explanation versions and optimistic locking."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


class Conflict(ValueError):
    """A different session changed the record, or its approval is no longer valid."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_patient(name: str, birth_date: str, email: str = "") -> None:
    import re
    if not name.strip() or len(name.strip()) > 200:
        raise ValueError("Enter a patient name of up to 200 characters.")
    try:
        born = date.fromisoformat(birth_date)
    except (ValueError, TypeError):
        raise ValueError("Enter a valid date of birth.") from None
    if born > date.today():
        raise ValueError("The date of birth cannot be in the future.")
    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        raise ValueError("Enter a valid email address, or leave it blank.")


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
 id TEXT PRIMARY KEY, reference TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 birth_date TEXT NOT NULL, email TEXT NOT NULL, language TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consultations (
 id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id),
 title TEXT NOT NULL, visit_date TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consultation_patient ON consultations(patient_id, visit_date);
CREATE TABLE IF NOT EXISTS versions (
 id TEXT PRIMARY KEY, consultation_id TEXT NOT NULL REFERENCES consultations(id),
 number INTEGER NOT NULL, source_revision INTEGER NOT NULL, source TEXT NOT NULL,
 title TEXT NOT NULL, language TEXT NOT NULL, text TEXT NOT NULL,
 structured TEXT NOT NULL, report TEXT NOT NULL, needs_review INTEGER NOT NULL,
 origin TEXT NOT NULL, parent_id TEXT REFERENCES versions(id), status TEXT NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','approved','rejected')), reviewer TEXT, approved_at TEXT,
 created_at TEXT NOT NULL, UNIQUE(consultation_id, number)
);
CREATE TABLE IF NOT EXISTS artifacts (
 version_id TEXT NOT NULL REFERENCES versions(id), kind TEXT NOT NULL,
 data BLOB NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(version_id, kind)
);
CREATE TABLE IF NOT EXISTS shares (
 token_hash TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
 expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id), channel TEXT NOT NULL,
 recipient TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _one(db, table, key):
        # Table names are application constants, never user input.
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("This record no longer exists.")
        result = dict(row)
        if table == "versions":
            result["structured"] = json.loads(result["structured"])
            result["report"] = json.loads(result["report"])
            result["needs_review"] = bool(result["needs_review"])
        return result

    def patient(self, key):
        with self.connection() as db:
            return self._one(db, "patients", key)

    def patients(self, search=""):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""
                SELECT p.*, COUNT(c.id) AS consultation_count, MAX(c.visit_date) AS last_visit
                FROM patients p LEFT JOIN consultations c ON c.patient_id=p.id
                WHERE instr(lower(p.name), lower(?))>0 OR instr(lower(p.reference), lower(?))>0
                GROUP BY p.id ORDER BY p.name COLLATE NOCASE, p.birth_date
                """, (search.strip(), search.strip()))]

    def save_patient(self, name, birth_date, email, language, patient_id=None):
        name, email = name.strip(), email.strip()
        validate_patient(name, birth_date, email)
        stamp = now()
        with self.connection(write=True) as db:
            if patient_id:
                self._one(db, "patients", patient_id)
                db.execute("UPDATE patients SET name=?,birth_date=?,email=?,language=?,updated_at=? WHERE id=?",
                           (name, birth_date, email, language, stamp, patient_id))
            else:
                patient_id = uuid.uuid4().hex
                db.execute("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?)",
                           (patient_id, "CC-" + patient_id[:10].upper(), name, birth_date, email, language, stamp, stamp))
        return patient_id

    def new_consultation(self, patient_id, title="New consultation", visit_date=None):
        key, stamp = uuid.uuid4().hex, now()
        with self.connection(write=True) as db:
            self._one(db, "patients", patient_id)
            db.execute("INSERT INTO consultations VALUES (?,?,?,?,?,0,?,?)",
                       (key, patient_id, title.strip() or "New consultation", visit_date or date.today().isoformat(), "", stamp, stamp))
        return key

    def consultation(self, key):
        with self.connection() as db:
            return self._one(db, "consultations", key)

    def consultations(self, patient_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""
                SELECT c.*, (SELECT COUNT(*) FROM versions v WHERE v.consultation_id=c.id) AS version_count,
                (SELECT status FROM versions v WHERE v.consultation_id=c.id AND v.source_revision=c.revision
                 ORDER BY v.number DESC LIMIT 1) AS status
                FROM consultations c WHERE patient_id=? ORDER BY visit_date DESC, created_at DESC
                """, (patient_id,))]

    def save_source(self, key, source, title, expected_revision):
        with self.connection(write=True) as db:
            old = self._one(db, "consultations", key)
            if old["revision"] != expected_revision:
                raise Conflict("This consultation changed in another session. Reopen it before saving.")
            title = title.strip() or "New consultation"
            if source != old["source"] or title != old["title"]:
                db.execute("UPDATE consultations SET source=?,title=?,revision=revision+1,updated_at=? WHERE id=?",
                           (source, title, now(), key))
            return self._one(db, "consultations", key)

    def add_version(self, consultation_id, source_revision, language, text, structured, report,
                    needs_review=False, origin="agent", parent_id=None):
        if not text.strip():
            raise ValueError("An empty explanation cannot be saved.")
        with self.connection(write=True) as db:
            c = self._one(db, "consultations", consultation_id)
            if c["revision"] != source_revision:
                raise Conflict("The doctor's input changed while generating. Generate again from the saved input.")
            if parent_id:
                parent = self._one(db, "versions", parent_id)
                if parent["consultation_id"] != consultation_id or parent["source_revision"] != source_revision:
                    raise Conflict("The explanation does not belong to this input revision.")
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE consultation_id=?",
                                (consultation_id,)).fetchone()[0]
            key = uuid.uuid4().hex
            db.execute("""INSERT INTO versions
                (id,consultation_id,number,source_revision,source,title,language,text,structured,report,
                 needs_review,origin,parent_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?)""",
                       (key, consultation_id, number, source_revision, c["source"], c["title"], language, text.strip(),
                        json.dumps(structured, ensure_ascii=False), json.dumps(report, ensure_ascii=False),
                        int(needs_review), origin, parent_id, now()))
        return key

    def version(self, key):
        with self.connection() as db:
            return self._one(db, "versions", key)

    def versions(self, consultation_id, language=None):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""
                SELECT id,number,language,status,origin,created_at,source_revision FROM versions
                WHERE consultation_id=? AND (? IS NULL OR language=?) ORDER BY number DESC
                """, (consultation_id, language, language))]

    def approve(self, key, reviewer, acknowledge=False):
        if not reviewer.strip():
            raise ValueError("Enter the reviewing doctor's name.")
        with self.connection(write=True) as db:
            v = self._one(db, "versions", key)
            c = self._one(db, "consultations", v["consultation_id"])
            if v["status"] != "draft" or c["revision"] != v["source_revision"]:
                raise Conflict("Only a draft matching the current doctor's input can be approved.")
            if v["needs_review"] and not acknowledge:
                raise ValueError("Review and acknowledge the agent's uncertainty before approval.")
            db.execute("UPDATE versions SET status='approved',reviewer=?,approved_at=? WHERE id=?",
                       (reviewer.strip(), now(), key))

    def reject(self, key):
        with self.connection(write=True) as db:
            if self._one(db, "versions", key)["status"] != "draft":
                raise Conflict("An approved or rejected version cannot be changed.")
            db.execute("UPDATE versions SET status='rejected' WHERE id=?", (key,))

    def approved(self, key):
        v = self.version(key)
        if v["status"] != "approved":
            raise ValueError("Approve this version before sharing, downloading or generating audio.")
        return v

    def artifact(self, key, kind):
        self.approved(key)
        with self.connection() as db:
            row = db.execute("SELECT data FROM artifacts WHERE version_id=? AND kind=?", (key, kind)).fetchone()
            return bytes(row[0]) if row else None

    def put_artifact(self, key, kind, data):
        self.approved(key)
        with self.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?)", (key, kind, data, now()))

    def share(self, key, days=7):
        self.approved(key)
        if not 0 < days <= 30:
            raise ValueError("Share links must expire within 30 days.")
        token = secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            db.execute("INSERT INTO shares VALUES (?,?,?,0,?)", (hashlib.sha256(token.encode()).hexdigest(), key,
                       (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(), now()))
        return token

    def shared_version(self, token):
        if not token or len(token) > 200:
            return None
        with self.connection() as db:
            row = db.execute("SELECT version_id FROM shares WHERE token_hash=? AND revoked=0 AND expires_at>?",
                             (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
        return self.approved(row[0]) if row else None

    def revoke_shares(self, key):
        with self.connection(write=True) as db:
            db.execute("UPDATE shares SET revoked=1 WHERE version_id=?", (key,))

    def delivery(self, key, channel, recipient="", status="prepared"):
        self.approved(key)
        delivery_id = uuid.uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (delivery_id, key, channel, recipient, status, now()))
        return delivery_id

    def delivery_status(self, delivery_id, status):
        with self.connection(write=True) as db:
            db.execute("UPDATE deliveries SET status=? WHERE id=?", (status, delivery_id))

    def deliveries(self, key):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE version_id=? ORDER BY created_at DESC", (key,))]
