# Phase 3 plan — type checking

Record of phase 3 of [the development-loop spec](spec.md), built on 2026-09-14 as one chunk: pyright in basic mode over `server/` and `scripts/`, every error fixed with no suppression, the type-check step in CI, and the one latent defect the checker found fixed as a defect. Milestone, met: `pyright` reports zero errors with no ignore list in `pyproject.toml`, and party creation still works when driven.

## Scope

In scope: the dev dependency, `[tool.pyright]`, the 84 errors, the kit-equipping defect, and the CI step.

Out of scope: strict mode (declined in the spec's non-goals), `tests/` (the acceptance tests exercise private helpers and would need their own narrowing pass for no gain in what the gate proves), and the standing-gate section in `AGENTS.md` (phase 5).

## Decisions

- **Configuration is osrlib's shape:** `include = ["server", "scripts"]`, `typeCheckingMode = "basic"`, `pythonVersion = "3.14"`, and nothing else. The acceptance test fails on any key beginning with `report`, so a suppression cannot be added without editing a test.
- **The builder is narrowed once, at the seam that already decides.** `PartyBuilder._classed_draft` returned a draft and a class definition as two optionals beside a rejection list, and every step tested the list rather than the values, which is why 33 of the 84 errors were one root cause. It now returns the pair or `None`: a step that holds the pair holds both, and the private helpers take the plain values they always declared. `_shopping_draft` has the same shape one step on. No rejection code or ordering changed.
- **Guards return rejections; nothing asserts.** Two impossible states in `finalize` (a gold roll without a hit-point roll, an alignment that parsed but is `None`) are handled by the rejection the builder would give anyway. An inventory built from a draft with no gold roll raises, because every caller reaches it after the gold step and a silent empty purse would outlive a future refactor.
- **The defect.** `_auto_equip` read `.template` off every inventory entry; `MagicItemInstance` names its template by id and has none. The pass now keeps only mundane instances for the wear-and-wield decision and leaves everything else packed. `content.py::_outfit` had the same shape and got the same fix. Legality and the equip itself stay osrlib's (`validate_equip`, `equip`).
- **`server/app.py` reads the party's dungeon coordinates once**, through a helper that returns `None` in town, and keys the per-member payloads through one `_members_by_id` helper. The wire payloads are unchanged, which the phase 5 wire-contract test also holds.

## The chunk

- Measured before: 84 errors, 61 in `server/creation.py`, matching the spec's July count exactly.
- Acceptance tests: `tests/test_gates.py::TestPyrightGate` (configuration and CI step, plus a standing test that no inline ignore exists under `server/` or `scripts/`) and `tests/test_creation.py::TestAutoEquipWithMagicItems`, which reproduces the crash with a real catalog id and pins that the leather and sword still equip. Committed marked, unmarked at merge.
- An independent reviewer read every hunk, confirmed every rejection path, enumerated the armour catalog to show the new `ac is not None` filter changes no choice, and confirmed no osrlib rule was re-implemented.

## Definition of done

- `uv run pyright` reports 0 errors, 0 warnings over 9 files.
- `uv run pytest` is green: 502 passed, 12 xfailed (the phase 4 markers).
- CI runs `uv run pyright` between lint and the unit suite.
