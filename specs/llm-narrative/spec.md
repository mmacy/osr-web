# Spec: LLM-provided narrative

Status: draft
Owner: unassigned
Depends on: nothing outside this repo (Ollama is an external runtime dependency at play time, not at build time)

## Summary

Add an optional narrative layer that asks a large language model to embellish significant moments of play — entering a new area, an encounter erupting, a battle won or lost, a death, a trap, a haul of treasure — with a few sentences of referee prose. The deterministic transcript produced by `server/narrate.py` remains the ground truth and is never delayed, altered, or replaced; LLM passages arrive asynchronously and are woven into the transcript as a new entry kind. The feature is off unless a provider is configured. Ollama (local models) is the first provider; the provider seam is designed so hosted providers (Anthropic, OpenAI-compatible) drop in later without touching the pipeline.

## Motivation

The narrator's lines are truthful and terse by design — `"Monsters! 3 × Skeleton, 30 feet away."` tells the player exactly what the kernel knows. What it can't do is sound like a referee at the table: react to the party's condition, connect this room to the last one, or give a battle its due drama. The adventure documents carry evocative descriptions, hooks, and named places; an LLM can weave those with the mechanical facts into storybook prose that matches the app's existing serif "referee voice" — without ever being trusted to state a fact the engine didn't.

## Goals

- LLM-written prose for significant beats of play, appended to the transcript.
- A provider abstraction with Ollama first; adding a provider means one small class and env vars, no pipeline changes.
- Zero impact on game correctness, latency, or availability: command responses return exactly as fast as today, and every failure mode degrades to the current experience.
- Zero new required Python dependencies (providers speak plain HTTP JSON via the standard library).
- Prompts built from player-visible material, mirroring the wire contract — plus exactly one marked referee-steering block (authored `guidance`, adopted with the authored layer; see Design principles), which never crosses the wire and is never quoted.

## Non-goals

- Replacing the deterministic transcript. Mechanics lines (`mech`, `danger`, `treasure`, `system`) always render; the LLM adds, never substitutes. One deliberate exception (added after v1): on `area_entered`, the passage *stands in for* the module's raw `prose` — the referee reads the room to the players, keeping the module text's secrets unspoken — with the raw text as the fail-open fallback whenever no passage arrives.
- LLM-driven game logic, NPC dialogue that affects state, or LLM interpretation of player input. osrlib owns every rule; this layer is presentation only.
- Token streaming to the browser. Passages are 2–4 sentences; the delivery channel below leaves room to add SSE later.
- Per-player or per-session opt-in UI. v1 is a server-level configuration.
- Persisting narrative into save documents (saves are pure osrlib documents; see Persistence).

## Design principles

1. **The engine narrates facts; the LLM narrates color.** A passage may restate what the deterministic entries already said, in better prose. It must not introduce mechanical claims (damage numbers, HP, map facts) — the prompt forbids it, and even when the model disobeys, players can always check the `mech` lines beside it.
2. **Player-safe prompts, by construction — with one named exception.** The prompt is assembled from material that already crosses the wire: rendered transcript entries, area names and descriptions the party has entered, adventure name and hooks, and a party summary from the player projection. The one deliberate exception is authored `guidance` (spec: `specs/authored-layer/spec.md`): referee-only steering read off the adventure document — the occupied level's ambient slot, an active quest's block with its revealed-but-incomplete objectives' riding along, a trigger's in its firing run, a door gate's on its success — rendered as a single marked block the prompt orders the model never to quote, reveal, or state as fact. Referee events, monster HP, hidden geometry, the master seed, and unvisited-area content still never reach the model. For everything outside that block, player safety remains structural — the model cannot leak what it never saw. For the guidance block alone it is structural-plus-instructed: the block enters through exactly one keyword-only parameter (`build_prompt(..., guidance=...)`), is assembled in exactly one place (`observe`, under the game lock), never crosses the wire on any surface, and the existing output sanitation caps the blast radius of a disobedient model at bad prose beside deterministic entries that anchor the truth.
3. **Fail open, silently.** No provider configured, provider down, model missing, timeout, empty output — all of these mean "no passage appears," never an error in the transcript or a failed request.
4. **Never hold the game lock across an LLM call.** Everything the prompt needs is snapshotted as plain strings while the lock is already held in the command path; generation happens on a worker thread with no session access at all.

## Architecture

```
POST /api/games/{id}/command
  └─ (under game.lock, unchanged path)
       execute → narrator.render(delta) → beat detector
                                             │ snapshot: anchor, entries, context strings
                                             ▼
                                     game.narration.enqueue(beat)     ← bounded queue
                                                                         │
                                   (worker thread, no lock, no session) │
                                             provider.generate(system, prompt)
                                                                         │
                                             game.narration.passages ◄──┘
                                                                         ▲
GET /api/games/{id}/narration?after=N  ── client polls ─────────────────┘
```

New module `server/llm.py` holds the provider protocol, the Ollama provider, configuration loading, and the health check. New module `server/narration.py` holds the beat detector, prompt builder, per-game queue/worker (`NarrationEngine`), and passage store. `Game` in `server/app.py` gains a `narration: NarrationEngine | None` field (`None` when the feature is off, so the hot path stays a no-op).

### The beat pipeline

1. `execute_command` already renders the log delta under the lock. After rendering, if narration is enabled, it calls `game.narration.observe(anchor, entries, session)` — still under the lock.
2. `observe` runs the beat detector over the freshly rendered entries (see Narrative beats). If no beat matches, it returns immediately.
3. If a beat matches, `observe` snapshots everything the prompt will need **as plain strings** (context block, recent transcript tail, the beat's entries) and enqueues a `Beat(anchor, prompt_parts)` on a bounded queue. `anchor` is `len(session.event_log)` at snapshot time — the stable position this passage belongs after.
4. A daemon worker thread (one per game, started lazily on first beat) pulls beats, coalescing everything currently queued into one prompt when the player has raced ahead (see Concurrency). It calls `provider.generate(...)` with a timeout, sanitizes the output, and appends `Passage(seq, anchor, text)` to the game's passage list.
5. The client polls `GET /api/games/{id}/narration?after=<seq>` while work is pending and appends arriving passages to the transcript.

Session creation (`POST /api/games`) runs the same `observe` over the initial log so the opening area description gets a passage too.

## Provider abstraction

In `server/llm.py`:

```python
class NarrativeProvider(Protocol):
    """One LLM backend. Implementations are stateless and thread-safe."""

    name: str

    def generate(self, system: str, prompt: str, *, timeout: float) -> str:
        """One completion. Raises ProviderError on any failure; never returns None."""

    def health_check(self) -> str | None:
        """None when ready; otherwise a human-readable reason narration is disabled."""
```

`ProviderError` is the single exception type the pipeline catches; wrap transport errors, HTTP errors, and malformed responses in it. Providers use `urllib.request` — every planned backend is plain HTTP JSON, so the project keeps its zero-new-dependencies posture.

**Endpoint selection rule**: when a backend offers both a modern responses-style API and a legacy completions-style API, implement the provider against the responses API. Legacy completions endpoints are maintenance-mode surfaces; new capabilities (and sometimes new models) land only on the newer API, and building on it keeps providers from needing a rewrite later.

A registry maps provider ids to constructors:

```python
_PROVIDERS = {"ollama": OllamaProvider}  # later: "anthropic", "openai", "openai_compatible"
```

### Ollama provider (v1)

- Endpoint: `POST {base_url}/api/chat` with `{"model": ..., "messages": [{"role": "system", ...}, {"role": "user", ...}], "stream": false, "options": {"temperature": 0.8, "num_predict": 200}, "keep_alive": "15m"}`. The reply's `message.content` is the passage.
- Default `base_url` is `http://localhost:11434`.
- `keep_alive` keeps the model resident between beats so only the first passage of a session pays the model-load cost.
- `health_check` calls `GET {base_url}/api/tags` and verifies the configured model appears in the list (match on the name before the `:tag` when the configured name carries no tag). Failure reasons: server unreachable, model not pulled.

### Future providers (design targets, not v1 deliverables)

- **Anthropic**: `POST https://api.anthropic.com/v1/messages` with `x-api-key`, `system` as a top-level field. A small fast model (e.g. `claude-haiku-4-5`) fits this workload.
- **OpenAI**: `POST {base_url}/v1/responses` with `Authorization: Bearer`, per the endpoint selection rule — the Responses API is OpenAI's primary surface; Chat Completions is legacy.
- **OpenAI-compatible local/self-hosted backends** (LM Studio, vLLM, llama.cpp server, and Ollama's own compatibility layer): most implement only `POST {base_url}/v1/chat/completions`, so a separate compatibility provider targets that. Where such a backend grows Responses API support, prefer it.

The protocol above is sufficient for both; neither requires pipeline changes.

## Configuration

All server-side env vars, following the `OSR_WEB_ADVENTURE`/`OSR_WEB_ADVENTURES` precedent. Read once at startup in `server/llm.py::load_narration_config()`.

| Variable | Meaning | Default |
| --- | --- | --- |
| `OSR_WEB_NARRATOR` | Provider id (`ollama`). Unset or empty: feature off. | unset (off) |
| `OSR_WEB_NARRATOR_MODEL` | Model name (e.g. `llama3.2`, `qwen3:8b`). Required when a provider is set. | none |
| `OSR_WEB_NARRATOR_URL` | Provider base URL. | provider-specific |
| `OSR_WEB_NARRATOR_API_KEY` | API key for hosted providers. Ignored by `ollama`. | none |
| `OSR_WEB_NARRATOR_TIMEOUT` | Per-generation timeout, seconds. | `30` |

Startup behavior: when a provider is configured, run `health_check()` once. On failure, log one clear warning with the reason (`narration disabled: model 'llama3.2' not found — try 'ollama pull llama3.2'`) and start with narration off. A misconfigured narrator must never keep the game from serving.

## Narrative beats

A beat is a moment worth prose. The detector inspects the rendered entries of one command (their `kind` plus the originating event codes, which `observe` receives alongside) against this table:

| Beat | Trigger | Prompt emphasis |
| --- | --- | --- |
| `area_entered` | First-visit `place` + `prose` pair rendered (`Narrator.seen_areas` just grew) | Re-render the module's own description in the referee's voice; sensory detail; no new map facts |
| `encounter_started` | `encounter_started` event rendered | Dread and first impressions; monster count/kind exactly as stated |
| `battle_ended` | `battle.ended.*` rendered | The shape of the whole fight (its `mech` lines are in the prompt); victory, flight, or defeat tone |
| `member_died` | `combat.death.died` for a party member | A short elegy; name the fallen, nothing more |
| `trap_sprung` | `exploration.trap.sprung` rendered | Sudden violence; what the party perceives |
| `treasure_found` | `treasure`-kind entry with items or ≥ 50 gp | Wonder; describe only the listed items |
| `game_over` | `game_over` event rendered | A closing paragraph for the campaign |

Deliberately **not** beats: ordinary movement, individual battle rounds, searches that find nothing, light/rest bookkeeping, town commerce. Local models take seconds per generation; narrating every command would back the queue up permanently and cheapen the prose. The table lives as data (`BEATS` in `server/narration.py`) so tuning is an edit, not a refactor.

At most one beat fires per command; when several match (a trap springs and kills someone), the highest row in the table wins and the others' entries still appear in the prompt as facts.

## Prompt design

Two parts, both plain text.

**System prompt** (constant): establishes the voice and the rules of the game the model is playing:

- You are the referee of an old-school fantasy RPG, narrating in third person, past-brushing-present tense, matching a storybook register.
- Write 2–4 sentences. No markdown, no headings, no quotation of the mechanics, no dice, no numbers unless they appear in the facts.
- Never invent rooms, exits, items, monsters, damage, or outcomes. Everything you may treat as true is in the FACTS block. If facts are thin, write atmosphere, not invention.
- Never address the player, never mention rules, never break the fourth wall.

**User prompt** (per beat), assembled from snapshotted strings:

```
ADVENTURE: <name> — <description>
PARTY: <name the class, level, and visible condition of each living member>
LOCATION: <area name>: <area description as the module wrote it>
RECENT EVENTS:
<the last ~12 transcript entries, prefixed by their kind>
JUST NOW (<beat name>):
<the entries this beat is about>
REFEREE GUIDANCE (private steering — never quote it, never reveal it, never state it as fact):
<the authored guidance blocks in play at this beat — absent entirely when the adventure authors none>

Narrate this moment.
```

Everything in the FACTS blocks is text the player has already seen or is entitled to see: transcript entries come out of `Narrator.render`, the area description is the same string the `prose` entry carried, the party summary is derived from the player projection. The one block that is not is REFEREE GUIDANCE — authored steering read off the adventure document, present only when the adventure authors any and absent entirely otherwise, marked never-to-be-quoted (see Design principles). The prompt builder takes strings, not the session — it cannot leak what it is never handed — and guidance is handed to it through a single keyword-only parameter, assembled at one seam (`observe`, under the game lock), which is what keeps "never crosses the wire, never prints as written" auditable in one place.

**Output sanitation**: trim whitespace, strip wrapping quotes, collapse internal newlines to spaces, strip markdown emphasis characters, hard-cap at 600 characters (cut at the last sentence boundary before the cap), drop the passage entirely if empty after cleaning. Adventure documents are third-party content, so treat their text as untrusted for prompt purposes: the model has no tools and its output is rendered as `textContent` (the existing `appendLog` path never injects HTML), so the blast radius of a hostile description is bad prose, which sanitation caps.

## API changes

### State payload

`_state()` gains one player-safe field on every response that carries state:

```json
"narration": {"enabled": true, "pending": 1, "seq": 7}
```

`enabled` reflects the server config (post-health-check), `pending` is the number of beats queued or generating, `seq` is the id of the newest stored passage. When the feature is off: `{"enabled": false, "pending": 0, "seq": 0}`. Nothing here leaks referee state.

### New endpoint

```
GET /api/games/{game_id}/narration?after=<seq>
```

Returns `{"entries": [{"seq": 8, "anchor": 141, "kind": "narrative", "text": "..."}], "pending": 0}` — every stored passage with `seq > after`, in order, plus the current pending count. Unknown game id: 404, matching the rest of the API. The endpoint takes the game lock only long enough to copy the passage list tail (appends come from the worker thread).

### Reload behavior

`GET /api/games/{id}` currently rebuilds the transcript by rendering the full event log. With narration on, it interleaves stored passages at their anchors: render `event_log[0:a₁]`, emit passage 1, render `event_log[a₁:a₂]`, emit passage 2, and so on. `Narrator.render` already accepts slices and its `seen_areas`/`_labels` state accumulates correctly across sequential slices, so this is a loop around existing machinery. Passages still generating at reload time simply arrive through the poll endpoint afterward.

### Unchanged surfaces

`POST .../command` response shape is unchanged apart from the `narration` state field — the deterministic `log` delta returns immediately, exactly as today. The save endpoint and save documents are untouched.

## Client changes

All in `static/` (no build step; bump `app.js?v=N` in `index.html` per the standing rule).

- **New log entry kind `narrative`**: rendered through the existing `appendLog` path (`log-entry narrative`). Style: the serif prose voice, italic, with a moon-silver left border and slight indent so it reads as the bard's aside against the referee's `prose`. Colors come from existing `:root` tokens only.
- **Poll loop**: after any response whose `narration.pending > 0` (or whose `seq` is ahead of the client's high-water mark), poll `/narration?after=<hwm>` every 1500 ms until `pending` is 0 and no new entries arrive, capped at 90 s per burst. Arriving passages append to the transcript in `seq` order. The client keeps `hwm` in memory; on page reload the interleaved `log` from `GET /api/games/{id}` already contains stored passages, and `hwm` re-seeds from `narration.seq`.
- **Pending indicator**: while a poll burst is active, show a subtle placeholder line (`✦ …`) at the transcript tail; replace it with the passage or remove it on timeout. One indicator regardless of queue depth.
- **No settings UI** in v1: when `narration.enabled` is false the client never polls and renders nothing new.

Passages append at the transcript tail in arrival order rather than being spliced next to their anchor entries mid-scroll — during live play the anchor is at most a few commands back, and retro-splicing scrolled DOM is churn for no readability gain. Anchors exist for the reload interleave, where order is rebuilt from scratch.

## Concurrency and failure handling

- **Lock discipline**: `observe` runs under the game lock the command path already holds and does string snapshots only — no I/O. The worker thread never touches `session` or takes the game lock except for the single append of a finished passage (guard the passage list with the game lock or a dedicated smaller lock; either is fine, document the choice in code).
- **Bounded queue with coalescing**: queue capacity 4 beats. When the worker wakes it drains everything queued and, if more than one beat is waiting, merges them into a single prompt ("JUST NOW" carries all of them, oldest first) producing one passage anchored at the newest beat. When the queue is full, the oldest queued beat is dropped. A fast player never builds an unbounded backlog and never waits.
- **Timeouts**: each `generate` call gets `OSR_WEB_NARRATOR_TIMEOUT`. On timeout or `ProviderError`, drop the beat, decrement `pending`, log at debug level. After 3 consecutive failures, disable narration for that game (worker exits, `enabled` flips false in its state payload) — the local model has almost certainly gone away, and silence is better than a stall every beat.
- **Thread lifecycle**: worker threads are daemons, started on the first beat, one per game, idle-blocking on their queue. Games already live forever in `_games`; the worker adds one parked thread per active game, which matches the app's in-memory session posture.
- **Determinism**: seeded games (`{"seed": 42}`) stay mechanically reproducible — passages never influence state, and the deterministic transcript is byte-identical with narration on or off.

## Persistence

Passages live in memory on the `Game`, alongside the session and narrator. Save documents are osrlib's — this app does not extend them, so narrative is lost across save/restore and server restarts, the same way transcript scroll position is. This is acceptable for a presentation layer; if it ever matters, a sidecar `saves/{code}.narration.json` is the escape hatch (future work, not v1).

## Testing and verification

Unit tests (introduce `tests/` with pytest, the project standard):

- **Beat detector**: each trigger row fires on a synthetic entry/event list; movement and no-op commands fire nothing; the multi-match rule picks the top row.
- **Prompt builder**: given snapshotted strings, output contains the facts blocks and nothing else — assert the builder's signature takes strings only (the player-safety property is structural, but a test documents it).
- **Queue/coalescing**: with a `FakeProvider` (records prompts, returns canned text, optionally raises or sleeps), verify coalescing, capacity drop, the 3-failure disable, and that `pending`/`seq` bookkeeping matches.
- **Sanitation**: markdown stripping, cap-at-sentence, empty-drop.
- **Interleave**: a rendered log plus anchored passages reloads in the right order via the segmented render.

End-to-end verification (per AGENTS.md practice, a short `urllib` script against `/api/games`):

1. Start Ollama with a small model pulled; start the server with `OSR_WEB_NARRATOR=ollama OSR_WEB_NARRATOR_MODEL=<model>`.
2. `POST /api/games {"seed": 42}` — expect `narration.enabled: true` and, after polling, a passage for the entrance area.
3. Three moves east from (3,3) to the keyed skeleton encounter — expect an `encounter_started` passage; fight it out — expect a `battle_ended` passage. *(Route as of 2026-09-02: the bundled document is now* The Cold Vein *— entrance (2,15), three moves north to four kobolds at (2,12); see AGENTS.md.)*
4. Kill Ollama mid-session — commands keep working, passages stop, no errors surface; after three beats, `enabled` flips false.
5. Reload the page — stored passages reappear in position.
6. Unset `OSR_WEB_NARRATOR` — byte-identical behavior to today.

## Acceptance criteria

- With no narrator configured, every response and the client experience are unchanged.
- With Ollama configured and healthy, each beat in the table yields at most one passage of ≤ 600 characters, appearing in the transcript without delaying any command response.
- Prompts contain only player-visible material plus the one marked referee-guidance block (code-review gate: the prompt builder accepts strings, never the session or events, and authored `guidance` enters through its single keyword-only parameter — never through `location_line`, an emphasis string, or the transcript).
- Provider outage at any point never produces a player-visible error or a request failure.
- Adding a second provider requires only a new class in `server/llm.py`, a registry entry, and env vars.

## Future work

- **Anthropic and OpenAI-compatible providers** (design targets above).
- **SSE delivery** replacing the poll loop, and optionally token streaming into the pending line.
- ~~**Replacement mode**~~: implemented — passages stand in for raw module descriptions on `area_entered` (`replace_id` on held prose entries, `replaces` on passages, fail-open reveal client-side, in-place interleave on reload). A click-through to the original module text remains future work.
- **Battle-round color** for fast hosted models, gated on measured latency.
- **Narrative sidecar persistence** so restores keep their prose.
- **Per-game toggle** on the title screen once there is more than one thing to configure.
