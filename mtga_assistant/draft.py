"""Offline import and comparison of one visible draft pack against a pool."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import gzip
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterator
import zlib

from mtga_assistant.cards import CatalogError, lookup_card
from mtga_assistant.decks import (
    COLORS, MAX_REPORT_BYTES, _creature_subtypes, _known_card_types,
    _suggestion_role_signals,
)


STATS_SCHEMA_VERSION = 1
MAX_STATS_SOURCE_BYTES = 512 * 1024 * 1024
MAX_STATS_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_STATS_ROWS = 10_000_000
MAX_STATS_CARD_COLUMNS = 10_000
MAX_STATS_CARD_NAMES = 100_000
MAX_CSV_LINE_BYTES = 1_000_000
MAX_STATE_BYTES = 1_000_000
MAX_PACK_CARDS = 64
MAX_POOL_CARDS = 256
MAX_DRAFT_REPORT_BYTES = MAX_REPORT_BYTES
MIN_PICK_SAMPLE_FOR_RANKING = 20
_SET_CODE = re.compile(r'[A-Za-z0-9]{1,16}')
_EVENT_TYPE = re.compile(r'[A-Za-z][A-Za-z0-9]{0,31}')


class DraftInputError(ValueError):
    """A safe, content-free draft state/statistics error."""


@dataclass(frozen=True)
class DraftStatsImportSummary:
    imported_rows: int
    filtered_rows: int
    skipped_second_pick_rows: int
    skipped_invalid_rows: int
    card_count: int
    source_bytes: int
    imported_at_utc: str


@dataclass(frozen=True)
class DraftState:
    set_code: str
    event_type: str
    pack_number: int
    pick_number: int
    pack: tuple[int, ...]
    pool: tuple[int, ...]


def _valid_identity(set_code: Any, event_type: Any) -> tuple[str, str]:
    if not isinstance(set_code, str) or _SET_CODE.fullmatch(set_code) is None:
        raise DraftInputError('Invalid draft set or event type')
    if not isinstance(event_type, str) or _EVENT_TYPE.fullmatch(event_type) is None:
        raise DraftInputError('Invalid draft set or event type')
    return set_code.upper(), event_type


def _source_stream(source: Path):
    raw = source.open('rb')
    magic = raw.read(2)
    raw.seek(0)
    if magic == b'\x1f\x8b':
        return raw, gzip.GzipFile(fileobj=raw, mode='rb')
    return raw, raw


def _csv_lines(stream: Any) -> Iterator[str]:
    total = 0
    while True:
        raw = stream.readline(MAX_CSV_LINE_BYTES + 1)
        if not raw:
            return
        total += len(raw)
        if len(raw) > MAX_CSV_LINE_BYTES or total > MAX_STATS_UNCOMPRESSED_BYTES:
            raise DraftInputError('17Lands data exceeds the safe size limit')
        try:
            yield raw.decode('utf-8-sig' if total == len(raw) else 'utf-8')
        except UnicodeDecodeError:
            raise DraftInputError('Invalid 17Lands data file') from None


def _selected_second_pick(value: str) -> bool:
    return value.strip().casefold() not in ('', '0', '0.0', 'false', 'none', 'null')


def _safe_card_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > 128 or any(not char.isprintable() for char in name):
        return None
    return name


def import_17lands_draft_stats(source: Path, database: Path, set_code: str,
                               event_type: str) -> DraftStatsImportSummary:
    """Stream a user-supplied 17Lands per-pick CSV into a compact local database."""
    source, database = Path(source), Path(database)
    set_code, event_type = _valid_identity(set_code, event_type)
    connection: sqlite3.Connection | None = None
    temp_path: Path | None = None
    raw = stream = None
    try:
        if not source.is_file():
            raise DraftInputError('17Lands source is unavailable')
        before = source.stat()
        if before.st_size > MAX_STATS_SOURCE_BYTES:
            raise DraftInputError('17Lands source exceeds the safe size limit')
        if source.resolve(strict=False) == database.resolve(strict=False):
            raise DraftInputError('17Lands source and destination must differ')
        if database.exists() and os.path.samefile(source, database):
            raise DraftInputError('17Lands source and destination must differ')
        database.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f'.{database.name}.', suffix='.tmp', dir=database.parent, delete=False)
        temp_path = Path(handle.name)
        handle.close()
        connection = sqlite3.connect(temp_path)
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.executescript('''
            CREATE TABLE cards (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                picked_count INTEGER NOT NULL CHECK (picked_count >= 0),
                selected_pick_index_sum REAL NOT NULL CHECK (selected_pick_index_sum >= 0)
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
        ''')

        raw, stream = _source_stream(source)
        old_limit = csv.field_size_limit()
        csv.field_size_limit(MAX_CSV_LINE_BYTES)
        try:
            reader = csv.reader(_csv_lines(stream))
            try:
                header = next(reader)
            except StopIteration:
                raise DraftInputError('17Lands data file is empty') from None
            if (not header or len(header) > MAX_STATS_CARD_COLUMNS + 32 or
                    any(not field or len(field) > 256 or not field.isprintable() for field in header) or
                    len(header) != len(set(header))):
                raise DraftInputError('Invalid 17Lands data header')
            index = {name: position for position, name in enumerate(header)}
            required = ('expansion', 'event_type', 'pick_number', 'pick', 'pick_2')
            card_columns = [name for name in header if name.startswith('pack_card_')]
            if any(name not in index for name in required) or not card_columns:
                raise DraftInputError('Unsupported 17Lands draft data format')
            for column in card_columns:
                if _safe_card_name(column[len('pack_card_'):]) is None:
                    raise DraftInputError('Invalid 17Lands data header')
            card_names: dict[str, str] = {}
            sums: dict[str, tuple[int, float]] = {}
            imported = filtered = second_pick = invalid = row_count = 0
            for row in reader:
                row_count += 1
                if row_count > MAX_STATS_ROWS:
                    raise DraftInputError('17Lands data has too many rows')
                if len(row) != len(header):
                    raise DraftInputError('Invalid 17Lands data row')
                row_set = row[index['expansion']].strip().upper()
                row_event = row[index['event_type']].strip()
                if row_set != set_code or row_event != event_type:
                    filtered += 1
                    continue
                if _selected_second_pick(row[index['pick_2']]):
                    second_pick += 1
                    continue
                name = _safe_card_name(row[index['pick']])
                try:
                    pick_number = int(row[index['pick_number']])
                except (TypeError, ValueError):
                    pick_number = -1
                if name is None or not 0 <= pick_number <= 99:
                    invalid += 1
                    continue
                key = name.casefold()
                existing = sums.get(key)
                if existing is None:
                    if len(sums) >= MAX_STATS_CARD_NAMES:
                        raise DraftInputError('17Lands data has too many cards')
                    card_names[key] = name
                    sums[key] = (1, float(pick_number))
                else:
                    sums[key] = (existing[0] + 1, existing[1] + float(pick_number))
                imported += 1
        except DraftInputError:
            raise
        except (csv.Error, EOFError, OSError, UnicodeError, zlib.error):
            raise DraftInputError('Unable to read 17Lands data') from None
        finally:
            csv.field_size_limit(old_limit)
        if imported == 0:
            raise DraftInputError('No matching single-pick rows were found')
        connection.executemany(
            'INSERT INTO cards (name, picked_count, selected_pick_index_sum) VALUES (?, ?, ?)',
            ((card_names[key], count, pick_sum) for key, (count, pick_sum) in sums.items()),
        )
        imported_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        values = {
            'schema_version': str(STATS_SCHEMA_VERSION),
            'set_code': set_code,
            'event_type': event_type,
            'imported_at_utc': imported_at,
            'source_bytes': str(before.st_size),
            'imported_rows': str(imported),
            'filtered_rows': str(filtered),
            'skipped_second_pick_rows': str(second_pick),
            'skipped_invalid_rows': str(invalid),
            'card_count': str(len(sums)),
        }
        connection.executemany('INSERT INTO metadata (key, value) VALUES (?, ?)', values.items())
        connection.commit()
        connection.close()
        connection = None
        if stream is not raw:
            stream.close()
        raw.close()
        stream = raw = None
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise DraftInputError('17Lands source changed during import')
        os.replace(temp_path, database)
        temp_path = None
        return DraftStatsImportSummary(imported, filtered, second_pick, invalid,
                                       len(sums), before.st_size, imported_at)
    except DraftInputError:
        raise
    except (OSError, sqlite3.Error, csv.Error, EOFError, UnicodeError, zlib.error, ValueError):
        raise DraftInputError('Unable to import local 17Lands data') from None
    finally:
        if stream is not None and stream is not raw:
            try:
                stream.close()
            except OSError:
                pass
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if temp_path is not None:
            for leftover in (temp_path, Path(str(temp_path) + '-journal'),
                             Path(str(temp_path) + '-wal'), Path(str(temp_path) + '-shm')):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass


@contextmanager
def _open_stats(database: Path) -> Iterator[sqlite3.Connection]:
    database = Path(database)
    if not database.is_file():
        raise DraftInputError('Draft statistics are unavailable')
    connection = None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        metadata = dict(connection.execute('SELECT key, value FROM metadata'))
        if version != STATS_SCHEMA_VERSION or metadata.get('schema_version') != str(version):
            raise DraftInputError('Unsupported draft statistics schema')
        yield connection
    except DraftInputError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise DraftInputError('Invalid draft statistics') from None
    finally:
        if connection is not None:
            connection.close()


def load_draft_card_stats(database: Path, set_code: str, event_type: str,
                          card_names: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    set_code, event_type = _valid_identity(set_code, event_type)
    if not isinstance(card_names, list) or len(card_names) > MAX_PACK_CARDS:
        raise DraftInputError('Invalid draft card names')
    names: set[str] = set()
    for name in card_names:
        safe = _safe_card_name(name)
        if safe is None:
            raise DraftInputError('Invalid draft card names')
        names.add(safe.casefold())
    with _open_stats(database) as connection:
        try:
            metadata = dict(connection.execute('SELECT key, value FROM metadata'))
            if metadata.get('set_code') != set_code or metadata.get('event_type') != event_type:
                raise DraftInputError('Draft statistics do not match this set and event')
            imported_at = metadata.get('imported_at_utc')
            datetime.fromisoformat(imported_at.replace('Z', '+00:00'))
            stats: dict[str, dict[str, Any]] = {}
            for row in connection.execute(
                    'SELECT name, picked_count, selected_pick_index_sum FROM cards ORDER BY name COLLATE NOCASE'):
                name = row['name']
                count, pick_sum = row['picked_count'], row['selected_pick_index_sum']
                if (not isinstance(name, str) or _safe_card_name(name) is None or
                        type(count) is not int or count <= 0 or
                        type(pick_sum) not in (int, float) or not math.isfinite(pick_sum) or pick_sum < 0):
                    raise DraftInputError('Invalid draft statistics')
                if name.casefold() in names:
                    stats[name.casefold()] = {
                        'picked_count': count,
                        'average_selected_pick_index': float(pick_sum) / count,
                    }
            safe_metadata = {
                'schema_version': STATS_SCHEMA_VERSION,
                'source': '17Lands public draft data',
                'set_code': set_code,
                'event_type': event_type,
                'imported_at_utc': imported_at,
            }
            return safe_metadata, stats
        except DraftInputError:
            raise
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError):
            raise DraftInputError('Invalid draft statistics') from None


def _state_from_value(value: Any) -> DraftState:
    if not isinstance(value, dict):
        raise DraftInputError('Invalid draft state')
    set_code, event_type = _valid_identity(value.get('set_code'), value.get('event_type'))
    pack_number, pick_number = value.get('pack_number'), value.get('pick_number')
    if (type(pack_number) is not int or not 0 <= pack_number <= 3 or
            type(pick_number) is not int or not 0 <= pick_number <= 99):
        raise DraftInputError('Invalid draft state')
    pack, pool = value.get('pack'), value.get('pool')
    if (not isinstance(pack, list) or not 1 <= len(pack) <= MAX_PACK_CARDS or
            not isinstance(pool, list) or len(pool) > MAX_POOL_CARDS):
        raise DraftInputError('Invalid draft state')
    for ids in (pack, pool):
        if any(type(arena_id) is not int or not 0 < arena_id <= 2**63 - 1 for arena_id in ids):
            raise DraftInputError('Invalid draft state')
    return DraftState(set_code, event_type, pack_number, pick_number, tuple(pack), tuple(pool))


def load_draft_state(path: Path) -> DraftState:
    path = Path(path)
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise DraftInputError('Draft state is too large')
        value = json.loads(raw.decode('utf-8-sig'))
        return _state_from_value(value)
    except DraftInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise DraftInputError('Unable to read draft state') from None


def _basic_pool_colors(card: dict[str, Any]) -> set[str]:
    colors = {color for color in card.get('colors', []) if color in COLORS}
    type_line = (card.get('type_line') or '').casefold()
    if 'basic land' in type_line:
        for basic, color in (('plains', 'W'), ('island', 'U'), ('swamp', 'B'),
                             ('mountain', 'R'), ('forest', 'G')):
            if basic in type_line:
                colors.add(color)
    return colors


def rank_draft(state: DraftState, card_catalog: Path, stats_database: Path | None = None,
               *, limit: int = MAX_PACK_CARDS) -> dict[str, Any]:
    if not isinstance(state, DraftState):
        state = _state_from_value(state)
    if type(limit) is not int or not 1 <= limit <= MAX_PACK_CARDS:
        raise DraftInputError('Invalid draft result limit')

    pool_cards: list[dict[str, Any]] = []
    candidate_cards: dict[str, dict[str, Any]] = {}
    pack_counts: dict[str, int] = {}
    unknown_pool = unknown_pack = 0
    for arena_id in state.pool:
        try:
            card = lookup_card(card_catalog, arena_id)
        except CatalogError:
            raise DraftInputError('Unable to read local card catalog') from None
        if card is None:
            unknown_pool += 1
        else:
            pool_cards.append(card)
    for arena_id in state.pack:
        try:
            card = lookup_card(card_catalog, arena_id)
        except CatalogError:
            raise DraftInputError('Unable to read local card catalog') from None
        if card is None:
            unknown_pack += 1
            continue
        key = card['name'].casefold()
        pack_counts[key] = pack_counts.get(key, 0) + 1
        candidate_cards.setdefault(key, card)

    stats_metadata: dict[str, Any] | None = None
    stats_by_name: dict[str, dict[str, Any]] = {}
    if stats_database is not None:
        stats_metadata, stats_by_name = load_draft_card_stats(
            stats_database, state.set_code, state.event_type,
            [card['name'] for card in candidate_cards.values()])

    pool_colors: set[str] = set()
    pool_subtypes: dict[str, int] = {}
    pool_curve: dict[int, int] = {}
    pool_roles: dict[str, int] = {}
    for card in pool_cards:
        pool_colors.update(_basic_pool_colors(card))
        for subtype in _creature_subtypes(card.get('type_line')):
            pool_subtypes[subtype] = pool_subtypes.get(subtype, 0) + 1
        types = _known_card_types(card.get('type_line'))
        cmc = card.get('cmc')
        if 'Land' not in types and type(cmc) in (int, float) and float(cmc).is_integer():
            bucket = int(cmc)
            if 1 <= bucket <= 5:
                pool_curve[bucket] = pool_curve.get(bucket, 0) + 1
        for role in _suggestion_role_signals(card):
            pool_roles[role] = pool_roles.get(role, 0) + 1

    statistical_candidates = [
        (name, stats_by_name[name]['average_selected_pick_index'])
        for name in candidate_cards
        if name in stats_by_name and
        stats_by_name[name]['picked_count'] >= MIN_PICK_SAMPLE_FOR_RANKING
    ]
    statistical_candidates.sort(key=lambda pair: (pair[1], pair[0]))
    stats_position: dict[str, int] = {}
    for index, (name, _) in enumerate(statistical_candidates):
        stats_position[name] = index

    candidates = []
    for key, card in candidate_cards.items():
        colors = [color for color in card['colors'] if color in COLORS]
        card_subtypes = _creature_subtypes(card.get('type_line'))
        shared_subtypes = sorted(set(card_subtypes) & set(pool_subtypes))
        reasons: list[str] = []
        pool_score = 50
        if pool_colors:
            if set(colors).issubset(pool_colors):
                reasons.append('fits_current_pool_colors')
                pool_score += 20
            elif colors:
                reasons.append('adds_color_to_pool')
                pool_score -= 15
            else:
                reasons.append('colorless_card')
                pool_score += 5
        else:
            reasons.append('pool_colors_not_established')
        if shared_subtypes:
            reasons.append('shares_creature_subtype')
            pool_score += min(20, 10 + sum(pool_subtypes[item] for item in shared_subtypes))
        types = _known_card_types(card.get('type_line'))
        cmc = card.get('cmc')
        if 'Land' not in types and type(cmc) in (int, float) and float(cmc).is_integer():
            bucket = int(cmc)
            if 1 <= bucket <= 5 and pool_curve.get(bucket, 0) < 4:
                reasons.append('fills_limited_curve_slot')
                pool_score += 8
        roles = _suggestion_role_signals(card)
        for role in sorted(roles):
            if pool_roles.get(role, 0) == 0:
                reasons.append(f'adds_{role}_signal')
                pool_score += 8
        pool_score = max(0, min(100, pool_score))

        stat = stats_by_name.get(key)
        if stat is None:
            stat_status = 'unavailable' if stats_metadata is not None else 'not_loaded'
            stat_score = None
            picked_count = 0
            average_pick_index = None
        else:
            picked_count = stat['picked_count']
            average_pick_index = round(stat['average_selected_pick_index'], 3)
            if picked_count < MIN_PICK_SAMPLE_FOR_RANKING:
                stat_status = 'sparse'
                stat_score = None
            else:
                stat_status = 'available'
                if len(statistical_candidates) <= 1:
                    stat_score = 50
                else:
                    position = stats_position[key]
                    stat_score = round(100 * (len(statistical_candidates) - 1 - position) /
                                       (len(statistical_candidates) - 1))
        if stat_score is None:
            overall_score = pool_score
        else:
            overall_score = round(stat_score * 0.65 + pool_score * 0.35)
            reasons.append('ranked_by_matching_17lands_pick_history')
        candidates.append({
            'name': card['name'],
            'set': card.get('set'),
            'collector_number': card.get('collector_number'),
            'cmc': cmc,
            'colors': colors,
            'rarity': card.get('rarity'),
            'score': overall_score,
            'pool_fit_score': pool_score,
            'pool_reasons': reasons,
            'shared_creature_subtypes': shared_subtypes,
            'statistics': {
                'status': stat_status,
                'picked_count': picked_count,
                'average_selected_pick_index': average_pick_index,
                'sample_threshold_for_ranking': MIN_PICK_SAMPLE_FOR_RANKING,
            },
        })
    candidates.sort(key=lambda item: (
        item['statistics']['status'] != 'available', -item['score'], item['name'].casefold(),
        item['set'] or '', item['collector_number'] or '',
    ))
    candidates = candidates[:limit]
    limitations = [
        'Historical pick counts and average selected pick indexes describe observed selections, not win rate or card strength.',
        'Pool fit uses card colors, creature subtypes, mana value and narrow text/type cues; it is not a draft strategy model.',
        'Statistics can be stale or biased by the contributing player sample. Check the source dataset date before use.',
        'Pack and pool contents come only from the supplied local state file; live Arena draft-log extraction is not verified.',
        'Unknown Arena IDs are counted but never displayed.',
    ]
    report = {
        'schema_version': 1,
        'provenance': 'local_draft_state_unverified',
        'independent_validation': 'pending',
        'set_code': state.set_code,
        'event_type': state.event_type,
        'pack_number': state.pack_number,
        'pick_number': state.pick_number,
        'known_pool_count': len(pool_cards),
        'unknown_pool_cards': unknown_pool,
        'unknown_pack_cards': unknown_pack,
        'pool_colors': sorted(pool_colors),
        'statistics': stats_metadata or {
            'source': None,
            'status': 'not_loaded',
        },
        'candidates': candidates,
        'limitations': limitations,
    }
    if len(json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_DRAFT_REPORT_BYTES:
        raise DraftInputError('Draft ranking output exceeds the safe size limit')
    return report
