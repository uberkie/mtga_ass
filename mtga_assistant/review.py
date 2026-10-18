"""Bounded, visibility-aware post-game review summaries."""
from __future__ import annotations

from collections import Counter
import json
from typing import Any

from mtga_assistant.state import DIAGNOSTICS
from mtga_assistant.stream import StreamRecord

MAX_ENTRIES_PER_GAME = 250
MAX_GAMES_PER_LOG = 100
MAX_MATCHES_PER_LOG = 100
MAX_CHANGES_PER_ENTRY = 64
MAX_VISIBLE_INSTANCES = 20_000
MAX_LABEL_LENGTH = 128
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_DETAIL_BYTES = 6 * 1024 * 1024

EVENT_KINDS = (
    'turn_progress', 'life_total_change', 'zone_count_change',
    'visible_card_moved', 'continuity_note',
)
ZONE_NAMES = frozenset({'Hand', 'Battlefield', 'Graveyard', 'Exile', 'Stack', 'Library'})
PHASES = frozenset({'Phase_Beginning', 'Phase_Main1', 'Phase_Combat', 'Phase_Main2', 'Phase_Ending'})
STEPS = frozenset({
    'Step_Untap', 'Step_Upkeep', 'Step_Draw', 'Step_BeginCombat',
    'Step_DeclareAttack', 'Step_DeclareBlock', 'Step_CombatDamage',
    'Step_FirstStrikeDamage', 'Step_EndCombat', 'Step_End', 'Step_Cleanup',
})
GAME_STAGES = frozenset({'GameStage_Start', 'GameStage_Play', 'GameStage_GameOver'})
MATCH_STATES = frozenset({'MatchState_GameInProgress', 'MatchState_MatchComplete'})
SAFE_LIMITATIONS = DIAGNOSTICS | frozenset({
    'missing_turn', 'missing_life_totals', 'missing_zones',
    'unknown_zone_membership', 'missing_visible_objects', 'same_game_replaced',
    'source_reset', 'uncertain_continuity', 'no_supported_game_updates',
    'detail_limit_reached',
})
SOURCE_BREAK_DIAGNOSTICS = frozenset({
    'malformed_json', 'oversized_json', 'incomplete_json', 'invalid_schema',
    'state_gap', 'unsupported_state_message', 'unsupported_instance_remap',
    'unresolved_instance_remap', 'conflicting_zone', 'unsupported_zone',
})


class ReviewOutputError(ValueError):
    """The safe report cannot fit in the documented output limit."""


def _valid_int(value: Any, *, minimum: int | None = None) -> bool:
    return type(value) is int and (minimum is None or value >= minimum)


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = ''.join(ch for ch in value if ch.isprintable()).strip()
    if not value:
        return None
    return value[:MAX_LABEL_LENGTH]


def _safe_integrity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {'status': 'uncertain', 'limitations': ['invalid_schema']}
    status = value.get('status')
    if status not in ('waiting', 'partial', 'consistent', 'uncertain', 'ended'):
        status = 'uncertain'
    raw = value.get('limitations', [])
    limitations = sorted({code for code in raw if isinstance(code, str) and code in SAFE_LIMITATIONS}) \
        if isinstance(raw, list) else ['invalid_schema']
    return {'status': status, 'limitations': limitations}


def _safe_counter(counter: Counter) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter) if key in EVENT_KINDS and counter[key] > 0}


class ReviewBuilder:
    """Build a compact report from already visibility-filtered snapshots only."""

    def __init__(self, seat: int):
        if type(seat) is not int or seat not in (1, 2):
            raise ValueError('Invalid seat')
        self.seat = seat
        self._matches: list[dict[str, Any]] = []
        self._current_match: dict[str, Any] | None = None
        self._current_game: dict[str, Any] | None = None
        self._previous: dict[str, Any] | None = None
        self._match_sequence = 0
        self._game_sequence = 0
        self._total_games = 0
        self._observed_states = 0
        self._diagnostics: Counter[str] = Counter()
        self._last_state_diagnostics: Counter[str] = Counter()
        self._pending_break: set[str] = set()
        self._represented_breaks: set[str] = set()
        self._force_new_match = False
        self._detail_bytes = 0
        self._omitted_games = 0
        self._omitted_entries = 0
        self._omitted_game_summary = {
            'turn_start': None, 'turn_end': None,
            'event_counts': Counter({kind: 0 for kind in EVENT_KINDS}),
        }
        self._final_integrity = {'status': 'waiting', 'limitations': []}

    def record_stream(self, record: StreamRecord, state: Any, applied: int) -> None:
        """Retain safe diagnostic categories and continuity markers, never payloads."""
        seen_from_state: set[str] = set()
        represented_this_record: set[str] = set()
        if record.kind == 'reset':
            self._diagnostics['source_reset'] += 1
            self._pending_break.add('source_reset')
            self._force_new_match = True
            self._previous = None
        current = Counter(getattr(state, 'diagnostic_counts', {}))
        for code, amount in current.items():
            if not isinstance(code, str) or code not in DIAGNOSTICS or type(amount) is not int:
                continue
            delta = max(0, amount - self._last_state_diagnostics.get(code, 0))
            if delta:
                self._diagnostics[code] += delta
                seen_from_state.add(code)
                if code in SOURCE_BREAK_DIAGNOSTICS:
                    if code in self._represented_breaks:
                        self._represented_breaks.discard(code)
                        represented_this_record.add(code)
                    else:
                        self._pending_break.add(code)
                    if self._current_game is not None:
                        self._add_game_limitation(self._current_game, code)
        self._last_state_diagnostics = current
        if record.kind == 'diagnostic' and record.code in DIAGNOSTICS:
            code = record.code
            if code not in seen_from_state:
                self._diagnostics[code] += 1
            if code in SOURCE_BREAK_DIAGNOSTICS and code not in represented_this_record:
                self._pending_break.add(code)
                if self._current_game is not None:
                    self._add_game_limitation(self._current_game, code)
        try:
            self._final_integrity = _safe_integrity(state.integrity())
        except (AttributeError, TypeError, ValueError):
            self._final_integrity = {'status': 'uncertain', 'limitations': ['invalid_schema']}
        if 'source_reset' in self._pending_break and self._current_game is not None:
            self._add_game_limitation(self._current_game, 'source_reset')

    def observe(self, snapshot: dict[str, Any], boundary: str | None) -> None:
        """Consume one sanitized public snapshot and an optional safe boundary."""
        normalized = self._normalize(snapshot)
        self._observed_states += 1
        self._final_integrity = normalized['integrity']

        if self._force_new_match and self._current_match is not None:
            boundary = 'match_changed'
            normalized['integrity']['limitations'] = sorted(set(
                normalized['integrity']['limitations'] + ['source_reset']))
            normalized['integrity']['status'] = 'uncertain'
        self._force_new_match = False

        if boundary not in (None, 'match_changed', 'game_changed', 'same_game_replaced'):
            boundary = 'same_game_replaced'
            normalized['integrity']['limitations'] = sorted(set(
                normalized['integrity']['limitations'] + ['uncertain_continuity']))
            normalized['integrity']['status'] = 'uncertain'

        previous = self._previous
        if boundary is None and previous is not None:
            prior = set(previous['integrity']['limitations'])
            current_breaks = set(normalized['integrity']['limitations']) & SOURCE_BREAK_DIAGNOSTICS
            for code in current_breaks - prior:
                if code not in self._pending_break:
                    self._represented_breaks.add(code)
                self._pending_break.add(code)
        if boundary == 'match_changed':
            self._new_match()
            self._new_game(normalized)
            self._previous = normalized
            self._pending_break.clear()
            return
        if self._current_match is None and previous is None:
            self._new_match()
            self._new_game(normalized)
            self._previous = normalized
            self._pending_break.clear()
            return

        game_changed = (boundary == 'game_changed' or
                        (boundary is None and previous is not None and
                         normalized['game_number'] is not None and
                         previous['game_number'] is not None and
                         normalized['game_number'] != previous['game_number']))
        if game_changed:
            self._new_game(normalized)
            self._previous = normalized
            self._pending_break.clear()
            return

        if self._current_game is None:
            # The detail cap has been reached. Keep aggregate counts and turn range,
            # but do not retain another game's card names or timeline details.
            if boundary == 'same_game_replaced':
                self._pending_break.add('same_game_replaced')
            self._summarize_omitted_snapshot(normalized)
            self._previous = normalized
            self._pending_break.clear()
            return

        if boundary == 'same_game_replaced':
            self._add_game_limitation(self._current_game, 'same_game_replaced')
            self._append_event(normalized, [{
                'kind': 'continuity_note',
                'message': 'Arena supplied a replacement state for this game; changes across the replacement were not compared.',
            }], ['A replacement state was observed.'], [], ['same_game_replaced'])
            self._previous = normalized
            self._pending_break.clear()
            self._merge_game_integrity(self._current_game, normalized['integrity'])
            self._mark_game_end(self._current_game, normalized)
            return

        if self._pending_break:
            codes = sorted(self._pending_break)
            for code in codes:
                self._add_game_limitation(self._current_game, code)
            self._add_game_limitation(self._current_game, 'uncertain_continuity')
            self._append_event(normalized, [{
                'kind': 'continuity_note',
                'message': 'A log or state gap interrupted the timeline; changes across it were not compared.',
            }], ['A supported update followed an input or state diagnostic.'], [], codes + ['uncertain_continuity'])
            self._previous = normalized
            self._pending_break.clear()
            self._merge_game_integrity(self._current_game, normalized['integrity'])
            self._mark_game_end(self._current_game, normalized)
            return

        if previous is not None:
            changes, evidence, questions = self._diff(previous, normalized)
            limitations = normalized['integrity']['limitations']
            self._append_event(normalized, changes, evidence, questions, limitations)
        self._previous = normalized
        self._merge_game_integrity(self._current_game, normalized['integrity'])
        self._mark_game_end(self._current_game, normalized)

    def _normalize(self, snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict) or snapshot.get('my_seat') != self.seat:
            return {
                'game_number': None, 'turn': None, 'phase': None, 'step': None,
                'game_stage': None, 'match_status': None,
                'integrity': {'status': 'uncertain', 'limitations': ['invalid_schema']},
                'life_totals': {}, 'zones': {}, 'cards': {}, 'zone_cards': {},
            }
        integrity = _safe_integrity(snapshot.get('integrity'))
        life_raw = snapshot.get('life_totals')
        life_totals = {}
        if isinstance(life_raw, dict):
            for seat in ('1', '2'):
                value = life_raw.get(seat)
                if _valid_int(value):
                    life_totals[seat] = value
        zones: dict[tuple[Any, ...], dict[str, Any]] = {}
        zone_cards: dict[tuple[int, int], dict[str, Any]] = {}
        cards: dict[int, dict[str, Any]] = {}
        zone_raw = snapshot.get('zones')
        if isinstance(zone_raw, list):
            for zone in zone_raw:
                if not isinstance(zone, dict):
                    continue
                name, owner, zone_id = zone.get('zone'), zone.get('owner'), zone.get('zone_id')
                if (name not in ZONE_NAMES or
                        owner is not None and (type(owner) is not int or owner not in (1, 2)) or
                        not _valid_int(zone_id, minimum=1)):
                    continue
                count = zone.get('count')
                count = count if _valid_int(count, minimum=0) else None
                key = (zone_id, name, owner)
                zones[key] = {'zone': name, 'owner': owner, 'count': count}
                cards_raw = zone.get('cards')
                if not isinstance(cards_raw, list):
                    continue
                for card in cards_raw[:MAX_VISIBLE_INSTANCES]:
                    if (not isinstance(card, dict) or card.get('identity_status') != 'visible' or
                            not _valid_int(card.get('id'), minimum=1)):
                        continue
                    if name == 'Library' or (name == 'Hand' and owner != self.seat):
                        continue
                    instance_id = card['id']
                    name_value = _safe_label(card.get('name'))
                    if name_value is None:
                        continue
                    card_types = card.get('types')
                    card_types = sorted({t[:32] for t in card_types if isinstance(t, str) and t.isprintable()})[:8] \
                        if isinstance(card_types, list) else []
                    safe_card = {'name': name_value, 'types': card_types}
                    if instance_id not in cards and len(cards) < MAX_VISIBLE_INSTANCES:
                        cards[instance_id] = safe_card
                        zone_cards[(zone_id, instance_id)] = {'zone': name, 'owner': owner, **safe_card}
        turn = snapshot.get('turn_number')
        turn = turn if _valid_int(turn, minimum=0) else None
        game_number = snapshot.get('game_number')
        game_number = game_number if _valid_int(game_number, minimum=1) else None
        phase = snapshot.get('phase')
        phase = phase if isinstance(phase, str) and phase in PHASES else None
        step = snapshot.get('step')
        step = step if isinstance(step, str) and step in STEPS else None
        stage = snapshot.get('game_stage')
        stage = stage if isinstance(stage, str) and stage in GAME_STAGES else None
        match_status = snapshot.get('match_status')
        match_status = match_status if isinstance(match_status, str) and match_status in MATCH_STATES else None
        return {
            'game_number': game_number, 'turn': turn, 'phase': phase, 'step': step,
            'game_stage': stage, 'match_status': match_status, 'integrity': integrity,
            'life_totals': life_totals, 'zones': zones, 'cards': cards, 'zone_cards': zone_cards,
        }

    def _new_match(self) -> None:
        self._match_sequence += 1
        self._game_sequence = 0
        if len(self._matches) >= MAX_MATCHES_PER_LOG:
            self._current_match = None
            self._current_game = None
            self._previous = None
            return
        self._current_match = {'sequence': self._match_sequence, 'games': [], 'limitations': []}
        self._matches.append(self._current_match)
        self._current_game = None
        self._previous = None

    def _new_game(self, snapshot: dict[str, Any]) -> None:
        self._game_sequence += 1
        self._total_games += 1
        self._previous = None
        if (self._total_games > MAX_GAMES_PER_LOG or self._current_match is None or
                len(self._matches) >= MAX_MATCHES_PER_LOG and not self._current_match['games']):
            self._omitted_games += 1
            self._current_game = None
            self._summarize_omitted_snapshot(snapshot)
            return
        game = {
            'sequence': self._game_sequence,
            'game_number': snapshot['game_number'],
            'start_turn': snapshot['turn'],
            'end_turn': snapshot['turn'],
            'integrity': dict(snapshot['integrity']),
            'end_status': 'ended' if (snapshot['game_stage'] == 'GameStage_GameOver' or
                                      snapshot['match_status'] == 'MatchState_MatchComplete') else 'not_observed',
            'timeline': [],
            'event_counts': Counter({kind: 0 for kind in EVENT_KINDS}),
            'omitted_entries': 0,
            'truncated': False,
        }
        self._current_match['games'].append(game)
        self._current_game = game
        if self._pending_break:
            for code in self._pending_break:
                self._add_game_limitation(game, code)
            self._add_game_limitation(game, 'uncertain_continuity')
            self._current_match['limitations'] = sorted(set(
                self._current_match['limitations'] + ['uncertain_continuity']))
            self._pending_break.clear()

    def _add_game_limitation(self, game: dict[str, Any], code: str) -> None:
        if code not in SAFE_LIMITATIONS:
            return
        integrity = game['integrity']
        integrity['limitations'] = sorted(set(integrity['limitations'] + [code]))
        if code not in ('missing_turn', 'missing_life_totals', 'missing_zones',
                        'unknown_zone_membership', 'missing_visible_objects'):
            integrity['status'] = 'uncertain'

    def _merge_game_integrity(self, game: dict[str, Any], integrity: dict[str, Any]) -> None:
        game_integrity = game['integrity']
        game_integrity['limitations'] = sorted(set(
            game_integrity['limitations'] + integrity['limitations']))
        rank = {'waiting': 0, 'consistent': 1, 'partial': 2, 'ended': 3, 'uncertain': 4}
        if rank[integrity['status']] > rank[game_integrity['status']]:
            game_integrity['status'] = integrity['status']
        if (integrity['status'] == 'ended' or
                integrity['limitations'] and integrity['status'] == 'uncertain'):
            if integrity['status'] == 'ended':
                game['end_status'] = 'ended'

    @staticmethod
    def _mark_game_end(game: dict[str, Any] | None, snapshot: dict[str, Any]) -> None:
        if game is not None and (snapshot['game_stage'] == 'GameStage_GameOver' or
                                 snapshot['match_status'] == 'MatchState_MatchComplete'):
            game['end_status'] = 'ended'

    def _summarize_omitted_snapshot(self, snapshot: dict[str, Any]) -> None:
        turn = snapshot['turn']
        summary = self._omitted_game_summary
        if turn is not None:
            summary['turn_start'] = turn if summary['turn_start'] is None else min(summary['turn_start'], turn)
            summary['turn_end'] = turn if summary['turn_end'] is None else max(summary['turn_end'], turn)

    def _diff(self, old: dict[str, Any], new: dict[str, Any]) -> tuple[
        list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        changes: list[dict[str, Any]] = []
        evidence: list[str] = []
        questions: list[dict[str, Any]] = []
        turn_changed = (old['turn'], old['phase'], old['step']) != (new['turn'], new['phase'], new['step'])
        if turn_changed and any(value is not None for value in
                                (old['turn'], new['turn'], old['phase'], new['phase'], old['step'], new['step'])):
            changes.append({
                'kind': 'turn_progress', 'from_turn': old['turn'], 'to_turn': new['turn'],
                'from_phase': old['phase'], 'to_phase': new['phase'],
                'from_step': old['step'], 'to_step': new['step'],
            })
            evidence.append(
                f"Observed turn or phase progression to turn {new['turn'] if new['turn'] is not None else 'unknown'}.")

        for seat in ('1', '2'):
            before, after = old['life_totals'].get(seat), new['life_totals'].get(seat)
            if before is not None and after is not None and before != after:
                changes.append({'kind': 'life_total_change', 'seat': int(seat), 'from': before, 'to': after})
                detail = f'Seat {seat} life total changed from {before} to {after}.'
                evidence.append(detail)
                questions.append({
                    'kind': 'life_change',
                    'question': f"What happened around seat {seat}'s life total change on turn {new['turn'] if new['turn'] is not None else 'unknown'}?",
                    'evidence': detail,
                    'limitations': ['The log does not establish every cause, choice, or legal alternative.'],
                })

        for key in sorted(set(old['zones']) & set(new['zones']), key=lambda item: (item[1], item[2] or 0, item[0])):
            before, after = old['zones'][key]['count'], new['zones'][key]['count']
            if before is None or after is None or before == after:
                continue
            zone = new['zones'][key]
            changes.append({'kind': 'zone_count_change', 'zone': zone['zone'], 'owner': zone['owner'],
                            'from': before, 'to': after})
            evidence.append(
                f"Observed {zone['zone']} count for seat {zone['owner'] if zone['owner'] is not None else 'unknown'} changed from {before} to {after}.")

        previous_cards = old['zone_cards']
        current_cards = new['zone_cards']
        old_by_id: dict[int, dict[str, Any]] = {}
        new_by_id: dict[int, dict[str, Any]] = {}
        for (zone_id, instance_id), card in previous_cards.items():
            old_by_id.setdefault(instance_id, card)
        for (zone_id, instance_id), card in current_cards.items():
            new_by_id.setdefault(instance_id, card)
        for instance_id in sorted(set(old_by_id) & set(new_by_id)):
            before, after = old_by_id[instance_id], new_by_id[instance_id]
            if before['name'] != after['name'] or before['zone'] == after['zone']:
                continue
            changes.append({'kind': 'visible_card_moved', 'name': after['name'],
                            'from_zone': before['zone'], 'to_zone': after['zone']})
            detail = f"Observed {after['name']} move from {before['zone']} to {after['zone']} on turn {new['turn'] if new['turn'] is not None else 'unknown'}."
            evidence.append(detail)
            if after['zone'] in ('Graveyard', 'Exile'):
                questions.append({
                    'kind': 'visible_card_movement',
                    'question': f"What led to {after['name']} moving from {before['zone']} to {after['zone']}?",
                    'evidence': detail,
                    'limitations': ['The log does not establish every cause, choice, or legal alternative.'],
                })
        return changes, evidence, questions

    def _append_event(self, snapshot: dict[str, Any], changes: list[dict[str, Any]],
                      evidence: list[str], questions: list[dict[str, Any]],
                      limitations: list[str]) -> None:
        if self._current_game is None or not changes:
            return
        game = self._current_game
        counts = game['event_counts']
        for change in changes:
            kind = change.get('kind')
            if kind in EVENT_KINDS:
                counts[kind] += 1
        if game['start_turn'] is None and snapshot['turn'] is not None:
            game['start_turn'] = snapshot['turn']
        if snapshot['turn'] is not None:
            game['end_turn'] = snapshot['turn']
        entry = {
            'turn_number': snapshot['turn'], 'phase': snapshot['phase'], 'step': snapshot['step'],
            'changes': changes[:MAX_CHANGES_PER_ENTRY],
            'evidence': evidence[:MAX_CHANGES_PER_ENTRY],
            'limitations': sorted({code for code in limitations if code in SAFE_LIMITATIONS}),
            'review_questions': questions[:MAX_CHANGES_PER_ENTRY],
        }
        if len(changes) > MAX_CHANGES_PER_ENTRY:
            entry['omitted_changes'] = len(changes) - MAX_CHANGES_PER_ENTRY
        serialized_size = len(json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if len(game['timeline']) >= MAX_ENTRIES_PER_GAME or self._detail_bytes + serialized_size > MAX_DETAIL_BYTES:
            game['omitted_entries'] += 1
            game['truncated'] = True
            self._omitted_entries += 1
            self._add_game_limitation(game, 'detail_limit_reached')
            self._count_omitted_game_event_counts(changes)
            return
        self._detail_bytes += serialized_size
        game['timeline'].append(entry)

    def _count_omitted_game_event_counts(self, changes: list[dict[str, Any]]) -> None:
        summary = self._omitted_game_summary['event_counts']
        for change in changes:
            kind = change.get('kind')
            if kind in EVENT_KINDS:
                summary[kind] += 1

    def finish(self) -> dict[str, Any]:
        if self._observed_states == 0:
            limitations = ['no_supported_game_updates']
        else:
            limitations = []
        matches = []
        for match in self._matches:
            games = []
            for game in match['games']:
                games.append({
                    'sequence': game['sequence'], 'game_number': game['game_number'],
                    'start_turn': game['start_turn'], 'end_turn': game['end_turn'],
                    'integrity': game['integrity'], 'end_status': game['end_status'],
                    'timeline': game['timeline'],
                    'event_counts': _safe_counter(game['event_counts']),
                    'omitted_entries': game['omitted_entries'], 'truncated': game['truncated'],
                })
            matches.append({'sequence': match['sequence'], 'games': games,
                            'limitations': sorted(set(match['limitations']))})
        report = {
            'schema_version': 1,
            'fixture_provenance': 'user_log_unverified',
            'independent_validation': 'pending',
            'matches': matches,
            'diagnostics': dict(sorted((code, count) for code, count in self._diagnostics.items()
                                       if code in DIAGNOSTICS or code == 'source_reset' and count > 0)),
            'final_integrity': self._final_integrity,
            'limitations': limitations,
            'truncated': bool(self._omitted_games or self._omitted_entries or
                              any(game['truncated'] for match in self._matches for game in match['games'])),
            'omitted_games': self._omitted_games,
            'omitted_entries': self._omitted_entries,
            'omitted_game_summary': {
                'turn_start': self._omitted_game_summary['turn_start'],
                'turn_end': self._omitted_game_summary['turn_end'],
                'event_counts': _safe_counter(self._omitted_game_summary['event_counts']),
            },
        }
        size = len(json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if size > MAX_REPORT_BYTES:
            raise ReviewOutputError('Review output exceeds the safe size limit')
        return report
