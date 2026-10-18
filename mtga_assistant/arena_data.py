"""Automatic, bounded MTGA local-card lookup and GRE deck-index extraction.

Read-only towards MTGA files. Card definitions are queried by grpId; we never
copy or load the whole Arena card database. The per-match selected deck is
prefetched, and unfamiliar visible cards are resolved on demand.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
import json
import html
import os
import re
import sqlite3


def _column(columns: list[str], *candidates: str) -> str | None:
    lower = {name.lower(): name for name in columns}
    return next((lower[x.lower()] for x in candidates if x.lower() in lower), None)


def discover_mtga_database(log_path: Path) -> tuple[Path | None, Path | None]:
    """Find Arena SQLite, using the game's own emitted install paths first.

    Never download or edit MTGA files. The return's second element is an optional
    localization database, which some Arena versions store separately.
    """
    raw_dirs: list[Path] = []
    try:
        # Startup logs normally print the actual installation path. Read only
        # a bounded prefix; this is not a scan of the match/game data.
        prefix = log_path.open('rb').read(96_000).decode('utf-8', 'replace')
        for match in re.finditer(r'(?i)([A-Z]:[/\\][^\r\n"{}]*?[/\\]Downloads[/\\]Raw)[/\\]', prefix):
            root = match.group(1).replace('\\', '/')
            raw_dirs.append(Path(root))
    except OSError:
        pass
    # Fallback paths for both standard and custom Steam libraries.
    localapp = os.environ.get('LOCALAPPDATA')
    programs = [Path(localapp).parent if localapp else Path.home()]
    for base in programs:
        raw_dirs.append(base / 'Wizards Of The Coast' / 'MTGA' / 'MTGA_Data' / 'Downloads' / 'Raw')
    if os.name == 'nt':
        for drive in 'CDEFGH':
            for prefix in ('SteamLibrary/steamapps/common/MTGA',
                           'Program Files (x86)/Steam/steamapps/common/MTGA',
                           'Program Files/Wizards of the Coast/MTGA'):
                raw_dirs.append(Path(f'{drive}:/{prefix}/MTGA_Data/Downloads/Raw'))
    visited = set()
    for directory in raw_dirs:
        if str(directory).lower() in visited:
            continue
        visited.add(str(directory).lower())
        try:
            databases = sorted(directory.glob('Raw_CardDatabase_*.mtga'),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            localization = sorted(directory.glob('Raw_ClientLocalization_*.mtga'),
                                  key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        if databases:
            return databases[0], localization[0] if localization else None
    return None, None


class ArenaCardResolver:
    """Compatibility mapping for state.public_snapshot and batched coach lookup.

    Uses SELECT ... WHERE GrpId IN (...) only, with bounded caches. `get` is
    deliberately lazy because the state projector reads `card_names.get(gid)`.
    """
    def __init__(self, db_path: Path | None, localization_path: Path | None = None,
                 limit: int = 2048):
        self.path = Path(db_path).resolve() if db_path else None
        self.localization_path = Path(localization_path).resolve() if localization_path else None
        self.limit = limit
        self.cache: dict[int, dict[str, Any]] = {}
        # Bounded on-demand ability cache; no global table scan.
        self._ability_cache: dict[int, dict[str, Any] | None] = {}
        self.ability_table = self.ability_id_col = self.ability_text_col = None
        self.ability_category_col = None
        self.misses: set[int] = set()
        self.connection: sqlite3.Connection | None = None
        self.fallback_connection: sqlite3.Connection | None = None
        self.fallback_hits = 0
        self.discovery_error: str | None = None
        self.localization_connection: sqlite3.Connection | None = None
        self.card_table = self.id_col = self.title_col = self.name_col = None
        self.loc_table = self.loc_id_col = self.loc_value_col = None
        # Keep all compatible localization sources: a matched table schema does
        # not prove a particular TitleId is present in that table.
        self._localization_sources: list[tuple[sqlite3.Connection, str, str, str]] = []
        self._extra_localization_connections: list[sqlite3.Connection] = []
        self.mode = 'unavailable'
        self.names = _LazyCardNames(self)
        if db_path:
            self._open()
        self._open_fallback()

    def _open_fallback(self) -> None:
        # Older name-only Arena catalog supplied with the user's original
        # project. Native installed files always take precedence. The fallback
        # prevents a missing/changed localization schema from disabling coaching.
        fallback = Path(__file__).parent / 'data' / 'historical_names.sqlite3'
        if not fallback.is_file():
            return
        try:
            self.fallback_connection = sqlite3.connect(fallback.resolve().as_uri() + '?mode=ro', uri=True)
            self.fallback_connection.row_factory = sqlite3.Row
            self.fallback_connection.execute('SELECT name FROM cards LIMIT 1').fetchone()
            if self.connection is None:
                self.mode = 'historical_fallback'
        except (sqlite3.Error, OSError) as exc:
            self.discovery_error = str(exc)[:200]
            if self.fallback_connection is not None:
                self.fallback_connection.close()
                self.fallback_connection = None

    def _open(self) -> None:
        try:
            assert self.path is not None
            conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            tables = {row[0].lower(): row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            name = tables.get('cards')
            if not name:
                raise ValueError('Arena card table missing')
            cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
            self.id_col = _column(cols, 'GrpId', 'arena_id')
            self.title_col = _column(cols, 'TitleId')
            self.name_col = _column(cols, 'name')
            if not self.id_col or not (self.title_col or self.name_col):
                raise ValueError('Unrecognized Arena card table schema')
            self.card_table = name
            ability_table = tables.get('abilities')
            if ability_table:
                ability_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{ability_table}")')]
                aid = _column(ability_cols, 'Id')
                text_id = _column(ability_cols, 'TextId')
                if aid and text_id:
                    self.ability_table = ability_table
                    self.ability_id_col = aid
                    self.ability_text_col = text_id
                    self.ability_category_col = _column(ability_cols, 'Category')
            self.connection = conn
            self.mode = 'native' if self.title_col else 'named_catalog'
            if self.title_col:
                # In the 2026 Arena schema BOTH `Localizations` and the
                # language-specific `Localizations_enUS` can be present. The
                # latter carries real card titles even when the former does
                # not have a matching row. Try it first, but retain others as
                # per-card fallbacks rather than accepting the first schema.
                seen: set[Path] = set()
                for db in (self.path, self.localization_path):
                    if db is None or db in seen:
                        continue
                    seen.add(db)
                    localization_conn = conn if db == self.path else sqlite3.connect(
                        db.as_uri() + '?mode=ro', uri=True)
                    localization_conn.row_factory = sqlite3.Row
                    sources = self._discover_localizations(localization_conn)
                    if sources:
                        self._localization_sources.extend(
                            (localization_conn, *source) for source in sources)
                        if localization_conn is not conn:
                            self._extra_localization_connections.append(localization_conn)
                    elif localization_conn is not conn:
                        localization_conn.close()
                # Apply language preference ACROSS both native/localization
                # files, not just within an individual SQLite file.
                # An explicit enUS table must win over generic Localizations.
                self._localization_sources.sort(
                    key=lambda source: 0 if source[1].lower() == 'localizations_enus'
                    else 1 if source[3].lower() == 'enus' else 2)
                if self._localization_sources:
                    (self.localization_connection, self.loc_table,
                     self.loc_id_col, self.loc_value_col) = self._localization_sources[0]
        except (sqlite3.Error, OSError, ValueError, AssertionError) as exc:
            self.discovery_error = str(exc)[:200]
            self.close()

    def _discover_localizations(self, conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
        candidates = []
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table.lower() not in ('localizations', 'localizations_enus'):
                continue
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            id_col = _column(cols, 'LocId')
            value_col = (_column(cols, 'Loc', 'enUS') if table.lower() == 'localizations_enus'
                         else _column(cols, 'enUS', 'Loc'))
            if id_col and value_col:
                candidates.append((table, id_col, value_col))
        # Dedicated English table wins over a generic multilingual table.
        candidates.sort(key=lambda item: 0 if item[0].lower() == 'localizations_enus' else 1)
        return candidates

    def prefetch(self, ids: Mapping[int, int] | list[int] | tuple[int, ...]) -> None:
        values = list(dict.fromkeys(i for i in ids if type(i) is int and i > 0))[:512]
        missing = [i for i in values if i not in self.cache and i not in self.misses]
        if not missing:
            return
        if self.connection is not None:
            assert self.id_col and self.card_table
            try:
                for start in range(0, len(missing), 300):
                    batch = missing[start:start + 300]
                    placeholders = ','.join('?' for _ in batch)
                    query = (f'SELECT * FROM "{self.card_table}" WHERE "{self.id_col}" '
                             f'IN ({placeholders})')
                    for row in self.connection.execute(query, batch):
                        gid = row[self.id_col]
                        if type(gid) is not int or gid <= 0:
                            continue
                        label = row[self.name_col] if self.name_col else None
                        if (not isinstance(label, str) or not label.strip()) and self.title_col:
                            label = self._localize(row[self.title_col])
                        if isinstance(label, str) and label.strip() and len(self.cache) < self.limit:
                            definition: dict[str, Any] = {
                                'arena_id': gid, 'name': label.strip()[:150]}
                            keys = row.keys()
                            if 'TypeTextId' in keys:
                                type_text = self._localize(row['TypeTextId'])
                                if type_text:
                                    definition['type_line'] = self._clean_text(type_text, 256)
                            if 'AbilityIds' in keys:
                                text = self._card_ability_text(row['AbilityIds'])
                                if text:
                                    definition['ability_text'] = text
                            self.cache[gid] = definition
            except sqlite3.Error as exc:
                self.discovery_error = 'Native lookup failed: ' + str(exc)[:160]
                self.mode = 'native_query_error'
        still_missing = [i for i in missing if i not in self.cache]
        if self.fallback_connection is not None and still_missing:
            try:
                for start in range(0, len(still_missing), 300):
                    batch = still_missing[start:start + 300]
                    placeholders = ','.join('?' for _ in batch)
                    for row in self.fallback_connection.execute(
                            f'SELECT arena_id, name FROM cards WHERE arena_id IN ({placeholders})', batch):
                        label = row['name']
                        if isinstance(label, str) and label.strip() and len(self.cache) < self.limit:
                            self.cache[row['arena_id']] = {
                                'arena_id': row['arena_id'], 'name': label.strip()[:150]}
                            self.fallback_hits += 1
                if self.fallback_hits and self.connection is not None and self.mode == 'native':
                    self.mode = 'native+historical_fallback'
            except sqlite3.Error as exc:
                self.discovery_error = 'Fallback lookup failed: ' + str(exc)[:160]
        self.misses.update(set(missing) - set(self.cache))

    @staticmethod
    def _clean_text(value: str, limit: int) -> str:
        # Arena localization can have cosmetic <nobr> wrappers, entities, and
        # alternate formatting versions. Preserve rules words, not markup.
        value = re.sub(r'</?(?:nobr|b|i|u|small|span)(?:\s+[^<>]*)?>', '',
                       value, flags=re.IGNORECASE)
        value = html.unescape(value)
        return ' '.join(value.split())[:limit]

    def _localize(self, title_id: Any) -> str | None:
        if type(title_id) is not int or title_id <= 0:
            return None
        for conn, table, id_col, value_col in self._localization_sources:
            try:
                # Multiple presentation variants can share the same LocId.
                # Prefer the least-marked text, deterministically.
                rows = conn.execute(
                    f'SELECT "{value_col}" FROM "{table}" '
                    f'WHERE "{id_col}"=? LIMIT 12', (title_id,)).fetchall()
                options = [r[0].strip() for r in rows if isinstance(r[0], str) and r[0].strip()]
                if options:
                    chosen = min(options, key=lambda t: ('<' in t or '>' in t,
                                                         t.count('<'), len(t), t))
                    return self._clean_text(chosen, 2048)
            except sqlite3.Error as exc:
                self.discovery_error = 'Localization lookup failed: ' + str(exc)[:160]
        return None

    def _resolve_ability(self, ability_id: int) -> dict[str, Any] | None:
        if ability_id in self._ability_cache:
            return self._ability_cache[ability_id]
        result = None
        if (self.connection is not None and self.ability_table and
                self.ability_id_col and self.ability_text_col):
            try:
                row = self.connection.execute(
                    f'SELECT * FROM "{self.ability_table}" '
                    f'WHERE "{self.ability_id_col}"=? LIMIT 1', (ability_id,)).fetchone()
                if row is not None and type(row[self.ability_text_col]) is int:
                    text_id = row[self.ability_text_col]
                    localized = self._localize(text_id)
                    if localized:
                        result = {'text_id': text_id, 'text': localized,
                                  'category': (row[self.ability_category_col]
                                               if self.ability_category_col else None)}
            except sqlite3.Error as exc:
                self.discovery_error = 'Ability lookup failed: ' + str(exc)[:160]
        if len(self._ability_cache) < 4096:
            self._ability_cache[ability_id] = result
        return result

    def _card_ability_text(self, raw: Any) -> str | None:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 2048:
            return None
        # Confirmed 2026 schema: "1222:2331,1223:232069". The right-hand
        # TextId MUST match the Abilities table; never blindly use arbitrary IDs.
        fragments = raw.split(',')
        if len(fragments) > 16:
            return None
        texts: list[str] = []
        seen: set[int] = set()
        for fragment in fragments:
            match = re.fullmatch(r'\s*([1-9]\d*):([1-9]\d*)\s*', fragment)
            if not match:
                continue
            aid, tid = map(int, match.groups())
            if aid in seen:
                continue
            seen.add(aid)
            ability = self._resolve_ability(aid)
            if ability is None or ability['text_id'] != tid:
                continue
            value = ability['text']
            if value and value not in texts:
                texts.append(value)
        if not texts:
            return None
        # Do not silently truncate an ability sentence in mid-word.
        combined = ' | '.join(texts)
        if len(combined) > 1024:
            return None
        return combined

    def name(self, gid: int | None) -> str | None:
        if type(gid) is not int or gid <= 0:
            return None
        self.prefetch([gid])
        return self.cache.get(gid, {}).get('name')

    def lookup_cards(self, ids: tuple[int, ...] | list[int]) -> dict[int, dict[str, Any]]:
        self.prefetch(ids)
        return {i: dict(self.cache[i]) for i in ids if i in self.cache}

    def close(self) -> None:
        if self.fallback_connection:
            self.fallback_connection.close()
        self.fallback_connection = None
        for conn in self._extra_localization_connections:
            if conn is not self.connection:
                conn.close()
        self._extra_localization_connections.clear()
        if self.connection:
            self.connection.close()
        self.connection = self.localization_connection = None
        self._ability_cache.clear()
        self.ability_table = self.ability_id_col = self.ability_text_col = None
        self.ability_category_col = None
        self._localization_sources.clear()
        self.loc_table = self.loc_id_col = self.loc_value_col = None
        self.mode = 'unavailable'


class _LazyCardNames(dict):
    def __init__(self, resolver: ArenaCardResolver):
        super().__init__()
        self.resolver = resolver

    def __bool__(self):
        # State's optional-name-map fallback uses `names or {}`.
        return True

    def get(self, key: Any, default: Any = None):
        name = self.resolver.name(key)
        return name if name is not None else default



def enrich_visible_snapshot(snapshot: dict[str, Any],
                            visible_context: Mapping[int, Mapping[str, Any]],
                            resolver: ArenaCardResolver, max_cards: int = 48) -> None:
    """Attach only verified visible-card text, never private opponent identities.

    Caller MUST supply GameState._visible_card_context output (visibility-
    checked), not raw GRE objects. IDs are internal joins, not shown to the LLM.
    """
    if not isinstance(snapshot.get('zones'), list):
        return
    eligible: dict[int, tuple[int, str]] = {}
    for zone in snapshot['zones']:
        if not isinstance(zone, dict) or not isinstance(zone.get('cards'), list):
            continue
        for card in zone['cards']:
            if not isinstance(card, dict) or card.get('identity_status') != 'visible':
                continue
            ctx = visible_context.get(card.get('id'))
            if not isinstance(ctx, Mapping):
                continue
            gid, label = ctx.get('arena_id'), ctx.get('label')
            if (type(gid) is int and isinstance(label, str) and
                    label == card.get('name') and len(eligible) < max_cards):
                eligible[card['id']] = (gid, label)
    records = resolver.lookup_cards(list({gid for gid, _ in eligible.values()}))
    for zone in snapshot['zones']:
        for card in zone.get('cards', []):
            if card.get('id') not in eligible:
                continue
            gid, label = eligible[card['id']]
            record = records.get(gid, {})
            if record.get('name') != label:
                continue
            catalog = {k: record[k] for k in ('type_line', 'ability_text')
                       if isinstance(record.get(k), str) and record[k]}
            if catalog:
                card['catalog'] = catalog

class ArenaDeckIndex:
    """Capture selected match deck and saved deck summaries from log envelopes.

    Never guesses an opponent's hidden cards or persists private tokens.
    """
    def __init__(self, cache_path: Path | None = None):
        self.selected: Counter[int] = Counter()
        self.saved: dict[str, dict[int, int]] = {}
        self.saved_names: dict[str, str] = {}
        self.collection: dict[int, int] = {}
        self.generation = 0
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.dirty = False
        if self.cache_path is not None:
            self.restore()

    def restore(self) -> None:
        if self.cache_path is None or not self.cache_path.is_file():
            return
        try:
            with sqlite3.connect(self.cache_path) as conn:
                for deck_id, name, text in conn.execute('SELECT deck_id, name, contents FROM saved_decks'):
                    cards = json.loads(text)
                    if isinstance(cards, dict):
                        self.saved[deck_id] = {int(k): v for k, v in cards.items()
                                               if str(k).isdigit() and type(v) is int and v > 0}
                        if isinstance(name, str):
                            self.saved_names[deck_id] = name[:128]
                for gid, copies in conn.execute('SELECT arena_id, copies FROM owned_cards'):
                    if type(gid) is int and type(copies) is int:
                        self.collection[gid] = copies
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.saved.clear()
            self.saved_names.clear()
            self.collection.clear()

    def persist(self) -> bool:
        if not self.dirty or self.cache_path is None:
            return False
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.cache_path) as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS saved_decks ('
                             'deck_id TEXT PRIMARY KEY, name TEXT, contents TEXT NOT NULL)')
                conn.execute('CREATE TABLE IF NOT EXISTS owned_cards ('
                             'arena_id INTEGER PRIMARY KEY, copies INTEGER NOT NULL)')
                conn.executemany('INSERT OR REPLACE INTO saved_decks VALUES (?,?,?)',
                    ((deck_id, self.saved_names.get(deck_id, ''), json.dumps(cards))
                     for deck_id, cards in self.saved.items()))
                conn.executemany('INSERT OR REPLACE INTO owned_cards VALUES (?,?)',
                                 self.collection.items())
            self.dirty = False
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    def observe(self, payload: dict[str, Any]) -> bool:
        updated = False
        messages = (payload.get('greToClientEvent') or {}).get('greToClientMessages', [])
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict) or msg.get('type') != 'GREMessageType_ConnectResp':
                continue
            raw = (msg.get('connectResp') or {}).get('deckMessage', {}).get('deckCards')
            if isinstance(raw, list) and raw and all(type(i) is int and i > 0 for i in raw):
                new = Counter(raw)
                if new != self.selected:
                    self.selected = new
                    self.generation += 1
                    updated = True
        internal = payload.get('DecksInternal')
        if isinstance(internal, dict):
            for deckid, deck in internal.items():
                if not isinstance(deckid, str) or not isinstance(deck, dict):
                    continue
                new = {}
                for item in deck.get('MainDeck', []):
                    if not isinstance(item, dict):
                        continue
                    gid, count = item.get('cardId'), item.get('quantity')
                    if type(gid) is int and type(count) is int and gid > 0 and 0 < count < 256:
                        new[gid] = count
                if new and self.saved.get(deckid) != new:
                    self.saved[deckid] = new
                    self.dirty = True
        summary = payload.get('DeckSummaries')
        if isinstance(summary, list):
            for deck in summary:
                if isinstance(deck, dict) and isinstance(deck.get('DeckIdInternal'), str):
                    name = deck.get('Name')
                    if isinstance(name, str):
                        if self.saved_names.get(deck['DeckIdInternal']) != name[:128]:
                            self.saved_names[deck['DeckIdInternal']] = name[:128]
                            self.dirty = True
        # Arena's owned-card snapshot is build-dependent. Read only a bounded,
        # explicit ID->quantity mapping, never fabricate collection completeness.
        cards = payload.get('PlayerCards')
        if isinstance(cards, dict):
            for key, count in cards.items():
                try:
                    gid = int(key)
                except (TypeError, ValueError):
                    continue
                if gid > 0 and type(count) is int and count >= 0:
                    if self.collection.get(gid) != count:
                        self.collection[gid] = count
                        self.dirty = True
        return updated
