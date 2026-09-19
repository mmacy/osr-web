/* osr-web — an old-school dungeon crawler client over the osrlib API.

   The server owns every rule; this file owns the wireframe viewport, the
   summoned automap (with player notes), the transcript, the beat-paced
   battle round, and the command surface. Any adventure forged by osr-forge
   can be served; the front door (title → library → staging) is a real
   screen-state machine, and no game session exists until the player commits
   with "Begin the adventure".

   Everything rendered comes from the player projection: S.view mirrors the
   engine's PlayerView model (schema and field docs:
   osrlib-python/src/osrlib/crawl/views.py), refreshed whole on every
   response, so there is no client-side rule state to drift. The player-safe
   extras beside it (S.cell, S.spellbook, ...) are noted per-field on S
   below; the endpoints they ride on are listed in AGENTS.md.

   Editing this file? Bump the app.js?v=N query in index.html — the browser
   memory-caches this script and serves stale code with no error otherwise. */

"use strict";

/* ---------- state ---------- */

const S = {
  gameId: null,
  view: null,       // the engine's PlayerView — the whole truth the client may see
  cell: null,       // player-safe context for the party's cell (area name, treasure, light)
  spellbook: {},
  spellBooks: {},
  learnable: {},    // per-member learnable-spell candidates for open book picks (player-safe, from the server)
  hooks: [],
  dungeons: [],
  adventures: null,   // library entries, fetched lazily
  saves: [],          // save summaries for restore / veterans import
  adventureId: null,  // library id of the chosen (or current game's) adventure
  catalog: null,      // creation-wizard rulebook data, fetched lazily
  builder: null,      // roll-your-own party wizard state, when open
  busy: false,
  pacing: false,    // a battle round is revealing beat by beat
  sheets: {},       // per-member character-sheet extras (player-safe, from the server)
  sheetOpen: null,  // member id whose character sheet is open
  decl: {},         // battle declarations by character id
  declPicked: {},   // the members whose declaration the player set by hand this battle
  declTarget: null, // chosen target group when a battle has several
  // Town control state survives re-renders the way S.declTarget does: buyer,
  // temple picks, spell-prep picks (keyed "level:slot" per caster), study
  // picks (keyed "memberId:level:slot"), and which shop groups stand open.
  town: { buyer: null, templeWho: null, templeService: null, prep: {}, study: {}, shopOpen: { gear: true } },
  // The exploring cast card's picks (caster, spell, member target), persisted
  // across re-renders the same way.
  explore: { caster: null, spell: null, target: null },
  templeServices: [], // the temple's posted price list (the engine's table, served)
  narration: { enabled: false, pending: 0, seq: 0 }, // server narration state
};

const $ = (id) => document.getElementById(id);

const CLASS_NAMES = {
  fighter: "Fighter", cleric: "Cleric", thief: "Thief", magic_user: "Magic-user",
  dwarf: "Dwarf", elf: "Elf", halfling: "Halfling",
};

const DIRS = ["north", "east", "south", "west"];
const VEC = { north: [0, -1], east: [1, 0], south: [0, 1], west: [-1, 0] };
const LEFT = { north: "west", west: "south", south: "east", east: "north" };
const RIGHT = { north: "east", east: "south", south: "west", west: "north" };
const OPPOSITE = { north: "south", south: "north", east: "west", west: "east" };

/* ---------- api ---------- */

async function apiFetch(path, body, method) {
  const options = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (body !== undefined) {
    options.headers["content-type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api" + path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `request failed (${response.status})`);
  }
  return response.json();
}

function api(path, body, method) {
  return apiFetch("/games" + path, body, method);
}

async function fetchLibrary() {
  const [adventures, saves] = await Promise.all([
    apiFetch("/adventures"),
    apiFetch("/saves"),
  ]);
  S.adventures = adventures.adventures;
  S.saves = saves.saves;
}

function adventureIdByName(name) {
  const entry = (S.adventures || []).find((a) => a.name === name);
  return entry ? entry.id : null;
}

/* House rules are a global preference: they ride on every fresh premade party
   and prefill the creation wizard. The server owns what they mean; the client
   only remembers the choice (localStorage, like the map notes). */
function storedHouseRules() {
  let rules = {};
  try {
    rules = JSON.parse(localStorage.getItem("osrweb_house_rules")) || {};
  } catch {
    /* unreadable preference — fall back to by-the-book rules */
  }
  return {
    ability_method: rules.ability_method === "4d6_drop_lowest" ? "4d6_drop_lowest" : "3d6",
    max_hp: !!rules.max_hp,
  };
}

function saveHouseRules(rules) {
  localStorage.setItem("osrweb_house_rules", JSON.stringify(rules));
}

function absorb(payload, fullLoad) {
  if (payload.game_id) S.gameId = payload.game_id;
  S.view = payload.view;
  S.cell = payload.cell;
  S.sheets = payload.sheets || {};
  S.spellbook = payload.spellbook || {};
  S.spellBooks = payload.spell_books || {};
  S.learnable = payload.learnable || {};
  S.templeServices = payload.temple_services || [];
  if (payload.hooks) S.hooks = payload.hooks;
  if (payload.dungeons) S.dungeons = payload.dungeons;
  S.narration = payload.narration || { enabled: false, pending: 0, seq: 0 };
  if (fullLoad) {
    // A full-payload log already interleaves stored passages at their anchors.
    stopNarrationPoll();
    NARR.hwm = S.narration.seq;
  }
  maybePollNarration();
}

async function command(payload) {
  if (S.busy || S.pacing || !S.gameId) return null;
  S.busy = true;
  try {
    const result = await api(`/${S.gameId}/command`, payload);
    // Capture the round number from the pre-round view before absorb replaces it.
    const round =
      payload.command_type === "resolve_battle_round" && S.view.encounter
        ? S.view.encounter.battle_round || 1
        : null;
    absorb(result);
    if (round !== null && result.accepted && result.log.length && !reducedMotion()) {
      paceBattle(result.log, round);
      return result;
    }
    appendLog(result.log);
    maybePollNarration(); // appendLog may have parked prose in NARR.slots
    if (!result.accepted) {
      const message = result.rejections.map((r) => r.message).join(" ");
      appendLog([{ kind: "reject", text: message }]);
    }
    render();
    return result;
  } catch (error) {
    toast(error.message, false);
    return null;
  } finally {
    S.busy = false;
  }
}

/* ---------- battle pacing: the round lands one beat at a time ---------- */

const PACE = { timer: null, prevEl: null };

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function stopPacing() {
  if (PACE.timer) clearTimeout(PACE.timer);
  PACE.timer = null;
  if (PACE.prevEl) PACE.prevEl.classList.remove("now");
  PACE.prevEl = null;
  setActingRow(null);
  $("roundbar").classList.add("hidden");
  S.pacing = false;
}

function setActingRow(text) {
  for (const row of document.querySelectorAll("#party .member")) {
    const name = row.dataset.name;
    row.classList.toggle("acting", !!text && !!name && text.startsWith(name));
  }
}

function paceBattle(entries, round) {
  S.pacing = true;
  const bar = $("roundbar");
  bar.classList.remove("hidden");
  $("round-label").textContent = `round ${round}`;
  const ticks = $("round-ticks");
  ticks.innerHTML = entries.map(() => "<i></i>").join("");
  let i = 0;
  const step = () => {
    if (PACE.prevEl) PACE.prevEl.classList.remove("now");
    if (i >= entries.length) {
      stopPacing();
      maybePollNarration();
      render();
      return;
    }
    const entry = entries[i];
    appendLog([entry], { collapse: false });
    const el = $("log").lastElementChild;
    if (el) {
      el.classList.add("now");
      PACE.prevEl = el;
    }
    const text = entry.text || "";
    $("round-beat").textContent = text.length > 96 ? `${text.slice(0, 94)}…` : text;
    if (ticks.children[i]) ticks.children[i].classList.add("done");
    setActingRow(text);
    i += 1;
    PACE.timer = setTimeout(step, 700);
  };
  step();
}

/* ---------- helpers ---------- */

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML.replace(/"/g, "&quot;");
}

// One dungeon turn is 60 rounds. Every surface that shows in-world time goes
// through this, so the HUD clock and the journal's stamps cannot drift apart.
function turnOf(rounds) {
  return Math.floor(rounds / 60);
}

function livingMembers() {
  return S.view.party.filter((m) => m.current_hp > 0 && !m.conditions.includes("dead"));
}

/* The group this round's declarations are aimed at. Each group stands at its own
   distance, and the distance is what makes an attack legal or illegal, so the
   menu, the defaults, and the target select must all read the same group.
   Repairs a stale pick — a group the party has since wiped out — on the way. */
function targetGroup() {
  const enc = S.view.encounter;
  if (!enc) return null;
  const groups = enc.groups.filter((g) => g.count > 0);
  if (!groups.some((g) => g.id === S.declTarget)) S.declTarget = groups[0] && groups[0].id;
  return groups.find((g) => g.id === S.declTarget) || null;
}

/* Exactly the members a battle round must name — living and able to act, as the
   encounter view states. A slept or paralysed member is not one of them, and
   a round that names anybody else is rejected whole
   (battle.declaration.roster_mismatch), so this is the roster the declaration
   panel renders and the roster "Resolve round" sends. */
function declarers() {
  const enc = S.view.encounter;
  const ids = (enc && enc.declarers) || [];
  return ids.map((id) => S.view.party.find((m) => m.id === id)).filter(Boolean);
}

function memberByClass(...classes) {
  for (const cls of classes) {
    const member = livingMembers().find((m) => m.class_id === cls);
    if (member) return member;
  }
  return livingMembers()[0] || null;
}

/* Every weapon this member has in hand, in wielded order — a mundane weapon's
   template id, or a magic arm's instance id, identified or not. Weapon
   templates hold their damage directly, gear (a torch) under a combat facet,
   and a magic arm reports the `qualities` of the weapon underneath it, which
   is what marks the arm as something to swing rather than a shield or a ring.
   The engine states those facts whenever the arm's masked display names its
   base weapon ("a dagger with a faint aura"), identified or not, and states
   neither when the display names only a category ("a staff"). So `qualities`
   alone, not `identified`, is what makes an arm declarable — the engine
   accepts the swing either way and identifies the arm on the first attack
   roll. */
function wieldedWeapons(member) {
  const weapons = [];
  for (const w of member.inventory.wielded) {
    if (w.instance_id) {
      if (w.qualities) weapons.push(w.instance_id);
      continue;
    }
    const template = w.template;
    if (!template) continue;
    if (template.damage || (template.combat && template.combat.damage)) weapons.push(template.id);
  }
  return weapons;
}

function partyGold() {
  return S.view.party.reduce((sum, m) => {
    const p = m.inventory.purse;
    return sum + p.gp + p.pp * 5 + Math.floor(p.ep / 2) + Math.floor(p.sp / 10) + Math.floor(p.cp / 100);
  }, 0);
}

function torchCount() {
  let count = 0;
  for (const m of S.view.party) {
    for (const item of m.inventory.items) {
      if (item.template && item.template.id === "torch") count += item.quantity;
    }
  }
  return count;
}

function torchBearer() {
  for (const m of livingMembers()) {
    if (m.inventory.items.some((i) => i.template && i.template.id === "torch")) return m;
  }
  return null;
}

function spikeCount() {
  let count = 0;
  for (const m of S.view.party) {
    for (const item of m.inventory.items) {
      if (item.template && item.template.id === "iron_spikes") count += item.quantity;
    }
  }
  return count;
}

function lightState() {
  const effect = S.view.effects.find((e) => e.kind === "light");
  return effect ? effect.remaining_rounds : null;
}

function currentLevel() {
  const loc = S.view.location;
  if (loc.kind !== "dungeon") return null;
  return S.view.explored.find(
    (l) => l.dungeon_id === loc.dungeon_id && l.level_number === loc.level_number
  ) || null;
}

function edgeAt(level, x, y, dir) {
  // Canonical edge keys are north/west only (engine convention): a cell's
  // south edge lives at "x,y+1:north", its east edge at "x+1,y:west".
  let key;
  if (dir === "south") key = `${x},${y + 1}:north`;
  else if (dir === "east") key = `${x + 1},${y}:west`;
  else key = `${x},${y}:${dir}`;
  return level.edges[key] || null;
}

function isExplored(level, x, y) {
  return level.cells.some(([cx, cy]) => cx === x && cy === y);
}

/* ---------- log ---------- */

// Is the log scrolled to (near) the bottom? A little slack absorbs sub-pixel
// rounding and lets us leave the user alone when they've scrolled up to read.
function logPinnedToBottom(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight <= 40;
}

// Pin the log to the bottom, deferred past this frame's layout so freshly
// appended (and streamed-in) content is measured before we scroll. Re-asserts
// on the next frame in case a chunk lands a tick later.
function scrollLogToBottom(log) {
  requestAnimationFrame(() => {
    log.scrollTop = log.scrollHeight;
  });
}

/* Consecutive identical mechanics lines collapse into one line with a ×N
   counter — client-side only, the server transcript stays byte-identical.
   Eligibility is stamped on the element (dataset.text/kind): NARR-managed
   elements (slots, #narr-pending, streaming partials, revealed prose) never
   get stamps, so they can never absorb a line. Battle pacing passes
   { collapse: false } — each beat must stay its own line for the .now
   highlight and the roundbar ticks. */
function appendLog(entries, opts) {
  if (!entries || !entries.length) return;
  const log = $("log");
  const pinned = logPinnedToBottom(log);
  const collapsing = !(opts && opts.collapse === false);
  for (const entry of entries) {
    if (entry.replace_id != null) {
      // Replacement prose never collapses and never gets dataset stamps.
      const div = document.createElement("div");
      if (S.narration.enabled) {
        // Module prose held back: the storyteller's rendition replaces it, or
        // the original is revealed if no passage arrives.
        div.className = "log-entry narrative pending";
        div.textContent = "✦ …";
        NARR.slots.set(entry.replace_id, { el: div, text: entry.text });
      } else {
        div.className = `log-entry ${entry.kind}`;
        div.textContent = entry.text;
      }
      log.appendChild(div);
      continue;
    }
    const last = log.lastElementChild;
    if (
      collapsing &&
      entry.kind !== "narrative" && entry.kind !== "place" &&
      last && last.dataset.text === entry.text && last.dataset.kind === entry.kind
    ) {
      const count = (parseInt(last.dataset.count, 10) || 1) + 1;
      last.dataset.count = String(count);
      let counter = last.querySelector(".log-x");
      if (!counter) {
        counter = document.createElement("span");
        counter.className = "log-x";
        last.appendChild(counter); // the text node stays; the counter rides beside it
      }
      counter.textContent = `×${count}`;
      continue;
    }
    const div = document.createElement("div");
    div.className = `log-entry ${entry.kind}`;
    div.textContent = entry.text;
    div.dataset.text = entry.text;
    div.dataset.kind = entry.kind;
    log.appendChild(div);
  }
  if (pinned) scrollLogToBottom(log);
}

function resetLog() {
  $("log").innerHTML = "";
}

/* ---------- narrative passages (optional LLM color) ---------- */
/* The deterministic transcript never waits on these. The server queues beats,
   a worker writes prose, and this poll appends passages as they land. Any
   failure just ends the burst: the transcript already told the truth. */

/* hwm = highest passage seq applied; slots = replace_id → {el, text} for
   module prose held back awaiting its replacement passage (see appendLog);
   partial = the passage currently streaming in, grown in place each poll. */
const NARR = { timer: null, deadline: 0, hwm: 0, slots: new Map(), partial: null };

function stopNarrationPoll() {
  if (NARR.timer) clearTimeout(NARR.timer);
  NARR.timer = null;
  narrationPending(false);
  abandonPartial(); // a stream cut short by the burst ending fails open too
  revealHeldProse();
}

function revealHeldProse() {
  // Fail open: no passage came for these, so the module text speaks for itself.
  for (const slot of NARR.slots.values()) {
    slot.el.className = "log-entry prose";
    slot.el.textContent = slot.text;
  }
  NARR.slots.clear();
}

function placePassage(entry) {
  // Fast path: this finalizes the passage that has been streaming in place —
  // settle the same element on the clean, sanitized final text.
  const live = NARR.partial;
  if (live && live.seq === entry.seq) {
    const log = $("log");
    const pinned = logPinnedToBottom(log);
    live.el.className = "log-entry narrative";
    live.el.textContent = entry.text;
    if (live.keepId != null) NARR.slots.delete(live.keepId);
    NARR.partial = null;
    if (pinned) scrollLogToBottom(log);
    return;
  }
  const ids = entry.replaces || [];
  const slots = ids.map((id) => NARR.slots.get(id)).filter(Boolean);
  ids.forEach((id) => NARR.slots.delete(id));
  if (!slots.length) {
    appendLog([{ kind: "narrative", text: entry.text }]);
    return;
  }
  const log = $("log");
  const pinned = logPinnedToBottom(log);
  const target = slots.pop(); // the newest held description gets the passage
  for (const extra of slots) extra.el.remove();
  target.el.className = "log-entry narrative";
  target.el.textContent = entry.text;
  if (pinned) scrollLogToBottom(log); // streamed prose can grow the line past view
}

/* The in-flight passage grows in one element across polls. A replacement beat
   streams into the held module-prose slot; a plain beat streams into (or
   creates) the pending indicator. Either way, if the stream is abandoned
   before it finalizes, the element fails open — module prose revealed, or a
   half-written plain passage removed — never a broken transcript. */

function applyPartial(p) {
  const cur = NARR.partial;
  // Drop a live partial the poll no longer describes: gone, or a different
  // generation reusing the same tentative seq (target moved).
  if (cur && (!p || p.seq !== cur.seq || !sameReplaces(p, cur))) abandonPartial();
  if (!p || p.seq <= NARR.hwm) return; // nothing live, or already finalized
  const log = $("log");
  const pinned = logPinnedToBottom(log);
  if (!NARR.partial) NARR.partial = beginPartial(p);
  const text = p.text || "✦ …";
  if (NARR.partial.el.textContent !== text) NARR.partial.el.textContent = text;
  if (pinned) scrollLogToBottom(log);
}

function sameReplaces(p, partial) {
  const a = p.replaces || [];
  const b = partial.replaces;
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

function beginPartial(p) {
  const ids = p.replaces || [];
  const held = ids
    .map((id) => ({ id, slot: NARR.slots.get(id) }))
    .filter((pair) => pair.slot);
  if (held.length) {
    const target = held.pop(); // newest held description streams in place
    for (const extra of held) {
      // collapse any coalesced holds into the one that streams
      extra.slot.el.remove();
      NARR.slots.delete(extra.id);
    }
    target.slot.el.className = "log-entry narrative";
    // keep target's slot in NARR.slots so an abandon can still reveal its prose
    return { seq: p.seq, el: target.slot.el, replaces: ids, keepId: target.id };
  }
  // plain passage: reuse the pending indicator if it is up, else append fresh
  let el = $("narr-pending");
  if (el) el.removeAttribute("id");
  else {
    el = document.createElement("div");
    $("log").appendChild(el);
  }
  el.className = "log-entry narrative";
  return { seq: p.seq, el, replaces: ids, keepId: null };
}

function abandonPartial() {
  const cur = NARR.partial;
  if (!cur) return;
  if (cur.keepId != null) {
    const slot = NARR.slots.get(cur.keepId); // fail open to the module prose
    if (slot) {
      slot.el.className = "log-entry prose";
      slot.el.textContent = slot.text;
      NARR.slots.delete(cur.keepId);
    }
  } else {
    cur.el.remove(); // a plain passage's half-written line is pure color: drop it
  }
  NARR.partial = null;
}

function narrationPending(show) {
  const existing = $("narr-pending");
  if (!show) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const log = $("log");
  const pinned = logPinnedToBottom(log);
  const div = document.createElement("div");
  div.id = "narr-pending";
  div.className = "log-entry narrative pending";
  div.textContent = "✦ …";
  log.appendChild(div);
  if (pinned) scrollLogToBottom(log);
}

function maybePollNarration() {
  const n = S.narration;
  if (!n || !n.enabled || !S.gameId) return;
  // Held prose always warrants a poll: even a beat that already failed
  // server-side needs one round trip to reveal the module text.
  if (n.pending <= 0 && n.seq <= NARR.hwm && NARR.slots.size === 0) return;
  NARR.deadline = Date.now() + 90000; // cap each burst
  if (!NARR.timer) NARR.timer = setTimeout(pollNarration, 300);
}

async function pollNarration() {
  NARR.timer = null;
  const gameId = S.gameId;
  if (!gameId) { stopNarrationPoll(); return; }
  let result;
  try {
    result = await api(`/${gameId}/narration?after=${NARR.hwm}`);
  } catch {
    stopNarrationPoll();
    return;
  }
  if (gameId !== S.gameId) return; // a new tale began mid-fetch
  if (result.entries && result.entries.length) {
    narrationPending(false);
    for (const entry of result.entries) placePassage(entry);
    NARR.hwm = result.entries[result.entries.length - 1].seq;
    S.narration.seq = NARR.hwm;
  }
  applyPartial(result.partial || null); // grow the in-flight passage in place
  S.narration.pending = result.pending;
  if (result.pending > 0 && Date.now() < NARR.deadline) {
    // A live partial and held prose are each their own indicator.
    narrationPending(!NARR.partial && NARR.slots.size === 0);
    // Poll tighter while a passage streams so it reveals smoothly, not in jumps.
    NARR.timer = setTimeout(pollNarration, NARR.partial ? 350 : 1500);
  } else {
    stopNarrationPoll();
  }
}

let toastTimer = null;
function toast(message, ok) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (ok ? " ok" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

/* ---------- confirm dialog ---------- */
/* In-app stand-in for window.confirm: the accept button carries the action's
   own verb and the danger style; Cancel, Escape, or the scrim decline. */

let confirmResolve = null;

function confirmDialog(message, acceptLabel) {
  return new Promise((resolve) => {
    if (confirmResolve) confirmResolve(false);
    confirmResolve = resolve;
    $("confirm-message").textContent = message;
    $("confirm-accept").textContent = acceptLabel;
    $("confirm-overlay").classList.remove("hidden");
    $("confirm-cancel").focus();
  });
}

function settleConfirm(answer) {
  if (!confirmResolve) return;
  $("confirm-overlay").classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  resolve(answer);
}

$("confirm-accept").addEventListener("click", () => settleConfirm(true));
$("confirm-cancel").addEventListener("click", () => settleConfirm(false));
$("confirm-overlay").addEventListener("click", (event) => {
  if (event.target === $("confirm-overlay")) settleConfirm(false);
});
document.addEventListener("keydown", (event) => {
  if (confirmResolve && event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    settleConfirm(false);
  }
}, true);

/* ---------- render root ---------- */

function render() {
  if (!S.view) return;
  document.querySelector(".brand-title").textContent = S.view.adventure_name;
  document.title = S.view.adventure_name;
  $("game").classList.toggle("town-mode", S.view.mode === "town");
  renderHud();
  renderParty();
  renderViewport();
  renderActions();
  renderContext();
  renderTownBoard();
  if (MAP.open) {
    if (mapAvailable()) renderAutomap();
    else toggleMap(false);
  }
  // No availability clause beside it: the map stops being available when the
  // party leaves the dungeon, the journal never does.
  if (JOURNAL.open) renderJournal();
  if (S.sheetOpen) renderSheet();
  // The single router into an ending screen: boot, live play, and every restore
  // (`enterGame` shows the game screen *before* rendering, so this fires there
  // too) come through here rather than each testing the mode for itself.
  if (TERMINAL_MODES.has(S.view.mode) && UI.screen === "game") {
    showScreen(S.view.mode === "victory" ? "victory" : "gameover");
  }
}

function renderHud() {
  const mode = S.view.mode;
  const loc = S.view.location;
  $("brand-sub").textContent =
    loc.kind === "dungeon" ? `level ${loc.level_number}` : S.view.town_name;
  const hudMode = $("hud-mode");
  hudMode.textContent = mode.replace("_", " ");
  hudMode.className = "hud-item" + (mode === "encounter" || mode === "battle" ? " danger" : "");
  $("hud-clock").textContent = `turn ${turnOf(S.view.clock_rounds)}`;
  $("hud-gold").textContent = `${partyGold()} gp`;
  const light = lightState();
  const hudLight = $("hud-light");
  if (S.view.location.kind !== "dungeon") {
    hudLight.textContent = "daylight";
    hudLight.className = "hud-item";
  } else if (light !== null) {
    hudLight.textContent = `light ${Math.max(1, Math.ceil(light / 60))} turns`;
    hudLight.className = "hud-item lit";
  } else {
    hudLight.textContent = "darkness";
    hudLight.className = "hud-item unlit";
  }
}

/* ---------- party ---------- */

function renderParty() {
  const container = $("party");
  container.innerHTML = "";
  const battle = S.view.mode === "battle" && !!S.view.encounter;
  const enc = S.view.encounter;

  const head = document.createElement("div");
  head.className = "party-head";
  head.innerHTML = battle
    ? `Round ${enc.battle_round || 1} <span>declare, then resolve</span>`
    : `The party <span>double-click for the sheet</span>`;
  container.appendChild(head);

  let defaults = {};
  const acting = battle ? declarers() : [];
  if (battle) {
    // A pick the player made by hand persists from round to round while it stays
    // legal (declSelect resets it the round it does not); everyone else follows
    // this round's computed default, which moves with the fight — the member who
    // closed in at 30 ft swings at 5 ft rather than "closing in" again. A member
    // is dropped from both records the moment they stop declaring.
    defaults = declDefaults();
    for (const m of acting) if (!S.declPicked[m.id]) S.decl[m.id] = defaults[m.id];
    for (const id of Object.keys(S.decl)) {
      if (!acting.some((m) => m.id === id)) {
        delete S.decl[id];
        delete S.declPicked[id];
      }
    }
  } else if (Object.keys(S.decl).length || Object.keys(S.declPicked).length) {
    // Declarations belong to one battle; the next opens on fresh defaults.
    S.decl = {};
    S.declPicked = {};
  }

  // Marching order is a town-or-exploring decision (the engine locks it once
  // an encounter opens), and it is what fills the melee rank: the engine takes
  // the front rank off the head of the living order, as many as the party's
  // fighting space holds abreast.
  const orderable = S.view.mode === "town" || S.view.mode === "exploring";

  S.view.party.forEach((m, index) => {
    const dead = m.conditions.includes("dead") || m.current_hp <= 0;
    const row = document.createElement("div");
    row.className = "member" + (dead ? " dead" : "");
    row.dataset.name = m.name;
    const ratio = Math.max(0, m.current_hp) / Math.max(1, m.max_hp);
    const fillClass = ratio <= 0.25 ? "grave" : ratio <= 0.5 ? "hurt" : "";
    const enc = dead ? null : encumbrance(S.sheets && S.sheets[m.id]);
    const canLearn = ((S.sheets[m.id] || {}).spell_picks || []).some((n) => n > 0);
    const tags = dead
      ? '<span class="member-tag red">slain</span>'
      : m.conditions.map((c) => `<span class="member-tag">${esc(c)}</span>`).join("") +
        (enc && enc.overloaded ? '<span class="member-tag red">overloaded</span>' : "") +
        (canLearn ? '<span class="member-tag accent">new spell</span>' : "");

    const who = document.createElement("div");
    who.className = "member-who";
    who.innerHTML = `
      <span class="member-name">${esc(m.name)}</span>
      <span class="member-class">${esc(CLASS_NAMES[m.class_id] || m.class_id)} ${m.level}</span>
      ${tags}`;
    row.appendChild(who);

    if (battle && acting.some((a) => a.id === m.id)) {
      row.appendChild(declSelect(m, defaults[m.id]));
    } else {
      const bar = document.createElement("div");
      bar.className = "hp-bar";
      bar.innerHTML = `<span class="hp-fill ${fillClass}" style="width:${ratio * 100}%"></span>`;
      row.appendChild(bar);
    }

    const hp = document.createElement("span");
    hp.className = "member-hp";
    hp.innerHTML = `<b>${m.current_hp}</b>/${m.max_hp}`;
    row.appendChild(hp);

    if (orderable) row.appendChild(orderControls(index));

    row.addEventListener("dblclick", () => openSheet(m.id));
    container.appendChild(row);
  });

  if (battle) container.appendChild(battleFoot(enc));
}

/* The marching-order nudge: swap this row with its neighbour. reorder_party
   must name exactly the current member ids — the dead included — and an
   accepted reorder is silent (no events), so the re-rendered roster is the
   whole confirmation. */
function orderControls(index) {
  const span = document.createElement("span");
  span.className = "member-order";
  span.addEventListener("dblclick", (event) => event.stopPropagation());
  const last = S.view.party.length - 1;
  for (const [glyph, step, edge] of [["▲", -1, 0], ["▼", 1, last]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn mini ghost";
    btn.textContent = glyph;
    btn.disabled = index === edge;
    btn.title = "Marching order — the melee rank fills from the front, as many as the space allows.";
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const order = S.view.party.map((m) => m.id);
      const to = index + step;
      if (to < 0 || to > last) return;
      [order[index], order[to]] = [order[to], order[index]];
      command({ command_type: "reorder_party", order });
    });
    span.appendChild(btn);
  }
  return span;
}

/* One compact action control per declarer, in their own row.

   The menu offers only declarations the engine will accept: a round is
   all-or-nothing, so one illegal pick costs the whole party's turn. What the
   engine would refuse is still *shown* — disabled, with the reason in the label
   ("Attack — Dagger (back rank)") — because a weapon that silently vanished from
   the menu reads as a bug too. `fallback` is the member's computed default for
   this round: when a pick the player made by hand has gone stale (spell spent,
   weapon gone, the range closed past what the weapon allows) the select resets
   to that default, never blindly to the first option, which can itself be
   illegal. */
function declSelect(m, fallback) {
  const select = document.createElement("select");
  const enc = S.view.encounter;
  const options = attackChoices(m, enc, targetGroup());
  for (const spell of m.memorized_spells) {
    options.push({
      value: JSON.stringify({ action: "cast", spell: spell.spell_id }),
      label: `Cast — ${spellName(spell.spell_id)}`,
      legal: true,
    });
  }
  if (m.class_id === "cleric") {
    options.push({ value: JSON.stringify({ action: "turn_undead" }), label: "Turn undead", legal: true });
  }
  // Movement is a declaration like any other: an entangled member's would reject
  // the round (battle.declaration.cannot_move).
  const canMove = !(enc.immobile || []).includes(m.id);
  const held = canMove ? "" : " (held fast)";
  options.push({
    value: JSON.stringify({ action: "move", move: "close" }),
    label: `Close in${held}`,
    legal: canMove,
  });
  // Both take effect only once every living member declares the same one, since
  // one holdout keeps the formation in place, so the label says so rather than
  // implying a lone declarer moves the party.
  const heldNote = canMove ? "" : ", held fast";
  options.push({
    value: JSON.stringify({ action: "move", move: "fighting_withdrawal" }),
    label: `Fighting withdrawal (half rate, all must agree${heldNote})`,
    legal: canMove,
  });
  options.push({
    value: JSON.stringify({ action: "move", move: "retreat" }),
    label: `Retreat (full rate, ends the fight, all must agree${heldNote})`,
    legal: canMove,
  });
  options.push({ value: JSON.stringify({ action: "hold" }), label: "Hold", legal: true });
  for (const choice of options) {
    const option = document.createElement("option");
    option.value = choice.value;
    option.textContent = choice.label;
    option.disabled = !choice.legal;
    select.appendChild(option);
  }
  const legal = options.filter((choice) => choice.legal);
  const current = JSON.stringify(S.decl[m.id]);
  const preferred = JSON.stringify(fallback);
  const value = [current, preferred].find((v) => legal.some((c) => c.value === v)) || legal[0].value;
  select.value = value;
  if (value !== current) {
    // A hand-made pick that the fight made illegal is the default's again.
    S.decl[m.id] = JSON.parse(value);
    delete S.declPicked[m.id];
  }
  select.addEventListener("change", () => {
    S.decl[m.id] = JSON.parse(select.value);
    S.declPicked[m.id] = true;
  });
  return select;
}

function battleFoot(enc) {
  const foot = document.createElement("div");
  foot.className = "party-foot";
  const groups = enc.groups.filter((g) => g.count > 0);
  targetGroup();
  if (groups.length > 1) {
    const row = document.createElement("div");
    row.className = "panel-row";
    row.style.marginBottom = "8px";
    const label = document.createElement("span");
    label.className = "panel-label";
    label.textContent = "Target";
    const targetSelect = document.createElement("select");
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = group.id;
      option.textContent = `${group.label} ×${group.count}`;
      targetSelect.appendChild(option);
    }
    targetSelect.value = S.declTarget;
    // Groups stand at their own distances, so switching target can make a
    // declaration legal or illegal: re-render the panel rather than leave a
    // stale menu behind.
    targetSelect.addEventListener("change", () => { S.declTarget = targetSelect.value; renderParty(); });
    row.appendChild(label);
    row.appendChild(targetSelect);
    foot.appendChild(row);
  }
  // The engine validates the round all-or-nothing: one bad declaration
  // rejects them all, and the response names each failing member. Casts send
  // target_group_id only; the server resolves it to monster entity ids the
  // player view never carries (_fill_cast_targets in app.py).
  foot.appendChild(button("Resolve round", () => {
    const groupId = S.declTarget;
    const declarations = declarers().map((m) => {
      const d = S.decl[m.id] || { action: "hold" };
      if (d.action === "attack") {
        return { character_id: m.id, action: "attack", target_group_id: groupId, weapon_id: d.weapon };
      }
      if (d.action === "cast") {
        const info = S.spellbook[d.spell] || {};
        const decl = { character_id: m.id, action: "cast", spell_id: d.spell, spell_mode: info.mode };
        if (info.target === "member") decl.targets = [mostWounded().id];
        else decl.target_group_id = groupId;
        return decl;
      }
      if (d.action === "move") {
        return { character_id: m.id, action: "move", move: d.move, target_group_id: groupId };
      }
      if (d.action === "turn_undead") {
        return { character_id: m.id, action: "turn_undead" };
      }
      return { character_id: m.id, action: "hold" };
    });
    command({ command_type: "resolve_battle_round", declarations });
  }, { cls: "accent", disabled: S.pacing }));
  return foot;
}

function spellName(id) {
  return (S.spellbook[id] && S.spellbook[id].name) || id;
}

function spellLevel(id) {
  return (S.spellbook[id] && S.spellbook[id].level) || 1;
}

/* ---------- viewport ---------- */
/* A true one-point-perspective wireframe. Every explored cell inside the view
   frustum (VIEW_DEPTH forward, VIEW_SPAN to each side) projects its real wall
   edges — lateral cells included, so a wide room reads wide instead of
   flattening into corridor alcoves. Unexplored openings and anything past
   draw distance render as flat darkness. Matte lines, no monsters: the party
   panel and the log carry the encounter. */

const VP = { W: 920, H: 518 };
const VIEW_DEPTH = 5;
const VIEW_SPAN = 2;
const EYE = 0.9; // the eye sits this far behind the party's cell center
const NEAR = 0.18; // near-plane clamp for planes that graze the camera

const PAL = {
  bg: "#0b0d11", wall: "#12151b", floor: "#0d1015", ceil: "#0a0c10",
  void: "#07080b", line: "#c7cfdd", dim: "#5c6472",
  accent: "#e0a94f", danger: "#e2584a",
};

function renderViewport() {
  const canvas = $("viewport");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, VP.W, VP.H);
  ctx.fillStyle = PAL.bg;
  ctx.fillRect(0, 0, VP.W, VP.H);

  const note = $("viewport-note");
  note.classList.add("hidden");
  const facingEl = $("viewport-facing");

  if (S.view.location.kind !== "dungeon") {
    drawGateScene(ctx);
    $("nameplate").textContent = S.view.town_name;
    facingEl.textContent = "";
    return;
  }

  const level = currentLevel();
  const loc = S.view.location;
  const lit = S.cell && S.cell.lit;
  $("nameplate").textContent = (S.cell && S.cell.area_name) || "";
  facingEl.textContent = `facing ${loc.facing}`;

  if (level) {
    ctx.save();
    if (!lit) ctx.globalAlpha = 0.15;
    drawScene(ctx, level, loc.position[0], loc.position[1], loc.facing);
    ctx.restore();
    if (!lit) {
      note.textContent = "It is dark. Light a torch.";
      note.classList.remove("hidden");
    }
  }
}

function edgeKind(edge) {
  // No edge record on a cell boundary means solid rock (engine convention).
  if (!edge) return "wall";
  if (edge.kind === "door") return edge.door_open ? "door_open" : "door";
  return edge.kind; // wall | blocked | open
}

function drawScene(ctx, level, px, py, facing) {
  const [fx, fy] = VEC[facing];
  const [rx, ry] = VEC[RIGHT[facing]];
  const world = (dx, dz) => [px + fx * dz + rx * dx, py + fy * dz + ry * dx];
  const cx = VP.W / 2, cy = VP.H / 2;
  // Focal length is anchored to the frame's *width*, never its height: the
  // frame letterboxed from 4:3 to 16:9 and 920 * 0.7875 = 724.5 is byte-identical
  // to the old 690 * 1.05, so the shorter frame is a pure vertical crop of the
  // same scene (horizon centered, walls the same width) rather than a zoom-out.
  // Two knobs, never to be confused again: the multiplier is the zoom knob —
  // change it and every wall narrows or widens; VP.H is the letterbox knob —
  // change it and the scene is cropped or extended top and bottom.
  const f = VP.W * 0.7875;
  const P = (x, y, z) => [cx + (f * x) / z, cy + (f * y) / z];
  const clampZ = (z) => Math.max(z, NEAR);
  const explored = (dx, dz) => {
    const [wx, wy] = world(dx, dz);
    return isExplored(level, wx, wy);
  };

  // Ground planes first; wall fills occlude them where geometry stands.
  ctx.fillStyle = PAL.ceil;
  ctx.fillRect(0, 0, VP.W, cy);
  ctx.fillStyle = PAL.floor;
  ctx.fillRect(0, cy, VP.W, VP.H - cy);

  const quads = [];
  const seams = [];
  const sidesDrawn = new Set();

  for (let dz = 0; dz <= VIEW_DEPTH; dz++) {
    for (let dx = -VIEW_SPAN; dx <= VIEW_SPAN; dx++) {
      if (!explored(dx, dz)) continue;
      const [wx, wy] = world(dx, dz);
      const z0 = dz - 0.5 + EYE, z1 = dz + 0.5 + EYE;
      const zm = (z0 + z1) / 2;

      // The cell's forward boundary.
      const frontEdge = edgeAt(level, wx, wy, facing);
      const frontKind = edgeKind(frontEdge);
      if (frontKind === "wall" || frontKind === "blocked" || frontKind === "door") {
        quads.push({ z: z1, ax: Math.abs(dx), plane: "front", dx, z1,
                     door: frontKind === "door" ? frontEdge : null });
      } else {
        // Open passage (or an open door): darkness past the known world.
        if (frontKind === "door_open") {
          quads.push({ z: z1, ax: Math.abs(dx), plane: "front", dx, z1, frame: true });
        }
        if (!explored(dx, dz + 1) || dz === VIEW_DEPTH) {
          quads.push({ z: z1, ax: Math.abs(dx), plane: "front", dx, z1, void: true });
        } else {
          seams.push({ kind: "front", dx, z: z1 });
        }
      }

      // Lateral boundaries; each shared plane draws once.
      for (const side of [-1, 1]) {
        const dir = side < 0 ? LEFT[facing] : RIGHT[facing];
        const key = `${dx + (side < 0 ? 0 : 1)}|${dz}`;
        if (sidesDrawn.has(key)) continue;
        const sideEdge = edgeAt(level, wx, wy, dir);
        const kind = edgeKind(sideEdge);
        const x = dx + side * 0.5;
        if (kind === "wall" || kind === "blocked" || kind === "door") {
          sidesDrawn.add(key);
          quads.push({ z: zm, ax: Math.abs(x), plane: "side", x, z0, z1,
                       door: kind === "door" ? sideEdge : null });
        } else if (!explored(dx + side, dz)) {
          sidesDrawn.add(key);
          quads.push({ z: zm, ax: Math.abs(x), plane: "side", x, z0, z1, void: true });
        } else if (kind === "door_open") {
          sidesDrawn.add(key);
          quads.push({ z: zm, ax: Math.abs(x), plane: "side", x, z0, z1, frame: true });
        } else if (side > 0) {
          seams.push({ kind: "side", x, z0, z1 });
        }
      }
    }
  }

  // Floor seams: the depth cues between explored cells.
  ctx.strokeStyle = "rgba(199, 207, 221, 0.12)";
  ctx.lineWidth = 1.4;
  for (const seam of seams) {
    ctx.beginPath();
    if (seam.kind === "front") {
      const z = clampZ(seam.z);
      const a = P(seam.dx - 0.5, 0.5, z), b = P(seam.dx + 0.5, 0.5, z);
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
    } else {
      const a = P(seam.x, 0.5, clampZ(seam.z0)), b = P(seam.x, 0.5, clampZ(seam.z1));
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
    }
    ctx.stroke();
  }

  // Painter's order: far to near, outermost first among ties.
  quads.sort((a, b) => (b.z - a.z) || (b.ax - a.ax));
  ctx.lineJoin = "round";
  for (const q of quads) {
    const pts = quadPoints(q).map(([x, y, z]) => P(x, y, clampZ(z)));
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    if (q.void) {
      // The mouth of the unknown: filled darkness, edged faintly so the
      // aperture itself reads even when nothing beyond is mapped.
      ctx.fillStyle = PAL.void;
      ctx.fill();
      ctx.strokeStyle = PAL.dim;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.globalAlpha = 1;
      continue;
    }
    if (q.frame) {
      ctx.strokeStyle = PAL.accent;
      ctx.globalAlpha = 0.8;
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.globalAlpha = 1;
      continue;
    }
    ctx.fillStyle = PAL.wall;
    ctx.fill();
    ctx.strokeStyle = PAL.line;
    ctx.globalAlpha = Math.max(0.3, Math.min(1, 1.35 - q.z * 0.17));
    ctx.lineWidth = Math.max(1.1, 2.6 - q.z * 0.35);
    ctx.stroke();
    ctx.globalAlpha = 1;
    if (q.door) drawDoorInset(ctx, q, P, clampZ);
  }
}

function quadPoints(q) {
  if (q.plane === "front") {
    return [
      [q.dx - 0.5, -0.5, q.z1], [q.dx + 0.5, -0.5, q.z1],
      [q.dx + 0.5, 0.5, q.z1], [q.dx - 0.5, 0.5, q.z1],
    ];
  }
  return [
    [q.x, -0.5, q.z0], [q.x, -0.5, q.z1],
    [q.x, 0.5, q.z1], [q.x, 0.5, q.z0],
  ];
}

function drawDoorInset(ctx, q, P, clampZ) {
  ctx.strokeStyle = PAL.accent;
  ctx.globalAlpha = 0.85;
  ctx.lineWidth = 1.6;
  let pts;
  if (q.plane === "front") {
    const z = clampZ(q.z1);
    pts = [
      [q.dx - 0.26, 0.5, z], [q.dx - 0.26, -0.34, z],
      [q.dx + 0.26, -0.34, z], [q.dx + 0.26, 0.5, z],
    ];
  } else {
    const za = clampZ(q.z0 + (q.z1 - q.z0) * 0.24);
    const zb = clampZ(q.z0 + (q.z1 - q.z0) * 0.78);
    pts = [[q.x, 0.5, za], [q.x, -0.34, za], [q.x, -0.34, zb], [q.x, 0.5, zb]];
  }
  const s = pts.map(([x, y, z]) => P(x, y, z));
  ctx.beginPath();
  ctx.moveTo(s[0][0], s[0][1]);
  for (let i = 1; i < s.length; i++) ctx.lineTo(s[i][0], s[i][1]);
  ctx.stroke();
  const hx = (s[1][0] + s[2][0]) / 2, hy = (s[0][1] + s[1][1]) / 2;
  ctx.beginPath();
  ctx.arc(hx, hy, 2.4, 0, Math.PI * 2);
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawGateScene(ctx) {
  // The title-screen illustration: a walled hold at dusk, seen from the road
  // below, with a wrong-colored glow above it. Generic fantasy architecture —
  // it stands for adventure, not for any one adventure document.
  const horizon = VP.H * 0.66;
  const sky = ctx.createLinearGradient(0, 0, 0, horizon);
  sky.addColorStop(0, "#0b0d11");
  sky.addColorStop(1, "#111219");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, VP.W, horizon);
  ctx.fillStyle = "#08090c";
  ctx.fillRect(0, horizon, VP.W, VP.H - horizon);

  // Stars.
  ctx.save();
  ctx.fillStyle = "rgba(199, 207, 221, 0.7)";
  let sx = 87;
  for (let i = 0; i < 60; i++) {
    sx = (sx * 193 + 41) % 8401;
    const x = sx % VP.W, y = (sx * 7) % Math.floor(horizon * 0.82);
    const r = i % 9 === 0 ? 1.3 : 0.6;
    ctx.globalAlpha = 0.25 + (i % 5) * 0.12;
    ctx.fillRect(x, y, r, r);
  }
  ctx.restore();

  // The skyline silhouette: flanking towers and a central dome.
  const cx = VP.W / 2;
  ctx.strokeStyle = PAL.line;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(cx - 300, horizon);
  ctx.lineTo(cx - 300, horizon - 100);
  ctx.lineTo(cx - 270, horizon - 130);
  ctx.lineTo(cx - 240, horizon - 100);
  ctx.lineTo(cx - 240, horizon - 60);
  ctx.lineTo(cx - 130, horizon - 60);
  ctx.lineTo(cx - 130, horizon - 150);
  ctx.moveTo(cx - 130, horizon - 150);
  ctx.quadraticCurveTo(cx, horizon - 260, cx + 130, horizon - 150);
  ctx.lineTo(cx + 130, horizon - 60);
  ctx.lineTo(cx + 240, horizon - 60);
  ctx.lineTo(cx + 240, horizon - 100);
  ctx.lineTo(cx + 270, horizon - 130);
  ctx.lineTo(cx + 300, horizon - 100);
  ctx.lineTo(cx + 300, horizon);
  ctx.stroke();

  // The red glow over the dome — an omen in the picture, not a UI color.
  ctx.save();
  const glow = ctx.createRadialGradient(cx, horizon - 200, 10, cx, horizon - 200, 190);
  glow.addColorStop(0, "rgba(226, 88, 74, 0.2)");
  glow.addColorStop(1, "rgba(226, 88, 74, 0)");
  ctx.fillStyle = glow;
  ctx.fillRect(cx - 200, horizon - 390, 400, 400);
  ctx.restore();

  // Foreground road.
  ctx.strokeStyle = "rgba(107, 112, 120, 0.5)";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.moveTo(cx - 260, VP.H);
  ctx.lineTo(cx - 30, horizon + 8);
  ctx.moveTo(cx + 260, VP.H);
  ctx.lineTo(cx + 30, horizon + 8);
  ctx.stroke();
}

/* ---------- automap overlay + player notes ---------- */
/* The map is summoned (M), not enthroned. It draws only what the projection
   carries — explored cells, walls, doors, the party arrow — plus the player's
   own notes, which live client-side in localStorage and never touch the
   server. Click an explored cell, type in the footer, enter saves. */

const MAP = { open: false, sel: null, t: null };

function mapAvailable() {
  return !!(S.view && S.view.location.kind === "dungeon" && currentLevel());
}

function toggleMap(force) {
  const open = force !== undefined ? force : !MAP.open;
  if (open && !mapAvailable()) return;
  MAP.open = open;
  MAP.sel = null;
  if (open) toggleJournal(false); // one scrim at a time
  $("map-overlay").classList.toggle("hidden", !open);
  if (open) renderAutomap();
  updateNoteEditor();
}

function noteKey() {
  const loc = S.view.location;
  return `${S.view.adventure_name}|${loc.dungeon_id}|${loc.level_number}`;
}

function loadNotes() {
  try {
    return JSON.parse(localStorage.getItem("osrweb_notes") || "{}");
  } catch {
    return {};
  }
}

function notesForLevel() {
  return loadNotes()[noteKey()] || {};
}

function saveNote(cell, text) {
  const all = loadNotes();
  const key = noteKey();
  const notes = all[key] || {};
  const cellKey = `${cell[0]},${cell[1]}`;
  if (text) notes[cellKey] = text;
  else delete notes[cellKey];
  if (Object.keys(notes).length) all[key] = notes;
  else delete all[key];
  localStorage.setItem("osrweb_notes", JSON.stringify(all));
}

function updateNoteEditor() {
  const input = $("map-note");
  const hint = $("map-note-hint");
  if (!MAP.open) return;
  if (MAP.sel) {
    const notes = notesForLevel();
    input.disabled = false;
    input.value = notes[`${MAP.sel[0]},${MAP.sel[1]}`] || "";
    hint.textContent = `cell ${MAP.sel[0]},${MAP.sel[1]}`;
    input.focus();
  } else {
    input.disabled = false;
    input.value = "";
    hint.textContent = "";
  }
}

function renderAutomap() {
  if (!MAP.open) return;
  const level = currentLevel();
  if (!level) return;
  const canvas = $("automap");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = PAL.bg;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  $("map-title").textContent = `${S.view.adventure_name} — level ${level.level_number}`;

  const maxX = Math.max(...level.cells.map(([x]) => x)) + 1;
  const maxY = Math.max(...level.cells.map(([, y]) => y)) + 1;
  const minX = Math.min(...level.cells.map(([x]) => x));
  const minY = Math.min(...level.cells.map(([, y]) => y));
  const cols = Math.max(maxX - minX, 8), rows = Math.max(maxY - minY, 8);
  const size = Math.min(canvas.width / (cols + 2), canvas.height / (rows + 2), 72);
  const ox = (canvas.width - cols * size) / 2 - minX * size;
  const oy = (canvas.height - rows * size) / 2 - minY * size;
  MAP.t = { ox, oy, size };
  const X = (x) => ox + x * size;
  const Y = (y) => oy + y * size;

  for (const [x, y] of level.cells) {
    ctx.fillStyle = "rgba(199, 207, 221, 0.06)";
    ctx.fillRect(X(x) + 0.5, Y(y) + 0.5, size - 1, size - 1);
  }

  if (MAP.sel) {
    ctx.fillStyle = "rgba(224, 169, 79, 0.14)";
    ctx.fillRect(X(MAP.sel[0]) + 0.5, Y(MAP.sel[1]) + 0.5, size - 1, size - 1);
  }

  ctx.lineWidth = 2.4;
  for (const [x, y] of level.cells) {
    for (const dir of DIRS) {
      const edge = edgeAt(level, x, y, dir);
      const kind = edge ? edge.kind : null;
      if (!kind || kind === "open") continue;
      let x0, y0, x1, y1;
      if (dir === "north") { x0 = X(x); y0 = Y(y); x1 = X(x + 1); y1 = Y(y); }
      else if (dir === "south") { x0 = X(x); y0 = Y(y + 1); x1 = X(x + 1); y1 = Y(y + 1); }
      else if (dir === "west") { x0 = X(x); y0 = Y(y); x1 = X(x); y1 = Y(y + 1); }
      else { x0 = X(x + 1); y0 = Y(y); x1 = X(x + 1); y1 = Y(y + 1); }
      if (kind === "door") {
        ctx.strokeStyle = edge.door_open ? PAL.accent : "rgba(224, 169, 79, 0.75)";
        ctx.beginPath();
        const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
        if (dir === "north" || dir === "south") {
          ctx.moveTo(x0, y0); ctx.lineTo(mx - size * 0.18, y0);
          ctx.moveTo(mx + size * 0.18, y0); ctx.lineTo(x1, y1);
          ctx.strokeRect(mx - size * 0.18, my - size * 0.1, size * 0.36, size * 0.2);
        } else {
          ctx.moveTo(x0, y0); ctx.lineTo(x0, my - size * 0.18);
          ctx.moveTo(x0, my + size * 0.18); ctx.lineTo(x1, y1);
          ctx.strokeRect(mx - size * 0.1, my - size * 0.18, size * 0.2, size * 0.36);
        }
        ctx.stroke();
      } else {
        ctx.strokeStyle = "rgba(199, 207, 221, 0.65)";
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        ctx.stroke();
      }
    }
  }

  // The player's own marginalia.
  const notes = notesForLevel();
  for (const cellKey of Object.keys(notes)) {
    const [nx, ny] = cellKey.split(",").map(Number);
    if (!isExplored(level, nx, ny)) continue;
    const px = X(nx + 1) - size * 0.22, py = Y(ny) + size * 0.22;
    ctx.fillStyle = PAL.accent;
    ctx.beginPath();
    ctx.arc(px, py, Math.max(4, size * 0.1), 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = PAL.bg;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // Party arrow.
  const loc = S.view.location;
  const [px, py] = loc.position;
  const acx = X(px) + size / 2, acy = Y(py) + size / 2;
  const angle = { north: -Math.PI / 2, east: 0, south: Math.PI / 2, west: Math.PI }[loc.facing];
  ctx.save();
  ctx.translate(acx, acy);
  ctx.rotate(angle);
  ctx.fillStyle = PAL.accent;
  const s = size * 0.3;
  ctx.beginPath();
  ctx.moveTo(s, 0);
  ctx.lineTo(-s * 0.7, -s * 0.7);
  ctx.lineTo(-s * 0.3, 0);
  ctx.lineTo(-s * 0.7, s * 0.7);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

$("automap").addEventListener("click", (event) => {
  const level = currentLevel();
  if (!level || !MAP.t) return;
  const canvas = $("automap");
  const rect = canvas.getBoundingClientRect();
  const scale = canvas.width / rect.width;
  const mx = (event.clientX - rect.left) * scale;
  const my = (event.clientY - rect.top) * scale;
  const x = Math.floor((mx - MAP.t.ox) / MAP.t.size);
  const y = Math.floor((my - MAP.t.oy) / MAP.t.size);
  MAP.sel = isExplored(level, x, y) ? [x, y] : null;
  renderAutomap();
  updateNoteEditor();
});

$("map-note").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.target.blur();
    event.stopPropagation();
    return;
  }
  if (event.key !== "Enter" || !MAP.sel) return;
  saveNote(MAP.sel, event.target.value.trim());
  renderAutomap();
});

$("map-overlay").addEventListener("click", (event) => {
  if (event.target === $("map-overlay")) toggleMap(false);
});

/* ---------- journal overlay ---------- */
/* The party's own record, summoned (J) like the map but available in every
   mode — town included, where the map is not. It renders `view.journal`, which
   the engine appends to and never rewrites, so identical lines from a
   repeatable trigger stand as the two entries they are. */

const JOURNAL = { open: false };

function toggleJournal(force) {
  const open = force !== undefined ? force : !JOURNAL.open;
  if (open && !S.view) return; // toggleMap's shape: only opening is gated
  JOURNAL.open = open;
  if (open) toggleMap(false); // one scrim at a time
  $("journal-overlay").classList.toggle("hidden", !open);
  if (!open) return;
  // Unhide, render, then pin: on a `display: none` element every scroll metric
  // reads 0, so a pin computed before the class comes off is meaningless.
  renderJournal();
  scrollLogToBottom($("journal-body"));
}

/* The record's rows, built once and read twice: the summoned overlay fills its
   body with them, and the victory card carries the same tale on the ending
   screen (where `J` is unreachable under `#overlay`). The empty-state line is
   deliberately not here — it belongs to the overlay alone. */
function buildJournalEntries() {
  return (S.view.journal || []).map((entry) => {
    const row = document.createElement("div");
    row.className = "j-entry";
    const turn = document.createElement("span");
    turn.className = "j-turn";
    // The HUD clock's own helper, so the two can never disagree.
    turn.textContent = `turn ${turnOf(entry.rounds)}`;
    const text = document.createElement("span");
    text.className = "j-text";
    text.textContent = entry.text;
    row.appendChild(turn);
    row.appendChild(text);
    return row;
  });
}

function renderJournal() {
  const body = $("journal-body");
  const pinned = logPinnedToBottom(body);
  body.innerHTML = "";
  const rows = buildJournalEntries();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "j-empty";
    empty.textContent = "Nothing recorded yet.";
    body.appendChild(empty);
    return;
  }
  for (const row of rows) body.appendChild(row);
  // Leave a player reading the top of the record where they are.
  if (pinned) scrollLogToBottom(body);
}

$("journal-overlay").addEventListener("click", (event) => {
  if (event.target === $("journal-overlay")) toggleJournal(false);
});

/* ---------- actions bar ---------- */

/* Name the auto-picked actor on the button ("Search — Aravel"). Names are
   player-authored and button() sets innerHTML, so esc() is mandatory. */
function withActor(label, member) {
  return member ? `${label}<span class="actor"> — ${esc(member.name)}</span>` : label;
}

function button(label, onClick, opts) {
  opts = opts || {};
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn" + (opts.cls ? ` ${opts.cls}` : "");
  btn.innerHTML = label + (opts.key ? ` <span class="key-hint">${opts.key}</span>` : "");
  btn.disabled = !!opts.disabled;
  if (opts.title) btn.title = opts.title;
  btn.addEventListener("click", onClick);
  return btn;
}

function renderActions() {
  const bar = $("actions");
  bar.innerHTML = "";
  const mode = S.view.mode;

  if (mode === "town") {
    const dungeons = S.dungeons || [];
    for (const dungeon of dungeons) {
      bar.appendChild(button(`Enter ${esc(dungeon.name)}`, () => {
        command({ command_type: "enter_dungeon", dungeon_id: dungeon.id });
      }, { cls: dungeons.length === 1 ? "accent big" : "accent" }));
    }
    return;
  }

  if (mode === "encounter" || mode === "battle") {
    bar.appendChild(button("Map", () => toggleMap(), { key: "M", cls: "ghost" }));
    return;
  }
  if (mode !== "exploring") return;

  /* Three fixed full-width zones, so contextual verbs coming and going never
     move the persistent ones: movement, then the always-present verbs
     (Search, Find traps, Rest, Map — same order every render), then a
     contextual row that reserves one button row's height even when empty. */
  const zone = (cls) => {
    const div = document.createElement("div");
    div.className = `actions-zone ${cls}`;
    bar.appendChild(div);
    return div;
  };

  const move = zone("actions-move");
  move.appendChild(button("◀", () => turn(LEFT), { cls: "move", title: "Turn left (A / ←)" }));
  move.appendChild(button("▲", () => forward(), { cls: "move", title: "Forward (W / ↑)" }));
  move.appendChild(button("▶", () => turn(RIGHT), { cls: "move", title: "Turn right (D / →)" }));
  move.appendChild(button("⟲", () => turn(OPPOSITE), { cls: "move", title: "Turn around (S / ↓)" }));

  const verbs = zone("actions-verbs");
  const searcher = memberByClass("elf", "thief");
  verbs.appendChild(button(withActor("Search", searcher), () => {
    if (searcher) command({ command_type: "search", character_id: searcher.id, kind: "secret_doors" });
  }, {
    disabled: !searcher,
    title: searcher ? "Search this space for secret doors (one turn)" : "No one left standing to search.",
  }));
  const trapFinder = memberByClass("thief", "dwarf");
  verbs.appendChild(button(withActor("Find traps", trapFinder), () => {
    if (trapFinder) command({ command_type: "search", character_id: trapFinder.id, kind: "room_traps" });
  }, {
    disabled: !trapFinder,
    title: trapFinder ? "Search this space for traps (one turn)" : "No one left standing to search.",
  }));
  const spacer = document.createElement("div");
  spacer.className = "spacer";
  verbs.appendChild(spacer);
  verbs.appendChild(button("Rest", () => command({ command_type: "rest", kind: "turn" }), { title: "Rest one turn", key: "R" }));
  verbs.appendChild(button("Map", () => toggleMap(), { key: "M", cls: "ghost" }));

  const ctx = zone("actions-ctx");

  const facingEdge = facingDoor();
  if (facingEdge && facingEdge.kind === "door") {
    // The engine's wedge holds a door against *swinging shut*: close_door
    // rejects while the spike stands, but the party may still pull open a
    // door they spiked closed — so only Close ever disables for it.
    const wedged = !!facingEdge.door_wedged;
    if (facingEdge.door_open) {
      ctx.appendChild(button("Close door", () => command({ command_type: "close_door", direction: S.view.location.facing }), {
        key: "C", disabled: wedged, title: wedged ? "Wedged — the spike holds it." : "",
      }));
    } else {
      ctx.appendChild(button("Open door", () => command({ command_type: "open_door", direction: S.view.location.facing }), { key: "O" }));
      const forcer = memberByClass("fighter", "dwarf");
      ctx.appendChild(button(withActor("Force", forcer), () => {
        if (forcer) command({ command_type: "force_door", direction: S.view.location.facing, character_id: forcer.id });
      }, {
        key: "F", disabled: !forcer,
        title: forcer ? "" : "No one left standing to force it.",
      }));
      const listener = memberByClass("thief");
      ctx.appendChild(button(withActor("Listen", listener), () => {
        if (listener) command({ command_type: "listen_at_door", direction: S.view.location.facing, character_id: listener.id });
      }, {
        key: "G", disabled: !listener,
        title: listener ? "" : "No one left standing to listen.",
      }));
      const picker = livingMembers().find((m) => m.class_id === "thief");
      ctx.appendChild(button(withActor("Pick lock", picker), () => {
        if (picker) command({ command_type: "pick_lock", direction: S.view.location.facing, character_id: picker.id });
      }, {
        disabled: !picker,
        title: picker
          ? `A failed pick locks ${picker.name} out of this lock until they gain a level.`
          : "A thief's work — no living thief.",
      }));
    }
    if (!wedged) {
      // Wedging works on a door in either position: spike an open door so it
      // cannot swing shut behind the party, or a closed one so nothing beyond
      // can open it. Any living member's spike serves; one is consumed.
      const spikes = spikeCount();
      ctx.appendChild(button(`Wedge (${spikes})`, () => command({ command_type: "wedge_door", direction: S.view.location.facing }), {
        disabled: !spikes,
        title: spikes
          ? "Drive an iron spike so the door cannot swing (one spike is spent)."
          : "No iron spikes — the provisioner sells them.",
      }));
    }
  }

  if (S.cell && (S.cell.features.length || S.cell.pile)) {
    const feature = S.cell.features.length ? S.cell.features[0] : null;
    const target = feature ? feature.id : "pile";
    const trapSet = !!(feature && feature.trap_found);
    ctx.appendChild(button("Take treasure", () => command({ command_type: "take_treasure", feature_id: target }), {
      cls: "accent", key: "T",
      title: trapSet ? "A trap is set — taking it risks springing it." : "",
    }));
    if (feature) {
      // The thief's check-and-disarm play. Inspect is one attempt per thief
      // per cache (the engine remembers); a found trap offers Disarm, whose
      // failure springs it on the thief.
      const thief = livingMembers().find((m) => m.class_id === "thief");
      const inspected = !!(thief && (feature.inspected_by || []).includes(thief.id));
      ctx.appendChild(button("Inspect", () => {
        if (thief && !inspected) command({ command_type: "inspect_treasure", character_id: thief.id, feature_id: feature.id });
      }, {
        key: "I", disabled: !thief || inspected,
        title: !thief ? "A thief's work."
          : inspected ? `${thief.name} has already gone over it.`
          : "Check the treasure for a trap (one turn).",
      }));
      if (trapSet) {
        ctx.appendChild(button("Disarm", () => {
          if (thief) command({ command_type: "remove_treasure_trap", character_id: thief.id, feature_id: feature.id });
        }, {
          disabled: !thief,
          title: !thief ? "A thief's work." : "Failure springs the trap on the thief.",
        }));
      }
    }
  }

  if (S.cell && S.cell.transition) {
    // The cell payload carries the transition kind itself (app.py sends
    // transition.kind): stairs_up | stairs_down | trapdoor | chute.
    const kind = S.cell.transition;
    const label = kind === "stairs_down" ? "Descend the stairs"
      : kind === "stairs_up" ? "Climb the stairs"
      : "Use stairs";
    ctx.appendChild(button(label, () => command({ command_type: "use_stairs" })));
  }

  if (S.view.location.kind === "dungeon" && !lightIsBurning()) {
    const bearer = torchBearer();
    ctx.appendChild(button(`Light torch (${torchCount()})`, () => {
      if (bearer) command({ command_type: "light_source", character_id: bearer.id, item_id: "torch" });
      else toast("No torches left.", false);
    }, { cls: "accent", key: "L", disabled: !bearer }));
  } else if (S.view.location.kind === "dungeon") {
    // The light is burning; the bearer is whoever the effect names. Dousing
    // forfeits the remainder — a spent torch does not bank.
    const burning = S.view.effects.find((e) => e.kind === "light");
    if (burning) {
      ctx.appendChild(button("Snuff light", () => {
        command({ command_type: "extinguish_source", character_id: burning.character_id });
      }, { cls: "ghost", title: "Put out the light — what's left of it is lost." }));
    }
  }
  if (S.cell && S.cell.at_entrance) {
    ctx.appendChild(button("Leave for town", () => command({ command_type: "travel_to_town" })));
  }
}

function lightIsBurning() {
  return lightState() !== null;
}

function facingDoor() {
  const level = currentLevel();
  if (!level) return null;
  const [x, y] = S.view.location.position;
  return edgeAt(level, x, y, S.view.location.facing);
}

function forward() {
  if (S.view.mode !== "exploring") return;
  command({ command_type: "move_party", direction: S.view.location.facing });
}

function turn(mapping) {
  if (S.view.mode !== "exploring") return;
  command({ command_type: "turn_party", facing: mapping[S.view.location.facing] });
}

/* ---------- context panel ---------- */

function renderContext() {
  const panel = $("context");
  panel.innerHTML = "";
  const mode = S.view.mode;
  if (mode === "encounter") renderEncounterPanel(panel);
  else if (mode === "battle") renderBattleContext(panel);
  else if (mode === "exploring") renderExplorePanel(panel);
}

function renderExplorePanel(panel) {
  if (!S.cell) return;
  if (S.cell.features.length || S.cell.pile) {
    const card = document.createElement("div");
    card.className = "panel";
    card.innerHTML = "<h3>This area</h3>";
    for (const feature of S.cell.features) {
      const p = document.createElement("div");
      p.className = "prose-sm";
      p.textContent = feature.description;
      card.appendChild(p);
      if (feature.trap_found) {
        const note = document.createElement("div");
        note.className = "prose-sm trap-note";
        note.textContent = "A trap is set here.";
        card.appendChild(note);
      }
    }
    if (S.cell.pile) {
      const p = document.createElement("div");
      p.className = "prose-sm";
      p.textContent = `Goods are scattered on the floor (${S.cell.pile.coins_gp_value} gp in coin among them).`;
      card.appendChild(p);
    }
    panel.appendChild(card);
  }
  // What is underfoot first, then the standing charge, then the verb. The
  // charge sits ahead of the cast card because at the reference viewport
  // (1440×900) last in the rail is below the fold — measured on the barrow
  // fixture (tests/assets/barrow-adventure.json) at quest activation, the
  // card ran 867.5–1010.9 against a rail 848 tall, wholly invisible behind a
  // scrollbar this UI deliberately keeps near-invisible. A standing
  // reference nobody can see is not a reference.
  const quests = buildQuestsCard();
  if (quests) panel.appendChild(quests);
  renderCastCard(panel);
}

/* ---------- the quest log ---------- */

/* The standing charge, rendered from `view.quests` — the engine's own
   player-safe projection: active quests in document order, revealed objectives
   only. One card for all of them, because a forge document may carry several
   and N stacked panels would be N borders in a rail that already scrolls. The
   card is simply absent when nothing is active; nothing reserves its height.
   Same builder in the rail and on the town board, so the two cannot drift. */
function buildQuestsCard() {
  const quests = (S.view && S.view.quests) || [];
  if (!quests.length) return null;

  const card = document.createElement("div");
  card.className = "panel";
  card.innerHTML = "<h3>Quests</h3>";

  for (const quest of quests) {
    const name = document.createElement("div");
    name.className = "quest-name";
    name.textContent = quest.name;
    card.appendChild(name);

    if (quest.narrative) {
      const offer = document.createElement("div");
      offer.className = "prose-sm";
      offer.textContent = quest.narrative;
      card.appendChild(offer);
    }
    if (quest.speaker) {
      const speaker = document.createElement("div");
      speaker.className = "quest-speaker";
      speaker.textContent = `— ${quest.speaker}`;
      card.appendChild(speaker);
    }

    // A quest may legally hide every objective it has; it is still real and its
    // offer is still the charge, so an empty list renders as no list at all.
    if (!quest.objectives.length) continue;
    const list = document.createElement("ul");
    list.className = "quest-objectives";
    for (const objective of quest.objectives) {
      const done = objective.state === "complete";
      const item = document.createElement("li");
      if (done) item.className = "done";
      const mark = document.createElement("span");
      mark.className = "quest-obj-mark";
      mark.textContent = done ? "✓" : "○";
      item.appendChild(mark);
      // Verbatim, slug and all: the engine fills a name the author did not
      // write with the objective's own id, and that is what the thing is called.
      item.appendChild(document.createTextNode(objective.name));
      list.appendChild(item);
    }
    card.appendChild(list);
  }
  return card;
}

/* The exploring cast card: the cleric's heal between fights, the shield
   before a door. Only member-targeted and untargeted casts make sense here —
   group spells have nothing to aim at outside battle, so they list disabled.
   The picks persist across re-renders in S.explore. */
function renderCastCard(panel) {
  if (S.view.mode !== "exploring") return;
  const casters = livingMembers().filter((m) => m.memorized_spells.length);
  if (!casters.length) return;

  const card = document.createElement("div");
  card.className = "panel cast-card";
  card.innerHTML = "<h3>Cast a spell</h3>";

  const casterSelect = document.createElement("select");
  for (const m of casters) {
    const option = document.createElement("option");
    option.value = m.id;
    option.textContent = `${m.name} — ${m.memorized_spells.length} prepared`;
    casterSelect.appendChild(option);
  }
  persistSelect(casterSelect, S.explore, "caster");
  casterSelect.addEventListener("change", () => {
    S.explore.spell = null; // a new caster means a new spell list
    render();
  });
  const casterRow = document.createElement("div");
  casterRow.className = "panel-row";
  casterRow.appendChild(casterSelect);
  card.appendChild(casterRow);

  const caster = casters.find((m) => m.id === S.explore.caster) || casters[0];
  // The memorized copies, deduped by spell with ×n counts.
  const counts = {};
  const order = [];
  for (const s of caster.memorized_spells) {
    if (!(s.spell_id in counts)) order.push(s.spell_id);
    counts[s.spell_id] = (counts[s.spell_id] || 0) + 1;
  }
  const spellSelect = document.createElement("select");
  for (const id of order) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = spellName(id) + (counts[id] > 1 ? ` ×${counts[id]}` : "");
    const info = S.spellbook[id] || {};
    if (info.target === "group") {
      option.disabled = true;
      option.title = "No target outside battle.";
    }
    spellSelect.appendChild(option);
  }
  persistSelect(spellSelect, S.explore, "spell");
  spellSelect.addEventListener("change", () => render());
  const spellRow = document.createElement("div");
  spellRow.className = "panel-row";
  spellRow.appendChild(spellSelect);
  card.appendChild(spellRow);

  const spellId = S.explore.spell;
  const info = S.spellbook[spellId] || {};

  if (info.target === "member") {
    if (!livingMembers().some((m) => m.id === S.explore.target)) {
      S.explore.target = mostWounded().id;
    }
    const targetSelect = document.createElement("select");
    for (const m of livingMembers()) {
      const option = document.createElement("option");
      option.value = m.id;
      option.textContent = `${m.name} (${m.current_hp}/${m.max_hp})`;
      targetSelect.appendChild(option);
    }
    persistSelect(targetSelect, S.explore, "target");
    const targetRow = document.createElement("div");
    targetRow.className = "panel-row";
    targetRow.appendChild(targetSelect);
    card.appendChild(targetRow);
  }

  const castRow = document.createElement("div");
  castRow.className = "panel-row";
  castRow.appendChild(button("Cast", () => {
    command({
      command_type: "cast_spell",
      character_id: caster.id,
      spell_id: spellId,
      mode: info.mode,
      targets: info.target === "member" ? [S.explore.target] : [],
    });
  }, { cls: "accent" }));
  card.appendChild(castRow);
  panel.appendChild(card);
}

/* ---------- the town board: the stage's town presence ---------- */

function renderTownBoard() {
  const board = $("town");
  board.innerHTML = "";
  if (S.view.mode !== "town") return;

  const name = document.createElement("h2");
  name.className = "town-name";
  name.textContent = S.view.town_name;
  board.appendChild(name);

  if (S.view.town_description) {
    const desc = document.createElement("p");
    desc.className = "town-desc";
    desc.textContent = S.view.town_description;
    board.appendChild(desc);
  }

  for (const service of S.view.town_services || []) {
    const p = document.createElement("p");
    p.className = "town-service";
    p.textContent = service;
    board.appendChild(p);
  }

  if ((S.hooks || []).length) {
    const heading = document.createElement("h3");
    heading.className = "town-h";
    heading.textContent = "Rumors";
    board.appendChild(heading);
    for (const hook of S.hooks) {
      const p = document.createElement("p");
      p.className = "town-rumor";
      p.textContent = hook;
      board.appendChild(p);
    }
  }

  const cards = document.createElement("div");
  cards.className = "town-cards";
  // The charge before the shopping — and inside the card grid, which is what
  // keeps it from reading as an index of the rumors above it.
  const quests = buildQuestsCard();
  if (quests) cards.appendChild(quests);
  cards.appendChild(buildTemplePanel());
  cards.appendChild(buildProvisionerPanel());
  cards.appendChild(buildCampPanel());
  const study = buildStudyPanel();
  if (study) cards.appendChild(study);
  board.appendChild(cards);
}

/* A persisted select survives re-renders the way battle's target select does
   (S.declTarget): the stored value at obj[key] is validated against the
   options just built, falls back to the first, is applied AFTER the options
   are appended, and is written back on change. */
function persistSelect(select, obj, key) {
  const values = [...select.options].map((o) => o.value);
  if (!values.includes(obj[key])) obj[key] = values[0] !== undefined ? values[0] : null;
  if (obj[key] !== null) select.value = obj[key];
  select.addEventListener("change", () => { obj[key] = select.value; });
}

function persistTownSelect(select, key) {
  persistSelect(select, S.town, key);
}

const REST_HINT = "Sleep until morning — the party wakes ready to prepare spells; healing takes a full day of rest.";
const PREP_HINT = "Rest the night first — spells are prepared after a night's sleep.";

function buildTemplePanel() {
  const anyone = livingMembers().length > 0;
  const temple = document.createElement("div");
  temple.className = "panel";
  temple.innerHTML = "<h3>Temple</h3>";
  const whoRow = document.createElement("div");
  whoRow.className = "panel-row";
  const whoSelect = document.createElement("select");
  // Every member lists, the dead included — raise dead is a temple service.
  for (const m of S.view.party) {
    const option = document.createElement("option");
    option.value = m.id;
    const marks = m.conditions.filter((c) => c === "poisoned" || c === "diseased" || c === "dead");
    const flags = marks.length ? ` — ${marks.join(", ")}` : "";
    option.textContent = `${m.name} (${m.current_hp}/${m.max_hp}${flags})`;
    whoSelect.appendChild(option);
  }
  persistTownSelect(whoSelect, "templeWho");
  const serviceSelect = document.createElement("select");
  for (const service of S.templeServices) {
    const option = document.createElement("option");
    option.value = service.id;
    option.textContent = `${service.name} — ${service.cost_gp.toLocaleString()} gp`;
    serviceSelect.appendChild(option);
  }
  persistTownSelect(serviceSelect, "templeService");
  whoRow.appendChild(whoSelect);
  temple.appendChild(whoRow);
  const serviceRow = document.createElement("div");
  serviceRow.className = "panel-row";
  serviceRow.appendChild(serviceSelect);
  serviceRow.appendChild(button("Heal", () => {
    command({ command_type: "purchase_healing", character_id: whoSelect.value, service: serviceSelect.value });
  }, { cls: "accent", disabled: !anyone }));
  temple.appendChild(serviceRow);
  return temple;
}

/* The catalog fetch, shared by the wizard's shop and the provisioner. One
   in-flight promise so concurrent renders never race a second request. */
let catalogPromise = null;
function ensureCatalog() {
  if (S.catalog) return Promise.resolve(S.catalog);
  if (!catalogPromise) {
    catalogPromise = apiFetch("/creation/catalog")
      .then((catalog) => { S.catalog = catalog; return catalog; })
      .finally(() => { catalogPromise = null; });
  }
  return catalogPromise;
}

const SHOP_GROUPS = [["weapon", "Weapons"], ["armour", "Armour"], ["gear", "Gear"], ["ammunition", "Ammunition"]];

function buildProvisionerPanel() {
  const anyone = livingMembers().length > 0;
  const shop = document.createElement("div");
  shop.className = "panel";
  shop.innerHTML = "<h3>Provisioner</h3>";
  const buyerRow = document.createElement("div");
  buyerRow.className = "panel-row";
  const buyerSelect = document.createElement("select");
  for (const m of S.view.party) {
    const option = document.createElement("option");
    option.value = m.id;
    option.textContent = `${m.name} (${m.inventory.purse.gp} gp)`;
    buyerSelect.appendChild(option);
  }
  persistTownSelect(buyerSelect, "buyer");
  buyerRow.appendChild(buyerSelect);
  shop.appendChild(buyerRow);
  if (!S.catalog) {
    const fetching = document.createElement("div");
    fetching.className = "panel-hint";
    fetching.textContent = "Fetching the price list…";
    shop.appendChild(fetching);
    ensureCatalog()
      .then(() => S.view.mode === "town" && render())
      .catch(() => {});
    return shop;
  }
  // The whole catalog, ungated by the buyer's class: the engine validates
  // equipping, and anyone may buy a thing to hand off.
  const list = document.createElement("div");
  list.className = "shop-list";
  for (const [kind, label] of SHOP_GROUPS) {
    const items = S.catalog.equipment.filter((item) => item.kind === kind);
    if (!items.length) continue;
    const group = document.createElement("details");
    group.className = "shop-group";
    group.open = !!S.town.shopOpen[kind];
    const summary = document.createElement("summary");
    summary.textContent = label;
    group.appendChild(summary);
    group.addEventListener("toggle", () => { S.town.shopOpen[kind] = group.open; });
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "shop-row";
      const name = document.createElement("span");
      name.className = "cr-name";
      name.textContent = `${item.name} — ${item.cost_gp} gp`;
      row.appendChild(name);
      row.appendChild(button("Buy", () => {
        command({ command_type: "purchase_equipment", character_id: buyerSelect.value, item_ids: [item.id] });
      }, { cls: "mini", disabled: !anyone }));
      group.appendChild(row);
    }
    list.appendChild(group);
  }
  shop.appendChild(list);
  return shop;
}

/* Lay the current memorized spells into the slot grid in order, remainder
   empty — so Prepare's full-replacement semantics start from what stands. */
function prepDefaults(caster, slots) {
  const picks = {};
  const byLevel = {};
  for (const s of caster.memorized_spells) {
    const level = spellLevel(s.spell_id);
    (byLevel[level] = byLevel[level] || []).push(s.spell_id);
  }
  slots.forEach((count, i) => {
    const level = i + 1;
    const queue = byLevel[level] || [];
    for (let slot = 0; slot < count; slot++) {
      picks[`${level}:${slot}`] = queue[slot] || "";
    }
  });
  return picks;
}

function buildCampPanel() {
  const camp = document.createElement("div");
  camp.className = "panel";
  camp.innerHTML = "<h3>Rest and trade</h3>";
  const campRow = document.createElement("div");
  campRow.className = "panel-row";
  const valuables = S.view.party.flatMap((m) => m.inventory.valuables);
  const total = valuables.reduce((sum, v) => sum + (v.value_gp || 0), 0);
  campRow.appendChild(button(`Sell treasure — ${total.toLocaleString()} gp`, async () => {
    if (!valuables.length) return;
    const n = valuables.length;
    if (!await confirmDialog(`Sell ${n} piece${n === 1 ? "" : "s"} of treasure for ${total} gp?`, "Sell")) return;
    command({ command_type: "sell_treasure", item_ids: valuables.map((v) => v.instance_id) });
  }, { cls: "accent", disabled: !valuables.length }));
  campRow.appendChild(button("Rest the night", () => command({ command_type: "rest", kind: "night" }), { title: REST_HINT }));
  camp.appendChild(campRow);
  const hint = document.createElement("div");
  hint.className = "panel-hint";
  hint.textContent = REST_HINT;
  camp.appendChild(hint);

  const casters = S.view.party.filter((m) => (S.spellBooks[m.id] || []).length && m.current_hp > 0);
  for (const caster of casters) {
    const sheet = S.sheets[caster.id] || {};
    const slots = sheet.spell_slots || [];
    const totalSlots = slots.reduce((a, b) => a + b, 0);
    const rested = !!sheet.rested;
    const book = S.spellBooks[caster.id] || [];

    const head = document.createElement("div");
    head.className = "panel-row";
    const label = document.createElement("span");
    label.className = "panel-label";
    label.textContent = `${caster.name} — prepared ${caster.memorized_spells.length} of ${totalSlots}`;
    head.appendChild(label);
    camp.appendChild(head);

    // Slot picks persist across renders in S.town.prep, keyed "level:slot";
    // first sight of a caster lays their memorized spells in as defaults.
    if (!S.town.prep[caster.id]) S.town.prep[caster.id] = prepDefaults(caster, slots);
    const picks = S.town.prep[caster.id];
    const multiLevel = slots.filter((count) => count > 0).length > 1;
    let lastRow = head;
    slots.forEach((count, i) => {
      const level = i + 1;
      if (!count) return;
      const row = document.createElement("div");
      row.className = "panel-row";
      if (multiLevel) {
        const levelLabel = document.createElement("span");
        levelLabel.className = "panel-label";
        levelLabel.textContent = `Level ${level}`;
        row.appendChild(levelLabel);
      }
      const atLevel = book.filter((id) => spellLevel(id) === level);
      for (let slot = 0; slot < count; slot++) {
        const key = `${level}:${slot}`;
        const select = document.createElement("select");
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "(leave empty)";
        select.appendChild(empty);
        for (const spellId of atLevel) {
          const option = document.createElement("option");
          option.value = spellId;
          option.textContent = spellName(spellId);
          select.appendChild(option);
        }
        if (!["", ...atLevel].includes(picks[key])) picks[key] = "";
        select.value = picks[key];
        select.addEventListener("change", () => { picks[key] = select.value; });
        row.appendChild(select);
      }
      camp.appendChild(row);
      lastRow = row;
    });
    lastRow.appendChild(button("Prepare", () => {
      const selections = [];
      slots.forEach((count, i) => {
        const level = i + 1;
        for (let slot = 0; slot < count; slot++) {
          const pick = picks[`${level}:${slot}`];
          if (pick) selections.push({ spell_id: pick, reversed: false });
        }
      });
      command({ command_type: "prepare_spells", character_id: caster.id, selections });
    }, { disabled: !rested, title: rested ? "" : PREP_HINT }));
  }
  return camp;
}

/* The study card: an arcane caster whose spell book has an open pick chooses
   what to inscribe. Clerics and non-casters never appear here — the server
   sends no `learnable` key for them, deliberately: a divine caster's new
   capability surfaces through the camp prep grid, not a book. */
function buildStudyPanel() {
  const casters = S.view.party.filter((m) =>
    m.current_hp > 0 &&
    (S.learnable[m.id] || []).length &&
    ((S.sheets[m.id] || {}).spell_picks || []).some((n) => n > 0));
  if (!casters.length) return null; // no empty card, ever
  const study = document.createElement("div");
  study.className = "panel";
  study.innerHTML = "<h3>Study</h3>";
  for (const caster of casters) {
    const picks = (S.sheets[caster.id] || {}).spell_picks || [];
    const candidates = S.learnable[caster.id] || [];
    const total = picks.reduce((a, b) => a + b, 0);

    const head = document.createElement("div");
    head.className = "panel-row";
    const label = document.createElement("span");
    label.className = "panel-label";
    label.textContent = `${caster.name} — ${total} spell${total === 1 ? "" : "s"} to learn`;
    head.appendChild(label);
    study.appendChild(head);

    const multiLevel = picks.filter((count) => count > 0).length > 1;
    const truncate = (intro) => (intro.length > 110 ? intro.slice(0, 107) + "…" : intro);
    picks.forEach((count, i) => {
      const level = i + 1;
      if (!count) return;
      const atLevel = candidates.filter((spell) => spell.level === level);
      if (!atLevel.length) return;
      for (let slot = 0; slot < count; slot++) {
        const row = document.createElement("div");
        row.className = "panel-row";
        if (multiLevel) {
          const levelLabel = document.createElement("span");
          levelLabel.className = "panel-label";
          levelLabel.textContent = `Level ${level}`;
          row.appendChild(levelLabel);
        }
        const select = document.createElement("select");
        for (const spell of atLevel) {
          const option = document.createElement("option");
          option.value = spell.id;
          option.textContent = spell.name;
          select.appendChild(option);
        }
        persistSelect(select, S.town.study, `${caster.id}:${level}:${slot}`);
        row.appendChild(select);
        row.appendChild(button("Learn", async () => {
          const spell = atLevel.find((s) => s.id === select.value);
          if (!spell) return;
          const message = `Inscribe ${spell.name} into ${caster.name}'s spell book? A spell learned cannot be unlearned.`;
          if (!await confirmDialog(message, "Inscribe")) return;
          command({ command_type: "learn_spell", character_id: caster.id, spell_id: spell.id });
        }, { cls: "accent" }));
        study.appendChild(row);
        const hint = document.createElement("div");
        hint.className = "panel-hint";
        const introOf = (id) => {
          const spell = atLevel.find((s) => s.id === id);
          return spell ? truncate(spell.intro) : "";
        };
        hint.textContent = introOf(select.value);
        select.addEventListener("change", () => { hint.textContent = introOf(select.value); });
        study.appendChild(hint);
      }
    });
  }
  return study;
}

function renderEncounterPanel(panel) {
  const enc = S.view.encounter;
  if (!enc) return;
  const card = document.createElement("div");
  card.className = "panel danger";
  card.innerHTML = "<h3>Encounter</h3>";
  for (const group of enc.groups) {
    const chip = document.createElement("div");
    chip.className = "group-chip";
    chip.innerHTML = `<span>${group.label}</span><span class="count">×${group.count}</span><span class="dist">${group.distance_feet} ft</span>`;
    card.appendChild(chip);
  }
  if (enc.stance) {
    const stance = document.createElement("div");
    stance.className = "prose-sm";
    stance.style.marginTop = "8px";
    stance.textContent = `Reaction: ${enc.stance}.`;
    card.appendChild(stance);
  }
  if (enc.pursuit_gap_feet !== null && enc.pursuit_gap_feet !== undefined) {
    const gap = document.createElement("div");
    gap.className = "prose-sm";
    gap.textContent = `They pursue — the gap is ${enc.pursuit_gap_feet} feet.`;
    card.appendChild(gap);
  }
  const row1 = document.createElement("div");
  row1.className = "panel-row";
  row1.appendChild(button("Fight", () => command({ command_type: "engage_battle" }), { cls: "accent" }));
  row1.appendChild(button("Parley", () => {
    const speaker = memberByClass("cleric", "fighter");
    if (speaker) command({ command_type: "parley", character_id: speaker.id });
  }));
  row1.appendChild(button("Wait", () => command({ command_type: "wait" })));
  card.appendChild(row1);
  const row2 = document.createElement("div");
  row2.className = "panel-row";
  const dropSelect = document.createElement("select");
  for (const [value, label] of [["none", "drop nothing"], ["treasure", "drop treasure"], ["food", "drop food"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    dropSelect.appendChild(option);
  }
  row2.appendChild(button("Run", () => command({ command_type: "evade", drop: dropSelect.value })));
  row2.appendChild(dropSelect);
  const cleric = livingMembers().find((m) => m.class_id === "cleric");
  if (cleric) {
    row2.appendChild(button("Turn undead", () => command({ command_type: "turn_undead", character_id: cleric.id })));
  }
  card.appendChild(row2);
  panel.appendChild(card);
}

const MELEE_REACH_FEET = 5;

/* The combat facts behind a weapon id — `qualities` and `missile_ranges` — read
   off whichever shape the wielded entry has: a weapon template holds them
   directly, gear with a combat facet (a torch) holds them under `combat`, and
   the player view reports a magic arm's beside its entry, identified or not. This is
   the engine's own lookup (_declaration_facet), so every shape answers the same
   two questions: how the engine classifies the weapon, and how far it reaches. */
function weaponFacts(member, weaponId) {
  for (const w of member.inventory.wielded) {
    if (w.template && w.template.id === weaponId) return w.template.combat || w.template;
    if (w.instance_id === weaponId) return w;
  }
  return null;
}

function missileReach(member, weaponId) {
  const facts = weaponFacts(member, weaponId);
  const ranges = facts && facts.missile_ranges;
  return ranges ? ranges.long.max_feet : 0;
}

/* Does this weapon, at this distance, resolve as a MISSILE attack? The engine
   classifies a declaration by the weapon's qualities and the range, never by
   which control sent it (_is_missile_declaration): a missile weapon counts as a
   shot, except a melee+missile hybrid (dagger, hand axe, spear) inside melee
   reach, which counts as a swing. Everything else — a plain melee arm, an
   unclassifiable one — is a melee attack. */
function isMissileAttack(member, weaponId, distanceFeet) {
  const facts = weaponFacts(member, weaponId);
  const qualities = (facts && facts.qualities) || [];
  if (!qualities.includes("missile")) return false;
  return !qualities.includes("melee") || distanceFeet > MELEE_REACH_FEET;
}

/* Every attack this member could name against `group`, each marked with whether
   the engine would take it and, when it would not, the reason to show alongside
   it. The rules are the engine's own, read off the encounter view rather than
   guessed at: melee reaches only the front rank (as wide as the space the party
   stands in allows — never assume the first two), melee reaches only 5 ft, a
   missile weapon must have the range, and a `reload` weapon cannot fire two
   rounds running. An illegal declaration rejects the WHOLE round, so an illegal
   choice must never be selectable. */
function attackChoices(m, enc, group) {
  const distance = group ? group.distance_feet : MELEE_REACH_FEET;
  const inRank = (enc.front_rank || []).includes(m.id);
  const reloading = (enc.reloading || []).includes(m.id);
  const choices = [];
  for (const weapon of wieldedWeapons(m)) {
    const facts = weaponFacts(m, weapon);
    const qualities = (facts && facts.qualities) || [];
    const missile = isMissileAttack(m, weapon, distance);
    let reason = "";
    if (missile && distance > missileReach(m, weapon)) reason = "out of range";
    else if (missile && reloading && qualities.includes("reload")) reason = "reloading";
    else if (!missile && !inRank) reason = "back rank";
    else if (!missile && distance > MELEE_REACH_FEET) reason = "out of reach";
    const verb = missile ? "Shoot" : "Attack";
    choices.push({
      value: JSON.stringify({ action: "attack", weapon }),
      label: `${verb} — ${weaponLabel(m, weapon)}` + (reason ? ` (${reason})` : ""),
      legal: !reason,
    });
  }
  if (!choices.length) {
    // Bare hands are a melee attack like any other: front rank, within reach.
    const reason = !inRank ? "back rank" : distance > MELEE_REACH_FEET ? "out of reach" : "";
    choices.push({
      value: JSON.stringify({ action: "attack", weapon: null }),
      label: "Attack — unarmed" + (reason ? ` (${reason})` : ""),
      legal: !reason,
    });
  }
  return choices;
}

// Opening orders, one per declarer, so a round can be a single click.
// Preference: a memorized spell against a grouped foe; else the best legal
// attack — a shot when the range is open, a swing when it is not; else close
// the distance if the member can move; else hold. Nothing here proposes a
// declaration `attackChoices` marked illegal, so a one-click round is always a
// round the engine accepts.
function declDefaults() {
  const enc = S.view.encounter;
  const group = targetGroup();
  const distance = group ? group.distance_feet : MELEE_REACH_FEET;
  const decls = {};
  for (const m of declarers()) {
    const spell = m.memorized_spells[0];
    const attack = attackChoices(m, enc, group).find((c) => c.legal);
    const canMove = !(enc.immobile || []).includes(m.id);
    if (spell && group && group.count > 1) {
      decls[m.id] = { action: "cast", spell: spell.spell_id };
    } else if (attack) {
      decls[m.id] = JSON.parse(attack.value);
    } else if (spell) {
      decls[m.id] = { action: "cast", spell: spell.spell_id };
    } else if (canMove && distance > MELEE_REACH_FEET) {
      decls[m.id] = { action: "move", move: "close" };
    } else {
      decls[m.id] = { action: "hold" };
    }
  }
  return decls;
}

function renderBattleContext(panel) {
  const enc = S.view.encounter;
  if (!enc) return;
  const card = document.createElement("div");
  card.className = "panel danger";
  card.innerHTML = "<h3>The enemy</h3>";
  for (const group of enc.groups.filter((g) => g.count > 0)) {
    const chip = document.createElement("div");
    chip.className = "group-chip";
    chip.innerHTML = `<span>${esc(group.label)}</span><span class="count">×${group.count}</span><span class="dist">${group.distance_feet} ft</span>`;
    card.appendChild(chip);
  }
  panel.appendChild(card);
}

function weaponLabel(m, id) {
  for (const w of m.inventory.wielded) {
    if (w.template && w.template.id === id) return w.template.name;
    if (w.instance_id === id) return w.name || w.display || "magic arm";
  }
  return id;
}

function mostWounded() {
  return livingMembers().reduce((worst, m) =>
    (m.max_hp - m.current_hp) > (worst.max_hp - worst.current_hp) ? m : worst);
}

/* ---------- character sheet (double-click a party row) ---------- */
/* Laid out the way the Moldvay set laid it out: abilities down the side,
   saves and combat in their own blocks, equipment as a list. Every value
   comes from the server's player-safe sheet extras — it's a window into the
   engine's character record, not a form. */

const SHEET_ABILITIES = [
  ["str", "Strength"], ["int", "Intelligence"], ["wis", "Wisdom"],
  ["dex", "Dexterity"], ["con", "Constitution"], ["cha", "Charisma"],
];

const SHEET_SAVES = [
  ["death", "Death ray or poison"],
  ["wands", "Magic wands"],
  ["paralysis", "Paralysis or turn to stone"],
  ["breath", "Dragon breath"],
  ["spells", "Rods, staves, or spells"],
];

const SHEET_MODS = [
  ["melee", "Melee attack and damage"],
  ["missile", "Missile attack"],
  ["armour_class", "Armour class"],
  ["hit_points", "Hit points per die"],
  ["magic_saves", "Saves vs magic"],
  ["reactions", "Reactions"],
];

function languageName(id) {
  const raw = id.startsWith("alignment_") ? id.slice("alignment_".length) : id;
  const text = raw.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function signed(n) {
  return n > 0 ? `+${n}` : `${n}`;
}

function openSheet(id) {
  S.sheetOpen = id;
  renderSheet();
  $("sheet-overlay").classList.remove("hidden");
}

function closeSheet() {
  S.sheetOpen = null;
  $("sheet-overlay").classList.add("hidden");
}

function cycleSheet(step) {
  const ids = S.view.party.map((m) => m.id);
  const at = ids.indexOf(S.sheetOpen);
  if (at === -1) return;
  S.sheetOpen = ids[(at + step + ids.length) % ids.length];
  renderSheet();
}

/* Encumbrance, derived from the server's player-safe sheet extras. Returns null
   when the table tracks no weight (encumbrance mode "none"); otherwise the coins
   carried against the 1,600-coin maximum load, and whether the load has frozen
   the character (movement 0 — and, since the party moves at its slowest member,
   the whole party with them). */
function encumbrance(sheet) {
  const e = sheet && sheet.encumbrance;
  if (!e || e.mode === "none") return null;
  return {
    carried: e.carried_coins,
    max: e.max_load_coins,
    overloaded: sheet.movement_per_turn === 0,
    pct: Math.min(100, Math.round((e.carried_coins / e.max_load_coins) * 100)),
  };
}

function itemName(it) {
  if (it.template) return it.template.name || it.template.id || "item";
  return it.name || it.display || "an item";
}

/* The id a drop/give/equip command names: the catalog id for a mundane item
   (which carries no per-instance id), the instance id for a magic item. */
function itemActionId(it) {
  return it.template ? it.template.id : it.instance_id;
}

/* A mundane template is equippable when it can be wielded (has weapon damage) or
   worn (armour and shields carry an armour-class figure). Magic items are offered
   to equip regardless — the engine validates class usability and rejects the rest. */
function isEquippable(template) {
  return !!(template && (template.damage || "ac" in template));
}

function weaponAttackLine(w) {
  const template = w.template;
  if (!template) return null;
  const damage = template.damage || (template.combat && template.combat.damage);
  if (!damage) return null;
  let value = damage;
  const ranges = template.missile_ranges;
  if (ranges && ranges.short && ranges.medium && ranges.long) {
    value += ` · ${ranges.short.max_feet}/${ranges.medium.max_feet}/${ranges.long.max_feet}`;
  }
  return { name: template.name, value };
}

const COIN_UNITS = ["pp", "gp", "ep", "sp", "cp"];

/* The interactive "carry" section: the encumbrance readout (the overload
   indicator), plus per-item and per-coin controls to equip, hand off, or drop
   weight. Buttons carry data-* the delegated #sheet-panel handler reads; giving
   needs a recipient, so a two-member (or larger) living party gates it. Full
   management is a town-or-exploring action — mid-encounter and mid-battle the
   controls step aside, though the readout stays. */
function carrySection(m, sheet, inv) {
  const enc = encumbrance(sheet);
  const readout = enc
    ? `<div class="load-readout ${enc.overloaded ? "over" : ""}">
         <span class="lr-num">${enc.carried} / ${enc.max} cn</span>
         <span class="load-bar"><span style="width:${enc.pct}%"></span></span>
         ${enc.overloaded ? '<span class="member-tag red">overloaded — cannot move</span>' : ""}
       </div>`
    : "";

  const manageable = S.view.mode === "town" || S.view.mode === "exploring";
  if (!manageable) {
    return `<div class="sheet-manage">
      <div class="manage-head"><h4>Carry</h4>${readout}</div>
      <div class="manage-hint">Distribute the load in town or while exploring.</div>
    </div>`;
  }

  const recipients = livingMembers().filter((x) => x.id !== m.id);
  const canGive = recipients.length > 0;
  const giveTo = canGive
    ? `<label class="give-to">Give to
         <select id="give-target">${recipients
           .map((r) => `<option value="${r.id}">${esc(r.name)}</option>`)
           .join("")}</select>
       </label>`
    : '<span class="give-none">No one else can carry.</span>';

  // Dropping goods onto the floor is an exploring (or encounter) action — never
  // in town, where a character would sell or hand off instead. Giving and
  // (un)equipping are legal in town too.
  const canDrop = S.view.mode === "exploring";
  // Selling is the town's verb: one appraised piece at a time, engine-credited
  // to whoever carries it.
  const canSell = S.view.mode === "town";
  // Using is a dungeon verb (use_item is legal exploring and in encounters,
  // but the sheet's full controls only show town-or-exploring), and only magic
  // instances have a usable action — mundane gear never (a torch is lit via
  // light_source, not used).
  const canUse = S.view.mode === "exploring";
  const giveBtn = (attrs) => (canGive ? `<button class="btn mini ghost" data-act="give-item" ${attrs}>Give</button>` : "");
  const dropBtn = (attrs) => (canDrop ? `<button class="btn mini danger" data-act="drop-item" ${attrs}>Drop</button>` : "");
  const sellBtn = (attrs) => (canSell ? `<button class="btn mini ghost" data-act="sell-item" ${attrs}>Sell</button>` : "");
  const useBtn = (attrs) => (canUse ? `<button class="btn mini ghost" data-act="use-item" ${attrs}>Use</button>` : "");

  const coinRows = COIN_UNITS.filter((u) => inv.purse[u] > 0)
    .map((u) => {
      const n = inv.purse[u];
      return `<div class="carry-row">
        <span class="cr-name">${n} ${u}</span>
        <input class="cr-amt" type="number" data-denom="${u}" value="${n}" min="1" max="${n}" aria-label="${u} to move">
        ${canGive ? `<button class="btn mini ghost" data-act="give-coins" data-denom="${u}">Give</button>` : ""}
        ${canDrop ? `<button class="btn mini danger" data-act="drop-coins" data-denom="${u}">Drop</button>` : ""}
      </div>`;
    })
    .join("");

  const valuableRows = inv.valuables
    .map(
      (v) => `<div class="carry-row">
        <span class="cr-name">${esc(v.name || v.kind)} <em>${v.value_gp} gp</em></span>
        ${giveBtn(`data-id="${esc(v.instance_id)}"`)}
        ${sellBtn(`data-id="${esc(v.instance_id)}"`)}
        ${dropBtn(`data-id="${esc(v.instance_id)}"`)}
      </div>`,
    )
    .join("");

  const packRows = inv.items
    .map((it) => {
      const id = itemActionId(it);
      const qty = it.quantity || 1;
      const magic = !it.template;
      const cursed = magic && it.cursed;
      const attrs = `data-id="${esc(id)}" data-qty="${qty}"${magic ? ' data-magic="1"' : ""}`;
      const label = esc(itemName(it)) + (qty > 1 ? ` <em>×${qty}</em>` : "");
      const equip = (magic || isEquippable(it.template))
        ? `<button class="btn mini ghost" data-act="equip" data-id="${esc(id)}">Equip</button>`
        : "";
      const use = magic ? useBtn(attrs) : "";
      const hand = cursed
        ? '<span class="cr-note">cursed — stuck</span>'
        : `${giveBtn(attrs)}${dropBtn(attrs)}`;
      return `<div class="carry-row"><span class="cr-name">${label}</span>${equip}${use}${hand}</div>`;
    })
    .join("");

  const equipped = [...inv.wielded];
  if (inv.worn_armour) equipped.push(inv.worn_armour);
  if (inv.shield) equipped.push(inv.shield);
  equipped.push(...inv.rings);
  const equippedRows = equipped
    .map((it) => {
      const id = itemActionId(it);
      const cursed = !it.template && it.cursed;
      return `<div class="carry-row">
        <span class="cr-name">${esc(itemName(it))} <em>equipped</em></span>
        <button class="btn mini ghost" data-act="unequip" data-id="${esc(id)}"${cursed ? " disabled title=\"A cursed item cannot be removed.\"" : ""}>Unequip</button>
      </div>`;
    })
    .join("");

  const rows = coinRows + valuableRows + packRows + equippedRows;
  return `<div class="sheet-manage">
    <div class="manage-head"><h4>Carry</h4>${readout}${giveTo}</div>
    <div class="manage-grid">${rows || '<div class="cr-empty">Nothing to carry.</div>'}</div>
  </div>`;
}

function renderSheet() {
  const m = S.view.party.find((x) => x.id === S.sheetOpen);
  if (!m) {
    closeSheet();
    return;
  }
  const sheet = S.sheets[m.id];
  const panel = $("sheet-panel");
  if (!sheet) {
    panel.innerHTML = `<div class="sheet-head"><span class="s-name">${esc(m.name)}</span></div>`;
    return;
  }
  const inv = m.inventory;
  const enc = encumbrance(sheet);

  const abilityRows = SHEET_ABILITIES.map(([key, label]) =>
    `<div class="ability"><span class="a-name">${label}</span>
       <span class="a-val">${sheet.scores[key]}</span></div>`).join("");

  const modRows = SHEET_MODS.map(([key, label]) => {
    const value = sheet.modifiers[key];
    const cls = value > 0 ? "plus" : value < 0 ? "minus" : "";
    return `<div class="kv"><span class="k">${label}</span>
      <span class="v ${cls}">${signed(value)}</span></div>`;
  }).join("") +
    `<div class="kv"><span class="k">Open doors</span>
      <span class="v">${sheet.modifiers.open_doors}-in-6</span></div>`;

  const saveRows = SHEET_SAVES.map(([key, label]) =>
    `<div class="kv"><span class="k">${label}</span><span class="v">${sheet.saves[key]}</span></div>`).join("");

  const attacks = inv.wielded.map(weaponAttackLine).filter(Boolean);
  const attackRows = attacks.length
    ? attacks.map((a) => `<div class="kv"><span class="k">${esc(a.name)}</span><span class="v">${esc(a.value)}</span></div>`).join("")
    : '<div class="kv"><span class="k">Unarmed</span><span class="v">1</span></div>';

  const gear = [];
  if (inv.worn_armour && inv.worn_armour.template) gear.push({ name: inv.worn_armour.template.name });
  if (inv.shield) gear.push({ name: "Shield" });
  for (const w of inv.wielded) gear.push({ name: w.template ? w.template.name : (w.name || "weapon") });
  for (const item of inv.items) {
    gear.push({ name: item.template ? item.template.name : "item", qty: item.quantity > 1 ? item.quantity : null });
  }
  const gearRows = gear.map((g) =>
    `<li>${esc(g.name)}${g.qty ? `<span>×${g.qty}</span>` : ""}</li>`).join("");

  const purse = inv.purse;
  const coins = [["pp", purse.pp], ["gp", purse.gp], ["ep", purse.ep], ["sp", purse.sp], ["cp", purse.cp]]
    .filter(([, n]) => n > 0)
    .map(([unit, n]) => `${n} ${unit}`)
    .join(", ") || "0 gp";
  const treasure = inv.valuables.length
    ? `${inv.valuables.length} piece${inv.valuables.length === 1 ? "" : "s"} (${inv.valuables.reduce((sum, v) => sum + (v.value_gp || 0), 0)} gp)`
    : null;

  const memorized = new Set(m.memorized_spells.map((s) => s.spell_id));
  const book = S.spellBooks[m.id] || [];
  const spellRows = book.map((id) =>
    `<li>${esc(spellName(id))}${memorized.has(id) ? "<span>prepared</span>" : ""}</li>`).join("");
  const spellPickHint = (sheet.spell_picks || []).some((n) => n > 0)
    ? '<div class="panel-hint">A new spell can be learned — visit the town.</div>'
    : "";

  const languages = sheet.languages.map(languageName).join(", ");
  const nextXp = sheet.next_level_xp;
  const xpLine = `${sheet.xp.toLocaleString()} xp` +
    (nextXp ? ` · ${nextXp.toLocaleString()} for level ${m.level + 1}` : " · highest level") +
    (sheet.xp_bonus_pct ? ` · ${signed(sheet.xp_bonus_pct)}% earned` : "");
  const xpWidth = nextXp ? Math.min(100, (sheet.xp / nextXp) * 100) : 100;
  const alignment = sheet.alignment.charAt(0).toUpperCase() + sheet.alignment.slice(1);

  panel.innerHTML = `
    <div class="sheet-head">
      <span class="s-name">${esc(m.name)}</span>
      <span class="s-class">${esc(sheet.class_name)}${sheet.level_title ? ` · ${esc(sheet.level_title)}` : ""} · level ${m.level} · ${esc(alignment)}</span>
      <div class="s-xp">
        <div class="n">${xpLine}</div>
        <div class="xp-bar"><span class="xp-fill" style="width:${xpWidth}%"></span></div>
      </div>
    </div>
    <div class="sheet-body">
      <div class="sheet-col">
        <h4>Abilities</h4>
        ${abilityRows}
        <h4>Adjustments</h4>
        ${modRows}
      </div>
      <div class="sheet-col">
        <h4>Combat</h4>
        <div class="kv"><span class="k">Armour class</span><span class="v">${sheet.armour_class} [${sheet.armour_class_ascending}]</span></div>
        <div class="kv"><span class="k">Hit points</span><span class="v">${m.current_hp} / ${m.max_hp}</span></div>
        <div class="kv"><span class="k">THAC0</span><span class="v">${sheet.thac0} [${signed(sheet.attack_bonus)}]</span></div>
        <div class="kv"><span class="k">Movement</span><span class="v${enc && enc.overloaded ? " over" : ""}">${
          enc && enc.overloaded
            ? "0&prime; — overloaded"
            : `${sheet.movement_per_turn}&prime; (${Math.floor(sheet.movement_per_turn / 3)}&prime;)`
        }</span></div>
        ${enc ? `<div class="kv"><span class="k">Load</span><span class="v${enc.overloaded ? " over" : ""}">${enc.carried} / ${enc.max} cn</span></div>` : ""}
        <h4>Attacks</h4>
        ${attackRows}
        <h4>Saving throws</h4>
        ${saveRows}
      </div>
      <div class="sheet-col">
        <h4>Equipment</h4>
        <ul>${gearRows}</ul>
        <h4>Wealth</h4>
        <div class="kv"><span class="k">Purse</span><span class="v">${coins}</span></div>
        ${treasure ? `<div class="kv"><span class="k">Treasure</span><span class="v">${treasure}</span></div>` : ""}
        ${spellRows ? `<h4>Spells</h4><ul>${spellRows}</ul>${spellPickHint}` : ""}
        <h4>Languages</h4>
        <div class="kv"><span class="k">${esc(languages)}</span></div>
      </div>
    </div>
    ${carrySection(m, sheet, inv)}
    <div class="sheet-foot">
      <span>esc to close</span>
      <span>&larr; &rarr; other members</span>
    </div>`;
}

$("sheet-overlay").addEventListener("click", (event) => {
  if (event.target === $("sheet-overlay")) closeSheet();
});

/* Delegated once: the "carry" section rebuilds on every render, so its equip /
   give / drop buttons are read by data-* rather than bound per node. Giving needs
   the recipient chosen in #give-target; coins move the amount typed for that
   denomination. command() re-renders the sheet with fresh weight afterward. */
$("sheet-panel").addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn || btn.disabled) return;
  const m = S.view.party.find((x) => x.id === S.sheetOpen);
  if (!m) return;
  const panel = $("sheet-panel");
  const act = btn.dataset.act;

  const recipient = () => {
    const select = panel.querySelector("#give-target");
    return select ? select.value : null;
  };
  const coinsFor = (denom) => {
    const input = panel.querySelector(`.cr-amt[data-denom="${denom}"]`);
    let n = parseInt(input && input.value, 10);
    if (!Number.isFinite(n) || n < 1) return null;
    const max = parseInt(input.max, 10);
    if (Number.isFinite(max) && n > max) n = max;
    return { [denom]: n };
  };
  const itemIds = () => {
    const id = btn.dataset.id;
    if (btn.dataset.magic) return [id];
    return Array(parseInt(btn.dataset.qty || "1", 10) || 1).fill(id);
  };

  if (act === "equip") {
    command({ command_type: "equip_item", character_id: m.id, item_id: btn.dataset.id });
  } else if (act === "unequip") {
    command({ command_type: "unequip_item", character_id: m.id, item_id: btn.dataset.id });
  } else if (act === "drop-item") {
    command({ command_type: "drop_items", character_id: m.id, item_ids: itemIds() });
  } else if (act === "give-item") {
    const to = recipient();
    if (to) command({ command_type: "give_items", character_id: m.id, recipient_id: to, item_ids: itemIds() });
  } else if (act === "drop-coins") {
    const coins = coinsFor(btn.dataset.denom);
    if (coins) command({ command_type: "drop_items", character_id: m.id, coins });
  } else if (act === "give-coins") {
    const to = recipient();
    const coins = coinsFor(btn.dataset.denom);
    if (to && coins) command({ command_type: "give_items", character_id: m.id, recipient_id: to, coins });
  } else if (act === "sell-item") {
    command({ command_type: "sell_treasure", item_ids: [btn.dataset.id] });
  } else if (act === "use-item") {
    command({ command_type: "use_item", character_id: m.id, item_id: btn.dataset.id });
  }
});

/* ---------- the front door: title, library, staging, saves, menu ---------- */
/* A real screen-state machine. The title and everything behind it
   are application states, not a modal over a live game: browsing adventures
   and saves is client-side reading, staging a party lives in a server-side
   party builder, and nothing POSTs /api/games until "Begin the adventure".
   Summoned from a live game (Menu / Esc), these screens overlay the session
   non-destructively — the game waits server-side until the player commits to
   a new one or abandons it. */

/* The two modes in which the session has ended, mirroring osrlib's
   `SessionMode.terminal` (`../osrlib-python/src/osrlib/crawl/commands.py:127-130`).
   Every "has this session ended" question in the client branches on this set —
   string-matching `game_over` alone is what it retires. */
const TERMINAL_MODES = new Set(["game_over", "victory"]);

const UI = {
  screen: "game",   // game | title | library | staging | saves | menu | gameover | victory
  origin: "title",  // where Back leads from the flow screens: "title" or "game"
  libSel: null,     // adventure highlighted in the library pane (client-side only)
  staging: null,    // { builderId, state, source: "premade"|"veterans", saveId }
  renamingSave: null, // save_id whose name is open for editing in the browser
};

function showScreen(name) {
  UI.screen = name;
  const overlay = $("overlay");
  const card = $("overlay-card");
  card.classList.remove("wizard");
  if (name === "game") {
    overlay.classList.add("hidden");
    return;
  }
  overlay.classList.remove("hidden");
  overlay.scrollTop = 0;
  if (name === "title") renderTitle(card);
  else if (name === "library") renderLibrary(card);
  else if (name === "staging") renderStaging(card);
  else if (name === "saves") renderSavesBrowser(card);
  else if (name === "menu") renderMenu(card);
  else if (name === "gameover") renderGameOver(card);
  else if (name === "victory") renderVictory(card);
}

function goBack() {
  if (UI.screen === "menu") showScreen("game");
  else if (UI.screen === "library") showScreen(UI.origin === "game" ? "menu" : "title");
  else if (UI.screen === "staging") showScreen("library");
  else if (UI.screen === "saves") showScreen(UI.origin === "game" ? "menu" : "title");
}

function saveDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function rosterLine(party) {
  return party.map((p) => (p.dead ? `†${p.name}` : p.name)).join(", ");
}

/* --- the title: three verbs, nothing created --- */

async function openTitle() {
  try {
    await fetchLibrary();
  } catch {
    /* the door still opens without the library; Continue and Load just hide */
  }
  showScreen("title");
}

function renderTitle(card) {
  const newest = S.saves[0] || null;
  card.innerHTML = `
    <h2 class="overlay-title">OSR Web</h2>
    <div class="overlay-sub">An old-school dungeon crawler.</div>
    <div class="door-actions">
      ${newest ? `
      <button class="btn accent big door-btn" id="door-continue" type="button">
        Continue
        <span class="btn-sub">${esc(newest.name)}</span>
      </button>` : ""}
      <button class="btn big ${newest ? "" : "accent "}door-btn" id="door-new" type="button">
        New adventure
        <span class="btn-sub">choose an adventure and a party</span>
      </button>
      ${S.saves.length ? `
      <button class="btn big door-btn" id="door-load" type="button">
        Load game
        <span class="btn-sub">${S.saves.length} saved game${S.saves.length === 1 ? "" : "s"}</span>
      </button>` : ""}
    </div>`;
  const cont = $("door-continue");
  if (cont) cont.addEventListener("click", () => restoreSave(newest.save_id));
  $("door-new").addEventListener("click", () => { UI.origin = "title"; openLibrary(); });
  const load = $("door-load");
  if (load) load.addEventListener("click", () => { UI.origin = "title"; openSavesBrowser(); });
}

/* --- the adventure library: reading costs nothing --- */

async function openLibrary() {
  try {
    await fetchLibrary();
  } catch (error) {
    toast(error.message, false);
  }
  const entries = S.adventures || [];
  if (!UI.libSel || !entries.some((a) => a.id === UI.libSel)) {
    UI.libSel = S.adventureId || (entries[0] && entries[0].id) || null;
  }
  showScreen("library");
}

function renderLibrary(card) {
  card.classList.add("wizard"); // the wide, left-aligned card layout
  const entries = S.adventures || [];
  const chosen = entries.find((a) => a.id === UI.libSel) || entries[0] || null;
  const list = entries.map((a) => `
    <button class="lib-item${chosen && a.id === chosen.id ? " selected" : ""}" data-adventure="${esc(a.id)}" type="button">
      ${esc(a.name)}
    </button>`).join("");
  const facts = chosen ? [
    chosen.town_name ? `the town of ${chosen.town_name}` : null,
    chosen.dungeon_count === 1 ? "one dungeon" : `${chosen.dungeon_count} dungeons`,
  ].filter(Boolean).join(" · ") : "";
  const detail = chosen ? `
    <h3 class="lib-name">${esc(chosen.name)}</h3>
    <div class="lib-desc">${esc(chosen.description)}</div>
    ${chosen.hooks && chosen.hooks.length ? `<p class="overlay-hook lib-hook">${esc(chosen.hooks[0])}</p>` : ""}
    ${facts ? `<div class="lib-facts">${esc(facts)}</div>` : ""}
    ${chosen.removable ? '<div class="lib-manage"><button class="btn danger" id="lib-delete" type="button">Delete</button></div>' : ""}` :
    '<div class="wiz-note">No adventures found.</div>';
  card.innerHTML = `
    <div class="overlay-eyebrow">new adventure — step one</div>
    <h2 class="overlay-title wiz-title">Choose an adventure</h2>
    <div class="lib-cols">
      <div class="lib-list">${list}
        <button class="lib-add" id="lib-add" type="button">Load from file…</button>
      </div>
      <div class="lib-detail wiz-panel">${detail}</div>
    </div>
    <input type="file" id="lib-file" accept=".json,application/json" hidden>
    <div class="overlay-actions">
      <button class="btn ghost big push" id="lib-back" type="button">Back</button>
      <button class="btn accent big" id="lib-next" type="button" ${chosen ? "" : "disabled"}>Next — the party</button>
    </div>`;
  for (const btn of card.querySelectorAll(".lib-item")) {
    btn.addEventListener("click", () => {
      UI.libSel = btn.dataset.adventure; // selection is client-side state: no POST
      renderLibrary(card);
    });
  }
  $("lib-add").addEventListener("click", () => $("lib-file").click());
  $("lib-file").addEventListener("change", () => addAdventureFile(card));
  const remove = $("lib-delete");
  if (remove) remove.addEventListener("click", () => deleteAdventure(chosen, card));
  $("lib-back").addEventListener("click", goBack);
  $("lib-next").addEventListener("click", () => {
    S.adventureId = UI.libSel;
    openStaging();
  });
}

/* Upload a browsed adventure JSON into the library. The File object itself is
   the request body — fetch sends a Blob's bytes raw, so the server vets,
   dedupes, and writes exactly what sits on disk (no re-encode, no BOM strip) —
   which is why this bypasses apiFetch's stringify on purpose. */
async function addAdventureFile(card) {
  const input = $("lib-file");
  const file = input.files && input.files[0];
  if (!file) return;
  // Clear now: after a failure the input survives, and re-picking the same
  // file fires no change event while a value is set.
  input.value = "";
  try {
    const response = await fetch("/api/adventures", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: file,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `request failed (${response.status})`);
    }
    const payload = await response.json();
    S.adventures = payload.adventures;
    UI.libSel = payload.adventure_id;
    // The overlay card is shared by every screen: only repaint if the library
    // is still the one showing (the POST may resolve after Back or Next).
    if (UI.screen === "library") renderLibrary(card);
    const entry = (S.adventures || []).find((a) => a.id === payload.adventure_id);
    toast(`${entry ? entry.name : "The adventure"} is in the library.`, true);
  } catch (error) {
    toast(error.message, false);
  }
}

/* What deleting an adventure actually removes from the server's own disk. The
   three cases differ far too much to paper over with one sentence: a symlinked
   forge run unlinks and leaves the run whole, a dropped folder goes with
   everything in it. The server sends the kind (never a path). */
const ADVENTURE_DELETE_NOTE = {
  file: "Its file is removed from the server's adventures folder.",
  link: "The link in the server's adventures folder is removed — the files it points at are left alone.",
  folder: "Its folder in the server's adventures folder is deleted, with everything in it.",
};

/* Delete one adventure from the library. A game already under way is untouched:
   the server holds its document in memory, and a save carries its own copy. */
async function deleteAdventure(entry, card) {
  if (!entry) return;
  const note = ADVENTURE_DELETE_NOTE[entry.source_kind] || "";
  const ok = await confirmDialog(
    `Delete "${entry.name}"? ${note} This cannot be undone.`,
    "Delete"
  );
  if (!ok) return;
  try {
    const payload = await apiFetch(`/adventures/${encodeURIComponent(entry.id)}`, undefined, "DELETE");
    // Ids are name slugs, so a delete can renumber a collided neighbour — repaint
    // from the server's refreshed listing rather than filtering the old one.
    S.adventures = payload.adventures;
    const known = (id) => payload.adventures.some((a) => a.id === id);
    if (!known(UI.libSel)) UI.libSel = payload.default_id;
    if (!known(S.adventureId)) S.adventureId = null;
    if (UI.screen === "library") renderLibrary(card);
    toast(`${entry.name} is out of the library.`, true);
  } catch (error) {
    toast(error.message, false);
  }
}

/* --- staging: the party, chosen explicitly; Begin is the only commit --- */

async function openStaging() {
  if (!S.adventures) {
    try {
      await fetchLibrary();
    } catch { /* staging still renders; veterans just stays empty */ }
  }
  if (!UI.staging) {
    UI.staging = { builderId: null, state: null, source: "premade", saveId: null };
    await stagePremadeBuilder();
  }
  showScreen("staging");
}

async function stagePremadeBuilder() {
  try {
    const payload = await apiFetch("/party-builders", {
      premade: true,
      house_rules: storedHouseRules(),
    });
    UI.staging.builderId = payload.builder_id;
    UI.staging.state = payload;
  } catch (error) {
    toast(error.message, false);
  }
}

async function stagingStep(params) {
  if (S.busy || !UI.staging) return;
  S.busy = true;
  try {
    const payload = await apiFetch(`/party-builders/${UI.staging.builderId}/step`, params);
    UI.staging.state = payload;
    if (!payload.accepted) toast(payload.rejections.map((r) => r.message).join(" "), false);
  } catch {
    // A restarted server forgot the builder — roll a fresh premade set.
    await stagePremadeBuilder();
  } finally {
    S.busy = false;
  }
  if (UI.screen === "staging") showScreen("staging");
}

function renderStaging(card) {
  card.classList.add("wizard");
  const st = UI.staging || { source: "premade", state: null };
  const adventure = (S.adventures || []).find((a) => a.id === S.adventureId);
  const saves = S.saves || [];
  const veterans = st.source === "veterans";
  const rules = storedHouseRules();

  let body = "";
  if (!veterans) {
    const members = (st.state && st.state.members) || [];
    const rows = members.map((m, i) => `
      <div class="roster-row">
        <input class="r-name-input" data-index="${i}" maxlength="40" value="${esc(m.name)}" aria-label="Character name">
        <span class="r-class">${esc(m.class_name)}</span>
        <span class="r-hp">${m.max_hp} hp</span>
        <span class="r-scores">${ABILITIES.map((a) => `${a.toUpperCase()} ${m.scores[a]}`).join(" ")}</span>
      </div>`).join("");
    body = `
      <div class="overlay-rules staging-rules">
        <span class="rules-label">House rules</span>
        <label class="rule-pick" title="Roll four dice per ability, keep the best three">
          <input type="checkbox" id="st-4d6"${rules.ability_method === "4d6_drop_lowest" ? " checked" : ""}>
          4d6, drop the lowest
        </label>
        <label class="rule-pick" title="Take the top of the hit die instead of rolling">
          <input type="checkbox" id="st-maxhp"${rules.max_hp ? " checked" : ""}>
          Maximum starting hit points
        </label>
      </div>
      <div class="roster staging-roster">${rows || '<div class="wiz-note">Rolling the party…</div>'}</div>
      <div class="roster-hint">Click a name to edit it.</div>`;
  } else {
    if (!saves.some((s) => s.save_id === st.saveId)) st.saveId = saves[0] ? saves[0].save_id : null;
    const chosen = saves.find((s) => s.save_id === st.saveId) || null;
    const options = saves.map((s) =>
      `<option value="${esc(s.save_id)}"${s.save_id === st.saveId ? " selected" : ""}>${esc(s.name)}</option>`).join("");
    const rows = chosen ? chosen.party.map((p) => `
      <div class="roster-row${p.dead ? " dead" : ""}">
        <span class="r-name">${p.dead ? "†" : ""}${esc(p.name)}</span>
        <span class="r-class">${esc(CLASS_NAMES[p.class_id] || p.class_id)} ${p.level > 1 ? p.level : ""}</span>
        <span class="r-hp">${p.dead ? "slain" : ""}</span>
      </div>`).join("") : "";
    body = `
      <div class="vet-pick">
        <label class="overlay-pick">Saved game
          <select id="st-save"> ${options}</select>
        </label>
        ${chosen ? '<button class="btn danger" id="st-save-del" type="button">Delete</button>' : ""}
      </div>
      <div class="roster staging-roster">${rows}</div>
      <div class="roster-hint">Levels, experience, and gear carry over; wounds and spent effects do not.</div>`;
  }

  card.innerHTML = `
    <div class="overlay-eyebrow">new adventure — step two</div>
    <h2 class="overlay-title wiz-title">The party</h2>
    <div class="overlay-sub wiz-sub">${adventure ? `setting out for ${esc(adventure.name)}` : ""}</div>
    <div class="stage-tabs">
      <button class="stage-tab${veterans ? "" : " selected"}" id="tab-premade" type="button">Premade party</button>
      ${saves.length ? `<button class="stage-tab${veterans ? " selected" : ""}" id="tab-veterans" type="button">Veterans of a saved game</button>` : ""}
    </div>
    ${body}
    <div class="overlay-actions">
      <button class="btn ghost big push" id="st-back" type="button">Back</button>
      ${veterans ? "" : '<button class="btn big" id="st-reroll" type="button">Roll new characters</button>'}
      <button class="btn big" id="st-build" type="button">Build a custom party</button>
      <button class="btn accent big" id="st-begin" type="button" ${veterans && !st.saveId ? "disabled" : ""}>Begin the adventure</button>
    </div>`;

  $("st-back").addEventListener("click", goBack);
  $("tab-premade").addEventListener("click", () => { st.source = "premade"; renderStaging(card); });
  const vetTab = $("tab-veterans");
  if (vetTab) vetTab.addEventListener("click", () => { st.source = "veterans"; renderStaging(card); });
  $("st-build").addEventListener("click", () => startBuilder());
  $("st-begin").addEventListener("click", beginAdventure);
  const reroll = $("st-reroll");
  if (reroll) reroll.addEventListener("click", () => stagingStep({ action: "premade" }));

  if (!veterans) {
    const applyRules = () => {
      const next = {
        ability_method: $("st-4d6").checked ? "4d6_drop_lowest" : "3d6",
        max_hp: $("st-maxhp").checked,
      };
      saveHouseRules(next);
      stagingStep({ action: "premade", ...next });
    };
    $("st-4d6").addEventListener("change", applyRules);
    $("st-maxhp").addEventListener("change", applyRules);
    for (const input of card.querySelectorAll(".r-name-input")) {
      input.addEventListener("keydown", (event) => {
        event.stopPropagation();
        if (event.key === "Enter") input.blur();
        else if (event.key === "Escape") {
          const member = st.state.members[Number(input.dataset.index)];
          if (member) input.value = member.name;
          input.blur();
        }
      });
      input.addEventListener("blur", () => {
        const index = Number(input.dataset.index);
        const member = st.state.members[index];
        const name = input.value.trim();
        if (!member || !name || name === member.name) {
          if (member) input.value = member.name;
          return;
        }
        stagingStep({ action: "rename", index, name });
      });
    }
  } else {
    $("st-save").addEventListener("change", (event) => {
      st.saveId = event.target.value;
      renderStaging(card);
    });
    const removeSave = $("st-save-del");
    if (removeSave) removeSave.addEventListener("click", () => deleteStagedSave(card));
  }
}

/* Prune the picked save without leaving the staging screen. Deleting the last
   one takes the veterans tab with it, so fall back to the premade party rather
   than leave an empty picker selected. */
async function deleteStagedSave(card) {
  const st = UI.staging;
  const save = (S.saves || []).find((s) => s.save_id === st.saveId);
  if (!save) return;
  if (!await confirmDialog(`Delete "${save.name}"? This cannot be undone.`, "Delete")) return;
  try {
    await apiFetch(`/saves/${save.save_id}`, undefined, "DELETE");
    S.saves = S.saves.filter((s) => s.save_id !== save.save_id);
    if (!S.saves.length) st.source = "premade";
    renderStaging(card); // picks the next save itself when this one is gone
  } catch (error) {
    toast(error.message, false);
  }
}

/* The single commit: the one call that creates a game. */
async function beginAdventure() {
  if (S.busy || !UI.staging) return;
  S.busy = true;
  try {
    const st = UI.staging;
    const body = {};
    if (S.adventureId) body.adventure_id = S.adventureId;
    if (st.source === "veterans" && st.saveId) {
      body.party_save_id = st.saveId;
    } else {
      if (!st.builderId) {
        // The staged builder never materialized (or the server restarted and
        // forgot it) — roll it again rather than silently shipping a party
        // the player never saw.
        S.busy = false;
        await stagePremadeBuilder();
        S.busy = true;
        if (!UI.staging || !UI.staging.builderId) throw new Error("The party could not be rolled.");
        showScreen("staging");
        return;
      }
      body.party_builder_id = st.builderId;
    }
    const payload = await api("", body);
    const arrival = st.source === "veterans" ? "The party arrives" : "A new party arrives";
    enterGame(payload, (town) => `${arrival} in ${town}.`);
    toast("The adventure begins.", true);
  } catch (error) {
    toast(error.message, false);
  } finally {
    S.busy = false;
  }
}

/* --- the save browser: legible, managed, and only about restoring --- */

async function openSavesBrowser() {
  try {
    await fetchLibrary();
  } catch (error) {
    toast(error.message, false);
  }
  UI.renamingSave = null;
  showScreen("saves");
}

function renderSavesBrowser(card) {
  card.classList.add("wizard");
  const rows = S.saves.map((save) => {
    const renaming = UI.renamingSave === save.save_id;
    const label = renaming
      ? `<input class="save-name-input" data-save="${esc(save.save_id)}" maxlength="60" value="${esc(save.name)}" aria-label="Save name">`
      : `<span class="save-name">${esc(save.name)}</span>`;
    return `
    <div class="save-row" data-save="${esc(save.save_id)}">
      <div class="save-label">
        ${label}
        <div class="save-meta">${esc(rosterLine(save.party))}</div>
        <div class="save-meta faint">${esc(save.place)} · turn ${save.turn} · ${esc(saveDate(save.saved_at))} · <span class="save-code">${esc(save.save_id)}</span></div>
      </div>
      <div class="save-actions">
        <button class="btn accent" data-act="restore" type="button">Restore</button>
        <button class="btn ghost" data-act="rename" type="button">Rename</button>
        <button class="btn danger" data-act="delete" type="button">Delete</button>
      </div>
    </div>`;
  }).join("");
  card.innerHTML = `
    <div class="overlay-eyebrow">saved games</div>
    <h2 class="overlay-title wiz-title">Load game</h2>
    <div class="save-list">${rows || '<div class="wiz-note">No saved games yet.</div>'}</div>
    <div class="overlay-actions">
      <button class="btn ghost big push" id="sv-back" type="button">Back</button>
      ${S.saves.length ? '<button class="btn danger big" id="sv-clear" type="button">Delete all</button>' : ""}
    </div>`;
  $("sv-back").addEventListener("click", goBack);
  const clear = $("sv-clear");
  if (clear) clear.addEventListener("click", async () => {
    const count = S.saves.length;
    const ok = await confirmDialog(
      `Delete all ${count} saved game${count === 1 ? "" : "s"}? This cannot be undone.`,
      "Delete all"
    );
    if (!ok) return;
    try {
      // The response carries the list as it stands afterwards, so a save that
      // refused to delete stays visible instead of vanishing from the screen.
      const payload = await apiFetch("/saves", undefined, "DELETE");
      S.saves = payload.saves;
      UI.renamingSave = null;
      renderSavesBrowser(card);
    } catch (error) {
      toast(error.message, false);
    }
  });
  for (const row of card.querySelectorAll(".save-row")) {
    const saveId = row.dataset.save;
    const save = S.saves.find((s) => s.save_id === saveId);
    row.querySelector('[data-act="restore"]').addEventListener("click", () => restoreSave(saveId));
    row.querySelector('[data-act="rename"]').addEventListener("click", () => {
      UI.renamingSave = saveId;
      renderSavesBrowser(card);
      const input = card.querySelector(".save-name-input");
      if (input) { input.focus(); input.select(); }
    });
    row.querySelector('[data-act="delete"]').addEventListener("click", async () => {
      if (!await confirmDialog(`Delete "${save.name}"? This cannot be undone.`, "Delete")) return;
      try {
        await apiFetch(`/saves/${saveId}`, undefined, "DELETE");
        S.saves = S.saves.filter((s) => s.save_id !== saveId);
        renderSavesBrowser(card);
      } catch (error) {
        toast(error.message, false);
      }
    });
  }
  const input = card.querySelector(".save-name-input");
  if (input) {
    const commit = async () => {
      const save = S.saves.find((s) => s.save_id === input.dataset.save);
      const name = input.value.trim();
      UI.renamingSave = null;
      if (!save || !name || name === save.name) {
        renderSavesBrowser(card);
        return;
      }
      try {
        const result = await apiFetch(`/saves/${save.save_id}`, { name }, "PATCH");
        save.name = result.name;
      } catch (error) {
        toast(error.message, false);
      }
      renderSavesBrowser(card);
    };
    input.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Enter") input.blur();
      else if (event.key === "Escape") {
        UI.renamingSave = null;
        renderSavesBrowser(card);
      }
    });
    input.addEventListener("blur", commit);
  }
}

async function restoreSave(saveId) {
  if (S.busy) return;
  S.busy = true;
  try {
    const payload = await api("", { save_id: saveId });
    enterGame(payload, null);
    toast("Game restored.", true);
  } catch (error) {
    toast(error.message, false);
  } finally {
    S.busy = false;
  }
}

/* --- the menu: every road back, only one of them destructive --- */

function openMenu() {
  if (!S.view) {
    openTitle();
    return;
  }
  showScreen("menu");
}

function renderMenu(card) {
  card.innerHTML = `
    <div class="overlay-eyebrow">the menu</div>
    <h2 class="overlay-title menu-title">${esc(S.view.adventure_name)}</h2>
    <div class="menu-actions">
      <button class="btn accent big" id="mn-return" type="button">Return to game</button>
      <button class="btn big" id="mn-save" type="button">Save game</button>
      <button class="btn big" id="mn-load" type="button">Load game</button>
      <button class="btn big" id="mn-new" type="button">New adventure</button>
      <button class="btn danger big" id="mn-abandon" type="button">Abandon adventure</button>
    </div>
    <div class="overlay-note">Esc returns to the game. Nothing here touches your adventure until you abandon it.</div>`;
  $("mn-return").addEventListener("click", () => showScreen("game"));
  $("mn-save").addEventListener("click", saveGame);
  $("mn-load").addEventListener("click", () => { UI.origin = "game"; openSavesBrowser(); });
  $("mn-new").addEventListener("click", () => { UI.origin = "game"; openLibrary(); });
  $("mn-abandon").addEventListener("click", async () => {
    const ok = await confirmDialog(
      "Abandon this adventure? The session is discarded — anything not saved is gone.",
      "Abandon adventure"
    );
    if (!ok) return;
    abandonGame();
  });
}

async function abandonGame() {
  const gameId = S.gameId;
  clearGameState();
  if (gameId) api(`/${gameId}`, undefined, "DELETE").catch(() => {});
  await openTitle();
}

function clearGameState() {
  stopNarrationPoll();
  stopPacing();
  closeSheet();
  toggleMap(false);
  toggleJournal(false);
  S.gameId = null;
  S.view = null;
  S.decl = {};
  S.declPicked = {};
  localStorage.removeItem("osrweb_game");
  resetLog();
}

/* --- the endings: both terminal modes, one mold --- */

/* Same adventure, fresh party: straight to staging with a new builder. Shared
   by both ending screens — two copies of the library fallback would drift. */
async function rollNewParty() {
  UI.origin = "title";
  UI.staging = null;
  if (!S.adventures) {
    try {
      await fetchLibrary();
    } catch { /* staging still works; the server default stands in */ }
  }
  if (!S.adventureId) S.adventureId = adventureIdByName(S.view.adventure_name);
  UI.libSel = S.adventureId;
  await openStaging();
}

/* --- game over: the tale ends, the door stays open --- */

function renderGameOver(card) {
  card.innerHTML = `
    <div class="overlay-eyebrow">the tale ends</div>
    <h2 class="overlay-title">Darkness takes<br>the party</h2>
    <div class="overlay-sub">the party has fallen — but ${esc(S.view.town_name)} still waits for characters</div>
    <div class="overlay-actions">
      <button class="btn accent big" id="ov-again" type="button">Roll a new party</button>
      <button class="btn big" id="ov-door" type="button">Return to the title</button>
    </div>`;
  $("ov-again").addEventListener("click", rollNewParty);
  $("ov-door").addEventListener("click", openTitle);
}

/* --- victory: the tale is told, and the party may carry on --- */

/* The ending's one sentence about the haul, never a per-member breakdown:
   commerce is over the moment victory fires (treasure XP was awarded on the
   homecoming arrival), so what is carried is the retirement purse. The coin is
   `partyGold()` — party-wide, the fallen's packs included, which is also the
   engine's own reading of who carries what — and the treasure is summed the way
   the town's sell card sums it. */
function victoryFortune() {
  const coin = partyGold();
  const valuables = S.view.party.flatMap((m) => m.inventory.valuables);
  const worth = valuables.reduce((sum, v) => sum + (v.value_gp || 0), 0);
  if (!coin && !valuables.length) {
    return { text: "The party carries home nothing but the story.", quiet: true };
  }
  const haul = valuables.length
    ? ` and ${valuables.length} treasure${valuables.length === 1 ? "" : "s"} worth ${worth.toLocaleString()} gp`
    : "";
  return { text: `The party carries home ${coin.toLocaleString()} gp in coin${haul}.`, quiet: false };
}

function renderVictory(card) {
  const fortune = victoryFortune();
  const rows = S.view.party.map((m) => {
    const dead = m.conditions.includes("dead") || m.current_hp <= 0;
    return `
      <div class="roster-row${dead ? " dead" : ""}">
        <span class="r-name">${dead ? "†" : ""}${esc(m.name)}</span>
        <span class="r-class">${esc(CLASS_NAMES[m.class_id] || m.class_id)} ${m.level}</span>
      </div>`;
  }).join("");
  card.innerHTML = `
    <div class="overlay-eyebrow">the adventure is won</div>
    <h2 class="overlay-title">The tale is told</h2>
    <div class="overlay-sub">${esc(S.view.adventure_name)} is done — ${esc(S.view.town_name)} will tell of it for a generation</div>
    <div class="victory-fortune${fortune.quiet ? " quiet" : ""}">${esc(fortune.text)}</div>
    <div class="roster">${rows}</div>
    <div class="overlay-actions">
      <button class="btn accent big" id="vt-again" type="button">Roll a new party</button>
      <button class="btn big" id="vt-save" type="button">Save the party</button>
      <button class="btn big" id="vt-door" type="button">Return to the title</button>
    </div>`;
  // The record, below the exits on purpose: the journal grows with the
  // adventure's length, and the ways out stay reachable without scrolling past
  // it. `J` cannot summon the overlay from under `#overlay`, so this is the
  // only place the whole tale is readable once the game is won. An empty
  // journal omits the block entirely, label included.
  const entries = buildJournalEntries();
  if (entries.length) {
    const label = document.createElement("div");
    label.className = "victory-journal-label";
    label.textContent = "The journal";
    card.appendChild(label);
    const record = document.createElement("div");
    record.className = "victory-journal";
    for (const row of entries) record.appendChild(row);
    card.appendChild(record);
  }
  $("vt-again").addEventListener("click", rollNewParty);
  // The only route to a save once victory is up: the topbar is unreachable
  // under the overlay and Esc is the floor, so without it the one party that
  // finishes an adventure alive is the one the veterans flow can never import.
  $("vt-save").addEventListener("click", saveGame);
  $("vt-door").addEventListener("click", openTitle);
}

/* --- entering a game: the one place a session becomes "the" session --- */

function enterGame(payload, arrivalLine) {
  const oldId = S.gameId;
  absorb(payload, true);
  S.adventureId = adventureIdByName(S.view.adventure_name);
  closeSheet();
  toggleMap(false);
  toggleJournal(false);
  stopPacing();
  S.decl = {};
  S.declPicked = {};
  S.town = { buyer: null, templeWho: null, templeService: null, prep: {}, study: {}, shopOpen: { gear: true } };
  S.explore = { caster: null, spell: null, target: null };
  S.builder = null;
  UI.staging = null;
  localStorage.setItem("osrweb_game", S.gameId);
  resetLog();
  if (arrivalLine) appendLog([{ kind: "system", text: arrivalLine(S.view.town_name) }]);
  appendLog(payload.log);
  // Screen first, then render — the order is load-bearing. `render()`'s
  // terminal-mode trigger only fires while `UI.screen === "game"`, and a
  // restore arrives here from "saves" or "title": rendering first would leave
  // a restored `victory`/`game_over` session on the live game screen with an
  // empty action bar and a keyboard handler that returns. Both calls are
  // synchronous, so there is no intermediate paint.
  showScreen("game");
  render();
  // The replaced session is done for: free it server-side (fire and forget).
  if (oldId && oldId !== S.gameId) api(`/${oldId}`, undefined, "DELETE").catch(() => {});
}

async function saveGame() {
  if (!S.gameId) return;
  try {
    const result = await api(`/${S.gameId}/save`, {});
    toast(`Saved — ${result.name}.`, true);
    appendLog([{ kind: "system", text: `Game saved — ${result.name}.` }]);
  } catch (error) {
    toast(error.message, false);
  }
}

/* ---------- party creation wizard ---------- */
/* One character at a time through the SRD's steps: roll ability scores in
   order (3d6, or 4d6-drop-lowest under the house rule), choose a class the
   dice allow, trade points into prime requisites, inscribe the spell book,
   roll hp and gold, buy gear, then name the character. The server owns every
   rule; this is only the stepping. */

const ABILITIES = ["str", "int", "wis", "dex", "con", "cha"];
const ABILITY_NAMES = {
  str: "Strength", int: "Intelligence", wis: "Wisdom",
  dex: "Dexterity", con: "Constitution", cha: "Charisma",
};
const ALIGNMENT_NAMES = { lawful: "Lawful", neutral: "Neutral", chaotic: "Chaotic" };
const ARMOUR_WORDS = { any: "any armour", leather_only: "leather only", none: "no armour" };

function fmtMod(n) { return n >= 0 ? `+${n}` : `${n}`; }

function abilityHint(ability, mods) {
  if (ability === "str") return `melee ${fmtMod(mods.melee)}, open doors ${mods.open_doors}-in-6`;
  if (ability === "int") return `${mods.literacy}, ${mods.additional_languages} extra language${mods.additional_languages === 1 ? "" : "s"}`;
  if (ability === "wis") return `magic saves ${fmtMod(mods.magic_saves)}`;
  if (ability === "dex") return `AC ${fmtMod(mods.ac)}, missile ${fmtMod(mods.missile)}`;
  if (ability === "con") return `hp ${fmtMod(mods.hit_points)} per die`;
  return `reactions ${fmtMod(mods.npc_reactions)}`;
}

function builderClass() {
  const draft = S.builder.state.draft;
  if (!draft || !draft.class_id) return null;
  return S.catalog.classes.find((c) => c.id === draft.class_id) || null;
}

async function startBuilder() {
  try {
    await ensureCatalog();
    const payload = await apiFetch("/party-builders", { house_rules: storedHouseRules() });
    S.builder = {
      id: payload.builder_id, state: payload, screen: "roster",
      pending: null, spellPick: null, langPicks: [], name: "", alignment: "lawful",
    };
    renderBuilder();
  } catch (error) {
    toast(error.message, false);
  }
}

async function builderStep(action, params, nextScreen) {
  if (S.busy) return null;
  S.busy = true;
  try {
    const payload = await apiFetch(`/party-builders/${S.builder.id}/step`, { action, ...params });
    S.builder.state = payload;
    if (payload.accepted && nextScreen) S.builder.screen = nextScreen;
    if (!payload.accepted) toast(payload.rejections.map((r) => r.message).join(" "), false);
    renderBuilder();
    return payload;
  } catch (error) {
    toast(error.message, false);
    return null;
  } finally {
    S.busy = false;
  }
}

function wizGo(screen) {
  S.builder.screen = screen;
  renderBuilder();
}

function exitBuilder() {
  S.builder = null;
  showScreen("staging");
}

function clearDraftScratch() {
  S.builder.pending = null;
  S.builder.spellPick = null;
  S.builder.langPicks = [];
  S.builder.name = "";
  S.builder.alignment = "lawful";
}

async function scrapDraft() {
  if (!await confirmDialog("Discard this character and start over?", "Discard")) return;
  clearDraftScratch();
  await builderStep("discard", null, "roster");
}

function nextAfterAdjust() {
  const cls = builderClass();
  return cls && cls.arcane ? "spells" : "hp";
}

function renderBuilder() {
  if (!S.builder) return;
  const overlay = $("overlay");
  const card = $("overlay-card");
  const scroll = overlay.scrollTop;
  card.classList.add("wizard");
  overlay.classList.remove("hidden");
  // Every screen but the roster reads the draft; with none, only the roster can render.
  if (!S.builder.state.draft && S.builder.screen !== "roster") S.builder.screen = "roster";
  const screen = S.builder.screen;
  if (screen === "roster") renderWizRoster(card);
  else if (screen === "abilities") renderWizAbilities(card);
  else if (screen === "class") renderWizClass(card);
  else if (screen === "adjust") renderWizAdjust(card);
  else if (screen === "spells") renderWizSpells(card);
  else if (screen === "hp") renderWizHp(card);
  else if (screen === "gold") renderWizGold(card);
  else if (screen === "shop") renderWizShop(card);
  else if (screen === "finalize") renderWizFinalize(card);
  overlay.scrollTop = scroll;
}

function wizChrome(title, sub) {
  const st = S.builder.state;
  const count = st.members.length;
  return `
    <div class="overlay-eyebrow">roll up your party — ${count} of ${st.max_members} characters</div>
    <h2 class="overlay-title wiz-title">${esc(title)}</h2>
    ${sub ? `<div class="overlay-sub wiz-sub">${esc(sub)}</div>` : ""}`;
}

function wizScrapButton() {
  return '<button class="btn danger big push" id="wiz-scrap" type="button">Discard character</button>';
}

function wireScrap() {
  const btn = $("wiz-scrap");
  if (btn) btn.addEventListener("click", scrapDraft);
}

/* --- house rules: optional table rules, set before the first character --- */

function houseRulesPanel(rules) {
  const r = rules || { ability_method: "3d6", max_hp: false, locked: false };
  const disabled = r.locked ? "disabled" : "";
  const toggle = (id, on, label, hint) => `
    <label class="hr-toggle${r.locked ? " locked" : ""}">
      <input type="checkbox" id="${id}" ${on ? "checked" : ""} ${disabled}>
      <span class="hr-text"><span class="hr-label">${label}</span>
        <span class="hr-hint">${hint}</span></span>
    </label>`;
  return `
    <div class="wiz-panel house-rules">
      <div class="hr-head">House rules</div>
      ${toggle("hr-4d6", r.ability_method === "4d6_drop_lowest",
        "4d6, drop the lowest", "roll four dice per ability, keep the best three")}
      ${toggle("hr-maxhp", r.max_hp,
        "Maximum starting hit points", "take the top of the hit die instead of rolling")}
      <div class="wiz-note">${r.locked
        ? "Locked in — house rules are set before the first character is rolled."
        : "Optional, and set for the whole party."}</div>
    </div>`;
}

function wireHouseRules() {
  // If the step is dropped (another in flight) or errors, builderStep returns
  // null without re-rendering — snap the checkbox back to the server's truth.
  // An accepted change also updates the global preference.
  const method = $("hr-4d6");
  if (method && !method.disabled) {
    method.addEventListener("change", async () => {
      const value = method.checked ? "4d6_drop_lowest" : "3d6";
      const ok = await builderStep("house_rules", { ability_method: value });
      if (!ok) renderBuilder();
      else if (ok.accepted) saveHouseRules({ ...storedHouseRules(), ability_method: value });
    });
  }
  const maxHp = $("hr-maxhp");
  if (maxHp && !maxHp.disabled) {
    maxHp.addEventListener("change", async () => {
      const value = maxHp.checked;
      const ok = await builderStep("house_rules", { max_hp: value });
      if (!ok) renderBuilder();
      else if (ok.accepted) saveHouseRules({ ...storedHouseRules(), max_hp: value });
    });
  }
}

/* --- roster: the party so far --- */

function renderWizRoster(card) {
  const st = S.builder.state;
  const adventure = (S.adventures || []).find((a) => a.id === S.adventureId);
  const rows = st.members.map((m, i) => `
    <div class="roster-row wiz-roster-row">
      <span class="r-name">${esc(m.name)}</span>
      <span class="r-class">${esc(m.class_name)} ${m.level}</span>
      <span class="r-hp">${m.max_hp} hp · AC ${m.armour_class}</span>
      <button class="btn danger tiny wiz-remove" data-index="${i}" type="button" title="Remove ${esc(m.name)} from the party">×</button>
    </div>`).join("");
  const draftNote = st.draft
    ? `<div class="wiz-note">A character is in progress.
         <button class="btn tiny" id="wiz-resume" type="button">Continue</button>
         <button class="btn danger tiny" id="wiz-scrap" type="button">Discard</button></div>`
    : "";
  card.innerHTML = `
    ${wizChrome("The party", adventure ? `playing ${adventure.name}` : "")}
    ${houseRulesPanel(st.house_rules)}
    <div class="wiz-panel">
      ${rows || '<div class="wiz-note">No characters yet.</div>'}
      ${draftNote}
    </div>
    <div class="overlay-actions">
      <button class="btn ghost big push" id="wiz-back" type="button">Back to the party</button>
      ${!st.draft && st.members.length < st.max_members
        ? `<button class="btn big${st.members.length ? "" : " accent"}" id="wiz-roll" type="button">Roll a character</button>` : ""}
      ${st.members.length
        ? '<button class="btn accent big" id="wiz-begin" type="button">Begin the adventure</button>' : ""}
    </div>`;
  wireHouseRules();
  const roll = $("wiz-roll");
  if (roll) roll.addEventListener("click", () => {
    clearDraftScratch();
    builderStep("roll_abilities", null, "abilities");
  });
  const begin = $("wiz-begin");
  if (begin) begin.addEventListener("click", beginBuilderGame);
  $("wiz-back").addEventListener("click", exitBuilder);
  const resume = $("wiz-resume");
  if (resume) resume.addEventListener("click", () => {
    const stage = st.draft.stage;
    wizGo(stage === "class" ? "class" : stage === "hit_points" ? "adjust" : stage === "gold" ? "gold" : "shop");
  });
  wireScrap();
  for (const btn of card.querySelectorAll(".wiz-remove")) {
    btn.addEventListener("click", async () => {
      const index = Number(btn.dataset.index);
      if (await confirmDialog(`Remove ${st.members[index].name} from the party?`, "Remove")) {
        builderStep("remove_member", { index });
      }
    });
  }
}

/* --- step 1: the ability rolls --- */

function renderWizAbilities(card) {
  const draft = S.builder.state.draft;
  const fourD6 = S.builder.state.house_rules.ability_method === "4d6_drop_lowest";
  const rows = ABILITIES.map((a) => {
    const roll = draft.rolls[a];
    const dice = roll.dropped !== undefined
      ? `${roll.dice.join(" + ")} <span class="die-dropped">(${roll.dropped})</span>`
      : roll.dice.join(" + ");
    return `
    <tr>
      <td class="a-name">${ABILITY_NAMES[a]}</td>
      <td class="dice">${dice}</td>
      <td class="score">${roll.total}</td>
      <td class="mod-hint">${esc(abilityHint(a, draft.modifiers))}</td>
    </tr>`;
  }).join("");
  const method = fourD6
    ? "4d6, drop the lowest — STR, INT, WIS, DEX, CON, CHA"
    : "3d6 down the line — STR, INT, WIS, DEX, CON, CHA";
  card.innerHTML = `
    ${wizChrome("Ability scores", method)}
    <div class="wiz-panel">
      <table class="ability-table"><tbody>${rows}</tbody></table>
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn big" id="wiz-reroll" type="button">Reroll abilities</button>
      <button class="btn accent big" id="wiz-next" type="button">Continue</button>
    </div>`;
  $("wiz-next").addEventListener("click", () => wizGo("class"));
  $("wiz-reroll").addEventListener("click", async () => {
    if (!await builderStep("discard", null, "roster")) return;
    await builderStep("roll_abilities", null, "abilities");
  });
  wireScrap();
}

/* --- step 2: the class --- */

function renderWizClass(card) {
  const draft = S.builder.state.draft;
  const cards = S.catalog.classes.map((cls) => {
    const unmet = Object.entries(cls.requirements)
      .filter(([ability, minimum]) => draft.rolls[ability].total < minimum);
    const reqText = Object.entries(cls.requirements)
      .map(([ability, minimum]) => `${ability.toUpperCase()} ${minimum}`).join(", ");
    const meta = [
      `d${cls.hit_die} hit die`,
      `prime ${cls.prime_requisites.map((a) => a.toUpperCase()).join(" & ")}`,
      ARMOUR_WORDS[cls.armour.kind] || cls.armour.kind,
      cls.arcane ? "spell book" : null,
    ].filter(Boolean).join(" · ");
    return `
      <button class="class-card" data-class="${cls.id}" type="button" ${unmet.length ? "disabled" : ""}>
        <span class="c-name">${esc(cls.name)}</span>
        ${cls.title ? `<span class="c-title">${esc(cls.title)}</span>` : ""}
        <span class="c-meta">${esc(meta)}</span>
        ${reqText ? `<span class="c-meta ${unmet.length ? "c-req" : ""}">needs ${reqText}</span>` : ""}
      </button>`;
  }).join("");
  card.innerHTML = `
    ${wizChrome("Choose a class", "your rolls determine which classes are available")}
    <div class="class-grid">${cards}</div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn ghost big" id="wiz-prev" type="button">Back to the rolls</button>
    </div>`;
  for (const btn of card.querySelectorAll(".class-card")) {
    btn.addEventListener("click", () => {
      S.builder.pending = null;
      S.builder.spellPick = null;
      S.builder.langPicks = [];
      builderStep("choose_class", { class_id: btn.dataset.class }, "adjust");
    });
  }
  $("wiz-prev").addEventListener("click", () => wizGo("abilities"));
  wireScrap();
}

/* --- step 3: the optional adjustment --- */

function pendingAdjust() {
  if (!S.builder.pending) {
    const stored = S.builder.state.draft.adjustment;
    S.builder.pending = {
      lowered: { ...(stored ? stored.lowered : {}) },
      raised: { ...(stored ? stored.raised : {}) },
    };
  }
  return S.builder.pending;
}

function renderWizAdjust(card) {
  const draft = S.builder.state.draft;
  const cls = builderClass();
  const pending = pendingAdjust();
  const lowerable = ABILITIES.filter((a) =>
    ["str", "int", "wis"].includes(a) &&
    !cls.prime_requisites.includes(a) &&
    !cls.may_not_lower.includes(a));
  const totalLowered = Object.values(pending.lowered).reduce((s, n) => s + n, 0);
  const totalRaised = Object.values(pending.raised).reduce((s, n) => s + n, 0);
  const points = Math.floor(totalLowered / 2) - totalRaised;
  const rows = ABILITIES.map((a) => {
    const base = draft.rolls[a].total;
    const delta = (pending.raised[a] || 0) - (pending.lowered[a] || 0);
    const now = base + delta;
    const prime = cls.prime_requisites.includes(a);
    const controls = [];
    if (lowerable.includes(a)) {
      controls.push(`<button class="btn tiny wiz-adj" data-ability="${a}" data-op="lower" type="button"
        ${now - 2 < 9 ? "disabled" : ""}>−2</button>`);
      if (pending.lowered[a]) controls.push(`<button class="btn tiny wiz-adj" data-ability="${a}" data-op="unlower" type="button">undo</button>`);
    }
    if (prime) {
      controls.push(`<button class="btn tiny wiz-adj" data-ability="${a}" data-op="raise" type="button"
        ${points <= 0 || now >= 18 ? "disabled" : ""}>+1</button>`);
      if (pending.raised[a]) controls.push(`<button class="btn tiny wiz-adj" data-ability="${a}" data-op="unraise" type="button">undo</button>`);
    }
    return `
      <tr>
        <td class="a-name">${ABILITY_NAMES[a]}${prime ? ' <span class="prime-tag">prime</span>' : ""}</td>
        <td class="score">${now}</td>
        <td class="dice">${delta ? `(${base} ${delta > 0 ? "+" : "−"} ${Math.abs(delta)})` : ""}</td>
        <td class="adj-controls">${controls.join(" ")}</td>
      </tr>`;
  }).join("");
  card.innerHTML = `
    ${wizChrome("Adjust ability scores", "lower STR, INT, or WIS two points to raise a prime requisite one — never below 9, never above 18")}
    <div class="wiz-panel">
      <table class="ability-table"><tbody>${rows}</tbody></table>
      <div class="wiz-note points-line">${points > 0
        ? `${points} point${points === 1 ? "" : "s"} still to place`
        : points < 0 ? "too many points placed" : "balanced"}
        · XP bonus now ${fmtMod(draft.xp_modifier_pct)}%</div>
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn ghost big" id="wiz-prev" type="button">Back to classes</button>
      <button class="btn accent big" id="wiz-apply" type="button" ${points !== 0 ? "disabled" : ""}>Continue</button>
    </div>`;
  for (const btn of card.querySelectorAll(".wiz-adj")) {
    btn.addEventListener("click", () => {
      const ability = btn.dataset.ability, op = btn.dataset.op;
      if (op === "lower") pending.lowered[ability] = (pending.lowered[ability] || 0) + 2;
      else if (op === "unlower") delete pending.lowered[ability];
      else if (op === "raise") pending.raised[ability] = (pending.raised[ability] || 0) + 1;
      else if (op === "unraise") delete pending.raised[ability];
      // Dropping a reduction may strand raises; trim them to the points left.
      let budget = Math.floor(Object.values(pending.lowered).reduce((s, n) => s + n, 0) / 2);
      for (const key of Object.keys(pending.raised)) {
        pending.raised[key] = Math.min(pending.raised[key], budget);
        budget -= pending.raised[key];
        if (!pending.raised[key]) delete pending.raised[key];
      }
      renderBuilder();
    });
  }
  $("wiz-apply").addEventListener("click", () => {
    builderStep("adjust", { lowered: pending.lowered, raised: pending.raised }, nextAfterAdjust());
  });
  $("wiz-prev").addEventListener("click", () => wizGo("class"));
  wireScrap();
}

/* --- step 4: the arcane spell book --- */

function renderWizSpells(card) {
  const draft = S.builder.state.draft;
  const cls = builderClass();
  if (S.builder.spellPick === null && draft.spell_ids.length) S.builder.spellPick = draft.spell_ids[0];
  const options = S.catalog.spells
    .filter((spell) => spell.spell_list === cls.spell_list)
    .map((spell) => `
      <label class="spell-row" title="${esc(spell.intro)}">
        <input type="radio" name="wiz-spell" value="${spell.id}" ${S.builder.spellPick === spell.id ? "checked" : ""}>
        <span class="s-name">${esc(spell.name)}</span>
        <span class="s-intro">${esc(spell.intro.length > 110 ? spell.intro.slice(0, 107) + "…" : spell.intro)}</span>
      </label>`).join("");
  card.innerHTML = `
    ${wizChrome("Choose a spell", `a beginning ${cls.name.toLowerCase()} knows exactly ${cls.spell_slots_level_1} spell${cls.spell_slots_level_1 === 1 ? "" : "s"}`)}
    <div class="wiz-panel spell-list">${options}</div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn ghost big" id="wiz-prev" type="button">Back</button>
      <button class="btn accent big" id="wiz-next" type="button" ${S.builder.spellPick ? "" : "disabled"}>Continue</button>
    </div>`;
  for (const input of card.querySelectorAll('input[name="wiz-spell"]')) {
    input.addEventListener("change", () => {
      S.builder.spellPick = input.value;
      $("wiz-next").disabled = false;
    });
  }
  $("wiz-next").addEventListener("click", () => {
    builderStep("choose_spells", { spell_ids: [S.builder.spellPick] }, "hp");
  });
  $("wiz-prev").addEventListener("click", () => wizGo("adjust"));
  wireScrap();
}

/* --- steps 5 and 6: the hp and gold rolls --- */

function renderWizHp(card) {
  const draft = S.builder.state.draft;
  const cls = builderClass();
  const mod = draft.modifiers.hit_points;
  const result = draft.hit_points;
  const maxHp = S.builder.state.house_rules.max_hp;
  const sub = maxHp
    ? `d${cls.hit_die} maximum${mod ? ` ${fmtMod(mod)} for Constitution` : ""}, no fewer than 1`
    : `1d${cls.hit_die}${mod ? ` ${fmtMod(mod)} for Constitution` : ""}, no fewer than 1`;
  const doneNote = maxHp
    ? `maximum die face ${result ? result.rolls[result.rolls.length - 1] : cls.hit_die}${mod ? `, ${fmtMod(mod)}` : ""}`
    : `the die showed ${result ? result.rolls.join(", then ") : ""}${mod ? `, ${fmtMod(mod)}` : ""}${result && result.total === 1 && result.rolls[result.rolls.length - 1] + mod < 1 ? " — never below 1" : ""}`;
  card.innerHTML = `
    ${wizChrome("Hit points", sub)}
    <div class="wiz-panel wiz-roll-panel">
      ${result
        ? `<div class="big-roll">${result.total}</div>
           <div class="wiz-note">${doneNote}</div>`
        : `<div class="wiz-note">${maxHp ? "No roll needed." : "Not rolled yet."}</div>`}
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      ${result
        ? '<button class="btn accent big" id="wiz-next" type="button">Continue</button>'
        : `<button class="btn accent big" id="wiz-roll" type="button">${maxHp ? "Take maximum hit points" : `Roll 1d${cls.hit_die}`}</button>`}
    </div>`;
  const roll = $("wiz-roll");
  if (roll) roll.addEventListener("click", () => builderStep("roll_hit_points"));
  const next = $("wiz-next");
  if (next) next.addEventListener("click", () => wizGo("gold"));
  wireScrap();
}

function renderWizGold(card) {
  const draft = S.builder.state.draft;
  const result = draft.gold;
  card.innerHTML = `
    ${wizChrome("Roll starting gold", "3d6 × 10 gold pieces for starting equipment")}
    <div class="wiz-panel wiz-roll-panel">
      ${result
        ? `<div class="big-roll">${result.total} gp</div>
           <div class="wiz-note">the dice showed ${result.dice.join(" + ")} — × 10</div>`
        : '<div class="wiz-note">Not rolled yet.</div>'}
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      ${result
        ? '<button class="btn accent big" id="wiz-next" type="button">Continue</button>'
        : '<button class="btn accent big" id="wiz-roll" type="button">Roll 3d6 × 10</button>'}
    </div>`;
  const roll = $("wiz-roll");
  if (roll) roll.addEventListener("click", () => builderStep("roll_gold"));
  const next = $("wiz-next");
  if (next) next.addEventListener("click", () => wizGo("shop"));
  wireScrap();
}

/* --- step 7: the provisioner --- */

function classCanUse(cls, item) {
  if (item.kind === "weapon") {
    if (cls.weapons.kind === "allowed") return cls.weapons.weapon_ids.includes(item.id);
    if (cls.weapons.kind === "forbidden") return !cls.weapons.weapon_ids.includes(item.id);
    return true;
  }
  if (item.kind === "armour") {
    if (item.shield) return cls.armour.shields;
    if (cls.armour.kind === "none") return false;
    if (cls.armour.kind === "leather_only") return item.id === "leather";
    return true;
  }
  return true;
}

function itemDetail(item) {
  if (item.kind === "weapon") {
    const notes = [];
    if (item.qualities.includes("two_handed")) notes.push("two-handed");
    if (item.qualities.includes("missile")) notes.push(item.qualities.includes("melee") ? "thrown" : "missile");
    return `${item.damage}${notes.length ? " · " + notes.join(", ") : ""}`;
  }
  if (item.kind === "armour") return item.shield ? "AC +1" : `AC ${item.ac}`;
  return "";
}

function renderWizShop(card) {
  const draft = S.builder.state.draft;
  const cls = builderClass();
  const purse = draft.purse_gp;
  const groups = [["weapon", "Weapons"], ["armour", "Armour"], ["gear", "Gear"], ["ammunition", "Ammunition"]];
  const wares = groups.map(([kind, label]) => {
    const rows = S.catalog.equipment
      .filter((item) => item.kind === kind && classCanUse(cls, item))
      .map((item) => `
        <tr>
          <td>${esc(item.name)}</td>
          <td class="dice">${esc(itemDetail(item))}</td>
          <td class="s-cost">${item.cost_gp} gp</td>
          <td><button class="btn tiny wiz-buy" data-item="${item.id}" type="button"
            ${item.cost_gp > purse ? "disabled" : ""}>Buy</button></td>
        </tr>`).join("");
    return rows ? `<tr><td colspan="4" class="shop-kind">${label}</td></tr>${rows}` : "";
  }).join("");
  const pack = draft.purchases.map((p) => `
    <tr>
      <td>${esc(p.name)}${p.quantity > p.lots ? ` ×${p.quantity}` : p.lots > 1 ? ` ×${p.lots}` : ""}</td>
      <td class="s-cost">${p.cost_gp} gp</td>
      <td><button class="btn ghost tiny wiz-return" data-index="${p.index}" type="button">Return</button></td>
    </tr>`).join("");
  const owned = new Set(draft.purchases.map((p) => p.item_id));
  const kit = (S.catalog.kits || []).find((k) => k.class_id === cls.id);
  let kitPanel = "";
  if (kit) {
    const summary = kit.items
      .map((it) => it.lots > 1 ? `${it.name} ×${it.lots}` : it.name).join(" · ");
    const dupes = kit.items.filter((it) => owned.has(it.item_id));
    const dupeNote = dupes.length
      ? `<div class="kit-dupe">Already in your pack: ${esc(dupes.map((d) => d.name).join(", "))} — buying the kit adds another.</div>`
      : "";
    kitPanel = `
    <div class="wiz-panel kit-panel">
      <div class="shop-kind">Class kit — optional</div>
      <div class="kit-grid">
        <div class="kit-card">
          <div class="kit-head">
            <span class="kit-name">${esc(kit.name)}</span>
            <span class="s-cost">${kit.cost_gp} gp</span>
          </div>
          <div class="kit-desc">${esc(kit.description)}</div>
          <div class="kit-items">${esc(summary)}</div>
          ${dupeNote}
          <button class="btn tiny wiz-kit" data-kit="${kit.id}" type="button"
            ${kit.cost_gp > purse ? "disabled" : ""}>Buy the kit</button>
        </div>
      </div>
      <div class="wiz-note">The kit drops its gear straight into the pack; return any piece you don't want.</div>
    </div>`;
  }
  card.innerHTML = `
    ${wizChrome("The provisioner", "spend the starting gold; unspent gold stays in the purse")}
    ${kitPanel}
    <div class="shop-cols">
      <div class="wiz-panel shop-pane">
        <table class="shop-table"><tbody>${wares}</tbody></table>
      </div>
      <div class="wiz-panel shop-pane">
        <div class="purse-line">${purse} gp remain</div>
        <table class="shop-table"><tbody>${pack || '<tr><td class="wiz-note">Nothing bought yet.</td></tr>'}</tbody></table>
        <div class="wiz-note">The best armour and every usable weapon are readied automatically.</div>
      </div>
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn accent big" id="wiz-next" type="button">Continue</button>
    </div>`;
  for (const btn of card.querySelectorAll(".wiz-kit")) {
    btn.addEventListener("click", () => builderStep("buy_kit", { kit_id: btn.dataset.kit }));
  }
  for (const btn of card.querySelectorAll(".wiz-buy")) {
    btn.addEventListener("click", () => builderStep("buy", { item_id: btn.dataset.item, lots: 1 }));
  }
  for (const btn of card.querySelectorAll(".wiz-return")) {
    btn.addEventListener("click", () => builderStep("return", { index: Number(btn.dataset.index) }));
  }
  $("wiz-next").addEventListener("click", () => wizGo("finalize"));
  wireScrap();
}

/* --- step 8: name, alignment, languages --- */

function renderWizFinalize(card) {
  const draft = S.builder.state.draft;
  const cls = builderClass();
  const slots = draft.modifiers.additional_languages;
  if (!S.builder.langPicks.length && draft.language_ids.length) S.builder.langPicks = [...draft.language_ids];
  const scoreLine = ABILITIES.map((a) => `${a.toUpperCase()} ${draft.scores[a]}`).join(" · ");
  const gear = draft.purchases.map((p) => p.quantity > p.lots ? `${p.name} ×${p.quantity}` : p.name).join(", ");
  const spell = draft.spell_ids.length
    ? (S.catalog.spells.find((s) => s.id === draft.spell_ids[0]) || { name: draft.spell_ids[0] }).name
    : null;
  const languages = slots > 0 ? `
    <div class="wiz-field">
      <span class="f-label">Languages (${ABILITY_NAMES.int} grants ${slots})</span>
      <div class="lang-grid">${S.catalog.languages.map((lang) => `
        <label class="lang-pick"><input type="checkbox" value="${lang.id}"
          ${S.builder.langPicks.includes(lang.id) ? "checked" : ""}> ${esc(lang.name)}</label>`).join("")}
      </div>
    </div>` : "";
  card.innerHTML = `
    ${wizChrome("Name the character", `${cls.name} — ${draft.hit_points.total} hp — ${draft.purse_gp} gp in the purse`)}
    <div class="wiz-panel">
      <div class="wiz-field">
        <span class="f-label">Name</span>
        <input class="wiz-input" id="wiz-name" maxlength="40" value="${esc(S.builder.name)}" placeholder="Character name">
      </div>
      <div class="wiz-field">
        <span class="f-label">Alignment</span>
        <select id="wiz-alignment">${(S.catalog.alignments).map((a) =>
          `<option value="${a}">${ALIGNMENT_NAMES[a] || a}</option>`).join("")}</select>
      </div>
      ${languages}
      <div class="wiz-note sheet-line">${scoreLine}</div>
      ${spell ? `<div class="wiz-note sheet-line">Spell book: ${esc(spell)}</div>` : ""}
      <div class="wiz-note sheet-line">${gear ? `Pack: ${esc(gear)}` : "The pack is empty."}</div>
    </div>
    <div class="overlay-actions">
      ${wizScrapButton()}
      <button class="btn ghost big" id="wiz-prev" type="button">Back to the provisioner</button>
      <button class="btn accent big" id="wiz-done" type="button">Add to the party</button>
    </div>`;
  const nameInput = $("wiz-name");
  nameInput.addEventListener("input", () => { S.builder.name = nameInput.value; });
  const alignSelect = $("wiz-alignment");
  alignSelect.value = S.builder.alignment;
  alignSelect.addEventListener("change", () => { S.builder.alignment = alignSelect.value; });
  for (const input of card.querySelectorAll(".lang-pick input")) {
    input.addEventListener("change", () => {
      const picks = new Set(S.builder.langPicks);
      if (input.checked) picks.add(input.value); else picks.delete(input.value);
      if (picks.size > slots) {
        input.checked = false;
        picks.delete(input.value);
        toast(`Only ${slots} extra language${slots === 1 ? "" : "s"}.`, false);
      }
      S.builder.langPicks = [...picks];
    });
  }
  $("wiz-done").addEventListener("click", async () => {
    if (!S.builder.name.trim()) {
      toast("Enter a name.", false);
      nameInput.focus();
      return;
    }
    const picks = S.builder.langPicks;
    if (picks.join() !== draft.language_ids.join()) {
      const result = await builderStep("choose_languages", { language_ids: picks });
      if (!result || !result.accepted) return;
    }
    const payload = await builderStep("finalize", { name: S.builder.name.trim(), alignment: S.builder.alignment }, "roster");
    if (payload && payload.accepted) clearDraftScratch();
  });
  $("wiz-prev").addEventListener("click", () => wizGo("shop"));
  wireScrap();
}

/* --- setting out --- */

async function beginBuilderGame() {
  if (S.busy) return;
  S.busy = true;
  try {
    const body = { party_builder_id: S.builder.id };
    if (S.adventureId) body.adventure_id = S.adventureId;
    const payload = await api("", body);
    enterGame(payload, (town) => `Your party arrives in ${town}.`);
    toast("The adventure begins.", true);
  } catch (error) {
    toast(error.message, false);
  } finally {
    S.busy = false;
  }
}

/* ---------- boot ---------- */
/* A live session in localStorage lands straight in the game — the fast path.
   Anything else lands on the front door with nothing created: no session
   exists until the player walks through "Begin the adventure".
   A dead session id (the server restarted) is cleared, and the door's
   Continue button offers the newest save instead of a silent new game. */

async function boot() {
  const existing = localStorage.getItem("osrweb_game");
  if (existing) {
    try {
      const payload = await api(`/${existing}`);
      absorb(payload, true);
      S.gameId = existing;
      resetLog();
      appendLog(payload.log);
      render();
      if (!TERMINAL_MODES.has(S.view.mode)) showScreen("game");
      return;
    } catch {
      localStorage.removeItem("osrweb_game"); // stale id — the door will offer the newest save
    }
  }
  await openTitle();
}

$("btn-menu").addEventListener("click", openMenu);
$("btn-save").addEventListener("click", saveGame);
// Always rendered, always enabled, in every mode: hiding it until the journal
// had content would shift Save and Menu the first time a beat landed, and an
// empty journal says so in words.
$("btn-journal").addEventListener("click", () => toggleJournal());

document.addEventListener("keydown", (event) => {
  if (confirmResolve) return;
  if (event.target.tagName === "SELECT" || event.target.tagName === "INPUT") return;
  const key = event.key.toLowerCase();

  if (!$("overlay").classList.contains("hidden")) {
    // A front-door screen is up. Esc walks back one step; the wizard keeps
    // its own explicit buttons (a stray Esc must not scrap a half-made
    // character), and the title and the two ending screens are the floor.
    if (key === "escape" && !S.builder) {
      event.preventDefault();
      goBack();
    }
    return;
  }
  if (!S.view) return;

  if (key === "escape") {
    if (S.sheetOpen) { closeSheet(); event.preventDefault(); }
    else if (JOURNAL.open) { toggleJournal(false); event.preventDefault(); }
    else if (MAP.open) { toggleMap(false); event.preventDefault(); }
    else { openMenu(); event.preventDefault(); }
    return;
  }
  if (S.sheetOpen) {
    if (key === "arrowleft") { cycleSheet(-1); event.preventDefault(); }
    else if (key === "arrowright") { cycleSheet(1); event.preventDefault(); }
    return;
  }
  if (key === "m" && mapAvailable()) {
    toggleMap();
    event.preventDefault();
    return;
  }
  // No mode gate: the record is readable in town, in an encounter, in battle,
  // and while exploring — the deliberate widening of the map's dungeon-only rule.
  if (key === "j") {
    toggleJournal();
    event.preventDefault();
    return;
  }
  // The scrim swallows clicks, so only the keyboard can reach the game behind
  // an open overlay. Without the JOURNAL clause, `W` walks the party blind.
  if (MAP.open || JOURNAL.open || S.pacing) return;

  const mode = S.view.mode;
  if (mode !== "exploring") return;
  const map = {
    arrowup: () => forward(), w: () => forward(),
    arrowleft: () => turn(LEFT), a: () => turn(LEFT),
    arrowright: () => turn(RIGHT), d: () => turn(RIGHT),
    arrowdown: () => turn(OPPOSITE), s: () => turn(OPPOSITE),
    o: () => command({ command_type: "open_door", direction: S.view.location.facing }),
    c: () => command({ command_type: "close_door", direction: S.view.location.facing }),
    f: () => { const m = memberByClass("fighter", "dwarf"); if (m) command({ command_type: "force_door", direction: S.view.location.facing, character_id: m.id }); },
    g: () => { const m = memberByClass("thief"); if (m) command({ command_type: "listen_at_door", direction: S.view.location.facing, character_id: m.id }); },
    t: () => { if (S.cell && (S.cell.features.length || S.cell.pile)) command({ command_type: "take_treasure", feature_id: S.cell.features.length ? S.cell.features[0].id : "pile" }); },
    i: () => {
      // Same gating as the Inspect button: a feature (never the bare pile), a
      // living thief, and their one attempt still unspent.
      const feature = S.cell && S.cell.features.length ? S.cell.features[0] : null;
      if (!feature) return;
      const thief = livingMembers().find((m) => m.class_id === "thief");
      if (!thief || (feature.inspected_by || []).includes(thief.id)) return;
      command({ command_type: "inspect_treasure", character_id: thief.id, feature_id: feature.id });
    },
    l: () => { const b = torchBearer(); if (b && !lightIsBurning()) command({ command_type: "light_source", character_id: b.id, item_id: "torch" }); },
    r: () => command({ command_type: "rest", kind: "turn" }),
  };
  if (map[key]) {
    event.preventDefault();
    map[key]();
  }
});

boot();
