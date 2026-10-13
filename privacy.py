"""
privacy.py — Keep only the medical information of a prescription, before anything is sent to an AI.

Runs locally, with plain rules (no AI), so personal details never leave the computer:
    - lines about the patient, the doctor or the practice are removed entirely
      ("Patient :", "Nom", "Âge", "Adresse", "Tél", "CIN", "Mutuelle", "Dr ...", "Cabinet",
       "Signature", "Fait à ... le ...", a date on its own line, ...)
    - inside the remaining lines, names after Mme/M./Mlle, ages, phone numbers, e-mails,
      ID numbers and exact dates are removed.

What was removed is reported by category only (never the values), so the doctor can see it
worked without the details being shown or logged again.
"""

from __future__ import annotations

import re

# Lines that are about who/where, not about the treatment. Matched at the start of a line.
# Labels must be whole words: "AMO" (insurance) must not match "Amoxicilline".
_PERSONAL_LINE_RE = re.compile(
    r"^\s*[-•*]?\s*(?:("
    r"patient(e)?|nom|pr[ée]nom|name|first name|last name|"
    r"[âa]ge|n[ée]e? le|date de naissance|ddn|birth|"
    r"adresse|address|ville|city|"
    r"t[ée]l([ée]phone)?|tel|phone|gsm|portable|mobile|fax|e-?mail|courriel|"
    r"cin|c\.i\.n|carte d'identit[ée]|passeport|passport|id|"
    r"cnss|cnops|amo|mutuelle|assurance|insurance|n°\s*(de\s*)?(dossier|s[ée]cu|immatriculation|affiliation)|dossier|"
    r"dr|docteur|pr|professeur|m[ée]decin|"
    r"cabinet|clinique|clinic|h[ôo]pital|hospital|centre de sant[ée]|inpe|ordre des m[ée]decins|"
    r"signature|cachet|stamp"
    r")(?![\w])|date\s*:|fait [àa]\s|le\s+\d{1,2}[/.-]\d{1,2})",
    re.IGNORECASE,
)
# Words that carry no medical meaning once the personal details around them are gone.
_FILLER_WORDS = {"pour", "for", "joindre", "contacter", "contact", "appeler", "call", "au", "à", "a", "ou", "or",
                 "et", "and", "le", "la", "les", "de", "du", "des", "chez", "at", "to", "the", "par", "by", "with", "avec"}
# A line that is only a date (e.g. the date of the prescription).
_DATE_ONLY_LINE_RE = re.compile(r"^\s*(le\s+)?\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*$", re.IGNORECASE)

_INLINE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("e-mail", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("phone number", re.compile(r"(\+212|00212|\b0)\s?[5-7](?:[\s.-]?\d{2}){4}\b")),
    ("ID number", re.compile(r"\b[A-Z]{1,2}\s?\d{5,7}\b")),
    ("date", re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")),
    ("age", re.compile(r"\b\d{1,3}\s*(ans|years?\s*old|yo)\b", re.IGNORECASE)),
    ("name", re.compile(
        r"\b(Mme|Mlle|Mr|Mrs|Ms|Madame|Mademoiselle|Monsieur|M\.)\s+"
        r"[A-ZÀ-ÖØ-Þ][\w'-]*\.?(?:\s+[A-ZÀ-ÖØ-Þ][\w'-]*\.?){0,3}"
    )),
    ("name", re.compile(r"\b(patient|patiente)\s*:?\s*[A-ZÀ-ÖØ-Þ][^,.;\n]*", re.IGNORECASE)),
]

# What a removed line most likely was, for the report.
_LINE_KINDS = [
    ("patient name", r"^\s*[-•*]?\s*(patient|nom|pr[ée]nom|name)"),
    ("age or birth date", r"^\s*[-•*]?\s*([âa]ge|n[ée]e? le|date de naissance|ddn|birth)"),
    ("address", r"^\s*[-•*]?\s*(adresse|address|ville|city)"),
    ("phone number or e-mail", r"^\s*[-•*]?\s*(t[ée]l|tel|phone|gsm|portable|mobile|fax|e-?mail|courriel)"),
    ("ID or insurance number", r"^\s*[-•*]?\s*(cin|c\.i\.n|carte|passeport|passport|id\b|cnss|cnops|amo|mutuelle|assurance|insurance|n°|dossier)"),
    ("doctor or practice details", r"^\s*[-•*]?\s*(dr|docteur|pr\b|professeur|m[ée]decin|cabinet|clinique|clinic|h[ôo]pital|hospital|centre|inpe|ordre)"),
    ("signature or date", r"^\s*[-•*]?\s*(signature|cachet|stamp|fait|date|le\s+\d)"),
]


def keep_medical_only(text: str) -> tuple[str, list[str]]:
    """Return (text with only the medical content, categories of what was removed)."""
    removed: list[str] = []
    kept_lines: list[str] = []

    for line in text.splitlines():
        if _PERSONAL_LINE_RE.match(line) or _DATE_ONLY_LINE_RE.match(line):
            kind = next((k for k, p in _LINE_KINDS if re.match(p, line, re.IGNORECASE)), "personal details")
            removed.append(kind)
            continue
        changed = False
        for kind, pattern in _INLINE_PATTERNS:
            line, count = pattern.subn("", line)
            removed += [kind] * count
            changed = changed or count > 0
        if changed:  # tidy only the lines we touched; others stay exactly as written
            line = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", line))
            line = re.sub(r"^[,.;:]\s*|(?<=[,;:])\s*[,;:]", "", line).strip(" ,;:-—")
            if all(w in _FILLER_WORDS for w in re.findall(r"[\wÀ-ÿ]+", line.lower())):
                continue  # nothing medical is left on this line
        if line.strip():
            kept_lines.append(line)

    return "\n".join(kept_lines).strip(), list(dict.fromkeys(removed))


if __name__ == "__main__":
    sample = """Dr. Karim Benali — Médecine générale, Casablanca
Tél : 0522 33 44 55
Patient : Mme Fatima Zahra El Idrissi, 54 ans
CIN : BE123456
Date : 10/10/2026
Diagnostic : Angine
Amoxicilline 1 g : 1 cp matin et soir pendant 8 jours.
Pour M. Ahmed Alaoui, joindre au 06 12 34 56 78 ou ahmed@mail.com
Revoir dans 10 jours.
Signature et cachet"""
    clean, removed = keep_medical_only(sample)
    print(clean)
    print("\nRemoved:", ", ".join(removed))
