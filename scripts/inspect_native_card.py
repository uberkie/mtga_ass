"""Read-only diagnostic of Arena's locally installed card and ability tables."""
from pathlib import Path
import argparse
import json
import sys

# Let the script run directly from the extracted project directory on Windows.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtga_assistant.arena_data import ArenaCardResolver, discover_mtga_database
from mtga_assistant.paths import default_arena_log


def main():
    parser = argparse.ArgumentParser(description='Inspect local Arena card and ability text (read only)')
    parser.add_argument('grpids', nargs='+', type=int, help='Arena card grpId(s)')
    parser.add_argument('--db', type=Path, default=None,
                        help='Optional explicit Raw_CardDatabase_*.mtga path for diagnostics')
    parser.add_argument('--log', type=Path, default=None,
                        help='Optional Player.log path for finding Arena install')
    args = parser.parse_args()
    db, loc = ((args.db, None) if args.db else
               discover_mtga_database(args.log or default_arena_log()))
    if not db:
        parser.error('Native database not found. Supply --db with its path.')
    resolver = ArenaCardResolver(db, loc)
    try:
        if resolver.connection is None:
            parser.error('Native SQLite database could not be opened: ' +
                         str(resolver.discovery_error))
        records = resolver.lookup_cards(args.grpids[:32])
        print('Native database:', db)
        print('Card lookup mode:', resolver.mode)
        for gid in args.grpids[:32]:
            data = records.get(gid)
            print(json.dumps({'grpId': gid, 'resolved': data or None},
                             ensure_ascii=False, indent=2))
    finally:
        resolver.close()


if __name__ == '__main__':
    main()
