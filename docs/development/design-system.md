# Frontend foundations: tokens, data cache, error handling

What every screen inherits from the shell, and the conventions a new screen
follows. The code is the reference: `frontend/src/styles.css` (tokens),
`src/api/cache.ts` and `src/api/hooks.ts` (data), `src/components/ErrorBoundary.tsx`
and `src/lib/lazy.ts` (failures), `src/lib/theme.ts` (theme).

## Design tokens

All defined once on `:root` in `src/styles.css`. A new stylesheet
(`src/styles/<feature>.css`) uses them instead of literals; a literal that stays
(a glyph sized to the tile it sits in, a line in a drawing) says why in a comment.

| Kind | Tokens | Use |
|---|---|---|
| Type size | `--text-2xs` 11 · `--text-xs` 12 · `--text-sm` 13 · `--text-base` 14 · `--text-md` 15 · `--text-lg` 17 · `--text-xl` 20 · `--text-2xl` 24 · `--text-3xl` 28 · `--text-4xl` 36 (px) | keys/markers · captions, badges, table headings · secondary text, labels · body, controls · card titles · dialog/section titles · phone page titles · hero headings · page titles, figures · display |
| Line height | `--leading-none` 1 · `--leading-tight` 1.15 · `--leading-snug` 1.3 · `--leading-normal` 1.5 · `--leading-relaxed` 1.65 | display · headings · body · reading (answers) |
| Weight | `--weight-regular` 400 · `--weight-medium` 500 · `--weight-strong` 550 · `--weight-semibold` 600 · `--weight-bold` 650 | Inter is variable: 550 for controls and labels, 650 for display |
| Tracking | `--tracking-tight` −0.025em · `--tracking-snug` −0.01em · `--tracking-wide` 0.04em | display · headings · uppercase overlines |
| Space | `--space-1` 4px … `--space-16` 64px (4px grid), half steps `--space-0-5` 2 · `--space-1-5` 6 · `--space-2-5` 10 · `--space-3-5` 14 | gap, padding, margin |
| Motion | `--dur-fast` 120ms · `--dur-base` 200ms · `--dur-slow` 320ms · `--ease-out` · `--ease-spring` (`--ease` is an alias of `--ease-out`) | hover/press · things that enter · large surfaces. All are cut to nothing under `prefers-reduced-motion` |
| Stacking | `--z-raised` 1 · `--z-sticky` 5 · `--z-topbar` 20 · `--z-sidebar` 30 · `--z-scrim` 35 · `--z-drawer` / `--z-dropdown` 40 · `--z-skip-link` 100 · `--z-toast` 200 | native modal dialogs sit in the browser's top layer, above all of these |
| Colour, radius, shadow | unchanged: `--bg`, `--surface*`, `--text*`, `--primary`, `--accent*`, status tones, `--radius-*`, `--shadow-*` | |

Breakpoints are conventions (custom properties cannot be used in media queries):
560px phone, 860px tablet (the sidebar becomes a drawer), 1080px narrow desktop,
1200px; 480/640/720px are component breakpoints.

### Dark theme

`data-theme` on `<html>` always names the theme on screen, `light` or `dark`:
the saved choice, or under "System" the operating system's current answer.
`public/theme-init.js` resolves it before the first paint and `src/lib/theme.ts`
follows the system setting live, so the dark tokens are declared once, under
`:root[data-theme="dark"]`. Write a dark-only rule the same way:

```css
:root[data-theme="dark"] .my-chart-grid { stroke: var(--border-strong); }
```

The `theme-color` metas (the browser's address bar on a phone) follow the same
resolved theme. The theme is chosen in the user menu (Light / Dark / System)
and in quick find.

## Data: `useResource` and its cache

`useResource(loader, deps, options)` is stale-while-revalidate:

- A loader that returns **one API call's promise unchanged** (`() => api.document(id)`)
  is cached under the request's path. A revisit within 60 s paints the cached
  data before the first frame, with `stale: true`, and **always loads again** -
  the cache never saves a request, so audited reads (a document view) are still
  recorded on every visit.
- A loader that reshapes its response (`() => api.x().then(...)`) returns a new,
  untagged promise and is not cached - two screens can never be handed each
  other's shapes. A composite call can opt in by tagging its own result with
  `tagRequest(promise, key)` (`api.collections` does, as `/api/v1/collections/#all`).
- Identical GETs in flight at the same moment share one request (not when the
  caller passes an `AbortSignal`).
- Any request that can change data - POST, PUT, PATCH, DELETE, an upload, a chat
  turn - invalidates the whole cache once the server has answered, as does any
  401 (so one person's data is never shown to the next to sign in). A response
  that left the server before a change is not cached. A POST that only reads
  passes `mutates: false` to `request` (search does).
- If a reload fails after showing cached data, the data is withdrawn and the error
  shown exactly as on a first load.
- Options: `{ cache: false }` opts a call out; `{ maxAge: ms }` changes how old a
  cached result may be and still be shown (default 60 s). `invalidateResources(prefix?)`
  (from `api/hooks`) forgets cached results - for changes the client cannot see,
  such as a server push.
- Treat `data` as immutable: the same object may be on two screens.

## Failures

- **Render errors** are caught by `AppErrorBoundary` (around everything, in
  `main.tsx`; its fallback uses no context) and `RouteErrorBoundary` (around the
  routed page, inside the shell, in `Layout.tsx`; moving to another screen clears
  it). Both show "Something went wrong" with Reload and Go to dashboard, the
  request id of a failed API call as a reference, and the error message folded
  under "Technical details". API errors that a page handles (`ErrorState`) never
  reach a boundary; `ErrorState` itself quotes the request id for server faults.
- **Lazy screens** use `lazy` from `src/lib/lazy.ts` (same call as React's). A
  chunk that fails to load - an open tab after a deploy - triggers one hard
  reload, guarded by a sessionStorage timestamp (no second automatic reload within
  60 s) and only after a HEAD request shows the server is reachable, so an
  offline tab is not reloaded into the browser's error page. The optional second
  argument is the route path, so hovering or focusing a sidebar link prefetches
  the screen: `lazy(() => import("./pages/X"), "/x")`.
- **Toasts** (`useToast()(message, tone)`): confirmations stay 5 s, warnings 8 s,
  errors until dismissed; every timer waits while the pointer is over the toasts,
  while focus is in them, and while the tab is hidden. The same message raised
  again restarts the toast on screen instead of stacking a copy; at most five show.

## Shell conventions

- The top bar names a top-level screen alone; a deeper page names its trail with
  `usePageTrail([{ label: "Documents", to: "/documents" }, { label: title }])`.
- Keyboard: Ctrl/⌘+K quick find, `?` the shortcuts sheet, `/` the page's search
  or question box - mark it with `data-primary-search` (otherwise the first
  `input[type="search"]` in the page, or Ask's composer is used). Neither `?` nor
  `/` acts while a field has focus or a dialog is open.
- Headline numbers use `StatTile` (ui.tsx): a skeleton while `loading`, a dash
  only when the value could not be loaded.

## Bundle size

`npm run size` after `npx vite build` lists every JS and CSS asset raw and
gzipped (as nginx sends it) and fails if the first load - what `index.html`
loads itself - exceeds 180 kB gzipped (about 125 kB today). Only react,
react-dom, react-router and scheduler are forced into the `vendor` chunk
(`vite.config.ts`); a library imported only by a lazy screen stays in that
screen's chunk.
