"""Map free-text skill names ("Python3", "py", "python developer") onto the platform's Skill taxonomy.

Order: exact name/alias → common abbreviation → name without version/role words → fuzzy (≥ 85% similar).
Matching onto an existing Skill keeps CV skills linked to their quizzes and learning paths.
"""
import re
from difflib import SequenceMatcher

FUZZY_THRESHOLD = 0.85
FUZZY_MIN_LENGTH = 4  # short names ("C", "R", "Go") only match exactly

# Shorthand the LLM or a CV may use → the taxonomy's preferred label (lower-case).
ABBREVIATIONS = {
    "py": "python", "js": "javascript", "ts": "typescript", "nodejs": "node.js", "node": "node.js",
    "reactjs": "react", "react.js": "react", "vuejs": "vue.js", "golang": "go", "postgres": "postgresql",
    "k8s": "kubernetes", "ml": "machine learning", "ai": "artificial intelligence", "dl": "deep learning",
    "nlp": "natural language processing", "ms excel": "excel", "microsoft excel": "excel",
    "ms word": "microsoft word", "ps": "photoshop", "adobe photoshop": "photoshop", "ui/ux": "ui/ux design",
}
_VERSION = re.compile(r"(?<=[a-z+#])\s*v?\d+(?:\.\d+)*$")  # python3, html5, css 3, angular v17
# Words about the person or level, never part of a skill's name.
_ROLE_WORDS = re.compile(r"\s+(?:developer|programmer|skills?|expert|specialist|proficiency|basics)$")
# Activity words: dropped only to find an existing skill ("python programming" → Python), kept on new ones
# ("Web Development" stays as is).
_ACTIVITY_WORDS = re.compile(r"\s+(?:development|programming|coding|language)$")


def normalize_key(name: str) -> str:
    key = re.sub(r"\s+", " ", (name or "").lower()).strip(" .,;:-–—()")
    key = re.sub(r"[إأآ]", "ا", key)
    return key.replace("ى", "ي").replace("ة", "ه")


def _strip(key: str, activity: bool = True) -> str:
    """Drop trailing role words and version numbers: "python 3 developer" → "python"."""
    previous = None
    while previous != key:
        previous = key
        key = _ROLE_WORDS.sub("", key)
        if activity:
            key = _ACTIVITY_WORDS.sub("", key)
        cut = _VERSION.sub("", key).strip()
        if len(cut) >= 2:
            key = cut
    return key


def _variants(key: str):
    stripped = _strip(key)
    for variant in dict.fromkeys((key, ABBREVIATIONS.get(key), stripped, ABBREVIATIONS.get(stripped))):
        if variant:
            yield variant


class TaxonomyIndex:
    """Lookup over a fixed list of Skill objects (their names and aliases)."""

    def __init__(self, skills):
        self.by_key = {}
        for skill in skills:
            for term in skill.alias_list:
                self.by_key.setdefault(normalize_key(term), skill)
        for skill in skills:  # canonical names win over another skill's alias
            self.by_key[normalize_key(skill.name)] = skill

    def add(self, skill):
        self.by_key.setdefault(normalize_key(skill.name), skill)

    def match(self, name: str):
        """The Skill this name means, or None when nothing in the taxonomy is close enough."""
        key = normalize_key(name)
        if not key:
            return None
        variants = list(_variants(key))
        for variant in variants:
            if variant in self.by_key:
                return self.by_key[variant]
        best, best_ratio = None, FUZZY_THRESHOLD
        for variant in variants:
            if len(variant) < FUZZY_MIN_LENGTH:
                continue
            for known, skill in self.by_key.items():
                if len(known) < FUZZY_MIN_LENGTH:
                    continue
                ratio = SequenceMatcher(None, variant, known).ratio()
                if ratio >= best_ratio:
                    best, best_ratio = skill, ratio
        return best

    def canonical_name(self, name: str) -> str:
        """Name to store for a skill not in the taxonomy yet (version and role words removed)."""
        cleaned = re.sub(r"\s+", " ", name).strip(" .,;:-–—()")
        return cleaned[:len(_strip(normalize_key(cleaned), activity=False))].strip() or cleaned
