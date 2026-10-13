# Data

## `sample_prescriptions.jsonl` (included)

10 French prescription lines paired with a plain-English reference explanation.
Use them for demos and to compare model outputs.

- **Synthetic:** written for this project, based on common prescribing patterns. No real patients.
- **Not clinically reviewed:** a pharmacist should check them before you use them as ground truth.

Each line has the fields `id`, `language`, `prescription` and `reference_explanation`.

## External datasets

No public dataset pairs prescriptions with patient-friendly explanations. These are the closest:

| Dataset | What it has | License | Link |
|---|---|---|---|
| **PxCorpus** (Kocabiyikoglu et al., LREC 2022) | 4 h of French **spoken** drug prescriptions: 1,981 recordings with verified transcripts and semantic labels (drug, dose, frequency…). Matches the voice input path. | CC BY 4.0 | [Zenodo](https://zenodo.org/records/10080490) (`PxCorpus.zip`, 376 MB) |
| **CLARA-MeD** | 24k Spanish professional ↔ simplified medical text pairs, ~10k of them from drug leaflets | CC BY-NC-SA 4.0 (non-commercial) | [CSIC](https://datos.cchs.csic.es/en/dataset/clara-med-corpus) |

PxCorpus is not downloaded yet (376 MB). To fetch it:

```bash
curl -L -o data/PxCorpus.zip https://zenodo.org/api/records/10080490/files/PxCorpus.zip/content
```

`data/PxCorpus*` is git-ignored.

## Explanations of medicines and diseases

These have trusted plain-language explanations, not prescriptions. You can use them to ground the
model's "what it is for / what to avoid" text in real sources instead of model memory, or to evaluate it.

| Dataset | What it has | License | Link |
|---|---|---|---|
| **MedicationQA** (Ben Abacha et al., MEDINFO 2019) | 674 real consumer questions about medicines ("what is X for?", "can I take X with Y?", side effects…), answered by experts from MedlinePlus / DailyMed. Closest match to "explain my medicine". | CC BY 4.0 | [GitHub](https://github.com/abachaa/Medication_QA_MedInfo2019) (one .xlsx) |
| **MedQuAD** (NIH) | ~47k Q&A pairs from 12 NIH websites about diseases, drugs and tests (symptoms, causes, treatment…). The HF copy drops ~31k MedlinePlus answers for copyright, keeping mostly disease/genetics content. | Check terms per source | [Hugging Face](https://huggingface.co/datasets/lavita/MedQuAD), [original](https://github.com/abachaa/MedQuAD) |
| **MedlinePlus Health Topics XML** (NLM) | Official plain-language summaries of ~1,000 diseases and conditions, in English and Spanish, plus a health-terms glossary. Updated daily. | Free to download and use; credit MedlinePlus.gov | [medlineplus.gov/xml.html](https://medlineplus.gov/xml.html) |
| **openFDA drug labels** | Official US drug labels with sections like purpose, warnings, "do not use", "ask a doctor if", patient information. Good source for the "avoid" and warning-sign fields. | Public domain | [open.fda.gov](https://open.fda.gov/apis/drug/label/) |
