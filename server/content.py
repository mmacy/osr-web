"""Game content wiring: adventure documents and the party that walks them.

Adventures are stamped `adventure` documents produced by osr-forge; the library
of playable ones lives in [`server.library`][server.library]. A fresh party is
the classic B/X six: fighter, dwarf, cleric, thief, elf, and magic-user, each
with a tiered equipment kit so creation succeeds whatever the starting-gold
roll, and a retry loop for classes with ability requirements (an elf needs the
dice to cooperate). Creation drives osrlib's stepwise functions directly (in
`create_character`'s draw order) so the optional
[`HouseRules`][server.creation.HouseRules] — 4d6-drop-lowest ability scores and
maximum first-level hit points — bend the pregenerated party the same way they
bend the wizard's. A party can instead be carried over from any save: the
members keep their sheets, gear, and scars, and only session-owned state (timed
effects, stat modifiers) is stripped.
"""

import json
import os
import re
from pathlib import Path

from osrlib.core.abilities import AbilityScore
from osrlib.core.alignment import Alignment
from osrlib.core.character import (
    CHARACTER_CREATION_STREAM,
    Character,
    roll_ability_scores,
    roll_hit_points,
    roll_starting_gold,
    validate_class_choice,
)
from osrlib.core.classes import ClassDefinition
from osrlib.core.items import (
    Inventory,
    ItemInstance,
    MagicItemInstance,
    equip,
    purchase,
    validate_equip,
    validate_purchase,
)
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.core.spells import MemorizedSpell, memorize_spells
from osrlib.crawl.adventure import Adventure, validate_adventure
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.data import (
    load_ability_tables,
    load_classes,
    load_equipment,
    load_magic_items,
    load_monsters,
    load_spells,
)
from osrlib.persistence import load_game
from osrlib.versioning import check_document

from .creation import HouseRules, roll_abilities_4d6

_DEFAULT_ADVENTURE = Path(__file__).resolve().parent.parent / "content" / "adventure.json"

_ID_PATTERN = re.compile(r"^([a-z][a-z-]*)-(\d+)$")

# Each member: name, class, alignment, starting spells, and kit tiers from
# best to barest — the first tier the purse can afford wins.
_ROSTER = (
    (
        "Corwin",
        "fighter",
        Alignment.LAWFUL,
        (),
        (
            (
                ("sword", 1),
                ("chainmail", 1),
                ("shield", 1),
                ("torch", 2),
                ("tinder_box", 1),
                ("rations_standard", 1),
                ("waterskin", 1),
            ),
            (
                ("sword", 1),
                ("leather", 1),
                ("shield", 1),
                ("torch", 2),
                ("tinder_box", 1),
            ),
            (("spear", 1), ("leather", 1), ("torch", 1), ("tinder_box", 1)),
            (("spear", 1), ("torch", 1), ("tinder_box", 1)),
        ),
    ),
    (
        "Brunhild",
        "dwarf",
        Alignment.LAWFUL,
        (),
        (
            (
                ("battle_axe", 1),
                ("chainmail", 1),
                ("shield", 1),
                ("hammer", 1),
                ("iron_spikes", 1),
                ("torch", 1),
            ),
            (("battle_axe", 1), ("leather", 1), ("shield", 1), ("iron_spikes", 1)),
            (("hand_axe", 1), ("leather", 1), ("iron_spikes", 1)),
            (("hand_axe", 1), ("iron_spikes", 1)),
        ),
    ),
    (
        "Meriel",
        "cleric",
        Alignment.LAWFUL,
        (),
        (
            (
                ("mace", 1),
                ("chainmail", 1),
                ("shield", 1),
                ("holy_symbol", 1),
                ("torch", 1),
            ),
            (("mace", 1), ("leather", 1), ("holy_symbol", 1), ("torch", 1)),
            (("club", 1), ("leather", 1), ("holy_symbol", 1)),
            (("club", 1), ("holy_symbol", 1)),
        ),
    ),
    (
        "Fox",
        "thief",
        Alignment.NEUTRAL,
        (),
        (
            (
                ("short_sword", 1),
                ("leather", 1),
                ("thieves_tools", 1),
                ("torch", 1),
                ("tinder_box", 1),
            ),
            (("dagger", 1), ("leather", 1), ("thieves_tools", 1)),
            (("dagger", 1), ("thieves_tools", 1)),
        ),
    ),
    (
        "Aravel",
        "elf",
        Alignment.NEUTRAL,
        ("magic_missile",),
        (
            (("sword", 1), ("chainmail", 1), ("short_bow", 1), ("arrows", 1)),
            (("sword", 1), ("leather", 1), ("short_bow", 1), ("arrows", 1)),
            (("sword", 1), ("leather", 1)),
            (("dagger", 1),),
        ),
    ),
    (
        "Zindal",
        "magic_user",
        Alignment.NEUTRAL,
        ("sleep",),
        (
            (
                ("dagger", 1),
                ("oil_flask", 2),
                ("torch", 1),
                ("tinder_box", 1),
                ("rations_standard", 1),
                ("waterskin", 1),
            ),
            (("dagger", 1), ("torch", 1), ("tinder_box", 1)),
            (("dagger", 1),),
        ),
    ),
)

# If a class with ability requirements never rolls a legal set, swap in a
# requirement-free stand-in so the party always fields six.
_FALLBACK_CLASS = {
    "dwarf": ("fighter", "Brunhild"),
    "elf": ("magic_user", "Aravel"),
    "halfling": ("thief", "Fox"),
}

# Body armour first, then every weapon, then the shield last — so a two-handed
# arm (the dwarf's battle axe) wins the hands and the shield is left packed
# rather than blocking the weapon. Mirrors the wizard's `_auto_equip` order.
_EQUIP_PRIORITY = (
    "chainmail",
    "leather",
    "sword",
    "battle_axe",
    "mace",
    "club",
    "short_sword",
    "hand_axe",
    "spear",
    "short_bow",
    "dagger",
    "shield",
)


def load_adventure(path: Path | str | None = None) -> Adventure:
    """Load and vet an adventure document; defaults to the bundled one (or OSR_WEB_ADVENTURE).

    Beyond the envelope and the model, the document's cross-references are walked
    ([`validate_adventure`][osrlib.crawl.adventure.validate_adventure]): dangling
    item, area, monster, and quest references, colliding bundled ids, and
    consequences naming a literal character id. Without the walk an authored typo
    loads clean and then either fails at session creation or — the deceptive
    case — silently never fires.

    Raises:
        ContentValidationError: Listing every dangling reference found; the API
            turns it into a 422.
    """
    if path is None:
        path = Path(os.environ.get("OSR_WEB_ADVENTURE", _DEFAULT_ADVENTURE))
    with Path(path).open(encoding="utf-8") as handle:
        document = json.load(handle)
    adventure = Adventure.model_validate(check_document(document, "adventure"))
    validate_adventure(adventure, load_monsters(), load_equipment())
    return adventure


def _equip_ids(purchases: tuple[tuple[str, int], ...]) -> tuple[str, ...]:
    bought = {item_id for item_id, _ in purchases}
    return tuple(item_id for item_id in _EQUIP_PRIORITY if item_id in bought)


def _outfit(definition: ClassDefinition, gold: int, tiers: tuple) -> Inventory | None:
    """The first kit tier the purse affords, bought and worn; `None` if none fits.

    Tiers are tried best to barest; a tier is affordable only if every item fits
    the remaining gold in sequence. The bought kit is then worn: armour, then
    every weapon, then the shield, gated by `validate_equip` exactly like the
    wizard's auto-equip.
    """
    equipment = load_equipment()
    for kit in tiers:
        inventory = Inventory()
        inventory.purse.gp = gold
        affordable = True
        for item_id, lots in kit:
            template = equipment.get(item_id)
            if validate_purchase(inventory.purse, template, lots):
                affordable = False
                break
            purchase(inventory, template, lots)
        if not affordable:
            continue
        for item_id in _equip_ids(kit):
            instance = next(
                (
                    candidate
                    for candidate in inventory.items
                    if isinstance(candidate, ItemInstance) and candidate.template.id == item_id
                ),
                None,
            )
            if instance is not None and not validate_equip(definition, instance, inventory):
                equip(inventory, definition, instance)
        return inventory
    return None


def _create_member(name, class_id, alignment, spells, tiers, ruleset, stream, house_rules):
    """One member: retry ability rolls for gated classes, walk kit tiers down.

    Mirrors `create_character`'s fixed draw order — abilities, hit points, gold —
    with the house rules applied: 4d6-drop-lowest replaces the 3d6 score draws,
    and maximum first-level hit points skips the hit-point draw entirely.
    """
    definition = load_classes().get(class_id)
    tables = load_ability_tables()
    for _attempt in range(24):
        if house_rules.ability_method == "4d6_drop_lowest":
            ability_rolls, _ = roll_abilities_4d6(stream)
        else:
            ability_rolls = roll_ability_scores(stream)
        if validate_class_choice(ability_rolls.scores, definition):
            continue  # the dice missed a class requirement: reroll abilities
        scores = dict(ability_rolls.scores)
        con_modifier = tables.hit_point_modifier(scores[AbilityScore.CON])
        if house_rules.max_hp:
            die = definition.row(1).hit_dice.die
            hit_points = max(1, die + con_modifier)
        else:
            hit_points = roll_hit_points(definition, con_modifier, ruleset, stream).hit_points
        gold = roll_starting_gold(stream).total
        inventory = _outfit(definition, gold, tiers)
        if inventory is None:
            continue  # even the barest tier didn't fit (30 gp minimum: unreachable)
        return Character(
            name=name,
            class_id=definition.id,
            race=definition.race,
            level=1,
            xp=0,
            scores=scores,
            alignment=alignment,
            extra_languages=(),
            max_hp=hit_points,
            current_hp=hit_points,
            inventory=inventory,
            spell_book=tuple(spells),
        )
    fallback = _FALLBACK_CLASS.get(class_id)
    if fallback is None:
        raise RuntimeError(f"could not create {name} the {class_id}")
    fallback_class, _ = fallback
    fallback_spells = ("sleep",) if fallback_class == "magic_user" else ()
    fallback_tiers = next(entry[4] for entry in _ROSTER if entry[1] == fallback_class)
    return _create_member(
        name,
        fallback_class,
        alignment,
        fallback_spells,
        fallback_tiers,
        ruleset,
        stream,
        house_rules,
    )


def premade_members(stream, ruleset: Ruleset, house_rules: HouseRules | None = None):
    """Roll the classic six on the caller's own stream, one roster entry at a time.

    The stream is the caller's — a session's creation stream for `build_party`,
    a party builder's for its premade staging — so whoever owns the seed owns
    the reproducibility. Optional house rules (4d6-drop-lowest, maximum
    first-level hit points) apply to every member; `None` rolls by the book.

    Returns:
        The six freshly rolled [`Character`][osrlib.core.character.Character]s,
        in roster order.
    """
    rules = house_rules if house_rules is not None else HouseRules()
    return [
        _create_member(name, class_id, alignment, spells, tiers, ruleset, stream, rules)
        for name, class_id, alignment, spells, tiers in _ROSTER
    ]


def build_party(seed: int, ruleset: Ruleset, house_rules: HouseRules | None = None) -> tuple[Party, RngStreams]:
    """Roll the six-member party on the session's own creation stream."""
    streams = RngStreams(master_seed=seed)
    stream = streams.get(CHARACTER_CREATION_STREAM)
    members = premade_members(stream, ruleset, house_rules)
    return Party(members=members), streams


def _shipped_item(instance) -> bool:
    """Whether an imported instance's template is in the shipped catalogs.

    An adventure may bundle items of its own, which join the equipment catalog
    for that adventure's sessions alone. An instance whose template neither
    catalog knows is therefore an object bound to the adventure it came out of.
    """
    if isinstance(instance, MagicItemInstance):
        catalog, item_id = load_magic_items(), instance.template_id
    else:
        catalog, item_id = load_equipment(), instance.template.id
    try:
        catalog.get(item_id)
    except ValueError:
        return False
    return True


def party_from_save(document: dict) -> Party:
    """Extract a save's party for a new adventure, shorn of session-owned state.

    The members keep their sheets, gear, purses, spellbooks, and the dead their
    deaths. Conditions owned by a ledger effect (a timed light, a poison clock)
    and stat modifiers (always effect-owned) belong to the session the save
    recorded and are stripped — the new session's ledger starts empty.

    Adventure-bound objects go the same way. `has_item` is stateless and
    party-wide — every member, living or dead, worn slots included — so a veteran
    who marched home with adventure A's key would walk into adventure B holding a
    pre-satisfied gate, or into a re-run of their own adventure holding its own
    solution. Every carry surface is filtered against the shipped catalogs;
    valuables and the purse are money and stay.
    """
    party = load_game(document).party
    for member in party.members:
        member.conditions = tuple(active for active in member.conditions if active.effect_id is None)
        member.stat_modifiers = ()
        inventory = member.inventory
        inventory.items = [instance for instance in inventory.items if _shipped_item(instance)]
        if inventory.worn_armour is not None and not _shipped_item(inventory.worn_armour):
            inventory.worn_armour = None
        if inventory.shield is not None and not _shipped_item(inventory.shield):
            inventory.shield = None
        inventory.wielded = [instance for instance in inventory.wielded if _shipped_item(instance)]
        inventory.rings = [instance for instance in inventory.rings if _shipped_item(instance)]
    return party


def _advance_allocator(session: GameSession, party: Party) -> None:
    """Fast-forward the fresh session's id counters past every imported id.

    An imported party keeps its `character-NNNN` member ids and any
    `valuable-`/`magic-item-` instance ids from its old session; without this,
    the new session's allocator would hand the same ids to entities it creates
    later.
    """
    carried: list[str | None] = []
    for member in party.members:
        carried.append(member.id)
        carried.extend(getattr(instance, "instance_id", None) for instance in member.inventory.all_instances())
        carried.extend(valuable.instance_id for valuable in member.inventory.valuables)
    for value in carried:
        if not isinstance(value, str):
            continue
        match = _ID_PATTERN.match(value)
        if match is None:
            continue
        prefix, ordinal = match.group(1), int(match.group(2))
        if ordinal > session.allocator.counters.get(prefix, 0):
            session.allocator.counters[prefix] = ordinal


def new_session(
    seed: int,
    adventure_path: Path | str | None = None,
    party: Party | None = None,
    house_rules: HouseRules | None = None,
) -> GameSession:
    """A fresh game: party rolled (or carried over), spells prepared, standing in town.

    `house_rules` bends the pregenerated party's creation draws and is ignored
    when a party is carried in — imported members keep their sheets.
    """
    ruleset = Ruleset()
    adventure = load_adventure(adventure_path)
    if party is None:
        party, streams = build_party(seed, ruleset, house_rules)
        session = GameSession.new(party, adventure, seed=seed, ruleset=ruleset)
        session.streams.restore_states(streams.export_states())
    else:
        session = GameSession.new(party, adventure, seed=seed, ruleset=ruleset)
        _advance_allocator(session, party)
    _prepare_spells(session)
    return session


def restore_session(document) -> GameSession:
    """Restore a session from a save document."""
    return load_game(document)


def _prepare_spells(session: GameSession) -> None:
    """Fill each caster's level-1 slot before the adventure starts.

    This is a creation-time affordance, not play: the pre-game night's sleep
    happened off-screen, so the memorization goes straight through the core
    [`memorize_spells`][osrlib.core.spells.memorize_spells] function — the same
    stepwise-creation pattern the rest of this module uses — rather than through
    `PrepareSpells`/`Rest` commands. Commands would burn a night on the session
    clock and write bookkeeping into the transcript before the player has done
    anything; this touches neither. The session's sleep ledger stays
    untouched too, so the *next* preparation still demands a real night's rest,
    exactly as the rules want.

    A caster whose selection the engine rejects (an imported member mid-level,
    say) simply arrives unprepared and manages their own preparation in town.
    """
    catalog = load_spells()
    classes = load_classes()
    for member in session.party.living_members():
        if not member.spell_book or member.memorized_spells:
            continue
        memorize_spells(
            member,
            classes.get(member.class_id),
            catalog,
            (MemorizedSpell(spell_id=member.spell_book[0]),),
        )
