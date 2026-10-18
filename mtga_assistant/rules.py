"""Deterministic evidence from one sanitized Arena-presented decision."""
from __future__ import annotations

import math
import json
import re
from collections import Counter
from typing import Any
from mtga_assistant.projection import live_snapshot_fingerprint


SCHEMA_VERSION = 1
MAX_CHOICES = 64
MAX_FACTS_PER_CHOICE = 16
MAX_FACT_TEXT = 120

_SUPPORTED_KINDS = frozenset({
    'actions', 'attackers', 'blockers', 'payment', 'targets', 'mulligan', 'bottom_cards', 'other',
})
_FLAT_KINDS = frozenset({'actions', 'mulligan', 'bottom_cards'})
_CANDIDATE_KINDS = frozenset({'attackers', 'blockers', 'payment', 'targets'})
_ACTION_LABELS = frozenset({
    'Cast spell', 'Play land', 'Pass priority', 'Activate mana ability',
    'Float mana', 'Activate ability', 'Opening-hand option', 'Other Arena option',
})
_MULLIGAN_OPTIONS = frozenset({'Keep hand', 'Take a mulligan'})
_MANA_COLORS = frozenset({
    'White', 'Blue', 'Black', 'Red', 'Green', 'Colorless', 'Generic', 'X', 'Any color',
})
_PHASES = frozenset({
    'Phase_Beginning', 'Phase_Main1', 'Phase_Combat', 'Phase_Main2', 'Phase_Ending',
})
_STEPS = frozenset({
    'Step_Untap', 'Step_Upkeep', 'Step_Draw', 'Step_BeginCombat',
    'Step_DeclareAttack', 'Step_DeclareBlock', 'Step_CombatDamage',
    'Step_FirstStrikeDamage', 'Step_EndCombat', 'Step_End', 'Step_Cleanup',
})

_FACT_SOURCES = {
    'action_label': frozenset({'decision.options.action'}),
    'visible_card': frozenset({
        'decision.options.card', 'decision.attackers.card', 'decision.blockers.card',
        'decision.groups.candidates.card', 'decision.cost_selection.visible_candidates',
    }),
    'mana_cost': frozenset({'decision.options.mana_cost', 'decision.mana_cost'}),
    'mana_cost_supported': frozenset({'snapshot.zones.battlefield+decision.options.mana_cost'}),
    'legal_recipients': frozenset({'decision.attackers.legal_recipients'}),
    'can_submit_attackers': frozenset({'decision.can_submit'}),
    'legal_attackers': frozenset({'decision.blockers.legal_attackers'}),
    'selected_attackers': frozenset({'decision.blockers.selected_attackers'}),
    'max_attackers': frozenset({'decision.blockers.max_attackers'}),
    'target_action': frozenset({'decision.groups.candidates.action'}),
    'selection_minimum': frozenset({'decision.groups.minimum', 'decision.cost_selection.minimum', 'decision.bottom_required'}),
    'selection_maximum': frozenset({'decision.groups.maximum', 'decision.cost_selection.maximum'}),
    'selected_count': frozenset({'decision.groups.selected'}),
    'payment_action': frozenset({'decision.mana_actions'}),
    'cost_candidate': frozenset({'decision.cost_selection.visible_candidates'}),
    'mulligan_option': frozenset({'decision.options'}),
    'mulligan_type': frozenset({'decision.mulligan_type'}),
    'mulligans_taken': frozenset({'decision.mulligans_taken'}),
    'opening_assessment': frozenset({'snapshot.opening_evaluation'}),
    'opening_hand': frozenset({'snapshot.zones.hand'}),
    'phase': frozenset({'snapshot.phase'}),
    'step': frozenset({'snapshot.step'}),
    'turn_number': frozenset({'snapshot.turn_number'}),
    'life_total': frozenset({'snapshot.life_totals.you', 'snapshot.life_totals.opponent'}),
    'catalog_mana_cost': frozenset({'catalog.card.mana_cost'}),
    'catalog_type_line': frozenset({'catalog.card.type_line'}),
    'catalog_colors': frozenset({'catalog.card.colors'}),
    'catalog_mana_value': frozenset({'catalog.card.cmc'}),
    'catalog_power': frozenset({'catalog.card.power'}),
    'catalog_toughness': frozenset({'catalog.card.toughness'}),
    'catalog_oracle_text': frozenset({'catalog.card.oracle_text'}),
    'catalog_ability_text': frozenset({'catalog.card.ability_text'}),
    'catalog_face_summary': frozenset({'catalog.card_faces'}),
}
_FACT_CODES = frozenset(_FACT_SOURCES)
_CATALOG_FACT_TEXT_LIMITS = {
    'catalog_mana_cost': 128,
    'catalog_type_line': 256,
    'catalog_power': 64,
    'catalog_toughness': 64,
    'catalog_oracle_text': 1_024,
    'catalog_ability_text': 1_024,
}
_CATALOG_FULL_COLORS = frozenset({'White', 'Blue', 'Black', 'Red', 'Green'})
_REDUCER_LIMITATIONS = frozenset({
    'choice_payload_unavailable', 'unrecognized_action_type', 'mana_cost_unavailable',
    'choice_list_truncated', 'some_choices_unavailable', 'unrecognized_choice_type',
    'choice_bounds_unavailable', 'unrecognized_choice_action', 'details_not_supported',
    'catalog_unavailable', 'catalog_missing', 'catalog_name_mismatch', 'catalog_partial',
})
_OPTION_LIMITATIONS = frozenset({
    'malformed_choice', 'choice_payload_unavailable', 'choice_list_truncated',
    'unrecognized_action_type', 'unrecognized_choice_type', 'choice_bounds_unavailable',
})


def _result(*, status: str = 'unavailable', provenance: str = 'unknown',
            kind: str | None = None, mode: str = 'unavailable',
            choices: list[dict[str, Any]] | None = None,
            unknowns: set[str] | None = None,
            limitations: set[str] | None = None) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'status': status,
        'choice_provenance': provenance,
        'independent_legality': 'unverified',
        'decision_kind': kind,
        'comparison_mode': mode,
        'choices': choices or [],
        'unknowns': sorted(unknowns or ()),
        'limitations': sorted(limitations or ()),
    }


class _BoundDecisionAnalysis(dict):
    """Schema-compatible analysis carrying a non-serialized source binding."""
    __slots__ = ('snapshot_fingerprint',)

    def __init__(self, value: dict[str, Any], snapshot_fingerprint: str | None):
        super().__init__(value)
        self.snapshot_fingerprint = snapshot_fingerprint


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = ''.join(ch for ch in value if ch.isprintable()).strip()
    return text[:MAX_FACT_TEXT] or None


def _unknown_identity(value: Any) -> bool:
    text = _clean_text(value)
    return text is None or text.startswith(('Unknown visible ', 'Card #'))


def _add_fact(facts: list[dict[str, Any]], code: str, value: Any,
              source: str, limitations: set[str]) -> None:
    if code not in _FACT_CODES or source not in _FACT_SOURCES.get(code, ()):
        limitations.add('malformed_choice')
        return
    if code in _CATALOG_FACT_TEXT_LIMITS:
        maximum = _CATALOG_FACT_TEXT_LIMITS[code]
        if (not isinstance(value, str) or not value or len(value) > maximum or
                any(not ch.isprintable() for ch in value)):
            limitations.add('catalog_partial')
            return
        safe_value = value
    elif code == 'catalog_colors':
        if (not isinstance(value, list) or len(value) > 5 or
                any(not isinstance(color, str) or color not in _CATALOG_FULL_COLORS
                    for color in value) or len(set(value)) != len(value)):
            limitations.add('catalog_partial')
            return
        safe_value = list(value)
    elif code == 'catalog_mana_value':
        if (type(value) not in (int, float) or not math.isfinite(value) or
                not 0 <= value <= 1_000):
            limitations.add('catalog_partial')
            return
        safe_value = value
    elif code == 'catalog_face_summary':
        if (not isinstance(value, list) or len(value) > 4 or any(
                not isinstance(item, str) or not item or len(item) > 2_048 or
                any(not ch.isprintable() for ch in item) for item in value)):
            limitations.add('catalog_partial')
            return
        safe_value = list(value)
    elif isinstance(value, str):
        safe_value = _clean_text(value)
        if safe_value is None:
            limitations.add('malformed_choice')
            return
    elif isinstance(value, list):
        if len(value) > MAX_CHOICES:
            value = value[:MAX_CHOICES]
            limitations.add('choice_list_truncated')
        if any(not isinstance(item, str) or _clean_text(item) is None for item in value):
            limitations.add('malformed_choice')
            return
        safe_value = [_clean_text(item) for item in value]
    elif value is None or type(value) in (int, bool):
        safe_value = value
    else:
        limitations.add('malformed_choice')
        return
    if len(facts) >= MAX_FACTS_PER_CHOICE:
        limitations.add('choice_list_truncated')
        return
    facts.append({'code': code, 'value': safe_value, 'source': source})


def _context_facts(snapshot: dict[str, Any], unknowns: set[str],
                   limitations: set[str]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    phase = snapshot.get('phase')
    if isinstance(phase, str) and phase in _PHASES:
        _add_fact(facts, 'phase', phase, 'snapshot.phase', limitations)
    else:
        unknowns.add('missing_phase')

    step = snapshot.get('step')
    if isinstance(step, str) and step in _STEPS:
        _add_fact(facts, 'step', step, 'snapshot.step', limitations)
    else:
        unknowns.add('missing_step')

    turn = snapshot.get('turn_number')
    if type(turn) is int and 1 <= turn <= 100_000:
        _add_fact(facts, 'turn_number', turn, 'snapshot.turn_number', limitations)
    else:
        unknowns.add('missing_turn_number')

    seat = snapshot.get('my_seat')
    totals = snapshot.get('life_totals')
    if type(seat) is int and seat in (1, 2) and isinstance(totals, dict):
        for owner, label in ((seat, 'you'), (3 - seat, 'opponent')):
            value = totals.get(str(owner))
            if type(value) is int and abs(value) <= 1_000_000:
                _add_fact(facts, 'life_total', value,
                          f'snapshot.life_totals.{label}', limitations)
            else:
                unknowns.add('missing_life_total')
    else:
        unknowns.add('missing_life_total')
    return facts


def _safe_string_list(value: Any, unknowns: set[str],
                      limitations: set[str]) -> list[str] | None:
    if not isinstance(value, list):
        limitations.add('choice_payload_unavailable')
        return None
    if len(value) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    result = []
    for item in value[:MAX_CHOICES]:
        text = _clean_text(item)
        if text is None:
            limitations.add('malformed_choice')
            continue
        if _unknown_identity(text):
            unknowns.add('missing_card_identity')
            continue
        if text not in result:
            result.append(text)
    return result


def _card_fact(facts: list[dict[str, Any]], card: Any, source: str,
               unknowns: set[str], limitations: set[str], *, required: bool = True) -> str | None:
    text = _clean_text(card)
    if _unknown_identity(text):
        if required:
            unknowns.add('missing_card_identity')
        # Show a distinct visible Arena ID when available, without presenting
        # it to the LLM as a verified card name or rules fact.
        return text if isinstance(text, str) and text.startswith('Card #') else None
    _add_fact(facts, 'visible_card', text, source, limitations)
    return text


def _safe_card_references(value: Any, unknowns: set[str],
                          limitations: set[str]) -> list[tuple[str, dict[str, Any] | None]] | None:
    if not isinstance(value, list):
        limitations.add('choice_payload_unavailable')
        return None
    if len(value) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    result = []
    seen = set()
    for item in value[:MAX_CHOICES]:
        if isinstance(item, dict):
            if set(item) != {'card', 'catalog'} or not isinstance(item.get('catalog'), dict):
                limitations.add('malformed_choice')
                continue
            card, catalog = item.get('card'), item.get('catalog')
        else:
            card, catalog = item, None
        text = _clean_text(card)
        if text is None:
            limitations.add('malformed_choice')
            continue
        if _unknown_identity(text):
            unknowns.add('missing_card_identity')
            continue
        catalog_key = json.dumps(catalog, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':')) if catalog is not None else None
        identity = (text, catalog_key)
        if identity in seen:
            continue
        seen.add(identity)
        result.append((text, catalog))
    return result


def _catalog_face_summary(faces: Any, limitations: set[str]) -> list[str] | None:
    if not isinstance(faces, list):
        limitations.add('catalog_partial')
        return None
    summaries = []
    if len(faces) > 4:
        limitations.add('catalog_partial')
    text_fields = (
        ('mana_cost', 'mana cost'), ('type_line', 'type'), ('oracle_text', 'text'),
    )
    for face in faces[:4]:
        if not isinstance(face, dict) or not isinstance(face.get('name'), str):
            limitations.add('catalog_partial')
            continue
        name = face['name']
        if not name.strip() or len(name) > 120 or any(not char.isprintable() for char in name):
            limitations.add('catalog_partial')
            continue
        parts = [name]
        for field, label in text_fields:
            value = face.get(field)
            maximum = _CATALOG_FACT_TEXT_LIMITS.get(
                {'mana_cost': 'catalog_mana_cost',
                 'type_line': 'catalog_type_line',
                 'oracle_text': 'catalog_oracle_text'}.get(field, ''), 0)
            if value is None:
                continue
            if (not isinstance(value, str) or len(value) > maximum or
                    any(not char.isprintable() for char in value)):
                limitations.add('catalog_partial')
                continue
            if value:
                parts.append(f'{label}: {value}')
        power, toughness = face.get('power'), face.get('toughness')
        if power is not None or toughness is not None:
            if (power is not None and (not isinstance(power, str) or len(power) > 64)) or (
                    toughness is not None and
                    (not isinstance(toughness, str) or len(toughness) > 64)):
                limitations.add('catalog_partial')
            else:
                parts.append(f'power/toughness: {power or "?"}/{toughness or "?"}')
        summary = '; '.join(parts)
        if len(summary) > 2_048:
            limitations.add('catalog_partial')
            continue
        summaries.append(summary)
    return summaries or None


def _add_catalog_facts(facts: list[dict[str, Any]], catalog: Any,
                       limitations: set[str]) -> None:
    if catalog is None:
        return
    if not isinstance(catalog, dict):
        limitations.add('catalog_partial')
        return
    mappings = (
        ('mana_cost', 'catalog_mana_cost', 'catalog.card.mana_cost'),
        ('type_line', 'catalog_type_line', 'catalog.card.type_line'),
        ('colors', 'catalog_colors', 'catalog.card.colors'),
        ('cmc', 'catalog_mana_value', 'catalog.card.cmc'),
        ('power', 'catalog_power', 'catalog.card.power'),
        ('toughness', 'catalog_toughness', 'catalog.card.toughness'),
        ('oracle_text', 'catalog_oracle_text', 'catalog.card.oracle_text'),
        ('ability_text', 'catalog_ability_text', 'catalog.card.ability_text'),
    )
    color_names = {'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green'}
    for field, code, source in mappings:
        if field not in catalog or catalog[field] is None:
            continue
        value = catalog[field]
        if field == 'colors' and isinstance(value, list):
            if any(color not in color_names for color in value):
                limitations.add('catalog_partial')
                continue
            value = [color_names[color] for color in value]
        elif isinstance(value, str) and not value:
            continue
        _add_fact(facts, code, value, source, limitations)
    if 'card_faces' in catalog and catalog['card_faces'] is not None:
        summaries = _catalog_face_summary(catalog['card_faces'], limitations)
        if summaries is not None:
            _add_fact(facts, 'catalog_face_summary', summaries,
                      'catalog.card_faces', limitations)


def _mana_symbols(cost: Any, unknowns: set[str],
                  limitations: set[str]) -> list[str] | None:
    if cost is None:
        unknowns.add('missing_mana_cost')
        return None
    if not isinstance(cost, list):
        limitations.add('mana_cost_unavailable')
        return None
    if len(cost) > 16:
        limitations.add('choice_list_truncated')
        return None
    result = []
    for symbol in cost:
        if not isinstance(symbol, dict):
            limitations.add('mana_cost_unavailable')
            return None
        colors = symbol.get('colors')
        count = symbol.get('count')
        if (not isinstance(colors, list) or not colors or len(colors) > 5 or
                any(not isinstance(color, str) or color not in _MANA_COLORS for color in colors) or
                type(count) is not int or not 0 <= count <= 99):
            limitations.add('mana_cost_unavailable')
            return None
        result.append(f"{count} {'/'.join(colors)}")
    return result


def _context_copy(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(fact) for fact in context]


def _choice(number: int, label: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {'number': number, 'label': label[:MAX_FACT_TEXT], 'facts': facts[:MAX_FACTS_PER_CHOICE]}


def _read_reducer_limitations(decision: dict[str, Any], limitations: set[str]) -> None:
    raw = decision.get('limitations', [])
    if not isinstance(raw, list):
        limitations.add('malformed_choice')
        return
    for code in raw[:MAX_CHOICES]:
        if isinstance(code, str) and code in _REDUCER_LIMITATIONS:
            limitations.add(code)
        else:
            limitations.add('malformed_choice')
    if len(raw) > MAX_CHOICES:
        limitations.add('choice_list_truncated')


_MANA_SYMBOL_COLORS = {
    'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green', 'C': 'Colorless',
}
_SIMPLE_TAP_MANA = re.compile(
    r'\{o?T\}: Add (\{o?[WUBRGC]\}(?:,? (?:or )?\{o?[WUBRGC]\})*)\.'
)


def _land_mana_options(card: dict[str, Any]) -> frozenset[str]:
    """Recognize intrinsic basic mana or a complete, unconditional one-tap clause.

    Native Arena text uses {oT}/{oG}; catalog text may use {T}/{G}.
    Costed, restricted, conditional and variable-output clauses fail closed.
    This lower-bound resource evidence does not verify all battlefield effects.
    """
    color = card.get('basic_mana')
    if isinstance(color, str) and color in {'White', 'Blue', 'Black', 'Red', 'Green'}:
        return frozenset({color})
    catalog = card.get('catalog')
    if not isinstance(catalog, dict):
        return frozenset()
    colors = set()
    for field in ('ability_text', 'oracle_text'):
        text = catalog.get(field)
        if not isinstance(text, str) or not 0 < len(text) <= 1_024:
            continue
        for clause in re.split(r'\s*\|\s*|[\r\n]+', text):
            clause = clause.strip()
            match = _SIMPLE_TAP_MANA.fullmatch(clause)
            if match:
                colors.update(_MANA_SYMBOL_COLORS[symbol] for symbol in
                              re.findall(r'\{o?([WUBRGC])\}', match.group(1)))
            elif clause in ('{T}: Add one mana of any color.', '{oT}: Add one mana of any color.'):
                colors.update({'White', 'Blue', 'Black', 'Red', 'Green'})
    return frozenset(colors)


def _observed_land_mana(snapshot: dict[str, Any]) -> list[frozenset[str]]:
    """One color-choice set per visible untapped land; never from the hand."""
    seat = snapshot.get('my_seat')
    zones = snapshot.get('zones')
    if type(seat) is not int or seat not in (1, 2) or not isinstance(zones, list):
        return []
    sources = []
    for zone in zones[:64]:
        if not isinstance(zone, dict) or zone.get('zone') != 'Battlefield':
            continue
        cards, count = zone.get('cards'), zone.get('count')
        if (not isinstance(cards, list) or type(count) is not int or
                count != len(cards) or not 0 <= count <= MAX_CHOICES):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            types = card.get('types')
            if (card.get('identity_status') == 'visible' and card.get('controller') == seat and
                    card.get('tapped') is False and isinstance(types, list) and
                    'Land' in types and 'Creature' not in types):
                colors = _land_mana_options(card)
                if not colors:
                    continue
                sources.append(colors)
                if len(sources) == MAX_CHOICES:
                    return sources
    return sources


def _mana_cost_supported(cost: Any, sources: list[frozenset[str]]) -> bool:
    """Match fixed costs to distinct observed lands, each spent at most once.

    Other mana sources, floating mana, restrictions, alternate costs, X and
    hybrid/Phyrexian payments are not established by this limited check.
    A false result means insufficient evidence, not a claim of illegality.
    """
    if not isinstance(cost, list) or len(cost) > 16:
        return False
    required: Counter[str] = Counter()
    for symbol in cost:
        if not isinstance(symbol, dict):
            return False
        colors, count = symbol.get('colors'), symbol.get('count')
        if (not isinstance(colors, list) or len(colors) != 1 or
                not isinstance(colors[0], str) or colors[0] not in
                {'White', 'Blue', 'Black', 'Red', 'Green', 'Colorless', 'Generic'} or
                type(count) is not int or not 0 <= count <= 99):
            return False
        required[colors[0]] += count
    if sum(required.values()) > len(sources):
        return False
    demands = [color for color, count in required.items() if color != 'Generic'
               for _ in range(count)]
    # Augmenting-path matching lets a flexible land cover whichever colored
    # requirement needs it, without counting its alternative colors as more mana.
    assigned = [-1] * len(sources)

    def assign(demand: int, seen: set[int]) -> bool:
        for source, colors in enumerate(sources):
            if source in seen or demands[demand] not in colors:
                continue
            seen.add(source)
            if assigned[source] == -1 or assign(assigned[source], seen):
                assigned[source] = demand
                return True
        return False

    return all(assign(demand, set()) for demand in range(len(demands)))


def recommendable_choice_numbers(analysis: dict[str, Any]) -> set[int]:
    """Constrain both generation and validation to the same mana evidence gate."""
    if not isinstance(analysis, dict) or not isinstance(analysis.get('choices'), list):
        return set()
    choices = analysis.get('choices', [])
    result = set()
    pass_numbers = set()
    unsupported_mana = False
    for choice in choices[:MAX_CHOICES]:
        if not isinstance(choice, dict) or type(choice.get('number')) is not int:
            continue
        if analysis.get('decision_kind') != 'actions':
            result.add(choice['number'])
            continue
        facts = choice.get('facts')
        if isinstance(facts, list):
            if any(isinstance(fact, dict) and fact.get('code') == 'action_label' and
                   fact.get('value') == 'Pass priority' for fact in facts):
                pass_numbers.add(choice['number'])
            unsupported_mana = unsupported_mana or any(
                isinstance(fact, dict) and fact.get('code') == 'mana_cost_supported' and
                fact.get('value') is False for fact in facts)
        if isinstance(facts, list) and any(
                isinstance(fact, dict) and fact.get('code') == 'mana_cost_supported' and
                fact.get('value') is True and fact_source_is_valid(fact['code'], fact.get('source'))
                for fact in facts[:MAX_FACTS_PER_CHOICE]):
            result.add(choice['number'])
    if result and result <= pass_numbers and unsupported_mana:
        # Unknown resources are not evidence that passing is preferable. Keep
        # abstention available instead of manufacturing a default pass advice.
        return set()
    return result


def _action_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                    unknowns: set[str], limitations: set[str],
                    snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    raw = decision.get('options')
    if not isinstance(raw, list):
        limitations.add('choice_payload_unavailable')
        return []
    if len(raw) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    mana_sources = _observed_land_mana(snapshot)
    choices = []
    for index, option in enumerate(raw[:MAX_CHOICES], start=1):
        if not isinstance(option, dict):
            limitations.add('malformed_choice')
            continue
        number = option.get('number')
        action = option.get('action')
        if (type(number) is not int or number != index or not isinstance(action, str) or
                action not in _ACTION_LABELS):
            limitations.add('malformed_choice')
            continue
        facts = _context_copy(context)
        _add_fact(facts, 'action_label', action, 'decision.options.action', limitations)
        card = _card_fact(facts, option.get('card'), 'decision.options.card', unknowns,
                          limitations, required=action in {
                              'Cast spell', 'Play land', 'Activate mana ability', 'Activate ability',
                          })
        cost = option.get('mana_cost')
        if cost is not None:
            symbols = _mana_symbols(cost, unknowns, limitations)
            if symbols is not None:
                _add_fact(facts, 'mana_cost', symbols, 'decision.options.mana_cost', limitations)
        elif (isinstance(decision.get('limitations'), list) and
              'mana_cost_unavailable' in decision['limitations']):
            unknowns.add('missing_mana_cost')
        supported = (action in {'Play land', 'Pass priority'} or
                     action in {'Cast spell', 'Activate ability', 'Activate mana ability', 'Float mana'}
                     and _mana_cost_supported(cost, mana_sources))
        _add_fact(facts, 'mana_cost_supported', supported,
                  'snapshot.zones.battlefield+decision.options.mana_cost', limitations)
        if action not in {'Play land', 'Pass priority'}:
            if not supported:
                unknowns.add('mana_availability_unverified')
        _add_catalog_facts(facts, option.get('catalog'), limitations)
        label = action + (f' — {card}' if card else '')
        choices.append(_choice(number, label, facts))
    if not choices:
        limitations.add('choice_payload_unavailable')
    return choices


def _mulligan_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                      unknowns: set[str], limitations: set[str],
                      snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raw = decision.get('options')
    if not isinstance(raw, list) or not raw:
        limitations.add('choice_payload_unavailable')
        return []
    if len(raw) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    mulligan_type = decision.get('mulligan_type')
    if mulligan_type is not None and mulligan_type != 'London':
        limitations.add('unrecognized_choice_type')
        mulligan_type = None
    count = decision.get('mulligans_taken')
    if count is not None and (type(count) is not int or not 0 <= count <= 10):
        limitations.add('choice_bounds_unavailable')
        count = None
    choices = []
    for index, option in enumerate(raw[:MAX_CHOICES], start=1):
        if not isinstance(option, str) or option not in _MULLIGAN_OPTIONS:
            limitations.add('malformed_choice')
            continue
        facts = _context_copy(context)
        _add_fact(facts, 'mulligan_option', option, 'decision.options', limitations)
        if option == 'Keep hand' and isinstance(snapshot, dict):
            evaluation = snapshot.get('opening_evaluation')
            if (isinstance(evaluation, dict) and evaluation.get('status') == 'ready' and
                    evaluation.get('recommendation') in ('KEEP_CANDIDATE', 'STRONG_KEEP_CANDIDATE',
                                                       'MULLIGAN_CANDIDATE', 'REVIEW')):
                opening_counts = evaluation.get('counts')
                if isinstance(opening_counts, dict):
                    forests = opening_counts.get('forests')
                    dorks = opening_counts.get('one_drop_mana_dorks')
                    if (type(forests) is int and 0 <= forests <= 7 and
                            type(dorks) is int and 0 <= dorks <= 7):
                        report = (f"{evaluation['recommendation']}: {forests} Forest(s), "
                                  f"{dorks} one-drop mana dork(s); deck-profile heuristic.")
                        _add_fact(facts, 'opening_assessment', report,
                                  'snapshot.opening_evaluation', limitations)
            seat = snapshot.get('my_seat')
            hand = next((z for z in snapshot.get('zones', [])
                         if isinstance(z, dict) and z.get('zone') == 'Hand' and
                         z.get('owner') == seat), None)
            if isinstance(hand, dict) and type(hand.get('count')) is int:
                cards = hand.get('cards')
                if (isinstance(cards, list) and len(cards) == hand['count']
                        and 0 < len(cards) <= 7 and
                        all(isinstance(c, dict) and c.get('identity_status') == 'visible'
                            and isinstance(c.get('name'), str)
                            and not _unknown_identity(c['name']) for c in cards)):
                    from collections import Counter
                    names = Counter(c['name'] for c in cards)
                    summary = ', '.join(f'{qty}x {name}' if qty > 1 else name
                                        for name, qty in names.items())
                    if len(summary) <= MAX_FACT_TEXT:
                        _add_fact(facts, 'opening_hand', summary,
                                  'snapshot.zones.hand', limitations)
        if mulligan_type:
            _add_fact(facts, 'mulligan_type', mulligan_type,
                      'decision.mulligan_type', limitations)
        if count is not None:
            _add_fact(facts, 'mulligans_taken', count,
                      'decision.mulligans_taken', limitations)
        choices.append(_choice(index, option, facts))
    if not choices:
        limitations.add('choice_payload_unavailable')
    return choices


def _bottom_card_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                         unknowns: set[str], limitations: set[str]) -> list[dict[str, Any]]:
    options = decision.get('options')
    count = decision.get('bottom_required')
    if not isinstance(options, list) or type(count) is not int or not 0 <= count <= len(options):
        limitations.add('choice_payload_unavailable')
        return []
    choices = []
    occurrences: dict[str, int] = {}
    for option in options:
        if isinstance(option, dict) and isinstance(option.get('card'), str):
            name = option['card']
            occurrences[name] = occurrences.get(name, 0) + 1
    for index, option in enumerate(options[:MAX_CHOICES], 1):
        if not isinstance(option, dict) or not isinstance(option.get('card'), str) or not option['card']:
            limitations.add('malformed_choice')
            continue
        facts = _context_copy(context)
        name = _card_fact(facts, option['card'], 'decision.options.card', unknowns,
                          limitations, required=True)
        _add_fact(facts, 'selection_minimum', count, 'decision.bottom_required', limitations)
        label = name or 'Unknown card'
        if occurrences.get(option['card'], 0) > 1:
            copy = option.get('copy')
            if type(copy) is int and 1 <= copy <= MAX_CHOICES:
                label += f' (copy {copy})'
        choices.append(_choice(index, 'Bottom candidate: ' + label, facts))
    return choices


def _attacker_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                      unknowns: set[str], limitations: set[str]) -> list[dict[str, Any]]:
    raw = decision.get('attackers')
    if not isinstance(raw, list):
        limitations.add('choice_payload_unavailable')
        raw = []
    if len(raw) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    can_submit = decision.get('can_submit')
    if can_submit is not None and type(can_submit) is not bool:
        limitations.add('malformed_choice')
        can_submit = None
    choices = []
    for index, attacker in enumerate(raw[:MAX_CHOICES], start=1):
        if not isinstance(attacker, dict):
            limitations.add('malformed_choice')
            continue
        facts = _context_copy(context)
        card = _card_fact(facts, attacker.get('card'), 'decision.attackers.card', unknowns,
                          limitations)
        _add_catalog_facts(facts, attacker.get('catalog'), limitations)
        recipient_refs = _safe_card_references(
            attacker.get('legal_recipients'), unknowns, limitations)
        if recipient_refs is not None:
            _add_fact(facts, 'legal_recipients', [label for label, _ in recipient_refs],
                      'decision.attackers.legal_recipients', limitations)
            for _, catalog in recipient_refs:
                _add_catalog_facts(facts, catalog, limitations)
        if can_submit is not None:
            _add_fact(facts, 'can_submit_attackers', can_submit,
                      'decision.can_submit', limitations)
        choices.append(_choice(index, card or 'Unknown visible attacker', facts))
    return choices


def _blocker_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                     unknowns: set[str], limitations: set[str]) -> list[dict[str, Any]]:
    raw = decision.get('blockers')
    if not isinstance(raw, list):
        limitations.add('choice_payload_unavailable')
        return []
    if len(raw) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    choices = []
    for index, blocker in enumerate(raw[:MAX_CHOICES], start=1):
        if not isinstance(blocker, dict):
            limitations.add('malformed_choice')
            continue
        facts = _context_copy(context)
        card = _card_fact(facts, blocker.get('card'), 'decision.blockers.card', unknowns,
                          limitations)
        _add_catalog_facts(facts, blocker.get('catalog'), limitations)
        for key, code, source in (
            ('legal_attackers', 'legal_attackers', 'decision.blockers.legal_attackers'),
            ('selected_attackers', 'selected_attackers', 'decision.blockers.selected_attackers'),
        ):
            references = _safe_card_references(blocker.get(key), unknowns, limitations)
            if references is not None:
                _add_fact(facts, code, [name for name, _ in references], source, limitations)
                for _, catalog in references:
                    _add_catalog_facts(facts, catalog, limitations)
        maximum = blocker.get('max_attackers')
        if maximum is not None:
            if type(maximum) is int and 0 <= maximum <= MAX_CHOICES:
                _add_fact(facts, 'max_attackers', maximum,
                          'decision.blockers.max_attackers', limitations)
            else:
                limitations.add('choice_bounds_unavailable')
        choices.append(_choice(index, card or 'Unknown visible blocker', facts))
    return choices


def _target_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                    unknowns: set[str], limitations: set[str]) -> list[dict[str, Any]]:
    groups = decision.get('groups')
    if not isinstance(groups, list):
        limitations.add('choice_payload_unavailable')
        return []
    if len(groups) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    choices = []
    number = 0
    bounded_groups = groups[:MAX_CHOICES]
    for group_index, group in enumerate(bounded_groups):
        if not isinstance(group, dict):
            limitations.add('malformed_choice')
            continue
        minimum, maximum, selected = (group.get('minimum'), group.get('maximum'),
                                      group.get('selected'))
        bounds_valid = (
            type(minimum) is int and 0 <= minimum <= MAX_CHOICES and
            type(maximum) is int and 0 <= maximum <= MAX_CHOICES and minimum <= maximum and
            type(selected) is int and 0 <= selected <= maximum
        )
        if not bounds_valid:
            limitations.add('choice_bounds_unavailable')
        candidates = group.get('candidates')
        if not isinstance(candidates, list):
            limitations.add('choice_payload_unavailable')
            continue
        if len(candidates) > MAX_CHOICES:
            limitations.add('choice_list_truncated')
        bounded_candidates = candidates[:MAX_CHOICES]
        for candidate_index, candidate in enumerate(bounded_candidates):
            if number >= MAX_CHOICES:
                limitations.add('choice_list_truncated')
                return choices
            action = candidate.get('action') if isinstance(candidate, dict) else None
            if not isinstance(action, str) or action not in {'select', 'unselect'}:
                limitations.add('malformed_choice')
                continue
            number += 1
            facts = _context_copy(context)
            _add_fact(facts, 'target_action', action,
                      'decision.groups.candidates.action', limitations)
            card = _card_fact(facts, candidate.get('card'),
                              'decision.groups.candidates.card', unknowns, limitations)
            _add_catalog_facts(facts, candidate.get('catalog'), limitations)
            if bounds_valid:
                _add_fact(facts, 'selection_minimum', minimum,
                          'decision.groups.minimum', limitations)
                _add_fact(facts, 'selection_maximum', maximum,
                          'decision.groups.maximum', limitations)
                _add_fact(facts, 'selected_count', selected,
                          'decision.groups.selected', limitations)
            choices.append(_choice(number, f"{action}" +
                                   (f' — {card}' if card else ''), facts))
            if number >= MAX_CHOICES:
                if (candidate_index + 1 < len(candidates) or
                        group_index + 1 < len(groups)):
                    limitations.add('choice_list_truncated')
                return choices
    return choices


def _payment_choices(decision: dict[str, Any], context: list[dict[str, Any]],
                     unknowns: set[str], limitations: set[str]) -> list[dict[str, Any]]:
    choices = []
    selection = decision.get('cost_selection')
    if selection is not None and not isinstance(selection, dict):
        limitations.add('malformed_choice')
        selection = None
    minimum = selection.get('minimum') if selection else None
    maximum = selection.get('maximum') if selection else None
    bounds_valid = (
        (minimum is None or type(minimum) is int and 0 <= minimum <= MAX_CHOICES) and
        (maximum is None or type(maximum) is int and 0 <= maximum <= MAX_CHOICES) and
        (minimum is None or maximum is None or minimum <= maximum)
    )
    if selection and not bounds_valid:
        limitations.add('choice_bounds_unavailable')

    if selection:
        raw_candidates = selection.get('visible_candidates')
        candidates = _safe_card_references(raw_candidates, unknowns, limitations)
        if candidates:
            for name, catalog in candidates:
                if len(choices) >= MAX_CHOICES:
                    limitations.add('choice_list_truncated')
                    break
                facts = _context_copy(context)
                _add_fact(facts, 'cost_candidate', name,
                          'decision.cost_selection.visible_candidates', limitations)
                _add_fact(facts, 'visible_card', name,
                          'decision.cost_selection.visible_candidates', limitations)
                _add_catalog_facts(facts, catalog, limitations)
                if bounds_valid:
                    if minimum is not None:
                        _add_fact(facts, 'selection_minimum', minimum,
                                  'decision.cost_selection.minimum', limitations)
                    if maximum is not None:
                        _add_fact(facts, 'selection_maximum', maximum,
                                  'decision.cost_selection.maximum', limitations)
                choices.append(_choice(len(choices) + 1, name, facts))
        unlabeled = selection.get('unlabeled_candidate_count')
        if type(unlabeled) is int and unlabeled > 0:
            unknowns.add('missing_card_identity')
        elif unlabeled is not None and (type(unlabeled) is not int or unlabeled < 0):
            limitations.add('malformed_choice')

    raw_actions = decision.get('mana_actions')
    if not isinstance(raw_actions, list):
        limitations.add('choice_payload_unavailable')
        raw_actions = []
    if len(raw_actions) > MAX_CHOICES:
        limitations.add('choice_list_truncated')
    mana_cost = decision.get('mana_cost')
    cost_facts = None
    if mana_cost is not None:
        symbols = _mana_symbols(mana_cost, unknowns, limitations)
        if symbols is not None:
            cost_facts = symbols
    else:
        unknowns.add('missing_mana_cost')

    for action in raw_actions[:MAX_CHOICES]:
        if not isinstance(action, str) or action not in _ACTION_LABELS:
            limitations.add('malformed_choice')
            continue
        if len(choices) >= MAX_CHOICES:
            limitations.add('choice_list_truncated')
            break
        facts = _context_copy(context)
        _add_fact(facts, 'payment_action', action, 'decision.mana_actions', limitations)
        if cost_facts is not None:
            _add_fact(facts, 'mana_cost', cost_facts, 'decision.mana_cost', limitations)
        if bounds_valid:
            if minimum is not None:
                _add_fact(facts, 'selection_minimum', minimum,
                          'decision.cost_selection.minimum', limitations)
            if maximum is not None:
                _add_fact(facts, 'selection_maximum', maximum,
                          'decision.cost_selection.maximum', limitations)
        choices.append(_choice(len(choices) + 1, action, facts))
    return choices


def _analyze_decision(compact_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, sourced facts without inferring legality or new choices."""
    if not isinstance(compact_snapshot, dict):
        return _result(unknowns={'partial_state'}, limitations={'malformed_snapshot'})
    integrity = compact_snapshot.get('integrity')
    decision = compact_snapshot.get('decision')
    kind_value = decision.get('kind') if isinstance(decision, dict) else None
    kind = (kind_value if isinstance(kind_value, str) and kind_value in _SUPPORTED_KINDS
            else None)
    integrity_status = integrity.get('status') if isinstance(integrity, dict) else None
    if (not isinstance(integrity, dict) or not isinstance(integrity_status, str) or
            integrity_status not in {'consistent', 'partial'}):
        return _result(kind=kind, unknowns={'partial_state'},
                       limitations={'unusable_state'})
    if not isinstance(decision, dict):
        return _result(unknowns={'missing_decision'}, limitations={'missing_decision'})
    if decision.get('source') != 'arena_presented':
        return _result(kind=kind, unknowns={'missing_decision'},
                       limitations={'unknown_choice_provenance'})
    if kind is None or kind == 'other':
        return _result(provenance='arena_presented', kind=kind,
                       unknowns={'candidate_space_not_enumerated'},
                       limitations={'unsupported_decision_kind'})

    unknowns: set[str] = set()
    limitations: set[str] = set()
    if integrity_status == 'partial' or integrity.get('limitations'):
        unknowns.add('partial_state')
    _read_reducer_limitations(decision, limitations)
    context = _context_facts(compact_snapshot, unknowns, limitations)

    if kind == 'actions':
        choices = _action_choices(decision, context, unknowns, limitations, compact_snapshot)
    elif kind == 'mulligan':
        choices = _mulligan_choices(decision, context, unknowns, limitations, compact_snapshot)
    elif kind == 'bottom_cards':
        choices = _bottom_card_choices(decision, context, unknowns, limitations)
    elif kind == 'attackers':
        choices = _attacker_choices(decision, context, unknowns, limitations)
    elif kind == 'blockers':
        choices = _blocker_choices(decision, context, unknowns, limitations)
    elif kind == 'targets':
        choices = _target_choices(decision, context, unknowns, limitations)
    else:
        choices = _payment_choices(decision, context, unknowns, limitations)

    mode = 'candidate_evidence' if kind in _CANDIDATE_KINDS else 'flat_options'
    if kind == 'bottom_cards' and decision.get('bottom_required') != 1:
        # Selecting multiple bottoms is a combination, not one independent option.
        mode = 'candidate_evidence'
        limitations.add('candidate_space_not_enumerated')
    if kind in _CANDIDATE_KINDS:
        unknowns.add('candidate_space_not_enumerated')
        limitations.add('candidate_space_not_enumerated')
    blocking_limitations = limitations.intersection(_OPTION_LIMITATIONS)
    if kind == 'actions':
        # One unfamiliar action does not invalidate the other source-listed
        # options. It remains visible as Other Arena option with mana support
        # false, so neither generation nor validation can select it.
        blocking_limitations.discard('unrecognized_action_type')
    if kind in _FLAT_KINDS and (not choices or blocking_limitations):
        mode = 'unavailable'

    status = 'partial' if unknowns or limitations else 'analyzed'
    return _result(status=status, provenance='arena_presented', kind=kind, mode=mode,
                   choices=choices, unknowns=unknowns, limitations=limitations)


def analyze_decision(compact_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return sourced facts bound to the compact public snapshot they describe."""
    result = _analyze_decision(compact_snapshot)
    return _BoundDecisionAnalysis(result, live_snapshot_fingerprint(compact_snapshot))


def fact_source_is_valid(code: Any, source: Any) -> bool:
    """Check a rendered evidence source against the analyzer's fixed mapping."""
    return (isinstance(code, str) and isinstance(source, str) and
            source in _FACT_SOURCES.get(code, ()))
