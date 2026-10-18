"""Conservative heuristic coaching. Does not assert legal moves or probabilities."""
from __future__ import annotations
from typing import Any


def zone_cards(snapshot: dict[str, Any], name: str, seat: int) -> list[dict[str, Any]]:
    cards = []
    for z in snapshot.get('zones', []):
        if z.get('zone') != name:
            continue
        # Battlefield is typically shared; controller matters, not zone owner.
        if name == 'Battlefield':
            cards.extend(c for c in z['cards'] if c.get('controller') == seat)
        elif z.get('owner') == seat:
            cards.extend(z['cards'])
    return cards


def observations(s: dict[str, Any]) -> list[dict[str, Any]]:
    mine = s['my_seat']
    other = 2 if mine == 1 else 1
    mine_life = s.get('life_totals', {}).get(str(mine))
    their_life = s.get('life_totals', {}).get(str(other))
    my_board = zone_cards(s, 'Battlefield', mine)
    their_board = zone_cards(s, 'Battlefield', other)
    my_hand = zone_cards(s, 'Hand', mine)
    advice: list[dict[str, Any]] = []

    def add(code: str, level: str, message: str, why: str, unknowns: list[str] | None = None) -> None:
        advice.append({'code': code, 'level': level, 'message': message, 'evidence': why,
                       'unknowns': unknowns or ['rules_and_legal_actions'], 'legal_status': 'unverified'})

    integrity = s.get('integrity', {})
    if integrity.get('status') == 'uncertain':
        add('uncertain_state', 'info', 'State continuity is uncertain; waiting for an authoritative update.',
            'A supported update could not be applied reliably.', integrity.get('limitations') or ['state_continuity'])
        return advice
    if s.get('game_stage') == 'GameStage_GameOver' or integrity.get('status') == 'ended':
        add('game_ended', 'info', 'Game ended. Live coaching stopped.', 'Arena reported the game-over stage.')
        return advice
    # Arena asks for mulligans before assigning a turn number. Pregame
    # decisions are fully legitimate snapshots and must not hit the turn gate.
    if s.get('lifecycle') in ('MULLIGAN', 'BOTTOM_CARDS', 'OPENING_HAND', 'PREGAME_ACTION'):
        evaluation = s.get('opening_evaluation')
        if isinstance(evaluation, dict) and evaluation.get('status') in ('cards_unresolved', 'partial'):
            known = evaluation.get('known_cards')
            unknown = evaluation.get('unknown_cards')
            status_text = (f'{known} recognized, {unknown} unresolved.' if
                           type(known) is int and type(unknown) is int else
                           'Some card identities unresolved.')
            add('opening_unresolved', 'info',
                'Partial opening-hand assessment: ' + status_text,
                'Recognized cards can be reviewed, but the unresolved card prevents a full-hand assessment.',
                ['card_identity'])
        elif not isinstance(evaluation, dict) or evaluation.get('status') == 'waiting_for_hand':
            add('opening_waiting_hand', 'info',
                'Mulligan prompt detected; waiting for the complete opening hand.',
                'Arena sends the card objects separately from the mulligan request.',
                ['opening_hand'])
        elif evaluation.get('status') == 'ready':
            if s.get('lifecycle') == 'BOTTOM_CARDS':
                required = evaluation.get('bottom_required')
                remaining = evaluation.get('remaining_count')
                hand_count = evaluation.get('hand_count')
                counts_known = (type(required) is int and type(remaining) is int and
                                type(hand_count) is int and 0 <= required <= hand_count and
                                required + remaining == hand_count)
                if counts_known:
                    add('opening_bottom', 'info',
                        f'Arena requests returning {required} of {hand_count} cards; '
                        f'{remaining} will remain in hand.',
                        'Uses Arena\'s reported return count and current hand membership.')
                if counts_known and remaining == 0:
                    add('opening_bottom', 'info', 'All cards in this hand must be returned; none can be kept.',
                        'The reported return count equals the current hand size.')
                elif evaluation.get('suggested_bottom'):
                    selected = evaluation['suggested_bottom']
                    labels = ', '.join(c.get('name', 'Unknown') for c in selected[:7]
                                       if isinstance(c, dict))
                    add('opening_bottom', 'info', 'Heuristic bottom suggestion: ' + labels[:130],
                        'Uses visible cards and Arena\'s reported return count; not an independently verified optimal line.')
                elif required is None:
                    add('opening_bottom', 'info', 'Waiting for Arena\'s required return count.',
                        'The mulligan count alone does not establish how many cards to return.',
                        ['bottom_required'])
                elif required != 0:
                    add('opening_bottom', 'info',
                        'Bottom-card selection detected; no deck-specific ranking is available.',
                        'Review the current hand and which card is needed least.')
            else:
                if evaluation.get('profile') == 'generic':
                    add('opening_ready', 'info',
                        'General opening-hand review; no deck-specific strategy is configured.',
                        'Visible hand counts are available; deck-specific keep/mulligan criteria are not.')
                else:
                    add('opening_ready', 'info', 'Opening hand analyzed using the selected deck.',
                        'The keep/mulligan suggestion is a deck-specific heuristic, not a verified win rate.')
        my_opening_hand = zone_cards(s, 'Hand', mine)
        if my_opening_hand:
            from collections import Counter
            visible = [c['name'] for c in my_opening_hand
                       if c.get('identity_status') == 'visible' and
                       isinstance(c.get('name'), str)]
            if visible:
                groups = Counter(visible)
                details = ', '.join(f'{count}× {name}' if count > 1 else name
                                    for name, count in groups.items())
                add('opening_cards', 'info', 'Opening hand: ' + details[:205],
                    f'{len(visible)} visible cards in the current hand; '
                    'no opponent hidden identities are exposed.')
        return advice
    if not s.get('turn_number') or integrity.get('status') == 'waiting':
        add('partial_state', 'info', 'Waiting for a supported turn state.',
            'No supported turn state has been received yet.', ['turn_state'])
        return advice

    if isinstance(mine_life, int) and mine_life <= 5:
        add('low_life', 'high', 'Prioritize survival; review available blockers and interaction.',
            f'Your life total is {mine_life}. Combat tricks and instant-speed effects are not fully modeled.')
    decision = s.get('decision')
    blocking = (isinstance(decision, dict) and decision.get('source') == 'arena_presented'
                and decision.get('kind') == 'blockers')
    if blocking:
        add('combat_review', 'medium', 'Review blocks against the current attackers before confirming.',
            'Arena is requesting blockers; permitted pairings are candidates, not a complete verified combat plan.')
    if isinstance(their_life, int) and their_life <= 6 and s.get('active_player') == mine and not blocking:
        add('opponent_low', 'medium', 'Check whether a safe lethal line is available.',
            f'Opponent life total is {their_life}; this is not a verified lethal calculation.')

    if s.get('active_player') == mine and 'Phase_Main' in str(s.get('phase')):
        lands_hand = [c for c in my_hand if 'Land' in c.get('types', [])]
        lands_board = [c for c in my_board if 'Land' in c.get('types', [])]
        # We do not know whether Arena has already used the land drop.
        if lands_hand and len(lands_board) < 6:
            add('land_drop', 'medium', 'Check whether making a land drop now improves your curve.',
                f'{len(lands_hand)} land card(s) identified in hand; {len(lands_board)} land(s) identified on the battlefield. Land-drop availability unverified.',
                ['land_drop_available', 'mana_available', 'unidentified_board_cards'])

    my_creatures = [c for c in my_board if 'Creature' in c.get('types', [])]
    opp_creatures = [c for c in their_board if 'Creature' in c.get('types', [])]
    if s.get('active_player') == mine and my_creatures and 'Phase_Combat' in str(s.get('phase')):
        add('combat_review', 'medium', 'Review attacks, blocks, and instant-speed interaction before committing.',
            f'{len(my_creatures)} visible creature(s) on your battlefield; {len(opp_creatures)} on theirs. Summoning sickness and abilities not verified.')
    boards = [z for z in s.get('zones', []) if z.get('zone') == 'Battlefield']
    complete_board = bool(boards) and all(
        z.get('count') is not None and z['count'] == len(z['cards']) and
        all(c.get('identity_status') == 'visible' and c.get('controller') in (1, 2) and c.get('types') for c in
            z['cards'])
        for z in boards)
    if complete_board and my_creatures and opp_creatures and len(opp_creatures) >= len(my_creatures) + 2:
        add('board_pressure', 'medium', 'Consider whether you need to preserve blockers or trade resources.',
            f'Creature counts: yours {len(my_creatures)}, opponent {len(opp_creatures)}. Creature counts alone do not determine board strength.')
    if not advice:
        add('review', 'info', 'State captured. Review mana, open interaction, combat, and your next decision.',
            'The rules model is incomplete; no confident play recommendation is available.')
    return advice
