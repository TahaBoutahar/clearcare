"""
AMD Full stack — dashboard backend for ClearCare.

Shows what the pipeline does and how well it does it:
    GET  /api/config               provider, model, number of benchmark cases
    GET  /api/runs                 saved benchmark runs (newest first), with their summary
    GET  /api/runs/{file}          one full run: summary + every case with its checks
    POST /api/benchmark            start a benchmark run in the background
    GET  /api/benchmark/status     live progress of that run
    POST /api/explain              explain one prescription, streamed as JSON lines
    POST /api/translate            the patient sheet in another language (9 of them) and the judge's rating
    GET  /api/judge                how well the judge catches planted errors (judge-the-judge runs)

The pipeline itself is the project this folder sits in (PIPELINE_DIR, by default the parent
folder); nothing is copied here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
# The dashboard lives inside the project, so the pipeline is the parent folder.
PIPELINE_DIR = Path(os.getenv("PIPELINE_DIR", ROOT.parent)).resolve()
sys.path.insert(0, str(PIPELINE_DIR))

from doctor_cli import load_settings  # noqa: E402

load_settings(PIPELINE_DIR / ".env")
import ai_pipeline as pipeline  # noqa: E402
from benchmark import run as bench  # noqa: E402
from privacy import keep_medical_only  # noqa: E402

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

app = FastAPI(title="AMD Full stack — ClearCare dashboard")

# One run at a time: the pipeline's provider/model is process-wide.
_job_lock = threading.Lock()
_job: dict = {"running": False}


class BenchmarkRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    cold: bool = False
    limit: int | None = None
    dataset: str = "cases.jsonl"


class ExplainRequest(BaseModel):
    text: str
    provider: str | None = None


class TranslateRequest(BaseModel):
    prescription: str
    english: str
    extraction: dict
    lang: str = "ary"
    judge: bool = True


# The translation module (local only) has its own Python with torch; it runs as a worker process
# that stays up, so the translation model loads once.
TRANSLATION_DIR = PIPELINE_DIR / "translation-pipeline"
_worker: subprocess.Popen | None = None
_worker_lock = threading.Lock()


def _translation_worker() -> subprocess.Popen:
    global _worker
    if _worker and _worker.poll() is None:
        return _worker
    python = TRANSLATION_DIR / ".venv" / "bin" / "python"
    if not python.exists():
        raise HTTPException(501, f"The translation module is not set up on this computer ({python} is missing)")
    _worker = subprocess.Popen([str(python), "worker.py"], cwd=TRANSLATION_DIR, text=True, encoding="utf-8",
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if json.loads(_worker.stdout.readline() or "{}").get("ready") is not True:
        raise HTTPException(500, "The translation worker did not start")
    return _worker


def _use(provider: str | None, model: str | None = None) -> None:
    if provider and provider not in pipeline._PROVIDERS:
        raise HTTPException(400, f"Unknown provider {provider}")
    if provider or model:
        pipeline.configure(provider=provider, model=model)


def _key_missing() -> str | None:
    key_env = pipeline._PROVIDERS[pipeline.LLM_PROVIDER][1]
    return key_env if key_env and not os.getenv(key_env) else None


@app.get("/api/config")
def config():
    return {
        "provider": pipeline.LLM_PROVIDER,
        "model": pipeline.LLM_MODEL,
        "providers": sorted(pipeline._PROVIDERS),
        "key_missing": _key_missing(),
        "cases": len(bench.load_cases()),
        "datasets": {name: len(bench.load_cases(bench.CASES_FILE.parent / name)) for name in bench.datasets()},
        "pipeline_dir": str(PIPELINE_DIR),
    }


@app.get("/api/runs")
def runs():
    out = []
    for path in sorted(bench.RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({"file": path.name, "meta": data["meta"], "summary": data["summary"]})
    return out


@app.get("/api/runs/{name}")
def run_detail(name: str):
    path = (bench.RESULTS_DIR / name).resolve()
    if path.parent != bench.RESULTS_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "No such run")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/benchmark")
def start_benchmark(req: BenchmarkRequest):
    if not _job_lock.acquire(blocking=False):
        raise HTTPException(409, "A benchmark is already running")
    try:
        _use(req.provider, req.model)
        if missing := _key_missing():
            raise HTTPException(400, f"{missing} is not set in {PIPELINE_DIR / '.env'}")
        if req.dataset not in bench.datasets():
            raise HTTPException(400, f"Unknown test set {req.dataset}")
        cases = bench.load_cases(bench.CASES_FILE.parent / req.dataset)[: req.limit or None]
    except Exception:
        _job_lock.release()
        raise

    _job.clear()
    _job.update(running=True, done=0, total=len(cases), cases=[], file=None, error=None,
                provider=pipeline.LLM_PROVIDER, model=pipeline.LLM_MODEL, dataset=req.dataset, started=time.time())

    def progress(i, n, result):
        _job["done"] = i
        _job["cases"].append({
            "id": result["id"], "category": result["category"], "time_total": result["time_total"],
            "passed": sum(c["ok"] for c in result["checks"]), "checks": len(result["checks"]),
            "failed": [c["name"] for c in result["checks"] if not c["ok"]],
        })

    def work():
        try:
            report = bench.run_benchmark(cases, cold=req.cold, on_progress=progress, dataset=req.dataset)
            _job["file"] = report["meta"]["file"]
        except Exception as exc:  # shown in the dashboard instead of failing silently
            _job["error"] = str(exc)
        finally:
            _job["running"] = False
            _job_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "total": len(cases)}


@app.get("/api/benchmark/status")
def benchmark_status():
    return _job


@app.post("/api/explain")
def explain(req: ExplainRequest):
    if _job.get("running"):
        raise HTTPException(409, "A benchmark is running; try again when it is done")
    _use(req.provider)
    if missing := _key_missing():
        raise HTTPException(400, f"{missing} is not set in {PIPELINE_DIR / '.env'}")

    def stream():
        start = time.monotonic()
        sent, removed = keep_medical_only(req.text)
        yield json.dumps({"type": "privacy", "sent_to_ai": sent, "removed": removed}) + "\n"
        try:
            for event in pipeline.stream_prescription(req.text):
                elapsed = round(time.monotonic() - start, 2)
                if event["type"] == "medicine_ready":
                    yield json.dumps({
                        "type": "medicine_ready", "elapsed": elapsed, "index": event["index"],
                        "lines": pipeline.render_medicine(event["index"] + 1, event["medicine"], part="dosing"),
                    }, ensure_ascii=False) + "\n"
                elif event["type"] == "dosing":
                    p = event["prescription"]
                    yield json.dumps({
                        "type": "dosing", "elapsed": elapsed, "replaces_stream": event.get("replaces_stream", False),
                        "illness": pipeline.render_conditions(p["conditions"]),
                        "medicines": [pipeline.render_medicine(i, m, part="dosing") for i, m in enumerate(p["medicines"], 1)],
                    }, ensure_ascii=False) + "\n"
                elif event["type"] == "medicine":
                    yield json.dumps({
                        "type": "medicine", "elapsed": elapsed, "index": event["index"],
                        "lines": pipeline.render_medicine(event["index"] + 1, event["medicine"], part="details"),
                    }, ensure_ascii=False) + "\n"
                elif event["type"] == "done":
                    yield json.dumps({
                        "type": "done", "elapsed": elapsed, "text": event["text"], "review": event["review"],
                        "needs_review": event["needs_review"], "structured": event["prescription"],
                        "unsupported_numbers": bench.unsupported_numbers(event["text"], sent),
                        "reading_grade": bench.reading_grade(event["text"]),
                    }, ensure_ascii=False, default=str) + "\n"
        except pipeline.PipelineError as exc:
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/translate")
def translate(req: TranslateRequest):
    with _worker_lock:  # one translation at a time
        worker = _translation_worker()
        start = time.monotonic()
        worker.stdin.write(json.dumps({"prescription": req.prescription, "english": req.english,
                                       "extraction": req.extraction, "lang": req.lang, "judge": req.judge},
                                      ensure_ascii=False) + "\n")
        worker.stdin.flush()
        line = worker.stdout.readline()
    if not line:
        raise HTTPException(500, "The translation worker stopped")
    out = json.loads(line)
    if "error" in out:
        raise HTTPException(500, f"Translation failed: {out['error']}")
    out["elapsed"] = round(time.monotonic() - start, 2)
    return out


@app.get("/api/judge")
def judge_runs():
    """Judge-the-judge results: planted errors caught, false alarms, made-up reasons."""
    out = []
    for path in sorted((TRANSLATION_DIR / "data").glob("judge_eval_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({"file": path.name, **data})
    return out


FRONTEND = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
