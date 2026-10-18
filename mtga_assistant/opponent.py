"""Local reference-deck matching using reducer-approved public observations."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from mtga_assistant.cards import CatalogError, get_catalog_metadata, search_card_printings
from mtga_assistant.decks import FORMATS as DECK_FORMATS, DeckInputError, parse_decklist
from mtga_assistant.state import DIAGNOSTICS


MAX_MANIFEST_BYTES = 1_000_000
MAX_DECK_BYTES = 1_000_000
MAX_TOTAL_DECK_BYTES = 32 * 1024 * 1024
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_DECKS = 500
MAX_REFERENCE_CARDS = 250
MAX_LABEL_LENGTH = 80
MAX_PUBLIC_COPIES = 250
MAX_CANDIDATES = 20
MAX_SUPPORTED_CARDS = 30
MAX_REPORT_BYTES = 2 * 1024 * 1024
_POSITIVE_ID = re.compile(r'[1-9][0-9]{0,18}')
_SAFE_LIMITATIONS = DIAGNOSTICS | frozenset({
    'missing_turn', 'missing_life_totals', 'missing_zones',
    'unknown_zone_membership', 'missing_visible_objects', 'same_game_replaced',
})


class OpponentDataError(ValueError):
    """A content-free error for local opponent-reference inputs."""


def _read_json(path: Path, byte_limit: int, error: str) -> dict[str, Any]:
    try:
        with Path(path).open('rb') as source:
            raw = source.read(byte_limit + 1)
        if len(raw) > byte_limit:
            raise OpponentDataError(error)
        value = json.loads(raw.decode('utf-8-sig'))
    except OpponentDataError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise OpponentDataError(error) from None
    if not isinstance(value, dict):
        raise OpponentDataError(error)
    return value


def _date_text(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise OpponentDataError('Invalid opponent reference data')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise OpponentDataError('Invalid opponent reference data') from None
    if parsed.isoformat() != value:
        raise OpponentDataError('Invalid opponent reference data')
    return value


def _label(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_LABEL_LENGTH or
            any(not character.isprintable() for character in value) or
            any(character in value for character in ('/', '\\', ':')) or value.strip() in {'.', '..'}):
        raise OpponentDataError('Invalid opponent reference data')
    return value.strip()


def _metadata_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise OpponentDataError('Invalid opponent reference data')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise OpponentDataError('Invalid opponent reference data') from None
    if parsed.tzinfo is None:
        raise OpponentDataError('Invalid opponent reference data')
    return value


def load_reference_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a user manifest, resolving deck paths locally."""
    manifest_path = Path(path)
    value = _read_json(manifest_path, MAX_MANIFEST_BYTES, 'Invalid opponent manifest')
    if set(value) != {'format', 'reference_date', 'decks'}:
        raise OpponentDataError('Invalid opponent manifest')
    format_name = value.get('format')
    if not isinstance(format_name, str) or format_name not in DECK_FORMATS:
        raise OpponentDataError('Invalid opponent manifest')
    reference_date = _date_text(value.get('reference_date'))
    decks = value.get('decks')
    if not isinstance(decks, list) or not 1 <= len(decks) <= MAX_REFERENCE_DECKS:
        raise OpponentDataError('Invalid opponent manifest')
    base = manifest_path.resolve(strict=False).parent
    labels: set[str] = set()
    normalized = []
    for item in decks:
        if not isinstance(item, dict) or set(item) != {'label', 'path'}:
            raise OpponentDataError('Invalid opponent manifest')
        label = _label(item.get('label'))
        source_name = item.get('path')
        if (not isinstance(source_name, str) or not source_name.strip() or len(source_name) > 4096 or
                any(not character.isprintable() for character in source_name)):
            raise OpponentDataError('Invalid opponent manifest')
        if label in labels:
            raise OpponentDataError('Invalid opponent manifest')
        labels.add(label)
        source_path = Path(source_name)
        if not source_path.is_absolute():
            source_path = base / source_path
        normalized.append({'label': label, 'source_path': source_path})
    return {'format': format_name, 'reference_date': reference_date, 'decks': normalized}


def build_reference_data(manifest_path: Path, cards_db: Path) -> dict[str, Any]:
    """Resolve manifest decklists to exact Arena IDs without retaining source paths."""
    manifest = load_reference_manifest(manifest_path)
    try:
        metadata = get_catalog_metadata(Path(cards_db))
    except (CatalogError, OSError, ValueError):
        raise OpponentDataError('Unable to read the local card catalog') from None

    total_source_bytes = 0
    result_decks = []
    deck_signatures: set[tuple[tuple[int, int], ...]] = set()
    for item in manifest['decks']:
        try:
            with item['source_path'].open('rb') as source:
                raw = source.read(MAX_DECK_BYTES + 1)
        except OSError:
            raise OpponentDataError('Unable to read an opponent reference deck') from None
        if len(raw) > MAX_DECK_BYTES:
            raise OpponentDataError('An opponent reference deck is too large')
        total_source_bytes += len(raw)
        if total_source_bytes > MAX_TOTAL_DECK_BYTES:
            raise OpponentDataError('Opponent reference inputs are too large')
        try:
            deck = parse_decklist(raw.decode('utf-8-sig'))
        except (DeckInputError, UnicodeError):
            raise OpponentDataError('Invalid opponent reference deck') from None
        if deck.main_count > MAX_REFERENCE_CARDS:
            raise OpponentDataError('Opponent reference deck has too many main-deck cards')
        counts: Counter[int] = Counter()
        try:
            for entry in deck.entries:
                if entry.section != 'main':
                    continue
                matches = search_card_printings(
                    Path(cards_db), entry.name, entry.set_code, entry.collector_number,
                )
                if len(matches) != 1:
                    raise OpponentDataError('An opponent reference card is missing or ambiguous in the catalog')
                arena_id = matches[0].get('arena_id')
                if type(arena_id) is not int or arena_id <= 0:
                    raise OpponentDataError('Invalid local card catalog record')
                counts[arena_id] += entry.count
        except OpponentDataError:
            raise
        except (CatalogError, OSError, ValueError, TypeError):
            raise OpponentDataError('Unable to resolve opponent reference cards') from None
        if not counts or sum(counts.values()) != deck.main_count:
            raise OpponentDataError('Invalid opponent reference deck')
        signature = tuple(sorted(counts.items()))
        if signature in deck_signatures:
            raise OpponentDataError('Duplicate opponent reference deck')
        deck_signatures.add(signature)
        result_decks.append({
            'label': item['label'],
            'cards': {str(arena_id): counts[arena_id] for arena_id in sorted(counts)},
        })

    return {
        'schema_version': 1,
        'format': manifest['format'],
        'reference_date': manifest['reference_date'],
        'catalog_imported_at_utc': metadata.get('imported_at_utc'),
        'decks': result_decks,
    }


def write_reference_data(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically publish a bounded reference file without replacing an existing path."""
    target = Path(path)
    temporary_path: Path | None = None
    file_descriptor: int | None = None
    try:
        serialized = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        raw = serialized.encode('utf-8')
        if len(raw) > MAX_REFERENCE_BYTES:
            raise OpponentDataError('Opponent reference data is too large')

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{target.name or "opponent-reference"}.', suffix='.tmp',
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError('Unable to write opponent reference data')
            remaining = remaining[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None

        # A same-directory hard link publishes the completed file atomically
        # and fails if another process already created the destination.
        os.link(temporary_path, target)
    except OpponentDataError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
        raise OpponentDataError('Unable to write opponent reference data') from None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _normalize_reference(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {
            'schema_version', 'format', 'reference_date', 'catalog_imported_at_utc', 'decks'} or
            type(value.get('schema_version')) is not int or value.get('schema_version') != 1 or
            not isinstance(value.get('format'), str) or value.get('format') not in DECK_FORMATS):
        raise OpponentDataError('Invalid opponent reference data')
    reference_date = _date_text(value.get('reference_date'))
    catalog_time = _metadata_time(value.get('catalog_imported_at_utc'))
    decks = value.get('decks')
    if not isinstance(decks, list) or not 1 <= len(decks) <= MAX_REFERENCE_DECKS:
        raise OpponentDataError('Invalid opponent reference data')
    labels: set[str] = set()
    signatures: set[tuple[tuple[int, int], ...]] = set()
    normalized_decks = []
    for item in decks:
        if not isinstance(item, dict) or set(item) != {'label', 'cards'}:
            raise OpponentDataError('Invalid opponent reference data')
        label = _label(item.get('label'))
        cards = item.get('cards')
        if not isinstance(cards, dict) or not 1 <= len(cards) <= MAX_REFERENCE_CARDS:
            raise OpponentDataError('Invalid opponent reference data')
        counts: dict[int, int] = {}
        for raw_id, count in cards.items():
            if (not isinstance(raw_id, str) or _POSITIVE_ID.fullmatch(raw_id) is None or
                    len(raw_id) > 19 or int(raw_id) > 2**63 - 1 or
                    type(count) is not int or not 1 <= count <= 250):
                raise OpponentDataError('Invalid opponent reference data')
            counts[int(raw_id)] = count
        if sum(counts.values()) > MAX_REFERENCE_CARDS or label in labels:
            raise OpponentDataError('Invalid opponent reference data')
        signature = tuple(sorted(counts.items()))
        if signature in signatures:
            raise OpponentDataError('Invalid opponent reference data')
        labels.add(label)
        signatures.add(signature)
        normalized_decks.append({
            'label': label,
            'cards': {str(arena_id): counts[arena_id] for arena_id in sorted(counts)},
        })
    return {
        'schema_version': 1,
        'format': value['format'],
        'reference_date': reference_date,
        'catalog_imported_at_utc': catalog_time,
        'decks': normalized_decks,
    }


def load_reference_data(path: Path) -> dict[str, Any]:
    """Read a bounded, path-free local opponent reference file."""
    value = _read_json(Path(path), MAX_REFERENCE_BYTES, 'Invalid opponent reference data')
    return _normalize_reference(value)


def _integrity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {'status': 'uncertain', 'limitations': ['invalid_schema']}
    status = value.get('status')
    if not isinstance(status, str) or status not in {
            'waiting', 'partial', 'consistent', 'uncertain', 'ended'}:
        status = 'uncertain'
    raw = value.get('limitations', [])
    limitations = sorted({
        item for item in raw if isinstance(item, str) and item in _SAFE_LIMITATIONS
    }) if isinstance(raw, list) else ['invalid_schema']
    return {'status': status, 'limitations': limitations}


def _public_counts(value: Any) -> dict[int, int]:
    if not isinstance(value, Mapping) or len(value) > MAX_REFERENCE_CARDS * 2:
        raise OpponentDataError('Invalid public observation data')
    counts: dict[int, int] = {}
    for arena_id, copies in value.items():
        if (type(arena_id) is not int or not 0 < arena_id <= 2**63 - 1 or
                type(copies) is not int or not 1 <= copies <= MAX_PUBLIC_COPIES):
            raise OpponentDataError('Invalid public observation data')
        counts[arena_id] = copies
    if sum(counts.values()) > MAX_PUBLIC_COPIES:
        raise OpponentDataError('Invalid public observation data')
    return counts


def estimate_opponent(public_counts: Mapping[int, int], integrity: Any, *, my_seat: int,
                      reference_data: Any, card_names: Mapping[int, str]) -> dict[str, Any]:
    """Rank local deck references by exact overlap with public opponent cards."""
    if type(my_seat) is not int or my_seat not in (1, 2):
        raise OpponentDataError('Invalid opponent estimate request')
    counts = _public_counts(public_counts)
    references = _normalize_reference(reference_data)
    state_integrity = _integrity(integrity)
    names = card_names if isinstance(card_names, Mapping) else {}
    all_reference_ids = {
        int(arena_id) for deck in references['decks'] for arena_id in deck['cards']
    }
    if any(not isinstance(names.get(arena_id), str) or not names[arena_id].strip()
           for arena_id in all_reference_ids):
        raise OpponentDataError('Opponent references do not match the local card catalog')

    total_copies = sum(counts.values())
    known_counts = {
        arena_id: copies for arena_id, copies in counts.items()
        if isinstance(names.get(arena_id), str) and names[arena_id].strip()
    }
    known_copies = sum(known_counts.values())
    base = {
        'status': 'estimated',
        'format': references['format'],
        'reference_date': references['reference_date'],
        'catalog_imported_at_utc': references['catalog_imported_at_utc'],
        'my_seat': my_seat,
        'opponent_seat': 3 - my_seat,
        'integrity': state_integrity,
        'public_observations': {
            'visible_copies': total_copies,
            'catalog_matched_copies': known_copies,
            'unmatched_catalog_copies': total_copies - known_copies,
            'cards': [
                {'name': names[arena_id][:128], 'copies': copies}
                for arena_id, copies in sorted(known_counts.items(), key=lambda pair: names[pair[0]].casefold())
            ],
        },
        'matching_reference_count': 0,
        'candidates': [],
        'supported_unseen_cards': [],
        'limitations': [
            'Reference representativeness and current legality are unverified.',
            'Exact Arena printing IDs are matched; alternate printings do not match automatically.',
            'Sideboards are not included in this estimate.',
            'Reference support describes the supplied lists; it is not a probability.',
            'Only currently visible opponent-owned cards in public zones are used.',
            'Opponent hand and library identities are never inspected.',
        ],
    }
    if state_integrity['status'] == 'uncertain':
        base['status'] = 'state_uncertain'
        return base
    if state_integrity['status'] == 'partial':
        base['status'] = 'partial_state'
        base['limitations'].append('Public observations may be incomplete because the game state is partial.')
    if not total_copies or not known_copies:
        if base['status'] != 'partial_state':
            base['status'] = 'insufficient_public_evidence'
        return base

    ranked = []
    for deck in references['decks']:
        deck_counts = {int(arena_id): copies for arena_id, copies in deck['cards'].items()}
        matched = sum(min(copies, deck_counts.get(arena_id, 0))
                      for arena_id, copies in known_counts.items())
        if not matched:
            continue
        ranked.append({
            'label': deck['label'],
            'matched_public_copies': matched,
            'public_copy_coverage_percent': round(100 * matched / total_copies, 1),
            '_cards': deck_counts,
        })
    ranked.sort(key=lambda row: (
        -row['matched_public_copies'], -row['public_copy_coverage_percent'], row['label'].casefold(),
    ))
    base['matching_reference_count'] = len(ranked)
    if not ranked:
        if base['status'] != 'partial_state':
            base['status'] = 'no_reference_match'
        return base

    unseen_support: Counter[int] = Counter()
    for candidate in ranked:
        for arena_id in candidate['_cards']:
            if arena_id not in counts:
                unseen_support[arena_id] += 1
    base['candidates'] = [
        {key: row[key] for key in ('label', 'matched_public_copies', 'public_copy_coverage_percent')}
        for row in ranked[:MAX_CANDIDATES]
    ]
    support_rows = [
        {
            'name': names[arena_id][:128],
            'reference_decks': support,
            'reference_fraction': round(support / len(ranked), 3),
        }
        for arena_id, support in unseen_support.items()
    ]
    support_rows.sort(key=lambda row: (
        -row['reference_decks'], -row['reference_fraction'], row['name'].casefold(),
    ))
    base['supported_unseen_cards'] = support_rows[:MAX_SUPPORTED_CARDS]
    return base


def render_opponent_report(report: dict[str, Any], json_output: bool) -> str:
    """Render a bounded, path-free public-information estimate."""
    if json_output:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    else:
        lines = [
            'Public-information opponent estimate',
            f"Status: {report['status']} | format: {report['format']} | reference date: {report['reference_date']}",
            'Reference catalog imported at (UTC): ' +
            (report['catalog_imported_at_utc'] or 'unknown'),
            f"Observed public copies: {report['public_observations']['visible_copies']} "
            f"({report['public_observations']['catalog_matched_copies']} matched to the local catalog)",
            'Observed opponent-owned cards:',
        ]
        if report['public_observations']['cards']:
            lines.extend(f"  {card['name']} × {card['copies']}" for card in report['public_observations']['cards'])
        else:
            lines.append('  (none available)')
        lines.append('Matching reference lists (overlap is not verified deck identity):')
        if report['candidates']:
            lines.extend(
                f"  {candidate['label']}: {candidate['matched_public_copies']} copies, "
                f"{candidate['public_copy_coverage_percent']}% public-copy overlap"
                for candidate in report['candidates']
            )
        else:
            lines.append('  (none)')
        lines.append('Cards supported by matching reference lists (reference support, not probabilities):')
        if report['supported_unseen_cards']:
            lines.extend(
                f"  {card['name']}: {card['reference_decks']}/{report['matching_reference_count']} "
                f"references ({card['reference_fraction']:.1%})"
                for card in report['supported_unseen_cards']
            )
        else:
            lines.append('  (none)')
        lines.extend('Limitations: ' + limitation for limitation in report['limitations'])
        rendered = '\n'.join(lines) + '\n'
    if len(rendered.encode('utf-8')) > MAX_REPORT_BYTES:
        raise OpponentDataError('Opponent estimate is too large to display')
    return rendered
