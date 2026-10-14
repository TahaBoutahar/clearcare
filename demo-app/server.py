"""
Demo app server — Prescription Explainer and Translator, for testing on a phone in hospitals.

    ./run.sh            same Wi-Fi / iPhone hotspot: https://<this-mac>:8443 (QR code shown)
    ./run.sh --tunnel   from anywhere, through a temporary public HTTPS link (PIN required)

Self-contained: its own virtual environment, settings (demo-app/.env) and logs (demo-app/data/).
It only reads the pipeline from the project folder above; it never changes it.

Endpoints:
    POST /api/transcribe   voice recording -> text (Whisper, on this computer)
    POST /api/explain      prescription -> streamed reading, patient sheet (EN/FR/AR) and doctor review
    POST /api/translate    same result in another language
    POST /api/decision     doctor's decision for the pilot log (no patient text)
    GET  /api/stats        pilot numbers
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = Path(os.getenv("PIPELINE_DIR", HERE.parent)).resolve()
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(HERE))



def load_settings(path: Path) -> None:
    """KEY=VALUE lines into the environment; values already set win (real env > demo .env > project .env)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_settings(HERE / ".env")      # demo settings first (PIN, provider for the demo, Whisper size...)
load_settings(PROJECT / ".env")   # then the project's provider / model / keys (read only)

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import ai_pipeline as pipeline  # noqa: E402
import translator  # noqa: E402
from privacy import keep_medical_only  # noqa: E402

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

DATA = HERE / "data"
LOG_FILE = DATA / "demo_log.jsonl"
PIN = os.getenv("DEMO_PIN", "").strip()

app = FastAPI(title="Prescription Explainer and Translator — demo")


@app.on_event("startup")
def _warm_up_whisper() -> None:
    """Load (and, the first time, download) the speech model in the background."""
    def load():
        try:
            pipeline._get_whisper_model()
        except Exception as exc:  # dictation will report it; typing still works
            logging.getLogger(__name__).warning("Speech model not ready: %s", exc)
    threading.Thread(target=load, daemon=True).start()

# Recent results, so switching language does not re-read the prescription. Memory only.
_results: OrderedDict[str, dict] = OrderedDict()
_results_lock = threading.Lock()
_log_lock = threading.Lock()


def _check_pin(pin: str | None) -> None:
    if PIN and not secrets.compare_digest((pin or "").strip(), PIN):
        raise HTTPException(401, "Wrong or missing PIN")


def _remember(extraction: dict) -> str:
    rid = uuid.uuid4().hex[:12]
    with _results_lock:
        _results[rid] = extraction
        while len(_results) > 50:
            _results.popitem(last=False)
    return rid


class ExplainRequest(BaseModel):
    text: str
    lang: str = "en"


class TranslateRequest(BaseModel):
    id: str
    lang: str


class AudioRequest(BaseModel):
    text: str
    lang: str = "en"


class Decision(BaseModel):
    id: str | None = None
    doctor: str = ""
    decision: str                      # approved | edited | rejected
    reason: str | None = None          # why it was rejected
    lang: str = "en"
    seconds: float | None = None       # from "Explain" to the decision
    time_to_dosing: float | None = None
    lines_total: int | None = None
    lines_changed: int | None = None   # how much the doctor edited
    needs_review: bool | None = None
    medicines: int | None = None
    flags_unsure: int | None = None
    flags_risk: int | None = None
    input_mode: str = "typed"          # typed | voice


@app.get("/api/config")
def config():
    return {"provider": pipeline.LLM_PROVIDER, "model": pipeline.LLM_MODEL, "pin": bool(PIN),
            "languages": translator.LANGUAGES}


@app.post("/api/check-pin")
def check_pin(x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)
    return {"ok": True}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)
    suffix = Path(audio.filename or "voice.m4a").suffix or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(await audio.read())
        path = Path(f.name)
    try:
        return {"text": pipeline.transcribe_voice(str(path))}
    except pipeline.PipelineError as exc:
        raise HTTPException(422, str(exc))
    finally:
        path.unlink(missing_ok=True)  # the recording is never kept


@app.post("/api/explain")
def explain(req: ExplainRequest, x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)

    def stream():
        start = time.monotonic()
        sent, removed = keep_medical_only(req.text)
        yield json.dumps({"type": "privacy", "removed": removed, "sent_to_ai": sent}, ensure_ascii=False) + "\n"
        try:
            for event in pipeline.stream_prescription(req.text):
                elapsed = round(time.monotonic() - start, 2)
                if event["type"] == "dosing":
                    p = event["prescription"]
                    yield json.dumps({
                        "type": "dosing", "elapsed": elapsed,
                        "lines": pipeline.render_conditions(p["conditions"]) + [""] + [
                            line for i, m in enumerate(p["medicines"], 1) for line in pipeline.render_medicine(i, m, part="dosing")],
                    }, ensure_ascii=False) + "\n"
                elif event["type"] == "done":
                    extraction = event["prescription"]
                    rid = _remember(extraction)
                    sheet = translator.render(extraction, req.lang)
                    issues = pipeline._all_issues(extraction)
                    yield json.dumps({
                        "type": "done", "elapsed": elapsed, "id": rid, "sheet": sheet,
                        "review": event["review"], "needs_review": event["needs_review"],
                        "medicines": len(extraction["medicines"]),
                        "flags_unsure": sum(i["level"] == "unsure" for i in issues),
                        "flags_risk": sum(i["level"] == "risk" for i in issues),
                    }, ensure_ascii=False) + "\n"
        except pipeline.PipelineError as exc:
            yield json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/translate")
def translate(req: TranslateRequest, x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)
    with _results_lock:
        extraction = _results.get(req.id)
    if extraction is None:
        raise HTTPException(404, "This result has expired; explain the prescription again")
    return translator.render(extraction, req.lang)


@app.post("/api/audio")
def audio(req: AudioRequest, background: BackgroundTasks, x_pin: str | None = Header(default=None)):
    """The patient sheet as an MP3 the patient can keep (Google's text-to-speech; the file is deleted after sending)."""
    _check_pin(x_pin)
    language = {"en": "english", "fr": "french", "ar": "arabic"}.get(req.lang, "english")
    path = Path(tempfile.mkdtemp()) / "patient-sheet.mp3"
    try:
        pipeline.text_to_audio(_speakable(req.text), language, str(path))
    except pipeline.PipelineError as exc:
        raise HTTPException(502, f"Sorry, I could not make the audio. Why: {exc}")
    background.add_task(lambda: (path.unlink(missing_ok=True), path.parent.rmdir()))
    return FileResponse(path, media_type="audio/mpeg", filename=f"prescription-{req.lang}.mp3")


def _speakable(text: str) -> str:
    """Read numbered medicine titles naturally: "1. Amoxicilline 1 g" -> "Amoxicilline 1 g." """
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            line = re.sub(r"^\d+\.\s*", "", line)
            lines.append(line if line.endswith((".", ":", "؟", "!")) else line + ".")
    return "\n".join(lines)


@app.post("/api/decision")
def decision(d: Decision, x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)
    record = {"time": datetime.now().isoformat(timespec="seconds"), "provider": pipeline.LLM_PROVIDER,
              "model": pipeline.LLM_MODEL, **d.model_dump(exclude={"id"})}
    with _log_lock:
        DATA.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/api/stats")
def stats(x_pin: str | None = Header(default=None)):
    _check_pin(x_pin)
    rows = []
    if LOG_FILE.is_file():
        rows = [json.loads(l) for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = len(rows)
    count = lambda k: sum(r["decision"] == k for r in rows)
    secs = sorted(r["seconds"] for r in rows if r.get("seconds") is not None)
    flagged = [r for r in rows if r.get("needs_review")]
    clean = [r for r in rows if r.get("needs_review") is False]
    reasons: dict[str, int] = {}
    for r in rows:
        if r["decision"] == "rejected":
            reasons[r.get("reason") or "no reason"] = reasons.get(r.get("reason") or "no reason", 0) + 1
    edited = [r for r in rows if r["decision"] == "edited" and r.get("lines_total")]
    return {
        "total": n,
        "approved": count("approved"), "edited": count("edited"), "rejected": count("rejected"),
        "median_seconds": secs[len(secs) // 2] if secs else None,
        "edit_share": round(sum(r["lines_changed"] / r["lines_total"] for r in edited) / len(edited), 3) if edited else None,
        "flagged": len(flagged),
        "flagged_changed": sum(r["decision"] != "approved" for r in flagged),
        "clean": len(clean),
        "clean_changed": sum(r["decision"] != "approved" for r in clean),
        "reasons": reasons,
        "by_language": {lang: sum(r.get("lang") == lang for r in rows) for lang in translator.LANGUAGES},
        "voice": sum(r.get("input_mode") == "voice" for r in rows),
        "doctors": len({r.get("doctor") for r in rows if r.get("doctor")}),
    }


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")
