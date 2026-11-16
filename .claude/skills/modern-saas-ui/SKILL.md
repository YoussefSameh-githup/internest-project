---
name: modern-saas-ui
description: Linear/Stripe-level SaaS UI standards for Internest screens. Use for any template, layout, CSS, or component work.
---

# Modern SaaS UI

## Layout
- Dashboards use a **two-column Bento layout**: sticky identity/summary card on the leading side, tabbed or segmented workspace on the other. Reference: `internship/profile.html`.
- Group long forms into collapsible sections (`<details>`), open the first one and any one with errors. No endless scrolling.
- One primary CTA per card; secondary actions are ghost buttons.

## Visual hierarchy
- Generous whitespace, subtle borders, soft shadows, one accent gradient. Numbers big, captions small.
- Every list has an **empty state** with an action (`components/empty_state.html`).
- Status is a pill (colour + icon + text), never colour alone.

## Responsive and RTL
- Mobile-first. Test at 375px: no horizontal scroll, tap targets ≥ 40px, sticky parts become static on small screens.
- Use logical CSS (`inset-inline-*`, `margin-inline-*`) so Arabic RTL mirrors correctly. Wrap LTR fragments (emails, URLs, handles) in `dir="ltr"`/`<bdi>`.

## Behaviour
- Works without JS (links with `?tab=`), enhanced with JS (in-place tab switching, filters).
- Keep embedded page CSS in the page's `_styles.html` partial so it ships even when the server serves a cached stylesheet.
- Accessible: roles/aria on tabs and progress, visible focus, `hidden` for inactive panels.

## Check before finishing
Preview in the browser pane at desktop and 375px, and in Arabic. Reuse `templates/components/` before writing new markup.
