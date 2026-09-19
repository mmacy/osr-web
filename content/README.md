# The bundled adventure

`adventure.json` is the document the app boots into when `OSR_WEB_ADVENTURE` is unset. It is the
first thing anyone who clones this repository plays, so it is authored to be played, not merely to
validate.

## Provenance

"The Cold Vein" — a silver mine above a smelting hamlet called Cinderhope, two levels, twenty-one
keyed areas.

It is original work authored for this repository. Every proper noun, area name, description, hook,
town blurb, service line, feature description and piece of map geometry in the file was written from
scratch for this purpose. It contains no text, names, maps, boxed text, or keyed content from any
published module — not B1–B12, not the Dungeon Crawl Classics line, not Necrotic Gnome's catalogue,
not any other retail product — and it is not derived from, adapted from, or a paraphrase of one.

The only third-party content it touches is by reference: `template_id` strings (`kobold`,
`giant_bat`, `giant_rat`, `cave_locust`, `skeleton`, `zombie`), equipment ids (`torch`, `oil_flask`,
`lantern`, `tinder_box`, `wine`, `rope`, `sack_large`, `hammer`, `holy_water`), the kobolds' lair
treasure type and the `C` and unguarded treasure bands — all of which resolve against osrlib's
shipped OGL SRD catalogs at runtime. No SRD prose is copied into the file. The two items the
document bundles itself, `verrow-token` and `captains-day-book`, are original, as is the quest that
sends the party after the second one and the headman who offers it: a bundled id names a thing that
exists for this adventure alone.

## Shape

The document is a stamped osr-forge envelope: `kind: "adventure"`, `schema_version: 3`,
`engine_version: "1.6.0"`. It loads through `check_document(document, "adventure")` followed by
`Adventure.model_validate(payload)`, and passes `validate_adventure` against the shipped monster and
equipment catalogs with no dangling references.

The stamp is load-bearing, in both halves. An adventure envelope is checked but never migrated, and
`Adventure` ignores keys it does not know — so a schema-2 stamp on a document carrying `items`,
`triggers` and `quests` would load happily on a current engine and be silently stripped to an
unwinnable shell by anything older. Stamped 3, an older engine refuses it out loud instead.

The engine version is the narrower claim, and it moved to `1.6.0` when the quest arrived. The
document authors `ObjectiveSpec.name` — a field osrlib added in 1.6.0 — and 1.5.0 would load the
file without a word of complaint and quietly show both objectives by their slugs. The stamp names
the engine the document is actually written for.

`server/content.py::load_adventure` and `server/library.py::add_adventure` both run
`validate_adventure` now, so an authored typo — a cache naming an item that does not exist, a
trigger pointing at a level that does not — fails at load or upload as a 422 rather than loading
clean and quietly never firing.

| | Level 1 — the upper workings | Level 2 — the deep drift |
| --- | --- | --- |
| Grid | 20 × 16 | 18 × 14 |
| Keyed areas | 13 (ids `1`–`12` and `21`) | 8 (ids `13`–`20`) |
| Entrance | (2, 15), area 1 | none — reached by the winze |
| Wandering | `0`-in-6 every 2 turns | `1`-in-6 every 2 turns (RAW) |

Area `21` is on level 1 despite its number: area ids are level-scoped but the document numbers them
across the whole dungeon, and `13`–`20` were already spent on level 2 when the saint's cupboard was
added.

Level 1's wandering chance is authored at `0` so the opening rooms cannot be interrupted by a random
level-1 encounter (the SRD level-1 table includes two acolytes at AC 2, which will kill a fresh
party). It is not silence: `wandering_check` adds +1 for noise since the last check and a battle is
noise, so a check *can* still fire in the two turns after a fight. Level 2 is RAW, and dangerous.

## What the document exercises

| Feature | Where |
| --- | --- |
| Entrance | level 1, cell (2, 15), area 1 "The adit mouth" |
| Keyed encounter, pinned to attack | level 1, area 2 "The dressing floor" — 4 × `kobold` |
| Keyed encounter, rolled count | level 1, area 8 "The high stope" — `1d3` × `giant_bat` |
| Keyed encounter, rolled reaction | level 1, area 12 "The flooded heading" — 3 × `giant_rat` |
| Keyed encounter with a lair hoard | level 1, area 10 "The scratch-den" — 5 × `kobold`, `hoard: true` |
| Keyed encounter, undead | level 2, area 16 "The walled rise" — 3 × `skeleton` |
| Keyed encounter, the climax | level 2, area 18 "The cold face" — 2 × `zombie` |
| Plain door | level 1, edge `7,11:north` (into the crib) |
| Stuck door | level 1, edge `13,10:north` (the magazine); level 2, edge `2,4:north` (the nook) |
| Locked door | level 2, edge `4,4:north` (the company's grille) |
| Secret door | level 1, edge `17,5:north` (the slab into the captain's office) |
| Gated door | level 1, edge `11,3:north` (the lead wicket into the saint's cupboard) |
| Bundled items | `verrow-token` ("Saint Verrow's token") and `captains-day-book` ("the captain's day-book") — gear templates in `payload.items` |
| Authored trigger | `adit-draught`, an `area_entered` trigger on level 1, area 1 |
| Concluding quest | `the-day-book` in `payload.quests` — two visible objectives, `completion: "all"`, `concludes_adventure: true` |
| Narrator guidance | both levels' ambient slots, plus the quest's, the gate's and the trigger's blocks |
| Room trap | level 1, area 5 — falling rock, `1d4`, save vs. breath for half |
| Room trap, a pit | level 2, area 17 — 10-foot fall, save vs. breath negates |
| Treasure trap | level 1, `captains-strongbox` — powder flash, `1d4` and blind `1d4` turns |
| Authored caches | `crib-jar`, `candle-tally-box`, `offering-ledge`, `plunder-sack`, `captains-strongbox`, `weighing-box`, `verrow-plate`, `captains-board` |
| Generated area treasure, unguarded | level 2, area 15 "The drowned stope" |
| Generated area treasure, by letter | level 2, area 18 "The cold face" — treasure type `C` |
| Construction trick | level 1, `shaft-hatch` in area 3 |
| Custom feature | level 2, `roll-of-names` in area 20 |
| Level transition, both ways | level 1 `stairs_down` (18, 1) ⇄ level 2 `stairs_up` (2, 12) |
| Wide rooms for the viewport | area 8 is 5 × 4, area 20 is 7 × 2, area 18 is 4 × 4, area 15 is 5 × 3 |
| Long corridors for the viewport | the main drift, x = 2 from y = 9 up to y = 5; the haulage cross-cut, y = 8 from x = 3 to x = 6; the east drift, y = 8 from x = 11 to x = 15 |

## The authored layer

The document carries four pieces of osrlib's authored layer — bundled items, a gated door, a
trigger, and the concluding quest — so every surface that renders it triggers against the default
adventure.

**The bundled items.** `verrow-token` ("Saint Verrow's token") is a `gear` template in
`payload.items`. It joins the equipment catalog for this adventure's sessions only, which is why
`Narrator` resolves item names through `session.effective_equipment` rather than the shipped lists —
against the shipped lists the token printed as its raw id everywhere it was named. The town shop
never stocks it: `PurchaseEquipment` reads the shipped lists alone, and the authored `cost_gp: 0` is
the convention for an item that is found rather than bought, not a price. The token lies on the
`offering-ledge` cache in area 9, among the offerings the saint's niche has collected.

Every word of that applies equally to the second bundled item, `captains-day-book` ("the captain's
day-book"), which is the quest relic and is described with the quest below.

**The gated door.** Edge `11,3:north` — the lead wicket into area 21, "The saint's cupboard" — hangs
a `has_item` gate on `verrow-token` with an authored refusal and success beat. The refusal renders
verbatim as the command's rejection line; the success beat rides the `DoorEvent` and renders in the
prose voice under the mechanical line.

Three authoring decisions worth keeping:

- **The gate is non-consuming** (`consumes: false`), so the token stays a keepsake and the door
  reopens after it swings shut. The consuming (toll) form is exercised in the test suite against an
  in-memory copy of this document; the shipped document authors no toll.
- **The gate is a door, not a transition.** A transition gate's success beat rides the arrival
  event, and a same-level transition emits no arrival event at all — the beat would simply vanish.
- **The cupboard is a side room, never a route.** Its only connection to the rest of level 1 is the
  gated door; nothing pre-existing lies behind it, and a party that never finds the token loses
  nothing but the plate on the shelf.

The gate authors no `journal` beat: a gate's journal form has no consumer.

**The trigger.** `adit-draught` is a non-repeatable `area_entered` trigger on level 1, area 1, so it
fires on `enter_dungeon` itself — the entrance cell is inside the adit mouth. It carries no
consequences: no draw, no clock, no state beyond its own fired-mark, so the deterministic route
below is unchanged. Its `journal` beat is the players' line and reaches the transcript and
`view.journal`; its `fired` beat rides a referee-visibility event and reaches only the server log
(`osr_web.referee`), which is where the operator sees `trigger fired: adit-draught — …`.

**The quest.** `the-day-book` ("The Captain's Day-Book") is the document's one quest and the one
that ends the adventure. It collects a promise hook 1 has been making since the file was written:
the headman will stake anyone willing to prove the pay-vein, and pay twice over for the mine
captain's day-book. The relic itself is the second bundled item, `captains-day-book`, and it lies on
the `captains-board` cache in area 18, "The cold face" — the deepest room on level 2, behind the two
zombies keyed there and behind nothing else. No lock, no gate, no secret door: the route rule the
rest of the document follows holds hardest for the one thing the adventure cannot be won without.
The area's generated type-C treasure sits alongside the authored cache, the way areas already mix
features.

The quest's shape, decision by decision:

- **Activation is a `dungeon_entered` clause on `cold-vein`**, so the charge lands at the adit mouth
  the first time the party goes in, beside `adit-draught`. A quest with no activation is active from
  round 0 and announces itself through nothing — there is no event channel before the first command
  — so its offer would stand silently in `view.quests` and never reach the transcript or the
  journal at all.
- **Two objectives, both visible, `completion: "all"`.** `recover-day-book` completes on an
  `item_acquired` clause naming the relic; `bring-it-home` completes on `town_entered` with a
  `has_item` condition on the same id. The acquisition spine is the anti-short-circuit: `has_item`
  alone is satisfied by a relic that was merely carried in, and `item_acquired` never fires for one.
  Belt over braces, `party_from_save` strips a non-shipped template on the way in anyway.
- **Objective matching is edge-triggered.** Walking home without the book fires `town_entered`, the
  condition fails, and nothing retries it — the objective waits for the *next* arrival. That is
  correct and it is survivable: go back up the hill, fetch the book, come home again.
- **Each objective authors a `progress` beat and nothing else.** An objective's `offer` beat rides
  its reveal, and neither objective here is hidden, so an authored `offer` would never be shown.
  The consequence is deliberate: `objective_revealed` never fires in the shipped document, and that
  surface stays exercised by the barrow fixture in `tests/test_quest_log.py`.
- **The quest block authors `offer`, `completion` and a `speaker`** — Headman Orrin Slake, whose
  name appears under the offer in the quests card and nowhere in the mechanics. The offer is written
  to fit two rendered card lines: the exploring rail has room for two `#context` cards and part of a
  third, and a third line here is paid for by the card below.
- **Rewards, in authored order:** `grant_coins` of 100 gp to `@party`, then `award_xp` of 200 to
  `@party`. The selector expands per living member, so both are per head — six survivors take 100 gp
  each, which is how "twice over" reads at a table. Both land *after* the session has already turned
  `victory`, so both have to be legal there; coins and XP are, spawns and placements are not.
  `set_flag` is deliberately not authored: nothing renders a flag and no later content reads one, so
  it would be wiring nobody could see.

**The steering.** The document authors five `guidance` blocks: both levels' ambient slots
(`LevelSpec.guidance`), the quest's block, the gate's, and the trigger's. The two objectives'
slots deliberately stay empty — the quest's own block says what the errand is. Level 1 sets the
register of a working abandoned mid-shift — workaday dread, cold economics, the surface still faintly
present in draughts and daylight; level 2 makes cold the fact of the place and forbids naming its
cause; the quest keeps the errand a debt the town is owed rather than a treasure hunt, and Orrin Slake
flinty and fair; the gate lends the wicket a little parish reverence without ever letting the saint
act; the trigger asks for the draught to read as the mine taking notice without saying so. Every one
of them is steering, not script: none restates a display beat, and none tells the narrator a fact.

`guidance` is referee-side and stays there. `server/narration.py::assemble_guidance` reads it off the
document under the game lock and hands it to the prompt builder as one marked block the model is told
never to quote, reveal, or state as fact. It rides no event, appears in no view, and crosses the wire
on no response — the same trust posture an area's description prose already has, one step more
private, since the prose is text the players are meant to hear and this is not.

The windows are the carriers' own: a level's block steers while the party is on that level, a quest's
while it is active — through the run that completes it — with its revealed-but-incomplete objectives
riding along, a trigger's in the run it fires in, and a door gate's in the run that carries its
success beat.

One honest note about that last one. Steering only reaches a prompt when a beat fires, and nothing in
this document puts a beat on the wicket's opening: the cupboard behind it is a small side room, and
opening a door is not a moment worth prose on its own. So the gate's block is proven at the assembly
seam by `tests/test_narration.py` and waits for a document that spends a beat on a gate's success run
before it colors a live passage.

One deliberate absence remains: `FeatureSpec.magic_item_ids` is unused — nothing in the document
places a magic item by id yet.

## The deterministic route to the first fight

`EnterDungeon` always lands the party on the entrance cell facing **north**, so the whole route is
forward movement.

```
POST /api/games                {"seed": 42}
POST /api/games/{id}/command   {"command_type": "enter_dungeon", "dungeon_id": "cold-vein"}
POST /api/games/{id}/command   {"command_type": "move_party", "direction": "north"}   -> (2, 14)
POST /api/games/{id}/command   {"command_type": "move_party", "direction": "north"}   -> (2, 13)
POST /api/games/{id}/command   {"command_type": "move_party", "direction": "north"}   -> (2, 12)
```

The `enter_dungeon` response carries three lines the room description alone would not. The quest
activates first — `New quest: The Captain's Day-Book.` in the system voice, then the headman's offer
in prose — and the `adit-draught` trigger fires on the entrance area after it, its journal beat
rendering as prose. Both write to `view.journal` as well, in that order. None of it costs time or
draws dice, so it moves nothing below.

The third move crosses into area 2, "The dressing floor". Its keyed encounter — four kobolds — sets
`aware: true` and pins `stance: "attacks"`, so the session lands in `battle` mode on that move with
no encounter menu in between and no branch on the reaction roll.

No wandering check can fire before that. The pregenerated party's `exploration_rate` is 60 feet per
turn, which puts the odometer threshold at 180 thirds — six unexplored cells to the turn — and three
moves spend 90. Measured at seeds 42, 7 and 31337, `view.clock_rounds` does not cross a turn boundary
over the three moves.

In the UI the same route is the Forward button (or `W` / `ArrowUp`) pressed three times.

## Survivability

Measured against the live API, ten seeds (1, 7, 13, 42, 99, 123, 777, 2024, 31337, 65535), the
pregenerated six level-1 party, no house rules:

- Driven by the client's own opening orders (`declDefaults()` in `static/app.js`, which is what a
  player gets by clicking **Resolve round** without touching a select): **0 deaths in 60 characters**,
  battles ending in 1–4 accepted rounds. The elf's *magic missile* and the magic-user's *sleep* carry
  it.
- Driven with no magic at all (front rank melees, back rank shoots only beyond 5 feet): **16 deaths
  in 60 characters**, battles running 1–10 rounds. Four kobolds are a real fight for two front-rank
  swords, and B/X hit points at level 1 are what they are.

One caveat on the first row, and it belongs to the client rather than to this document. Once a
caster's one spell is spent and the kobolds are at 5 feet, `declDefaults()` still hands a back-rank
member a thrown dagger as a "missile" attack, which the engine reads as melee out of the front rank
and rejects the whole round with `battle.declaration.not_in_front_rank`. At seed 1 that happens in
round 2. The player fixes it by changing that one member's select to **Hold**, after which seed 1
finishes in four rounds with nobody hurt. It reproduces against any adventure whose opening fight
lasts more than one round.

Level 2 is not a level-1 party's dungeon and is not written as one. Walked at seed 42 with the
maximum-hit-points house rule, level 1 clears without a scratch (the only damage is the strongbox
trap), and level 2 takes four of the six between the pump chamber and the cold face.

## Re-deriving all of this

Start a guarded server and drive `POST /api/games` and `POST /api/games/{id}/command` with `urllib`:

```sh
OSR_WEB_NARRATOR= \
OSR_WEB_ADVENTURES= \
OSR_WEB_ADVENTURES_DIR="$(mktemp -d)" \
OSR_WEB_SAVES_DIR="$(mktemp -d)" \
  uv run uvicorn server.app:app --port 8681
```

`OSR_WEB_NARRATOR` is set to the *empty string*, not unset: `server/app.py` calls `load_dotenv()`
with the default `override=False`, so on a checkout whose `.env` names a live provider, launching
with `env -u OSR_WEB_NARRATOR` turns narration back **on**. Confirm the state payload's
`narration.enabled` reads `false`.

The response's `view.location.position`, `view.mode`, `view.encounter` and `cell` fields report
everything the route above claims.
