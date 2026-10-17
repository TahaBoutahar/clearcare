"""The three-page doctor workflow, plus narrowly scoped patient share links."""
from __future__ import annotations

import hashlib
import hmac
import html
import os
from datetime import date
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from .agent import Agent, AgentError, ROOT, languages
from .outputs import patient_url, pdf_bytes, send_email
from .store import Conflict, Store


CSS = """
<style>
.block-container {padding-top:4.5rem; max-width:1500px;}
h1 {font-size:1.8rem!important; font-weight:600!important;}
h2 {font-size:1.25rem!important;}
h3 {font-size:1.05rem!important;}
[data-testid="stSidebar"] {background:#f1f4f1; width:240px!important; min-width:240px!important; max-width:240px!important; border-right:1px solid #e2e8e4;}
.cc-brand {display:flex; align-items:center; gap:.6rem; font-size:1.15rem; font-weight:650; color:#203a32; margin-bottom:.25rem;}
.cc-mark {display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:8px; background:#22654e; color:white; font-size:1.2rem;}
.cc-workspace {font-size:.8rem; color:#68786f; margin:0 0 1.5rem 2.35rem;}
.cc-reading {white-space:pre-wrap; overflow-wrap:anywhere; background:#fff;
 border:1px solid #dce4de; border-radius:10px; padding:1.3rem; line-height:1.75; color:#203a32;}
.st-key-consultation_panels [data-testid="stHorizontalBlock"] {flex-wrap:wrap;}
.st-key-consultation_panels [data-testid="stColumn"] {min-width:min(100%, 320px); flex:1 1 320px;}
.cc-identity {font-size:.9rem; color:#63746c; margin-bottom:1rem;}
</style>
"""


def reading(text, language="en"):
    direction = languages().get(language, {}).get("dir", "ltr")
    # One block per line (Markdown would read "1. Amoxicilline" as a list and split the box at blank lines),
    # each isolated so a line mixing Arabic and a Latin medicine name keeps the sheet's direction.
    lines = "".join(f'<div dir="{direction}" style="unicode-bidi:isolate">{html.escape(line) or "&nbsp;"}</div>'
                    for line in text.splitlines())
    st.html(f'<div class="cc-reading" dir="{direction}">{lines}</div>')


def language_label(code):
    item = languages()[code]
    return item["native"] + (f" ({item['name']})" if item["native"] != item["name"] else "")


def flash(message):
    st.session_state["flash"] = message


def go(view, patient_id=None, consultation_id=None):
    st.session_state["view"] = view
    if patient_id:
        st.session_state["patient_id"] = patient_id
    if consultation_id:
        st.session_state["consultation_id"] = consultation_id
    st.rerun()


def get_store():
    data = Path(os.getenv("CLEARCARE_DATA_DIR", str(ROOT / ".clearcare-data"))).expanduser()
    return Store(data / "clearcare.sqlite3")


# A request that came through a proxy or tunnel (e.g. a public HTTPS link) carries one of these headers,
# even though the app itself listens on 127.0.0.1.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "cf-connecting-ip")


def _reached_from_outside() -> bool:
    try:
        headers = {k.lower() for k in st.context.headers.keys()}
    except Exception:
        return False
    return any(h in headers for h in _PROXY_HEADERS)


def login():
    password = os.getenv("CLEARCARE_APP_PASSWORD", "")
    address = st.get_option("server.address") or "0.0.0.0"
    if not password:
        if address not in ("127.0.0.1", "localhost", "::1") or _reached_from_outside():
            st.error("Set CLEARCARE_APP_PASSWORD before exposing the doctor workspace (a public link or tunnel "
                     "reaches it from outside this computer), or use it only on this computer.")
            st.stop()
        return
    password_tag = hashlib.sha256(password.encode()).hexdigest()
    if st.session_state.get("authenticated") == password_tag:
        return
    st.title("ClearCare")
    with st.form("login"):
        supplied = st.text_input("Workspace password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            if hmac.compare_digest(supplied, password):
                st.session_state["authenticated"] = password_tag
                st.rerun()
            else:
                st.error("The password is incorrect.")
    st.stop()


@st.dialog("Add patient")
def patient_dialog(store, patient=None):
    patient = patient or {}
    with st.form("patient_form"):
        name = st.text_input("Full name", value=patient.get("name", ""), max_chars=200)
        born = st.date_input("Date of birth", value=date.fromisoformat(patient["birth_date"]) if patient else None,
                             min_value=date(1900, 1, 1), max_value=date.today())
        email = st.text_input("Email (optional)", value=patient.get("email", ""))
        codes = list(languages())
        preferred = patient.get("language", "en")
        language = st.selectbox("Preferred language", codes, index=codes.index(preferred), format_func=language_label)
        if st.form_submit_button("Save patient", type="primary"):
            try:
                key = store.save_patient(name, born.isoformat() if born else "", email, language, patient.get("id"))
            except ValueError as exc:
                st.error(str(exc))
            else:
                flash("Patient saved.")
                go("patient", key)


def patients_page(store):
    title, action = st.columns([4, 1], vertical_alignment="center")
    title.title("Patients")
    title.caption("Find a patient or open their consultation history.")
    if action.button("Add patient", type="primary", use_container_width=True):
        patient_dialog(store)
    search = st.text_input("Search by patient name or ID", placeholder="Patient name or CC-…")
    patients = store.patients(search)
    if not patients:
        st.info("No matching patients. Add a patient to start their first consultation.")
    for p in patients:
        with st.container(border=True):
            identity, history, action = st.columns([3, 2, 1], vertical_alignment="center")
            identity.write(p["name"])
            identity.caption(f"{p['reference']} · Born {p['birth_date']}")
            history.caption(f"{p['consultation_count']} consultations · Last visit: {p['last_visit'] or '—'}")
            if action.button("Open patient", key=f"open_{p['id']}", use_container_width=True):
                go("patient", p["id"])


def patient_page(store):
    patient = store.patient(st.session_state["patient_id"])
    if st.button("← Patients"):
        go("patients")
    title, action = st.columns([4, 1], vertical_alignment="center")
    title.title(patient["name"])
    title.caption(f"{patient['reference']} · Born {patient['birth_date']} · {language_label(patient['language'])}")
    if action.button("Edit patient details", use_container_width=True):
        patient_dialog(store, patient)
    heading, action = st.columns([4, 1], vertical_alignment="center")
    heading.subheader("Consultation history")
    if action.button("New consultation", type="primary", use_container_width=True):
        key = store.new_consultation(patient["id"])
        go("consultation", patient["id"], key)
    consultations = store.consultations(patient["id"])
    if not consultations:
        st.info("No consultations yet. Start a new consultation for this patient.")
    for c in consultations:
        with st.container(border=True):
            content, status, action = st.columns([4, 1, 1], vertical_alignment="center")
            content.write(f"{c['visit_date']} · {c['title']}")
            content.caption(f"{c['version_count']} saved explanation versions")
            status.caption((c["status"] or "No explanation").replace("draft", "Needs review").title())
            if action.button("Open", key=f"visit_{c['id']}", use_container_width=True):
                go("consultation", patient["id"], c["id"])


# Languages the doctor can dictate in ("" = let Whisper guess). Darija is transcribed as Arabic.
DICTATION_LANGUAGES = {"fr": "Français", "en": "English", "ar": "العربية / الدارجة", "": "Detect automatically"}


def add_transcript(store, key):
    """Append the reviewed transcript to the doctor's input and save it (runs before the page redraws)."""
    title_key, source_key = input_keys(key)
    transcript = st.session_state.get(f"transcript_{key}", "").strip()
    if not transcript:
        return
    current = st.session_state.get(source_key, "").rstrip()
    st.session_state[source_key] = f"{current}\n{transcript}" if current else transcript
    st.session_state[f"transcript_{key}"] = ""
    save_input(store, key)


def input_keys(key):
    return f"title_{key}", f"notes_{key}"


def save_input(store, key):
    drafts = st.session_state["drafts"]
    draft = drafts[key]
    title_key, source_key = input_keys(key)
    draft["title"] = st.session_state.get(title_key, draft["title"])
    draft["source"] = st.session_state.get(source_key, draft["source"])
    try:
        saved = store.save_source(key, draft["source"], draft["title"], draft["revision"])
    except Conflict as exc:
        draft["error"] = str(exc)
        return False
    draft.update(revision=saved["revision"], error=None)
    return True


def review_report(version):
    with st.expander("Review notes", expanded=version["needs_review"]):
        for line in version["report"].get("review", []):
            st.text(line)
        translation = version["report"].get("translation", {})
        if translation.get("kept_in_english"):
            st.warning("Some sentences remain in English. Review the exact text before approval.")
            for problem in translation["kept_in_english"]:
                st.write(problem["why"])
        if translation and not translation.get("is_right_language", True):
            st.warning("The translation service reported a different language from the requested one.")
        rating = version["report"].get("rating")
        if rating:
            st.caption(f"Second-model check: {rating.get('score', 'unavailable')}/5")
            st.write(rating.get("verdict", ""))
        if version["origin"] == "doctor_edit":
            st.info("A doctor edited this wording. It has not been regenerated or retranslated by the agent.")


def consultation_page(store):
    c = store.consultation(st.session_state["consultation_id"])
    p = store.patient(c["patient_id"])
    st.session_state["patient_id"] = p["id"]
    if st.button("← Patient history"):
        go("patient", p["id"])
    st.title(c["title"])
    st.caption(f"{p['name']} · {p['reference']} · Born {p['birth_date']} · Consultation {c['visit_date']}")
    drafts = st.session_state.setdefault("drafts", {})
    draft = drafts.setdefault(c["id"], {"title": c["title"], "source": c["source"], "revision": c["revision"], "error": None})
    title_key, source_key = input_keys(c["id"])
    for key, value in ((title_key, draft["title"]), (source_key, draft["source"])):
        if key not in st.session_state:
            st.session_state[key] = value
    with st.container(key="consultation_panels"):
        left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.subheader("Doctor’s input")
        if draft.get("error"):
            st.error(draft["error"] + " Your typed notes remain below so you can copy them.")
            if st.button("Reload saved input"):
                drafts.pop(c["id"], None)
                st.session_state.pop(title_key, None)
                st.session_state.pop(source_key, None)
                st.rerun()
        st.text_input("Consultation title", key=title_key, on_change=save_input, args=(store, c["id"]))
        st.text_area("Condition, prescription and advice", key=source_key, height=380,
                     placeholder="Enter the doctor's clinical notes…", on_change=save_input, args=(store, c["id"]))
        st.caption("Input saves when you finish an edit. The existing agent decides what it can explain.")
        with st.expander("Dictate a note"):
            spoken = st.selectbox("Language you speak", list(DICTATION_LANGUAGES), format_func=DICTATION_LANGUAGES.get,
                                  key=f"spoken_{c['id']}",
                                  help="Choose it: guessing the language often fails on short dictations.")
            recording = st.audio_input("Record the doctor's note", key=f"record_{c['id']}")
            if st.button("Transcribe recording", disabled=recording is None):
                try:
                    with st.spinner("Transcribing…"):
                        transcript = Agent().transcribe(recording.getvalue(), spoken or None)
                    st.session_state[f"transcript_{c['id']}"] = transcript
                except AgentError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("Transcription failed. Check that faster-whisper is installed, or type the note instead.")
            if st.session_state.get(f"transcript_{c['id']}"):
                st.text_area("Transcript: check it, correct it, then add it", key=f"transcript_{c['id']}", height=150)
                st.button("Add to the doctor’s input", type="primary", on_click=add_transcript, args=(store, c["id"]))
        codes = list(languages())
        language = st.selectbox("Patient language", codes, index=codes.index(p["language"]),
                                format_func=language_label, key=f"language_{c['id']}")
        generate = st.button("Create explanation", type="primary", use_container_width=True,
                             disabled=not st.session_state[source_key].strip() or bool(draft.get("error")))
    with right, st.container(border=True):
        st.subheader("What the patient will get")
        if generate and save_input(store, c["id"]):
            progress = st.empty()
            status = st.empty()

            def on_event(event):
                if event["type"] == "medicine_ready":
                    status.caption(f"Medicine {event['index'] + 1} read. Completing the explanation…")
                elif event["type"] == "dosing":
                    status.caption("Checking the complete explanation…")

            try:
                with st.spinner("Reading and checking the doctor's input…"):
                    result = Agent().generate(draft["source"], language, on_event=on_event)
                    store.add_version(c["id"], draft["revision"], language, **result)
            except AgentError as exc:  # the agent's own "Sorry, ... Why: ..." message
                st.error(f"{exc} Your input is saved.")
            except Exception:
                st.error("The explanation could not be completed. Your input is saved. Check the model configuration and retry.")
            else:
                progress.empty()
                status.empty()
                st.rerun()
        versions = store.versions(c["id"], language)
        if not versions:
            st.info("Create an explanation for this language. Generation only runs when you click the button.")
            return
        labels = {v["id"]: f"Version {v['number']} · {v['status']} · {v['created_at'][:16].replace('T', ' ')}" for v in versions}
        option_state = hashlib.sha256(repr(labels).encode()).hexdigest()[:12]
        selected = st.selectbox("Saved explanation", list(labels), format_func=labels.get,
                                key=f"version_{c['id']}_{language}_{option_state}")
        v = store.version(selected)
        fresh = v["source_revision"] == store.consultation(c["id"])["revision"] and not draft.get("error")
        if not fresh:
            st.info("This version belongs to earlier input. It stays available in history; generate again for the current input.")
        reading(v["text"], v["language"])
        st.caption(f"{language_label(v['language'])} · {v['status'].title()}")
        review_report(v)
        if v["status"] == "approved":
            st.success(f"Checked by {v['reviewer']} on {v['approved_at'][:10]}.")
            if st.button("Give to patient", type="primary", use_container_width=True):
                delivery_dialog(store, v["id"])
        elif v["status"] == "draft" and fresh:
            st.session_state.setdefault("reviewer_input", st.session_state.get("reviewer_name", os.getenv("CLEARCARE_CLINICIAN", "")))
            reviewer = st.text_input("Reviewing doctor", key="reviewer_input", placeholder="Enter your name to sign the explanation")
            st.session_state["reviewer_name"] = reviewer
            checked = st.checkbox("I reviewed this exact explanation for accuracy.", key=f"checked_{selected}")
            acknowledged = not v["needs_review"] or st.checkbox("I checked the flagged uncertainties and confirm this wording.",
                                                               key=f"ack_{selected}")
            if st.button("Approve & give to patient", type="primary", disabled=not checked or not acknowledged or not reviewer.strip(),
                         use_container_width=True):
                try:
                    store.approve(selected, reviewer, acknowledge=acknowledged)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.session_state["open_delivery"] = selected
                    st.rerun()
            if st.button("Reject this draft", key=f"reject_{selected}"):
                store.reject(selected)
                st.rerun()
        if fresh:
            with st.expander("Edit explanation"):
                with st.form(f"edit_{selected}"):
                    edited = st.text_area("Patient wording", value=v["text"], height=280)
                    if st.form_submit_button("Save as a new draft"):
                        try:
                            store.add_version(c["id"], v["source_revision"], language, edited, v["structured"], v["report"],
                                              True, "doctor_edit", selected)
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()
        activity = store.deliveries(selected)
        if activity:
            with st.expander("Delivery activity"):
                for item in activity:
                    st.caption(f"{item['created_at'][:16]} · {item['channel']} · {item['status']} · {item['recipient']}")


@st.dialog("Give to your patient", width="large")
def delivery_dialog(store, version_id):
    v = store.approved(version_id)
    c = store.consultation(v["consultation_id"])
    p = store.patient(c["patient_id"])
    st.caption(f"{p['name']} · {p['reference']} · {c['visit_date']} · Version {v['number']} · {language_label(v['language'])}")
    pdf_tab, email_tab, audio_tab, link_tab = st.tabs(["PDF / Print", "Email", "Audio", "Patient link"])
    pdf = None
    with pdf_tab:
        try:
            pdf = pdf_bytes(store, version_id)
        except Exception:
            st.error("The PDF could not be created. Check the bundled fonts and PDF dependencies; text download remains available.")
        if pdf:
            st.download_button("Download PDF", pdf, file_name=f"consultation-v{v['number']}.pdf",
                               mime="application/pdf", on_click="ignore", type="primary")
            st.caption("Open the downloaded PDF to print. A download does not confirm a paper copy printed.")
        st.download_button("Download written explanation", v["text"].encode("utf-8"),
                           file_name="explanation.txt", mime="text/plain; charset=utf-8", on_click="ignore")
    with audio_tab:
        if v["language"] == "ary":
            st.caption("Darija text · Arabic speech voice. Preview the pronunciation before sharing.")
        audio = store.artifact(version_id, "audio")
        if audio:
            st.audio(audio, format="audio/mpeg")
            st.download_button("Download audio", audio, file_name="explanation.mp3", mime="audio/mpeg", on_click="ignore")
        else:
            st.caption("Generate speech from the approved explanation.")
            if st.button("Generate approved audio"):
                try:
                    with st.spinner("Generating speech…"):
                        audio = Agent().audio(v["text"], v["language"])
                        store.put_artifact(version_id, "audio", audio)
                except Exception:
                    st.error("Speech generation failed. Your approved written version is still available.")
                else:
                    st.audio(audio, format="audio/mpeg")
                    st.download_button("Download audio", audio, file_name="explanation.mp3", mime="audio/mpeg", on_click="ignore")
    with email_tab:
        configured = bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))
        if not configured:
            st.info("Configure SMTP_HOST and SMTP_FROM to send email. PDF and text downloads already work.")
        with st.form(f"email_{version_id}"):
            recipient = st.text_input("Patient email", value=p["email"])
            confirmed = st.checkbox("I confirmed this email address with the patient.")
            include_audio = st.checkbox("Attach the approved audio", disabled=audio is None)
            st.caption("Email includes the approved text and PDF attachment.")
            if st.form_submit_button("Send email", type="primary", disabled=not configured or pdf is None):
                if not confirmed:
                    st.error("Confirm the patient's address before sending.")
                else:
                    try:
                        send_email(store, version_id, recipient.strip(), include_audio)
                    except (ValueError, RuntimeError) as exc:
                        st.error(str(exc))
                    else:
                        st.success("Email sent. This does not confirm the patient has read it.")
    with link_tab:
        if not os.getenv("CLEARCARE_PUBLIC_URL"):
            st.info("Set CLEARCARE_PUBLIC_URL to this app's reachable base URL to create patient links.")
        else:
            if st.button("Create patient link (7 days)"):
                try:
                    # Validate the URL before persisting a share token.
                    patient_url("test")
                    token = store.share(version_id)
                    url = patient_url(token)
                    st.session_state[f"share_url_{version_id}"] = url
                    store.delivery(version_id, "patient link", status="created")
                except ValueError as exc:
                    st.error(str(exc))
            if url := st.session_state.get(f"share_url_{version_id}"):
                st.code(url, language=None)
                st.link_button("Open patient view", url)
                st.caption("Anyone holding this link can read this approved version until it expires or is revoked.")
            if st.button("Revoke patient links for this version"):
                store.revoke_shares(version_id)
                st.session_state.pop(f"share_url_{version_id}", None)
                st.success("Links revoked.")


def patient_view(store, token):
    version = store.shared_version(token)
    if not version:
        st.title("This link is unavailable")
        st.info("It may have expired or been revoked. Contact your clinic for another copy.")
        return
    st.title("Your consultation, explained")
    st.caption(f"{language_label(version['language'])} · Checked by {version['reviewer']}")
    reading(version["text"], version["language"])
    try:
        st.download_button("Download PDF", pdf_bytes(store, version["id"]), "consultation.pdf", "application/pdf",
                           on_click="ignore")
    except Exception:
        st.caption("PDF is temporarily unavailable. You can still read the explanation above.")
    audio = store.artifact(version["id"], "audio")
    if audio:
        st.audio(audio, format="audio/mpeg")
        st.download_button("Download audio", audio, "explanation.mp3", "audio/mpeg", on_click="ignore")
    st.caption("For questions about your condition or treatment, contact your doctor or pharmacist.")


def _secrets_to_environment():
    """On a host such as Streamlit Cloud, settings and keys come as secrets: give them to the agent too."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, (str, int, float, bool)):
                os.environ.setdefault(key, str(value))
    except Exception:  # no secrets file: running locally with .env
        pass


def main():
    load_dotenv(ROOT / ".env", override=False)
    _secrets_to_environment()
    st.set_page_config(page_title="ClearCare", page_icon="📋", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    store = get_store()
    # Share links never render clinician navigation, patient search or consultation history.
    if token := st.query_params.get("share"):
        patient_view(store, token)
        st.stop()
    login()
    with st.sidebar:
        st.markdown('<div class="cc-brand"><span class="cc-mark" aria-hidden="true">+</span>ClearCare</div>'
                    '<div class="cc-workspace">Clinician workspace</div>', unsafe_allow_html=True)
        if st.button("Patients", use_container_width=True, type="primary"):
            go("patients")
        if os.getenv("CLEARCARE_APP_PASSWORD") and st.button("Sign out"):
            st.session_state.clear()
            st.rerun()
    if message := st.session_state.pop("flash", None):
        st.success(message)
    view = st.session_state.get("view", "patients")
    try:
        {"patients": patients_page, "patient": patient_page, "consultation": consultation_page}.get(view, patients_page)(store)
    except (ValueError, KeyError):
        st.error("This record is unavailable. Return to Patients and select a record again.")
    if version_id := st.session_state.pop("open_delivery", None):
        delivery_dialog(store, version_id)
