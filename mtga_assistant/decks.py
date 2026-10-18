"""Offline Arena deck parsing, catalog-based audits and collection accounting."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any

from mtga_assistant.cards import (
    get_catalog_metadata, list_format_card_printings, search_card_printings,
)


FORMATS = frozenset({'standard', 'alchemy', 'explorer', 'historic', 'timeless'})
RARITIES = ('common', 'uncommon', 'rare', 'mythic')
COLORS = ('W', 'U', 'B', 'R', 'G')
CARD_TYPES = frozenset({
    'artifact', 'battle', 'creature', 'enchantment', 'instant', 'kindred',
    'land', 'planeswalker', 'sorcery', 'tribal',
})
MAX_DECK_CHARS = 1_000_000
MAX_DECK_ROWS = 5_000
MAX_COLLECTION_CHARS = 1_000_000
MAX_COLLECTION_ROWS = 100_000
MAX_COUNT = 1_000_000
MAX_REPORT_BYTES = 4 * 1024 * 1024
SUGGESTION_GOALS = frozenset({'curve', 'interaction', 'card_advantage', 'ramp', 'threats', 'synergy'})
MAX_SUGGESTION_LIMIT = 20
MAX_SUGGESTION_COPIES = 4
_DECK_LINE = re.compile(
    r'^(?P<count>[1-9][0-9]{0,2})\s+(?P<name>.+?)'
    r'(?:\s+\((?P<set>[A-Za-z0-9]{1,5})\)\s+(?P<collector>\S{1,32}))?$'
)
_POSITIVE_KEY = re.compile(r'[1-9][0-9]{0,18}')


class DeckInputError(ValueError):
    """A content-free deck or collection input error."""


@dataclass(frozen=True)
class DeckEntry:
    name: str
    count: int
    section: str
    set_code: str | None = None
    collector_number: str | None = None


@dataclass(frozen=True)
class DeckList:
    entries: tuple[DeckEntry, ...]

    @property
    def main_count(self) -> int:
        return sum(entry.count for entry in self.entries if entry.section == 'main')

    @property
    def sideboard_count(self) -> int:
        return sum(entry.count for entry in self.entries if entry.section == 'sideboard')


def parse_decklist(text: str) -> DeckList:
    if not isinstance(text, str) or len(text) > MAX_DECK_CHARS:
        raise DeckInputError('Invalid deck list')
    section = 'main'
    merged: dict[tuple[str, str, str | None, str | None], int] = {}
    display_names: dict[tuple[str, str, str | None, str | None], str] = {}
    order: list[tuple[str, str, str | None, str | None]] = []
    rows = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or line.startswith('//'):
            continue
        header = line.casefold().rstrip(':')
        if header in ('deck', 'main', 'main deck', 'maindeck'):
            section = 'main'
            continue
        if header == 'sideboard':
            section = 'sideboard'
            continue
        match = _DECK_LINE.fullmatch(line)
        if not match:
            raise DeckInputError('Invalid deck list row')
        count = int(match.group('count'))
        name = match.group('name').strip()
        set_code = match.group('set')
        collector = match.group('collector')
        if (count < 1 or count > 250 or not name or len(name) > 128 or
                any(not ch.isprintable() for ch in name)):
            raise DeckInputError('Invalid deck list row')
        if set_code is not None:
            set_code = set_code.lower()
            if len(collector) > 32 or any(not ch.isprintable() for ch in collector):
                raise DeckInputError('Invalid deck list row')
        key = (section, name.casefold(), set_code, collector)
        if key not in merged:
            order.append(key)
            merged[key] = 0
            display_names[key] = name
            rows += 1
            if rows > MAX_DECK_ROWS:
                raise DeckInputError('Deck list has too many rows')
        merged[key] += count
        if merged[key] > MAX_COUNT:
            raise DeckInputError('Invalid deck list count')
    if not order:
        raise DeckInputError('Deck list is empty')
    entries = tuple(DeckEntry(
        name=display_names[key], count=merged[key], section=key[0],
        set_code=key[2], collector_number=key[3],
    ) for key in order)
    return DeckList(entries)


def parse_collection_json(text: str) -> dict[str, dict[Any, int]]:
    if not isinstance(text, str) or len(text) > MAX_COLLECTION_CHARS:
        raise DeckInputError('Invalid collection file')
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        raise DeckInputError('Invalid collection file') from None
    if not isinstance(value, dict):
        raise DeckInputError('Invalid collection file')
    cards_raw, wildcards_raw = value.get('cards', {}), value.get('wildcards', {})
    if not isinstance(cards_raw, dict) or len(cards_raw) > MAX_COLLECTION_ROWS:
        raise DeckInputError('Invalid collection file')
    cards: dict[int, int] = {}
    for key, count in cards_raw.items():
        if (not isinstance(key, str) or _POSITIVE_KEY.fullmatch(key) is None or
                len(key) > 19 or int(key) > 2**63 - 1 or type(count) is not int or
                count < 0 or count > MAX_COUNT):
            raise DeckInputError('Invalid collection file')
        cards[int(key)] = count
    if not isinstance(wildcards_raw, dict) or any(key not in RARITIES for key in wildcards_raw):
        raise DeckInputError('Invalid collection file')
    wildcards: dict[str, int] = {rarity: 0 for rarity in RARITIES}
    for rarity, count in wildcards_raw.items():
        if type(count) is not int or count < 0 or count > MAX_COUNT:
            raise DeckInputError('Invalid collection file')
        wildcards[rarity] = count
    return {'cards': cards, 'wildcards': wildcards}


def load_decklist(path: Path) -> DeckList:
    path = Path(path)
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_DECK_CHARS + 1)
        if len(raw) > MAX_DECK_CHARS:
            raise DeckInputError('Deck list is too large')
        return parse_decklist(raw.decode('utf-8-sig'))
    except DeckInputError:
        raise
    except (OSError, UnicodeError):
        raise DeckInputError('Unable to read deck list') from None


def load_collection(path: Path) -> dict[str, dict[Any, int]]:
    path = Path(path)
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_COLLECTION_CHARS + 1)
        if len(raw) > MAX_COLLECTION_CHARS:
            raise DeckInputError('Collection file is too large')
        return parse_collection_json(raw.decode('utf-8-sig'))
    except DeckInputError:
        raise
    except (OSError, UnicodeError):
        raise DeckInputError('Unable to read collection file') from None


def _issue(kind: str, **fields: Any) -> dict[str, Any]:
    return {'kind': kind, **fields}


def audit_decklist(deck_text: str | DeckList, format_name: str, catalog_path: Path,
                   collection: dict[str, dict[Any, int]] | None = None) -> dict[str, Any]:
    if not isinstance(format_name, str) or format_name.casefold() not in FORMATS:
        raise DeckInputError('Unsupported format')
    fmt = format_name.casefold()
    deck = parse_decklist(deck_text) if isinstance(deck_text, str) else deck_text
    if not isinstance(deck, DeckList) or not deck.entries:
        raise DeckInputError('Invalid deck list')
    if collection is not None:
        if (not isinstance(collection, dict) or not isinstance(collection.get('cards'), dict) or
                not isinstance(collection.get('wildcards'), dict)):
            raise DeckInputError('Invalid collection data')
        cards_inventory, wildcard_inventory = collection['cards'], collection['wildcards']
        if (any(type(arena_id) is not int or arena_id <= 0 or arena_id > 2**63 - 1 or
                type(count) is not int or count < 0 or count > MAX_COUNT
                for arena_id, count in cards_inventory.items()) or
                any(not isinstance(rarity, str) or rarity not in RARITIES or
                    type(count) is not int or count < 0 or count > MAX_COUNT
                    for rarity, count in wildcard_inventory.items())):
            raise DeckInputError('Invalid collection data')
    metadata = get_catalog_metadata(catalog_path)
    issues: list[dict[str, Any]] = []
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    arena_totals: dict[int, int] = {}
    by_name: dict[str, dict[str, Any]] = {}
    resolution_cache: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = {}
    card_by_id: dict[int, dict[str, Any]] = {}
    composition = {
        'color_counts': {color: 0 for color in COLORS},
        'mana_curve': {**{str(i): 0 for i in range(6)}, '6+': 0, 'unknown': 0},
        'type_counts': {}, 'unknown_mana_value_copies': 0,
        'unknown_color_copies': 0, 'unknown_type_copies': 0,
    }

    for entry in deck.entries:
        search_key = (entry.name.casefold(), entry.set_code, entry.collector_number)
        if search_key not in resolution_cache:
            resolution_cache[search_key] = search_card_printings(
                catalog_path, entry.name, entry.set_code, entry.collector_number)
        printings = resolution_cache[search_key]
        card = printings[0] if len(printings) == 1 else None
        if card is None:
            legality = 'unknown'
            kind = 'ambiguous_card_printing' if len(printings) > 1 else 'card_not_found'
            issues.append(_issue(kind, card_name=entry.name, count=entry.count))
            group_key = ('unknown', entry.name.casefold(), entry.set_code, entry.collector_number)
            resolved_name = entry.name
            rarity = None
            type_line = None
            cmc = None
            colors: list[str] = []
        else:
            arena_id = card['arena_id']
            card_by_id[arena_id] = card
            arena_totals[arena_id] = arena_totals.get(arena_id, 0) + entry.count
            group_key = ('id', arena_id)
            resolved_name = card['name']
            rarity = card['rarity']
            type_line = card['type_line']
            cmc = card['cmc']
            colors = card['colors']
            status = card['legalities'].get(fmt)
            if status == 'legal':
                legality = 'legal'
            elif status in ('not_legal', 'banned'):
                legality = 'not_legal'
                issues.append(_issue('card_not_legal', card_name=resolved_name, format=fmt))
            elif status == 'restricted':
                legality = 'restricted'
            else:
                legality = 'unknown'
                issues.append(_issue('legality_unknown', card_name=resolved_name, format=fmt))

        group = grouped.get(group_key)
        if group is None:
            group = {
                'name': resolved_name, 'set': card['set'] if card else None,
                'collector_number': card['collector_number'] if card else None,
                'rarity': rarity, 'legality': legality, 'main_count': 0,
                'sideboard_count': 0, 'type_line': type_line, 'cmc': cmc,
                'colors': colors, 'basic_land': bool(type_line and 'basic land' in type_line.casefold()),
                'arena_id': card['arena_id'] if card else None,
            }
            grouped[group_key] = group
        section_key = 'main_count' if entry.section == 'main' else 'sideboard_count'
        group[section_key] += entry.count

        if entry.section == 'main':
            if card is None:
                composition['unknown_mana_value_copies'] += entry.count
                composition['unknown_color_copies'] += entry.count
                composition['unknown_type_copies'] += entry.count
            else:
                valid_colors = [color for color in colors if isinstance(color, str) and color in COLORS]
                for color in valid_colors:
                    composition['color_counts'][color] += entry.count
                if not isinstance(cmc, (int, float)) or type(cmc) is bool or not math.isfinite(cmc) or cmc < 0:
                    composition['unknown_mana_value_copies'] += entry.count
                    composition['mana_curve']['unknown'] += entry.count
                elif float(cmc).is_integer():
                    bucket = '6+' if cmc >= 6 else str(int(cmc))
                    composition['mana_curve'][bucket] += entry.count
                else:
                    composition['unknown_mana_value_copies'] += entry.count
                    composition['mana_curve']['unknown'] += entry.count
                if not valid_colors and colors:
                    composition['unknown_color_copies'] += entry.count
                types = _known_card_types(type_line)
                if types:
                    for card_type in types:
                        composition['type_counts'][card_type] = composition['type_counts'].get(card_type, 0) + entry.count
                else:
                    composition['unknown_type_copies'] += entry.count

        if card is not None:
            by_name.setdefault(card['name'].casefold(), {
                'name': card['name'], 'count': 0, 'basic_land': group['basic_land'],
                'legality': legality, 'arena_id': card['arena_id'],
            })['count'] += entry.count

    main_count = deck.main_count
    sideboard_count = deck.sideboard_count
    if main_count < 60:
        issues.append(_issue('main_deck_below_minimum', count=main_count, minimum=60))
    if sideboard_count > 15:
        issues.append(_issue('sideboard_over_maximum', count=sideboard_count, maximum=15))
    for card in by_name.values():
        if card['basic_land']:
            continue
        maximum = 1 if card['legality'] == 'restricted' else 4
        if card['count'] > maximum:
            kind = 'restricted_copy_limit_exceeded' if maximum == 1 else 'copy_limit_exceeded'
            issues.append(_issue(kind, card_name=card['name'], count=card['count'], maximum=maximum))

    collection_result = None
    if collection is not None:
        wildcards = collection['wildcards']
        needs = {rarity: 0 for rarity in RARITIES}
        unknown_rarity_missing = 0
        missing_copies = 0
        unresolved_missing = 0
        for arena_id, required in arena_totals.items():
            card = card_by_id.get(arena_id)
            if card is None:
                continue
            owned = collection['cards'].get(arena_id, 0)
            missing = max(0, required - owned)
            missing_copies += missing
            rarity = card['rarity']
            if rarity in needs:
                needs[rarity] += missing
            elif missing:
                unknown_rarity_missing += missing
        for group in grouped.values():
            if group['arena_id'] is None:
                unresolved_missing += group['main_count'] + group['sideboard_count']
        wildcard_rows = {
            rarity: {
                'available': wildcards.get(rarity, 0),
                'needed': needs[rarity],
                'remaining': wildcards.get(rarity, 0) - needs[rarity],
            } for rarity in RARITIES
        }
        deficits = any(row['remaining'] < 0 for row in wildcard_rows.values())
        collection_result = {
            'missing_copies': missing_copies,
            'unresolved_missing_copies': unresolved_missing,
            'unknown_rarity_missing_copies': unknown_rarity_missing,
            'wildcards': wildcard_rows,
            'wildcard_budget_status': ('unknown' if unresolved_missing or unknown_rarity_missing else
                                       'short' if deficits else 'within_budget'),
        }

    invalid_issue_kinds = {
        'main_deck_below_minimum', 'sideboard_over_maximum', 'card_not_legal',
        'copy_limit_exceeded', 'restricted_copy_limit_exceeded',
    }
    if any(issue['kind'] in invalid_issue_kinds for issue in issues):
        legality_status = 'issues_found'
    elif any(issue['kind'] in ('card_not_found', 'ambiguous_card_printing', 'legality_unknown')
             for issue in issues):
        legality_status = 'unknown'
    else:
        legality_status = 'legal_from_catalog'

    cards_out = []
    for group in grouped.values():
        cards_out.append({
            'name': group['name'], 'set': group['set'],
            'collector_number': group['collector_number'], 'rarity': group['rarity'],
            'legality': group['legality'], 'main_count': group['main_count'],
            'sideboard_count': group['sideboard_count'],
        })
    cards_out.sort(key=lambda card: (card['name'].casefold(), card['set'] or '', card['collector_number'] or ''))
    issues.sort(key=lambda issue: (issue['kind'], issue.get('card_name', '').casefold()))
    report = {
        'schema_version': 1,
        'provenance': 'catalog_based_unverified',
        'independent_validation': 'pending',
        'format': fmt,
        'format_legality_status': legality_status,
        'catalog': {
            'schema_version': metadata['schema_version'],
            'imported_at_utc': metadata['imported_at_utc'],
        },
        'main_count': main_count,
        'sideboard_count': sideboard_count,
        'cards': cards_out,
        'issues': issues,
        'composition': {
            'color_counts': {color: composition['color_counts'][color] for color in COLORS},
            'mana_curve': composition['mana_curve'],
            'type_counts': dict(sorted(composition['type_counts'].items())),
            'unknown_mana_value_copies': composition['unknown_mana_value_copies'],
            'unknown_color_copies': composition['unknown_color_copies'],
            'unknown_type_copies': composition['unknown_type_copies'],
        },
        'collection': collection_result,
        'limitations': [
            'Format legality reflects only the supplied catalog import and has not been independently verified.',
            'Special card-specific deck limits and sideboard conventions are not interpreted.',
        ],
    }
    if len(json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_REPORT_BYTES:
        raise DeckInputError('Deck audit output exceeds the safe size limit')
    return report


def suggest_deck_cards(deck_text: str | DeckList, format_name: str, catalog_path: Path,
                       collection: dict[str, dict[Any, int]], goal: str, *,
                       copies: int = 1, limit: int = 10) -> dict[str, Any]:
    """Rank legal local card candidates by narrow, explainable deck-fit signals."""
    if not isinstance(format_name, str) or format_name.casefold() not in FORMATS:
        raise DeckInputError('Unsupported format')
    fmt = format_name.casefold()
    if not isinstance(goal, str) or goal.casefold() not in SUGGESTION_GOALS:
        raise DeckInputError('Unsupported suggestion goal')
    goal = goal.casefold()
    if type(copies) is not int or not 1 <= copies <= MAX_SUGGESTION_COPIES:
        raise DeckInputError('Invalid suggestion copy count')
    if type(limit) is not int or not 1 <= limit <= MAX_SUGGESTION_LIMIT:
        raise DeckInputError('Invalid suggestion result limit')
    if not isinstance(collection, dict):
        raise DeckInputError('Collection data is required')
    cards_inventory = collection.get('cards')
    wildcard_inventory = collection.get('wildcards')
    if (not isinstance(cards_inventory, dict) or len(cards_inventory) > MAX_COLLECTION_ROWS or
            not isinstance(wildcard_inventory, dict)):
        raise DeckInputError('Invalid collection data')
    if (any(type(arena_id) is not int or arena_id <= 0 or arena_id > 2**63 - 1 or
            type(count) is not int or count < 0 or count > MAX_COUNT
            for arena_id, count in cards_inventory.items()) or
            any(not isinstance(rarity, str) or rarity not in RARITIES or
                type(count) is not int or count < 0 or count > MAX_COUNT
                for rarity, count in wildcard_inventory.items())):
        raise DeckInputError('Invalid collection data')

    deck = parse_decklist(deck_text) if isinstance(deck_text, str) else deck_text
    if not isinstance(deck, DeckList) or not deck.entries:
        raise DeckInputError('Invalid deck list')
    metadata = get_catalog_metadata(catalog_path)
    catalog = list_format_card_printings(catalog_path, fmt)
    if metadata['schema_version'] != 2 or catalog['schema_version'] != 2:
        raise DeckInputError('Deck suggestions require a schema-v2 card catalog')

    current_cards: dict[int, dict[str, Any]] = {}
    current_names: dict[str, int] = {}
    current_id_counts: dict[int, int] = {}
    current_colors: set[str] = set()
    current_curve: dict[int, int] = {}
    current_subtypes: dict[str, int] = {}
    unknown_current = 0
    resolve_cache: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = {}
    for entry in deck.entries:
        key = (entry.name.casefold(), entry.set_code, entry.collector_number)
        if key not in resolve_cache:
            resolve_cache[key] = search_card_printings(
                catalog_path, entry.name, entry.set_code, entry.collector_number)
        matches = resolve_cache[key]
        if len(matches) != 1:
            if entry.section == 'main':
                unknown_current += 1
            continue
        card = matches[0]
        arena_id = card['arena_id']
        card_name_key = card['name'].casefold()
        current_cards[arena_id] = card
        current_names[card_name_key] = current_names.get(card_name_key, 0) + entry.count
        current_id_counts[arena_id] = current_id_counts.get(arena_id, 0) + entry.count
        if entry.section != 'main':
            continue
        current_colors.update(color for color in card['colors'] if color in COLORS)
        for basic_type, color in (('plains', 'W'), ('island', 'U'), ('swamp', 'B'),
                                  ('mountain', 'R'), ('forest', 'G')):
            if ('basic land' in (card['type_line'] or '').casefold() and
                    basic_type in (card['type_line'] or '').casefold()):
                current_colors.add(color)
        type_line = card['type_line']
        types = _known_card_types(type_line)
        cmc = card['cmc']
        if 'Land' not in types and type(cmc) in (int, float) and float(cmc).is_integer():
            bucket = int(cmc)
            if 1 <= bucket <= 5:
                current_curve[bucket] = current_curve.get(bucket, 0) + entry.count
        for subtype in _creature_subtypes(type_line):
            current_subtypes[subtype] = current_subtypes.get(subtype, 0) + entry.count

    curve_peak = max(current_curve.values(), default=0)
    restricted_current = {
        name for name, count in current_names.items()
        if any(card['name'].casefold() == name and card['legalities'].get(fmt) == 'restricted'
               for card in current_cards.values()) and count >= 1
    }
    ranked: list[dict[str, Any]] = []
    for card in catalog['cards']:
        name = card['name']
        name_key = name.casefold()
        status = card['legalities'].get(fmt)
        if status not in ('legal', 'restricted'):
            continue
        if name_key in restricted_current:
            continue
        current_count = current_names.get(name_key, 0)
        maximum = 1 if status == 'restricted' else 4
        if current_count + copies > maximum:
            continue
        types = _known_card_types(card['type_line'])
        if 'Land' in types:
            continue
        colors = card['colors']
        if not set(colors).issubset(current_colors):
            continue

        role_signals = _suggestion_role_signals(card)
        goal_matches: list[str] = []
        curve_gap = 0
        shared_subtypes = sorted(set(_creature_subtypes(card['type_line'])) & set(current_subtypes))
        if goal == 'curve':
            cmc = card['cmc']
            if type(cmc) not in (int, float) or not float(cmc).is_integer():
                continue
            bucket = int(cmc)
            if bucket < 1 or bucket > 5:
                continue
            curve_gap = max(0, curve_peak - current_curve.get(bucket, 0))
            goal_matches = ['fills_underrepresented_mana_value']
        elif goal == 'synergy':
            if not shared_subtypes:
                continue
            goal_matches = ['shares_creature_subtype']
        else:
            if goal not in role_signals:
                continue
            goal_matches = [f'has_{goal}_text_or_type_signal']

        owned = cards_inventory.get(card['arena_id'], 0)
        already_owned = max(0, owned - current_id_counts.get(card['arena_id'], 0))
        wildcard_needed = max(0, copies - already_owned)
        rarity = card['rarity']
        wildcard_available = wildcard_inventory.get(rarity, 0) if rarity in RARITIES else 0
        if wildcard_needed == 0:
            availability = 'owned'
        elif rarity not in RARITIES:
            availability = 'unknown_rarity'
        elif wildcard_available >= wildcard_needed:
            availability = 'within_budget'
        else:
            availability = 'over_budget'

        reasons = ['format_legal_in_catalog', 'fits_known_card_colors', *goal_matches]
        if goal == 'curve' and curve_gap:
            reasons.append(f'curve_bucket_gap_{min(curve_gap, 20)}')
        if goal == 'synergy':
            reasons.append('shared_subtype_' + shared_subtypes[0].casefold())
        if availability == 'owned':
            reasons.append('owned_exact_printing')
        elif availability == 'within_budget':
            reasons.append('wildcard_budget_available')

        score = 40 + 10
        if goal == 'curve':
            score += min(curve_gap, 20)
        elif goal == 'synergy':
            score += min(10, sum(current_subtypes[item] for item in shared_subtypes))
        if availability == 'owned':
            score += 15
        elif availability == 'within_budget':
            score += 10
        elif availability == 'over_budget':
            score -= 10
        suggestion = {
            'name': name,
            'set': card['set'],
            'collector_number': card['collector_number'],
            'rarity': rarity,
            'cmc': card['cmc'],
            'colors': colors,
            'format_status': status,
            'score': score,
            'role_signals': sorted(role_signals),
            'reasons': reasons,
            'collection': {
                'status': availability,
                'copies_in_deck': current_count,
                'owned_copies_for_exact_printing': owned,
                'copies_to_add': copies,
                'wildcard_rarity': rarity if wildcard_needed and rarity in RARITIES else None,
                'wildcard_needed': wildcard_needed,
                'wildcard_available': wildcard_available,
            },
            '_sort': (availability not in ('owned', 'within_budget'), name_key,
                      card['set'] or '', card['collector_number'] or ''),
        }
        ranked.append(suggestion)

    ranked.sort(key=lambda item: (-item['score'], item['_sort']))
    # Keep one printing per card name, preferring the best-ranked/most affordable one.
    unique: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for suggestion in ranked:
        key = suggestion['name'].casefold()
        if key in seen_names:
            continue
        seen_names.add(key)
        suggestion.pop('_sort')
        unique.append(suggestion)
        if len(unique) >= limit:
            break

    limitations = [
        'The score ranks catalog and collection fit signals; it is not a card-power rating, win-rate estimate, or proof of deck improvement.',
        'Role signals use narrow text/type patterns and may miss effects or misclassify a card.',
        'Collection counts use exact Arena IDs; alternate printings are not combined.',
        'Catalog format status is unverified and reflects only its import time; special construction rules are not checked.',
        'Suggestions are additions to inspect. No card is selected for removal and no deck is changed.',
    ]
    if unknown_current:
        limitations.append('current_deck_has_unresolved_cards')
    if catalog['truncated']:
        limitations.append('catalog_scan_truncated')
    report = {
        'schema_version': 1,
        'provenance': 'catalog_based_unverified',
        'independent_validation': 'pending',
        'format': fmt,
        'goal': goal,
        'copies_to_add': copies,
        'catalog': {
            'schema_version': metadata['schema_version'],
            'imported_at_utc': metadata['imported_at_utc'],
            'scan_truncated': catalog['truncated'],
        },
        'current_deck_known_colors': sorted(current_colors),
        'current_deck_unresolved_main_entries': unknown_current,
        'suggestions': unique,
        'limitations': limitations,
    }
    if len(json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_REPORT_BYTES:
        raise DeckInputError('Deck suggestion output exceeds the safe size limit')
    return report


def _creature_subtypes(type_line: Any) -> list[str]:
    if not isinstance(type_line, str) or 'creature' not in type_line.casefold():
        return []
    parts = type_line.split('—', 1)
    if len(parts) != 2:
        return []
    return sorted({word.casefold() for word in re.findall(r'[A-Za-z]+', parts[1])
                   if len(word) > 1})


def _suggestion_role_signals(card: dict[str, Any]) -> set[str]:
    text = (card.get('oracle_text') or '').casefold()
    roles: set[str] = set()
    interaction = (
        r'\b(?:destroy|exile|counter) target\b',
        r'\bdeals?\b.{0,80}\bdamage to (?:any target|target creature|target planeswalker)\b',
        r'\bgets? -[0-9x]+/-[0-9x]+\b',
        r'\bfights? target\b',
        r'\breturn target\b.{0,64}\bto its owner.s hand\b',
    )
    if any(re.search(pattern, text) for pattern in interaction):
        roles.add('interaction')
    if (re.search(r'\bdraw (?:a|one|two|three|four|x) cards?\b', text) or
            re.search(r'\bdraws? (?:a|one|two|three|four|x) cards?\b', text)):
        roles.add('card_advantage')
    if (re.search(r'\badd\s+\{[cwubrgx]\}', text) or
            re.search(r'\bsearch your library for (?:a|up to \w+) basic land card\b', text)):
        roles.add('ramp')
    if any(card_type in ('Creature', 'Planeswalker') for card_type in _known_card_types(card.get('type_line'))):
        roles.add('threats')
    return roles


def _known_card_types(type_line: Any) -> list[str]:
    if not isinstance(type_line, str):
        return []
    left = type_line.split('—', 1)[0]
    return sorted({word.title() for word in re.findall(r'[A-Za-z]+', left)
                   if word.casefold() in CARD_TYPES})


