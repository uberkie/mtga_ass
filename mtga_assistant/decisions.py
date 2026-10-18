"""Sanitized, seat-specific decision options reported by Arena."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any, Callable, Mapping, Sequence

from mtga_assistant.cards import CatalogError, MALFORMED_CATALOG_FIELD


MAX_DECISION_OPTIONS = 64
MAX_DECISION_LABEL_LENGTH = 120
MAX_MANA_SYMBOLS = 16
MAX_RESPONSE_SCAN_NODES = 50_000
MAX_RESPONSE_SCAN_DEPTH = 32
MAX_CATALOG_CARDS = 128
MAX_CATALOG_FACT_BYTES = 24 * 1024
_CLIENT_GRE_ENVELOPE = 'ClientToMatchServiceMessageType_ClientToGREMessage'

_ACTION_LABELS = {
    'ActionType_Cast': 'Cast spell',
    'ActionType_Play': 'Play land',
    'ActionType_Pass': 'Pass priority',
    'ActionType_Activate_Mana': 'Activate mana ability',
    'ActionType_FloatMana': 'Float mana',
    'ActionType_Activate': 'Activate ability',
    'ActionType_OpeningHandAction': 'Opening-hand option',
}
_CARD_ACTION_TYPES = frozenset({
    'ActionType_Cast', 'ActionType_Play', 'ActionType_Activate_Mana',
    'ActionType_Activate',
})
_CATALOG_TEXT_LIMITS = {
    'mana_cost': 128,
    'type_line': 256,
    'oracle_text': 1_024,
    'ability_text': 1_024,
    'power': 64,
    'toughness': 64,
}
_MANA_COLORS = {
    'ManaColor_White': 'White',
    'ManaColor_Blue': 'Blue',
    'ManaColor_Black': 'Black',
    'ManaColor_Red': 'Red',
    'ManaColor_Green': 'Green',
    'ManaColor_Colorless': 'Colorless',
    'ManaColor_Generic': 'Generic',
    'ManaColor_X': 'X',
    'ManaColor_AnyColor': 'Any color',
}
_REQUESTS = {
    'GREMessageType_ActionsAvailableReq': ('actions', 'actionsAvailableReq', frozenset({
        'ClientMessageType_PerformActionResp',
    })),
    'GREMessageType_DeclareAttackersReq': ('attackers', 'declareAttackersReq', frozenset({
        'ClientMessageType_DeclareAttackersResp', 'ClientMessageType_SubmitAttackersReq',
    })),
    'GREMessageType_DeclareBlockersReq': ('blockers', 'declareBlockersReq', frozenset({
        'ClientMessageType_DeclareBlockersResp', 'ClientMessageType_SubmitBlockersReq',
    })),
    'GREMessageType_PayCostsReq': ('payment', 'payCostsReq', frozenset({
        'ClientMessageType_PerformAutoTapActionsResp', 'ClientMessageType_EffectCostResp',
        'ClientMessageType_PerformActionResp',
    })),
    'GREMessageType_SelectTargetsReq': ('targets', 'selectTargetsReq', frozenset({
        'ClientMessageType_SelectTargetsResp', 'ClientMessageType_SubmitTargetsReq',
    })),
    'GREMessageType_MulliganReq': ('mulligan', 'mulliganReq', frozenset({
        'ClientMessageType_MulliganResp',
    })),
    'GREMessageType_SelectNReq': ('other', 'selectNReq', frozenset({
        'ClientMessageType_SelectNResp',
    })),
    'GREMessageType_GroupReq': ('bottom_cards', 'groupReq', frozenset({
        'ClientMessageType_GroupResp',
    })),
    'GREMessageType_OrderReq': ('other', 'orderReq', frozenset({
        'ClientMessageType_OrderResp',
    })),
    'GREMessageType_AssignDamageReq': ('other', 'assignDamageReq', frozenset({
        'ClientMessageType_AssignDamageResp',
    })),
}


def _clean_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = ''.join(character for character in value if character.isprintable()).strip()
    return cleaned[:MAX_DECISION_LABEL_LENGTH] or None


def _catalog_facts(record: Any, arena_id: int, label: str,
                   limitations: set[str]) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        limitations.add('catalog_name_mismatch')
        return None
    if (type(record.get('arena_id')) is not int or record.get('arena_id') != arena_id or
            record.get('name') != label):
        limitations.add('catalog_name_mismatch')
        return None

    result: dict[str, Any] = {}
    for field, maximum in _CATALOG_TEXT_LIMITS.items():
        value = record.get(field)
        if value is None:
            continue
        if (value is MALFORMED_CATALOG_FIELD or not isinstance(value, str) or
                len(value) > maximum or any(not character.isprintable() for character in value)):
            limitations.add('catalog_partial')
            continue
        result[field] = value

    colors = record.get('colors')
    if colors is not None:
        if (colors is MALFORMED_CATALOG_FIELD or not isinstance(colors, list) or
                len(colors) > 5 or any(not isinstance(color, str) or
                                       color not in {'W', 'U', 'B', 'R', 'G'}
                                       for color in colors) or
                len(set(colors)) != len(colors)):
            limitations.add('catalog_partial')
        else:
            result['colors'] = list(colors)

    cmc = record.get('cmc')
    if cmc is not None:
        if (cmc is MALFORMED_CATALOG_FIELD or type(cmc) not in (int, float) or
                not math.isfinite(cmc) or not 0 <= cmc <= 1_000):
            limitations.add('catalog_partial')
        else:
            result['cmc'] = cmc

    faces = record.get('card_faces')
    if faces is not None:
        if faces is MALFORMED_CATALOG_FIELD or not isinstance(faces, list):
            limitations.add('catalog_partial')
        else:
            safe_faces = []
            if len(faces) > 4:
                limitations.add('catalog_partial')
            for face in faces[:4]:
                if not isinstance(face, Mapping):
                    limitations.add('catalog_partial')
                    continue
                name = face.get('name')
                if (not isinstance(name, str) or not name.strip() or len(name) > 120 or
                        any(not character.isprintable() for character in name)):
                    limitations.add('catalog_partial')
                    continue
                safe_face: dict[str, Any] = {'name': name}
                for field, maximum in _CATALOG_TEXT_LIMITS.items():
                    value = face.get(field)
                    if value is None:
                        continue
                    if (value is MALFORMED_CATALOG_FIELD or not isinstance(value, str) or
                            len(value) > maximum or
                            any(not character.isprintable() for character in value)):
                        limitations.add('catalog_partial')
                        continue
                    safe_face[field] = value
                safe_faces.append(safe_face)
            if safe_faces:
                result['card_faces'] = safe_faces
    return result or None


class _CatalogBudget:
    def __init__(self, facts_by_instance: Mapping[int, Mapping[str, Any]],
                 limitations: set[str]) -> None:
        self.facts_by_instance = facts_by_instance
        self.limitations = limitations
        self.used_bytes = 0

    def take(self, instance_id: Any) -> dict[str, Any] | None:
        if type(instance_id) is not int:
            return None
        facts = self.facts_by_instance.get(instance_id)
        if not facts:
            return None
        encoded = json.dumps(facts, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
        if len(encoded) > MAX_CATALOG_FACT_BYTES - self.used_bytes:
            self.limitations.add('catalog_partial')
            return None
        self.used_bytes += len(encoded)
        return dict(facts)


def _catalog_reference(label: str, instance_id: Any,
                       budget: _CatalogBudget | None) -> str | dict[str, Any]:
    catalog = budget.take(instance_id) if budget is not None else None
    return {'card': label, 'catalog': catalog} if catalog else label


def _request_instance_ids(kind: str, request: Any) -> list[int]:
    """Collect only the bounded card references consumed by this request sanitizer."""
    if not isinstance(request, dict):
        return []
    references: list[int] = []

    def add(value: Any) -> None:
        if type(value) is int:
            references.append(value)

    if kind == 'actions':
        actions = request.get('actions')
        if isinstance(actions, list):
            for action in actions[:MAX_DECISION_OPTIONS]:
                if (isinstance(action, dict) and isinstance(action.get('actionType'), str) and
                        action.get('actionType') in _CARD_ACTION_TYPES):
                    add(action.get('instanceId'))
    elif kind == 'attackers':
        attackers = request.get('qualifiedAttackers', request.get('attackers'))
        if isinstance(attackers, list):
            for entry in attackers[:MAX_DECISION_OPTIONS]:
                if not isinstance(entry, dict):
                    continue
                add(entry.get('attackerInstanceId'))
                recipients = entry.get('legalDamageRecipients')
                if isinstance(recipients, list):
                    for recipient in recipients[:MAX_DECISION_OPTIONS]:
                        if (isinstance(recipient, dict) and
                                recipient.get('type') == 'DamageRecType_PlanesWalker'):
                            add(recipient.get('planeswalkerInstanceId'))
    elif kind == 'blockers':
        blockers = request.get('blockers')
        if isinstance(blockers, list):
            for entry in blockers[:MAX_DECISION_OPTIONS]:
                if not isinstance(entry, dict):
                    continue
                add(entry.get('blockerInstanceId'))
                for key in ('attackerInstanceIds', 'selectedAttackerInstanceIds'):
                    values = entry.get(key)
                    if isinstance(values, list):
                        for instance_id in values[:MAX_DECISION_OPTIONS]:
                            add(instance_id)
    elif kind == 'targets':
        groups = request.get('targets')
        if isinstance(groups, list):
            for group in groups[:MAX_DECISION_OPTIONS]:
                candidates = group.get('targets') if isinstance(group, dict) else None
                if isinstance(candidates, list):
                    for candidate in candidates[:MAX_DECISION_OPTIONS]:
                        if (isinstance(candidate, dict) and candidate.get('legalAction') in
                                ('SelectAction_Select', 'SelectAction_Unselect')):
                            add(candidate.get('targetInstanceId'))
    elif kind == 'payment':
        effect_cost = request.get('effectCostReq')
        selection = (effect_cost.get('costSelection')
                     if isinstance(effect_cost, dict) and 'costSelection' in effect_cost else None)
        if not isinstance(selection, dict):
            selection = request.get('paymentSelection')
        values = selection.get('ids') if isinstance(selection, dict) else None
        if isinstance(values, list):
            for instance_id in values[:MAX_DECISION_OPTIONS]:
                add(instance_id)
    return references


def _lookup_visible_catalogs(references: Sequence[int],
                             visible_context: Mapping[int, Mapping[str, Any]],
                             card_lookup: Callable[[Sequence[int]], Mapping[int, Mapping[str, Any]]] | None,
                             limitations: set[str]) -> dict[int, dict[str, Any]]:
    if card_lookup is None:
        return {}

    instance_context: dict[int, tuple[int, str]] = {}
    ordered_arena_ids: dict[int, None] = {}
    for instance_id in references:
        context = visible_context.get(instance_id)
        if not isinstance(context, Mapping):
            continue
        arena_id, label = context.get('arena_id'), context.get('label')
        if (type(arena_id) is not int or not 1 <= arena_id <= 2**63 - 1 or
                not isinstance(label, str) or not label.strip()):
            limitations.add('catalog_partial')
            continue
        if arena_id not in ordered_arena_ids and len(ordered_arena_ids) >= MAX_CATALOG_CARDS:
            limitations.add('catalog_partial')
            continue
        ordered_arena_ids.setdefault(arena_id, None)
        instance_context[instance_id] = (arena_id, label)

    if not ordered_arena_ids:
        return {}
    try:
        records = card_lookup(tuple(ordered_arena_ids))
        if not isinstance(records, Mapping):
            raise CatalogError('Invalid card catalog result')
    except CatalogError:
        limitations.add('catalog_unavailable')
        return {}

    facts_by_instance: dict[int, dict[str, Any]] = {}
    seen_instances: set[int] = set()
    for instance_id in references:
        if instance_id in seen_instances or instance_id not in instance_context:
            continue
        seen_instances.add(instance_id)
        arena_id, label = instance_context[instance_id]
        if arena_id not in records:
            limitations.add('catalog_missing')
            continue
        facts = _catalog_facts(records[arena_id], arena_id, label, limitations)
        if facts:
            facts_by_instance[instance_id] = facts
    return facts_by_instance


def _mana_cost(value: Any) -> tuple[list[dict[str, Any]] | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, list) or len(value) > MAX_MANA_SYMBOLS:
        return None, True
    result = []
    for symbol in value:
        if not isinstance(symbol, dict):
            return None, True
        colors = symbol.get('color')
        count = symbol.get('count')
        if (not isinstance(colors, list) or not colors or len(colors) > 5 or
                any(not isinstance(color, str) or color not in _MANA_COLORS
                    for color in colors) or
                type(count) is not int or not 0 <= count <= 99):
            return None, True
        result.append({
            'colors': [_MANA_COLORS[color] for color in colors],
            'count': count,
        })
    return result, False


def _is_unmapped(name: Any) -> bool:
    return isinstance(name, str) and name.startswith('Card #')


def _visible_name(instance_id: Any, visible_cards: Mapping[int, str],
                   fallback: str = 'Unknown visible card') -> str:
    if type(instance_id) is not int:
        return fallback
    return _clean_label(visible_cards.get(instance_id)) or fallback


def _bounded_selection(selection: Any, visible_cards: Mapping[int, str],
                        limitations: set[str], *, fallback: str,
                        catalog_budget: _CatalogBudget | None = None) -> dict[str, Any] | None:
    if not isinstance(selection, dict):
        return None
    ids = selection.get('ids')
    if not isinstance(ids, list):
        limitations.add('choice_payload_unavailable')
        return None
    if len(ids) > MAX_DECISION_OPTIONS:
        ids = ids[:MAX_DECISION_OPTIONS]
        limitations.add('choice_list_truncated')
    if any(type(instance_id) is not int for instance_id in ids):
        limitations.add('choice_payload_unavailable')
        return None
    candidates = []
    unknown = 0
    for instance_id in ids:
        name = _clean_label(visible_cards.get(instance_id))
        if name:
            candidates.append(_catalog_reference(name, instance_id, catalog_budget))
        else:
            unknown += 1
    if unknown:
        limitations.add('some_choices_unavailable')
    minimum = selection.get('minSel')
    maximum = selection.get('maxSel')
    safe_min = minimum if type(minimum) is int and 0 <= minimum <= MAX_DECISION_OPTIONS else None
    safe_max = maximum if type(maximum) is int and 0 <= maximum <= MAX_DECISION_OPTIONS else None
    if minimum is not None and safe_min is None or maximum is not None and safe_max is None:
        limitations.add('choice_bounds_unavailable')
    return {
        'minimum': safe_min,
        'maximum': safe_max,
        'visible_candidates': candidates,
        'unlabeled_candidate_count': unknown,
    }


def _action_options(value: Any, visible_cards: Mapping[int, str],
                    catalog_budget: _CatalogBudget | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], ['choice_payload_unavailable']
    limitations = set()
    options = []
    for index, action in enumerate(value[:MAX_DECISION_OPTIONS], start=1):
        if not isinstance(action, dict):
            limitations.add('choice_payload_unavailable')
            continue
        action_type = action.get('actionType')
        label = _ACTION_LABELS.get(action_type) if isinstance(action_type, str) else None
        if label is None:
            label = 'Other Arena option'
            limitations.add('unrecognized_action_type')
        instance_id = action.get('instanceId')
        card_name = (_clean_label(visible_cards.get(instance_id))
                     if type(instance_id) is int else None)
        if isinstance(action_type, str) and action_type in _CARD_ACTION_TYPES and card_name is None:
            limitations.add('some_choices_unavailable')
        cost = None
        if 'manaCost' in action:
            cost, invalid_cost = _mana_cost(action.get('manaCost'))
            if invalid_cost:
                limitations.add('mana_cost_unavailable')
        option = {
            'number': index,
            'action': label,
            'card': card_name,
            'mana_cost': cost,
        }
        if (catalog_budget is not None and card_name is not None and
                isinstance(action_type, str) and action_type in _CARD_ACTION_TYPES):
            catalog = catalog_budget.take(instance_id)
            if catalog:
                option['catalog'] = catalog
        options.append(option)
    if len(value) > MAX_DECISION_OPTIONS:
        limitations.add('choice_list_truncated')
    return options, sorted(limitations)


def _damage_recipient(recipient: Any, my_seat: int,
                      visible_cards: Mapping[int, str],
                      limitations: set[str],
                      catalog_budget: _CatalogBudget | None = None) -> Any:
    if not isinstance(recipient, dict):
        limitations.add('choice_payload_unavailable')
        return None
    kind = recipient.get('type')
    if kind == 'DamageRecType_Player':
        seat = recipient.get('playerSystemSeatId')
        if type(seat) is int and seat in (1, 2):
            return 'you' if seat == my_seat else 'opponent'
    elif kind == 'DamageRecType_PlanesWalker':
        instance_id = recipient.get('planeswalkerInstanceId')
        name = _visible_name(instance_id, visible_cards, 'Unknown visible planeswalker')
        if name == 'Unknown visible planeswalker':
            limitations.add('some_choices_unavailable')
        return _catalog_reference(name, instance_id, catalog_budget)
    limitations.add('unrecognized_choice_type')
    return None


def _attacker_options(request: Any, my_seat: int,
                      visible_cards: Mapping[int, str],
                      catalog_budget: _CatalogBudget | None = None) -> tuple[dict[str, Any], set[str]]:
    limitations: set[str] = set()
    if not isinstance(request, dict):
        return {'attackers': [], 'can_submit': None}, {'choice_payload_unavailable'}
    values = request.get('qualifiedAttackers', request.get('attackers'))
    attackers = []
    if not isinstance(values, list):
        limitations.add('choice_payload_unavailable')
        values = []
    for entry in values[:MAX_DECISION_OPTIONS]:
        if not isinstance(entry, dict):
            limitations.add('choice_payload_unavailable')
            continue
        attacker_id = entry.get('attackerInstanceId')
        attacker_name = _visible_name(attacker_id, visible_cards,
                                      'Unknown visible attacker')
        attacker_catalog = (catalog_budget.take(attacker_id)
                            if catalog_budget is not None and attacker_name != 'Unknown visible attacker'
                            else None)
        recipients = entry.get('legalDamageRecipients')
        safe_recipients = []
        if not isinstance(recipients, list):
            limitations.add('choice_payload_unavailable')
        else:
            for recipient in recipients[:MAX_DECISION_OPTIONS]:
                label = _damage_recipient(recipient, my_seat, visible_cards, limitations,
                                          catalog_budget)
                if label is not None and label not in safe_recipients:
                    safe_recipients.append(label)
            if len(recipients) > MAX_DECISION_OPTIONS:
                limitations.add('choice_list_truncated')
        if attacker_name == 'Unknown visible attacker' or _is_unmapped(attacker_name):
            limitations.add('some_choices_unavailable')
        attacker = {
            'card': attacker_name,
            'legal_recipients': safe_recipients,
        }
        if attacker_catalog:
            attacker['catalog'] = attacker_catalog
        attackers.append(attacker)
    if len(values) > MAX_DECISION_OPTIONS:
        limitations.add('choice_list_truncated')
    can_submit = request.get('canSubmitAttackers')
    if can_submit is not None and type(can_submit) is not bool:
        limitations.add('choice_payload_unavailable')
        can_submit = None
    result = {'attackers': attackers, 'can_submit': can_submit}
    if 'manaCost' in request:
        mana_cost, invalid = _mana_cost(request.get('manaCost'))
        result['mana_cost'] = mana_cost
        if invalid:
            limitations.add('mana_cost_unavailable')
    return result, limitations


def _blocker_options(request: Any, visible_cards: Mapping[int, str],
                     catalog_budget: _CatalogBudget | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    limitations: set[str] = set()
    if not isinstance(request, dict) or not isinstance(request.get('blockers'), list):
        return [], {'choice_payload_unavailable'}
    blockers = request['blockers']
    result = []
    for entry in blockers[:MAX_DECISION_OPTIONS]:
        if not isinstance(entry, dict):
            limitations.add('choice_payload_unavailable')
            continue
        blocker_id = entry.get('blockerInstanceId')
        blocker_name = _visible_name(blocker_id, visible_cards,
                                     'Unknown visible blocker')
        if blocker_name == 'Unknown visible blocker' or _is_unmapped(blocker_name):
            limitations.add('some_choices_unavailable')
        blocker_catalog = (catalog_budget.take(blocker_id)
                           if catalog_budget is not None and blocker_name != 'Unknown visible blocker'
                           else None)
        legal = entry.get('attackerInstanceIds')
        selected = entry.get('selectedAttackerInstanceIds', [])
        if not isinstance(legal, list) or not isinstance(selected, list):
            limitations.add('choice_payload_unavailable')
            legal = legal if isinstance(legal, list) else []
            selected = selected if isinstance(selected, list) else []
        if len(legal) > MAX_DECISION_OPTIONS or len(selected) > MAX_DECISION_OPTIONS:
            limitations.add('choice_list_truncated')
        legal_labels = []
        for instance_id in legal[:MAX_DECISION_OPTIONS]:
            if type(instance_id) is not int:
                limitations.add('choice_payload_unavailable')
                continue
            if instance_id not in visible_cards or _is_unmapped(visible_cards.get(instance_id)):
                limitations.add('some_choices_unavailable')
            label = _catalog_reference(
                _visible_name(instance_id, visible_cards, 'Unknown visible attacker'),
                instance_id, catalog_budget)
            if label not in legal_labels:
                legal_labels.append(label)
        selected_labels = []
        for instance_id in selected[:MAX_DECISION_OPTIONS]:
            if type(instance_id) is not int:
                limitations.add('choice_payload_unavailable')
                continue
            if instance_id not in visible_cards or _is_unmapped(visible_cards.get(instance_id)):
                limitations.add('some_choices_unavailable')
            label = _catalog_reference(
                _visible_name(instance_id, visible_cards, 'Unknown visible attacker'),
                instance_id, catalog_budget)
            if label not in selected_labels:
                selected_labels.append(label)
        maximum = entry.get('maxAttackers')
        if maximum is not None and (type(maximum) is not int or
                                    not 0 <= maximum <= MAX_DECISION_OPTIONS):
            limitations.add('choice_bounds_unavailable')
            maximum = None
        blocker = {
            'card': blocker_name,
            'legal_attackers': legal_labels,
            'selected_attackers': selected_labels,
            'max_attackers': maximum,
        }
        if blocker_catalog:
            blocker['catalog'] = blocker_catalog
        result.append(blocker)
    if len(blockers) > MAX_DECISION_OPTIONS:
        limitations.add('choice_list_truncated')
    return result, limitations


def _target_options(request: Any, visible_cards: Mapping[int, str],
                    catalog_budget: _CatalogBudget | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    limitations: set[str] = set()
    if not isinstance(request, dict) or not isinstance(request.get('targets'), list):
        return [], {'choice_payload_unavailable'}
    targets = request['targets']
    result = []
    for target_group in targets[:MAX_DECISION_OPTIONS]:
        if not isinstance(target_group, dict):
            limitations.add('choice_payload_unavailable')
            continue
        candidates = target_group.get('targets')
        if not isinstance(candidates, list):
            limitations.add('choice_payload_unavailable')
            candidates = []
        safe_candidates = []
        for candidate in candidates[:MAX_DECISION_OPTIONS]:
            if not isinstance(candidate, dict):
                limitations.add('choice_payload_unavailable')
                continue
            legal_action = candidate.get('legalAction')
            action = {
                'SelectAction_Select': 'select',
                'SelectAction_Unselect': 'unselect',
            }.get(legal_action) if isinstance(legal_action, str) else None
            if action is None:
                limitations.add('unrecognized_choice_action')
                continue
            instance_id = candidate.get('targetInstanceId')
            if type(instance_id) is not int:
                limitations.add('choice_payload_unavailable')
            elif instance_id not in visible_cards:
                limitations.add('some_choices_unavailable')
            safe_candidate = {
                'card': _visible_name(instance_id, visible_cards, 'Unknown visible target'),
                'action': action,
            }
            if catalog_budget is not None and type(instance_id) is int and instance_id in visible_cards:
                catalog = catalog_budget.take(instance_id)
                if catalog:
                    safe_candidate['catalog'] = catalog
            safe_candidates.append(safe_candidate)
        minimum = target_group.get('minTargets')
        maximum = target_group.get('maxTargets')
        selected = target_group.get('selectedTargets', 0)
        def bounded(value: Any) -> int | None:
            return value if type(value) is int and 0 <= value <= MAX_DECISION_OPTIONS else None
        safe_min, safe_max, safe_selected = bounded(minimum), bounded(maximum), bounded(selected)
        if ((minimum is not None and safe_min is None) or
                (maximum is not None and safe_max is None) or safe_selected is None):
            limitations.add('choice_bounds_unavailable')
        if len(candidates) > MAX_DECISION_OPTIONS:
            limitations.add('choice_list_truncated')
        result.append({
            'minimum': safe_min,
            'maximum': safe_max,
            'selected': safe_selected,
            'candidates': safe_candidates,
        })
    if len(targets) > MAX_DECISION_OPTIONS:
        limitations.add('choice_list_truncated')
    return result, limitations


def _payment_options(request: Any, visible_cards: Mapping[int, str],
                     catalog_budget: _CatalogBudget | None = None) -> tuple[dict[str, Any], set[str]]:
    limitations: set[str] = set()
    if not isinstance(request, dict):
        return {'mana_cost': None, 'cost_selection': None, 'mana_actions': []}, {'choice_payload_unavailable'}
    mana_cost = None
    if 'manaCost' in request:
        mana_cost, invalid_cost = _mana_cost(request.get('manaCost'))
        if invalid_cost:
            limitations.add('mana_cost_unavailable')
    cost_selection = None
    effect_cost = request.get('effectCostReq')
    if isinstance(effect_cost, dict) and 'costSelection' in effect_cost:
        cost_selection = _bounded_selection(effect_cost.get('costSelection'), visible_cards,
                                            limitations, fallback='Unknown cost candidate',
                                            catalog_budget=catalog_budget)
    payment_selection = request.get('paymentSelection')
    if cost_selection is None and isinstance(payment_selection, dict):
        cost_selection = _bounded_selection(payment_selection, visible_cards,
                                            limitations, fallback='Unknown cost candidate',
                                            catalog_budget=catalog_budget)
    actions = []
    payment = request.get('paymentActions')
    raw_actions = payment.get('actions') if isinstance(payment, dict) else None
    if raw_actions is not None:
        if not isinstance(raw_actions, list):
            limitations.add('choice_payload_unavailable')
        else:
            seen = set()
            for entry in raw_actions[:MAX_DECISION_OPTIONS]:
                if not isinstance(entry, dict):
                    limitations.add('choice_payload_unavailable')
                    continue
                action_type = entry.get('actionType')
                label = (_ACTION_LABELS.get(action_type)
                         if isinstance(action_type, str) else None) or 'Other payment option'
                if label == 'Other payment option':
                    limitations.add('unrecognized_action_type')
                if label not in seen:
                    seen.add(label)
                    actions.append(label)
            if len(raw_actions) > MAX_DECISION_OPTIONS:
                limitations.add('choice_list_truncated')
    return {'mana_cost': mana_cost, 'cost_selection': cost_selection,
            'mana_actions': actions}, limitations


def _mulligan_options(request: Any) -> tuple[dict[str, Any], set[str]]:
    limitations: set[str] = set()
    if not isinstance(request, dict):
        return {'mulligan_type': None, 'mulligans_taken': None,
                'options': ['Keep hand', 'Take a mulligan']}, {'choice_payload_unavailable'}
    raw_type = request.get('mulliganType', request.get('mulligan_type'))
    mulligan_type = 'London' if raw_type == 'MulliganType_London' else None
    if raw_type is not None and mulligan_type is None:
        limitations.add('unrecognized_choice_type')
    count = request.get('mulliganCount')
    if type(count) is not int or not 0 <= count <= 10:
        if count is not None:
            limitations.add('choice_bounds_unavailable')
        count = None
    return {'mulligan_type': mulligan_type, 'mulligans_taken': count,
            'options': ['Keep hand', 'Take a mulligan']}, limitations


def _bottom_cards_options(request: Any, visible_cards: Mapping[int, str]) -> tuple[dict[str, Any], set[str]]:
    """Only enumerate the cards in an actual London mulligan grouping request.

    These are eligible *individual* bottom candidates, not necessarily legal
    complete combinations. For 2+ required bottoms the AI must not rank them
    as complete alternative actions.
    """
    limitations: set[str] = set()
    if not isinstance(request, dict) or request.get('context') != 'GroupingContext_LondonMulligan':
        return {'options': [], 'bottom_required': None}, {'details_not_supported'}
    ids = request.get('instanceIds')
    specs = request.get('groupSpecs')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_DECISION_OPTIONS or not isinstance(specs, list):
        return {'options': [], 'bottom_required': None}, {'choice_payload_unavailable'}
    bottom = [s for s in specs if isinstance(s, dict) and
              s.get('zoneType') == 'ZoneType_Library' and
              s.get('subZoneType') == 'SubZoneType_Bottom']
    if (len(bottom) != 1 or type(bottom[0].get('lowerBound')) is not int or
            type(bottom[0].get('upperBound')) is not int or
            bottom[0]['lowerBound'] != bottom[0]['upperBound'] or
            not 0 <= bottom[0]['lowerBound'] <= len(ids)):
        return {'options': [], 'bottom_required': None}, {'choice_bounds_unavailable'}
    required = bottom[0]['lowerBound']
    seen_instances: set[int] = set()
    duplicates: dict[str, int] = {}
    cards = []
    for instance in ids:
        if type(instance) is not int or instance <= 0 or instance in seen_instances:
            limitations.add('choice_payload_unavailable')
            continue
        seen_instances.add(instance)
        # The request IDs are not themselves evidence that a hidden card's
        # identity is available. Only use names from the public projection.
        name = visible_cards.get(instance)
        if not isinstance(name, str) or not name.strip():
            name = 'Unknown visible card'
        if _is_unmapped(name) or name == 'Unknown visible card':
            limitations.add('some_choices_unavailable')
        duplicates[name] = duplicates.get(name, 0) + 1
        cards.append({'card': name, 'copy': duplicates[name]})
    if len(cards) != len(ids):
        limitations.add('choice_payload_unavailable')
    return {'options': cards, 'bottom_required': required}, limitations


class DecisionTracker:
    """Keep at most one current Arena-presented option set."""

    _REQUEST_TYPES = frozenset(_REQUESTS)

    def __init__(self) -> None:
        self._current: dict[str, Any] | None = None

    @classmethod
    def is_request_type(cls, value: Any) -> bool:
        return isinstance(value, str) and value in cls._REQUEST_TYPES

    def clear(self) -> bool:
        changed = self._current is not None
        self._current = None
        return changed

    def expire_for_state(self, state_id: int | None, *, full: bool = False) -> bool:
        if full or (self._current is not None and self._current['_game_state_id'] != state_id):
            return self.clear()
        return False

    def observe_request(self, message: dict[str, Any], *, my_seat: int,
                        current_state_id: int | None,
                        visible_cards: Mapping[int, str],
                        visible_context: Mapping[int, Mapping[str, Any]] | None = None,
                        card_lookup: Callable[[Sequence[int]], Mapping[int, Mapping[str, Any]]] | None = None) -> bool:
        request_type = message.get('type')
        if not isinstance(request_type, str) or request_type not in _REQUESTS:
            return False
        kind, body_name, response_types = _REQUESTS[request_type]
        if (request_type == 'GREMessageType_GroupReq' and
                (not isinstance(message.get('groupReq'), dict) or
                 message['groupReq'].get('context') != 'GroupingContext_LondonMulligan')):
            kind = 'other'  # Other grouping requests are not bottom-card decisions.
        game_state_id = message.get('gameStateId')
        message_id = message.get('msgId')
        seats = message.get('systemSeatIds')
        valid_seats = (isinstance(seats, list) and bool(seats) and
                       all(type(seat) is int and seat in (1, 2) for seat in seats))
        if not valid_seats or my_seat not in seats:
            return False
        if (type(game_state_id) is not int or game_state_id <= 0 or
                game_state_id != current_state_id):
            return False
        if type(message_id) is not int or message_id <= 0:
            return self.clear()

        request = message.get(body_name)
        limitations: set[str] = set()
        catalog_facts = _lookup_visible_catalogs(
            _request_instance_ids(kind, request), visible_context or {}, card_lookup, limitations)
        catalog_budget = _CatalogBudget(catalog_facts, limitations) if card_lookup is not None else None
        if kind == 'actions':
            options, action_limitations = _action_options(
                request.get('actions') if isinstance(request, dict) else None,
                visible_cards, catalog_budget)
            limitations.update(action_limitations)
            public = {'kind': kind, 'options': options}
        elif kind == 'attackers':
            public, action_limitations = _attacker_options(
                request, my_seat, visible_cards, catalog_budget)
            limitations.update(action_limitations)
            public['kind'] = kind
        elif kind == 'blockers':
            blockers, action_limitations = _blocker_options(request, visible_cards, catalog_budget)
            limitations.update(action_limitations)
            public = {'kind': kind, 'blockers': blockers}
        elif kind == 'payment':
            public, action_limitations = _payment_options(request, visible_cards, catalog_budget)
            limitations.update(action_limitations)
            public['kind'] = kind
        elif kind == 'targets':
            groups, action_limitations = _target_options(request, visible_cards, catalog_budget)
            limitations.update(action_limitations)
            public = {'kind': kind, 'groups': groups}
        elif kind == 'mulligan':
            public, limitations = _mulligan_options(request)
        elif kind == 'bottom_cards':
            public, limitations = _bottom_cards_options(request, visible_cards)
            public['kind'] = kind
        else:
            public = {'kind': 'other'}
            limitations.add('details_not_supported')
        public = {'source': 'arena_presented', **public,
                  'limitations': sorted(limitations)}
        candidate = {
            '_game_state_id': game_state_id,
            '_message_id': message_id,
            '_request_type': request_type,
            '_response_types': response_types,
            'public': public,
        }
        if candidate == self._current:
            return False
        self._current = candidate
        return True

    def observe_response(self, message: Any) -> bool:
        if self._current is None or not isinstance(message, dict):
            return False
        response_type = message.get('type')
        if (not isinstance(response_type, str) or
                response_type not in self._current['_response_types'] or
                type(message.get('respId')) is not int or
                message.get('respId') != self._current['_message_id'] or
                type(message.get('gameStateId')) is not int or
                message.get('gameStateId') != self._current['_game_state_id']):
            return False
        return self.clear()

    def observe_client_envelopes(self, payload: Any) -> bool:
        """Find bounded client-to-GRE response envelopes without retaining their payloads."""
        stack = [(payload, 0)]
        visited = set()
        nodes = 0
        changed = False
        while stack and nodes < MAX_RESPONSE_SCAN_NODES:
            value, depth = stack.pop()
            if not isinstance(value, (dict, list)) or id(value) in visited:
                continue
            visited.add(id(value))
            nodes += 1
            if isinstance(value, dict):
                if value.get('clientToMatchServiceMessageType') == _CLIENT_GRE_ENVELOPE:
                    changed |= self.observe_response(value.get('payload'))
                    continue
                if depth < MAX_RESPONSE_SCAN_DEPTH:
                    stack.extend((child, depth + 1) for child in value.values()
                                 if isinstance(child, (dict, list)))
            elif depth < MAX_RESPONSE_SCAN_DEPTH:
                stack.extend((child, depth + 1) for child in value[:4096]
                             if isinstance(child, (dict, list)))
        return changed

    def public_snapshot(self) -> dict[str, Any] | None:
        return deepcopy(self._current['public']) if self._current is not None else None
