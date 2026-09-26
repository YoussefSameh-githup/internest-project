"""Translation catalog helper for environments without GNU gettext (e.g. Windows).

    python scripts/i18n_catalog.py check                 # list msgids missing an Arabic translation
    python scripts/i18n_catalog.py apply ar_pairs.json   # add {"English msgid": "Arabic"} pairs to locale/ar
    python scripts/i18n_catalog.py compile               # build locale/*/LC_MESSAGES/django.mo

On a machine with gettext installed, `manage.py makemessages -l ar` / `compilemessages` do the same job.
Requires: pip install polib
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ["internest_core", "internest_skills", "internest_app_project"]
LANGS = ["ar", "en"]
GETTEXT_FUNCS = {"_", "gettext", "gettext_lazy", "pgettext", "pgettext_lazy", "ngettext", "ngettext_lazy"}


def _templatize_msgids(path: Path):
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(USE_I18N=True)
        django.setup()
    from django.utils.translation.template import templatize

    code = templatize(path.read_text(encoding="utf-8"), origin=str(path))
    # templatize emits gettext(u'...') / ngettext(u'...', u'...', n)
    for m in re.finditer(r"\bn?gettext\(\s*u?'((?:[^'\\]|\\.)*)'", code):
        yield ast.literal_eval("'" + m.group(1) + "'")
    for m in re.finditer(r"\bngettext\(\s*u?'(?:[^'\\]|\\.)*',\s*u?'((?:[^'\\]|\\.)*)'", code):
        yield ast.literal_eval("'" + m.group(1) + "'")  # plural form


def _python_msgids(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in GETTEXT_FUNCS:
            for arg in node.args[:2]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield arg.value


def collect_msgids() -> dict[str, str]:
    found = {}
    for app in APPS:
        for path in (ROOT / app).rglob("*"):
            if "migrations" in path.parts or "tests" in path.name:
                continue
            if path.suffix == ".html":
                for msgid in _templatize_msgids(path):
                    found.setdefault(msgid, str(path.relative_to(ROOT)))
            elif path.suffix == ".py":
                for msgid in _python_msgids(path):
                    found.setdefault(msgid, str(path.relative_to(ROOT)))
    return found


def po_path(lang):
    return ROOT / "locale" / lang / "LC_MESSAGES" / "django.po"


def missing(lang="ar"):
    import polib

    po = polib.pofile(str(po_path(lang)))
    have = {e.msgid for e in po if e.msgstr and not e.obsolete and "fuzzy" not in e.flags}
    return {m: src for m, src in collect_msgids().items() if m and m not in have}


def apply(pairs_file):
    import polib

    pairs = json.loads(Path(pairs_file).read_text(encoding="utf-8"))
    po = polib.pofile(str(po_path("ar")))
    index = {e.msgid: e for e in po}
    for en, ar in pairs.items():
        entry = index.get(en)
        if entry is None:
            po.append(polib.POEntry(msgid=en, msgstr=ar))
        else:
            entry.msgstr = ar
            if "fuzzy" in entry.flags:
                entry.flags.remove("fuzzy")
    po.save()
    print(f"applied {len(pairs)} Arabic strings")


def compile_all():
    import polib

    for lang in LANGS:
        po = polib.pofile(str(po_path(lang)))
        po.save_as_mofile(str(po_path(lang).with_suffix(".mo")))
        print(f"{lang}: {po.percent_translated()}% translated → django.mo")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        todo = missing()
        print(json.dumps(todo, ensure_ascii=False, indent=1))
        print(f"{len(todo)} msgids without Arabic translation", file=sys.stderr)
    elif cmd == "apply":
        apply(sys.argv[2])
    elif cmd == "compile":
        compile_all()
