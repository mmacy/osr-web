"""Roll-your-own party: the stepwise B/X character creation flow behind the wizard.

A [`PartyBuilder`][server.creation.PartyBuilder] walks the OSE SRD's Creating a
Character steps one decision at a time, driving the same osrlib creation
functions `create_character` batches for the pregenerated roster: roll ability
scores in order, choose a class the scores allow, optionally trade points into
prime requisites, inscribe an arcane spell book, roll hit points and starting
gold, buy equipment, then name the character and pick an alignment. Every step
returns structured rejections instead of raising, so the client can surface the
rule that was broken. Finished characters accumulate up to ten; the builder
hands the session a deep copy of the party so an abandoned builder never shares
mutable state with a running game.

Optional [`HouseRules`][server.creation.HouseRules], set before the first
character is rolled, bend two of those draws: 4d6-drop-lowest ability scores in
place of 3d6, and maximum first-level hit points in place of a roll.

Creation draws come from the builder's own RNG streams, forked from a seed that
belongs to the builder alone and drawn in `create_character`'s fixed order per
character: six ability scores (each 3d6, or 4d6 under the house rule), hit points
(skipped when hit points are maximised), then gold. A caller may supply that seed
to make a wizard run reproducible — the same seed driven through the same steps
repeats every draw — and it is never a game session's master seed, never derived
from one, and never returned in a payload; the session's master seed stays
server-side, as it always has.
"""

import secrets
import threading
from dataclasses import dataclass, field

from osrlib.core.abilities import AbilityAdjustment, AbilityScore, validate_adjustment
from osrlib.core.abilities import apply_adjustment as apply_ability_adjustment
from osrlib.core.alignment import Alignment
from osrlib.core.character import (
    ABILITY_ROLL_ORDER,
    CHARACTER_CREATION_STREAM,
    AbilityScoreRolls,
    Character,
    HitPointRoll,
    roll_ability_scores,
    roll_hit_points,
    roll_starting_gold,
    validate_class_choice,
    validate_extra_languages,
    validate_starting_spells,
)
from osrlib.core.classes import ClassDefinition, xp_modifier_pct
from osrlib.core.dice import RollResult, roll
from osrlib.core.items import (
    ArmourTemplate,
    Inventory,
    ItemInstance,
    WeaponTemplate,
    equip,
    purchase,
    validate_equip,
    validate_purchase,
)
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.core.spells import caster_profile
from osrlib.core.validation import Rejection
from osrlib.crawl.party import Party
from osrlib.data import (
    load_ability_tables,
    load_classes,
    load_equipment,
    load_languages,
    load_spells,
)

MAX_PARTY = 10
"""The most characters one builder can field — B/X tables seat six to eight; ten is the roof."""

SEED_BOUND = 1 << 128
"""Exclusive upper bound on a builder seed — osrlib's `RngStreams` takes a 128-bit master seed."""

_MAX_NAME_LENGTH = 40

ABILITY_METHODS = ("3d6", "4d6_drop_lowest")
"""The ability-score roll methods the builder offers: 3d6 straight, or the 4d6-drop-lowest house rule."""

EQUIPMENT_KITS = (
    {
        "id": "cleric",
        "class_id": "cleric",
        "name": "Cleric's kit",
        "description": "Mace and mail, the holy symbol, and provisions for the crypt.",
        "items": (
            ("mace", 1),
            ("chainmail", 1),
            ("shield", 1),
            ("holy_symbol", 1),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
    {
        "id": "dwarf",
        "class_id": "dwarf",
        "name": "Dwarf's kit",
        "description": "Axe, mail, and the spikes and hammer of a born tunneler.",
        "items": (
            ("battle_axe", 1),
            ("chainmail", 1),
            ("shield", 1),
            ("hammer", 1),
            ("iron_spikes", 1),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
    {
        "id": "elf",
        "class_id": "elf",
        "name": "Elf's kit",
        "description": "Sword and bow over mail — steel for the melee, arrows for the reach.",
        "items": (
            ("sword", 1),
            ("chainmail", 1),
            ("short_bow", 1),
            ("arrows", 1),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
    {
        "id": "fighter",
        "class_id": "fighter",
        "name": "Fighter's kit",
        "description": "Sword, mail, and shield — the front line, provisioned.",
        "items": (
            ("sword", 1),
            ("chainmail", 1),
            ("shield", 1),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
            ("rope", 1),
        ),
    },
    {
        "id": "halfling",
        "class_id": "halfling",
        "name": "Halfling's kit",
        "description": "Short sword, leather, and a sling — light gear for light feet.",
        "items": (
            ("short_sword", 1),
            ("leather", 1),
            ("shield", 1),
            ("sling", 1),
            ("sling_stones", 20),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
    {
        "id": "magic_user",
        "class_id": "magic_user",
        "name": "Magic-user's kit",
        "description": "A dagger, a lantern, and oil — light to read by, little to carry.",
        "items": (
            ("dagger", 1),
            ("lantern", 1),
            ("oil_flask", 2),
            ("backpack", 1),
            ("tinder_box", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
    {
        "id": "thief",
        "class_id": "thief",
        "name": "Thief's kit",
        "description": "Leather, blades, rope, and the tools of the trade.",
        "items": (
            ("short_sword", 1),
            ("dagger", 1),
            ("leather", 1),
            ("thieves_tools", 1),
            ("backpack", 1),
            ("torch", 1),
            ("tinder_box", 1),
            ("rope", 1),
            ("rations_standard", 1),
            ("waterskin", 1),
        ),
    },
)
"""Curated starting equipment, one kit per class — weapons, armour, and dungeoneering gear.

Every item is legal for the kit's class, so a kit may carry arms and armour the
generic shop sells piecemeal. A draft may buy only its own class's kit, and each
kit is bought all-or-nothing against the purse via
[`PartyBuilder.buy_kit`][server.creation.PartyBuilder.buy_kit].
"""

_KITS_BY_ID = {kit["id"]: kit for kit in EQUIPMENT_KITS}

_REJECTION_TEXT = {
    # Builder-owned codes.
    "creation.party.full": "The party is full — {max} is the most.",
    "creation.party.empty": "The party needs at least one character.",
    "creation.draft.in_progress": "A character is already half-rolled — finish or scrap them first.",
    "creation.step.out_of_order": "First things first — {needs} comes before this.",
    "creation.step.locked": "That decision is already locked in.",
    "creation.step.already_done": "Those dice are already cast.",
    "creation.class.unknown": "No such class.",
    "creation.item.unknown": "The provisioner stocks no such thing.",
    "creation.kit.unknown": "The provisioner packs no such kit.",
    "creation.kit.wrong_class": "The {kit} is fitted for a {class}.",
    "creation.kit.unaffordable": "Not enough gold for the {kit}.",
    "creation.purchase.unknown_index": "No such purchase to return.",
    "creation.purchase.bad_quantity": "Buy at least one, and no more than {max} at a time.",
    "creation.member.unknown_index": "No such member.",
    "creation.name.invalid": "Every character needs a name (at most {max} characters).",
    "creation.alignment.unknown": "Choose lawful, neutral, or chaotic.",
    "creation.adjustment.malformed": "Adjustments trade whole, positive points.",
    "creation.house_rules.locked": "House rules lock once the first character is rolled.",
    "creation.house_rules.unknown_method": "Unknown ability-score method.",
    # osrlib creation codes.
    "creation.class.requirements_not_met": "A {class} needs {ability} {minimum} — these dice rolled {score}.",
    "creation.adjustment.not_lowerable": "Only STR, INT, and WIS can be lowered.",
    "creation.adjustment.prime_requisite_lowered": "{ability} is a prime requisite — it cannot be lowered.",
    "creation.adjustment.class_restriction": "This class may not lower {ability}.",
    "creation.adjustment.reduction_not_even": "Scores are lowered two points at a time.",
    "creation.adjustment.below_floor": "No score may drop below 9.",
    "creation.adjustment.raise_not_prime_requisite": "Only a prime requisite can be raised.",
    "creation.adjustment.above_cap": "No score may rise above 18.",
    "creation.adjustment.points_mismatch": (
        "Two points lowered buy one point raised — {points_available} to spend, {points_spent} spent."
    ),
    "creation.languages.too_many": "INT grants only {allowed} extra language(s).",
    "creation.languages.duplicate_choice": "{language} is chosen twice.",
    "creation.languages.not_available": "{language} cannot be learned.",
    "creation.languages.duplicates_native": "This class already speaks {language}.",
    "magic.book.not_arcane": "Only arcane casters keep spell books.",
    "magic.book.duplicate": "{spell} is inscribed twice.",
    "magic.book.unknown_spell": "No such spell.",
    "magic.book.wrong_list": "{spell} is not on this class's spell list.",
    "magic.book.capacity_mismatch": "The book holds exactly {capacity} level-{spell_level} spell(s), not {chosen}.",
    "items.purchase.insufficient_funds": "Not enough gold.",
}


def _param_text(key: str, value) -> str:
    text = str(value)
    if key == "ability":
        return text.upper()
    if key == "class":
        try:
            return load_classes().get(text).name
        except ValueError:
            return text
    if key in ("language", "spell", "item"):
        return text.replace("_", " ").capitalize()
    return text


def creation_rejection_text(rejection: Rejection) -> str:
    """One creation rejection as a friendly line, params interpolated."""
    template = _REJECTION_TEXT.get(rejection.code)
    if template is None:
        return rejection.code
    params = {key: _param_text(key, value) for key, value in rejection.params.items()}
    try:
        return template.format(**params)
    except KeyError, IndexError:
        return rejection.code


def creation_catalog() -> dict:
    """Reference data for the creation wizard: classes, spells, languages, wares.

    Everything here is SRD data the player's own rulebook would show — no
    referee state. Weapon and armour policies ride along so the client can
    hide wares a class cannot use.
    """
    classes = []
    arcane_lists: set[str] = set()
    for definition in load_classes().classes:
        profile = caster_profile(definition)
        spell_list = profile.spell_list if profile is not None and profile.kind == "arcane" else None
        if spell_list is not None:
            arcane_lists.add(spell_list)
        slots = definition.row(1).spell_slots
        classes.append(
            {
                "id": definition.id,
                "name": definition.name,
                "title": definition.level_titles[0] if definition.level_titles else None,
                "hit_die": definition.hit_die,
                "requirements": {ability.value: minimum for ability, minimum in definition.requirements.items()},
                "prime_requisites": [a.value for a in definition.prime_requisites],
                "may_not_lower": [a.value for a in definition.may_not_lower],
                "armour": {
                    "kind": definition.armour.kind.value,
                    "shields": definition.armour.shields_allowed,
                },
                "weapons": {
                    "kind": definition.weapons.kind.value,
                    "weapon_ids": list(definition.weapons.weapon_ids),
                },
                "arcane": spell_list is not None,
                "spell_list": spell_list,
                "spell_slots_level_1": slots[0] if slots else 0,
            }
        )
    spells = [
        {
            "id": spell.id,
            "name": spell.name,
            "spell_list": spell.spell_list,
            "intro": spell.intro,
        }
        for spell in load_spells().spells
        if spell.level == 1 and spell.spell_list in arcane_lists
    ]
    languages = [
        {"id": language.id, "name": language.name} for language in load_languages().languages if language.choosable
    ]
    equipment = []
    catalog = load_equipment()
    for template in catalog.weapons:
        equipment.append(
            {
                "id": template.id,
                "name": template.name,
                "cost_gp": template.cost_gp,
                "kind": "weapon",
                "damage": template.damage,
                "qualities": [quality.value for quality in template.qualities],
            }
        )
    for template in catalog.armour:
        equipment.append(
            {
                "id": template.id,
                "name": template.name,
                "cost_gp": template.cost_gp,
                "kind": "armour",
                "ac": template.ac,
                "ac_bonus": template.ac_bonus,
                "shield": template.is_shield,
            }
        )
    for template in catalog.gear:
        equipment.append(
            {
                "id": template.id,
                "name": template.name,
                "cost_gp": template.cost_gp,
                "kind": "gear",
                "lot_size": template.lot_size,
            }
        )
    for template in catalog.ammunition:
        equipment.append(
            {
                "id": template.id,
                "name": template.name,
                "cost_gp": template.cost_gp,
                "kind": "ammunition",
                "lot_size": template.lot_size,
            }
        )
    kits = []
    for kit in EQUIPMENT_KITS:
        items = []
        total_gp = 0
        for item_id, lots in kit["items"]:
            template = catalog.get(item_id)
            lot_size = getattr(template, "lot_size", 1)
            items.append(
                {
                    "item_id": item_id,
                    "name": template.name,
                    "lots": lots,
                    "quantity": lot_size * lots,
                    "cost_gp": template.cost_gp * lots,
                }
            )
            total_gp += template.cost_gp * lots
        kits.append(
            {
                "id": kit["id"],
                "class_id": kit["class_id"],
                "name": kit["name"],
                "description": kit["description"],
                "items": items,
                "cost_gp": total_gp,
            }
        )
    return {
        "max_members": MAX_PARTY,
        "ability_methods": list(ABILITY_METHODS),
        "alignments": [alignment.value for alignment in Alignment],
        "classes": classes,
        "spells": spells,
        "languages": languages,
        "equipment": equipment,
        "kits": kits,
    }


@dataclass
class HouseRules:
    """The optional table rules a builder plays under, chosen before the first roll.

    Both default to the by-the-book B/X rules, so a builder left untouched rolls
    exactly as [`create_character`][osrlib.core.character.create_character] does.
    """

    ability_method: str = "3d6"
    """How ability scores are rolled — `"3d6"` straight, or `"4d6_drop_lowest"`."""

    max_hp: bool = False
    """When true, first-level hit points are the maximum die result plus the CON modifier."""


@dataclass
class Draft:
    """One character mid-creation: the rolls made and the choices taken so far."""

    ability_rolls: AbilityScoreRolls
    class_id: str | None = None
    adjustment: AbilityAdjustment | None = None
    spell_ids: tuple[str, ...] = ()
    language_ids: tuple[str, ...] = ()
    hit_point_roll: HitPointRoll | None = None
    gold_roll: RollResult | None = None
    purchases: list[tuple[str, int]] = field(default_factory=list)
    ability_dropped: dict[AbilityScore, int] | None = None
    """The die dropped from each ability under 4d6-drop-lowest, for display; `None` for 3d6."""


def _reject(code: str, **params) -> list[Rejection]:
    return [Rejection(code=code, params=params)]


def roll_abilities_4d6(stream) -> tuple[AbilityScoreRolls, dict[AbilityScore, int]]:
    """Roll 4d6 and keep the highest three per ability, down the SRD's order.

    The kept three dice are stored the same way a 3d6 roll would be, so the rest
    of creation reads the scores identically; the dropped die is returned
    separately for the wizard to show.

    Args:
        stream: The RNG stream to draw from.

    Returns:
        The kept-dice roll set and the die dropped from each ability.
    """
    scores: dict[AbilityScore, int] = {}
    kept: dict[AbilityScore, tuple[int, int, int]] = {}
    dropped: dict[AbilityScore, int] = {}
    for ability in ABILITY_ROLL_ORDER:
        dice = sorted(roll("4d6", stream).rolls)
        dropped[ability] = dice[0]
        top_three = (dice[1], dice[2], dice[3])
        kept[ability] = top_three
        scores[ability] = sum(top_three)
    return AbilityScoreRolls(scores=scores, rolls=kept), dropped


class PartyBuilder:
    """A party being rolled one character at a time, SRD step by SRD step.

    Steps mutate a single [`Draft`][server.creation.Draft]; `finalize` turns it
    into a [`Character`][osrlib.core.character.Character] and appends it to
    `members`. Every step method returns structured rejections (empty means
    accepted) and never raises for a bad player choice. The step order is
    enforced: abilities → class → (adjust) → (spells) → hit points → gold →
    (shop) → finalize, with class and adjustment locked once hit points are
    rolled, because the hit die and CON modifier are spent.

    Every draw comes from one stream forked off the builder's own seed, so a
    caller who supplies a seed can replay a whole wizard run.
    """

    def __init__(self, seed: int | None = None):
        """Open an empty builder, optionally on a caller-supplied seed.

        Args:
            seed: The builder's own master seed, in `[0, SEED_BOUND)`. Left
                `None` the builder takes a fresh random seed, which is the
                by-the-book behaviour; supplied, the same seed driven through
                the same sequence of steps repeats every draw exactly. This seed
                is the builder's alone — it is never a game session's master
                seed, is never derived from one, and never rides on a response.

        Raises:
            ValueError: If `seed` is outside `[0, SEED_BOUND)`.
        """
        self.ruleset = Ruleset()
        self.streams = RngStreams(master_seed=secrets.randbits(63) if seed is None else seed)
        self.stream = self.streams.get(CHARACTER_CREATION_STREAM)
        self.house_rules = HouseRules()
        self.members: list[Character] = []
        self.draft: Draft | None = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ steps

    def set_house_rules(self, ability_method=None, max_hp=None) -> list[Rejection]:
        """Set the optional table rules — 4d6-drop-lowest and maximum first-level HP.

        Only settable while the party is empty and no draft is in progress, so
        the whole party is rolled under one set of rules. Either argument left
        `None` keeps its current value.
        """
        if self.members or self.draft is not None:
            return _reject("creation.house_rules.locked")
        # Validate everything before touching state, so a bad method never
        # silently drops a valid max_hp sent in the same request.
        method = None
        if ability_method is not None:
            method = str(ability_method)
            if method not in ABILITY_METHODS:
                return _reject("creation.house_rules.unknown_method")
        if method is not None:
            self.house_rules.ability_method = method
        if max_hp is not None:
            self.house_rules.max_hp = bool(max_hp)
        return []

    def fill_premade(self, ability_method=None, max_hp=None) -> list[Rejection]:
        """Roll the pregenerated six into the roster, replacing whatever was there.

        This is the staging screen's premade party: the same roster path the
        direct `POST /api/games` premade route uses (`content.premade_members`),
        rolled on the builder's own stream so a seeded builder reproduces the
        set. Calling it again is the reroll; either house-rule argument may ride
        along (the whole set rerolls under the new rules, which is why the
        usual house-rules lock doesn't apply — no already-rolled member
        survives the change). A hand-rolled draft in progress blocks the fill
        so the wizard's half-made character can't be silently clobbered.
        """
        if self.draft is not None:
            return _reject("creation.draft.in_progress")
        method = None
        if ability_method is not None:
            method = str(ability_method)
            if method not in ABILITY_METHODS:
                return _reject("creation.house_rules.unknown_method")
        if method is not None:
            self.house_rules.ability_method = method
        if max_hp is not None:
            self.house_rules.max_hp = bool(max_hp)
        from .content import premade_members  # circular at module scope

        self.members = premade_members(self.stream, self.ruleset, self.house_rules)
        return []

    def rename_member(self, index, name) -> list[Rejection]:
        """Rename a finished member — the staging screen's inline rename."""
        try:
            index = int(index)
        except ValueError, TypeError:
            return _reject("creation.member.unknown_index")
        if not 0 <= index < len(self.members):
            return _reject("creation.member.unknown_index")
        name = str(name or "").strip()
        if not name or len(name) > _MAX_NAME_LENGTH:
            return _reject("creation.name.invalid", max=_MAX_NAME_LENGTH)
        self.members[index].name = name
        return []

    def roll_abilities(self) -> list[Rejection]:
        """Start a new character: six scores down the line, STR INT WIS DEX CON CHA."""
        if len(self.members) >= MAX_PARTY:
            return _reject("creation.party.full", max=MAX_PARTY)
        if self.draft is not None:
            return _reject("creation.draft.in_progress")
        if self.house_rules.ability_method == "4d6_drop_lowest":
            rolls, dropped = roll_abilities_4d6(self.stream)
            self.draft = Draft(ability_rolls=rolls, ability_dropped=dropped)
        else:
            self.draft = Draft(ability_rolls=roll_ability_scores(self.stream))
        return []

    def discard_draft(self) -> list[Rejection]:
        """Scrap the character in progress — the SRD's mercy for hopeless rolls."""
        self.draft = None
        return []

    def choose_class(self, class_id) -> list[Rejection]:
        """Choose (or re-choose, until hit points are rolled) the draft's class."""
        draft = self.draft
        if draft is None:
            return _reject("creation.step.out_of_order", needs="rolling ability scores")
        if draft.hit_point_roll is not None:
            return _reject("creation.step.locked")
        try:
            definition = load_classes().get(str(class_id))
        except ValueError:
            return _reject("creation.class.unknown")
        rejections = validate_class_choice(draft.ability_rolls.scores, definition)
        if rejections:
            return rejections
        if draft.class_id != definition.id:
            draft.adjustment = None
            draft.spell_ids = ()
            draft.language_ids = ()
        draft.class_id = definition.id
        return []

    def adjust_scores(self, lowered, raised) -> list[Rejection]:
        """Apply (or replace) the optional adjustment: 2 points off STR/INT/WIS buy 1 on a prime requisite."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        if draft.hit_point_roll is not None:
            return _reject("creation.step.locked")
        parsed: list[dict[AbilityScore, int]] = []
        for amounts in (lowered, raised):
            entries: dict[AbilityScore, int] = {}
            for key, value in (amounts or {}).items():
                try:
                    ability = AbilityScore(str(key))
                    amount = int(value)
                except ValueError, TypeError:
                    return _reject("creation.adjustment.malformed")
                if amount < 0:
                    return _reject("creation.adjustment.malformed")
                if amount:
                    entries[ability] = amount
            parsed.append(entries)
        adjustment = AbilityAdjustment(lowered=parsed[0], raised=parsed[1])
        rejections = validate_adjustment(
            draft.ability_rolls.scores,
            adjustment,
            definition.prime_requisites,
            definition.may_not_lower,
        )
        if rejections:
            return rejections
        draft.adjustment = adjustment if (adjustment.lowered or adjustment.raised) else None
        return []

    def choose_spells(self, spell_ids) -> list[Rejection]:
        """Inscribe the arcane starting spell book (exactly the level-1 capacity)."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        ids = tuple(str(spell_id) for spell_id in (spell_ids or []))
        rejections = validate_starting_spells(definition, load_spells(), ids)
        if rejections:
            return rejections
        draft.spell_ids = ids
        return []

    def choose_languages(self, language_ids) -> list[Rejection]:
        """Pick the INT-granted extra languages (fewer than the allowance is fine)."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        ids = tuple(str(language_id) for language_id in (language_ids or []))
        scores = self._scores(draft, definition)
        rejections = validate_extra_languages(definition, scores[AbilityScore.INT], ids)
        if rejections:
            return rejections
        draft.language_ids = ids
        return []

    def roll_hp(self) -> list[Rejection]:
        """Roll first-level hit points: the class hit die plus the CON modifier, minimum 1.

        Under the maximum-HP house rule the die is not rolled — hit points are the
        die's top face plus the CON modifier (still floored at 1).
        """
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        if draft.hit_point_roll is not None:
            return _reject("creation.step.already_done")
        scores = self._scores(draft, definition)
        con_modifier = load_ability_tables().hit_point_modifier(scores[AbilityScore.CON])
        if self.house_rules.max_hp:
            die = definition.row(1).hit_dice.die
            draft.hit_point_roll = HitPointRoll(rolls=(die,), hit_points=max(1, die + con_modifier))
        else:
            draft.hit_point_roll = roll_hit_points(definition, con_modifier, self.ruleset, self.stream)
        return []

    def roll_gold(self) -> list[Rejection]:
        """Roll starting money: 3d6 × 10 gold pieces."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        if draft.hit_point_roll is None:
            return _reject("creation.step.out_of_order", needs="rolling hit points")
        if draft.gold_roll is not None:
            return _reject("creation.step.already_done")
        draft.gold_roll = roll_starting_gold(self.stream)
        return []

    def buy(self, item_id, lots) -> list[Rejection]:
        """Buy purchase lots of one item from the starting gold."""
        draft, rejections = self._shopping_draft()
        if draft is None:
            return rejections
        try:
            lots = int(lots)
        except ValueError, TypeError:
            return _reject("creation.purchase.bad_quantity", max=100)
        if not 1 <= lots <= 100:
            return _reject("creation.purchase.bad_quantity", max=100)
        try:
            template = load_equipment().get(str(item_id))
        except ValueError:
            return _reject("creation.item.unknown")
        inventory = self._inventory(draft)
        rejections = validate_purchase(inventory.purse, template, lots)
        if rejections:
            return rejections
        draft.purchases.append((template.id, lots))
        return []

    def return_purchase(self, index) -> list[Rejection]:
        """Put one purchase back on the shelf for a full refund — creation-time only."""
        draft, rejections = self._shopping_draft()
        if draft is None:
            return rejections
        try:
            index = int(index)
        except ValueError, TypeError:
            return _reject("creation.purchase.unknown_index")
        if not 0 <= index < len(draft.purchases):
            return _reject("creation.purchase.unknown_index")
        draft.purchases.pop(index)
        return []

    def buy_kit(self, kit_id) -> list[Rejection]:
        """Buy the class's whole equipment kit at once, all-or-nothing against the purse.

        Kits carry class-specific arms and armour, so only the draft's own
        class's kit may be bought. The kit is affordable only if every item
        fits the remaining gold in sequence; if any does not, nothing is bought
        and the kit is rejected. Kit items are ordinary purchases afterward,
        returnable one by one.
        """
        draft, rejections = self._shopping_draft()
        if draft is None:
            return rejections
        kit = _KITS_BY_ID.get(str(kit_id))
        if kit is None:
            return _reject("creation.kit.unknown")
        if kit["class_id"] != draft.class_id:
            return _reject(
                "creation.kit.wrong_class",
                kit=kit["name"],
                **{"class": kit["class_id"]},
            )
        equipment = load_equipment()
        inventory = self._inventory(draft)
        additions: list[tuple[str, int]] = []
        for item_id, lots in kit["items"]:
            template = equipment.get(item_id)
            if validate_purchase(inventory.purse, template, lots):
                return _reject("creation.kit.unaffordable", kit=kit["name"])
            purchase(inventory, template, lots)
            additions.append((item_id, lots))
        draft.purchases.extend(additions)
        return []

    def finalize(self, name, alignment) -> list[Rejection]:
        """Name the character, pick an alignment, and seat them in the party."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return rejections
        draft, definition = classed
        # Hit points are rolled before gold, so a draft missing either has not
        # reached the shop yet and owes the same step.
        hit_point_roll = draft.hit_point_roll
        if draft.gold_roll is None or hit_point_roll is None:
            return _reject("creation.step.out_of_order", needs="rolling starting gold")
        rejections = []
        name = str(name or "").strip()
        if not name or len(name) > _MAX_NAME_LENGTH:
            rejections += _reject("creation.name.invalid", max=_MAX_NAME_LENGTH)
        try:
            chosen_alignment = Alignment(str(alignment))
        except ValueError:
            chosen_alignment = None
            rejections += _reject("creation.alignment.unknown")
        profile = caster_profile(definition)
        if draft.spell_ids or (profile is not None and profile.kind == "arcane"):
            rejections += validate_starting_spells(definition, load_spells(), draft.spell_ids)
        scores = self._scores(draft, definition)
        rejections += validate_extra_languages(definition, scores[AbilityScore.INT], draft.language_ids)
        # An unparsed alignment is None only when its own rejection is in the list.
        if rejections or chosen_alignment is None:
            return rejections
        inventory = self._inventory(draft)
        self._auto_equip(inventory, definition)
        hit_points = hit_point_roll.hit_points
        self.members.append(
            Character(
                name=name,
                class_id=definition.id,
                race=definition.race,
                level=1,
                xp=0,
                scores=scores,
                alignment=chosen_alignment,
                extra_languages=draft.language_ids,
                max_hp=hit_points,
                current_hp=hit_points,
                inventory=inventory,
                spell_book=draft.spell_ids,
            )
        )
        self.draft = None
        return []

    def remove_member(self, index) -> list[Rejection]:
        """Send a finished character home before the adventure starts."""
        try:
            index = int(index)
        except ValueError, TypeError:
            return _reject("creation.member.unknown_index")
        if not 0 <= index < len(self.members):
            return _reject("creation.member.unknown_index")
        self.members.pop(index)
        return []

    def party(self) -> tuple[Party | None, list[Rejection]]:
        """The finished party as a deep copy, so the builder never shares state with a session."""
        if not self.members:
            return None, _reject("creation.party.empty")
        return Party(members=[member.model_copy(deep=True) for member in self.members]), []

    # ------------------------------------------------------------------ internals

    def _classed_draft(self) -> tuple[tuple[Draft, ClassDefinition] | None, list[Rejection]]:
        """The draft and its class, paired, or the rejection naming the step still owed.

        This is where the builder decides once whether there is anything to act
        on. A step that gets a pair has both, so every private helper below takes
        them as the plain values they are rather than re-testing for `None`.
        """
        draft = self.draft
        if draft is None:
            return None, _reject("creation.step.out_of_order", needs="rolling ability scores")
        if draft.class_id is None:
            return None, _reject("creation.step.out_of_order", needs="choosing a class")
        return (draft, load_classes().get(draft.class_id)), []

    def _shopping_draft(self) -> tuple[Draft | None, list[Rejection]]:
        """The draft once its gold is rolled, or the rejection naming the step still owed."""
        classed, rejections = self._classed_draft()
        if classed is None:
            return None, rejections
        draft, _ = classed
        if draft.gold_roll is None:
            return None, _reject("creation.step.out_of_order", needs="rolling starting gold")
        return draft, []

    def _scores(self, draft: Draft, definition: ClassDefinition) -> dict[AbilityScore, int]:
        scores = dict(draft.ability_rolls.scores)
        if draft.adjustment is not None:
            scores = apply_ability_adjustment(
                scores,
                draft.adjustment,
                definition.prime_requisites,
                definition.may_not_lower,
            )
        return scores

    def _inventory(self, draft: Draft) -> Inventory:
        """Replay the purchase list into a fresh inventory — returns are free this way.

        The purse holds the starting-gold roll. Every caller reaches this after the
        gold step, so a draft without a roll is a programming error, not a state.

        Raises:
            ValueError: If the draft has not rolled starting gold.
        """
        if draft.gold_roll is None:
            raise ValueError("the draft has not rolled starting gold")
        inventory = Inventory()
        inventory.purse.gp = draft.gold_roll.total
        equipment = load_equipment()
        for item_id, lots in draft.purchases:
            purchase(inventory, equipment.get(item_id), lots)
        return inventory

    def _auto_equip(self, inventory: Inventory, definition: ClassDefinition) -> None:
        """Wear the best armour bought, wield every legal weapon, then raise the shield.

        The shield goes on last so a two-handed arm wins the hands — the shield
        rides in the pack. Anything the class may not use simply stays packed,
        and so does every magic item: an inventory holds either kind, and a
        [`MagicItemInstance`][osrlib.core.items.MagicItemInstance] names its
        template by id rather than carrying one, so there is no template here to
        weigh it by.
        """
        body: list[tuple[int, ItemInstance]] = []
        weapons: list[ItemInstance] = []
        shields: list[ItemInstance] = []
        for instance in inventory.items:
            if not isinstance(instance, ItemInstance):
                continue
            template = instance.template
            if isinstance(template, WeaponTemplate):
                weapons.append(instance)
            elif isinstance(template, ArmourTemplate):
                if template.is_shield:
                    shields.append(instance)
                elif template.ac is not None:
                    body.append((template.ac, instance))
        armour = [instance for _, instance in sorted(body, key=lambda worn: worn[0])]
        for instance in (*armour[:1], *weapons, *shields[:1]):
            if not validate_equip(definition, instance, inventory):
                equip(inventory, definition, instance)

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        """The player-safe builder snapshot: the roster so far and the draft's progress."""
        return {
            "max_members": MAX_PARTY,
            "house_rules": {
                "ability_method": self.house_rules.ability_method,
                "max_hp": self.house_rules.max_hp,
                "locked": bool(self.members) or self.draft is not None,
            },
            "members": [self._member_state(member) for member in self.members],
            "draft": self._draft_state(),
        }

    def _member_state(self, member: Character) -> dict:
        definition = member.definition
        return {
            "name": member.name,
            "class_id": member.class_id,
            "class_name": definition.name,
            "level": member.level,
            "max_hp": member.max_hp,
            "armour_class": member.armour_class,
            "alignment": member.alignment.value,
            "scores": {ability.value: score for ability, score in member.scores.items()},
            "spell_ids": list(member.spell_book),
            "gold_gp": member.inventory.purse.gp,
        }

    def _draft_state(self) -> dict | None:
        draft = self.draft
        if draft is None:
            return None
        tables = load_ability_tables()
        definition = load_classes().get(draft.class_id) if draft.class_id else None
        scores = self._scores(draft, definition) if definition else dict(draft.ability_rolls.scores)
        if draft.class_id is None:
            stage = "class"
        elif draft.hit_point_roll is None:
            stage = "hit_points"
        elif draft.gold_roll is None:
            stage = "gold"
        else:
            stage = "shop"
        state = {
            "stage": stage,
            "rolls": {
                ability.value: {
                    "dice": list(dice),
                    "total": draft.ability_rolls.scores[ability],
                    **({"dropped": draft.ability_dropped[ability]} if draft.ability_dropped is not None else {}),
                }
                for ability, dice in draft.ability_rolls.rolls.items()
            },
            "scores": {ability.value: score for ability, score in scores.items()},
            "modifiers": {
                "melee": tables.melee_modifier(scores[AbilityScore.STR]),
                "open_doors": tables.open_doors_chance(scores[AbilityScore.STR]),
                "missile": tables.missile_modifier(scores[AbilityScore.DEX]),
                "ac": tables.ac_modifier(scores[AbilityScore.DEX]),
                "initiative": tables.initiative_modifier(scores[AbilityScore.DEX]),
                "hit_points": tables.hit_point_modifier(scores[AbilityScore.CON]),
                "magic_saves": tables.magic_save_modifier(scores[AbilityScore.WIS]),
                "npc_reactions": tables.npc_reaction_modifier(scores[AbilityScore.CHA]),
                "additional_languages": tables.additional_languages(scores[AbilityScore.INT]),
                "literacy": tables.literacy(scores[AbilityScore.INT]).value,
            },
            "class_id": draft.class_id,
            "class_name": definition.name if definition else None,
            "xp_modifier_pct": xp_modifier_pct(definition, scores) if definition else None,
            "adjustment": (
                {
                    "lowered": {a.value: n for a, n in draft.adjustment.lowered.items()},
                    "raised": {a.value: n for a, n in draft.adjustment.raised.items()},
                }
                if draft.adjustment is not None
                else None
            ),
            "arcane": False,
            "spell_ids": list(draft.spell_ids),
            "language_ids": list(draft.language_ids),
            "hit_points": (
                {
                    "rolls": list(draft.hit_point_roll.rolls),
                    "total": draft.hit_point_roll.hit_points,
                }
                if draft.hit_point_roll is not None
                else None
            ),
            "gold": (
                {"dice": list(draft.gold_roll.rolls), "total": draft.gold_roll.total}
                if draft.gold_roll is not None
                else None
            ),
            "purse_gp": None,
            "purchases": [],
        }
        if definition is not None:
            profile = caster_profile(definition)
            state["arcane"] = profile is not None and profile.kind == "arcane"
        if draft.gold_roll is not None:
            equipment = load_equipment()
            inventory = self._inventory(draft)
            state["purse_gp"] = inventory.purse.gp
            purchases = []
            for index, (item_id, lots) in enumerate(draft.purchases):
                template = equipment.get(item_id)
                lot_size = getattr(template, "lot_size", 1)
                purchases.append(
                    {
                        "index": index,
                        "item_id": item_id,
                        "name": template.name,
                        "lots": lots,
                        "quantity": lot_size * lots,
                        "cost_gp": template.cost_gp * lots,
                    }
                )
            state["purchases"] = purchases
        return state
