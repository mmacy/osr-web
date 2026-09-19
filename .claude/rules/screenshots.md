---
paths:
  - "tests/screenshots/**"
  - "tests/assets/**"
  - "docs/images/**"
  - "scripts/check_screenshots.py"
  - "tests/test_check_screenshots.py"
  - "README.md"
  - "static/**"
  - "server/narrate.py"
---

# Documentation screenshots

The seven images in `README.md` are captured by a Playwright harness (`tests/screenshots/`), never by hand. Recapture whenever a change moves what a shot shows, including a wording change in `server/narrate.py`. **A capture run must never photograph a developer's own adventure library or saves.** That is why the harness pins `OSR_WEB_ADVENTURE`, blanks `OSR_WEB_ADVENTURES`, and redirects `OSR_WEB_ADVENTURES_DIR`, `OSR_WEB_SAVES_DIR`, and `HOME` at temp directories. It also lets `_guard_capture_safety` abort the session on the server's own report rather than trust the environment. For the harness commands, the capture-safety rationale, adding a shot, and what the staleness gate does and does not prove, see `.claude/skills/screenshots/SKILL.md`. **A topbar change moves four shots**, a new button included. `.scrim` is only 62% opaque and the topbar is a plain flow element beneath it, so `play-view`, `character-sheet`, `automap`, and `combat-round` all show the topbar. `.hud` has `margin-left: auto`, so the HUD chips shift by a new button's width in all four. **A viewport change moves the same four**, for the same reason. The wireframe sits in the left rail beneath the scrim, so a change to the wireframe's shape re-frames the picture and moves everything under it in all four shots (the 4:3 to 16:9 letterbox is an 81px lift). **The capture document has a quest of its own** (`the-ledger` in `tests/assets/capture-adventure/adventure.json`), so editing that document is a shot-moving change too. The quests card is in `play-view`'s rail, and the whole of `victory-screen` (the fortune line, the roster, the journal on the card) is that quest being finished. The three overlay shots (`adventure-library`, `party-builder`, and `victory-screen`) sit under `#overlay` at 96%, so the game behind them shows through only faintly. A full run rewrites all seven files regardless. On one machine two runs are byte-identical, so an inert change shows as no diff at all.
