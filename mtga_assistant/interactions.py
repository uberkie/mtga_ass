"""Evidence-based, public-card interaction warnings.

These are prompts to review the board, not a substitute for rules-engine
verification of ongoing effects, suppression, or trigger replacement.
"""
from typing import Any, Mapping


def visible_interaction_notes(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    if snapshot.get('lifecycle') not in ('IN_GAME', 'PREGAME_ACTION'):
        return []
    seat = snapshot.get('my_seat')
    if seat not in (1, 2):
        return []
    opponent = 3 - seat
    observed_sheoldred = False
    for zone in snapshot.get('zones', []):
        if not isinstance(zone, dict) or zone.get('zone') != 'Battlefield':
            continue
        for card in zone.get('cards', []):
            if not isinstance(card, dict):
                continue
            if (card.get('identity_status') == 'visible' and
                card.get('controller') == opponent and
                card.get('name', '').casefold() == 'sheoldred, the apocalypse'):
                observed_sheoldred = True
                break
    if not observed_sheoldred:
        return []
    return [{
        'level': 'high',
        'message': ('Opponent controls Sheoldred, the Apocalypse: drawing cards '
                    'normally costs you 2 life per draw. Seek is not a draw. '
                    'Check for effects that modify or disable the ability.'),
        'evidence': 'Verified visible opponent-controlled battlefield object',
        'unknowns': ['Replacement effects', 'Disabled abilities', 'Opponent responses'],
    }]
