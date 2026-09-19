# Phase 4 plan — the client gate

Record of phase 4 of [the development-loop spec](spec.md), built on 2026-09-14 as one chunk: a linter over `static/` pinned to an exact version and failing on warnings, its findings fixed, and the `app.js?v=N` drift check that turns the cache-bust rule into a gate, as a script with its own tests. Milestone, met: the client is linted on every pull request, and a change to `app.js` that forgets the version bump fails.

## Scope

In scope: the oxlint step, the nine findings in `static/app.js`, the version bump those edits require, `scripts/check_app_version.py` with its tests, the two CI steps, and one sentence in the client rule.

Out of scope: any client toolchain (declined by the spec's design constraints: no `package.json`, no `node_modules`, no build step), linting CSS or HTML (declined in the non-goals), and a type checker for the client.

## Decisions

- **oxlint, at an exact version, through `npx --yes`.** One binary that reads the files and exits, installed by nothing in the repository. `--deny-warnings` is what makes the gate mean zero findings rather than a burn-down list. The version is pinned in the workflow line, and the acceptance test fails if the pin is loosened or the flag dropped.
- **Findings are fixed in the code, not configured away.** Eight unused catch bindings became `catch {`, and one `...(params || {})` became `...params`, after enumerating every caller to confirm nothing but an object or a nullish value reaches it. No linter config file exists.
- **The cache-bust gate compares the pull request's base with its head.** If `static/app.js` is byte-identical at the two refs, it passes. Otherwise the `app.js?v=N` value at head must differ from the value at base, and the reference must still exist. A pull request merges as one change, so a bump anywhere in it covers every client edit in it, and an edit reverted within it needs no bump. A first version anchored the comparison to the newest commit touching the client, and review showed it failing both of those cases; the acceptance test that had forced that rule was wrong and was corrected on the branch.
- **The check runs on pull requests only**, against `origin/<base branch>`, and the Tests job's checkout fetches full history so the base ref exists to be compared against. A push to main has already merged its pull request.
- **An unresolvable ref is exit 2**, distinct from a failed rule at exit 1, and is reported with git's own message rather than a traceback.

## The chunk

- Measured before: 9 warnings at oxlint 1.83.0 (the spec's July count of 10 included a duplicated `else if` condition that had since been fixed).
- Acceptance tests: `tests/test_gates.py::TestClientGate` (the CI steps, the pin, the flag, the rule sentence, and a standing test that no toolchain files exist at the root) and `tests/test_check_app_version.py` (ten tests over throwaway git repositories: untouched client, bumped, forgotten, reference removed, reverted edit, named head, exit codes, unknown ref). Committed marked and corrected on the chunk branch, unmarked at merge.
- An independent reviewer reproduced the reverted-edit false failure in a scratch repository, enumerated all seventeen callers of the spread site, and checked the CI comment's account of a shallow checkout (wrong in its first form, corrected).

## Definition of done

- `npx --yes oxlint@1.83.0 --deny-warnings static/` reports no findings; `node --check static/app.js` parses.
- `uv run python scripts/check_app_version.py --base origin/main` passes on the merged tree, and the ten script tests pass.
- `uv run pytest` is green: 515 passed, no expected failures left.
- CI runs the client lint after the type check and the version check on pull requests.
