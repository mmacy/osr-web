---
name: verify
description: Launches osr-web and drives it in a browser to verify changes end to end. Use when confirming a change to server/ or static/ works in the running app.
---

# Verify osr-web changes

Use the user's own server on port 8620. Don't start a temp server on another port. After a `server/*.py` change, bounce it and leave it running the new code, detached, so it outlives the session:

```bash
kill $(lsof -ti :8620) 2>/dev/null; sleep 1
nohup .venv/bin/uvicorn server.app:app --port 8620 >> ~/Library/Logs/osr-web.log 2>&1 & disown
```

Restarting orphans every in-memory session, and the front door then offers the newest save. That's an accepted cost: the user prefers one always-current server. `static/` edits take effect on a page reload. Static files are mounted at root, so the client is `/app.js`, not `/static/app.js`.

Read the first line the server logs before saying anything about narration: `narration enabled: …` or `narration off: …`. On this machine `.env` sets `OSR_WEB_NARRATOR`, so a plain launch has narration on. To launch without it, blank the variable (`OSR_WEB_NARRATOR= …`), never unset it. `load_dotenv()` fills in variables that are absent and leaves alone variables that are present but empty, so `env -u OSR_WEB_NARRATOR` falls through to the `.env` value and turns narration back on. The same asymmetry applies to every `OSR_WEB_*` variable.

Then drive <http://localhost:8620> with `playwright-cli` (`playwright-cli open http://localhost:8620`), or drive the API with a short `urllib` script per the `api-surface` skill. Seedable sessions: `POST /api/games {"seed": 42}`, then `enter_dungeon` with `dungeon_id: "cold-vein"` and three `move_party north` reaches the dressing floor at (2,12), where four kobolds attack and the session is already in `battle` mode.

## Flows worth driving

- Front door: buttons render after the library fetch, so take a second `snapshot` if the first shows only the header bar. Continue restores the newest save. "Begin the adventure" is the only `POST /api/games`, so flipping through the library must produce zero game POSTs.
- Party builder: "Build a custom party", then "Roll a character", then the abilities table, the class grid, and the adjust screen. Destructive buttons ("Discard character", removing a member, "Abandon adventure" in the menu, Delete on a save or an adventure) open the in-app confirm dialog (`#confirm-overlay`, a scrim wrapping an `alertdialog` panel with Cancel and a danger-styled verb button). Click its buttons like any element. There is no native `confirm` to `dialog-accept`.
- Error popups surface via `#toast`. Check with `eval "document.getElementById('toast').className"`: `toast hidden` with empty text means no popup. Error toasts lack the `ok` class and auto-hide after 3.2s, so check promptly after the action.
- Run `playwright-cli console error` after each flow. The app logs nothing in normal operation, so any console message is a finding.

## Gotchas

- Client errors thrown during `renderBuilder()` inside `builderStep()` are caught and shown as an error toast, not in the console. Check the toast, not just the console.
- In game, each `Esc` press closes one thing (the sheet, then the journal, then the map), and with none open it opens the menu. On an overlay screen it goes back one step. In the wizard it is inert. On the title and the two ending screens it does nothing.
