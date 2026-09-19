# Test fixture documents

Adventure documents the tests and the documentation-screenshot harness load in place of the bundled
adventure. Two of them, with different jobs:

- `barrow-adventure.json` — *The Barrow of the Ninth*, the quest-carrying document the authored-layer
  tests drive. The capture harness must never load it.
- `capture-adventure/adventure.json` — *The Hollow Tithe*, the document every README screenshot is
  taken over. Everything from "Why the harness plays a fixture" down is about that one, capture
  safety included.

## `barrow-adventure.json`

"The Barrow of the Ninth" — one dungeon (`barrow`), one 4 × 1 level, the town Threshold one travel
turn away, and a quest that concludes the adventure. It is the quest-carrying adventure document the
journal and quest-log tests drive (`tests/test_quest_log.py`), and it is checked in because osrlib
ships no such document at all: its quest goldens (`phase15_quest.json` and friends) are run dumps —
`command_log`, `event_log`, `final_state` — and will not load through `check_document`.

### The recipe

Generated once from the osrlib checkout, and checked in exactly as written out:

```sh
uv run python -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '../osrlib-python/tests')
from crawl_fixtures import build_barrow_adventure
from osrlib.versioning import stamp_document
doc = stamp_document('adventure', build_barrow_adventure().model_dump(mode='json'))
Path('tests/assets/barrow-adventure.json').write_text(json.dumps(doc, indent=2) + '\n')
"
```

`stamp_document` writes the *installed* library's version into `engine_version`, so regenerate this
only from a checkout whose version is the one the file should claim. This copy reads
`schema_version: 3` / `engine_version: "1.6.0"`, generated against osrlib 1.6.0. Nothing compares
that field — an adventure envelope is checked, never migrated — but the file should not name an
engine it was not built from.

### What it carries

| Feature | Where |
| --- | --- |
| Entrance | level 1, cell (0, 0); the level is a 4 × 1 corridor, all four cells joined by open edges |
| Bundled item | `votive_idol`, "Votive idol of jade" |
| Authored cache | `reliquary` at (2, 0), in area `shrine` "Shrine of the Nine", holding the idol |
| Trigger | `barrow-mouth`, on `dungeon_entered`, carrying `fired` and `journal` beats and **no** consequences |
| Quest | `the-idol`, "The Votive Idol", offer spoken by Sister Halda, activating on `dungeon_entered` |
| Objective, plain | `recover-idol` "Recover the idol" — completes on acquiring `votive_idol`, with a `progress` beat |
| Objective, hidden | `speak-the-rite` "Speak the rite" — revealed by entering area `crypt` at (3, 0), completes on the flag `barrow.rite` = `spoken` |
| Quest rewards | a monster spawn (illegal in `victory`, so it drops with a note), 100 gp, 100 XP, a flag |
| Wandering | `0`-in-6 |

### Driving it

`enter_dungeon` lands the party on (0, 0) facing north, one travel turn spent — so the journal
entries written on arrival are stamped `rounds: 60`, which is turn 1 on the HUD clock, not turn 0.
Turn east, move twice to reach the shrine at (2, 0), and `take_treasure reliquary` completes
`recover-idol`. One more move east enters the crypt and reveals `speak-the-rite`.

**No sequence of *player* commands can conclude the quest**, which is what makes it safe to drive
before there is a victory screen: `speak-the-rite` waits on the session flag `barrow.rite`, and
`QuestSpec.completion` defaults to `"all"`. The general rule is narrower than it looks — `SetFlag` is
a legal trigger consequence, so an authored trigger or quest reward writes flags in response to
player commands — but this document's one trigger authors no consequences. The command endpoint is
*not* restricted to player commands, though: a single `{"command_type": "set_flag", "key":
"barrow.rite", "value": "spoken"}` after the idol is taken completes the quest and puts the session
in `victory`. That is by design, and since phase 3 of the authored-layer spec the client answers it
with the victory overlay: "the adventure is won", *The tale is told*, the fortune line, the roster,
the journal on the card, and three verbs (Roll a new party, Save the party, Return to the title).
Esc is the floor there, and a reload comes back to the same screen.

Tests point `OSR_WEB_ADVENTURE` at the file, which *replaces* the bundled entry. A manual drive that
wants both documents in one library uses `OSR_WEB_ADVENTURES` (plural, no `_DIR`) instead, which
*appends* the file as an extra source and leaves *The Cold Vein* selectable.

## Why the harness plays a fixture

A capture must never depend on whatever the bundled adventure happens to be. Today that is *The Cold
Vein* (`content/adventure.json`, provenance in `content/README.md`), original work written for this
repository — so photographing it would breach no licence. It is still the wrong document to
photograph, for three reasons that outlive any particular bundled adventure:

- **Churn.** Every capture asserts what is on screen before it fires, and those assertions name rooms,
  encounters, and a walked route. Aim them at the shipped document and any edit to it — a reworded
  area, one cell of map geometry, a rebalanced encounter — either rewrites `docs/images/` or breaks
  the harness, and from the outside those two failures look identical. Pointed at a fixture, a change
  to `content/adventure.json` cannot touch a single PNG.
- **Size and determinism.** This document is deliberately small — eleven keyed areas over two levels,
  level 1 authored at `0`-in-6 wandering — so the whole capture route can be driven, re-derived, and
  explained by hand. The bundled adventure is authored to be *played*; this one is authored to be
  *reproduced*, and every claim below was measured at seed 42.
- **Independence from the checkout.** A published image must show nothing a developer's machine
  happens to hold: no local adventure library, no save list, no home directory. A fixture document is
  the first half of that guarantee; the environment under "Loading it" is the rest.

Whatever the bundled default is replaced with next, the harness keeps playing this fixture.

## `capture-adventure/adventure.json`

"The Hollow Tithe" — a river toll-keep under a landing town called Thistlereach, two levels, eleven
keyed areas.

It is original work authored for this repository. Every proper noun, room name, description, hook,
town blurb, quest and objective line, and piece of map geometry in the file was written from scratch
for this purpose. It contains no text, names, maps, boxed text, or keyed content from any published
module — not B1–B12, not the Dungeon Crawl Classics line, not any other retail product — and it is
not derived from, adapted from, or a paraphrase of one.

The only third-party content it touches is by reference: `template_id` strings (`giant_rat`,
`kobold`, `skeleton`), equipment ids (`silver_dagger`, `holy_water`, `holy_symbol`), and treasure
generation, all of which resolve against osrlib's shipped OGL SRD catalogs at runtime. No SRD prose
is copied into the file.

The document is a stamped osr-forge envelope: `kind: "adventure"`, `schema_version: 3`,
`engine_version: "1.6.0"`. It loads through `check_document(document, "adventure")` followed by
`Adventure.model_validate(payload)`, and passes `validate_adventure` against the shipped monster and
equipment catalogs with no dangling references.

**The stamp is load-bearing, and it moved when the quest landed.** The file read `schema_version: 2`
/ `engine_version: "1.3.0"` until it carried `items` and `quests`, and a schema-2 stamp on a document
carrying those is the silently-stripped-to-an-unwinnable-shell case: an adventure envelope is checked
but never migrated, and `Adventure` ignores unknown keys, so a pre-1.5.0 engine would load the file
clean and simply drop the quest instead of refusing it. `engine_version` names 1.6.0 because the
quest authors `ObjectiveSpec.name`, a field 1.5.0 does not know — the same re-stamp *The Cold Vein*
took, for the same reason (`content/README.md`).

## Loading it

**`OSR_WEB_ADVENTURE` on its own is not enough, and getting this wrong publishes exactly what this
directory exists to prevent.** That variable replaces only the first of the library's three sources.
The `adventures/` drop directory and the `saves/` directory are still scanned, and both render into
the title screen's `<select>` pickers — so a `title-screen.png` taken with only `OSR_WEB_ADVENTURE`
set photographs whatever the local checkout happens to hold. On the checkout this was written
against, that was nine adventures — a shelf of converted commercial modules, none of them this
repository's to publish — and sixteen saves labelled with their titles.

Set all four library variables, and point the two directory overrides at empty directories:

```sh
OSR_WEB_NARRATOR= \
OSR_WEB_ADVENTURE="$PWD/tests/assets/capture-adventure/adventure.json" \
OSR_WEB_ADVENTURES= \
OSR_WEB_ADVENTURES_DIR="$(mktemp -d)" \
OSR_WEB_SAVES_DIR="$(mktemp -d)" \
  uv run uvicorn server.app:app --port 8657
```

What each one does:

- `OSR_WEB_ADVENTURE` replaces the bundled document. This document then becomes the library's first
  entry and its `default_id`, under the slug `the-hollow-tithe`.
- `OSR_WEB_ADVENTURES_DIR` **replaces** the `adventures/` drop directory. Pointed at an empty
  directory, the drop directory contributes nothing.
- `OSR_WEB_ADVENTURES` (plural, no `_DIR`) is a different knob: it **appends** a colon-separated list
  of extra sources and leaves the drop directory in place. Blank it so a value inherited from the
  shell or `.env` cannot add anything back.
- `OSR_WEB_SAVES_DIR` replaces the `saves/` directory, so `GET /api/saves` — and the title screen's
  restore picker — lists nothing.

Verified against the guarded command above: `GET /api/adventures` returns exactly one entry
(`the-hollow-tithe`, which is also `default_id`) and `GET /api/saves` returns zero.

Note that `OSR_WEB_NARRATOR` is set to the *empty string*, not merely unset. `server/app.py` calls
`load_dotenv()`, which fills in any variable the process environment does not already carry, so on a
checkout whose `.env` sets `OSR_WEB_NARRATOR=ollama` — as the one this was written against does —
launching with `env -u OSR_WEB_NARRATOR` silently turns narration back on. An empty value is present
in the environment, so dotenv leaves it alone, and the provider id is read with `.strip()`, so empty
means no provider. Confirm it took by checking that the state payload's `narration.enabled` is
`false`.

The screenshot harness additionally needs a redirected `HOME` (see the shared contract) so no
developer path can be rendered into an image.

## What the document exercises

| Feature | Where |
| --- | --- |
| Entrance | level 1, cell (3, 10), area 1 "Tollgate landing" |
| Keyed encounter, pinned to attack | level 1, area 2 "The oil cellar" — 3 × `giant_rat` |
| Keyed encounter, rolled reaction | level 1, area 7 "The signal loft" — 4 × `kobold` |
| Keyed encounter, level 2 | level 2, area 10 "The bone kiln" — 3 × `skeleton` |
| Normal door | level 1, edge `9,6:west`; level 2, edge `9,6:north` |
| Secret door | level 1, edge `14,6:west` (east wall of the countinghouse) |
| Room trap | level 1, area 4 "The sump gallery" — dart volley, save vs. breath for half |
| Authored treasure cache | level 1, `tithe-strongbox` at (14, 6); level 2, `lead-casket` at (8, 7) |
| Bundled item | `tollkeeps-ledger`, "the toll-keep's ledger", riding the `tithe-strongbox` cache |
| Quest | `the-ledger` "The Toll-Keep's Ledger", offered by the reeve of Thistlereach, activating on `dungeon_entered` |
| Objective, the spine | `recover-ledger` "Recover the toll-keep's ledger" — completes on acquiring the ledger |
| Objective, the homecoming | `return-it` "Bring it back to Thistlereach" — `town_entered` with a `has_item` condition on the ledger |
| Quest rewards | 25 gp and 100 XP a head, and `concludes_adventure`: the homecoming ends the session in `victory` |
| Treasure trap | level 2, on `lead-casket` — needle, save vs. death negates |
| Generated area treasure | level 2, area 9 "The drowned chapel" — unguarded band |
| Level transition | level 1 `stairs_down` at (10, 10) ⇄ level 2 `stairs_up` at (2, 8) |
| Wide rooms for the viewport | area 3 is 5 × 4 cells, area 2 is 4 × 3, area 9 is 5 × 3 |

Level 1 counts seven areas, level 2 counts four. Level 1's wandering-monster chance is authored at
`0`-in-6 so that the capture route below cannot be interrupted by a random encounter no matter how
many turns the harness burns getting there. Level 2 keeps the RAW `1`-in-6 every two turns.

## The quest, and why a capture document has one

The quest is the smallest one that puts both objective states in one published image and makes an
ending reachable. `play-view` is taken in the tithe vault with the ledger just taken, so the rail's
quests card shows `✓ Recover the toll-keep's ledger` over `○ Bring it back to Thistlereach`; the
walk home then completes the second, concludes the adventure, and *is* `victory-screen`. It is
seeded by the document's own first hook — the reeve paying in coin for the ledger — so nothing about
it is bolted on.

Two authoring constraints came out of that job, and both are worth keeping:

- **The offer is short on purpose.** The quests card renders it in the exploring rail, where a third
  card is already at the fold (the rail paragraph in `.claude/rules/client.md` has the measurements); at 1440×900 this
  one runs two rendered lines and the whole card lands 630.9–816.8 in a rail 848 tall — visible
  entire. A longer offer pushes its own objectives under the fold and out of the shot.
- **Nothing it added draws a die.** Quest lifecycle commands are bookkeeping — no rolls, no time —
  and the ledger rides a cache the seed-42 route already empties, so the two load-bearing draws in
  the route below (the tinder's second strike, the elf's one search attempt) are exactly where they
  were. The general escape hatch to remember: an authored cache carrying a **magic** item rolls its
  creation on the treasure stream at take, so adding one of those to a routed cache would move every
  roll after it. The ledger is mundane gear and does not.

## The deterministic route

The party starts in town. `EnterDungeon` lands it on the entrance cell facing north, so the whole
route is forward movement — no turning required.

Entrance: **level 1, cell (3, 10), facing north.**

```
POST /api/games                     {"seed": 42, "adventure_id": "the-hollow-tithe"}
POST /api/games/{id}/command        {"command_type": "enter_dungeon", "dungeon_id": "hollow-tithe"}
POST /api/games/{id}/command        {"command_type": "move_party", "direction": "north"}   -> (3, 9)
POST /api/games/{id}/command        {"command_type": "move_party", "direction": "north"}   -> (3, 8)
POST /api/games/{id}/command        {"command_type": "move_party", "direction": "north"}   -> (3, 7)
```

The third move crosses into area 2, "The oil cellar". Its keyed encounter — three giant rats — pins
`stance: "attacks"`, so the session lands in `battle` mode on that move, round 1, with no encounter
menu in between and no branch on the surprise roll. That is the shot the `combat-round` capture
wants: the roundbar is live the instant the move returns.

In the UI the same route is the Forward button (or `W` / `ArrowUp`) pressed three times from the
dungeon entrance.

Three giant rats against six level-1 characters is deliberately the gentlest keyed encounter the
catalog offers. Driven at seed 42 with each front-rank member attacking with their wielded melee
weapon, the battle ends in victory after four rounds with all six alive.

Driven instead through the *client's own* opening orders — `declDefaults()` in `static/app.js`, which
is what the screenshot harness gets when it clicks Resolve round without touching a select — it ends
in **one** round: both casters open on a grouped foe, the elf's *magic missile* kills one rat and the
magic-user's *sleep* drops the other two, and the front rank cuts the helpless survivors down. That
single round is 23 transcript entries. Which declarations the harness sends therefore changes the
transcript and every roll after it, not just the round count.

### Onward to the stairs down

From (3, 7), after the rats are dealt with:

```
move_party north                    -> (3, 6)
move_party east  × 5                -> (8, 6)
open_door east                      -- the normal door on edge 9,6:west
move_party east  × 2                -> (10, 6)
move_party south × 4                -> (10, 10), cell.transition == "stairs_down"
use_stairs                          -> level 2, cell (2, 8), facing north
```

`use_stairs` again on (2, 8) returns the party to level 1 at (10, 10). Both directions were driven
and confirmed.

### The secret door

Walk to (13, 6) — from (9, 6), four moves east — and the east edge reads as solid wall:
`move_party east` rejects with `exploration.move.blocked`, and `open_door east` rejects with
`exploration.door.no_door`. `search` with `kind: "secret_doors"` reveals it. Each character gets one
attempt per cell per kind, at the RAW 1-in-6 (2-in-6 for elves), so how many members it takes varies
by seed. Once discovered, `open_door east` succeeds and one move east lands on (14, 6), where
`cell.features` carries `tithe-strongbox` for `take_treasure`.

**The UI gets exactly one attempt, and at seed 42 it is not the first one.** The actions bar's Search
button calls `memberByClass("elf", "thief")`, which returns the elf whenever the elf is alive — so a
second click at the same cell is rejected with `exploration.search.already_tried`, and no other
character can be sent instead. Measured on the route above: the two exploration-stream draws that land
first are misses, and the third is a hit. Two draws therefore have to be spent before the elf tries.
The screenshot harness spends them on things a cautious party does anyway, both of which draw from the
same stream: `listen_at_door` at the normal door on the way in (the thief), and a `search` with
`kind: "room_traps"` at (13, 6) before poking at the wall (also the thief). With those two ahead of it
the elf finds `secret_door:east` on its one attempt, every run.

Moving, turning, opening an unstuck door and taking treasure draw nothing from the exploration stream;
lighting a torch, listening, forcing, picking a lock and searching all do. That is the whole list of
knobs for shifting where a search lands.

### The route the screenshot harness walks

`tests/screenshots/test_shots.py::_delve_to_the_tithe_vault` drives all of the above through the
client's own controls, and is the longest sequence verified end to end at seed 42:

```
enter_dungeon                       -> (3, 10) facing north; the quest activates here
light_source torch × 2              -- the second strike catches
move_party north × 3                -> (3, 7), the oil cellar's rats attack
resolve_battle_round × 1            -- client defaults: magic missile + sleep, then the front rank
move_party north                    -> (3, 6)
turn_party east
move_party east  × 5                -> (8, 6)
listen_at_door east (thief)         -- "silence", and one exploration draw spent
open_door east                      -- the normal door on edge 9,6:west
move_party east  × 5                -> (13, 6), the countinghouse floor
search room_traps (thief)           -- nothing here, and the second draw spent
search secret_doors (elf)           -- finds secret_door:east
open_door east
move_party east                     -> (14, 6), the tithe vault
take_treasure tithe-strongbox       -- silver dagger, holy water, the ledger, a beryl, 114 gp
```

That is 49 transcript entries — 46 of play and three of quest (the charge at the landing, its offer,
and the ledger's progress beat) — and the party reaches the vault at full strength with
`light 2 turns` left. The automap shot carries on — about face, three moves west to (11, 6), then four north to
(11, 2) — which leaves the walked trail spanning the level from the landing to the vault, with the
countinghouse behind and the sump gallery ahead both inside the torch's reach. Standing in the passage
rather than in the gallery is deliberate: area 4's dart trap triggers on entry.

### The walk home, and the ending

`_walk_home_from_the_vault` carries the same route back out for the `victory-screen` shot, and its
first leg is the automap shot's own:

```
turn_party west (about face)
move_party west  × 5                -> (9, 6), the countinghouse's side of the normal door
open_door west                      -- it swung shut behind the party on the way in
move_party west  × 6                -> (3, 6)
turn_party south
move_party south × 4                -> (3, 10), the tollgate landing
travel_to_town                      -- the homecoming: quest complete, session `victory`
```

Three things measured on that leg at seed 42, none of them guesses:

- **The normal door needs a second `open_door`; the secret door does not.** A party-opened door swings
  shut only once the party stands clear of *both* cells it joins, so the secret door on `14,6:west`
  is still open when the party walks back through it and the door on `9,6:west` is not.
- **Nothing wanders.** Level 1 is authored `0`-in-6 but `wandering_check` adds +1 for noise since the
  last check, so the return leg after the oil-cellar battle is a real 1-in-6 — and it never comes up,
  because no turn elapses on the way out. The walk back crosses cells the party has already explored,
  which accrue 10 odometer thirds apiece against 30 for new ground, so fifteen moves do not reach the
  180-third turn threshold: the clock reads the same at the landing (turn 7, 420 rounds) as it did at
  the vault.
- **The torch dies on the way to town, not underground.** `travel_to_town` spends the two travel
  turns, and its delta opens with "The light gutters and dies."

The homecoming completes `return-it`, the quest concludes the adventure, and the session goes
`victory` — which is what raises the overlay the shot is of. The card's fortune line reads *The party
carries home 518 gp in coin and 1 treasure worth 50 gp* (the beryl, never sold), and its journal is
the whole tale in four beats.

## Determinism notes for whoever maintains the harness

- Every seeded run must use the same command prefix. The engine's RNG streams advance per command,
  so inserting an extra action (lighting a torch, an extra turn) before the route shifts every roll
  after it. The route above is verified only as written, from a fresh `POST /api/games`.
- Two fresh seed-42 games driven through the harness's route produce byte-identical 49-entry
  transcripts, with the guarded launch command and `narration.enabled == false`. (This bullet used
  to claim 55 entries for "the route plus the rat battle", which no drive on record reproduces; it is
  re-anchored on the one route the tests actually walk, and measured twice.)
- The party starts with no lit light source. `search` and `listen_at_door` require light (infravision
  suffices for the dwarf); movement and combat do not. Lighting a torch is a 2-in-6 attempt per try
  and each attempt spends a turn, so if the harness lights up first it must do so at a fixed point in
  the command sequence or the downstream rolls move.
- `static/app.js` picks a random hook for the title-screen card (`S.hooks[Math.floor(Math.random() *
  S.hooks.length)]`). This document carries three hooks, per the brief, so that one line of the
  title-screen shot is not stable across page loads. It is a client behavior, not a content one —
  either accept the churn or stub `Math.random` in the capture page before the shot.
- The party's torch burns 360 rounds (six turns), so the fixed prefix has a light budget as well as a
  roll budget. Measured down the harness's route: 359 rounds once it catches, −60 for the rat battle,
  −58 when the walk east crosses a turn boundary (the odometer absorbs the partial move), then −60
  each for the listen, the trap sweep and the search. The party reaches the tithe vault with 61 rounds
  in hand and the HUD reading `light 2 turns`. One more turn-costing action and it finishes in the
  dark, which the automap and the wireframe viewport both show.
- Level 1's wandering chance is authored at `0`-in-6, but `wandering_check` adds +1 for noise since
  the last check, and a battle is noise. A check therefore *can* fire on level 1 in the two turns
  after a fight. It does not on the harness's route at seed 42 — driven end to end and confirmed —
  but a longer route is not automatically safe just because the level is authored quiet.
- `view.explored[].cells` *is* a growing set of everything the party has seen. It unions the cells
  the party has actually walked, every cell its light has ever revealed (persisted as map memory in
  osrlib's `DungeonState.seen`), and whatever the light reaches from where it
  stands right now (`_light_reveal` in osrlib's `crawl/exploration.py`): the keyed room it stands in
  whole, and three cells of open passage beyond. Rooms the party has walked *through* stay mapped
  whole behind it. So how much map the automap has to draw is decided by everything the route has
  seen, not only by the squares it stepped on or where it stops.

## Re-deriving all of this

Start a server with the guarded command under "Loading it", then drive `POST /api/games` and
`POST /api/games/{id}/command` with `urllib` exactly as listed above. The response's
`view.location.position`, `view.mode`, `view.encounter`, and `cell` fields report everything the
route claims, and `narration.enabled` should read `false` throughout.
