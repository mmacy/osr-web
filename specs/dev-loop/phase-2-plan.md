# Phase 2 plan — formatting and lint

Record of phase 2 of [the development-loop spec](spec.md), built on 2026-09-14 as one chunk: ruff as the format and lint gate, matching osrlib's configuration, enforced in CI over a locked sync. Milestone, met: `ruff format --check` and `ruff check` are clean and enforced on every pull request, the suite is still green, and the guide no longer describes a toolchain that isn't there.

## Scope

In scope: the dev dependency, the `[tool.ruff]` block, the one-time reflow, the findings that survived it, the locked sync and the two CI steps, and the Style bullet in `AGENTS.md`.

Out of scope: the standing-gate section in `AGENTS.md` (phase 5), any gate other than ruff (phases 3 and 4), and formatting or linting anything but Python (declined in the spec's non-goals).

## Decisions

- **Configuration mirrors osrlib's `[tool.ruff]`** at `../osrlib-python/pyproject.toml`: line length 120, rules B, D, E, F, I, UP, W, the Google docstring convention, and `tests/**` exempt from D. Only `src` and `known-first-party` differ, naming `server` and `scripts`. The acceptance test reads osrlib's file and compares, so drift between the two is a test failure rather than a guide sentence.
- **ruff is a `>=0.11` floor in the dev group and an exact version in `uv.lock`**, the same shape osrlib uses. The lock resolved 0.16.7.
- **Markdown is excluded from discovery.** ruff 0.16 formats fenced Python examples in Markdown by default, and one planning document (`specs/authored-layer/phase-4-plan.md`) would have been reflowed. `extend-exclude = ["*.md"]` keeps prose out of a code gate. The subtractive form was chosen over an `include` list so any Python that lands at the repository root is still checked.
- **No standing exceptions.** No `ignore` list, no `# noqa`, no per-file ignores beyond the tests' docstring exemption. The gate landed on a clean tree, as the spec's greenfield constraint requires.
- **The reflow and the lint fixes are separate commits** so the whitespace-only diff is reviewable on its own.

## The chunk

- Measured before: 30 of 34 files reflow at 120 columns; `ruff check` reports 21 findings (8 I001, 5 D107, 4 D102, 3 E501, 1 B007). These match the spec's July measurement within the two test files added since.
- Acceptance tests: `tests/test_gates.py::TestRuffGate`, four tests committed marked `xfail(reason="chunk: ruff", strict=True)`, unmarked at merge. They pin the dependency and lockfile entry, the configuration against osrlib's, the CI steps and the locked sync, and the guide sentence.
- An independent reviewer compared the token stream of every changed test file before and after the reflow and read every non-whitespace hunk in `server/` and `scripts/`, confirming docstrings added, three strings parenthesised, one loop variable renamed, one import block sorted, and three forward-reference annotations unquoted (valid under the 3.14 floor).

## Definition of done

- `uv lock --check` passes; `uv run ruff format --check` reports 38 files already formatted; `uv run ruff check` reports all checks passed.
- `uv run pytest` is green: 499 passed, 15 xfailed (the remaining phase 3 and 4 markers).
- CI runs `uv sync --locked`, `uv run ruff format --check`, and `uv run ruff check` before the unit suite.
