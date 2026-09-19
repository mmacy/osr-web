# The Table — front-end redesign plan

A reskin of the osr-web client to "The Table": a map-first, graph-paper-and-pencil interface with two themes — daylight (a fresh quadrille pad) and candlelight (a toned charcoal sheet under a lamp). This document is the implementation handoff. It assumes no prior context beyond the repo and [AGENTS.md](../../AGENTS.md).

## Goal

Replace the current "silverpoint on indigo night" look with "The Table" across the whole client, in both light and dark themes, driven by one set of CSS custom-property tokens plus a persisted toggle that follows the OS setting by default. The design thesis: **the hand-drawn map is the hero.** Old-school play happens on paper, so the screen is the referee's playing surface — the automap is promoted to the centerpiece, the first-person view becomes a small pencil-sketch inset, the party is a stack of character-sheet cards, and the transcript is a typed referee's log with the AI storyteller's passages set in a second, handwritten voice.

## Reference material

Render these and match them; they are the visual source of truth.

- [`mockups/exploring-light.html`](mockups/exploring-light.html) and [`mockups/exploring-dark.html`](mockups/exploring-dark.html) — the exploring screen in both themes, using real party, adventure, and transcript at turn 14 mid-search. PNG captures sit beside them.
- The mockups link Courier Prime and Caveat from Google Fonts for convenience; the shipped app must self-host both (see [Typography](#typography-and-fonts)).
- The mockups only show **exploring** mode. Town, encounter, and battle need the same treatment applied to their existing panels — see [Layout per mode](#layout-per-mode).

## Constraints (read before touching anything)

- **Client only.** This is `static/index.html`, `static/style.css`, `static/app.js`, and self-hosted fonts. Do not change the server or the wire contract. Do not add response fields (AGENTS.md "The wire contract").
- **Bump the cache-buster.** `index.html` loads `app.js?v=N`; increment `N` on every `app.js` edit or the change silently won't load (AGENTS.md "Client gotchas").
- **No new invented data.** The player projection exposes explored cells, wall/door edges, party position and facing, and the current cell's `area_name` — nothing else about the map. Per-room numbers and trap/secret marks in the mockup are illustrative only; do not render them unless a real, player-safe data source is confirmed (see [Questions and answers](#questions-and-answers)).
- **Keep it dependency-free and offline-capable.** No CDN links in the shipped client. Self-host fonts.
- **Sentence case** for all user-facing strings.
- **Update the docs** that describe the old design system (see [Docs to update](#docs-to-update)) — otherwise AGENTS.md will actively mislead the next contributor.

## Design tokens

One token set, two value maps. Define on `:root` (daylight is the default), redefine under `@media (prefers-color-scheme: dark)`, then redefine again under `:root[data-theme="dark"]` and `:root[data-theme="light"]` so the manual toggle wins over the OS in both directions. Style every component through the tokens — never hard-code a hex in a rule, and never put colors only inside the media query.

Semantic roles (what each token means, so the two value maps stay coherent):

| Token | Role |
| --- | --- |
| `--paper` | Page ground — the graph paper |
| `--surface` | Cards, plates, buttons (a slightly lifted paper) |
| `--grid` / `--grid-strong` | Minor / major graph rules |
| `--pencil` | Primary line and text: body type, borders, map walls, the sketch |
| `--pencil-dim` | Secondary text: class labels, mechanical log lines, captions |
| `--ink` | The blue hand: annotations, handwritten headings, links, compass, **the storyteller's voice** |
| `--ink-dim` | Blue borders and marks |
| `--red` | Danger and red-pencil marks: encounters, low HP, "back to town" |
| `--red-dim` | Softer red for borders |
| `--gold` | Treasure, torch, gold, the candle's warmth |
| `--gold-dim` | Gold borders |
| `--tape` | The masking-tape flourish on plates |

Daylight values (from `exploring-light.html`):

```css
:root{
  --paper:#e9e2ce; --surface:#efe9d8;
  --grid:rgba(52,88,124,.16); --grid-strong:rgba(52,88,124,.30);
  --pencil:#2a2822; --pencil-dim:#6c6759;
  --ink:#22507e; --ink-dim:#325f8a;
  --red:#b23524; --red-dim:#c1503f;
  --gold:#8a6a1e; --gold-dim:#9c7a2a;
  --tape:rgba(214,201,150,.55);
  --map-glow:transparent;                 /* no lamp pool in daylight */
  --card-shadow:2px 3px 0 rgba(42,40,34,.18);
  --hover-surface:#fff;
}
```

Candlelight values (from `exploring-dark.html`):

```css
/* apply to @media (prefers-color-scheme: dark) AND :root[data-theme="dark"] */
--paper:#17140d; --surface:#211c13;
--grid:rgba(112,142,182,.10); --grid-strong:rgba(112,142,182,.18);
--pencil:#d8d0ba; --pencil-dim:#a49b81;
--ink:#7ba9d6; --ink-dim:#6f9cc8;
--red:#e15c43; --red-dim:#e87a63;
--gold:#e0aa4e; --gold-dim:#b98a34;
--tape:rgba(190,165,105,.22);
--map-glow:rgba(232,172,92,.13);          /* warm candle pool over the map */
--card-shadow:3px 4px 0 rgba(0,0,0,.45);
--hover-surface:#2b2517;
```

The candlelight ground also carries a deeper page vignette and a warm radial pool centered on the map (see the `.map-card` background and `body::after` in `exploring-dark.html`). Both are expressed through `--map-glow` and shadow tokens so they vanish cleanly in daylight.

Verify both maps meet WCAG AA (4.5:1) for body text and (3:1) for large text and UI borders; `--pencil-dim` on `--surface` is the tightest pairing in each theme — check it.

## Typography and fonts

Two voices, mapped to two faces:

- **The engine / referee / module** is *typed*: **Courier Prime** (the 1981 module, retyped), in `--pencil`. This carries the mechanical log lines, room prose, place headers, HUD, party stats, buttons, and panel bodies.
- **The AI storyteller (LLM narration) and the player's own hand** are *handwritten*: **Caveat**, in `--ink`. This carries the `.narrative` transcript passages, the map's handwritten labels/legend, card names, and the "the referee's table" sub-title.

Self-host both (both are SIL Open Font License — free to bundle). Steps:

1. Fetch `Courier Prime` (regular, bold, italic) and `Caveat` (400–700) as `woff2`. Optionally subset to Latin to keep them small.
2. Place under `static/fonts/` and declare `@font-face` with `font-display:swap` in `style.css`.
3. Define the stacks as tokens with real fallbacks:

   ```css
   --type: "Courier Prime", "Courier New", ui-monospace, monospace;
   --hand: "Caveat", "Segoe Print", "Bradley Hand", cursive;
   ```

Keep the fallbacks sane — if Caveat fails to load, the hand voice degrades to a system script face rather than the body font, so the two voices stay distinguishable.

## Layout per mode

The frame is constant; the hero region adapts to `S.view.mode`. Drive it with a `data-mode` attribute on `#game` (or `<body>`) and switch grid content via CSS, keeping one DOM skeleton. No page scroll (match today's fixed 100vh behavior); each scrollable region scrolls internally.

New skeleton (three zones):

- **Header (full width):** brand plate (adventure name + "the referee's table" + current dungeon level) · HUD stamps (turn, gold, torch — colored per state) · right-side links (save the game, new party, **and the theme toggle**).
- **Hero (left, `1fr`):** mode-dependent.
  - `exploring` (in a dungeon): the big graph-paper **map** (enlarged automap over the CSS grid) with the **action bar** beneath it (compass moves, search, take, light torch, rest, back to town).
  - `town`: the town "papers on the table" — town description, temple, provisioner, before-the-road/camp, and one enter button per dungeon (reuse `renderTownPanel` content, restyled). The map is hidden in town, as it is today.
  - `encounter` / `battle`: the **first-person scene** (the viewport canvas enlarged — monsters approaching, drawn in pencil, red for danger) as the hero, with the encounter/battle **panel** (groups, distance, stance, per-member declaration rows) beneath it.
- **Rail (right, fixed ~452px, constant):**
  - the **party clipboard** (six character-sheet cards) — always;
  - a **context slot**: in `exploring`, the small first-person **pencil-sketch** viewport plus the "in this place" context; in other modes, the relevant compact context;
  - the **typed referee's log** (transcript) — always, filling remaining height.

The single `<canvas id="viewport">` moves between the small rail slot (exploring) and the large hero slot (encounter/battle) via CSS placement keyed on `data-mode`; its internal resolution stays fixed and CSS scales it. The single `<canvas id="automap">` lives in the hero slot and is shown only in dungeon modes.

## Component restyle map

Existing classes keep their names and JS wiring where possible; only their CSS treatment changes. Apply the tokens; the mockups show the target for each.

| Current | New treatment |
| --- | --- |
| `#topbar` `.brand` | Brand **plate**: bordered `--surface` card with a rotated masking-`--tape` corner; module/level eyebrow in `--red`, title in `--type` uppercase, sub-title in `--hand`/`--ink` |
| `.hud .hud-item` | Rubber-stamp **chips**, slightly rotated, bordered; `#hud-mode` neutral, `#hud-gold` in `--gold`, `#hud-light` in `--gold` when lit / `--red` when unlit |
| `.topbar-actions .btn.ghost` | Underlined `--hand`/`--ink` links ("save the game", "new party"); add the theme toggle here |
| `.viewport-frame` / `#viewport` | Pencil-sketch card on `--surface` with a tape corner; canvas redrawn as graphite/bone line art on paper (see [Canvas theming](#canvas-theming)) |
| `#nameplate`, `#viewport-note` | `--hand` caption under the sketch; note ("it is dark…") in `--pencil-dim` |
| `.actions .btn` (`.move/.accent/.gold`) | Pencil **buttons**: `--surface`, `--pencil` border, hard offset `--card-shadow`; key hints in `--red`; `.accent`/warn in `--red`; hover lifts to `--hover-surface` |
| `#log .log-entry` | The **typed log**: `.place`/`.prose`/`.mech`/`.treasure`/`.danger`/`.system`/`.reject` in `--type`; `.place` underlined; `.mech` in `--pencil-dim`; `.treasure` with a `--gold` underline; `.danger` in `--red`; **`.narrative` (the bard) in `--hand`/`--ink`** with a margin rule |
| `.party .member` | Character-sheet **cards**: name in `--hand`, class in `--type` `--pencil-dim`, HP numeric `cur/max` + restyled `.hp-bar`/`.hp-fill` (see caveat below); `.member-tag` marks in `--red` |
| `.panel` / `.panel.danger` | Table "papers": `--surface` card, `--type` heading; danger panels bordered `--red` |
| `.group-chip`, `.decl-row`, `select` | Restyle to tokens; keep structure and JS |
| `.map-frame` / `#automap` / `#map-caption` | Promoted to the hero: large graph-paper map card; caption in `--hand` |
| `.overlay` (title / game over) | Reskin `.overlay-*` to tokens; title in `--type`, hooks/sub in `--hand`/`--ink` |
| `.overlay-card.wizard` and all `.wiz-*`, `.class-card`, `.ability-table`, `.shop-*`, `.kit-*`, `.house-rules` | Reskin to tokens (own phase; largest surface after the in-game screen) |
| `.toast` | `--surface` card, `--red` (error) / `--gold` (ok) border |

HP display caveat: the mockup's filled/hollow pips only work at level 1 (low HP). Veterans carry levels between modules, so max HP can reach the 20s and pips break. **Use numeric `cur/max` plus the restyled `.hp-bar`** (pencil-filled, with `hurt`/`grave` color states), and treat pips as optional decoration only when `max_hp` is small (≤ 8).

## Canvas theming

The two canvases draw with hard-coded colors today (`COLORS` object at `app.js:43` plus inline hex/rgba throughout `renderViewport`, `drawCorridor`, `drawMonsters`, `renderAutomap`, `drawGateScene`). Canvas can't read CSS variables directly, but the code already reads one token for a font (`getComputedStyle(document.body).getPropertyValue("--mono")` at ~line 767). Generalize that:

1. Add a `readPalette()` that pulls every drawing token off `:root` once per render:

   ```js
   function readPalette() {
     const s = getComputedStyle(document.documentElement);
     const c = (n) => s.getPropertyValue(n).trim();
     return {
       paper: c("--paper"), surface: c("--surface"),
       pencil: c("--pencil"), pencilDim: c("--pencil-dim"),
       ink: c("--ink"), red: c("--red"), gold: c("--gold"),
       grid: c("--grid"),
     };
   }
   ```

2. Replace the `COLORS` object and every inline canvas color with palette lookups. Map old roles to new: night/deep/vault grounds → `paper`/`surface`; silver/bright line work → `pencil`; dim → `pencilDim`; ruby → `red`; torch → `gold`; the blue orientation elements (compass, party arrow can stay `red`) → `ink`.
3. Drop the glow: set `shadowBlur = 0` (or a hair) and stop using `strokeGlow`. The Table is matte pencil, not phosphor.
4. Redraw on theme change: the toggle calls the top-level `render()`, which already re-runs `renderViewport()` and `renderAutomap()`. Re-read the palette at the top of each so the new theme's colors take effect.

Map-as-hero canvas changes (`renderAutomap`):

- Enlarge the canvas backing store to its container and scale for `devicePixelRatio` so pencil lines stay crisp at hero size (today it's 312×404 with `size` capped at 18px/cell).
- Make the canvas **transparent** (`clearRect`, no ground fill) so the CSS graph-paper grid shows through; draw only translucent room fills, pencil walls, door marks, and the party arrow. Walls in `pencil`, doors in `gold`, the party arrow in `red` — all from the palette.
- Keep annotations to real data only: walls, doors, explored cells, party position/facing, and optionally the current `area_name` near the arrow. No fabricated room numbers or trap/secret marks.

Viewport-as-sketch changes (`drawCorridor` and friends):

- Recolor to `pencil` on `paper`, no glow. Recolor `drawGateScene` (town) and `drawMonsters`/`drawFigure` (encounter/battle, `red` for the threat) to the palette too.
- Ship clean thin pencil strokes with no glow; do not invest in canvas line-jitter to mimic the mockup's `feTurbulence` hand-drawn wobble (settled — see [Decisions](#decisions)).

## Theme toggle

- Control: a small link/button in the header right group, labeled by what it switches **to** ("candlelight" while in daylight, "daylight" while in candlelight), or a sun/candle glyph with an accessible label.
- Behavior: on click, flip `--data-theme` between `light`/`dark` on `:root`, persist to `localStorage` (e.g. `osrweb_theme`), and call `render()` so the canvases repaint.
- Default: no stored value → follow the OS via `prefers-color-scheme` (the media-query token map handles this with no JS). Only stamp `data-theme` once the user chooses, so the OS default keeps working until then.
- Respect `prefers-reduced-motion` (already in `style.css`); no transition on the theme swap beyond what's already allowed.

```js
function applyTheme(t) {                 // t: "light" | "dark" | null(=follow OS)
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  if (t) localStorage.setItem("osrweb_theme", t);
  render();                              // repaint canvases with the new palette
}
// on boot: const saved = localStorage.getItem("osrweb_theme"); if (saved) applyTheme(saved);
```

## JS changes summary

- `readPalette()` + replace `COLORS`/inline canvas colors (above).
- Theme toggle: control, `applyTheme`, boot restore, repaint on toggle.
- Layout: set `data-mode` on the layout root each render; retarget the existing render functions (`renderActions`, `renderContext`, `renderViewport`, `renderAutomap`, the town/encounter/battle panels) into the new hero/rail containers; move the viewport canvas placement between rail (exploring) and hero (battle) via CSS.
- HUD: derive the brand from `S.view.adventure_name` + level; turn is a plain number (no tally marks — they don't scale); gold and light from real state.
- Party: numeric HP + restyled bar (no fixed pip count).
- Bump `app.js?v=N` in `index.html`.

No change to game logic, commands, polling, or the narration client — only rendering and theming.

## Docs to update

- **AGENTS.md → "Style"** (line ~87): replace the indigo/silver/ruby/torch token description with The Table's tokens and the two themes, and the two-voice mapping (typed engine / handwritten storyteller). The current text tells contributors to reuse the *old* tokens — leaving it will actively mislead.
- **README.md → "Design notes"** and the `style.css` header comment: rewrite the palette rationale for The Table.
- Keep `specs/table-redesign/` as the living reference.

## Phasing

Each phase is independently verifiable; land and check before the next.

1. **Token foundation + fonts.** Self-host Courier Prime and Caveat. Define the token set and both value maps with the toggle plumbing. No visual layout change yet — just prove the tokens/toggle/fonts load and both themes swap.
2. **In-game exploring screen.** The map-first layout, the plates/stamps/buttons, the typed log with the handwritten `.narrative` voice, the party cards, the pencil-sketch viewport. Canvas theming for automap + viewport. This is the screen in the mockups — match it in both themes.
3. **Town, encounter, battle.** Apply the hero-per-mode layout and restyle the town/encounter/battle panels and the enlarged battle viewport scene.
4. **Overlays + wizard.** Title, game over, and the roll-your-own party builder — the biggest remaining surface — reskinned to tokens.
5. **Polish + a11y + docs.** Focus-visible states on every control, reduced-motion honored, toast, responsive breakpoints, contrast audit both themes, and the doc updates above.

## Verification

Run against a seeded game so states are reproducible (AGENTS.md "Run and verify"):

- Start the server (`uv run uvicorn server.app:app --port 8620`) and bounce it after changes; drive with playwright-cli or `urllib`.
- `POST /api/games {"seed": 42}` for a deterministic session; from the entrance at (3,3), **three moves east reaches a keyed skeleton encounter** — use it to exercise encounter and battle. *(Route as of 2026-09-02: the bundled document is now* The Cold Vein *— entrance (2,15), three moves north to four kobolds at (2,12); see AGENTS.md.)*
- Check every mode in **both themes**: title/overlay, town (temple/provisioner/camp, dungeon enter), exploring (map hero, sketch, log, actions), encounter, battle (declarations, rejection surfacing), game over, and the creation wizard.
- Toggle the theme mid-session and confirm both canvases repaint (no stale indigo). Reload the page and confirm the theme choice persists and the transcript rebuilds.
- Confirm the storyteller voice is visually distinct: with a narrator configured (or by injecting a `.narrative` entry), a passage should render in the handwritten `--ink` voice, not the typed voice.
- Accessibility: visible keyboard focus on all controls; `prefers-reduced-motion` kills transitions; check text contrast in both themes.
- `uv run pytest` stays green (narration tests are unaffected by a reskin, but run them to be sure nothing in the client contract shifted).

## Questions and answers

Sharp questions asked against this plan, and how it answers them.

- **Does the map have room numbers and trap/secret marks like the mockup?** No. The player projection gives cells, wall/door edges, party position/facing, and the current cell's `area_name` — nothing per-cell about identity or hidden features (secrets are referee-only until found). Those mockup marks are illustrative. The plan renders only real data and says so explicitly; adding fabricated marks would also risk leaking referee state the moment someone wired them to real geometry.
- **Do HP pips and the turn tally scale?** No. Veterans carry levels between modules, so HP reaches the 20s and the pip row breaks; turn counts reach the hundreds. The plan uses numeric HP + a bar and a plain turn number, keeping pips/tally as small-value decoration at most.
- **Is a module name safe to hard-code?** No. The library plays any adventure document. The brand derives from `adventure_name` + level.
- **How do town and battle work — the mockup only shows exploring?** The hero region is mode-adaptive: map in dungeon, town papers in town, the enlarged first-person scene + declarations in encounter/battle. The rail (party + context + log) is constant. This is called out as its own phase because the mockup doesn't cover it.
- **Can the canvases even do two themes?** Yes — the code already reads one CSS token into canvas; the plan generalizes that to a `readPalette()` and repaints on toggle via the existing `render()`.
- **Does self-hosting fonts break "dependency-free"?** No — self-hosting keeps the client offline-capable with no CDN. The mockups' Google Fonts links are reference-only and must not ship.
- **Biggest risk?** The layout inversion touching `renderActions`/`renderContext`/viewport/automap placement across four modes. Mitigation: phase 1 lands tokens/fonts/toggle with no layout change, phase 2 does exploring only, phase 3 handles the other modes — so a regression is contained to one mode at a time and verifiable against seed 42.
- **Door caveat.** An adventure with no door edges shows no door pencil marks on its map, so door rendering is checked against an adventure that has doors.

## Decisions

These are settled; build to them.

1. **Fonts — self-host.** Bundle Courier Prime + Caveat as local `woff2` (both OFL); keeps the client offline-capable and dependency-free. No CDN links ship.
2. **Default theme — follow the OS.** With no stored preference, follow `prefers-color-scheme`; the toggle sets a persisted manual override that wins thereafter.
3. **Wizard — in scope.** The roll-your-own party builder is reskinned in this effort (phase 4), so the whole client is consistent.
4. **Pencil roughness — clean strokes.** Ship clean thin pencil strokes; do not invest in canvas line-jitter to mimic the mockup's `feTurbulence` wobble.
