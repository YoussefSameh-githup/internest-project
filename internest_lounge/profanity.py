"""Arabic + English profanity / hate-speech filter for the Founders Network and private messages.

No external dependency: text is normalised (Arabic diacritics, letter variants and Franco-Arabic; English
leetspeak and stretched letters) and matched against whole words, so ordinary words that merely contain a
blocked sequence are not rejected. Extend the lists with settings.LOUNGE_BLOCKED_WORDS.
"""
import re
from functools import lru_cache

from django.conf import settings

# Kept to unambiguous insults, sexual slurs and hate speech to avoid false positives.
ENGLISH = [
    "fuck", "fucker", "fucking", "motherfucker", "shit", "bullshit", "bitch", "bastard", "asshole", "dickhead",
    "cunt", "whore", "slut", "wanker", "twat", "pussy", "cock", "dick", "douchebag", "jackass", "retard",
    "nigger", "nigga", "faggot", "fag", "kike", "spic", "chink", "tranny",
]
# Written in normalised form: no diacritics, ا for أ/إ/آ, ي for ى, ه for ة.
ARABIC = [
    "كسمك", "كسم", "كس امك", "كس اختك", "شرموط", "شرموطه", "شراميط", "عرص", "معرص", "معرصين", "خول", "خولات",
    "متناك", "متناكه", "منيوك", "منيوكه", "نيك", "نيكه", "زبي", "زبك", "طيز", "قحبه", "قحاب", "لبوه",
    "عرصه", "طيزك", "ابن الكلب", "ابن الوسخه", "ابن المتناكه", "يلعن ابوك", "يلعن دينك", "يلعن امك", "كلب ابن كلب",
]
# Franco-Arabic (Latin letters + digits), matched on the leet-normalised text.
FRANCO = ["kosomak", "kos omak", "kosom", "sharmota", "sharmouta", "metnak", "mitnak", "manyok", "khawal", "5awal",
          "3ars", "m3ars", "ma3ars", "mo3ars", "a7a"]

_ARABIC_PREFIX = r"(?:و|ف|ب|ل|ال|يا|وال|بال|فال|لل)?"
_ARABIC_SUFFIX = r"(?:ات|ين|ون|ك|كم|كو|هم)?"  # no bare ه: "خوله" is the name Khawla; feminine forms are listed
_ENGLISH_SUFFIX = r"(?:s|es|ed|er|ers|ing|y)?"
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # tashkeel + tatweel
_LEET = str.maketrans({"0": "o", "1": "i", "!": "i", "3": "e", "4": "a", "@": "a", "5": "s", "$": "s", "7": "t", "+": "t"})


def _normalize_arabic(text):
    text = _DIACRITICS.sub("", text)
    text = re.sub("[أإآٱ]", "ا", text)
    return text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")


def _collapse(text):
    """'fuuuuck' → 'fuck' (runs of 3+ letters become one; real double letters survive)."""
    return re.sub(r"(.)\1{2,}", r"\1", text)


def _phrase(words):
    return r"\s+".join(re.escape(w) for w in words.split())


@lru_cache(maxsize=1)
def _patterns():
    extra = [w.lower() for w in getattr(settings, "LOUNGE_BLOCKED_WORDS", [])]
    arabic = ARABIC + [w for w in extra if re.search(r"[؀-ۿ]", w)]
    latin = ENGLISH + [w for w in extra if not re.search(r"[؀-ۿ]", w)]
    arabic_re = re.compile(
        r"(?<![\w])" + _ARABIC_PREFIX + "(?:" + "|".join(_phrase(_normalize_arabic(w)) for w in arabic) + ")" + _ARABIC_SUFFIX + r"(?![\w])"
    )
    english_re = re.compile(r"\b(?:" + "|".join(_phrase(w) for w in latin) + ")" + _ENGLISH_SUFFIX + r"\b", re.IGNORECASE)
    # Franco words are written with digits (3, 5, 7…), so match them before leet normalisation.
    franco_re = re.compile(r"\b(?:" + "|".join(_phrase(w) for w in FRANCO) + r")\b", re.IGNORECASE)
    return arabic_re, english_re, franco_re


def contains_profanity(*texts) -> bool:
    arabic_re, english_re, franco_re = _patterns()
    for text in texts:
        if not text:
            continue
        lowered = _collapse(text.lower())
        if franco_re.search(lowered) or arabic_re.search(_normalize_arabic(lowered)):
            return True
        if english_re.search(lowered) or english_re.search(lowered.translate(_LEET)):
            return True
    return False
