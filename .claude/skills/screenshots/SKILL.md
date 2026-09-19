---
name: screenshots
description: Recaptures and validates the osr-web README screenshots. Covers the Playwright harness, the capture-safety guards that keep private library and save data out of committed images, adding a shot, and what the staleness gate proves. Use when a change moves what a shot shows or when working on tests/screenshots/.
---

# Documentation screenshots

The seven images in `README.md` are captured by a Playwright harness (`tests/screenshots/`), not taken by hand. Each capture test drives the real UI (the actions bar, `W`, `M`, a double-click, the opening flow's own buttons), asserts that the screen shows what its slug claims, and only then takes the shot. The slugs are `adventure-library`, `play-view`, `character-sheet`, `automap`, `combat-round`, `party-builder`, and `victory-screen`, listed in `capture.SHOT_SLUGS`. Each lands at `docs/images/<slug>.png`.

```sh
uv run playwright install chromium     # once
uv run pytest tests/screenshots        # recapture all seven, rewriting docs/images/*.png
uv run python scripts/check_screenshots.py   # the staleness gate; exit 0 is a pass
```

Bare `uv run pytest` does **not** reach the harness, by design. `norecursedirs` in `pyproject.toml` keeps `tests/screenshots` out of collection while pytest recurses, so the fast unit suite runs without Chromium or a server. Naming the directory on the command line is what collects it. CI runs the two as separate jobs (`.github/workflows/ci.yml`).

Recapture whenever a change moves what a shot shows: a UI change, or a wording change in `server/narrate.py`, which appears in the `play-view` and `automap` transcripts.

## Capture safety (the part that is not optional)

A capture run must reach the front door with exactly one adventure in the library and zero saves. `OSR_WEB_ADVENTURE` alone does not achieve that. It replaces only the first of the library's three sources, and the client still renders the `adventures/` and `saves/` scans into the library pane, the door's Continue/Load game buttons, the save browser, and the staging screen's veterans picker. On a developer checkout that is a local library of retail modules and a save list named after them, published as an image. So the harness runs its server with:

- `OSR_WEB_ADVENTURE`: the capture document, `tests/assets/capture-adventure/adventure.json` (see `tests/assets/README.md`). It is *The Hollow Tithe*: two levels, eleven keyed areas, and one authored quest, `the-ledger`, which activates the moment the party enters the toll-keep and concludes the adventure when its ledger comes home. That quest is what puts the quests card in `play-view` and makes `victory-screen` reachable at all. The harness does **not** photograph the bundled document, by design. Keeping the shots on a fixture means a change to `content/adventure.json` never churns `docs/images/`. The converse is the cost: a change to the *capture* document moves shots, so it is a recapture-and-commit change like any UI edit.
- `OSR_WEB_ADVENTURES_DIR` and `OSR_WEB_SAVES_DIR`: pointed at **empty temp directories**, which is the only way the drop directory and the saves directory contribute nothing. `OSR_WEB_ADVENTURES` (plural, no `_DIR`) is blanked too. The server appends its entries as extra sources rather than replacing the drop directory.
- `OSR_WEB_NARRATOR` and the rest of the `OSR_WEB_NARRATOR*` family: set to the **empty string, never unset**. `server/app.py` calls `load_dotenv()` with the default `override=False`, which fills in variables that are *absent* and leaves alone variables that are *present but empty*. On a checkout whose `.env` names a live provider, `env -u OSR_WEB_NARRATOR` therefore turns narration **on**. That defect reproduces only on developer machines (CI has no `.env`) and bakes nondeterministic model prose into committed PNGs.
- `HOME`: a temp directory, so no developer path can be rendered into an image.

`_guard_capture_safety` in `tests/screenshots/conftest.py` then asks the running server what it is actually serving (one adventure, zero saves, `narration.enabled == false`) and ends the whole session rather than let one shot be taken. Trust the server's report over the environment.

Determinism rests on three more things, all in the harness. The game is created server-side with `seed: 42` and adopted by the browser through `localStorage['osrweb_game']` (the client never sends a seed of its own). `Math.random` is stubbed in an init script as a second guard against randomly chosen chrome. The party builder's own seed is added to `POST /api/party-builders` on its way out. That patch seeds the staging screen's premade builder and the wizard's builder alike, and each draws from its own streams. Two consecutive full runs produce byte-identical PNGs.

## Adding a shot

1. Add the slug to `SHOT_SLUGS` in `tests/screenshots/capture.py`. `capture()` rejects any slug not in that tuple.
2. Write the test in `tests/screenshots/test_shots.py`. Drive the UI through `tests/screenshots/drive.py`, assert the on-screen state you are claiming, then call `capture(page, "<slug>")`. Nothing is photographed unconditionally.
3. Reference it from `README.md` as `![Alt text](docs/images/<slug>.png)`, that exact markdown form, in the section whose argument the image supports. The gate parses only that form: no HTML `<img>`, no reference-style links, no `./docs/...`. Alt text is sentence case and describes what a reader would otherwise be missing.
4. Recapture, then run the gate. Commit the new PNG.

## What the gate proves, and what it does not

`scripts/check_screenshots.py` checks three directions: every image `README.md` references exists on disk, every `docs/images/*.png` on disk is referenced by `README.md`, and the slug set in `.screenshot-manifest` (gitignored, appended by the harness as it runs) equals the slug set `README.md` references. The third is the load-bearing one. A deleted or skipped capture test leaves its stale PNG on disk, so every disk-based check stays green while the screenshots rot. A missing manifest is a loud failure, never "nothing to check", and a README referencing zero screenshots fails too (all three directions hold vacuously over an empty set).

The limit is worth stating. The gate proves the shots can still be taken against the current UI, and that the README, `docs/images/`, and the manifest name the same set. It does **not** prove that a screenshot shows the right thing, or anything at all. There is no pixel diffing and no image-content inspection of any kind. Fonts and font rasterisation differ across machines, so a pixel comparison (or `git diff --exit-code docs/images`) would fail on every run for purely cosmetic reasons and teach everyone to ignore CI. The consequence: a committed image can drift cosmetically from what CI renders, or go stale in a way no assertion happens to cover, while the gate stays green. The assertions inside each capture test are what defend the content of a shot. The gate only defends the set.
