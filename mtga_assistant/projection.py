"""Bounded, identifier-free projections for local live-state analysis."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


_PROTOCOL_ID_KEYS = frozenset({
    'id', 'zoneid', 'instanceid', 'objectid', 'grpid', 'groupid', 'msgid',
    'respid', 'gamestateid', 'matchid', 'accountid', 'promptid',
})
_CATALOG_TEXT_LIMITS = {
    'mana_cost': 128,
    'type_line': 256,
    'oracle_text': 1_024,
    'ability_text': 1_024,
    'power': 64,
    'toughness': 64,
}
_CATALOG_FIELD_ORDER = (
    'mana_cost', 'type_line', 'colors', 'cmc', 'power', 'toughness',
    'oracle_text', 'ability_text', 'card_faces',
)
_CATALOG_COLORS = frozenset({'W', 'U', 'B', 'R', 'G'})
_MAX_CATALOG_FACT_BYTES = 24 * 1024


def _safe_catalog(value: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        state['partial'] = True
        return None
    candidates: dict[str, Any] = {}
    for field in _CATALOG_FIELD_ORDER:
        if field not in value or value[field] is None:
            continue
        field_value = value[field]
        if field in _CATALOG_TEXT_LIMITS:
            maximum = _CATALOG_TEXT_LIMITS[field]
            if (not isinstance(field_value, str) or len(field_value) > maximum or
                    any(not character.isprintable() for character in field_value)):
                state['partial'] = True
                continue
            candidates[field] = field_value
        elif field == 'colors':
            if (not isinstance(field_value, list) or len(field_value) > 5 or
                    any(not isinstance(color, str) or color not in _CATALOG_COLORS
                        for color in field_value) or
                    len(set(field_value)) != len(field_value)):
                state['partial'] = True
                continue
            candidates[field] = list(field_value)
        elif field == 'cmc':
            if (type(field_value) not in (int, float) or
                    not 0 <= field_value <= 1_000 or not math.isfinite(field_value)):
                state['partial'] = True
                continue
            candidates[field] = field_value
        else:
            if not isinstance(field_value, list):
                state['partial'] = True
                continue
            if len(field_value) > 4:
                state['partial'] = True
            faces = []
            for face in field_value[:4]:
                if not isinstance(face, dict):
                    state['partial'] = True
                    continue
                if any(key not in {'name', *_CATALOG_TEXT_LIMITS} for key in face):
                    state['partial'] = True
                name = face.get('name')
                if (not isinstance(name, str) or not name.strip() or len(name) > 120 or
                        any(not character.isprintable() for character in name)):
                    state['partial'] = True
                    continue
                safe_face: dict[str, Any] = {'name': name}
                for face_field, maximum in _CATALOG_TEXT_LIMITS.items():
                    if face_field not in face or face[face_field] is None:
                        continue
                    text = face[face_field]
                    if (not isinstance(text, str) or len(text) > maximum or
                            any(not character.isprintable() for character in text)):
                        state['partial'] = True
                        continue
                    safe_face[face_field] = text
                faces.append(safe_face)
            if faces:
                candidates[field] = faces

    result: dict[str, Any] = {}
    for field in _CATALOG_FIELD_ORDER:
        if field not in candidates:
            continue
        candidate = {**result, field: candidates[field]}
        candidate_size = len(json.dumps(candidate, ensure_ascii=False, allow_nan=False,
                                        separators=(',', ':')).encode('utf-8'))
        previous_size = len(json.dumps(result, ensure_ascii=False, allow_nan=False,
                                       separators=(',', ':')).encode('utf-8')) if result else 0
        delta = candidate_size - previous_size
        if delta > _MAX_CATALOG_FACT_BYTES - state['used']:
            state['partial'] = True
            continue
        state['used'] += delta
        result = candidate
    return result or None


def _remove_protocol_ids(value: Any, depth: int = 0,
                         catalog_state: dict[str, Any] | None = None) -> Any:
    if catalog_state is None:
        catalog_state = {'used': 0, 'partial': False}
    if depth > 16:
        return None
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:512]:
            if not isinstance(key, str) or key.lower().replace('_', '') in _PROTOCOL_ID_KEYS:
                continue
            if key == 'catalog':
                safe_catalog = _safe_catalog(child, catalog_state)
                if safe_catalog is not None:
                    result[key] = safe_catalog
                continue
            result[key] = _remove_protocol_ids(child, depth + 1, catalog_state)
        return result
    if isinstance(value, list):
        return [_remove_protocol_ids(child, depth + 1, catalog_state) for child in value[:64]]
    if isinstance(value, str):
        clean = ''.join(ch for ch in value if ch.isprintable()).strip()[:400]
        if re.fullmatch(r'Card #[0-9]+', clean):
            return 'Unknown visible card'
        return clean
    if value is None or type(value) in (bool, int, float):
        return value
    return None


def compact_live_snapshot(snapshot: Any) -> dict[str, Any]:
    """Allowlist live state, strip protocol identifiers, and bound nested data."""
    if not isinstance(snapshot, dict):
        raise ValueError('Invalid live snapshot')
    allowed = {
        'revision', 'integrity', 'my_seat', 'game_number', 'turn_number', 'phase', 'step',
        'active_player', 'decision_player', 'priority_player', 'game_stage',
        'match_status', 'life_totals', 'zones', 'decision', 'lifecycle',
        'mulligans_taken', 'bottom_required', 'opening_evaluation',
    }
    compact = {key: value for key, value in snapshot.items() if key in allowed}
    catalog_state = {'used': 0, 'partial': False}
    clean = _remove_protocol_ids(compact, catalog_state=catalog_state)
    decision = clean.get('decision')
    if (not isinstance(decision, dict) or decision.get('source') != 'arena_presented' or
            not isinstance(decision.get('kind'), str) or
            decision.get('kind') not in {'actions', 'attackers', 'blockers', 'payment',
                                         'targets', 'mulligan', 'bottom_cards', 'other'}):
        clean['decision'] = None
    else:
        clean['decision'] = {
            key: value for key, value in decision.items()
            if key in {'source', 'kind', 'options', 'attackers', 'can_submit', 'mana_cost',
                       'blockers', 'cost_selection', 'mana_actions', 'groups',
                       'mulligan_type', 'mulligans_taken', 'bottom_required', 'limitations'}
        }
        if catalog_state['partial']:
            limitations = clean['decision'].get('limitations')
            if isinstance(limitations, list) and 'catalog_partial' not in limitations:
                limitations.append('catalog_partial')
    return clean


def live_snapshot_fingerprint(snapshot: Any) -> str | None:
    """Return a stable binding for a bounded public snapshot, if serializable."""
    try:
        compact = compact_live_snapshot(snapshot)
        encoded = json.dumps(compact, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(encoded) > 1024 * 1024:
            return None
        return hashlib.sha256(encoded).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
