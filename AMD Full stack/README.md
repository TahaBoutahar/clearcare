# AMD Full stack — Prescription Explainer and Translator dashboard

A small web app to see what the [prescription explainer](..) does and
how well it does it.

- **Overview**: accuracy per field, checks by area, privacy leaks, unsupported numbers,
  review flags, speed and reading level, and a table comparing every benchmark run.
- **Cases**: every labelled test case with each check (expected vs got), what was sent to the
  AI after personal details were removed, the patient text and the doctor's review.
- **Run benchmark**: start a run (any provider, optional empty cache) and watch it live.
- **Try it**: explain one prescription and see each step as it happens.

## Run

```bash
./run.sh            # then open http://localhost:8000
```

The pipeline and the benchmark are the project folder above this one (set `PIPELINE_DIR` to use
another path). Provider, model and API keys come from that project's `.env`.
The server only listens on `127.0.0.1`.

## Benchmark from the terminal

```bash
cd ..
.venv/bin/python -m benchmark.run -p ollama
```

Test sets are in `benchmark/*.jsonl` (core, real-world messy, random); each run is saved in
`benchmark/results/`.
