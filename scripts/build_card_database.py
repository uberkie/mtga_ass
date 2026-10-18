"""Build the assistant's SQLite catalog from a downloaded Scryfall default_cards JSON.

Usage: python scripts/build_card_database.py default-cards.json cards.sqlite3

Reads the large JSON array incrementally without loading the file into memory.
Only local files are accessed. No API keys or game credentials are needed.
"""
from __future__ import annotations
import gzip
import json
from pathlib import Path
import sys
import tempfile

# Allow running this script directly from an extracted project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtga_assistant.cards import import_catalog

MAX_OBJECT_SIZE = 4 * 1024 * 1024
READ_SIZE = 64 * 1024


def array_records(source: Path):
    """Stream a JSON array with bounded per-object memory consumption."""
    path = Path(source)
    with path.open('rb') as check:
        gzipped = check.read(2) == b'\x1f\x8b'
    open_file = gzip.open if gzipped else open
    with open_file(path, 'rt', encoding='utf-8-sig') as fp:
        decoder = json.JSONDecoder()
        buffer = ''
        exhausted = False

        def next_token():
            nonlocal buffer, exhausted
            while not buffer.lstrip() and not exhausted:
                data = fp.read(READ_SIZE)
                if data:
                    buffer += data
                else:
                    exhausted = True
            buffer = buffer.lstrip()
            return buffer[0] if buffer else None

        if next_token() != '[':
            raise ValueError('Expected Scryfall default_cards bulk JSON array.')
        buffer = buffer[1:]
        expecting_record = True
        while True:
            character = next_token()
            if character == ']' and expecting_record:
                buffer = buffer[1:]
                break
            if not expecting_record:
                if character == ']':
                    buffer = buffer[1:]
                    break
                if character != ',':
                    raise ValueError('Invalid JSON array delimiter.')
                buffer = buffer[1:]
            expecting_record = False
            while True:
                buffer = buffer.lstrip()
                try:
                    record, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    data = fp.read(READ_SIZE)
                    if not data or len(buffer) > MAX_OBJECT_SIZE:
                        raise ValueError('Incomplete or oversized Scryfall record.') from None
                    buffer += data
                    continue
                if not isinstance(record, dict):
                    raise ValueError('Expected Scryfall card records.')
                yield record
                buffer = buffer[end:]
                break
        if next_token() is not None:
            raise ValueError('Extra data after the Scryfall bulk array.')


def build(source: Path, database: Path):
    source, database = Path(source), Path(database)
    if source.resolve() == database.resolve():
        raise ValueError('Source and destination must differ.')
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mtga-cards-') as folder:
        path = Path(folder) / 'cards.jsonl'
        with path.open('w', encoding='utf-8') as out:
            for record in array_records(source):
                out.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        summary = import_catalog(path, database)
    return summary


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python scripts/build_card_database.py default-cards.json cards.sqlite3')
    try:
        summary = build(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError) as exc:
        raise SystemExit('Could not build card database: ' + str(exc)[:200])
    print(f'Imported {summary.imported} Arena-ID cards into {sys.argv[2]}.')
