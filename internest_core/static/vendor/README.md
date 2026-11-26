# Vendored front-end libraries (served from our own origin)

Copied unmodified from the npm packages (`npm pack`, registry-verified), except the `sourceMappingURL` comments were
removed (the .map files are not shipped and Django's manifest storage would fail on the missing reference).

| Folder | Package | Licence |
| --- | --- | --- |
| `bootstrap-5.3.3/` | bootstrap@5.3.3 (`dist/css/bootstrap{,.rtl}.min.css`, `dist/js/bootstrap.bundle.min.js`) | MIT (`LICENSE`) |
| `bootstrap-icons-1.11.3/` | bootstrap-icons@1.11.3 (`font/bootstrap-icons.min.css`, `font/fonts/*`) | MIT (`LICENSE`) |
| `fonts/` | @fontsource-variable/cairo@5.3.0, @fontsource/tajawal@5.3.0 (500/700/800), @fontsource-variable/inter@5.3.0: Arabic + Latin + Latin-ext woff2 only; `fonts-rtl.css` / `fonts-ltr.css` rename the families to `Cairo` / `Tajawal` / `Inter` | SIL OFL 1.1 (`LICENSE-*.txt`) |

Why: no third-party origin on every page's critical path (no extra DNS + TLS before first paint on mobile), and the files get
the same year-long immutable caching as the rest of /static/. To upgrade, replace the folder and bump the version in
`internship/base.html`.
