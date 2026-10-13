# Prescription Explainer and Translator

AMD Developer Hackathon ACT III — Health and Wellbeing track.

Turns a doctor's prescription (text or voice, often in French) into a short, plain-English
explanation for patients who may not read well. It **only restates the prescription**: no
diagnosis, no medical advice. A doctor reviews and approves every explanation before the
patient gets it.

## How it works

1. **Privacy filter, on this computer** ([privacy.py](privacy.py)): names, ages, phone numbers,
   ID/insurance numbers, dates and the doctor's stamp are removed before anything is sent to an AI.
2. **Reading the prescription** (AI, compact JSON): illness, and for each medicine the amount,
   times of day, food, duration, "only when needed", maximum per day.
3. **Checks against the original text** (code, not AI): every number must be on the prescription,
   tablet/capsule as written, food rules and follow-up visits must be written, missed details are
   re-read from the medicine's own line. Nothing is fixed or removed silently.
4. **Plain sentences built from templates** (code), so a dose cannot change while being "reworded".
5. **Uncertainty is visible**: the patient gets an apology with the reason ("Sorry, I am not sure
   about how much to take. Why: the amount is not written on the prescription"); the doctor gets a
   review box listing every doubt and correction.
6. **Doctor approval**: approve, edit or reject. Only approved text is saved for the patient,
   stamped "Checked by your doctor".

## Run it

```bash
./install.sh          # creates the venv, the settings file (.env) and the `rx` command
rx                    # interactive: paste a prescription, press Enter twice
rx -f examples/angine.txt
rx -p ollama          # free local model; default provider is set in .env
```

Providers: `amd` (AMD Developer Cloud, `amd/DeepSeek-V4-Flash`), `ollama` (local), `openrouter`.

## Measuring it

```bash
.venv/bin/python -m benchmark.run --cases cases.jsonl        # core set (25 cases)
.venv/bin/python -m benchmark.run --cases realworld.jsonl    # messy real-world prescriptions (24)
.venv/bin/python -m benchmark.generate --seed 7 -n 40         # random prescriptions with exact answers
.venv/bin/python -m benchmark.run --cases random_seed7.jsonl
```

Each run scores field accuracy, medicines found or invented, illness, review flags, privacy
leaks, numbers that are not on the prescription, speed and reading level, and is saved in
`benchmark/results/`. The **AMD Full stack** dashboard shows and compares the runs.

## Roadmap: translation for multi-country use

The explanation will be available in **at least 7 languages plus Moroccan Darija and other
Arabic dialects**, so the same tool works for patients across several countries:

- **Languages:** English, French, Modern Standard Arabic, Spanish, Portuguese, German, Italian
  (more as needed, e.g. Turkish).
- **Darija and dialects:** Moroccan Darija first, then Algerian, Tunisian, Egyptian, Levantine
  and Gulf Arabic; Tamazight (Amazigh) is being considered.
- **Safe by design:** the sentences that carry doses ("Take 1 tablet in the morning and in the
  evening, for 8 days") are built from fixed templates, so each template is translated once and
  checked by a native-speaking health professional. The AI never re-words a dose; only medicine
  names, the illness and the doctor's own instructions need translating, and those go through the
  same checks (numbers must match the prescription) and the doctor's approval.
- **Spoken version:** an audio reading of the translated text for patients who cannot read.

## Files

| File | What it does |
|---|---|
| `ai_pipeline.py` | The pipeline: extraction, checks, rendering, `process_prescription()` / `stream_prescription()` |
| `privacy.py` | Local removal of personal details |
| `doctor_cli.py` | Terminal tool for doctors, with review and approval |
| `benchmark/` | Test sets, random generator and scorer |
| `examples/` | Example prescriptions |
| `data/` | Sample data and dataset notes |
