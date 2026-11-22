"""PII redaction for text sent to EXTERNAL AI providers (a local model gets the original text).

    red = redact("Mona Adel, mona@x.com, 0101 234 5678", names=["Mona Adel"])
    red.text      -> "[NAME_1], [EMAIL_1], [PHONE_1]"
    restore(reply, red.mapping)                      # put the real values back into the model's answer

Covers e-mail addresses, URLs (scheme, www. or domain/path), phone numbers (Egyptian +20 / 0020 / 01x mobiles and
0x landlines, plus other international numbers) and 14-digit Egyptian national IDs, in Western and Arabic-Indic digits,
plus an optional list of known names (full name and each part of 3+ letters, Arabic or Latin). The same value always
gets the same placeholder within one call, so the model can still tell "the same e-mail twice" apart from two e-mails.
"""
import re
from dataclasses import dataclass, field

def _digit_class(western: str) -> str:
    """A character class for these Western digits plus their Arabic-Indic (U+066x) and Persian (U+06Fx) forms."""
    return "[" + "".join(d + chr(0x0660 + int(d)) + chr(0x06F0 + int(d)) for d in western) + "]"


_D = _digit_class("0123456789")
_ZERO, _ONE, _MOBILE, _AREA, _ID_CENTURY = (_digit_class(d) for d in ("0", "1", "0125", "23456789", "23"))
_SEP = r"[\s.\- ]?"

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Scheme / www. links, and bare "domain.tld/path" for common TLDs (not .js / .net, so "Node.js/React" or
# "ASP.NET/C#" in a skills list stay intact), plus the portfolio hosts students use.
URL = re.compile(r"(?:\bhttps?://|\bwww\.)[^\s<>\"'()\[\]]+"
                 r"|\b(?:[a-z0-9\-]+\.)+(?:com|org|io|me|dev|app|co|eg|ly|info|ai|xyz)/[^\s<>\"'()\[\]]*"
                 r"|\bbehance\.net/[^\s<>\"'()\[\]]*", re.I)
NATIONAL_ID = re.compile(rf"(?<!{_D}){_ID_CENTURY}{_D}{{13}}(?!{_D})")  # Egyptian: century digit 2/3 + 13 digits
PHONE = re.compile(
    rf"(?<![\w+])(?:"
    rf"(?:\+|00){_D}{{1,3}}{_SEP}\(?{_D}{{1,4}}\)?(?:{_SEP}{_D}{{2,4}}){{2,4}}"      # international: +20 10 1234 5678
    rf"|{_ZERO}?{_ONE}{_MOBILE}{_SEP}{_D}{{4}}{_SEP}{_D}{{4}}"                       # Egyptian mobile: 010 1234 5678
    rf"|{_ZERO}{_AREA}{_D}?{_SEP}{_D}{{3,4}}{_SEP}{_D}{{4}}"                          # landline: 02 2345 6789
    rf")(?!{_D})"
)
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴"
                               "۵۶۷۸۹", "01234567890123456789")


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text.translate(_ARABIC_DIGITS))


@dataclass
class Redaction:
    text: str
    mapping: dict = field(default_factory=dict)  # placeholder -> original value

    @property
    def changed(self) -> bool:
        return bool(self.mapping)


class _Redactor:
    def __init__(self, names=()):
        self.mapping: dict[str, str] = {}
        self._by_value: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}
        self.name_pattern = _name_pattern(names)

    def placeholder(self, kind: str, value: str) -> str:
        key = (kind, value.casefold())
        if key not in self._by_value:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"[{kind}_{self._counts[kind]}]"
            self._by_value[key] = token
            self.mapping[token] = value
        return self._by_value[key]

    def _sub(self, pattern, kind, text, check=None):
        def repl(match):
            value = match.group(0)
            if check is not None and not check(value):
                return value
            return self.placeholder(kind, value)
        return pattern.sub(repl, text)

    def text(self, text: str) -> str:
        if not text:
            return text
        text = self._sub(EMAIL, "EMAIL", text)
        text = self._sub(URL, "URL", text)
        text = self._sub(NATIONAL_ID, "NATIONAL_ID", text)
        text = self._sub(PHONE, "PHONE", text, check=lambda v: 8 <= _digits(v) <= 15)
        if self.name_pattern is not None:
            text = self._sub(self.name_pattern, "NAME", text)
        return text

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value


def _name_pattern(names):
    variants = set()
    for name in names or ():
        name = " ".join(str(name or "").split())
        if len(name) < 3:
            continue
        variants.add(name)
        variants.update(part for part in name.split(" ") if len(part) >= 3)
    if not variants:
        return None
    alternatives = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))  # full names before parts
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.I)


def redact(text: str, names=()) -> Redaction:
    redactor = _Redactor(names)
    return Redaction(redactor.text(text), redactor.mapping)


def redact_data(data, names=()):
    """(redacted copy of a str / dict / list, mapping) with one placeholder numbering across all of it."""
    redactor = _Redactor(names)
    return redactor.value(data), redactor.mapping


_TOKEN = re.compile(r"\[(?:EMAIL|URL|NATIONAL_ID|PHONE|NAME)_\d+\]")


def restore(value, mapping: dict):
    """Put the original values back into an AI reply (strings inside dicts / lists too)."""
    if not mapping:
        return value
    if isinstance(value, str):
        return _TOKEN.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, dict):
        return {k: restore(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [restore(v, mapping) for v in value]
    return value
