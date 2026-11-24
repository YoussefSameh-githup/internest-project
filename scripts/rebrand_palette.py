"""One-off migration from the 2025 blue/amber palette to the Internest brand (purple → orange), kept in the repo as the
record of the mapping. Idempotent: a second run finds nothing to change.

    python scripts/rebrand_palette.py

- styles.css :root tokens get the brand values (single source of truth for every page);
- other .css files: old hex values become var(--token) (centralised);
- templates (incl. e-mails, which cannot use CSS variables) and SVG attributes: old hex → new hex;
- rgba() tints of the old colours → tints of the new ones.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# old hex: (token, new hex)
PALETTE = {
    "#0b1e4d": ("--brand-900", "#170f3d"), "#112c6b": ("--brand-800", "#2a1a78"), "#1d3a8a": ("--brand-700", "#5b3fd6"),
    "#2746a8": ("--brand-600", "#6a4cff"), "#3b6fe0": ("--brand-500", "#7c5cff"), "#93b4fb": ("--brand-300", "#b7a6ff"),
    "#e0eaff": ("--brand-100", "#ebe5ff"), "#f1f6ff": ("--brand-50", "#f6f3ff"),
    "#b45309": ("--accent-700", "#c2410c"), "#d97706": ("--accent-600", "#f97316"), "#f59e0b": ("--accent-500", "#ff8a1f"),
    "#fbbf24": ("--accent-300", "#ffb054"), "#fef3c7": ("--accent-100", "#ffedd5"), "#fffbeb": ("--accent-50", "#fff7ed"),
}
PALETTE.update({  # stray Tailwind blues / ambers used directly in templates and app stylesheets
    "#1e3a8a": ("--brand-800", "#2a1a78"), "#1e40af": ("--brand-700", "#5b3fd6"), "#1d4ed8": ("--brand-700", "#5b3fd6"),
    "#2563eb": ("--brand-600", "#6a4cff"), "#3b82f6": ("--brand-500", "#7c5cff"), "#93c5fd": ("--brand-300", "#b7a6ff"),
    "#dbeafe": ("--brand-100", "#ebe5ff"), "#eff6ff": ("--brand-50", "#f6f3ff"),
    "#60a5fa": (None, "#9b85ff"), "#bfdbfe": (None, "#d6ccff"),
    "#fde68a": (None, "#fed7aa"), "#fcd34d": (None, "#fdba74"), "#92400e": (None, "#9a3412"), "#78350f": (None, "#7c2d12"),
})
RGB = {"59, 111, 224": "124, 92, 255", "245, 158, 11": "255, 138, 31", "29, 58, 138": "91, 63, 214",
       "11, 30, 77": "23, 15, 61", "39, 70, 168": "106, 76, 255",
       "37, 99, 235": "106, 76, 255", "29, 78, 216": "91, 63, 214", "59, 130, 246": "124, 92, 255"}
# The student's own résumé (PDF / ATS export / builder preview card) keeps its neutral document palette: it is the
# student's document, not Internest branding.
EXCLUDE = {"internest_students/templates/students/cv_ats.html", "internest_students/templates/students/_cv_card.html"}
HEX_RE = re.compile("(?:" + "|".join(re.escape(h) for h in PALETTE) + ")(?![0-9a-fA-F])", re.I)


def _css_value(match):
    token, value = PALETTE[match.group(0).lower()]
    return f"var({token})" if token else value
RGB_RE = re.compile(r"rgba\(\s*(" + "|".join(r",\s*".join(map(re.escape, k.split(", "))) for k in RGB) + r")\s*,", re.I)

ROOT_TOKENS = """    --brand-900: #170f3d;
    --brand-800: #2a1a78;
    --brand-700: #5b3fd6;
    --brand-600: #6a4cff;
    --brand-500: #7c5cff;
    --brand-300: #b7a6ff;
    --brand-100: #ebe5ff;
    --brand-50:  #f6f3ff;"""
ACCENT_TOKENS = """    --accent-700: #c2410c;
    --accent-600: #f97316;
    --accent-500: #ff8a1f;
    --accent-300: #ffb054;
    --accent-100: #ffedd5;
    --accent-50:  #fff7ed;"""
SEMANTIC = """
    /* Brand system (Internest logo: purple → orange on near-black). Every page reads these; nothing else hardcodes. */
    --ink:              #07071a;   /* the brand's near-black canvas (nav, hero, footer) */
    --ink-2:            #0d0c27;
    --brand-primary:    var(--brand-700);
    --brand-secondary:  var(--brand-500);
    --brand-accent:     var(--accent-500);
    --surface-primary:  var(--color-surface);
    --surface-secondary: var(--color-surface-2);
    --text-primary:     var(--color-text);
    --text-secondary:   var(--color-muted);
    --border-subtle:    var(--color-border);
    --gradient-brand:   linear-gradient(90deg, #6a4cff 0%, #9b5cf6 45%, #ff6a3d 72%, #ff8a1f 100%);
"""


def migrate_styles(text: str) -> str:
    start = text.index("    --brand-900:")
    end = text.index("    --brand-50:", start)
    end = text.index("\n", end)
    text = text[:start] + ROOT_TOKENS + text[end:]
    start = text.index("    --accent-700:")
    end = text.index("\n", text.index("    --accent-50:", start))
    text = text[:start] + ACCENT_TOKENS + text[end:]
    replacements = {
        "--gradient-hero: linear-gradient(135deg, #0b1e4d 0%, #1d3a8a 45%, #2746a8 100%);":
            "--gradient-hero: linear-gradient(135deg, #07071a 0%, #1a1147 55%, #2e1a6e 100%);",
        "--gradient-accent: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);":
            "--gradient-accent: linear-gradient(135deg, #ff8a1f 0%, #ff6a3d 100%);",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if "--gradient-brand:" not in text:
        anchor = text.index("    --gradient-hero:")
        text = text[:anchor] + SEMANTIC.lstrip("\n") + text[anchor:]
    root_end = text.index("\n}", text.index(":root"))
    head, body = text[:root_end], text[root_end:]
    body = HEX_RE.sub(_css_value, body)
    return head + body


def migrate(path: pathlib.Path) -> bool:
    text = path.read_text(encoding="utf-8")
    new = text
    if path.name == "styles.css" and path.parent.name == "css":
        new = migrate_styles(new)
    elif path.suffix == ".css":
        new = HEX_RE.sub(_css_value, new)
    else:  # templates, e-mails, SVG attributes: values, not variables
        new = HEX_RE.sub(lambda m: PALETTE[m.group(0).lower()][1], new)
    new = RGB_RE.sub(lambda m: "rgba(" + RGB[re.sub(r",\s*", ", ", m.group(1))] + ",", new)
    if new != text:
        path.write_text(new, encoding="utf-8", newline="")
        return True
    return False


if __name__ == "__main__":
    changed = []
    for pattern in ("internest_*/static/**/*.css", "internest_*/templates/**/*.html"):
        for path in sorted(ROOT.glob(pattern)):
            if path.relative_to(ROOT).as_posix() in EXCLUDE:
                continue
            if migrate(path):
                changed.append(path.relative_to(ROOT).as_posix())
    print(len(changed), "files changed")
    print("\n".join(changed))
