"""Ordered GRE reducer with explicit integrity and a sanitized snapshot boundary."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from mtga_assistant.decisions import DecisionTracker
from mtga_assistant.stream import StreamRecord

SUPPORTED_MESSAGES = frozenset({
    'GREMessageType_GameStateMessage', 'GREMessageType_QueuedGameStateMessage',
    'GREMessageType_ConnectResp',
})
IGNORABLE_MESSAGES = frozenset({'GREMessageType_TimerStateMessage'})
DIAGNOSTICS = frozenset({'malformed_json', 'oversized_json', 'incomplete_json',
    'invalid_schema', 'older_state', 'state_gap', 'unsupported_state_message',
    'unsupported_instance_remap', 'unresolved_instance_remap', 'conflicting_zone', 'unsupported_zone'})


def integer(value: Any) -> bool:
    return type(value) is int


def _effective_group_id(obj: Mapping[str, Any]) -> int | None:
    overlay = obj.get('overlayGrpId')
    if integer(overlay) and 1 <= overlay <= 2**63 - 1:
        return overlay
    group = obj.get('grpId')
    if integer(group) and 1 <= group <= 2**63 - 1:
        return group
    return None


def int_value(value: Any) -> int | None:
    return value if integer(value) else None


def seat_value(value: Any) -> int | None:
    return value if integer(value) and value in (1, 2) else None


def enum_value(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def stat_value(value: Any) -> int | None:
    return int_value(value.get('value') if isinstance(value, dict) else value)


def viewer_contains(value: Any, seat: int) -> bool:
    return (isinstance(value, list) and all(integer(v) and v in (1, 2) for v in value)
            and seat in value)


def instance_change(annotation: dict[str, Any]) -> tuple[int, int] | None:
    """Observed ObjectIdChanged orig_id/new_id singleton int32 detail shape."""
    details = annotation.get('details')
    if not isinstance(details, list) or len(details) != 2:
        return None
    ids = {}
    for detail in details:
        if not isinstance(detail, dict):
            return None
        key = detail.get('key')
        values = detail.get('valueInt32')
        if (key not in ('orig_id', 'new_id') or key in ids or
            detail.get('type') != 'KeyValuePairValueType_int32' or
            not isinstance(values, list) or len(values) != 1 or
            not integer(values[0]) or values[0] <= 0):
            return None
        ids[key] = values[0]
    original, replacement = ids['orig_id'], ids['new_id']
    return (original, replacement) if original != replacement else None


VISIBLE_ZONES = frozenset('ZoneType_' + z for z in ('Hand', 'Battlefield', 'Graveyard', 'Exile', 'Stack'))
SNAPSHOT_ZONES = VISIBLE_ZONES | {'ZoneType_Library'}
PHYSICAL_ZONES = SNAPSHOT_ZONES | {'ZoneType_Command', 'ZoneType_Sideboard'}
ZONE_TYPES = PHYSICAL_ZONES | {
    'ZoneType_Revealed', 'ZoneType_Suppressed', 'ZoneType_Pending', 'ZoneType_Limbo'}
CARD_TYPES = frozenset('CardType_' + t for t in ('Land', 'Creature', 'Artifact', 'Enchantment', 'Instant', 'Sorcery', 'Planeswalker', 'Battle', 'Tribal', 'Kindred'))
PHASES = frozenset('Phase_' + p for p in ('Beginning', 'Main1', 'Combat', 'Main2', 'Ending'))
STEPS = frozenset('Step_' + p for p in ('Untap', 'Upkeep', 'Draw', 'BeginCombat', 'DeclareAttack', 'DeclareBlock', 'CombatDamage', 'FirstStrikeDamage', 'EndCombat', 'End', 'Cleanup'))
GAME_STAGES = frozenset({'GameStage_Start', 'GameStage_Play', 'GameStage_GameOver'})
MATCH_STATES = frozenset({'MatchState_GameInProgress', 'MatchState_MatchComplete'})
ATTACK_STATES = frozenset({'AttackState_None', 'AttackState_Attacking', 'AttackState_NotAttacking'})
_BASIC_LAND_MANA = {
    'SubType_Plains': 'White', 'SubType_Island': 'Blue', 'SubType_Swamp': 'Black',
    'SubType_Mountain': 'Red', 'SubType_Forest': 'Green',
}


@dataclass
class GameState:
    my_seat: int
    match_id: str | None = None
    game_number: int | None = None
    game_stage: str | None = None
    match_status: str | None = None
    turn: dict[str, Any] = field(default_factory=dict)
    players: dict[int, dict[str, Any]] = field(default_factory=dict)
    zones: dict[int, dict[str, Any]] = field(default_factory=dict)
    objects: dict[int, dict[str, Any]] = field(default_factory=dict)
    uncertain_memberships: set[int] = field(default_factory=set)
    updates: int = 0
    revision: int = 0
    last_state_id: int | None = None
    limitations: set[str] = field(default_factory=set)
    diagnostic_counts: Counter = field(default_factory=Counter)
    decision_tracker: DecisionTracker = field(default_factory=DecisionTracker, repr=False)
    _has_state: bool = False
    mulligans_taken: int = 0
    bottom_required: int | None = None
    bottom_pending: bool = False
    opening_active: bool = False
    mulligan_pending: bool = False
    _pending_boundary: str | None = None

    def __post_init__(self) -> None:
        if type(self.my_seat) is not int or self.my_seat not in (1, 2):
            raise ValueError('Invalid seat')

    def reset_game(self) -> None:
        self.game_stage = None
        self.match_status = None
        self.mulligans_taken = 0
        self.bottom_required = None
        self.bottom_pending = False
        self.opening_active = False
        self.mulligan_pending = False
        self.turn.clear()
        self.players.clear()
        self.zones.clear()
        self.objects.clear()
        self.uncertain_memberships.clear()
        self.last_state_id = None
        self.limitations.clear()
        self.decision_tracker.clear()
        self._has_state = False
        self.revision += 1

    def invalidate(self, code: str) -> None:
        code = code if code in DIAGNOSTICS else 'invalid_schema'
        self.diagnostic_counts[code] += 1
        decision_cleared = self.decision_tracker.clear()
        if code not in self.limitations:
            self.limitations.add(code)
            self.revision += 1
        elif decision_cleared:
            self.revision += 1

    def _mapping(self, payload: dict, key: str) -> dict:
        value = payload.get(key, {})
        if not isinstance(value, dict):
            self.invalidate('invalid_schema')
            return {}
        return value

    def _array(self, payload: dict, key: str) -> list:
        value = payload.get(key, [])
        if not isinstance(value, list):
            self.invalidate('invalid_schema')
            return []
        return value

    def consume_record(self, record: StreamRecord, *,
                       on_applied: Callable[[dict[str, Any], str | None], None] | None = None,
                       card_names: Mapping[int, str] | None = None,
                       card_lookup: Callable[[Sequence[int]], Mapping[int, Mapping[str, Any]]] | None = None) -> int:
        if record.kind == 'reset':
            self.reset_game()
            self.match_id = None
            self.game_number = None
            self._pending_boundary = None
        elif record.kind == 'diagnostic':
            self.invalidate(record.code or 'invalid_schema')
        elif record.kind == 'event':
            return self.consume(record.payload, on_applied=on_applied, card_names=card_names,
                                card_lookup=card_lookup)
        return 0

    def _apply_match_room(self, payload: dict[str, Any]) -> bool:
        if 'matchGameRoomStateChangedEvent' not in payload:
            return False
        event = self._mapping(payload, 'matchGameRoomStateChangedEvent')
        room = self._mapping(event, 'gameRoomInfo')
        config = self._mapping(room, 'gameRoomConfig')
        final = self._mapping(room, 'finalMatchResult')
        match_id = config.get('matchId') or final.get('matchId')
        if match_id is not None and not isinstance(match_id, str):
            self.invalidate('invalid_schema')
        elif match_id and match_id != self.match_id:
            self.reset_game()
            self.match_id = match_id
            self.game_number = None
            self._pending_boundary = 'match_changed'
            return True
        return False

    def consume(self, payload: dict[str, Any], *,
                on_applied: Callable[[dict[str, Any], str | None], None] | None = None,
                card_names: Mapping[int, str] | None = None,
                card_lookup: Callable[[Sequence[int]], Mapping[int, Mapping[str, Any]]] | None = None) -> int:
        if not isinstance(payload, dict):
            self.invalidate('invalid_schema')
            return 0
        self._apply_match_room(payload)
        if 'greToClientEvent' not in payload:
            if self.decision_tracker.observe_client_envelopes(payload):
                self.revision += 1
            return 0
        event = self._mapping(payload, 'greToClientEvent')
        count = 0
        for message in self._array(event, 'greToClientMessages'):
            if not isinstance(message, dict):
                self.invalidate('invalid_schema')
                continue
            kind = message.get('type')
            # Arena emits timer-only updates alongside GRE state. They do not
            # change the game snapshot used for coaching.
            if isinstance(kind, str) and kind in IGNORABLE_MESSAGES:
                continue
            if kind == 'GREMessageType_MulliganReq':
                req = message.get('mulliganReq', {})
                self.opening_active = True
                self.mulligan_pending = True
                self.bottom_pending = False
                if isinstance(req, dict):
                    count = req.get('mulliganCount', 0)
                    if integer(count) and 0 <= count <= 7:
                        self.mulligans_taken = count
                self.bottom_required = None
            if kind == 'GREMessageType_GroupReq':
                req = message.get('groupReq', {})
                if isinstance(req, dict) and req.get('context') == 'GroupingContext_LondonMulligan':
                    self.opening_active = True
                    self.bottom_pending = True
                    for spec in req.get('groupSpecs', []):
                        if (isinstance(spec, dict) and spec.get('zoneType') == 'ZoneType_Library'
                                and spec.get('subZoneType') == 'SubZoneType_Bottom'
                                and integer(spec.get('lowerBound'))):
                            self.bottom_required = spec['lowerBound']
            if DecisionTracker.is_request_type(kind):
                if (self.limitations or self.game_stage == 'GameStage_GameOver' or
                        self.match_status == 'MatchState_MatchComplete'):
                    if self.decision_tracker.clear():
                        self.revision += 1
                    continue
                # Choice IDs reference game-object instances. Preserve distinct
                # *publicly visible* card labels even without an optional catalog,
                # but never infer identities for concealed or opponent-hand cards.
                visible_context = self._visible_card_context(card_names or {})
                visible_cards = self._visible_decision_labels(card_names or {})
                if self.decision_tracker.observe_request(
                        message, my_seat=self.my_seat, current_state_id=self.last_state_id,
                        visible_cards=visible_cards, visible_context=visible_context,
                        card_lookup=card_lookup):
                    self.revision += 1
                continue
            if not isinstance(kind, str) or kind not in SUPPORTED_MESSAGES:
                if 'gameStateMessage' in message or (isinstance(kind, str) and
                       any(word in kind for word in ('State', 'ObjectIdChanged'))):
                    self.invalidate('unsupported_state_message')
                continue
            game = message.get('gameStateMessage')
            if kind == 'GREMessageType_ConnectResp' and game is None:
                game = self._mapping(message, 'connectResp').get('gameStateMessage')
            if game is None and kind == 'GREMessageType_ConnectResp':
                continue
            if not isinstance(game, dict):
                self.invalidate('invalid_schema')
                continue
            before = self.updates
            old_game_number = self.game_number
            had_state = self._has_state
            pending_boundary = self._pending_boundary
            self._pending_boundary = None
            full = game.get('type') in ('GameStateType_Full', 'GameStateType_Initial')
            game_info = game.get('gameInfo')
            incoming_game_number = game_info.get('gameNumber') if isinstance(game_info, dict) else None
            new_game = (integer(incoming_game_number) and old_game_number is not None and
                        incoming_game_number != old_game_number)
            self.apply(game)
            applied = self.updates - before
            count += applied
            if applied:
                boundary = pending_boundary
                if boundary is None and new_game:
                    boundary = 'game_changed'
                elif boundary is None and full and had_state:
                    boundary = 'same_game_replaced'
                if on_applied is not None:
                    on_applied(self.public_snapshot(card_names), boundary)
            elif pending_boundary is not None:
                self._pending_boundary = pending_boundary
        if self.decision_tracker.observe_client_envelopes(payload):
            self.revision += 1
        # A bottom-card response closes the pregame grouping choice; the game
        # remains in Start stage until actual turn-1 state is applied.
        envelope = payload.get('payload')
        if isinstance(envelope, dict) and envelope.get('type') == 'ClientMessageType_MulliganResp':
            self.mulligan_pending = False
        if (isinstance(envelope, dict) and envelope.get('type') == 'ClientMessageType_GroupResp'
                and self.bottom_pending):
            self.bottom_pending = False
        return count

    def apply(self, game: dict[str, Any]) -> None:
        if not isinstance(game, dict):
            self.invalidate('invalid_schema')
            return
        full = game.get('type') in ('GameStateType_Full', 'GameStateType_Initial')
        raw_info = game.get('gameInfo', {})
        info = raw_info if isinstance(raw_info, dict) else {}
        number = info.get('gameNumber')
        new_game = integer(number) and number > 0 and self.game_number is not None and number != self.game_number
        state_id = game.get('gameStateId')
        prev_id = game.get('prevGameStateId')
        if state_id is not None and not integer(state_id):
            self.invalidate('invalid_schema')
            return
        if prev_id is not None and not integer(prev_id):
            self.invalidate('invalid_schema')
            return
        # Same-game duplicate/older full snapshots are no more authoritative
        # than duplicate/older deltas. Check before replacement clears the ID.
        if not new_game and self.last_state_id is not None:
            if state_id == self.last_state_id:
                return
            if state_id is not None and state_id < self.last_state_id:
                self.invalidate('older_state')
                return
            if not full and prev_id is not None and prev_id != self.last_state_id:
                self.invalidate('state_gap')
                return
        if state_id is not None:
            self.decision_tracker.expire_for_state(state_id, full=full or new_game)
        if full or new_game:
            self.reset_game()
        # Remember source locations before this delta can remove the old ID.
        source_zone_memberships: dict[int, set[int]] = {}
        for zone_id, zone in self.zones.items():
            if zone.get('type') in PHYSICAL_ZONES:
                for instance_id in zone.get('objectInstanceIds', []):
                    source_zone_memberships.setdefault(instance_id, set()).add(zone_id)
        for instance_id, obj in self.objects.items():
            zone_id = obj.get('zoneId')
            if zone_id in self.zones and self.zones[zone_id].get('type') in PHYSICAL_ZONES:
                source_zone_memberships.setdefault(instance_id, set()).add(zone_id)
        refreshed_zone_ids: set[int] = set()
        info = self._mapping(game, 'gameInfo')
        if number is not None:
            if integer(number) and number > 0:
                self.game_number = number
            else:
                self.invalidate('invalid_schema')
        if state_id is not None:
            self.last_state_id = state_id
        self.game_stage = info.get('stage', self.game_stage)
        self.match_status = info.get('matchState', self.match_status)
        if self.game_stage in ('GameStage_Play', 'GameStage_GameOver'):
            self.opening_active = False
            self.bottom_pending = False
            self.mulligan_pending = False
        if (self.game_stage == 'GameStage_GameOver' or
                self.match_status == 'MatchState_MatchComplete'):
            self.decision_tracker.clear()
        self.turn.update(self._mapping(game, 'turnInfo'))
        for player in self._array(game, 'players'):
            if not isinstance(player, dict):
                self.invalidate('invalid_schema')
                continue
            seat = player.get('systemSeatNumber', player.get('systemSeatId'))
            if integer(seat) and seat in (1, 2):
                self.players.setdefault(seat, {}).update(player)
            else:
                self.invalidate('invalid_schema')
        for instance_id in self._array(game, 'diffDeletedInstanceIds'):
            if not integer(instance_id):
                self.invalidate('invalid_schema')
                continue
            self.objects.pop(instance_id, None)
            for zone in self.zones.values():
                if 'objectInstanceIds' in zone:
                    zone['objectInstanceIds'] = [x for x in zone['objectInstanceIds'] if x != instance_id]
        for zone in self._array(game, 'zones'):
            if not isinstance(zone, dict) or not integer(zone.get('zoneId')):
                self.invalidate('invalid_schema')
                continue
            if 'type' in zone:
                if not isinstance(zone['type'], str):
                    self.invalidate('invalid_schema')
                    continue
                if zone['type'] not in ZONE_TYPES:
                    self.invalidate('unsupported_zone')
                    continue
            if (full and zone.get('visibility') == 'Visibility_Public' and
                    zone.get('type') in {'ZoneType_Battlefield', 'ZoneType_Stack',
                                         'ZoneType_Graveyard', 'ZoneType_Exile'} and
                    'objectInstanceIds' not in zone):
                # Full GRE snapshots omit empty protobuf repeated fields. A
                # delta without this field still leaves membership unchanged.
                zone = {**zone, 'objectInstanceIds': []}
            if 'objectInstanceIds' in zone:
                ids = zone['objectInstanceIds']
                if not isinstance(ids, list) or any(not integer(x) for x in ids) or len(set(ids)) != len(ids):
                    self.invalidate('invalid_schema')
                    continue
                refreshed_zone_ids.add(zone['zoneId'])
                self.uncertain_memberships.discard(zone['zoneId'])
            self.zones.setdefault(zone['zoneId'], {}).update(zone)
        for obj in self._array(game, 'gameObjects'):
            if not isinstance(obj, dict) or not integer(obj.get('instanceId')):
                self.invalidate('invalid_schema')
                continue
            if 'zoneId' in obj and not integer(obj['zoneId']):
                self.invalidate('invalid_schema')
                continue
            instance_id = obj['instanceId']
            combined = self.objects.setdefault(instance_id, {})
            complete_card = (
                obj.get('type') == 'GameObjectType_Card' and
                all(key in obj for key in ('zoneId', 'visibility', 'controllerSeatId', 'cardTypes'))
            )
            if full or complete_card:
                # Complete object records omit false/empty protobuf defaults,
                # including a land becoming untapped in a diff message.
                obj = {'isTapped': False, 'subtypes': [], 'superTypes': [], **obj}
            before = combined.get('zoneId')
            moved = 'zoneId' in obj and before is not None and obj['zoneId'] != before
            face_changed = any(k in obj and obj[k] != combined.get(k) for k in
                               ('isFaceDown', 'faceDown', 'faceDownStatus'))
            if moved:
                combined.pop('visibility', None)
            if face_changed:
                for key in ('grpId', 'overlayGrpId', 'cardTypes', 'subtypes', 'superTypes',
                            'power', 'toughness', 'visibility'):
                    combined.pop(key, None)
            combined.update(obj)
            # Private/unknown updates can themselves contain identities. Discard
            # those values too, so a later visibility-only delta cannot revive
            # either the formerly public face or private metadata from this one.
            private_hand_exception = (combined.get('visibility') == 'Visibility_Private' and
                                      self._private_hand_visible(combined))
            hidden_visibility = ('visibility' in combined and combined['visibility'] != 'Visibility_Public'
                                 and not private_hand_exception)
            hidden_face = (combined.get('isFaceDown') is True or combined.get('faceDown') is True or
                           combined.get('faceDownStatus') in ('FaceDown', 'FaceDownStatus_FaceDown'))
            unknown_face = any(k in combined and type(combined[k]) is not bool for k in ('isFaceDown', 'faceDown'))
            if hidden_visibility or hidden_face or unknown_face:
                for key in ('grpId', 'overlayGrpId', 'cardTypes', 'subtypes', 'superTypes',
                            'power', 'toughness'):
                    combined.pop(key, None)
            after = combined.get('zoneId')
            if before is not None and after != before and before in self.zones:
                old = self.zones[before]
                if 'objectInstanceIds' in old:
                    old['objectInstanceIds'] = [x for x in old['objectInstanceIds'] if x != instance_id]
        remaps: dict[int, int] = {}
        conflicting_remaps: set[int] = set()
        for annotation in self._array(game, 'annotations'):
            if not isinstance(annotation, dict):
                self.invalidate('invalid_schema')
                continue
            types = annotation.get('type', [])
            if types == 'AnnotationType_ObjectIdChanged' or (isinstance(types, list) and
                        'AnnotationType_ObjectIdChanged' in types):
                change = instance_change(annotation)
                if change is None:
                    self.invalidate('unsupported_instance_remap')
                    continue
                original, replacement = change
                if original in remaps and remaps[original] != replacement:
                    conflicting_remaps.add(original)
                    self.invalidate('unsupported_instance_remap')
                else:
                    remaps[original] = replacement
                # New objects carry their own authoritative fields. Never clone
                # a hidden/public face across an instance-ID change. Logs may
                # include both the old Limbo object and the fresh target object.
                self.objects.pop(original, None)
                for zone in self.zones.values():
                    if 'objectInstanceIds' in zone:
                        zone['objectInstanceIds'] = [i for i in zone['objectInstanceIds'] if i != original]
        # Resolve chains only after processing every annotation. Intermediate
        # IDs can be present briefly even when the final replacement is new.
        def final_instance(instance_id: int) -> int | None:
            seen: set[int] = set()
            current = instance_id
            while current in remaps:
                if current in seen:
                    return None
                seen.add(current)
                current = remaps[current]
            return current

        for original in remaps:
            terminal = final_instance(original)
            source_zones = source_zone_memberships.get(original, set())
            if terminal is None or original in conflicting_remaps:
                self.invalidate('unsupported_instance_remap')
                self.uncertain_memberships.update(source_zones)
                continue
            target = self.objects.get(terminal)
            target_zone_id = target.get('zoneId') if target else None
            target_zone = self.zones.get(target_zone_id, {}) if integer(target_zone_id) else {}
            object_location = target_zone_id if target_zone.get('type') in PHYSICAL_ZONES else None
            membership_locations = {
                zone_id for zone_id, zone in self.zones.items()
                if zone.get('type') in PHYSICAL_ZONES and terminal in zone.get('objectInstanceIds', [])
            }
            known_locations = membership_locations | ({object_location} if object_location is not None else set())
            if len(known_locations) > 1:
                self.invalidate('conflicting_zone')
                self.uncertain_memberships.update(source_zones | known_locations)
                continue
            if not known_locations:
                self.invalidate('unresolved_instance_remap')
                self.uncertain_memberships.update(source_zones)
                continue
            location = next(iter(known_locations))
            # A pure ID rename preserves zone membership when no replacement
            # membership list was sent. An explicit conflicting list is uncertain.
            for source_zone_id in source_zones:
                if source_zone_id != location:
                    continue
                source_zone = self.zones.get(source_zone_id)
                if not source_zone or 'objectInstanceIds' not in source_zone:
                    continue
                if source_zone_id in refreshed_zone_ids and terminal not in source_zone['objectInstanceIds']:
                    self.invalidate('conflicting_zone')
                    self.uncertain_memberships.add(source_zone_id)
                elif terminal not in source_zone['objectInstanceIds']:
                    source_zone['objectInstanceIds'].append(terminal)
        # Zone-only viewer changes can revoke private hand visibility too.
        for obj in self.objects.values():
            if obj.get('visibility') == 'Visibility_Private' and not self._private_hand_visible(obj):
                for key in ('grpId', 'overlayGrpId', 'cardTypes', 'subtypes', 'superTypes',
                            'power', 'toughness'):
                    obj.pop(key, None)
        for zone_id, zone in self.zones.items():
            if zone.get('type') not in PHYSICAL_ZONES:
                continue  # Revealed and other internal views can overlap physical zones.
            for instance_id in zone.get('objectInstanceIds', []):
                obj = self.objects.get(instance_id)
                if obj and obj.get('zoneId') is not None and obj['zoneId'] != zone_id:
                    self.invalidate('conflicting_zone')
        self._has_state = True
        self.updates += 1
        self.revision += 1

    def integrity(self) -> dict[str, Any]:
        limitations = set(self.limitations)
        if limitations:
            status = 'uncertain'
        elif (self.game_stage == 'GameStage_GameOver' or
                self.match_status == 'MatchState_MatchComplete'):
            status = 'ended'
        elif not self._has_state:
            status = 'waiting'
        else:
            if not integer(self.turn.get('turnNumber')):
                limitations.add('missing_turn')
            if any(not integer(self.players.get(seat, {}).get('lifeTotal')) for seat in (1, 2)):
                limitations.add('missing_life_totals')
            public = [z for z in self.zones.values() if z.get('type') == 'ZoneType_Battlefield']
            hand = [z for z in self.zones.values() if z.get('type') == 'ZoneType_Hand' and z.get('ownerSeatId') == self.my_seat]
            if not public or not hand:
                limitations.add('missing_zones')
            if (any('objectInstanceIds' not in z for z in self.zones.values() if z.get('type') in SNAPSHOT_ZONES) or
                any(zone_id in self.uncertain_memberships for zone_id, z in self.zones.items()
                    if z.get('type') in SNAPSHOT_ZONES)):
                limitations.add('unknown_zone_membership')
            public_zone_types = {'ZoneType_Battlefield', 'ZoneType_Stack',
                                 'ZoneType_Graveyard', 'ZoneType_Exile'}
            public_memberships: dict[int, set[int]] = {}
            for zone_id, zone in self.zones.items():
                if zone.get('type') not in public_zone_types:
                    continue
                for instance_id in zone.get('objectInstanceIds', []):
                    obj = self.objects.get(instance_id)
                    if obj and obj.get('zoneId') is None:
                        public_memberships.setdefault(instance_id, set()).add(zone_id)
            if any(len(zone_ids) > 1 for zone_ids in public_memberships.values()):
                limitations.add('unknown_zone_membership')
            if any(i not in self.objects for z in public + hand for i in z.get('objectInstanceIds', [])):
                limitations.add('missing_visible_objects')
            status = 'partial' if limitations else 'consistent'
        return {'status': status, 'limitations': sorted(limitations)}

    def _private_hand_visible(self, obj: dict[str, Any]) -> bool:
        zone_id = obj.get('zoneId')
        zone = self.zones.get(zone_id, {}) if integer(zone_id) else {}
        return (zone.get('type') == 'ZoneType_Hand' and
                seat_value(zone.get('ownerSeatId')) == self.my_seat and
                seat_value(obj.get('ownerSeatId')) == self.my_seat and
                viewer_contains(zone.get('viewers'), self.my_seat) and
                viewer_contains(obj.get('viewers'), self.my_seat))

    def _visible_cards(self, zone: dict[str, Any], card_names: dict[int, str]) -> list[dict[str, Any]]:
        kind = zone.get('type')
        owner = seat_value(zone.get('ownerSeatId'))
        if kind not in VISIBLE_ZONES or (kind == 'ZoneType_Hand' and owner != self.my_seat):
            return []
        result = []
        for instance_id in zone.get('objectInstanceIds', []):
            obj = self.objects.get(instance_id)
            if not obj or (obj.get('zoneId') is not None and obj['zoneId'] != zone.get('zoneId')):
                continue
            if kind == 'ZoneType_Hand' and 'ownerSeatId' in obj and seat_value(obj['ownerSeatId']) != self.my_seat:
                continue
            face_down = (obj.get('isFaceDown') is True or obj.get('faceDown') is True or
                         obj.get('faceDownStatus') in ('FaceDown', 'FaceDownStatus_FaceDown'))
            # Unknown face status and visibility never authorize disclosure.
            face_unknown = any(k in obj and type(obj[k]) is not bool for k in ('isFaceDown', 'faceDown'))
            face_unknown |= 'faceDownStatus' in obj and obj['faceDownStatus'] not in (
                'FaceDown', 'FaceDownStatus_FaceDown', 'FaceUp', 'FaceDownStatus_FaceUp')
            visible = not face_down and not face_unknown and (
                obj.get('visibility') == 'Visibility_Public' or
                (obj.get('visibility') == 'Visibility_Private' and self._private_hand_visible(obj)) or
                (kind == 'ZoneType_Hand' and 'visibility' not in obj))
            # There is no fixture-backed seat-specific face-down visibility marker.
            # Even ownership and public object visibility do not identify that face.
            status = 'visible' if visible else ('concealed' if face_down else 'unknown')
            gid = _effective_group_id(obj)
            name = card_names.get(gid) if visible else None
            types = obj.get('cardTypes', [])
            types = [t.removeprefix('CardType_') for t in types if isinstance(t, str) and t in CARD_TYPES] if isinstance(types, list) and visible else []
            result.append({
                'id': instance_id,
                'identity_status': status,
                'name': (name or (f'Card #{gid}' if gid else 'Unknown card')) if visible else ('Face-down card' if face_down else 'Unknown card'),
                'controller': seat_value(obj.get('controllerSeatId')),
                'owner': seat_value(obj.get('ownerSeatId')),
                'types': types,
                'power': stat_value(obj.get('power')) if visible else None,
                'toughness': stat_value(obj.get('toughness')) if visible else None,
                'tapped': obj.get('isTapped') if type(obj.get('isTapped')) is bool else None,
                'attack_state': enum_value(obj.get('attackState'), ATTACK_STATES),
            })
            subtypes = obj.get('subtypes')
            supertypes = obj.get('superTypes')
            if (visible and 'Land' in types and 'Creature' not in types and
                    isinstance(supertypes, list) and 'SuperType_Basic' in supertypes and
                    isinstance(subtypes, list)):
                colors = sorted({_BASIC_LAND_MANA[t] for t in subtypes
                                 if isinstance(t, str) and t in _BASIC_LAND_MANA})
                if len(colors) == 1:
                    result[-1]['basic_mana'] = colors[0]
        return result

    def public_opponent_card_counts(self) -> dict[int, int]:
        """Count visible opponent-owned cards in supported public zones only."""
        opponent_seat = 3 - self.my_seat
        public_zones = {'ZoneType_Battlefield', 'ZoneType_Stack',
                        'ZoneType_Graveyard', 'ZoneType_Exile'}
        memberships: dict[int, set[int]] = {}
        arena_ids: dict[int, int] = {}
        for zone in self.zones.values():
            if zone.get('type') not in public_zones:
                continue
            for card in self._visible_cards(zone, {}):
                if card.get('identity_status') != 'visible' or card.get('owner') != opponent_seat:
                    continue
                obj = self.objects.get(card.get('id'))
                if not obj:
                    continue
                overlay_id = obj.get('overlayGrpId')
                arena_id = overlay_id if integer(overlay_id) and overlay_id > 0 else obj.get('grpId')
                if integer(arena_id) and 0 < arena_id <= 2**63 - 1:
                    instance_id = card.get('id')
                    zone_id = zone.get('zoneId')
                    if not integer(instance_id) or not integer(zone_id):
                        continue
                    memberships.setdefault(instance_id, set()).add(zone_id)
                    arena_ids[instance_id] = arena_id

        # Logs can omit an object's zoneId while retaining stale or duplicated
        # memberships. Count only instances observed in one public zone.
        counts: Counter[int] = Counter(
            arena_ids[instance_id]
            for instance_id, zone_ids in memberships.items()
            if len(zone_ids) == 1
        )
        return dict(counts)

    def _visible_card_context(self, card_names: Mapping[int, str]) -> dict[int, dict[str, Any]]:
        context: dict[int, dict[str, Any]] = {}
        for zone in self.zones.values():
            for card in self._visible_cards(zone, card_names):
                if card.get('identity_status') != 'visible' or not integer(card.get('id')):
                    continue
                obj = self.objects.get(card['id'], {})
                group_id = _effective_group_id(obj)
                label = card_names.get(group_id) if group_id is not None else None
                if group_id is None or not isinstance(label, str) or not label.strip():
                    continue
                context[card['id']] = {'label': label, 'arena_id': group_id}
        return context

    def _visible_decision_labels(self, card_names: Mapping[int, str]) -> dict[int, str]:
        labels: dict[int, str] = {}
        for zone in self.zones.values():
            for card in self._visible_cards(zone, card_names):
                if card.get('identity_status') != 'visible' or not integer(card.get('id')):
                    continue
                # An Arena grpId identifies this visible object, not its card
                # name. Mark it as unmapped so the coach cannot fabricate rules.
                name = card.get('name')
                if isinstance(name, str) and name.strip():
                    labels[card['id']] = name[:120]
        return labels

    def _visible_card_labels(self, card_names: Mapping[int, str]) -> dict[int, str]:
        return {instance_id: card['label']
                for instance_id, card in self._visible_card_context(card_names).items()}

    def lifecycle(self) -> str:
        if self.game_stage == 'GameStage_GameOver':
            return 'MATCH_ENDED' if self.match_status == 'MatchState_MatchComplete' else 'GAME_ENDED'
        if self.match_status == 'MatchState_MatchComplete':
            return 'MATCH_ENDED'
        if self.game_stage == 'GameStage_Play':
            return 'IN_GAME'
        if self.bottom_pending:
            return 'BOTTOM_CARDS'
        if self.opening_active:
            return 'MULLIGAN' if self.mulligan_pending else 'OPENING_HAND'
        if self.game_stage == 'GameStage_Start':
            return 'MATCH_LOADING'
        return 'IDLE' if not self._has_state else 'RESYNCING'

    def public_snapshot(self, card_names: dict[int, str] | None = None) -> dict[str, Any]:
        """Allowlisted state for CLI, coaching and inference; never raw records."""
        card_names = card_names or {}
        zones = []
        for zone_id, zone in sorted(self.zones.items()):
            kind = zone.get('type')
            if not isinstance(kind, str) or kind not in SNAPSHOT_ZONES:
                continue
            zones.append({
                'zone_id': zone_id,
                'zone': kind.removeprefix('ZoneType_'),
                'owner': seat_value(zone.get('ownerSeatId')),
                'count': (len(zone['objectInstanceIds']) if 'objectInstanceIds' in zone and
                          zone_id not in self.uncertain_memberships else None),
                'cards': self._visible_cards(zone, card_names),
            })
        return {
            'revision': self.revision,
            'lifecycle': self.lifecycle(),
            'mulligans_taken': self.mulligans_taken,
            'bottom_required': self.bottom_required,
            'integrity': self.integrity(),
            'my_seat': self.my_seat,
            'game_number': self.game_number,
            'turn_number': int_value(self.turn.get('turnNumber')),
            'phase': enum_value(self.turn.get('phase'), PHASES),
            'step': enum_value(self.turn.get('step'), STEPS),
            'active_player': seat_value(self.turn.get('activePlayer')),
            'decision_player': seat_value(self.turn.get('decisionPlayer')),
            'priority_player': seat_value(self.turn.get('priorityPlayer')),
            'game_stage': enum_value(self.game_stage, GAME_STAGES),
            'match_status': enum_value(self.match_status, MATCH_STATES),
            'life_totals': {str(s): int_value(p.get('lifeTotal')) for s, p in self.players.items()},
            'zones': zones,
            'decision': self.decision_tracker.public_snapshot(),
        }
