# Streamlit clinician workspace

This application adds patient records, consultation histories and approval-controlled
delivery around ClearCare's **existing, unchanged AI agent**. It does not change the
model, prompts, medical checks, privacy filter, translation templates, or benchmark dashboard.
It uses Streamlit directly, with SQLite persistence; FastAPI is not required.

## Run locally

Use Python 3.11 or newer (3.12 recommended):

```sh
cp .env.example .env  # only if .env does not already exist
# Configure the existing provider/model credentials in .env as usual.
./run_streamlit.sh
```

The launcher installs `requirements-streamlit.txt` into `.venv` and opens the service
on `127.0.0.1:8501`. To choose the Python used to create a new environment:

```sh
PYTHON_BIN=python3.12 ./run_streamlit.sh
```

After installation, subsequent starts can skip dependency installation:

```sh
.venv/bin/python -m streamlit run streamlit_app.py
```

Set `CLEARCARE_CLINICIAN` to prefill the reviewing doctor's name at the approval step. Optional app/email settings
are documented in `.env.streamlit.example`; add those settings to the root `.env`.
Never commit a populated `.env` or patient database.

## Workflow

1. **Patients:** search by name or unique ID. Add/edit details in a dialog. Names need
   not be unique; the UI shows date of birth and an assigned reference.
2. **Patient record:** see dated consultations, continue a draft, or start a fresh visit.
3. **Consultation workspace:** original doctor notes on the left and the existing
   agent's explanation on the right. Notes save on committed edits; generation only
   occurs on **Create explanation**. A language selection displays that language's saved
   versions; it never silently regenerates a paid model call.
4. Review the exact text and agent report. Explicitly acknowledge flagged uncertainty,
   enter the reviewer name, and approve. Doctor edits create a **new draft version**.
5. A delivery dialog offers PDF/text, email, approved audio and optional patient links.

Source and title changes create a new input revision. Earlier approved versions stay
available in history, but cannot be approved against a different input. PDFs, audio,
delivery activity and share links reference a particular approved version. A new edit
does not reuse an older version's audio or approval. Concurrent stale writes fail with
a reload message instead of overwriting another session's work.

The app displays only the current agent's output. More detailed explanations of conditions
or multiple discussion topics depend on what that unchanged agent can already represent.
The existing privacy filter's behavior is unchanged, including its known limitations for
clinical sentences starting with “Patient …”. Review the source and output accordingly.

## Written, voice and email output

- Unicode PDF exports bundle Noto Sans and Noto Naskh Arabic under the SIL Open Font
  License. Arabic shaping uses HarfBuzz. Open the downloaded PDF in a viewer to print.
  A download does not prove that a physical copy printed.
- Dictation uses the existing `transcribe_voice()` implementation. A recording first
  becomes a transcript for the doctor to review and copy into the input; transcription
  does not automatically call the explanation model. Temporary recordings are deleted.
- Audio generation calls the existing gTTS function, with correct language-name mapping.
  gTTS is an external speech service. Darija wording is read with the existing Arabic
  voice, labelled **Darija text, Arabic voice**. This is not a dedicated Moroccan voice;
  preview pronunciation before delivery. The approved Darija text is not rewritten.
- Email requires `SMTP_HOST`, `SMTP_FROM`, and optionally `SMTP_USERNAME`/`SMTP_PASSWORD`.
  Port 587 uses STARTTLS; port 465 uses SMTP over TLS. The doctor confirms the recipient.
  The message contains the exact approved text and PDF, with optional approved audio.
  A failed send is recorded as failed; a sent message does not prove the patient read it.
- Set `CLEARCARE_PUBLIC_URL` to the application's reachable base URL to create patient
  links. They expire after 7 days, can be revoked, and expose only one approved explanation
  and its PDF/audio. Only a SHA-256 token digest is stored. The URL itself grants access,
  so treat it as confidential. Patient mode never renders clinician controls or history.
  Email attachments also work without a public URL.

## Storage and deployment boundary

Data defaults to `.clearcare-data/clearcare.sqlite3` (ignored by Git). Override with
`CLEARCARE_DATA_DIR` pointing to a private, persistent directory. The app uses foreign
keys, transactions, WAL, a restrictive directory mode and a private database file.
Patient data must not go in global Streamlit data caches. Session state is for UI context;
the database is the durable record. Keep the database, WAL and SHM files together when
running, and use SQLite's backup API for consistent backups.

This is a **local, single-clinic prototype**, not a multi-tenant medical record system.
For a shared instance, configure `CLEARCARE_APP_PASSWORD`, HTTPS, persistent private
storage, backups and an appropriate deployment environment. The application refuses
the clinician workspace on a non-loopback bind address without a password. The shared
password is a basic workspace gate, not individual clinician authentication; reviewer
names are entered by the operator. Per-user identity, role-based access and encryption
at rest need a separate deployment implementation before a production rollout.

No patient name or contact fields are added to agent requests. Clinical notes still pass
through the existing agent's filter on the server. Do not describe that as browser-local
redaction when deploying to a remote server. The app serializes agent calls within a
process because the existing pipeline uses process-wide settings and file caches; keep
one application process until those unchanged components support a broader deployment.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Tests use temporary databases and mocked agent, speech and SMTP boundaries. They cover
patient isolation, duplicate names, persistence, stale writes, approval, immutable versions,
Unicode PDFs, audio language mapping, share-link isolation/expiry/revocation, email status,
and the Streamlit patient-to-consultation workflow. They do not make paid model calls.

## App files

`streamlit_app.py` is the entrypoint. `ui.py` implements screens and dialogs, `store.py`
owns data/approval invariants, `agent.py` adapts the existing pipeline, and `outputs.py`
delivers approved snapshots. `.streamlit/config.toml` supplies the light neutral theme.
