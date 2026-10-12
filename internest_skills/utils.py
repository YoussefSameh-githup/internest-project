"""Profile handles in freelance / social links, and fuzzy matching of a handle against a student's identity."""
import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

OWNER_MATCH_THRESHOLD = 0.8

# host → path pattern whose first group is the account handle
_PROFILE_PATTERNS = {
    "mostaql.com": re.compile(r"^/u/([^/]+)"),
    "khamsat.com": re.compile(r"^/user/([^/]+)"),
    "upwork.com": re.compile(r"^/(?:freelancers|fl|o/profiles/users)/([^/]+)"),
    "fiverr.com": re.compile(r"^/([^/]+)"),
    "github.com": re.compile(r"^/([^/]+)"),
    "linkedin.com": re.compile(r"^/in/([^/]+)"),
}
# First path segments that are site pages, not usernames.
_RESERVED = {
    "fiverr.com": {"categories", "gigs", "search", "pro", "business", "support", "start_selling", "login", "join", "inbox",
                   "users", "s", "hc", "resources", "cp", "logo-maker"},
    "github.com": {"orgs", "topics", "explore", "marketplace", "settings", "features", "about", "login", "join", "sponsors",
                   "collections", "trending", "search", "notifications", "pricing", "enterprise", "apps", "site"},
}


def extract_platform_username(url) -> str | None:
    """Lower-case handle from a profile/gig link (mostaql.com/u/x, khamsat.com/user/x, github.com/x, fiverr.com/x/gig,
    upwork.com/freelancers/x, linkedin.com/in/x); None when the link has no readable username (gig ids, ~upwork ids)."""
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    parts = urlparse(raw if re.match(r"https?://", raw, re.I) else "https://" + raw)
    host = (parts.hostname or "").lower()
    site = next((s for s in _PROFILE_PATTERNS if host == s or host.endswith("." + s)), None)
    if site is None:
        return None
    match = _PROFILE_PATTERNS[site].match(unquote(parts.path))
    if not match:
        return None
    handle = match.group(1).strip().lstrip("@").lower()
    if not handle or handle.startswith("~") or handle in _RESERVED.get(site, ()):  # "~01ab…" is an anonymous Upwork id
        return None
    return handle


def _letters(text) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _tokens(text) -> list[str]:
    """Latin name parts of an identifier: "Youssef Sameh", "youssef.sameh95@x.com" prefix, "youssef-sameh-4487"."""
    return [t for t in re.split(r"[^a-z]+", re.sub(r"\d+", " ", (text or "").lower())) if len(t) >= 2]


def handle_match_score(handle: str, identifier: str) -> float:
    """0–1 similarity between a platform handle and one identifier (name, email prefix, username, profile slug)."""
    h = _letters(re.sub(r"\d+", "", handle or ""))
    tokens = _tokens(identifier)
    if len(h) < 3 or not tokens:
        return 0.0
    joined = "".join(tokens)
    if h == joined or h == "".join(reversed(tokens)):
        return 1.0
    score = SequenceMatcher(None, h, joined).ratio()
    long_tokens = [t for t in tokens if len(t) >= 3]
    if len(long_tokens) >= 2 and sum(t in h for t in long_tokens) >= 2:
        score = max(score, 0.95)  # "sameh_youssef_dev"
    if len(tokens) >= 2 and h in (tokens[0][0] + tokens[-1], tokens[0] + tokens[-1][0], tokens[-1] + tokens[0][0]):
        score = max(score, 0.9)  # "ysameh", "youssefs"
    if any(t in h for t in long_tokens if len(t) >= 4):
        score = max(score, 0.7)  # one name part only ("youssefdesigns"): not enough on its own
    return round(score, 2)


def best_owner_match(handle: str, identifiers) -> float:
    return max((handle_match_score(handle, i) for i in identifiers if i), default=0.0)
