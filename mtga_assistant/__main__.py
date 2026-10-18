"""Read-only CLI: python -m mtga_assistant demo|replay|watch."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import math
from http.client import HTTPException
from pathlib import Path
import re
import sys
import time
from urllib.error import URLError
from mtga_assistant.cards import (
    CatalogError, import_catalog, load_card_names, lookup_card, lookup_cards,
)
from mtga_assistant.stream import replay_records, follow_records
from mtga_assistant.arena_data import (
    ArenaCardResolver, ArenaDeckIndex, discover_mtga_database, enrich_visible_snapshot)
from mtga_assistant.opening import (
    OPENING_REVIEW_LIFECYCLES, evaluate_opening, opening_review_is_supported,
)
from mtga_assistant.interactions import visible_interaction_notes
from mtga_assistant.state import GameState, DIAGNOSTICS
from mtga_assistant.identity import ArenaSeatResolver
from mtga_assistant.paths import default_arena_log
from mtga_assistant.coach import observations
from mtga_assistant.ollama import (
    ExplanationWorker, ExplanationResult, explain_review,
    ollama_status, preload_model, unload_model, describe_ollama_error,
)
from mtga_assistant.projection import compact_live_snapshot
from mtga_assistant.rules import (
    analyze_decision, fact_source_is_valid, recommendable_choice_numbers,
)
from mtga_assistant.report import ValidationReport
from mtga_assistant.review import ReviewBuilder, ReviewOutputError, MAX_REPORT_BYTES
from mtga_assistant.decks import (
    DeckInputError, FORMATS as DECK_FORMATS, MAX_REPORT_BYTES as MAX_DECK_REPORT_BYTES,
    SUGGESTION_GOALS, MAX_SUGGESTION_LIMIT, audit_decklist, load_collection,
    load_decklist, suggest_deck_cards,
)
from mtga_assistant.draft import (
    DraftInputError, MAX_DRAFT_REPORT_BYTES, import_17lands_draft_stats,
    load_draft_state, rank_draft,
)
from mtga_assistant.opponent import (
    OpponentDataError, build_reference_data,
    estimate_opponent, load_reference_data, render_opponent_report,
    write_reference_data,
)

_DESKTOP_EVENT_PREFIX = '@@MTGA_UI_EVENT@@'

def default_path() -> Path:
    return default_arena_log()


class _ConfiguredNames:
    def __init__(self, names):
        self.names = names
    def name(self, gid):
        return self.names.get(gid)


def load_names(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    with path.open('r', encoding='utf-8-sig') as fp:
        obj = json.load(fp)
    if not isinstance(obj, dict) or any(
        not isinstance(k, str) or re.fullmatch(r'[1-9][0-9]*', k) is None or
        not isinstance(v, str) or not v.strip() for k, v in obj.items()
    ):
        raise ValueError('Invalid card mapping')
    return {int(k): v.strip() for k, v in obj.items()}


def interval_value(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError('AI interval must be a finite nonnegative number') from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError('AI interval must be a finite nonnegative number')
    return value


def relevant_signature(snapshot: dict) -> str:
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False)


def _format_mana_cost(cost: Any) -> str | None:
    if not isinstance(cost, list) or not cost:
        return None
    symbols = []
    for symbol in cost:
        if not isinstance(symbol, dict):
            return None
        colors = symbol.get('colors')
        count = symbol.get('count')
        if (not isinstance(colors, list) or not colors or
                any(not isinstance(color, str) for color in colors) or
                type(count) is not int):
            return None
        symbols.append(f"{count} {'/'.join(colors)}")
    return ' + '.join(symbols)


def _reference_label(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get('card'), str):
        return value['card']
    return None


def _without_catalog_context(snapshot: dict) -> dict:
    result = deepcopy(snapshot)

    def strip(value):
        if isinstance(value, dict):
            return {key: strip(child) for key, child in value.items() if key != 'catalog'}
        if isinstance(value, list):
            return [strip(child) for child in value]
        return value

    if isinstance(result.get('decision'), dict):
        result['decision'] = strip(result['decision'])
    return result


def _emit_decision(snapshot: dict) -> None:
    decision = snapshot.get('decision')
    if not isinstance(decision, dict):
        return
    kind = decision.get('kind')
    print('Arena-presented choices (not independently rules-verified):')
    if kind == 'actions':
        options = decision.get('options')
        if isinstance(options, list):
            for option in options[:64]:
                if not isinstance(option, dict):
                    continue
                number = option.get('number')
                action = option.get('action')
                if type(number) is not int or not isinstance(action, str):
                    continue
                detail = action
                if isinstance(option.get('card'), str):
                    detail += ' — ' + option['card']
                cost = _format_mana_cost(option.get('mana_cost'))
                if cost:
                    detail += ' (mana cost: ' + cost + ')'
                print(f'  {number}. {detail}')
    elif kind == 'attackers':
        attackers = decision.get('attackers')
        if isinstance(attackers, list):
            for attacker in attackers[:64]:
                if not isinstance(attacker, dict):
                    continue
                card = attacker.get('card')
                recipients = attacker.get('legal_recipients')
                if isinstance(card, str):
                    recipient_text = ', '.join(
                        label for x in recipients
                        if (label := _reference_label(x)) is not None
                    ) if isinstance(recipients, list) else ''
                    print('  ' + card + (f' may attack {recipient_text}' if recipient_text else ''))
        if decision.get('can_submit') is True:
            print('  Arena reports that attackers can be submitted now.')
    elif kind == 'blockers':
        blockers = decision.get('blockers')
        if isinstance(blockers, list):
            for blocker in blockers[:64]:
                if not isinstance(blocker, dict) or not isinstance(blocker.get('card'), str):
                    continue
                legal = blocker.get('legal_attackers')
                legal_text = ', '.join(
                    label for x in legal
                    if (label := _reference_label(x)) is not None
                ) if isinstance(legal, list) else ''
                print('  ' + blocker['card'] + (f' may block {legal_text}' if legal_text else ''))
    elif kind == 'payment':
        cost = _format_mana_cost(decision.get('mana_cost'))
        if cost:
            print('  Mana cost: ' + cost)
        selection = decision.get('cost_selection')
        if isinstance(selection, dict):
            candidates = selection.get('visible_candidates')
            names = ', '.join(
                label for x in candidates
                if (label := _reference_label(x)) is not None
            ) if isinstance(candidates, list) else ''
            if names:
                print('  Visible cost candidates: ' + names)
        actions = decision.get('mana_actions')
        if isinstance(actions, list):
            for action in actions[:64]:
                if isinstance(action, str):
                    print('  ' + action)
    elif kind == 'targets':
        groups = decision.get('groups')
        if isinstance(groups, list):
            for index, group in enumerate(groups[:64], start=1):
                if not isinstance(group, dict):
                    continue
                print(f"  Target group {index}: choose {group.get('minimum', '?')} to "
                      f"{group.get('maximum', '?')}; {group.get('selected', '?')} selected.")
                candidates = group.get('candidates')
                if isinstance(candidates, list):
                    for candidate in candidates[:64]:
                        if isinstance(candidate, dict) and isinstance(candidate.get('card'), str):
                            print(f"    {candidate['action']}: {candidate['card']}")
    elif kind == 'bottom_cards':
        required = decision.get('bottom_required')
        if type(required) is int:
            print(f'  Arena requires {required} bottom-card selection(s).')
        for option in decision.get('options', [])[:64]:
            if isinstance(option, dict) and isinstance(option.get('card'), str):
                print('  Eligible bottom card: ' + option['card'])
    elif kind == 'mulligan':
        mulligan_type = decision.get('mulligan_type')
        count = decision.get('mulligans_taken')
        if isinstance(mulligan_type, str):
            print('  Mulligan type: ' + mulligan_type)
        if type(count) is int:
            print(f'  Mulligans taken so far: {count}')
        options = decision.get('options')
        if isinstance(options, list):
            print('  Options: ' + ', '.join(x for x in options if isinstance(x, str)))
    else:
        print('  Arena requested a choice; details are not supported yet.')
    limitations = decision.get('limitations')
    if isinstance(limitations, list) and limitations:
        print('  Decision limitations: ' + ', '.join(x for x in limitations if isinstance(x, str)))


_DECISION_FACT_CODES = frozenset({
    'action_label', 'visible_card', 'mana_cost', 'legal_recipients',
    'can_submit_attackers', 'legal_attackers', 'selected_attackers', 'max_attackers',
    'target_action', 'selection_minimum', 'selection_maximum', 'selected_count',
    'payment_action', 'cost_candidate', 'mulligan_option', 'mulligan_type',
    'mulligans_taken', 'phase', 'step', 'turn_number', 'life_total',
    'mana_cost_supported',
    'catalog_mana_cost', 'catalog_type_line', 'catalog_colors', 'catalog_mana_value',
    'catalog_power', 'catalog_toughness', 'catalog_oracle_text', 'catalog_face_summary',
})
_DECISION_CATALOG_TEXT_LIMITS = {
    'catalog_mana_cost': 128,
    'catalog_type_line': 256,
    'catalog_power': 64,
    'catalog_toughness': 64,
    'catalog_oracle_text': 1_024,
}
_DECISION_UNKNOWN_CODES = frozenset({
    'partial_state', 'missing_phase', 'missing_step', 'missing_turn_number',
    'missing_life_total', 'missing_card_identity', 'missing_mana_cost',
    'mana_availability_unverified',
    'candidate_space_not_enumerated', 'missing_decision',
})
_DECISION_LIMITATION_CODES = frozenset({
    'choice_payload_unavailable', 'unrecognized_action_type', 'mana_cost_unavailable',
    'choice_list_truncated', 'some_choices_unavailable', 'unrecognized_choice_type',
    'choice_bounds_unavailable', 'unrecognized_choice_action', 'details_not_supported',
    'malformed_choice', 'candidate_space_not_enumerated', 'missing_decision',
    'unsupported_decision_kind', 'unknown_choice_provenance', 'unusable_state',
    'malformed_snapshot', 'catalog_unavailable', 'catalog_missing',
    'catalog_name_mismatch', 'catalog_partial',
})


def _decision_value_text(value: object, code: object = None) -> str | None:
    if isinstance(code, str) and code in _DECISION_CATALOG_TEXT_LIMITS:
        maximum = _DECISION_CATALOG_TEXT_LIMITS[code]
        if (isinstance(value, str) and value and len(value) <= maximum and
                all(ch.isprintable() for ch in value)):
            return json.dumps(value, ensure_ascii=False)
        return None
    if code == 'catalog_mana_value':
        if (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1_000):
            return str(value)
        return None
    if code == 'catalog_colors':
        if (isinstance(value, list) and len(value) <= 5 and all(
                isinstance(item, str) and item in {'White', 'Blue', 'Black', 'Red', 'Green'}
                for item in value)):
            return json.dumps(value, ensure_ascii=False)
        return None
    if code == 'catalog_face_summary':
        if isinstance(value, list) and len(value) <= 4 and all(
                isinstance(item, str) and item and len(item) <= 2_048 and
                all(ch.isprintable() for ch in item) for item in value):
            return json.dumps(value, ensure_ascii=False)
        return None
    if isinstance(value, str) and value and len(value) <= 120 and all(ch.isprintable() for ch in value):
        return json.dumps(value, ensure_ascii=False)
    if type(value) in (int, bool):
        return str(value)
    if isinstance(value, list) and len(value) <= 64 and all(
            isinstance(item, str) and item and len(item) <= 120 and
            all(ch.isprintable() for ch in item) for item in value):
        return json.dumps(value, ensure_ascii=False)
    return None


def render_decision_analysis(analysis: dict) -> str:
    """Render the fixed, deterministic evidence without adding game claims."""
    provenance = 'Arena-presented' if isinstance(analysis, dict) and analysis.get(
        'choice_provenance') == 'arena_presented' else 'provenance unknown'
    lines = [f'Decision analysis ({provenance}; independent legality unverified)']
    catalog_rendered = False
    status = analysis.get('status') if isinstance(analysis, dict) else None
    lines.append('  Status: ' + (status if isinstance(status, str) and
                                 status in {'analyzed', 'partial', 'unavailable'} else 'unavailable'))
    choices = analysis.get('choices') if isinstance(analysis, dict) else None
    if isinstance(choices, list):
        for choice in choices[:64]:
            if not isinstance(choice, dict) or type(choice.get('number')) is not int:
                continue
            label = choice.get('label')
            if not isinstance(label, str) or not label or len(label) > 120 or any(
                    not ch.isprintable() for ch in label):
                continue
            lines.append(f"  {choice['number']}. {label}")
            facts = choice.get('facts')
            if isinstance(facts, list):
                for fact in facts[:16]:
                    if not isinstance(fact, dict):
                        continue
                    code = fact.get('code')
                    source = fact.get('source')
                    rendered = _decision_value_text(fact.get('value'), code)
                    if (isinstance(code, str) and code in _DECISION_FACT_CODES and
                            fact_source_is_valid(code, source) and rendered is not None):
                        lines.append(f'    {code} [{source}]: {rendered}')
                        catalog_rendered |= code.startswith('catalog_')
    if not choices:
        lines.append('  No supported options are available for comparison.')
    if isinstance(analysis, dict):
        unknowns = analysis.get('unknowns')
        if isinstance(unknowns, list):
            safe = [code for code in unknowns[:32]
                    if isinstance(code, str) and code in _DECISION_UNKNOWN_CODES]
            if safe:
                lines.append('  Unknown: ' + ', '.join(safe))
        limitations = analysis.get('limitations')
        if isinstance(limitations, list):
            safe = [code for code in limitations[:32]
                    if isinstance(code, str) and code in _DECISION_LIMITATION_CODES]
            if safe:
                lines.append('  Limitations: ' + ', '.join(safe))
    if catalog_rendered:
        lines.insert(2, '  Local card text is reference-only and may be stale or incomplete; it does not prove legality or interactions.')
    return '\n'.join(lines) + '\n'


def render_decision_comparison(analysis: dict, comparison: dict, *, for_overlay: bool = False) -> str:
    """Resolve a validated model ordering against the deterministic evidence."""
    choices = analysis.get('choices') if isinstance(analysis, dict) else None
    by_number = {choice['number']: choice for choice in choices
                 if isinstance(choice, dict) and type(choice.get('number')) is int} \
        if isinstance(choices, list) else {}
    lines = ['Unverified strategic interpretation (Arena-presented choices; legality unverified)']
    status = analysis.get('status') if isinstance(analysis, dict) else None
    status = status if isinstance(status, str) and status in {'analyzed', 'partial'} else 'unavailable'
    lines.append('  Evidence status: ' + status)
    if for_overlay:
        lines = []
    catalog_rendered = False
    ordered = comparison.get('ordered_choices') if isinstance(comparison, dict) else None
    rendered_count = 0
    eligible = recommendable_choice_numbers(analysis) if isinstance(analysis, dict) else set()
    if isinstance(ordered, list):
        for ranked in ordered[:64]:
            if not isinstance(ranked, dict) or type(ranked.get('number')) is not int:
                continue
            choice = by_number.get(ranked['number'])
            fact_codes = ranked.get('fact_codes')
            if (choice is None or ranked['number'] not in eligible or not isinstance(fact_codes, list) or
                    any(not isinstance(code, str) or code not in _DECISION_FACT_CODES
                        for code in fact_codes)):
                continue
            label = choice.get('label')
            if not isinstance(label, str) or not label or len(label) > 120 or any(
                    not ch.isprintable() for ch in label):
                continue
            lines.append(f"  {ranked['number']}. {label}")
            facts = choice.get('facts')
            if for_overlay:
                action = next((f.get('value') for f in facts if isinstance(f, dict)
                               and f.get('code') == 'action_label'), None) if isinstance(facts, list) else None
                if action in {'Cast spell', 'Activate ability', 'Activate mana ability', 'Float mana'}:
                    costs = next((f.get('value') for f in facts if isinstance(f, dict)
                                  and f.get('code') == 'mana_cost'), None)
                    cost_text = ' + '.join(costs) if isinstance(costs, list) and costs else '0'
                    lines.append(f'  Listed mana cost: {cost_text}. Covered by observed untapped lands.')
                elif action == 'Play land':
                    lines.append('  Make the land play, then reassess the spells with the updated mana.')
                rendered_count += 1
                continue
            if isinstance(facts, list):
                for fact in facts[:16]:
                    if (not isinstance(fact, dict) or not isinstance(fact.get('code'), str) or
                            fact.get('code') not in fact_codes):
                        continue
                    rendered = _decision_value_text(fact.get('value'), fact.get('code'))
                    source = fact.get('source')
                    if (fact.get('code') in _DECISION_FACT_CODES and
                            fact_source_is_valid(fact.get('code'), source) and rendered is not None):
                        lines.append(f"    {fact['code']} [{source}]: {rendered}")
                        catalog_rendered |= fact['code'].startswith('catalog_')
            rendered_count += 1
    if not rendered_count:
        lines.append('  No supported preference for this position.')
        if isinstance(analysis, dict) and analysis.get('decision_kind') == 'actions':
            lines.append('  Check available mana before casting; listed actions may still need payment.')
    if catalog_rendered:
        lines.insert(2, '  Local card text is reference-only and may be stale or incomplete; it does not prove legality or interactions.')
    return '\n'.join(lines) + '\n'


def emit_match_summary(args: argparse.Namespace, snapshot: dict, notes: list[dict],
                       analysis: dict | None) -> None:
    """Small privacy-filtered match HUD event; never includes card IDs or hidden zones."""
    if not getattr(args, 'desktop_events', False):
        return
    seat = getattr(args, 'seat', None)
    if seat == 'auto':
        # Snapshot has already been bound to the confirmed local seat by the caller.
        seat = snapshot.get('my_seat')
    try:
        seat = int(seat)
    except (ValueError, TypeError):
        return
    if seat not in (1, 2):
        return
    lives = snapshot.get('life_totals') or {}
    def life(which):
        value = lives.get(str(which), lives.get(which)) if isinstance(lives, dict) else None
        return value if type(value) is int else None
    def count(zone_name, owner):
        zones = snapshot.get('zones', [])
        if not isinstance(zones, list):
            return None
        if zone_name == 'Battlefield':
            relevant = [z for z in zones if isinstance(z, dict) and z.get('zone') == zone_name]
            # Battlefield zones are frequently shared; use visible controller
            # rather than the zone owner (normally None).
            if (not relevant or any(type(z.get('count')) is not int or
                                    len(z.get('cards', [])) != z['count'] for z in relevant)):
                return None
            return sum(1 for z in relevant for card in z['cards']
                       if isinstance(card, dict) and card.get('controller') == owner)
        values = [z.get('count') for z in zones if isinstance(z, dict) and
                  z.get('zone') == zone_name and z.get('owner') == owner]
        return sum(values) if values and all(type(v) is int for v in values) else None
    choices = analysis.get('choices', []) if isinstance(analysis, dict) else []
    labels = [str(c.get('label', 'Unknown'))[:150]
              for c in choices[:12] if isinstance(c, dict)]
    # The identifier-free model projection deliberately redacts numeric Arena
    # IDs. The *local* HUD can still distinguish publicly visible blockers
    # even when the card catalog is missing. Never send these IDs to the model.
    decision = snapshot.get('decision')
    arena_choice_present = (
        isinstance(decision, dict) and decision.get('source') == 'arena_presented'
    )
    if (isinstance(decision, dict) and decision.get('source') == 'arena_presented' and
            decision.get('kind') == 'blockers' and isinstance(decision.get('blockers'), list)):
        for idx, blocker in enumerate(decision['blockers'][:len(labels)]):
            if (isinstance(blocker, dict) and isinstance(blocker.get('card'), str) and
                    blocker['card'].isprintable() and
                    labels[idx].startswith('Unknown visible blocker')):
                labels[idx] = blocker['card'][:150]
    emit_desktop_event(args, {
        'type': 'match_summary', 'revision': snapshot.get('revision', 0),
        'lifecycle': snapshot.get('lifecycle', 'IDLE'),
        'turn': snapshot.get('turn_number'),
        'phase': str(snapshot.get('phase') or snapshot.get('step') or '')[:90],
        'integrity': snapshot.get('integrity', {}).get('status', 'waiting'),
        'my_life': life(seat), 'opp_life': life(3 - seat),
        'hand': count('Hand', seat),
        'battlefield': count('Battlefield', seat),
        'opp_battlefield': count('Battlefield', 3 - seat),
        'arena_choice_present': arena_choice_present,
        'decision_kind': (str(analysis.get('decision_kind'))[:90] if isinstance(analysis, dict)
                          and analysis.get('choices') else None),
        'choices': labels,
        'notes': [str(n.get('message', ''))[:220] for n in notes[:6] if isinstance(n, dict)],
    })


def emit(snapshot: dict, notes: list[dict], args: argparse.Namespace,
         decision_analysis: dict | None = None) -> None:
    if decision_analysis is None:
        decision_analysis = analyze_decision(compact_live_snapshot(snapshot))
    if args.json:
        print(json.dumps({'state': _without_catalog_context(snapshot), 'coaching': notes,
                          'decision_analysis': decision_analysis}, ensure_ascii=False), flush=True)
        return
    print('\n' + '=' * 58)
    print(f"Turn {snapshot['turn_number'] if snapshot['turn_number'] is not None else '?'} | {snapshot['phase'] or '?'} / {snapshot['step'] or '?'}"
          f" | Active seat: {snapshot['active_player'] or '?'}")
    print(f"State: {snapshot['integrity']['status']} | Lifecycle: {snapshot.get('lifecycle', 'UNKNOWN')} | Life totals: {snapshot['life_totals']}")
    if snapshot['integrity']['limitations']:
        print('State limitations: ' + ', '.join(snapshot['integrity']['limitations']))
    for zone in snapshot['zones']:
        cards = ', '.join(c['name'] for c in zone['cards']) or '(identities not shown)'
        count = zone['count'] if zone['count'] is not None else 'unknown'
        print(f"  {zone['zone']} (seat {zone['owner']}, count {count}): {cards}")
    _emit_decision(snapshot)
    print(render_decision_analysis(decision_analysis), end='')
    for note in notes:
        print(f"  [{note['level']}; legal actions unverified] {note['message']}")
        print(f"        {note['evidence']}")
        print('        Unknown: ' + ', '.join(note['unknowns']))
    sys.stdout.flush()


def emit_ai(result: ExplanationResult, args: argparse.Namespace,
            decision_analysis: dict | None = None) -> None:
    if getattr(args, 'desktop_events', False) and result.display_deadline is not None:
        error_code = result.error_code
        text = ''
        response_kind = result.response_kind or (
            'comparison' if result.comparison is not None else 'state_review')
        if (error_code is None and result.completed_at is not None and
                result.inference_deadline is not None and
                result.completed_at >= result.inference_deadline):
            error_code = 'timeout'
        if error_code is None and result.comparison is not None and decision_analysis is not None:
            text = render_decision_comparison(decision_analysis, result.comparison, for_overlay=True)
        elif error_code is None and response_kind == 'state_review' and result.text:
            text = ''.join(ch for ch in result.text if ch.isprintable() or ch in '\n\t').strip()
            if not text or len(text) > 3_800:
                error_code = 'invalid_response'
                text = ''
        elif error_code is None:
            error_code = 'invalid_response'
        if error_code == 'unavailable':
            emit_desktop_event(args, {
                'type': 'model_status', 'status': 'unavailable',
            })
        if time.monotonic() >= result.display_deadline:
            error_code = 'expired'
            text = ''
        emit_desktop_event(args, {
            'type': 'decision_result', 'revision': result.revision,
            'response_kind': response_kind,
            'observed_at': result.observed_at,
            'inference_deadline': result.inference_deadline,
            'deadline_at': result.display_deadline,
            'completed_at': result.completed_at,
            'elapsed_ms': result.elapsed_ms if result.elapsed_ms is not None else 0,
            'legal_status': 'unverified', 'text': text, 'error_code': error_code,
        })
        return
    if result.error_code:
        print('Local AI unavailable: ' + result.error_code, file=sys.stderr, flush=True)
        if args.json:
            event = {'ai_status': result.error_code, 'revision': result.revision,
                     'legal_status': 'unverified'}
            if result.elapsed_ms is not None:
                event['elapsed_ms'] = result.elapsed_ms
            print(json.dumps(event), flush=True)
    elif result.comparison is not None:
        if args.json:
            event = {'decision_comparison': result.comparison,
                     'revision': result.revision,
                     'legal_status': 'unverified'}
            if result.elapsed_ms is not None:
                event['elapsed_ms'] = result.elapsed_ms
            print(json.dumps(event, ensure_ascii=False), flush=True)
        elif decision_analysis is not None:
            rendered = render_decision_comparison(decision_analysis, result.comparison)
            if result.elapsed_ms is not None:
                rendered += f"Model response time: {result.elapsed_ms} ms.\n"
            print(rendered, end='', flush=True)
    elif args.json:
        event = {'ai_explanation': result.text, 'revision': result.revision,
                 'legal_status': 'unverified'}
        if result.elapsed_ms is not None:
            event['elapsed_ms'] = result.elapsed_ms
        print(json.dumps(event, ensure_ascii=False), flush=True)
    else:
        print(f'  Local AI (unverified strategy, revision {result.revision}): {result.text}', flush=True)


def emit_desktop_event(args: argparse.Namespace, event: dict) -> None:
    if getattr(args, 'desktop_events', False):
        print(_DESKTOP_EVENT_PREFIX + json.dumps(
            event, ensure_ascii=False, separators=(',', ':')), flush=True)


def release_watch_model(worker: ExplanationWorker | None, model: str, args: argparse.Namespace) -> bool:
    """Stop decision work before asking Ollama to release its resident model."""
    def announce(status: str) -> None:
        if args.json:
            print(json.dumps({'model_status': status}), flush=True)
        else:
            emit_desktop_event(args, {'type': 'model_status', 'status': status})

    if worker is not None:
        worker.close()
        if not worker.wait(5.0):
            print('The local model is still handling a request; its release is not yet confirmed.',
                  file=sys.stderr, flush=True)
            announce('unavailable')
            return False
    try:
        unload_model(model, timeout=5.0)
    except (HTTPException, OSError, RuntimeError, URLError, ValueError, UnicodeError,
            RecursionError):
        print('Unable to confirm local model release; close Ollama before restarting Arena.',
              file=sys.stderr, flush=True)
        announce('unavailable')
        return False
    if not args.json:
        print('Local model unloaded after Watch paused or stopped.', flush=True)
    announce('unloaded')
    return True


def emit_seat_status(args: argparse.Namespace, status: str, seat: int | None = None) -> None:
    """Report automatic seat resolution without exposing account names."""
    if args.json:
        event: dict[str, object] = {'seat_status': status}
        if seat in (1, 2):
            event['seat'] = seat
        print(json.dumps(event), flush=True)
    elif status == 'waiting':
        print('Auto-detecting your seat by exact match between the signed-in Arena name and one match player. '
              'If it cannot confirm a unique match, no game state is sent to the model; stop Watch and choose a seat.',
              flush=True)
    elif status == 'confirmed' and seat in (1, 2):
        print(f'Confirmed signed-in Arena account on system seat {seat}.', flush=True)
    elif status == 'unconfirmed':
        print('Arena account-to-player identity is no longer unique; model advice is paused. '
              'Stop Watch and choose a seat manually.', flush=True)


def emit_opponent_estimate(report: dict, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps({'opponent_estimate': report}, ensure_ascii=False), flush=True)
    else:
        print(render_opponent_report(report, json_output=False), end='', flush=True)


def render_review(report: dict, json_output: bool) -> str:
    """Render only the already-sanitized review object."""
    if json_output:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    else:
        lines = [
            'MTGA post-game review',
            f"Provenance: {report['fixture_provenance']} | independent validation: {report['independent_validation']}",
        ]
        if not report['matches']:
            lines.append('No supported game-state updates were found.')
        for match in report['matches']:
            lines.append(f"Match {match['sequence']}")
            for game in match['games']:
                game_label = f"game {game['game_number']}" if game['game_number'] is not None else 'game number unknown'
                start = game['start_turn'] if game['start_turn'] is not None else '?'
                end = game['end_turn'] if game['end_turn'] is not None else '?'
                lines.append(f"  Game {game['sequence']} ({game_label}), turns {start}–{end}; "
                             f"state {game['integrity']['status']}; end {game['end_status']}")
                if game['integrity']['limitations']:
                    lines.append('    Limitations: ' + ', '.join(game['integrity']['limitations']))
                for entry in game['timeline']:
                    marker = f"turn {entry['turn_number']}" if entry['turn_number'] is not None else 'turn unknown'
                    if entry['phase']:
                        marker += f" / {entry['phase']}"
                    if entry['step']:
                        marker += f" / {entry['step']}"
                    lines.append(f'    {marker}')
                    for evidence in entry['evidence']:
                        lines.append(f'      Observed: {evidence}')
                    for question in entry['review_questions']:
                        lines.append(f"      Consider: {question['question']}")
        if report['diagnostics']:
            lines.append('Log diagnostics: ' + ', '.join(
                f'{code} ({count})' for code, count in sorted(report['diagnostics'].items())))
        if report['limitations']:
            lines.append('Review limitations: ' + ', '.join(report['limitations']))
        if report['truncated']:
            lines.append(f"Details truncated; omitted games: {report['omitted_games']}, "
                         f"omitted timeline entries: {report['omitted_entries']}.")
        if report.get('model_status'):
            lines.append('Local model status: ' + report['model_status'])
        if isinstance(report.get('model_explanation'), dict):
            lines.append('Unverified local model reflection:')
            lines.extend('  ' + line for line in report['model_explanation']['text'].splitlines())
        rendered = '\n'.join(lines) + '\n'
    if len(rendered.encode('utf-8')) > MAX_REPORT_BYTES:
        raise ReviewOutputError('Review output exceeds the safe size limit')
    return rendered


def render_deck_report(report: dict, json_output: bool) -> str:
    """Render the same catalog-based audit object as JSON or readable text."""
    if json_output:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    else:
        catalog_time = report['catalog']['imported_at_utc'] or 'unknown'
        lines = [
            'Offline deck audit',
            f"Provenance: {report['provenance']} | independent validation: {report['independent_validation']}",
            f"Format: {report['format']} | catalog-based status: {report['format_legality_status']}",
            f"Catalog import time (UTC): {catalog_time}",
            f"Main deck: {report['main_count']} cards | sideboard: {report['sideboard_count']} cards",
            'Cards:',
        ]
        for card in report['cards']:
            printing = f" ({card['set']} {card['collector_number']})" if card['set'] and card['collector_number'] else ''
            lines.append(f"  {card['main_count']} main / {card['sideboard_count']} sideboard — "
                         f"{card['name']}{printing} | {card['legality']}")
        if report['issues']:
            lines.append('Findings:')
            for issue in report['issues']:
                description = issue['kind'].replace('_', ' ')
                if issue.get('card_name'):
                    description += ': ' + issue['card_name']
                if issue.get('count') is not None:
                    description += f" ({issue['count']})"
                lines.append('  ' + description)
        else:
            lines.append('Findings: none within the supported audit checks.')
        colors = ', '.join(f'{color} {count}' for color, count in report['composition']['color_counts'].items())
        lines.append('Main-deck color counts: ' + colors)
        if report['collection'] is not None:
            collection = report['collection']
            lines.append(f"Collection shortage: {collection['missing_copies']} known copies; "
                         f"wildcard budget: {collection['wildcard_budget_status']}")
            for rarity, costs in collection['wildcards'].items():
                if costs['needed']:
                    lines.append(f"  {rarity}: need {costs['needed']}, have {costs['available']}, "
                                 f"remaining {costs['remaining']}")
        lines.append('Limitations: ' + ' '.join(report['limitations']))
        rendered = '\n'.join(lines) + '\n'
    if len(rendered.encode('utf-8')) > MAX_DECK_REPORT_BYTES:
        raise DeckInputError('Deck audit output exceeds the safe size limit')
    return rendered


def render_deck_suggestions(report: dict, json_output: bool) -> str:
    """Render bounded heuristic candidates without exposing source paths or Arena IDs."""
    if json_output:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    else:
        catalog_time = report['catalog']['imported_at_utc'] or 'unknown'
        lines = [
            'Offline deck candidate suggestions',
            f"Provenance: {report['provenance']} | independent validation: {report['independent_validation']}",
            f"Format: {report['format']} | goal: {report['goal']} | copies to add: {report['copies_to_add']}",
            f"Catalog import time (UTC): {catalog_time}",
            'Candidates:',
        ]
        if not report['suggestions']:
            lines.append('  No candidates matched the selected catalog signals and collection.')
        for item in report['suggestions']:
            printing = f" ({item['set']} {item['collector_number']})" if item['set'] and item['collector_number'] else ''
            cmc = item['cmc'] if item['cmc'] is not None else 'unknown'
            colors = ','.join(item['colors']) or 'colorless'
            budget = item['collection']
            lines.append(f"  {item['name']}{printing} | fit score {item['score']} | "
                         f"mana value {cmc} | colors {colors} | {budget['status']}")
            lines.append('    Reasons: ' + ', '.join(item['reasons']))
            if budget['wildcard_needed']:
                lines.append(f"    Wildcards: need {budget['wildcard_needed']} {budget['wildcard_rarity'] or 'unknown rarity'}, "
                             f"have {budget['wildcard_available']}")
        lines.append('Limitations: ' + ' '.join(report['limitations']))
        rendered = '\n'.join(lines) + '\n'
    if len(rendered.encode('utf-8')) > MAX_DECK_REPORT_BYTES:
        raise DeckInputError('Deck suggestion output exceeds the safe size limit')
    return rendered


def render_draft_report(report: dict, json_output: bool) -> str:
    """Render a pack-only draft comparison with explicit stats provenance."""
    if json_output:
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    else:
        stats = report['statistics']
        if stats.get('source'):
            stats_line = (f"Statistics: {stats['source']} | {stats['set_code']} {stats['event_type']} | "
                          f"imported {stats['imported_at_utc']}")
        else:
            stats_line = 'Statistics: not loaded; using pool-fit signals only'
        lines = [
            'Offline draft pick comparison',
            f"Provenance: {report['provenance']} | independent validation: {report['independent_validation']}",
            f"Set: {report['set_code']} | event: {report['event_type']} | "
            f"pack {report['pack_number'] + 1}, pick {report['pick_number'] + 1}",
            stats_line,
            'Cards in current pack:',
        ]
        if not report['candidates']:
            lines.append('  No visible pack card could be resolved in the local catalog.')
        for card in report['candidates']:
            printing = f" ({card['set']} {card['collector_number']})" if card['set'] and card['collector_number'] else ''
            metric = card['statistics']
            stat_text = (f"historical picks {metric['picked_count']}, average selected pick index "
                         f"{metric['average_selected_pick_index']}"
                         if metric['status'] in ('available', 'sparse') else metric['status'])
            lines.append(f"  {card['name']}{printing} | fit score {card['score']} | "
                         f"{stat_text} | pool fit {card['pool_fit_score']}")
            if card['pool_reasons']:
                lines.append('    Pool signals: ' + ', '.join(card['pool_reasons']))
        lines.append(f"Unresolved visible cards: pack {report['unknown_pack_cards']}, "
                     f"pool {report['unknown_pool_cards']}")
        lines.append('Limitations: ' + ' '.join(report['limitations']))
        rendered = '\n'.join(lines) + '\n'
    if len(rendered.encode('utf-8')) > MAX_DRAFT_REPORT_BYTES:
        raise DraftInputError('Draft ranking output exceeds the safe size limit')
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only MTGA observer and conditional coach.')
    subs = parser.add_subparsers(dest='action', required=True)
    subs.add_parser('overlay', aliases=['gui'],
                    help='Launch the read-only MTG Arena game overlay')
    cards_parser = subs.add_parser('cards', help='Import or inspect a local offline card catalog')
    cards_subs = cards_parser.add_subparsers(dest='cards_action', required=True)
    cards_import = cards_subs.add_parser('import', help='Build a local catalog from JSONL or gzip JSONL')
    cards_import.add_argument('--source', type=Path, required=True)
    cards_import.add_argument('--database', type=Path, required=True)
    cards_lookup = cards_subs.add_parser('lookup', help='Look up one Arena card ID in a local catalog')
    cards_lookup.add_argument('--database', type=Path, required=True)
    cards_lookup.add_argument('--arena-id', type=int, required=True)

    review_parser = subs.add_parser('review', help='Build a privacy-safe timeline from one Arena log')
    review_parser.add_argument('--log', type=Path, required=True, help='Explicit Player.log path to review')
    review_parser.add_argument('--seat', type=int, choices=(1, 2), required=True,
                               help='Your system seat; never guessed from a log')
    review_parser.add_argument('--cards-json', type=Path, help='Optional Arena grpId-to-name JSON')
    review_parser.add_argument('--cards-db', type=Path, help='Optional local SQLite Arena-ID card catalog')
    review_parser.add_argument('--model', help='Optional explicit local Ollama model selection')
    review_parser.add_argument('--json', action='store_true', help='Emit one sanitized JSON review object')

    model_parser = subs.add_parser('model', help='Check, preload or unload a local Ollama model')
    model_subs = model_parser.add_subparsers(dest='model_action', required=True)
    for model_action in ('status', 'warm', 'unload'):
        model_command = model_subs.add_parser(model_action)
        model_command.add_argument('--model', required=True, help='Exact installed local model name')
        model_command.add_argument('--json', action='store_true', help='Emit bounded JSON status')

    deck_parser = subs.add_parser('deck', help='Audit an Arena deck list against a local catalog')
    deck_subs = deck_parser.add_subparsers(dest='deck_action', required=True)
    deck_audit = deck_subs.add_parser('audit', help='Check deck structure, catalog legality and collection counts')
    deck_audit.add_argument('--list', dest='deck_list', type=Path, required=True,
                            help='Arena-exported deck list text file')
    deck_audit.add_argument('--format', choices=sorted(DECK_FORMATS), required=True)
    deck_audit.add_argument('--cards-db', type=Path, required=True,
                            help='Local offline Arena-ID card catalog')
    deck_audit.add_argument('--collection-json', type=Path,
                            help='Optional local Arena-ID owned-copy and wildcard JSON')
    deck_audit.add_argument('--json', action='store_true', help='Emit one sanitized JSON audit object')
    deck_suggest = deck_subs.add_parser('suggest', help='Find heuristic card candidates from a local catalog')
    deck_suggest.add_argument('--list', dest='deck_list', type=Path, required=True,
                              help='Arena-exported deck list text file')
    deck_suggest.add_argument('--format', choices=sorted(DECK_FORMATS), required=True)
    deck_suggest.add_argument('--cards-db', type=Path, required=True,
                              help='Local offline schema-v2 Arena-ID card catalog')
    deck_suggest.add_argument('--collection-json', type=Path, required=True,
                              help='Local Arena-ID owned-copy and wildcard JSON')
    deck_suggest.add_argument('--goal', choices=sorted(SUGGESTION_GOALS), required=True)
    deck_suggest.add_argument('--copies', type=int, choices=range(1, 5), default=1,
                              help='Number of copies to consider adding (default: 1)')
    deck_suggest.add_argument('--limit', type=int, choices=range(1, MAX_SUGGESTION_LIMIT + 1), default=10,
                              help='Maximum candidates to show (default: 10)')
    deck_suggest.add_argument('--json', action='store_true', help='Emit one sanitized JSON suggestion object')

    draft_parser = subs.add_parser('draft', help='Import local draft statistics or compare a visible pack')
    draft_subs = draft_parser.add_subparsers(dest='draft_action', required=True)
    draft_stats = draft_subs.add_parser('stats', help='Manage local draft statistics')
    draft_stats_subs = draft_stats.add_subparsers(dest='draft_stats_action', required=True)
    stats_import = draft_stats_subs.add_parser('import', help='Import a user-obtained 17Lands public draft CSV')
    stats_import.add_argument('--source', type=Path, required=True)
    stats_import.add_argument('--database', type=Path, required=True)
    stats_import.add_argument('--set', dest='set_code', required=True)
    stats_import.add_argument('--event-type', required=True)
    draft_rank = draft_subs.add_parser('rank', help='Compare the current pack with your drafted pool')
    draft_rank.add_argument('--state', type=Path, required=True,
                            help='Local JSON with set/event/position, pack IDs and pool IDs')
    draft_rank.add_argument('--cards-db', type=Path, required=True,
                            help='Local offline Arena-ID card catalog')
    draft_rank.add_argument('--stats-db', type=Path,
                            help='Optional local database imported from 17Lands public draft data')
    draft_rank.add_argument('--limit', type=int, choices=range(1, 65), default=64,
                            help='Maximum current-pack candidates to show (default: 64)')
    draft_rank.add_argument('--json', action='store_true', help='Emit one sanitized JSON ranking object')

    opponent_parser = subs.add_parser('opponent', help='Prepare local public deck references')
    opponent_subs = opponent_parser.add_subparsers(dest='opponent_action', required=True)
    opponent_import = opponent_subs.add_parser('import', help='Create a reference set from local Arena deck exports')
    opponent_import.add_argument('--manifest', type=Path, required=True,
                                 help='Local JSON manifest of labeled deck export paths')
    opponent_import.add_argument('--cards-db', type=Path, required=True,
                                 help='Local Arena-ID card catalog')
    opponent_import.add_argument('--output', type=Path, required=True,
                                 help='New JSON path for the sanitized reference set')

    for action in ('demo', 'replay', 'watch'):
        p = subs.add_parser(action)
        if action == 'watch':
            p.add_argument('--seat', choices=('auto', '1', '2'), default='auto',
                           help='Auto-match your signed-in Arena name to its exact match-room seat, or choose 1 or 2')
        else:
            p.add_argument('--seat', type=int, choices=(1, 2), required=action != 'demo',
                           help='Your system seat; required for real-log replay')
        p.add_argument('--log', type=Path,
                       help='Arena Player.log path; detected automatically when omitted if unambiguous')
        p.add_argument('--cards-json', type=Path, help='Optional Arena grpId-to-name JSON')
        p.add_argument('--cards-db', type=Path, help='Optional local SQLite Arena-ID card catalog')
        p.add_argument('--model', help='Explicit local Ollama model selection')
        p.add_argument('--ai-interval', type=interval_value, default=30.0)
        p.add_argument('--json', action='store_true', help='Sanitized JSON-lines output')
        if action == 'watch':
            p.add_argument('--desktop-events', action='store_true', help=argparse.SUPPRESS)
        if action == 'replay':
            p.add_argument('--report-json', type=Path, help='Write category-only report to a NEW file')
        if action in ('replay', 'watch'):
            p.add_argument('--opponent-data', type=Path,
                           help='Optional local reference JSON for public-only opponent estimates')
    args = parser.parse_args(argv)
    if args.action in ('overlay', 'gui'):
        from mtga_assistant.overlay import launch_overlay
        return launch_overlay()
    if args.action == 'model':
        try:
            model = args.model.strip()
            if not model or len(model) > 128 or any(not char.isprintable() for char in model):
                raise ValueError('Invalid model selection')
            if args.model_action == 'status':
                result = ollama_status(model)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False))
                else:
                    state = 'Loaded; text initialization not checked' if result['loaded'] else (
                        'Installed but not loaded' if result['installed'] else 'Not installed'
                    )
                    print(f"Local model {model}: {state} (Ollama {result['service_version']}).")
            elif args.model_action == 'warm':
                if args.json:
                    print(json.dumps({'model': model, 'status': 'warming'}), flush=True)
                else:
                    print(f'Warming {model}…', flush=True)
                result = preload_model(model)
                if args.json:
                    print(json.dumps({**result, 'status': 'ready'}, ensure_ascii=False), flush=True)
                else:
                    print(f"Ready: {model} ({result['warm_seconds']:.2f}s).", flush=True)
            else:
                unload_model(model)
                if args.json:
                    print(json.dumps({'model': model, 'status': 'unloaded'}))
                else:
                    print(f'Unloaded {model}.')
            return 0
        except (HTTPException, OSError, RuntimeError, URLError, ValueError, UnicodeError,
                RecursionError) as exc:
            print('Ollama error: ' + describe_ollama_error(exc), file=sys.stderr, flush=True)
            return 2
    if args.action == 'opponent':
        try:
            reference_data = build_reference_data(args.manifest, args.cards_db)
            write_reference_data(args.output, reference_data)
            print(json.dumps({
                'imported_decks': len(reference_data['decks']),
                'format': reference_data['format'],
                'reference_date': reference_data['reference_date'],
            }, ensure_ascii=False))
            return 0
        except (OpponentDataError, CatalogError, OSError, ValueError, UnicodeError, RecursionError):
            print('Unable to create a new local opponent reference file.', file=sys.stderr)
            return 2
    if args.action == 'cards':
        try:
            if args.cards_action == 'import':
                summary = import_catalog(args.source, args.database)
                print(json.dumps({
                    'imported': summary.imported,
                    'missing_arena_id': summary.missing_arena_id,
                    'duplicate_records': summary.duplicate_records,
                    'source_bytes': summary.source_bytes,
                    'imported_at_utc': summary.imported_at_utc,
                }, ensure_ascii=False))
            else:
                card = lookup_card(args.database, args.arena_id)
                print(json.dumps({'card': card}, ensure_ascii=False))
            return 0
        except (CatalogError, OSError, ValueError, UnicodeError, RecursionError):
            print('Unable to read or import the local card catalog.', file=sys.stderr)
            return 2

    if args.action == 'review':
        try:
            if args.cards_db is not None and args.cards_json is not None:
                print('Choose either --cards-db or --cards-json.', file=sys.stderr)
                return 2
            if not args.log.is_file():
                print('Log not found. Enable Detailed Logs in Arena and provide --log.', file=sys.stderr)
                return 2
            names = load_card_names(args.cards_db) if args.cards_db is not None else load_names(args.cards_json)
            if args.model is not None and not args.model.strip():
                raise ValueError('Invalid model')
        except (CatalogError, OSError, ValueError, UnicodeError, RecursionError):
            print('Cannot read the review log or card mapping.', file=sys.stderr)
            return 2
        state = GameState(my_seat=args.seat)
        builder = ReviewBuilder(args.seat)
        iterator = replay_records(args.log)
        try:
            for record in iterator:
                applied = state.consume_record(
                    record,
                    on_applied=lambda snapshot, boundary: builder.observe(snapshot, boundary),
                    card_names=names,
                )
                builder.record_stream(record, state, applied)
            report = builder.finish()
            if args.model is not None:
                try:
                    explanation = explain_review(report, args.model, timeout=25.0)
                    report['model_status'] = 'available_unverified'
                    report['model_explanation'] = {
                        'verification': 'unverified', 'text': explanation,
                    }
                except TimeoutError:
                    report['model_status'] = 'timeout'
                except URLError as exc:
                    report['model_status'] = 'timeout' if isinstance(exc.reason, TimeoutError) else 'unavailable'
                except (ValueError, TypeError, HTTPException):
                    report['model_status'] = 'invalid_response'
                except OSError:
                    report['model_status'] = 'unavailable'
            print(render_review(report, args.json), end='')
            return 0
        except (OSError, ValueError, UnicodeError, RecursionError):
            print('Unable to build a bounded review from this log.', file=sys.stderr)
            return 2
        finally:
            iterator.close()

    if args.action == 'deck':
        try:
            deck = load_decklist(args.deck_list)
            if args.deck_action == 'audit':
                collection = load_collection(args.collection_json) if args.collection_json else None
                report = audit_decklist(deck, args.format, args.cards_db, collection)
                rendered = render_deck_report(report, args.json)
            else:
                collection = load_collection(args.collection_json)
                report = suggest_deck_cards(
                    deck, args.format, args.cards_db, collection, args.goal,
                    copies=args.copies, limit=args.limit)
                rendered = render_deck_suggestions(report, args.json)
            print(rendered, end='')
            return 0
        except (CatalogError, DeckInputError, OSError, ValueError, UnicodeError, RecursionError):
            print('Unable to audit the deck with this local catalog or input.', file=sys.stderr)
            return 2

    if args.action == 'draft':
        try:
            if args.draft_action == 'stats':
                summary = import_17lands_draft_stats(
                    args.source, args.database, args.set_code, args.event_type)
                print(json.dumps({
                    'imported_rows': summary.imported_rows,
                    'filtered_rows': summary.filtered_rows,
                    'skipped_second_pick_rows': summary.skipped_second_pick_rows,
                    'skipped_invalid_rows': summary.skipped_invalid_rows,
                    'card_count': summary.card_count,
                    'source_bytes': summary.source_bytes,
                    'imported_at_utc': summary.imported_at_utc,
                }, ensure_ascii=False))
                return 0
            state = load_draft_state(args.state)
            report = rank_draft(state, args.cards_db, args.stats_db, limit=args.limit)
            print(render_draft_report(report, args.json), end='')
            return 0
        except (CatalogError, DraftInputError, OSError, ValueError, UnicodeError, RecursionError):
            print('Unable to process the local draft state, statistics or card catalog.', file=sys.stderr)
            return 2

    if args.cards_db is not None and args.cards_json is not None:
        print('Choose either --cards-db or --cards-json.', file=sys.stderr)
        return 2
    if args.action == 'demo':
        path = Path(__file__).resolve().parent.parent / 'examples' / 'demo.log'
        if args.cards_db is None and args.cards_json is None:
            args.cards_json = path.parent / 'cards.json'
    else:
        path = args.log or default_path()
    destination = getattr(args, 'report_json', None)
    try:
        if not path.is_file():
            print('Log not found. Enable Detailed Logs in Arena and provide --log.', file=sys.stderr)
            return 2
        if destination is not None and (destination.exists() or destination.resolve() == path.resolve()):
            print('Report destination must be a new file distinct from the log.', file=sys.stderr)
            return 2
        if getattr(args, 'opponent_data', None) is not None and args.cards_db is None:
            print('Opponent estimates require a local card catalog.', file=sys.stderr)
            return 2
        arena_db, arena_localization = discover_mtga_database(path)
        auto_catalog = (ArenaCardResolver(arena_db, arena_localization)
                        if args.cards_db is None and args.cards_json is None else None)
        names = (load_card_names(args.cards_db) if args.cards_db is not None
                 else load_names(args.cards_json) if args.cards_json is not None
                 else auto_catalog.names)
        card_lookup = ((lambda arena_ids: lookup_cards(args.cards_db, arena_ids))
                       if args.cards_db is not None else
                       auto_catalog.lookup_cards if auto_catalog is not None else None)
        opponent_data = (load_reference_data(args.opponent_data)
                         if getattr(args, 'opponent_data', None) is not None else None)
        if args.model is not None and not args.model.strip():
            raise ValueError('Invalid model')
        if args.action == 'watch' and args.model:
            readiness = ollama_status(args.model)
            if not readiness['installed'] or not readiness['loaded']:
                print('Selected local model is not ready; preload the installed model before watching.',
                      file=sys.stderr)
                return 2
    except (OpponentDataError, OSError, ValueError, UnicodeError, RecursionError):
        print('Cannot read input or card mapping. Map positive integer IDs to nonempty string names.', file=sys.stderr)
        return 2
    auto_seat = args.action == 'watch' and args.seat == 'auto'
    selected_seat = None if auto_seat else (int(args.seat) if args.seat is not None else 1)
    state = GameState(my_seat=selected_seat or 1)
    deck_index = ArenaDeckIndex(Path.home() / '.mtga_assistant' / 'arena_index.sqlite3')
    if auto_catalog is not None:
        print('Card discovery: ' + (str(auto_catalog.path) if auto_catalog.connection
              else 'Native database not found; using bundled historical name fallback'
                   if auto_catalog.fallback_connection else 'No usable card database')
              + '; mode=' + auto_catalog.mode, file=sys.stderr)
        if auto_catalog.discovery_error:
            print('Card discovery detail: ' + auto_catalog.discovery_error, file=sys.stderr)
    seat_resolver = ArenaSeatResolver() if auto_seat else None
    active_seat = selected_seat
    report = ValidationReport('synthetic' if args.action == 'demo' else 'user_log_unverified')
    worker = ExplanationWorker(args.model, args.ai_interval) if args.model else None
    iterator = follow_records(path) if args.action == 'watch' else replay_records(path)
    last_signature = ''
    last_decision_analysis = None
    last_analysis_revision = None
    ai_waiting = False
    model_released = False
    # A live watcher must reconstruct the current game from the existing file
    # without replaying old choices to the GUI or model as if they were live.
    bootstrapping = args.action == 'watch'
    if args.action == 'watch' and getattr(args, 'desktop_events', False):
        emit_desktop_event(args, {
            'type': 'model_status', 'status': 'ready' if args.model else 'unavailable',
        })
    print(f"Reading log read-only | your seat: {'auto-detect' if auto_seat else state.my_seat}", file=sys.stderr)
    if args.action == 'watch':
        print('Following live file; press Ctrl+C to exit.', file=sys.stderr)
        if auto_seat:
            emit_seat_status(args, 'waiting')
    try:
        for record in iterator:
            if record.kind == 'reset' and args.action == 'watch':
                bootstrapping = True
                last_signature = ''
            if record.kind == 'event':
                changed = deck_index.observe(record.payload or {})
                if deck_index.dirty:
                    deck_index.persist()
                if changed and auto_catalog is not None:
                    auto_catalog.prefetch(deck_index.selected)
            if seat_resolver is not None:
                previous_seat = active_seat
                if record.kind == 'reset':
                    seat_resolver.reset()
                    detected_seat = None
                elif record.kind == 'event':
                    detected_seat = seat_resolver.observe(record.payload or {})
                else:
                    detected_seat = seat_resolver.seat
                if detected_seat != active_seat:
                    if detected_seat is None:
                        state.reset_game()
                    else:
                        state.my_seat = detected_seat
                        state.reset_game()
                        emit_seat_status(args, 'confirmed', detected_seat)
                    if detected_seat is None and previous_seat is not None:
                        emit_seat_status(args, 'unconfirmed')
                        if worker is not None and args.model:
                            model_released = release_watch_model(worker, args.model, args)
                    active_seat = detected_seat
                    last_signature = ''
                    last_decision_analysis = None
                    last_analysis_revision = None
                    ai_waiting = False
                if active_seat is None:
                    applied = state.consume_record(record) if record.kind in ('reset', 'diagnostic') else 0
                    report.record(record, state, applied)
                    if worker:
                        worker.poll(state.revision)
                    continue
            applied = state.consume_record(record, card_names=names, card_lookup=card_lookup)
            report.record(record, state, applied)
            if record.kind == 'diagnostic':
                code = record.code if record.code in DIAGNOSTICS else 'invalid_schema'
                print('Log diagnostic: ' + code, file=sys.stderr)
            elif record.kind == 'reset':
                print('Log source reset; cleared prior game state.', file=sys.stderr)
            snapshot = state.public_snapshot(names)
            if auto_catalog is not None:
                enrich_visible_snapshot(snapshot, state._visible_card_context(names), auto_catalog)
            snapshot['selected_deck_size'] = sum(deck_index.selected.values()) or None
            snapshot['saved_deck_count'] = len(deck_index.saved)
            snapshot['collection_count'] = len(deck_index.collection) or None
            if auto_catalog is not None:
                snapshot['card_source'] = auto_catalog.mode
            opening_evaluation = evaluate_opening(snapshot, deck_index.selected,
                                                  auto_catalog if auto_catalog is not None
                                                  else _ConfiguredNames(names))
            if opening_evaluation is not None:
                snapshot['opening_evaluation'] = opening_evaluation
            # Poll even on idle/reset so obsolete results cannot survive a reset.
            if worker:
                result = worker.poll(state.revision)
                if result:
                    analysis = (last_decision_analysis
                               if result.revision == last_analysis_revision else None)
                    emit_ai(result, args, analysis)
                    ai_waiting = False
            just_caught_up = False
            if bootstrapping:
                if record.kind != 'idle':
                    continue
                bootstrapping = False
                just_caught_up = True
            if record.kind == 'idle' and not just_caught_up:
                continue
            signature = relevant_signature(snapshot)
            if signature == last_signature:
                continue
            if snapshot['integrity']['status'] == 'waiting' and not last_signature and record.kind != 'reset':
                continue
            last_signature = signature
            emit_desktop_event(args, {'type': 'state', 'revision': snapshot['revision']})
            decision_observed_at = (
                time.monotonic()
                if isinstance(snapshot.get('decision'), dict) and
                snapshot['decision'].get('source') == 'arena_presented'
                else None
            )
            notes = observations(snapshot)
            notes[:0] = visible_interaction_notes(snapshot)
            opening = snapshot.get('opening_evaluation')
            if isinstance(opening, dict) and opening.get('status') == 'ready':
                label = opening.get('recommendation', 'REVIEW')
                reasons = '; '.join(opening.get('reasons', [])[:2])
                heading = ('BOTTOM-CARD REVIEW' if snapshot.get('lifecycle') == 'BOTTOM_CARDS'
                           else 'OPENING ' + str(label).replace('_', ' '))
                notes.insert(0, {'level': 'info', 'message': f'{heading}: {reasons}'[:220],
                                 'evidence': 'Current visible opening hand and supplied opening assessment',
                                 'unknowns': ['Opponent hidden cards', 'Future draws']})
            decision_analysis = analyze_decision(compact_live_snapshot(snapshot))
            last_decision_analysis = decision_analysis
            last_analysis_revision = snapshot['revision']
            emit(snapshot, notes, args, decision_analysis)
            emit_match_summary(args, snapshot, notes, decision_analysis)
            if opponent_data is not None and args.action == 'watch':
                opponent_report = estimate_opponent(
                    state.public_opponent_card_counts(), state.integrity(),
                    my_seat=state.my_seat, reference_data=opponent_data, card_names=names,
                )
                emit_opponent_estimate(opponent_report, args)
            ai_waiting = False
            opening_review_available = opening_review_is_supported(snapshot)
            is_opening_lifecycle = snapshot.get('lifecycle') in OPENING_REVIEW_LIFECYCLES
            opening_missing = (is_opening_lifecycle
                               and not opening_review_available)
            if (worker and not opening_missing and snapshot['integrity']['status'] in ('partial', 'consistent') and
                    (snapshot['turn_number'] is not None or
                     is_opening_lifecycle)):
                has_current_arena_decision = (
                    isinstance(snapshot.get('decision'), dict) and
                    snapshot['decision'].get('source') == 'arena_presented'
                )
                analysis_choices = (decision_analysis.get('choices')
                                    if isinstance(decision_analysis, dict) else None)
                has_supported_comparison = (
                    has_current_arena_decision and
                    isinstance(decision_analysis, dict) and
                    decision_analysis.get('status') in {'analyzed', 'partial'} and
                    decision_analysis.get('choice_provenance') == 'arena_presented' and
                    decision_analysis.get('decision_kind') in {'actions', 'mulligan', 'bottom_cards'} and
                    decision_analysis.get('comparison_mode') == 'flat_options' and
                    isinstance(analysis_choices, list) and bool(analysis_choices)
                )
                if args.action != 'watch':
                    ai_waiting = worker.submit(
                        snapshot, notes, decision_analysis,
                        observed_at=decision_observed_at,
                    )
                elif has_supported_comparison:
                    ai_waiting = worker.submit(
                        snapshot, notes, decision_analysis,
                        observed_at=decision_observed_at,
                    )
                    emit_desktop_event(args, {
                        'type': 'decision_progress', 'revision': snapshot['revision'],
                        'status': 'pending' if ai_waiting else 'skipped',
                    })
                elif opening_review_available:
                    review_observed_at = (decision_observed_at if decision_observed_at is not None
                                          else time.monotonic())
                    ai_waiting = worker.submit(
                        snapshot, notes, observed_at=review_observed_at,
                        state_review=True,
                    )
                    if ai_waiting:
                        emit_desktop_event(args, {
                            'type': 'decision_progress', 'revision': snapshot['revision'],
                            'status': 'review_pending',
                        })
                elif has_current_arena_decision:
                    ai_waiting = worker.submit(
                        snapshot, notes, decision_analysis,
                        observed_at=decision_observed_at,
                    )
                    emit_desktop_event(args, {
                        'type': 'decision_progress', 'revision': snapshot['revision'],
                        'status': 'pending' if ai_waiting else 'skipped',
                    })
            elif worker and opening_missing and args.action == 'watch':
                emit_desktop_event(args, {
                    'type': 'decision_progress', 'revision': snapshot['revision'],
                    'status': 'review_skipped',
                })
        # Replay is finite; allow a bounded wait for its final position only.
        if worker and ai_waiting:
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                result = worker.poll(state.revision)
                if result:
                    analysis = (last_decision_analysis
                               if result.revision == last_analysis_revision else None)
                    emit_ai(result, args, analysis)
                    ai_waiting = False
                    break
                time.sleep(0.01)
            if ai_waiting:
                print('Local AI pending at replay end; no verified advice available.', file=sys.stderr)
                if args.json:
                    print(json.dumps({'ai_status': 'pending', 'revision': state.revision,
                                      'legal_status': 'unverified'}), flush=True)
        if opponent_data is not None and args.action == 'replay':
            opponent_report = estimate_opponent(
                state.public_opponent_card_counts(), state.integrity(),
                my_seat=state.my_seat, reference_data=opponent_data, card_names=names,
            )
            emit_opponent_estimate(opponent_report, args)
        if destination is not None:
            # Exclusive creation protects existing files, including a path raced
            # into existence after the initial destination check.
            with destination.open('x', encoding='utf-8') as fp:
                json.dump(report.as_dict(), fp, indent=2)
                fp.write('\n')
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
    except (OSError, ValueError, UnicodeError, RecursionError):
        print('Unable to read log or write a new replay report.', file=sys.stderr)
        return 2
    finally:
        if auto_catalog is not None:
            auto_catalog.close()
        iterator.close()
        if args.action == 'watch' and args.model and not model_released:
            model_released = release_watch_model(worker, args.model, args)
        elif worker:
            worker.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
