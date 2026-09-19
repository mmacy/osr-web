# AGENTS.md

Guidance for agents working on osr-web, an old-school web dungeon crawler over the osrlib rules engine. It plays any osr-forge adventure document. *The Cold Vein*, original work written for this repository (see `content/README.md`), ships bundled as the default.

## Architecture in one paragraph

osrlib owns every game rule. It lives at `../osrlib-python`, wired as an editable path dependency, with published docs at <https://mmacy.github.io/osrlib-python/> and their source in `../osrlib-python/docs/`. **This app is presentation only.** It never re-implements a rule. `server/app.py` keeps `GameSession` objects in memory behind per-session `threading.Lock`s and exposes a small JSON API. The rest of `server/` splits by concern (`narrate`, `library`, `content`, `creation`, `llm`, `narration`), and `static/` is a dependency-free vanilla-JS client. There is no build step and no client framework.

## Run and verify

```sh
uv sync
uv run uvicorn server.app:app --port 8620
```

Prerequisites: a checkout of `osrlib-python` at `../osrlib-python` (`git clone https://github.com/mmacy/osrlib-python ../osrlib-python`) and Python ≥ 3.14, which `uv sync` provisions. The editable path dependency means the app runs whatever that clone has checked out.

**Blank an `OSR_WEB_*` variable, don't delete it, whenever you mean to turn something off or point it at nothing.** `server/app.py` calls `load_dotenv()` with `override=False`, which fills in variables the environment lacks and leaves present-but-empty ones alone. So `env -u OSR_WEB_NARRATOR` falls through to the `.env` value, while the app reads `OSR_WEB_NARRATOR=` as off. The defect reproduces only on machines that have a `.env`, so confirm from what the server reports (its first log line, `GET /api/adventures`, `GET /api/saves`) rather than from what you set. The full account is in `.claude/rules/narration.md`.

Open <http://localhost:8620>. To verify changes end to end, drive the API with a short Python script (`urllib` against `/api/games`, see the wire contract below) or use playwright-cli against the page. Battles and encounters are seedable: `POST /api/games {"seed": 42}` gives reproducible sessions, and the bundled adventure is authored with a short scripted route to a first fight. `enter_dungeon` with `dungeon_id: "cold-vein"` lands the party on the entrance at (2,15) facing north, and **three `move_party north` reaches (2,12), the dressing floor, where four kobolds attack**. The encounter is pinned (`aware: true`, `stance: "attacks"`), so the session is in `battle` mode the instant that third move returns. There is no encounter menu, no reaction roll to branch on, and no wandering check on the way (three cells is 90 of the engine's `odometer_thirds`, the movement counter that reaches a turn boundary at 180).

Unit tests: `uv run pytest`. The suite covers the narration layer (`tests/test_narration.py`), the one subsystem whose logic this repo owns outright, plus the two contracts the standing gate below holds: the wire contract's key sets and the scripted opening route. Game rules are osrlib's and tested there. The rest of the app is wiring and rendering, verified end to end as above. Add tests in the same style for any new server logic that is more than a pass-through to the engine.

## The standing gate

Every pull request runs the same commands CI does, each a plain `uv run` or one `npx`, from the checkout:

```sh
uv sync --locked
uv run ruff format --check
uv run ruff check
uv run pyright
npx --yes oxlint@1.83.0 --deny-warnings static/
uv run pytest
```

On pull requests CI also runs `uv run python scripts/check_app_version.py --base origin/<base branch>`, which fails when `static/app.js` differs from the base branch while the `app.js?v=N` query in `static/index.html` does not. The gate has no standing exceptions: no ignore lists, no `noqa`, no `type: ignore`, no suppression keys, no linter config for the client. A finding is fixed in the code. Two tests hold rules as code rather than prose: `tests/test_wire_contract.py` pins the key set of every game payload, so a new response field means editing that set on purpose and answering the wire-contract question in review, and `tests/test_opening_route.py` drives the seed-42 route above and asserts structure, never dice. `tests/test_gates.py` pins the gate's own configuration against osrlib's. The screenshot harness is opt-in. The workflow is `.github/workflows/ci.yml`, and `specs/dev-loop/` describes the gate's phases.

## Greenfield discipline

Refactor freely and update every call site. The tests and the end-to-end drives are the safety net. No re-exports or aliases kept to preserve an old import path, no deprecation scaffolding, no code kept "just in case". Git history is the archive. Release changes nothing about this: delete, don't deprecate, and add no workarounds, compatibility shims, or other accommodations. Do the right thing even when it's the hard thing, and question existing code and the decisions behind it as the project matures. What was right then may not be right now. The two real constraints are the wire contract below (player-visible state only, never weakened) and osrlib's ownership of every game rule. This app never re-implements or forks one.

## Working method

Work is divided into chunks. For each chunk, the public signatures are written as stubs first, and the acceptance tests are written before the implementation, by a different agent or person than the one who implements it, and committed marked expected-to-fail. Whoever implements the chunk makes the tests pass without editing them, and the markers come off when the chunk merges. A system's design lives one directory per effort, `specs/<effort>/spec.md`, which names its phases, and each phase is a batch of chunks.

## The wire contract (do not weaken it)

Only the player projection crosses the wire: `session.view(Visibility.PLAYER)`, player-visibility events, and the enriched but player-safe extras. Those extras are `cell` context, `sheets` (per-member character-sheet data a B/X player reads off their own record, spell slots and rested included), `spellbook`, `spell_books`, `learnable` (per-member learnable-spell candidates, a rulebook-public class list minus the player's own book), `temple_services`, and `hooks`. The master seed, referee events, monster HP, hidden geometry, and save documents stay server-side. In-fiction rejections are HTTP 200 with `accepted: false`. Malformed content is 422. Unknown ids are 404. If you add a response field, ask first whether it leaks referee state.

## API surface

The endpoint reference (every `/api/` route, its body, and its response shape) is `.claude/skills/api-surface/SKILL.md`. Read it before adding or changing a route, or before driving the app over HTTP.

## Where the rest of the guide lives

The hard-won facts are split by the files they constrain into path-scoped rules in `.claude/rules/`, and Claude Code loads a rule only when one of those files is read:

| Rule | Loads for | Records |
|---|---|---|
| `engine-facts.md` | `server/**`, `static/app.js`, `tests/*.py` | How osrlib behaves where the app's rendering and wiring depend on it |
| `authored-layer.md` | `server/**`, `content/**`, `static/app.js`, `tests/*.py`, `specs/authored-layer/**` | Gates, triggers, the journal, quests, victory (spec: `specs/authored-layer/spec.md`) |
| `narration.md` | `server/narration.py`, `server/narrate.py`, `server/llm.py`, `server/app.py`, `static/app.js`, the two narration test files, `specs/llm-narrative/**` | The LLM narrator, and the full account of the `.env` asymmetry (spec: `specs/llm-narrative/spec.md`) |
| `content.md` | `content/**`, `adventures/**`, `server/content.py`, `server/library.py`, `server/app.py`, `static/app.js`, the library and paths tests, `tests/assets/**` | The adventure library and the bundled document |
| `opening-flow.md` | `static/**`, `server/app.py`, `tests/test_saves.py`, `tests/test_opening.py` | How a game begins, ends, and is saved: the screen-state machine and the routes behind it |
| `client.md` | `static/**`, `tests/screenshots/**` | Client gotchas, the UI's voice and measured layout |
| `screenshots.md` | `tests/screenshots/**`, `tests/assets/**`, `docs/images/**`, `scripts/check_screenshots.py`, its test, `README.md`, `static/**`, `server/narrate.py` | What moves a README screenshot |
| `licensing.md` | `README.md`, `LICENSE`, `LICENSE-*.md`, `content/**`, `docs/**` | The three-license split and the OGL facts that must not drift |

A fact goes in the rule for the files it constrains, and in this file only if it applies to every session. A change that falsifies a sentence in either place includes the correction in the same PR. Claude Code loads a rule when a matching file is read with the Read tool (measured: it does, live, with no restart). A shell read (`cat`, `head`, `sed -n`) does not trigger it, so open a file with the Read tool at least once before working on it, or Claude Code never loads the facts about it.

Skills, loaded on demand: `api-surface` (every `/api/` route and its shape), `verify` (launch and drive the running app), `screenshots` (the capture harness). Claude Code loads skills and rules live.

## Style

- Python is formatted with `uv run ruff format` and linted with `uv run ruff check`, at osrlib's rule selection and line length, both enforced in CI on every pull request.
- User-facing strings use sentence case, and it's always the "party", never the "company".

## Repo facts

- Repository at <https://github.com/mmacy/osr-web> (`origin`). `main` takes pull requests only.
- `saves/*.json` and `.playwright-cli/` are gitignored scratch output.
- `uv sync` fails in a git worktree under `.claude/worktrees/`: uv resolves the editable path dependency `../osrlib-python` relative to the checkout, so a worktree must sit beside the engine, a sibling directory in the same parent as `osrlib-python`. Make a worktree by hand as a sibling (`git worktree add ../osr-web-<branch>`). Subagents work in the checkout they are given and never create one.
