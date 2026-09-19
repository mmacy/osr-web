# Phase 5 plan — the contract gates

Record of phase 5 of [the development-loop spec](spec.md), built on 2026-09-14: the two tests that turn honor-system rules into observers, and the standing-gate section in `AGENTS.md`, written last because every gate it names now exists. Milestone, met: adding a throwaway key to the state payload fails the suite, and the opening route is proven by CI rather than by a person remembering to walk it.

## Scope

In scope: `tests/test_wire_contract.py`, `tests/test_opening_route.py`, the `AGENTS.md` section, and these records.

Out of scope: golden transcripts (declined in the spec's non-goals: the engine is a path dependency on `main`, so a byte-pinned transcript would be a tripwire on someone else's repository), and pinning the shape of osrlib's `PlayerView`, which is the engine's to grow.

## Decisions

- **The wire-contract test pins this app's envelope, not the engine's projection.** Three frozen sets: the state block every game route returns, the `POST /api/games` payload, and the command payload. Adding a field means editing a set on purpose, in the same change, with the wire-contract question answered in review. A fourth test walks both payloads and fails on any key from a short list of referee names.
- **The opening-route test asserts structure, never dice.** Seed 42, `enter_dungeon` into the Cold Vein, the entrance position and facing, three accepted moves north with no wandering check on the way, then battle mode at the dressing floor with one group of four kobolds at the `attacks` stance. An engine tuning that changes damage or initiative changes nothing here.
- **The tests passed on the first run against the tree.** They are the acceptance tests for the app as it stands rather than for a chunk, so no expected-to-fail marker was needed. They landed with the marked tests for phases 2 to 4, and phase 3's refactor of `server/app.py` ran under them.
- **The guide's gate section lists the commands and the two rules the tests now hold**, and says the gate has no standing exceptions.

## Definition of done

- `uv run pytest tests/test_wire_contract.py tests/test_opening_route.py` passes: 5 tests.
- Removing any key from `_state` or adding one fails `test_wire_contract.py`; verified by hand during authoring.
- `AGENTS.md` names every command CI runs and nothing CI does not.
