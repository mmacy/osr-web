"""Unit tests for the roll-your-own party builder's app-owned logic.

The builder drives osrlib's creation functions, so these tests cover only what
this repo adds on top: the optional house rules (4d6-drop-lowest ability scores,
maximum first-level hit points), the curated equipment kits, and the optional
builder seed. Unseeded ability and gold draws are random, so those assertions
hold for any RNG — where a known purse is needed, the gold roll is overwritten
with a fixed one. The seed tests are the exception: they pin the draws and
compare two runs against each other rather than against golden numbers, so they
never hard-code osrlib's draw sequence.
"""

import pytest
from fastapi import HTTPException
from osrlib.core.abilities import AbilityScore
from osrlib.core.character import AbilityScoreRolls
from osrlib.core.dice import RollResult
from osrlib.core.items import ArmourTemplate, WeaponTemplate
from osrlib.data import load_ability_tables, load_classes, load_equipment

from server.app import _builders, create_party_builder, party_builder_step
from server.creation import (
    ABILITY_METHODS,
    EQUIPMENT_KITS,
    SEED_BOUND,
    PartyBuilder,
    creation_catalog,
)


def _con_hp_modifier(builder: PartyBuilder) -> int:
    con = builder.draft.ability_rolls.scores[AbilityScore.CON]
    return load_ability_tables().hit_point_modifier(con)


def _fighter_to_shop(builder: PartyBuilder) -> None:
    """Walk a fighter draft to the shopping stage (no class requirements to trip on)."""
    builder.roll_abilities()
    builder.choose_class("fighter")
    builder.roll_hp()
    builder.roll_gold()


def _finish_fighter(builder: PartyBuilder, name: str = "Aldric") -> None:
    _fighter_to_shop(builder)
    builder.finalize(name, "lawful")


_SEED = 20260725
_OTHER_SEED = 8675309

_FIGHTER_STEPS = (
    {"action": "roll_abilities"},
    {"action": "choose_class", "class_id": "fighter"},
    {"action": "roll_hit_points"},
    {"action": "roll_gold"},
)


def _draws(draft: dict) -> dict:
    """Every random draw a draft carries: ability dice, hit points, gold.

    Reads the same shape whether it came from `builder.state()["draft"]` or from
    an endpoint payload, so a builder run and a driven HTTP run compare alike.
    """
    return {key: draft[key] for key in ("rolls", "hit_points", "gold")}


def _drive(builder_id: str, steps=_FIGHTER_STEPS) -> dict:
    """Drive one builder through `steps` via the step endpoint; return its draft."""
    payload = None
    for step in steps:
        payload = party_builder_step(builder_id, dict(step))
        assert payload["accepted"] is True, payload["rejections"]
    return payload["draft"]


class TestHouseRulesDefaults:
    def test_by_the_book_by_default(self):
        builder = PartyBuilder()
        rules = builder.state()["house_rules"]
        assert rules == {"ability_method": "3d6", "max_hp": False, "locked": False}

    def test_3d6_rolls_carry_no_dropped_die(self):
        builder = PartyBuilder()
        builder.roll_abilities()
        for roll in builder.state()["draft"]["rolls"].values():
            assert "dropped" not in roll
            assert len(roll["dice"]) == 3


class TestHouseRulesSetting:
    def test_set_and_echo(self):
        builder = PartyBuilder()
        assert builder.set_house_rules(ability_method="4d6_drop_lowest", max_hp=True) == []
        rules = builder.state()["house_rules"]
        assert rules["ability_method"] == "4d6_drop_lowest"
        assert rules["max_hp"] is True

    def test_partial_update_keeps_other_value(self):
        builder = PartyBuilder()
        builder.set_house_rules(max_hp=True)
        builder.set_house_rules(ability_method="4d6_drop_lowest")
        rules = builder.state()["house_rules"]
        assert rules == {
            "ability_method": "4d6_drop_lowest",
            "max_hp": True,
            "locked": False,
        }

    def test_unknown_method_rejected(self):
        builder = PartyBuilder()
        rejections = builder.set_house_rules(ability_method="2d6")
        assert [r.code for r in rejections] == ["creation.house_rules.unknown_method"]
        assert builder.house_rules.ability_method == "3d6"

    def test_bad_method_does_not_silently_apply_max_hp(self):
        # A rejected method must not partially apply a valid max_hp sent alongside.
        builder = PartyBuilder()
        rejections = builder.set_house_rules(ability_method="2d6", max_hp=True)
        assert [r.code for r in rejections] == ["creation.house_rules.unknown_method"]
        assert builder.house_rules.max_hp is False
        assert builder.house_rules.ability_method == "3d6"

    def test_locked_once_a_draft_exists(self):
        builder = PartyBuilder()
        builder.roll_abilities()
        rejections = builder.set_house_rules(max_hp=True)
        assert [r.code for r in rejections] == ["creation.house_rules.locked"]
        assert builder.state()["house_rules"]["locked"] is True

    def test_locked_once_a_member_is_seated(self):
        builder = PartyBuilder()
        _finish_fighter(builder)
        assert builder.draft is None
        rejections = builder.set_house_rules(max_hp=True)
        assert [r.code for r in rejections] == ["creation.house_rules.locked"]
        assert builder.state()["house_rules"]["locked"] is True


class TestFourD6DropLowest:
    def test_kept_dice_sum_to_score_and_lowest_is_dropped(self):
        builder = PartyBuilder()
        builder.set_house_rules(ability_method="4d6_drop_lowest")
        builder.roll_abilities()
        for roll in builder.state()["draft"]["rolls"].values():
            assert len(roll["dice"]) == 3
            assert sum(roll["dice"]) == roll["total"]
            assert "dropped" in roll
            assert 1 <= roll["dropped"] <= 6
            assert roll["dropped"] <= min(roll["dice"])
            for die in roll["dice"]:
                assert 1 <= die <= 6

    def test_catalog_lists_both_methods(self):
        assert creation_catalog()["ability_methods"] == list(ABILITY_METHODS)


class TestMaximumHitPoints:
    def test_max_hp_takes_the_top_of_the_die(self):
        builder = PartyBuilder()
        builder.set_house_rules(max_hp=True)
        builder.roll_abilities()
        builder.choose_class("fighter")  # d8
        builder.roll_hp()
        mod = _con_hp_modifier(builder)
        roll = builder.draft.hit_point_roll
        assert roll.rolls == (8,)
        assert roll.hit_points == max(1, 8 + mod)

    def test_rolled_hp_stays_within_die_range(self):
        builder = PartyBuilder()
        builder.roll_abilities()
        builder.choose_class("fighter")
        builder.roll_hp()
        mod = _con_hp_modifier(builder)
        roll = builder.draft.hit_point_roll
        assert roll.hit_points == max(1, roll.rolls[-1] + mod)
        assert 1 <= roll.rolls[-1] <= 8

    def test_max_hp_never_drops_below_one_for_a_frail_d4_caster(self):
        # A d4 class with the worst possible CON still floors at 1, not below.
        builder = PartyBuilder()
        builder.set_house_rules(max_hp=True)
        builder.roll_abilities()
        builder.choose_class("thief")  # d4, no ability requirement
        scores = dict(builder.draft.ability_rolls.scores)
        scores[AbilityScore.CON] = 3  # the lowest score, worst HP modifier
        rolls = dict(builder.draft.ability_rolls.rolls)
        rolls[AbilityScore.CON] = (1, 1, 1)
        builder.draft.ability_rolls = AbilityScoreRolls(scores=scores, rolls=rolls)
        builder.roll_hp()
        mod = load_ability_tables().hit_point_modifier(3)
        assert mod < 0
        roll = builder.draft.hit_point_roll
        assert roll.rolls == (4,)
        assert roll.hit_points == max(1, 4 + mod)
        assert roll.hit_points >= 1


class TestEquipmentKits:
    def test_one_kit_per_class(self):
        class_ids = {definition.id for definition in load_classes().classes}
        assert {kit["class_id"] for kit in EQUIPMENT_KITS} == class_ids
        assert len(EQUIPMENT_KITS) == len(class_ids)

    def test_every_kit_item_is_legal_for_its_class(self):
        # A kit carries class-specific arms and armour; nothing in it may be
        # gear the class cannot use (it would ride dead weight in the pack).
        classes = load_classes()
        equipment = load_equipment()
        for kit in EQUIPMENT_KITS:
            definition = classes.get(kit["class_id"])
            for item_id, _ in kit["items"]:
                template = equipment.get(item_id)  # raises if unknown
                if isinstance(template, WeaponTemplate):
                    kind = definition.weapons.kind.value
                    if kind == "allowed":
                        assert template.id in definition.weapons.weapon_ids
                    elif kind == "forbidden":
                        assert template.id not in definition.weapons.weapon_ids
                elif isinstance(template, ArmourTemplate):
                    if template.is_shield:
                        assert definition.armour.shields_allowed
                    else:
                        kind = definition.armour.kind.value
                        assert kind != "none"
                        if kind == "leather_only":
                            assert template.id == "leather"

    def test_catalog_resolves_kit_names_and_costs(self):
        kits = creation_catalog()["kits"]
        assert {kit["id"] for kit in kits} == {kit["id"] for kit in EQUIPMENT_KITS}
        for kit in kits:
            assert kit["class_id"]
            assert kit["cost_gp"] == sum(item["cost_gp"] for item in kit["items"])
            assert all(item["name"] for item in kit["items"])

    def test_each_class_can_buy_its_kit_with_a_full_purse(self):
        for kit in EQUIPMENT_KITS:
            builder = PartyBuilder()
            builder.roll_abilities()
            # Perfect scores, so requirement-gated classes always qualify.
            builder.draft.ability_rolls = AbilityScoreRolls(
                scores={ability: 18 for ability in AbilityScore},
                rolls={ability: (6, 6, 6) for ability in AbilityScore},
            )
            assert builder.choose_class(kit["class_id"]) == []
            builder.roll_hp()
            builder.roll_gold()
            builder.draft.gold_roll = RollResult(rolls=(6, 6, 6), modifier=0, multiplier=10, total=180)
            assert builder.buy_kit(kit["id"]) == []
            bought = {item_id for item_id, _ in builder.draft.purchases}
            assert bought == {item_id for item_id, _ in kit["items"]}

    def test_buy_kit_is_all_or_nothing_when_unaffordable(self):
        builder = PartyBuilder()
        _fighter_to_shop(builder)
        builder.draft.gold_roll = RollResult(rolls=(1,), modifier=0, multiplier=1, total=10)
        rejections = builder.buy_kit("fighter")  # 76 gp > 10 gp purse
        assert [r.code for r in rejections] == ["creation.kit.unaffordable"]
        assert builder.draft.purchases == []

    def test_buy_kit_accounts_for_already_spent_gold(self):
        builder = PartyBuilder()
        _fighter_to_shop(builder)
        builder.draft.gold_roll = RollResult(rolls=(8,), modifier=0, multiplier=10, total=80)
        assert builder.buy("sword", 1) == []  # 10 gp, leaving 70 in the purse
        rejections = builder.buy_kit("fighter")  # 76 gp > 70 gp remaining
        assert [r.code for r in rejections] == ["creation.kit.unaffordable"]
        assert builder.draft.purchases == [("sword", 1)]  # only the sword

    def test_buy_kit_of_another_class_rejected(self):
        builder = PartyBuilder()
        _fighter_to_shop(builder)
        builder.draft.gold_roll = RollResult(rolls=(6, 6, 6), modifier=0, multiplier=10, total=180)
        rejections = builder.buy_kit("thief")
        assert [r.code for r in rejections] == ["creation.kit.wrong_class"]
        assert builder.draft.purchases == []

    def test_buy_kit_unknown_id_rejected(self):
        builder = PartyBuilder()
        _fighter_to_shop(builder)
        rejections = builder.buy_kit("no_such_kit")
        assert [r.code for r in rejections] == ["creation.kit.unknown"]

    def test_buy_kit_requires_the_shopping_stage(self):
        builder = PartyBuilder()
        builder.roll_abilities()
        builder.choose_class("fighter")
        rejections = builder.buy_kit("fighter")  # no gold rolled yet
        assert [r.code for r in rejections] == ["creation.step.out_of_order"]


class TestCreateEndpointHouseRules:
    """The `POST /api/party-builders` handler, called directly (no HTTP layer)."""

    def test_empty_body_accepts_with_defaults(self):
        payload = create_party_builder(None)
        assert payload["accepted"] is True
        assert payload["rejections"] == []
        assert payload["house_rules"] == {
            "ability_method": "3d6",
            "max_hp": False,
            "locked": False,
        }

    def test_applies_house_rules_up_front(self):
        payload = create_party_builder({"house_rules": {"ability_method": "4d6_drop_lowest", "max_hp": True}})
        assert payload["accepted"] is True
        assert payload["house_rules"] == {
            "ability_method": "4d6_drop_lowest",
            "max_hp": True,
            "locked": False,
        }

    def test_surfaces_house_rule_rejection(self):
        payload = create_party_builder({"house_rules": {"ability_method": "2d6", "max_hp": True}})
        assert payload["accepted"] is False
        assert payload["rejections"][0]["code"] == "creation.house_rules.unknown_method"
        assert payload["rejections"][0]["message"]  # a friendly, non-empty line
        # The rejected request applies nothing — not even the valid max_hp.
        assert payload["house_rules"] == {
            "ability_method": "3d6",
            "max_hp": False,
            "locked": False,
        }


class TestSeededBuilder:
    """The builder's optional seed: same seed and same steps, same draws."""

    def test_same_seed_repeats_every_draw(self):
        first = PartyBuilder(seed=_SEED)
        second = PartyBuilder(seed=_SEED)
        _fighter_to_shop(first)
        _fighter_to_shop(second)
        assert _draws(first.state()["draft"]) == _draws(second.state()["draft"])

    def test_different_seeds_draw_differently(self):
        # The failure this guards: a seed that is accepted and then ignored
        # would make every seeded run identical to a fresh random one.
        first = PartyBuilder(seed=_SEED)
        second = PartyBuilder(seed=_OTHER_SEED)
        _fighter_to_shop(first)
        _fighter_to_shop(second)
        assert _draws(first.state()["draft"]) != _draws(second.state()["draft"])

    def test_unseeded_builders_are_not_reproducible(self):
        # Omitting the seed rolls fresh randomness.
        # Six ability rolls plus hit points and gold agreeing by chance is
        # vanishingly unlikely.
        first = PartyBuilder()
        second = PartyBuilder()
        _fighter_to_shop(first)
        _fighter_to_shop(second)
        assert _draws(first.state()["draft"]) != _draws(second.state()["draft"])

    def test_a_reroll_after_a_discard_stays_reproducible(self):
        def reroll(builder: PartyBuilder) -> dict:
            builder.roll_abilities()
            first_roll = builder.state()["draft"]["rolls"]
            builder.discard_draft()
            builder.roll_abilities()
            return {"first": first_roll, "second": builder.state()["draft"]["rolls"]}

        first = reroll(PartyBuilder(seed=_SEED))
        second = reroll(PartyBuilder(seed=_SEED))
        assert first == second
        # A reroll draws on — it does not replay the discarded character.
        assert first["second"] != first["first"]

    def test_seeded_house_rules_are_reproducible(self):
        def rolled(builder: PartyBuilder) -> dict:
            builder.set_house_rules(ability_method="4d6_drop_lowest", max_hp=True)
            _fighter_to_shop(builder)
            return _draws(builder.state()["draft"])

        first = rolled(PartyBuilder(seed=_SEED))
        second = rolled(PartyBuilder(seed=_SEED))
        assert first == second
        # The house rules really are in force: four dice per ability, and hit
        # points off the top of the d8 rather than a roll.
        assert all("dropped" in roll for roll in first["rolls"].values())
        assert first["hit_points"]["rolls"] == [8]

    def test_house_rules_still_bend_the_draws_under_one_seed(self):
        # 4d6-drop-lowest takes a fourth die per ability and max-HP skips the
        # hit-point draw, so the same seed cannot produce the same sequence.
        by_the_book = PartyBuilder(seed=_SEED)
        house = PartyBuilder(seed=_SEED)
        house.set_house_rules(ability_method="4d6_drop_lowest", max_hp=True)
        _fighter_to_shop(by_the_book)
        _fighter_to_shop(house)
        assert _draws(by_the_book.state()["draft"]) != _draws(house.state()["draft"])

    def test_a_finalized_party_is_reproducible(self):
        def rolled_party(builder: PartyBuilder) -> list[dict]:
            _fighter_to_shop(builder)
            builder.buy_kit("fighter")
            builder.finalize("Aldric", "lawful")
            _finish_fighter(builder, "Bevin")
            party, rejections = builder.party()
            assert rejections == []
            return [
                {
                    "name": member.name,
                    "scores": {a.value: s for a, s in member.scores.items()},
                    "max_hp": member.max_hp,
                    "gold_gp": member.inventory.purse.gp,
                    "armour_class": member.armour_class,
                }
                for member in party.members
            ]

        first = rolled_party(PartyBuilder(seed=_SEED))
        second = rolled_party(PartyBuilder(seed=_SEED))
        assert first == second
        assert [member["name"] for member in first] == ["Aldric", "Bevin"]
        assert first[0] != first[1]  # two characters, not the same draw twice

    def test_out_of_range_seed_raises(self):
        with pytest.raises(ValueError):
            PartyBuilder(seed=-1)
        with pytest.raises(ValueError):
            PartyBuilder(seed=SEED_BOUND)


class TestCreateEndpointSeed:
    """`POST /api/party-builders` with a seed, driven through the step endpoint."""

    @pytest.fixture(autouse=True)
    def _drop_builders(self):
        """The handler parks every builder in a module dict; don't leak them."""
        before = set(_builders)
        yield
        for builder_id in set(_builders) - before:
            _builders.pop(builder_id, None)

    def _open(self, body: dict | None = None) -> str:
        payload = create_party_builder(body)
        assert payload["accepted"] is True, payload["rejections"]
        return payload["builder_id"]

    def test_same_seed_repeats_a_driven_run(self):
        first = _drive(self._open({"seed": _SEED}))
        second = _drive(self._open({"seed": _SEED}))
        assert _draws(first) == _draws(second)
        assert first["scores"] == second["scores"]

    def test_a_different_seed_diverges(self):
        first = _drive(self._open({"seed": _SEED}))
        second = _drive(self._open({"seed": _OTHER_SEED}))
        assert _draws(first) != _draws(second)

    def test_omitting_the_seed_rolls_fresh(self):
        assert _draws(_drive(self._open())) != _draws(_drive(self._open({})))

    def test_seed_with_house_rules_is_reproducible(self):
        body = {
            "seed": _SEED,
            "house_rules": {"ability_method": "4d6_drop_lowest", "max_hp": True},
        }
        first = _drive(self._open(body))
        second = _drive(self._open(dict(body)))
        assert _draws(first) == _draws(second)
        assert all("dropped" in roll for roll in first["rolls"].values())

    def test_the_seed_never_rides_on_a_payload(self):
        payload = create_party_builder({"seed": _SEED})
        _builders.pop(payload["builder_id"], None)
        assert "seed" not in payload
        assert "seed" not in str(payload)

    def test_a_null_seed_is_fresh_randomness(self):
        payload = create_party_builder({"seed": None})
        assert payload["accepted"] is True

    @pytest.mark.parametrize("seed", ["banana", 1.5, True, -1, SEED_BOUND, [1], {"value": 1}])
    def test_a_bad_seed_is_422(self, seed):
        # Malformed content, not an in-fiction rejection — no builder is opened.
        before = set(_builders)
        with pytest.raises(HTTPException) as raised:
            create_party_builder({"seed": seed})
        assert raised.value.status_code == 422
        assert set(_builders) == before


class TestAutoEquipWithMagicItems:
    """A magic item in the kit must not crash the auto-equip pass (`specs/dev-loop/spec.md`, finding 3).

    `MagicItemInstance` carries `template_id`, not `template`, so reading `.template` off every
    inventory item raises the moment the shop stocks one. The builder's shop stocks none today,
    which is the only reason the party builder has never hit it.
    """

    def test_magic_item_stays_packed_and_the_mundane_kit_still_equips(self):
        from osrlib.core.items import MagicItemInstance

        builder = PartyBuilder()
        _fighter_to_shop(builder)
        builder.draft.gold_roll = RollResult(rolls=(6, 6, 6), modifier=0, multiplier=10, total=180)
        assert builder.buy("leather", 1) == []
        assert builder.buy("sword", 1) == []
        definition = load_classes().get("fighter")
        inventory = builder._inventory(builder.draft)
        amulet = MagicItemInstance(
            instance_id="magic-item-0001",
            template_id="amulet_of_protection_against_scrying",
        )
        inventory.items.append(amulet)

        builder._auto_equip(inventory, definition)

        assert inventory.worn_armour is not None
        assert inventory.worn_armour.template.id == "leather"
        assert amulet in inventory.items
        assert inventory.worn_armour is not amulet
