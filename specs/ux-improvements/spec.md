# UX improvements: the town view and the exploration view

The plan of record for reworking the town and exploration experiences. It came out of a
full review (code read plus a live browser walkthrough of both views) and is executed in
four phases, each landing as its own PR.

## Design constraints

These bound every phase. They restate settled direction — none of them are new decisions.

- **The viewport stays big and image-ready.** A later feature will render images in the
  viewport frame during encounters and battles, Bard's Tale style — the monster or the
  moment, shown where the wireframe stands in exploration. The exploration wireframe
  itself is permanent; images join the rotation for the other modes, they never replace
  it. Nothing in these phases may shrink the viewport frame, demote it, or repurpose its
  space in any mode. Concretely: the frame keeps its rail-width 4:3 footprint in every
  mode (town included), and text stays in DOM overlays (nameplate, facing, note) rather
  than drawn into the canvas, so a mode can swap what fills the frame without redoing the
  chrome.
- The chrome stays quiet and modern: existing tokens only (`:root` in `style.css`), amber
  accent for light/treasure/primary, red for danger only, the three type voices as
  assigned. No new colors, no themed decoration.
- Controls use boring, well-known patterns. Flavor lives in content, not in widgets.
- The transcript keeps the main column in dungeon modes. The town-mode layout change in
  phase 1 is scoped to town.
- The wire contract holds: only player-safe data crosses. Every proposed server extra in
  this plan is something a B/X player reads off their own sheet or rulebook (spell slot
  counts, whether they slept, posted service prices) — call that out explicitly in review
  whenever a payload field is added.
- The app stays presentation-only. Every new verb is a pass-through to an existing osrlib
  command; no rule re-implementation.

## Findings the plan answers

Verified defects, from a live walkthrough:

- Town selects forget the player's choice: every `command()` rebuilds the context panel,
  resetting the provisioner buyer (which also re-sorts by purse), the temple member and
  service picks, and the spell-prep picks. Battle already persists `S.declTarget`; town
  got nothing.
- Multi-slot casters can never prepare more than one spell: Prepare disables on
  `!!caster.memorized_spells.length`, though the engine's `prepare_spells` takes a
  `selections` list.
- Engine verbs with no UI at all: `cast_spell` outside battle (a cleric cannot heal while
  exploring), `use_item`, `inspect_treasure`, `remove_treasure_trap`, `wedge_door` (the
  provisioner sells iron spikes that cannot be used), `reorder_party`,
  `extinguish_source`.
- Authored content never shown: `PlayerView.town_services` and the adventure `hooks` are
  absorbed by the client and rendered nowhere in-game.
- Transcript spam: repeated identical mechanics lines (six consecutive "The tinder fails
  to catch.") render as separate lines and read like a hang.

Structural problems:

- In town the stage sits nearly empty while every interaction crams into the rail below
  the fold, behind a scroll past the party roster.
- The action bar reflows as contextual verbs (door, treasure, torch, leave) appear and
  vanish, moving persistent buttons under the pointer.
- Auto-picked actors (Search, Find traps, Force, Listen, Pick lock) are invisible, and
  Pick lock's permanent lockout risk is unwarned.
- "Sell treasure (3)" shows a count, not worth, and sells everything from everyone in one
  unconfirmed click.
- Temple prices and the goods list are hardcoded in `app.js`, duplicating the engine's
  `HEALING_SERVICES` table; the shop's six consumables mean a veteran party can never
  rearm or rearmour.

## Phase 1 — the town earns its screen

Town mode only. The transcript keeps its column; the empty stage acreage takes a town
presence built from the same panel styling that exists today.

- Render the town's content in the stage beside/above the transcript: the town
  description and the authored `town_services` prose in the serif voice, and the temple /
  provisioner / rest-and-trade panels as cards in the stage rather than rail stack.
- Render adventure `hooks` as rumors in the town presence — that is where a B/X table
  hears them.
- The rail keeps the viewport frame (unshrunk — see constraints), the enter-dungeon
  action, and the party roster.
- The transcript remains live and visible in town: command results still land there.
- Update `AGENTS.md`'s layout paragraph to record the town-mode exception.
- Recapture any README screenshot whose content moved (the screenshots harness owns
  this).

Client-only; `view.town_services` and `hooks` already cross the wire.

## Phase 2 — town mechanics

- Persist town control state across renders (buyer, temple member and service, prep
  picks), the way battle persists `S.declTarget`. Stop re-sorting the buyer list on every
  render.
- Slot-aware spell preparation: show prepared M of N and allow preparing up to the slot
  count in one flow. Needs a player-safe `sheets` extra for slot counts, and a
  player-safe "rested" flag so Prepare can say "rest first" up front instead of rejecting
  after the click.
- Sell treasure with eyes open: show the appraised total on the button ("Sell treasure —
  340 gp"), confirm before selling all, and add per-item sale from the character sheet's
  carry section (town mode).
- Serve the temple service list and provisioner goods from the server (the engine's
  `HEALING_SERVICES` and the equipment catalog) instead of hardcoding them in the client.
  Widen the shop beyond six consumables so veterans can replace weapons and armour.
- Tag conditions that temple services answer (poisoned, diseased, dead) in the temple's
  member options, not just hp.
- Give "Rest the night" a one-line statement of what it does.

Server work: player-safe sheet extras (slots, rested), a town commerce catalog in the
payload or a small endpoint. Each new field gets the leak review.

## Phase 3 — the missing exploration verbs

Thin pass-throughs to existing engine commands; the engine stays the sole rule owner.

- **Cast while exploring** — the priority. A Cast control for members with prepared
  spells, with member targeting for curative spells (reuse the spellbook metadata the
  battle declarations already use).
- **Use item** for usable inventory (potions and kin), from the sheet's carry section.
- **Inspect treasure** beside Take treasure, and **Disarm** (`remove_treasure_trap`) when
  a trap is known — the thief's check-and-disarm play.
- **Wedge door** (consumes an iron spike) on door edges, both to hold one open and to
  spike one shut.
- **Marching order**: reorder members from the party panel (front two are the melee rank
  and the defaults already assume it).
- **Extinguish** a burning light source.

## Phase 4 — exploration polish

- Stabilize the action bar into fixed zones: movement cluster, then persistent verbs
  (Search, Find traps, Rest, Map) in unchanging positions, contextual verbs (door,
  treasure, stairs, torch, leave) in their own reserved row.
- Name the auto-picked actor on the button ("Search — Aravel"); state Pick lock's lockout
  stakes in its title.
- Collapse consecutive identical transcript lines client-side ("The tinder fails to
  catch. ×6"); the server transcript stays byte-identical.
- "Descend the stairs" / "Climb the stairs" from `cell.transition`'s kind.
- Reconsider the turn-around glyph (▼ reads as "step back").
- Optional, engine-gated: stairs and entrance glyphs on the automap. `ExploredLevelView`
  carries only cells and edges today; explored-transition visibility belongs in the
  engine's projection first.

## Verification, per phase

- `uv run pytest` green; new server logic beyond pass-through brings tests in the
  existing style.
- Browser walkthrough of the affected flows against the dev server, plus
  `playwright-cli console` staying empty.
- `app.js?v=N` bumped on every `app.js` edit.
- `AGENTS.md` updated when a fact it states changes; README screenshots recaptured when a
  change moves what a shot shows.
- The user's server on port 8620 is bounced (detached) after server-side changes.
