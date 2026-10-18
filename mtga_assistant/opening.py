"""Deterministic opening-hand and mulligan review from visible current state.

This is a deck-specific *heuristic*, not a legal-action verifier or winrate
oracle. Hidden/unknown cards never receive inferred names.
"""
from __future__ import annotations
from typing import Any, Mapping

OPENING_REVIEW_LIFECYCLES = frozenset({
    'MULLIGAN', 'BOTTOM_CARDS', 'OPENING_HAND', 'PREGAME_ACTION',
})

# Functional equivalents recognized from locally resolved card definitions.
# Unknown 1-drops must not be assumed mana-producing based on mana value alone.
KNOWN_ONE_DROP_DORKS = frozenset({
    'llanowar elves', 'elvish mystic', 'fyndhorn elves', 'arbor elf',
    'birds of paradise', 'boreal druid', 'ignoble hierarch', 'noble hierarch',
    'delighted halfling'
})


def _normal(name: Any) -> str:
    return name.casefold().strip() if isinstance(name, str) else ''


def opening_review_is_supported(snapshot: Mapping[str, Any]) -> bool:
    """Allow a state-only review when the visible hand has verifiable evidence."""
    phase = snapshot.get('lifecycle')
    if not isinstance(phase, str) or phase not in OPENING_REVIEW_LIFECYCLES:
        return False
    integrity = snapshot.get('integrity')
    if not isinstance(integrity, dict) or integrity.get('status') not in ('consistent', 'partial'):
        return False
    evaluation = snapshot.get('opening_evaluation')
    if not isinstance(evaluation, dict):
        return False
    if evaluation.get('status') == 'ready':
        return True
    if evaluation.get('status') not in ('partial', 'cards_unresolved'):
        return False
    known = evaluation.get('known_cards')
    unknown = evaluation.get('unknown_cards')
    if type(known) is not int or type(unknown) is not int or known < 1 or unknown < 1:
        return False
    seat = snapshot.get('my_seat')
    zones = snapshot.get('zones')
    if type(seat) is not int or not isinstance(zones, list):
        return False
    hand = next((zone for zone in zones if isinstance(zone, dict) and
                 zone.get('zone') == 'Hand' and zone.get('owner') == seat), None)
    if not isinstance(hand, dict):
        return False
    cards = hand.get('cards')
    if (not isinstance(cards, list) or not cards or type(hand.get('count')) is not int or
            hand.get('count') != len(cards) or
            known + unknown != len(cards)):
        return False
    recognized = 0
    unresolved = 0
    for card in cards:
        if not isinstance(card, dict) or card.get('identity_status') != 'visible':
            return False
        name = _normal(card.get('name'))
        if name.startswith('card #') or name == 'unknown card':
            unresolved += 1
        elif name:
            recognized += 1
        else:
            return False
    return recognized == known and unresolved == unknown


def evaluate_opening(snapshot: Mapping[str, Any], deck_ids: Mapping[int, int],
                     resolver: Any) -> dict[str, Any] | None:
    """Evaluate only one's currently visible opening hand, when relevant."""
    phase = snapshot.get('lifecycle')
    if not isinstance(phase, str) or phase not in OPENING_REVIEW_LIFECYCLES:
        return None
    seat = snapshot.get('my_seat')
    zones = snapshot.get('zones', [])
    my_hand = next((z for z in zones if z.get('zone') == 'Hand' and z.get('owner') == seat), None)
    if not my_hand or not isinstance(my_hand.get('cards'), list):
        return {'status': 'waiting_for_hand', 'recommendation': 'WAIT', 'reasons': []}
    cards = my_hand['cards']
    # A missing definition should not erase all *known* opening-card evidence.
    # But do not recommend a definitive keep/mulligan on an incomplete hand.
    if my_hand.get('count') != len(cards) or not cards or any(
            c.get('identity_status') != 'visible' for c in cards):
        return {'status': 'waiting_for_hand', 'recommendation': 'WAIT',
                'reasons': ['Opening-hand visibility or membership is incomplete.']}
    required_bottom = snapshot.get('bottom_required') if phase == 'BOTTOM_CARDS' else None
    if type(required_bottom) is not int or not 0 <= required_bottom <= len(cards):
        required_bottom = None
    remaining_hand = len(cards) - required_bottom if required_bottom is not None else None
    bottom_reasons = []
    if phase == 'BOTTOM_CARDS':
        if required_bottom is None:
            bottom_reasons.append('The number of cards Arena requires returning is unavailable.')
        else:
            bottom_reasons.append(
                f'Arena requests returning {required_bottom} of {len(cards)} cards; '
                f'{remaining_hand} will remain in hand.'
            )
            if remaining_hand == 0:
                bottom_reasons.append('All cards in this hand must be returned; none can be kept.')
    unresolved = [c for c in cards if _normal(c.get('name')).startswith('card #')
                  or _normal(c.get('name')) == 'unknown card']
    names = [_normal(c.get('name')) for c in cards if c not in unresolved]
    deck_names = {_normal(resolver.name(gid)) for gid in deck_ids}
    dork_copies = sum(copies for gid, copies in deck_ids.items()
                      if _normal(resolver.name(gid)) in KNOWN_ONE_DROP_DORKS)
    is_blood_elves = ('leyline of abundance' in deck_names and dork_copies >= 4 and
                      bool({'elvish archdruid', 'priest of titania'} & deck_names))
    # For this user-established Blood Elves strategy, a *Forest* is specifically
    # required. Avoid treating Nykthos as an unconditional turn-one green source.
    green_source = sum(name in ('forest', 'snow-covered forest') for name in names)
    one_drops = sum(name in KNOWN_ONE_DROP_DORKS for name in names)
    second_land = sum(('forest' in name or name == 'nykthos, shrine to nyx') for name in names)
    leylines = names.count('leyline of abundance')
    nykthos = names.count('nykthos, shrine to nyx')
    minimum = green_source >= 1 and one_drops >= 1
    mulligans = snapshot.get('mulligans_taken')
    if type(mulligans) is not int or not 0 <= mulligans <= 7:
        mulligans = 0
    reasons = []
    if minimum:
        reasons.append('At least one Forest and one one-mana mana creature are present.')
    else:
        reasons.append('Missing the preferred Forest + one-mana mana creature opening.')
    if leylines:
        reasons.append('Leyline supports acceleration once the initial condition is met.'
                       if minimum else 'Leyline does not repair the missing opening requirement.')
    if nykthos:
        reasons.append('Nykthos is weak in the opening hand until devotion develops.')
    if second_land >= 2 and minimum:
        reasons.append('A second land improves follow-up consistency.')
    if unresolved:
        return {'status': 'partial', 'profile': 'blood_elves' if is_blood_elves else 'generic',
                'recommendation': 'WAIT', 'known_cards': len(names),
                'unknown_cards': len(unresolved), 'minimum_met_from_known_cards': minimum if is_blood_elves else None,
                'bottom_required': required_bottom,
                'hand_count': len(cards) if phase == 'BOTTOM_CARDS' else None,
                'remaining_count': remaining_hand,
                'reasons': bottom_reasons + ['Known cards analyzed, but one or more definitions are missing.',
                                             'No full-hand keep, mulligan, or bottom assessment is available.']}
    if phase == 'BOTTOM_CARDS' and remaining_hand == 0:
        return {'status': 'ready', 'profile': 'blood_elves' if is_blood_elves else 'generic',
                'recommendation': 'BOTTOM', 'bottom_required': required_bottom,
                'hand_count': len(cards), 'remaining_count': 0, 'reasons': bottom_reasons}
    if not is_blood_elves:
        known_types = [c.get('types') for c in cards
                       if isinstance(c.get('types'), list) and c['types']]
        lands = sum('Land' in types for types in known_types)
        nonlands = sum('Land' not in types for types in known_types)
        unknown_types = len(cards) - len(known_types)
        return {'status': 'ready', 'profile': 'generic',
                'recommendation': 'BOTTOM_REVIEW' if phase == 'BOTTOM_CARDS' else 'REVIEW',
                'bottom_required': required_bottom,
                'hand_count': len(cards) if phase == 'BOTTOM_CARDS' else None,
                'remaining_count': remaining_hand,
                'counts': {'hand_cards': len(cards), 'lands': lands, 'nonlands': nonlands,
                           'unknown_types': unknown_types},
                'reasons': (bottom_reasons + ['No deck-specific bottom ranking is configured.']
                            if phase == 'BOTTOM_CARDS' else
                            [f'{lands} recognized lands and {nonlands} recognized nonland cards in hand.',
                             'General opening review; no deck-specific keep/mulligan strategy is configured.'])}
    if phase == 'BOTTOM_CARDS':
        if required_bottom is None:
            return {'status': 'ready', 'profile': 'blood_elves',
                    'recommendation': 'BOTTOM_REVIEW', 'bottom_required': None,
                    'hand_count': len(cards), 'remaining_count': None,
                    'reasons': bottom_reasons}

        # Distinct instance IDs are essential if there are duplicates.
        def weight(card):
            n = _normal(card.get('name'))
            if n == 'nykthos, shrine to nyx': return -20
            if n in ('forest', 'snow-covered forest'): return (
                15 if green_source >= 3 else 60 if green_source == 2 else 90)
            if n in KNOWN_ONE_DROP_DORKS: return 100 if one_drops == 1 else 30
            if n == 'leyline of abundance': return 65 if minimum else 0
            if n in ('paradox engine', 'craterhoof behemoth', 'march of the world ooze'): return -6
            if n == 'allosaurus shepherd' and minimum and leylines: return -5
            if n == 'thornweald archer' and minimum and leylines: return -4
            return 10

        required = required_bottom
        ordered = sorted(cards, key=weight)
        return {
            'status': 'ready', 'profile': 'blood_elves', 'recommendation': 'BOTTOM',
            'minimum_met': minimum, 'mulligans_taken': mulligans,
            'bottom_required': required,
            'hand_count': len(cards), 'remaining_count': remaining_hand,
            'suggested_bottom': [{'instance_id': c['id'], 'name': c['name']} for c in ordered[:required]],
            'reasons': bottom_reasons + reasons + ['Verify suggested bottoms against your intended follow-up plays.'],
        }
    recommendation = 'KEEP_CANDIDATE' if minimum else 'MULLIGAN_CANDIDATE'
    if minimum and leylines and second_land >= 2:
        recommendation = 'STRONG_KEEP_CANDIDATE'
    if not minimum and mulligans >= 3:
        recommendation = 'REVIEW'  # avoid reflexively mulliganing to zero
    return {'status': 'ready', 'profile': 'blood_elves',
            'minimum_met': minimum, 'mulligans_taken': mulligans,
            'recommendation': recommendation,
            'reasons': reasons,
            'counts': {'forests': green_source, 'one_drop_mana_dorks': one_drops,
                       'lands': second_land, 'leylines': leylines, 'nykthos': nykthos}}
