# Spec: the development loop

osrlib and osr-web are one project in two repositories, and the engine repo has a development loop this one only half has. osrlib runs a five-part standing gate on every pull request — format, lint, type check, test, and a regenerate-and-diff drift check — and runs a written three-artifact loop: a spec defines a system and its phases, a plan is written for each phase, and the implementation executes the plan, each reviewed to a verdict. osr-web runs one and a half of the gates, and has been shipping specs straight to implementations with no plan in between. This spec closes both halves in five phases. The milestone: **every gate osrlib enforces is enforced here too, in a form that fits an app rather than a library, and the loop that produces the work is written down rather than remembered.**

## Summary

Measured against the tree at the time of writing (414 tests, green, 2.27s):

| Gate | osrlib | osr-web today | Measured gap |
|---|---|---|---|
| `ruff format --check` | CI, every PR | never run | clean at 88; 28 of 32 files reflow at 120 |
| `ruff check` (B, D, E, F, I, UP, W) | CI, every PR | no config exists | 188 findings at 88; **21 at 120** |
| `pyright` (basic) | CI, every PR | never run | **84 errors**, 61 in `server/creation.py` |
| `pytest` | CI | CI | none |
| `uv sync --locked` | CI | plain `uv sync` | lockfile drift is silent |
| Drift gate | SRD regen, fail on diff | screenshot manifest | `app.js?v=N` unenforced |
| Client gate | n/a (no JS) | **none, 164 KB** | oxlint finds 10, one a dead branch |
| Written loop | spec → plan → impl | spec → impl | no plan step, no skill, no templates |

## Findings the phases answer

1. **No formatter or linter runs anywhere.** `AGENTS.md` claims Python here is "formatted with `ruff format` (default line length) and linted with `ruff check` via `uvx ruff`" — an unpinned tool invoked by hand, never in CI, with no configuration file to say what "linted" means. The formatter claim happens to be true right now; the lint claim is unverifiable, because there is no rule selection to check against.
2. **No type checker has ever run on this code.** 84 errors in pyright's basic mode — the same mode osrlib gates on. They are not evenly spread: 61 are in `server/creation.py`, and roughly 50 of those are a single root cause, an unnarrowed `Draft | None` / `ClassDefinition | None` threaded through the party builder.
3. **One of those errors is a latent `AttributeError`.** `creation.py::_wear_best` reads `instance.template` across `inventory.items`, a union that includes `MagicItemInstance` — which carries `template_id` and `base_item_id` and *has no* `template` attribute. It cannot fire today only because the builder's shop stocks no magic items. Nothing but a type checker was ever going to find that.
4. **CI syncs unlocked.** The workflow runs `uv sync`, not `uv sync --locked`, so a `uv.lock` that no longer matches `pyproject.toml` is silently resolved around instead of failing the build.
5. **The client has no gate of any kind.** `static/app.js` is 164 KB — the largest file in the repository — with no lint, no type check, and not even a syntax check. Static files are served straight from disk and no test loads the file, so a parse error ships and is discovered by a human clicking. oxlint finds 10 issues, including `no-dupe-else-if` at `static/app.js:2206`: a duplicated condition in an if-else-if chain whose branch can never execute.
6. **The cache-bust discipline is honor-system.** "Bump N whenever you edit app.js, or your change silently won't load" is the first entry under Client gotchas, and nothing enforces it. Two commits in the history changed `app.js` without touching `index.html`.
7. **The wire contract is honor-system.** "If you add a response field, ask first whether it leaks referee state" is a rule with no observer: no test looks at `_state()`'s key set, so a field added in good faith and a field added carelessly are indistinguishable to CI.
8. **No test drives a command sequence.** All 414 tests are unit-level. The route `AGENTS.md` documents as the way to verify end to end — seed 42, `enter_dungeon`, three moves north, four kobolds attack — is a promise the suite never checks.
9. **The plan step is missing from the loop.** osrlib ships a spec, then a plan per phase, then an implementation per phase. Here, `specs/authored-layer/spec.md` was followed directly by an implementation with no plan document in between — so that phase's decisions were made and reviewed as a diff rather than as choices. There is no template for either artifact.

## Design constraints

- **Every gate runs locally, cheaply, from one command.** `uv run ruff format --check`, `uv run ruff check`, `uv run pyright`, `uv run pytest`, plus one `npx` for the client. No gate may require a service, a browser, or a network round trip — the screenshot harness stays opt-in exactly as it is today.
- **No client toolchain.** No `package.json`, no `node_modules`, no build step. "A dependency-free vanilla-JS client" is load-bearing architecture here, not an accident, so the client gate must be a single invoked binary that reads the file and exits.
- **Greenfield discipline applies to the gates themselves.** No suppression baselines, no burn-down lists, no per-file ignore blocks bought to make a gate green today. A gate that ships with standing exceptions is exactly the just-in-case accommodation `AGENTS.md` forbids — so a gate lands only when the tree is clean under it.
- **No gate may be machine-fragile.** `scripts/check_screenshots.py` already carries this lesson in its own comments: a pixel comparison would fail on every run for font-rasterisation reasons and teach everyone to ignore CI. Structure, never rendering.
- **osrlib is a path dependency tracking `main`.** Any gate that pins engine output pins *structure*, not dice — a test asserting damage numbers would break on every engine tuning and would be deleted within a month.
- **The wire contract is never weakened, including by a test.** A gate that observes the payload reads what already crosses; it never reaches for referee state to compare against.
- **This app never re-implements an osrlib rule**, and nothing in this spec creates a reason to.

## The phases

**Phase 1: the loop, written down.** The per-effort directory layout under `specs/`, with a spec per effort and a plan per phase as the record of what was built, and the working method that `AGENTS.md` states. Phases 2 to 5 shipped on 2026-09-14, each with its record beside this file.

**Phase 2 — formatting and lint.** `ruff` as a pinned dev dependency with a rule selection matching osrlib's, the one-time reflow that adopting osrlib's line length costs, the findings that survive it, and the format and lint steps in CI alongside a locked sync. Milestone: `ruff format --check` and `ruff check` are clean and enforced on every PR, the suite is still green, and the guide no longer describes a toolchain that isn't there.

**Phase 3 — type checking.** `pyright` as a pinned dev dependency in basic mode over `server/` and `scripts/`, all 84 errors fixed with no suppression list, and the type-check step in CI. The latent `AttributeError` in the kit-equipping path is fixed here as the defect it is. Milestone: `pyright` reports zero errors with no ignore list in `pyproject.toml`, and party creation still works when driven.

**Phase 4 — the client gate.** A linter over `static/`, pinned to an exact version and failing on warnings, its ten findings fixed, and the `app.js?v=N` drift check that turns the cache-bust rule into a gate — script plus its own tests, following the precedent that a script gating the build is code and code here is tested. Milestone: the client is linted on every PR, and a change to `app.js` that forgets the version bump fails.

**Phase 5 — the contract gates.** The two tests that turn honor-system rules into observers: a wire-contract test pinning the payload's key set so a new field must be added consciously, and a seeded end-to-end drive of the scripted route `AGENTS.md` promises, asserting structure rather than dice. `AGENTS.md` gains its standing-gate section here, once the gate it describes is real. Milestone: adding a throwaway key to the state payload fails the suite, and the opening route is proven by CI rather than by a human remembering to walk it.

## Sequencing

Phase 1 first: it is the smallest, it conflicts with nothing, and it governs how phases 2–5 are planned and reviewed. Phase 2 next and merged promptly — its reflow touches 28 files at once and is the merge-conflict hazard of the whole effort, so live branches rebase onto it rather than fight it. Phase 3 depends on phase 2 only for the shared `pyproject.toml` edits. Phases 4 and 5 are independent of each other and of 3; either may land first. `AGENTS.md`'s standing-gate section is deliberately last, because a guide that documents a gate before the gate exists is the kind of false sentence this project treats as a defect.

## Non-goals

- **A `CHANGELOG.md` and a release runbook.** osrlib has both because it is published to PyPI under semantic versioning, where a changelog has readers and a version means something. osr-web is a private, unreleased app whose `version = "0.1.0"` nobody reads and nothing consumes, and whose git log already *is* a changelog — every commit subject is a full sentence naming the change. A changelog here would be a file maintained for no reader. Revisit if osr-web is ever deployed or released, which would also be the moment a version number starts meaning something.
- **A published documentation site.** osrlib builds and deploys mkdocs because it has a public API that outside developers read. osr-web's surface is a UI plus a private JSON API already documented in `.claude/skills/api-surface/SKILL.md`, for an audience of one repository.
- **pyright strict mode.** Matching osrlib is the objective; osrlib runs basic.
- **Formatting or linting CSS and HTML.** `style.css` and `index.html` are hand-maintained and small; adding a second client toolchain to gate them buys less than the constraint it breaks.
- **Pixel-diffing the screenshots.** Re-declined here, on the argument `scripts/check_screenshots.py` already makes in its own comments.
- **Pre-commit hooks.** The gate is four commands and CI is the enforcement point; a hook that runs them on every commit taxes work-in-progress commits to catch what the pull request catches anyway.
- **Golden transcript files.** osrlib's goldens work because osrlib owns its RNG and its determinism contract. Here the engine is a path dependency on `main`, so a byte-pinned transcript would be a tripwire on someone else's repository. Phase 5's structural assertions are the honest version.
- **Retrofitting plans onto the phases already shipped.** The authored-layer effort's phase 1 landed without one; phases 2–4 get plans like everything else. A plan written after the fact documents nothing that the code and the PR do not already say.
