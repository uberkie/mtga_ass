"""Offline Arena-ID card catalog import and lookup."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from enum import Enum
import gzip
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterable, Iterator
import zlib


SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = frozenset({1, 2})
MAX_RECORD_BYTES = 1_000_000
MAX_LEGAL_CATALOG_RESULTS = 50_000
MAX_FORMAT_CATALOG_BYTES = 32 * 1024 * 1024
CARD_TEXT_FIELDS = ('mana_cost', 'type_line', 'oracle_text', 'power', 'toughness', 'layout')
FACE_TEXT_FIELDS = ('mana_cost', 'type_line', 'oracle_text', 'power', 'toughness')
RARITIES = frozenset({'common', 'uncommon', 'rare', 'mythic', 'special', 'bonus'})
LEGALITY_FORMATS = frozenset({
    'standard', 'future', 'historic', 'timeless', 'gladiator', 'pioneer', 'explorer',
    'modern', 'legacy', 'pauper', 'vintage', 'penny', 'commander', 'oathbreaker',
    'standardbrawl', 'brawl', 'alchemy', 'paupercommander', 'duel', 'oldschool',
    'premodern', 'predh',
})
LEGALITY_STATUSES = frozenset({'legal', 'not_legal', 'restricted', 'banned'})
COLOR_CODES = frozenset({'W', 'U', 'B', 'R', 'G'})


class CatalogError(ValueError):
    """A safe, content-free card catalog error."""


class _MalformedCatalogField(Enum):
    INVALID = 'invalid'


MALFORMED_CATALOG_FIELD = _MalformedCatalogField.INVALID


@dataclass(frozen=True)
class ImportSummary:
    imported: int
    missing_arena_id: int
    duplicate_records: int
    source_bytes: int
    imported_at_utc: str


def _text(value: Any, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (not isinstance(value, str) or (required and not value.strip()) or
            required and any(not ch.isprintable() for ch in value)):
        raise CatalogError('Invalid card catalog record')
    return value


def _normalize_card(record: Any) -> tuple[int | None, tuple[Any, ...] | None]:
    if not isinstance(record, dict):
        raise CatalogError('Invalid card catalog record')

    name = _text(record.get('name'), required=True)
    facts: dict[str, Any] = {'name': name}
    for field in CARD_TEXT_FIELDS:
        facts[field] = _text(record.get(field))

    colors = record.get('colors', [])
    if (not isinstance(colors, list) or any(not isinstance(color, str) or color not in COLOR_CODES
                                            for color in colors) or len(set(colors)) != len(colors)):
        raise CatalogError('Invalid card catalog record')
    facts['colors'] = colors

    faces = record.get('card_faces', [])
    if faces is None:
        faces = []
    if not isinstance(faces, list):
        raise CatalogError('Invalid card catalog record')
    normalized_faces = []
    for face in faces:
        if not isinstance(face, dict):
            raise CatalogError('Invalid card catalog record')
        normalized = {'name': _text(face.get('name'), required=True)}
        for field in FACE_TEXT_FIELDS:
            normalized[field] = _text(face.get(field))
        normalized_faces.append(normalized)
    facts['card_faces'] = normalized_faces

    arena_id = record.get('arena_id')
    if arena_id is None:
        return None, None
    if type(arena_id) is not int or arena_id <= 0:
        raise CatalogError('Invalid card catalog record')

    cmc = record.get('cmc')
    if cmc is not None:
        if type(cmc) not in (int, float) or cmc < 0:
            raise CatalogError('Invalid card catalog record')
        try:
            cmc = float(cmc)
        except OverflowError:
            raise CatalogError('Invalid card catalog record') from None
        if not math.isfinite(cmc) or cmc > 1_000:
            raise CatalogError('Invalid card catalog record')
    rarity = _text(record.get('rarity'))
    if rarity is not None and rarity not in RARITIES:
        raise CatalogError('Invalid card catalog record')
    set_code = _text(record.get('set'))
    if set_code is not None and (len(set_code) > 5 or re.fullmatch(r'[a-z0-9]+', set_code) is None):
        raise CatalogError('Invalid card catalog record')
    collector_number = _text(record.get('collector_number'))
    if collector_number is not None and (not collector_number.strip() or len(collector_number) > 32 or
                                         any(not ch.isprintable() for ch in collector_number)):
        raise CatalogError('Invalid card catalog record')
    legalities = record.get('legalities', {})
    if legalities is None:
        legalities = {}
    if (not isinstance(legalities, dict) or any(
        not isinstance(fmt, str) or fmt not in LEGALITY_FORMATS or
        not isinstance(status, str) or status not in LEGALITY_STATUSES
        for fmt, status in legalities.items()
    )):
        raise CatalogError('Invalid card catalog record')
    legalities_json = json.dumps(legalities, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    colors_json = json.dumps(colors, ensure_ascii=False, separators=(',', ':'))
    faces_json = json.dumps(normalized_faces, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    values = (
        arena_id,
        name,
        facts['mana_cost'],
        facts['type_line'],
        facts['oracle_text'],
        colors_json,
        facts['power'],
        facts['toughness'],
        facts['layout'],
        faces_json,
        cmc,
        rarity,
        set_code,
        collector_number,
        legalities_json,
    )
    return arena_id, values


def _read_jsonl(path: Path) -> Iterator[bytes]:
    """Yield bounded records, transparently opening gzip-compressed JSONL."""
    with path.open('rb') as source:
        magic = source.read(2)
        source.seek(0)
        if magic == b'\x1f\x8b':
            with gzip.GzipFile(fileobj=source, mode='rb') as stream:
                while True:
                    line = stream.readline(MAX_RECORD_BYTES + 1)
                    if not line:
                        return
                    if len(line) > MAX_RECORD_BYTES:
                        raise CatalogError('Card catalog record is too large')
                    yield line
        else:
            while True:
                line = source.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_RECORD_BYTES:
                    raise CatalogError('Card catalog record is too large')
                yield line


def _database_identity(path: Path) -> str:
    return str(path.resolve(strict=False))


def import_catalog(source: Path, database: Path) -> ImportSummary:
    """Stream a Scryfall JSONL file into a new SQLite catalog atomically."""
    source = Path(source)
    database = Path(database)
    try:
        if not source.is_file():
            raise CatalogError('Card catalog source is unavailable')
        before = source.stat()
        if _database_identity(source) == _database_identity(database):
            raise CatalogError('Card catalog source and destination must differ')
        if database.exists() and os.path.samefile(source, database):
            raise CatalogError('Card catalog source and destination must differ')
        database.parent.mkdir(parents=True, exist_ok=True)
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=f'.{database.name}.', suffix='.tmp', dir=database.parent, delete=False)
        temp_path = Path(temp_handle.name)
        temp_handle.close()
    except CatalogError:
        raise
    except OSError:
        raise CatalogError('Card catalog source or destination is unavailable') from None

    imported = missing = duplicates = 0
    imported_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp_path)
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.executescript('''
            CREATE TABLE cards (
                arena_id INTEGER PRIMARY KEY CHECK (arena_id > 0),
                name TEXT NOT NULL,
                mana_cost TEXT,
                type_line TEXT,
                oracle_text TEXT,
                colors_json TEXT NOT NULL,
                power TEXT,
                toughness TEXT,
                layout TEXT,
                card_faces_json TEXT NOT NULL,
                cmc REAL CHECK (cmc IS NULL OR cmc >= 0),
                rarity TEXT,
                set_code TEXT,
                collector_number TEXT,
                legalities_json TEXT NOT NULL
            );
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX cards_name_lookup ON cards(name COLLATE NOCASE);
            PRAGMA user_version=2;
        ''')
        connection.execute('BEGIN IMMEDIATE')
        for line in _read_jsonl(source):
            if not line.strip():
                raise CatalogError('Invalid card catalog record')
            try:
                record = json.loads(line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CatalogError('Invalid card catalog record') from None
            arena_id, values = _normalize_card(record)
            if arena_id is None:
                missing += 1
                continue
            previous = connection.execute(
                'SELECT arena_id, name, mana_cost, type_line, oracle_text, colors_json, '
                'power, toughness, layout, card_faces_json, cmc, rarity, set_code, '
                'collector_number, legalities_json FROM cards WHERE arena_id=?',
                (arena_id,),
            ).fetchone()
            if previous is not None:
                if tuple(previous) != values:
                    raise CatalogError('Conflicting Arena ID in card catalog')
                duplicates += 1
                continue
            connection.execute(
                'INSERT INTO cards (arena_id, name, mana_cost, type_line, oracle_text, '
                'colors_json, power, toughness, layout, card_faces_json, cmc, rarity, '
                'set_code, collector_number, legalities_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', values)
            imported += 1

        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CatalogError('Card catalog source changed during import')
        metadata = {
            'schema_version': str(SCHEMA_VERSION),
            'imported_at_utc': imported_at,
            'source_bytes': str(before.st_size),
            'imported_count': str(imported),
            'missing_arena_id_count': str(missing),
            'duplicate_count': str(duplicates),
        }
        connection.executemany('INSERT INTO metadata (key, value) VALUES (?, ?)', metadata.items())
        connection.commit()
        connection.close()
        connection = None
        os.replace(temp_path, database)
        return ImportSummary(imported, missing, duplicates, before.st_size, imported_at)
    except CatalogError:
        raise
    except (OSError, EOFError, UnicodeError, sqlite3.Error, zlib.error):
        raise CatalogError('Unable to import card catalog') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        for leftover in (temp_path, Path(str(temp_path) + '-journal'),
                         Path(str(temp_path) + '-wal'), Path(str(temp_path) + '-shm')):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _open_read_only(database: Path) -> Iterator[sqlite3.Connection]:
    database = Path(database)
    if not database.is_file():
        raise CatalogError('Card catalog is unavailable')
    connection = None
    try:
        uri = database.resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        metadata = dict(connection.execute('SELECT key, value FROM metadata'))
        if (version not in READABLE_SCHEMA_VERSIONS or
                metadata.get('schema_version') != str(version)):
            raise CatalogError('Unsupported card catalog schema')
        yield connection
    except CatalogError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise CatalogError('Invalid card catalog') from None
    finally:
        if connection is not None:
            connection.close()


def load_card_names(database: Path) -> dict[int, str]:
    """Load the compact Arena-ID name map for sanitized snapshot rendering."""
    names: dict[int, str] = {}
    with _open_read_only(database) as connection:
        try:
            rows = connection.execute('SELECT arena_id, name FROM cards ORDER BY arena_id')
            for row in rows:
                arena_id, name = row['arena_id'], row['name']
                if type(arena_id) is not int or arena_id <= 0 or not isinstance(name, str) or not name.strip():
                    raise CatalogError('Invalid card catalog')
                names[arena_id] = name
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError('Invalid card catalog') from None
    return names


def get_catalog_metadata(database: Path) -> dict[str, Any]:
    """Return safe catalog version/date/count metadata without exposing source paths."""
    with _open_read_only(database) as connection:
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            metadata = dict(connection.execute('SELECT key, value FROM metadata'))
        except sqlite3.Error:
            raise CatalogError('Invalid card catalog') from None
    imported_at = metadata.get('imported_at_utc')
    if imported_at is not None:
        if not isinstance(imported_at, str):
            raise CatalogError('Invalid card catalog')
        try:
            datetime.fromisoformat(imported_at.replace('Z', '+00:00'))
        except ValueError:
            raise CatalogError('Invalid card catalog') from None
    result: dict[str, Any] = {
        'schema_version': version,
        'imported_at_utc': imported_at,
    }
    for key in ('source_bytes', 'imported_count', 'missing_arena_id_count', 'duplicate_count'):
        value = metadata.get(key)
        if value is None:
            result[key] = None
            continue
        if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]*', value) is None:
            raise CatalogError('Invalid card catalog')
        if len(value) > 20:
            raise CatalogError('Invalid card catalog')
        result[key] = int(value)
    return result


def search_card_printings(database: Path, name: str, set_code: str | None = None,
                          collector_number: str | None = None) -> list[dict[str, Any]]:
    """Find at most two exact-name printings so callers can reject ambiguity."""
    if not isinstance(name, str) or not name.strip():
        raise CatalogError('Invalid card name')
    if (set_code is None) != (collector_number is None):
        raise CatalogError('Incomplete printing identity')
    with _open_read_only(database) as connection:
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            query = 'SELECT arena_id FROM cards WHERE name = ? COLLATE NOCASE'
            params: tuple[Any, ...] = (name.strip(),)
            if set_code is not None:
                if version != 2:
                    return []
                query += ' AND set_code = ? AND collector_number = ?'
                params += (set_code.lower(), collector_number)
            query += ' ORDER BY arena_id LIMIT 2'
            ids = [row['arena_id'] for row in connection.execute(query, params)]
        except sqlite3.Error:
            raise CatalogError('Invalid card catalog') from None
    result = []
    for arena_id in ids:
        card = lookup_card(database, arena_id)
        if card is not None:
            result.append(card)
    return result


def list_format_card_printings(database: Path, format_name: str, *, limit: int = 20_000) -> dict[str, Any]:
    """Return bounded, validated schema-v2 printings legal or restricted in one format."""
    if not isinstance(format_name, str) or format_name not in LEGALITY_FORMATS:
        raise CatalogError('Unsupported card format')
    if type(limit) is not int or not 1 <= limit <= MAX_LEGAL_CATALOG_RESULTS:
        raise CatalogError('Invalid card catalog result limit')
    with _open_read_only(database) as connection:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version == 1:
            return {'schema_version': version, 'cards': [], 'truncated': False}
        try:
            rows = connection.execute(
                'SELECT arena_id, name, mana_cost, type_line, oracle_text, colors_json, '
                'power, toughness, layout, card_faces_json, cmc, rarity, set_code, '
                'collector_number, legalities_json FROM cards '
                'WHERE legalities_json LIKE ? OR legalities_json LIKE ? '
                'ORDER BY arena_id LIMIT ?',
                (f'%"{format_name}":"legal"%', f'%"{format_name}":"restricted"%', limit + 1),
            ).fetchall()
        except sqlite3.Error:
            raise CatalogError('Invalid card catalog') from None
    truncated = len(rows) > limit
    cards = []
    returned_bytes = 0
    for row in rows[:limit]:
        card = _card_from_row(row, 2)
        projected = {key: card[key] for key in (
            'arena_id', 'name', 'mana_cost', 'type_line', 'oracle_text', 'colors',
            'cmc', 'rarity', 'set', 'collector_number', 'legalities',
        )}
        encoded_size = len(json.dumps(projected, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if encoded_size > MAX_FORMAT_CATALOG_BYTES - returned_bytes:
            truncated = True
            break
        cards.append(projected)
        returned_bytes += encoded_size
    return {'schema_version': version, 'cards': cards, 'truncated': truncated}


def _card_from_row(row: sqlite3.Row, version: int) -> dict[str, Any]:
    try:
        colors = json.loads(row['colors_json'])
        faces = json.loads(row['card_faces_json'])
        legalities = json.loads(row['legalities_json']) if version == 2 else {}
        if (not isinstance(colors, list) or any(not isinstance(color, str) or color not in COLOR_CODES
                                                for color in colors) or
                len(set(colors)) != len(colors)):
            raise CatalogError('Invalid card catalog')
        if not isinstance(faces, list):
            raise CatalogError('Invalid card catalog')
        if (not isinstance(legalities, dict) or any(
            not isinstance(fmt, str) or fmt not in LEGALITY_FORMATS or
            not isinstance(status, str) or status not in LEGALITY_STATUSES
            for fmt, status in legalities.items()
        )):
            raise CatalogError('Invalid card catalog')
        face_fields = {'name', *FACE_TEXT_FIELDS}
        for face in faces:
            if not isinstance(face, dict) or set(face) != face_fields:
                raise CatalogError('Invalid card catalog')
            if (not isinstance(face['name'], str) or not face['name'].strip() or
                    any(not ch.isprintable() for ch in face['name'])):
                raise CatalogError('Invalid card catalog')
            if any(face[field] is not None and not isinstance(face[field], str)
                   for field in FACE_TEXT_FIELDS):
                raise CatalogError('Invalid card catalog')
        for value in (row['name'], row['mana_cost'], row['type_line'], row['oracle_text'],
                      row['power'], row['toughness'], row['layout']):
            if value is not None and not isinstance(value, str):
                raise CatalogError('Invalid card catalog')
        if (type(row['arena_id']) is not int or row['arena_id'] <= 0 or
                not isinstance(row['name'], str) or not row['name'].strip() or
                any(not ch.isprintable() for ch in row['name'])):
            raise CatalogError('Invalid card catalog')
        result: dict[str, Any] = {
            'arena_id': row['arena_id'],
            'name': row['name'],
            'mana_cost': row['mana_cost'],
            'type_line': row['type_line'],
            'oracle_text': row['oracle_text'],
            'colors': colors,
            'power': row['power'],
            'toughness': row['toughness'],
            'layout': row['layout'],
            'card_faces': faces,
        }
        if version == 2:
            if (row['cmc'] is not None and
                    (type(row['cmc']) not in (int, float) or row['cmc'] < 0 or not math.isfinite(row['cmc']))):
                raise CatalogError('Invalid card catalog')
            if row['rarity'] is not None and row['rarity'] not in RARITIES:
                raise CatalogError('Invalid card catalog')
            if (row['set_code'] is not None and
                    (not isinstance(row['set_code'], str) or len(row['set_code']) > 5 or
                     re.fullmatch(r'[a-z0-9]+', row['set_code']) is None)):
                raise CatalogError('Invalid card catalog')
            if (row['collector_number'] is not None and
                    (not isinstance(row['collector_number'], str) or not row['collector_number'].strip() or
                     len(row['collector_number']) > 32 or
                     any(not ch.isprintable() for ch in row['collector_number']))):
                raise CatalogError('Invalid card catalog')
            result.update({
                'cmc': row['cmc'], 'rarity': row['rarity'], 'set': row['set_code'],
                'collector_number': row['collector_number'], 'legalities': legalities,
            })
        else:
            result.update({'cmc': None, 'rarity': None, 'set': None,
                           'collector_number': None, 'legalities': {}})
        return result
    except CatalogError:
        raise
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        raise CatalogError('Invalid card catalog') from None


def lookup_card(database: Path, arena_id: int) -> dict[str, object] | None:
    """Read one allowlisted card record without modifying the catalog."""
    if type(arena_id) is not int or arena_id <= 0 or arena_id > 2**63 - 1:
        raise CatalogError('Invalid Arena ID')
    with _open_read_only(database) as connection:
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 2:
                row = connection.execute(
                    'SELECT arena_id, name, mana_cost, type_line, oracle_text, colors_json, '
                    'power, toughness, layout, card_faces_json, cmc, rarity, set_code, '
                    'collector_number, legalities_json FROM cards WHERE arena_id=?', (arena_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    'SELECT arena_id, name, mana_cost, type_line, oracle_text, colors_json, '
                    'power, toughness, layout, card_faces_json FROM cards WHERE arena_id=?', (arena_id,),
                ).fetchone()
            if row is None:
                return None
            return _card_from_row(row, version)
        except CatalogError:
            raise
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise CatalogError('Invalid card catalog') from None


def _batch_card_from_row(row: sqlite3.Row, version: int) -> dict[str, object]:
    """Decode only facts needed by visible-decision catalog enrichment."""
    arena_id = row['arena_id']
    name = row['name']
    if (type(arena_id) is not int or not 1 <= arena_id <= 2**63 - 1 or
            not isinstance(name, str) or not name.strip() or
            any(not character.isprintable() for character in name)):
        raise CatalogError('Invalid card catalog')

    result: dict[str, object] = {'arena_id': arena_id, 'name': name}
    for field in ('mana_cost', 'type_line', 'oracle_text', 'power', 'toughness'):
        value = row[field]
        result[field] = (value if value is None or
                         isinstance(value, str) and all(character.isprintable() for character in value)
                         else MALFORMED_CATALOG_FIELD)

    try:
        colors = json.loads(row['colors_json'])
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        colors = MALFORMED_CATALOG_FIELD
    if (colors is not MALFORMED_CATALOG_FIELD and
            (not isinstance(colors, list) or
             any(not isinstance(color, str) or color not in COLOR_CODES for color in colors) or
             len(set(colors)) != len(colors))):
        colors = MALFORMED_CATALOG_FIELD
    result['colors'] = colors

    try:
        faces = json.loads(row['card_faces_json'])
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        faces = MALFORMED_CATALOG_FIELD
    face_fields = {'name', *FACE_TEXT_FIELDS}
    if faces is not MALFORMED_CATALOG_FIELD:
        if not isinstance(faces, list):
            faces = MALFORMED_CATALOG_FIELD
        else:
            for face in faces:
                if (not isinstance(face, dict) or set(face) != face_fields or
                        not isinstance(face['name'], str) or not face['name'].strip() or
                        any(not character.isprintable() for character in face['name']) or
                        any(value is not None and
                            (not isinstance(value, str) or
                             any(not character.isprintable() for character in value))
                            for field in FACE_TEXT_FIELDS
                            for value in (face[field],))):
                    faces = MALFORMED_CATALOG_FIELD
                    break
    result['card_faces'] = faces

    cmc: object = None
    if version == 2:
        cmc = row['cmc']
        if (cmc is not None and
                (type(cmc) not in (int, float) or cmc < 0 or cmc > 1_000 or
                 not math.isfinite(cmc))):
            cmc = MALFORMED_CATALOG_FIELD
    result['cmc'] = cmc
    return result


def lookup_cards(database: Path, arena_ids: Iterable[int]) -> dict[int, dict[str, object]]:
    """Read a bounded batch of visible card records by exact Arena ID only."""
    try:
        ordered_ids: dict[int, None] = {}
        for arena_id in arena_ids:
            if type(arena_id) is not int or not 1 <= arena_id <= 2**63 - 1:
                raise CatalogError('Invalid Arena ID')
            ordered_ids.setdefault(arena_id, None)
            if len(ordered_ids) > 128:
                raise CatalogError('Too many Arena IDs')
    except CatalogError:
        raise
    except (TypeError, ValueError):
        raise CatalogError('Invalid Arena IDs') from None

    if not ordered_ids:
        return {}

    with _open_read_only(database) as connection:
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            columns = (
                'arena_id, name, mana_cost, type_line, oracle_text, colors_json, '
                'power, toughness, card_faces_json' + (', cmc' if version == 2 else '')
            )
            placeholders = ', '.join('?' for _ in ordered_ids)
            rows = connection.execute(
                f'SELECT {columns} FROM cards WHERE arena_id IN ({placeholders})',
                tuple(ordered_ids),
            ).fetchall()
            by_id = {row['arena_id']: _batch_card_from_row(row, version) for row in rows}
            return {arena_id: by_id[arena_id] for arena_id in ordered_ids if arena_id in by_id}
        except CatalogError:
            raise
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError, OverflowError,
                RecursionError):
            raise CatalogError('Invalid card catalog') from None
