"""Reading statistics and a script-ratio language guess - no model.

Language is read off the letters themselves: Latin script that reads as
English (enough common English words) is "en", Devanagari is "hi", Telugu
is "te", two scripts each holding a real share is "mixed", and anything
else - too few letters to judge, Latin text that is not recognisably
English, OCR noise - is "unknown" rather than a guess. Devanagari is also
written for Marathi and Nepali; for this platform's documents Hindi is the
reading that matters, and the label says what script the text is in.
"""

import math
import re

from .text import STOPWORDS

WORDS_PER_MINUTE = 230
TOKEN = re.compile(r"\w+", re.UNICODE)
MIN_LETTERS = 20
DOMINANT = 0.8
SHARE = 0.15
# Share of words that are common English function words. Running English
# prose sits near 0.4; a table-heavy notice still clears 0.05 easily.
ENGLISH_STOPWORD_SHARE = 0.05

SCRIPTS = {
    "latin": lambda c: "a" <= c.lower() <= "z" or "À" <= c <= "ɏ",
    "hi": lambda c: "ऀ" <= c <= "ॿ",
    "te": lambda c: "ఀ" <= c <= "౿",
}


def detect_language(text: str) -> str:
    counts = dict.fromkeys(SCRIPTS, 0)
    letters = 0
    for character in text:
        if not character.isalpha():
            continue
        letters += 1
        for name, test in SCRIPTS.items():
            if test(character):
                counts[name] += 1
                break
    if letters < MIN_LETTERS:
        return "unknown"

    shares = {name: count / letters for name, count in counts.items()}
    dominant = max(shares, key=shares.get)
    if shares[dominant] >= DOMINANT:
        if dominant != "latin":
            return dominant
        words = [w.lower() for w in TOKEN.findall(text) if len(w) > 1]
        if not words:
            return "unknown"
        english = sum(1 for word in words if word in STOPWORDS) / len(words)
        return "en" if english >= ENGLISH_STOPWORD_SHARE else "unknown"
    if sum(1 for share in shares.values() if share >= SHARE) >= 2:
        return "mixed"
    return "unknown"


def compute_stats(pages) -> dict:
    """pages: (page_number, text, extraction_method) triples."""
    words = 0
    ocr_pages = 0
    sample = []
    for _, text, method in pages:
        text = text or ""
        words += len(TOKEN.findall(text))
        if method == "OCR":
            ocr_pages += 1
        if sum(len(part) for part in sample) < 50_000:
            sample.append(text[:20_000])
    return {
        "pages": len(pages),
        "words": words,
        "reading_minutes": math.ceil(words / WORDS_PER_MINUTE) if words else 0,
        "ocr_pages": ocr_pages,
        "language": detect_language("\n".join(sample)),
    }
