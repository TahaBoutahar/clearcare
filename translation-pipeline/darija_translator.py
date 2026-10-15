"""
darija_translator.py
--------------------
Moroccan Darija translation module for the AMD Hackathon ACT III
prescription explainer app.

Pipeline:
    [Simplified text in any language]
        → [NLLB-200 fine-tuned model for Darija]
            → [Darija in Arabic script]
                → if user chooses Arabizi:
                    → [Arabic to Arabizi transliteration]

Author: Mohamed Taha Boutahar — AI Logic
"""

import re
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 1. LAZY MODEL LOADING
# ─────────────────────────────────────────────

_translation_pipeline = None
_lang_detector = None

# AtlasIA Terjman-Nano: best small Darija model (request access on HuggingFace)
# → huggingface.co/atlasia/Terjman-Nano-v1  (click Request Access)
DARIJA_MODEL = "atlasia/Terjman-Nano"
# Fallback: Helsinki EN→AR (always public, no access needed, outputs Arabic MSA)
FALLBACK_MODEL = "Helsinki-NLP/opus-mt-en-ar"

SUPPORTED_SOURCE_LANGS = {
    "en": "English",
    "fr": "French",
    "ar": "Arabic (MSA)",
    "es": "Spanish",
    "de": "German",
}


def _load_translation_model():
    """Load the Darija translation model directly with AutoModelForSeq2SeqLM.
    Bypasses the broken pipeline API in transformers v5.
    """
    global _translation_pipeline
    if _translation_pipeline is not None:
        return _translation_pipeline

    def _try_load(model_id):
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        import torch
        logger.info(f"Loading '{model_id}' with AutoModelForSeq2SeqLM...")
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
        model.eval()
        logger.info(f"Model '{model_id}' loaded successfully.")
        return tokenizer, model

    # Try primary
    try:
        tokenizer, model = _try_load(DARIJA_MODEL)
        _translation_pipeline = (tokenizer, model, DARIJA_MODEL)
    except Exception as e:
        logger.warning(f"Primary model failed ({e}), trying fallback: {FALLBACK_MODEL}")
        try:
            tokenizer, model = _try_load(FALLBACK_MODEL)
            _translation_pipeline = (tokenizer, model, FALLBACK_MODEL)
            logger.info("Fallback model loaded successfully.")
        except Exception as e2:
            logger.error(f"Both models failed to load: {e2}")
            _translation_pipeline = None

    return _translation_pipeline


def _load_lang_detector():
    """Load langdetect (lazy)."""
    global _lang_detector
    if _lang_detector is not None:
        return _lang_detector
    try:
        from langdetect import detect
        _lang_detector = detect
    except ImportError:
        logger.warning("langdetect not installed. Defaulting to English.")
        _lang_detector = lambda x: "en"
    return _lang_detector


# ─────────────────────────────────────────────
# 2. ARABIZI TRANSLITERATION TABLE
# ─────────────────────────────────────────────

# Arabic character → Arabizi equivalent
# Keeps French loanwords (Latin chars) unchanged
ARABIZI_MAP = {
    # Darija-specific emphatic/special letters
    "ع": "3",
    "ح": "7",
    "خ": "kh",
    "ق": "9",
    "غ": "gh",
    "ش": "ch",
    "ص": "ss",
    "ض": "dd",
    "ط": "tt",
    "ظ": "dh",
    # Hamza variants → 2
    "ء": "2",
    "أ": "2",
    "إ": "2",
    "آ": "2a",
    # Regular consonants
    "ث": "th",
    "ذ": "dh",
    "ز": "z",
    "س": "s",
    "ج": "j",
    "ف": "f",
    "ك": "k",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ه": "h",
    "و": "w",
    "ي": "y",
    "ب": "b",
    "ت": "t",
    "د": "d",
    "ر": "r",
    # Vowels — alef after consonant adds "e" for natural Darija sound
    # e.g. واحد → wa7ed, كلام → klam
    "ا": "a",
    "ة": "a",
    "ى": "a",
    "َ": "e",   # fatha → e (more natural in Darija: wa7ed not wa7ad)
    "ُ": "ou",  # damma → ou (Darija: moul not mul)
    "ِ": "i",   # kasra
    "ّ": "",    # shadda
    "ْ": "",    # sukun
    "ـ": "",    # tatweel
    "\u200c": "",
    "\u200d": "",
}


# Common Darija words — direct override for natural Arabizi spelling
# These bypass the character-by-character transliteration
DARIJA_WORD_MAP = {
    "واحد": "wa7ed",
    "ديال": "dyal",
    "ديالك": "dyalek",
    "ديالي": "dyali",
    "ديالنا": "dyalna",
    "ديالهم": "dyalhom",
    "مرات": "mrat",
    "اليوم": "lyoum",
    "الوجبات": "lwajbat",
    "لمدة": "lmoddat",
    "أيام": "iyam",
    "تجنب": "tjenneb",
    "الكحول": "lk7ol",
    "توقف": "tw9af",
    "واتصل": "w-ttassel",
    "الطبيب": "ttbib",
    "شعرت": "ch3rti",
    "بالدوار": "b-ddwar",
    "اللوح": "llwa7",
    "مليغرام": "milighram",
    "من بعد": "men b3d",
    "إلا": "ila",
    "في": "f",
    "من": "men",
    "على": "3la",
    "مع": "m3a",
    "بعد": "b3d",
    "قبل": "9bel",
    "كل": "koll",
    "مرة": "merra",
    "يوم": "nhar",
    "صباح": "sba7",
    "ليلة": "lila",
    "حبة": "7abba",
    "حبوب": "7boub",
    "دواء": "dwa",
    "الدواء": "dwa",
    "طبيب": "tbib",
    "مستشفى": "sbitar",
    "صيدلية": "pharmacie",
    "ألم": "wja3",
    "حمى": "s5ana",
    "مريض": "mrid",
    "بصحة": "bsse77a",
}


def arabic_to_arabizi(arabic_text: str) -> str:
    """
    Transliterate Arabic-script Darija to Arabizi (Latin).
    Uses word-level dictionary for common Darija words first,
    then falls back to character-by-character for the rest.
    Latin characters (French loanwords) are kept unchanged.
    """
    # Step 1: word-level substitution for natural Darija words
    result_text = arabic_text
    for arabic_word, arabizi_word in DARIJA_WORD_MAP.items():
        result_text = result_text.replace(arabic_word, f" {arabizi_word} ")

    # Step 2: character-by-character for remaining Arabic chars
    result = []
    for char in result_text:
        if char in ARABIZI_MAP:
            result.append(ARABIZI_MAP[char])
        else:
            result.append(char)

    arabizi = "".join(result)
    # Clean up multiple spaces
    arabizi = re.sub(r" +", " ", arabizi).strip()
    return arabizi


# ─────────────────────────────────────────────
# 3. LANGUAGE DETECTION
# ─────────────────────────────────────────────

def detect_language(text: str) -> str:
    """
    Detect the language of the input text.

    Returns:
        ISO 639-1 language code (e.g. 'en', 'fr', 'ar').
        Defaults to 'en' if detection fails.
    """
    detect = _load_lang_detector()
    try:
        lang = detect(text)
        logger.info(f"Detected language: {lang}")
        return lang
    except Exception:
        logger.warning("Language detection failed, defaulting to 'en'.")
        return "en"


# ─────────────────────────────────────────────
# 4. CORE TRANSLATION
# ─────────────────────────────────────────────

def translate_to_darija(
    text: str,
    source_lang: str = "auto",
) -> str:
    """
    Translate simplified medical text into Moroccan Darija (Arabic script).

    Args:
        text:        Simplified prescription text in any supported language.
        source_lang: ISO 639-1 code ('en', 'fr', 'ar', 'es', 'de')
                     or 'auto' for automatic detection.

    Returns:
        Darija text in Arabic script, or an error message string
        starting with '[ERROR]' if translation fails.
    """
    if not text or not text.strip():
        return "[ERROR] Empty input text."

    # Auto-detect language
    if source_lang == "auto":
        source_lang = detect_language(text)

    # If not English or French, note it — model works best on EN/FR
    if source_lang not in ("en", "fr"):
        logger.warning(
            f"Source language '{source_lang}' may reduce quality. "
            "Model is optimised for English and French."
        )

    loaded = _load_translation_model()
    if loaded is None:
        return (
            "[ERROR] Darija translation model could not be loaded. "
            "Check your internet connection and that 'transformers' is installed."
        )

    tokenizer, model, model_id = loaded
    try:
        import torch
        inputs = tokenizer(text, return_tensors="pt", max_length=512, truncation=True)
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_length=512,
                num_beams=4,
                early_stopping=True,
            )
        darija_arabic = tokenizer.decode(outputs[0], skip_special_tokens=True)
        logger.info(f"Translation successful using '{model_id}'.")
        return darija_arabic
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return f"[ERROR] Translation failed: {e}"


# ─────────────────────────────────────────────
# 5. MASTER FUNCTION
# ─────────────────────────────────────────────

def process_darija(
    text: str,
    script_choice: str = "arabic",
    source_lang: str = "auto",
) -> dict:
    """
    Master function. Translate text to Darija and return both scripts.
    Always computes both; script_choice flags which one to display.

    Args:
        text:          Simplified prescription text (any supported language).
        script_choice: 'arabic' or 'arabizi' — tells the frontend which to show.
        source_lang:   'auto' or ISO 639-1 code.

    Returns:
        {
            "success":        bool,
            "darija_arabic":  str,   # Darija in Arabic script
            "darija_arabizi": str,   # Darija in Latin/Arabizi script
            "script_choice":  str,   # echoed back for the frontend
            "detected_lang":  str,   # detected or provided source language
            "error":          str | None
        }
    """
    detected = source_lang if source_lang != "auto" else detect_language(text)

    darija_arabic = translate_to_darija(text, source_lang=detected)

    if darija_arabic.startswith("[ERROR]"):
        return {
            "success": False,
            "darija_arabic": "",
            "darija_arabizi": "",
            "script_choice": script_choice,
            "detected_lang": detected,
            "error": darija_arabic,
        }

    darija_arabizi = arabic_to_arabizi(darija_arabic)

    return {
        "success": True,
        "darija_arabic": darija_arabic,
        "darija_arabizi": darija_arabizi,
        "script_choice": script_choice,
        "detected_lang": detected,
        "error": None,
    }


# ─────────────────────────────────────────────
# 6. QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    sample_en = (
        "Take 1 tablet of Amoxicillin 500mg three times a day after meals "
        "for 7 days. Avoid alcohol. If you feel dizzy, stop and call your doctor."
    )

    sample_fr = (
        "Prenez 1 comprimé d'Amoxicilline 500mg trois fois par jour après "
        "les repas pendant 7 jours. Évitez l'alcool."
    )

    print("=" * 60)
    print("TEST 1 — English input, Arabic script output")
    print("=" * 60)
    result = process_darija(sample_en, script_choice="arabic")
    print(f"Detected language : {result['detected_lang']}")
    print(f"Darija (Arabic)   : {result['darija_arabic']}")
    print(f"Darija (Arabizi)  : {result['darija_arabizi']}")
    print(f"Error             : {result['error']}")

    print()
    print("=" * 60)
    print("TEST 2 — French input, Arabizi output")
    print("=" * 60)
    result2 = process_darija(sample_fr, script_choice="arabizi", source_lang="fr")
    print(f"Detected language : {result2['detected_lang']}")
    print(f"Darija (Arabic)   : {result2['darija_arabic']}")
    print(f"Darija (Arabizi)  : {result2['darija_arabizi']}")
    print(f"Error             : {result2['error']}")