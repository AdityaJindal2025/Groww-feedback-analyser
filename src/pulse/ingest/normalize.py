"""Normalisation helpers (T2.2).

Stored text is the scrubbed text exactly as written: no lowercasing, spelling fixes or emoji
removal, so quotes stay verbatim. Whitespace is normalised only when comparing (T5.4).
"""

from __future__ import annotations

import re
import unicodedata

from pulse.models import Script
from pulse.privacy.scrubber import has_redaction

__all__ = ["detect_script", "has_emoji", "has_redaction", "is_hinglish", "word_count"]

LATIN_THRESHOLD = 0.8
DEVANAGARI_THRESHOLD = 0.5


def _is_latin(ch: str) -> bool:
    return unicodedata.name(ch, "").startswith("LATIN")


def _is_devanagari(ch: str) -> bool:
    return "ऀ" <= ch <= "ॿ"


def detect_script(text: str) -> Script:
    """`latin` if at least 80% of letters are Latin (English and Hinglish), `devanagari` if
    Devanagari letters dominate, otherwise `other` (including text with no letters)."""
    latin = devanagari = letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        if _is_latin(ch):
            latin += 1
        elif _is_devanagari(ch):
            devanagari += 1
    if letters == 0:
        return "other"
    if latin / letters >= LATIN_THRESHOLD:
        return "latin"
    if devanagari / letters >= DEVANAGARI_THRESHOLD:
        return "devanagari"
    return "other"


def word_count(text: str) -> int:
    """Whitespace-separated tokens containing at least one letter or digit."""
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


# Emoji and pictographs: the emoji blocks, misc symbols and dingbats (★ ✔ ❤), arrows/stars
# (⭐), plus the variation selector and zero-width joiner used to build emoji sequences.
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]")


def has_emoji(text: str) -> bool:
    return _EMOJI.search(text) is not None


# Common Hindi words as written in Latin script. Chosen to rarely collide with English in app
# reviews: "pe" (P/E ratio), "par", "ya" and "to" are deliberately left out.
HINGLISH_MARKERS = frozenset(
    """
    hai hain h nahi nhi nahin bahut bohot bhut bht kya kyu kyun kyon kar karo karna karne
    karte karta kiya kiye ho hota hoti hote hua hui hue raha rahi rahe gaya gayi gaye mein
    mai mujhe mera meri mere hum humko hame hamara aap aapka aapki apna apne apni yeh ye woh
    wo vo isme usme iska uska kuch koi sab sabse bhi toh tho aur lekin se ka ki ke ko liye
    wala wale wali paisa paise rupay rupaye jab tab abhi phir fir accha acha achha bekar
    bakwas thik theek sahi galat jaldi diya dete deta dena lena liya milta milti mila
    """.split()  # noqa: SIM905 - a word block is easier to review than a list literal
)
_LATIN_WORD = re.compile(r"[a-z]+")


def is_hinglish(text: str) -> bool:
    """Hindi written in Latin letters: 3+ marker words, or 2+ making up at least 15% of words."""
    words = _LATIN_WORD.findall(text.lower())
    markers = sum(1 for word in words if word in HINGLISH_MARKERS)
    return markers >= 3 or (markers >= 2 and markers / len(words) >= 0.15)
