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

## Zero duplication (single source of truth)
- Never show the same stat card, widget, list or action button in two places on a page, and never repeat dashboard widgets (stats, full skill-assessment lists, application lists) that live in the Student Workspace (`مساحتك الطلابية`). Link to it.
- Sidebars/identity cards stay minimal: avatar, name, handle, university/major, one or two subtle links. Details live in tabs.

## Responsive and RTL
- Explicitly verify **Desktop (>=1024px), Tablet (768-1023px), Mobile (<768px)** in both `dir="rtl"` and `dir="ltr"`: zero text/image overlap, zero horizontal overflow.
- Avatars: fixed square box (`width`/`height` in px, `flex-shrink: 0`, `overflow: hidden`, `border-radius: 50%`), `img { width: 100%; height: 100%; object-fit: cover; object-position: center top; display: block; }`, with a margin below before any text; badges sit on an outer wrapper so they are never clipped.
- Mobile-first. Test at 375px: no horizontal scroll, tap targets ≥ 40px, sticky parts become static on small screens.
- Use logical CSS (`inset-inline-*`, `margin-inline-*`) so Arabic RTL mirrors correctly. Wrap LTR fragments (emails, URLs, handles) in `dir="ltr"`/`<bdi>`.

## Behaviour
- Works without JS (links with `?tab=`), enhanced with JS (in-place tab switching, filters).
- Keep embedded page CSS in the page's `_styles.html` partial so it ships even when the server serves a cached stylesheet.
- Accessible: roles/aria on tabs and progress, visible focus, `hidden` for inactive panels.

## Check before finishing
Preview in the browser pane at desktop and 375px, and in Arabic. Reuse `templates/components/` before writing new markup.
