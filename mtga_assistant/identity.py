"""Fail-closed identification of the local Arena player's system seat."""
from __future__ import annotations

from typing import Any


class ArenaSeatResolver:
    """Match Arena's authenticated screen name to one match-room player.

    The resolver retains only the current match's player-name list in memory
    until the authenticated name arrives, then exposes a validated seat number.
    It never uses card identities, life totals, or turn order to infer ownership.
    """

    def __init__(self) -> None:
        self.screen_name: str | None = None
        self.match_id: str | None = None
        self.players: list[Any] = []
        self.seat: int | None = None

    def reset(self) -> None:
        """Forget match-specific identity while keeping the authenticated name."""
        self.match_id = None
        self.players = []
        self.seat = None

    @staticmethod
    def _room_players(payload: dict[str, Any]) -> tuple[str | None, list[Any] | None] | None:
        event = payload.get('matchGameRoomStateChangedEvent')
        if not isinstance(event, dict):
            return None
        room = event.get('gameRoomInfo')
        if not isinstance(room, dict):
            return None
        config = room.get('gameRoomConfig')
        config = config if isinstance(config, dict) else {}
        match_id = config.get('matchId')
        if not isinstance(match_id, str) or not match_id:
            final = room.get('finalMatchResult')
            match_id = final.get('matchId') if isinstance(final, dict) else None
        # Missing fields are a partial room update, not an empty roster.
        # Explicit malformed/empty rosters still revoke the mapping.
        if 'players' in room:
            players = room['players']
            players = players if isinstance(players, list) else []
        elif 'reservedPlayers' in config:
            players = config['reservedPlayers']
            players = players if isinstance(players, list) else []
        else:
            players = None
        return (match_id if isinstance(match_id, str) else None, players)

    def _resolve(self, players: list[Any]) -> int | None:
        if self.screen_name is None or len(players) != 2:
            return None
        matches = []
        valid_seats = []
        for player in players:
            if not isinstance(player, dict):
                return None
            name = player.get('playerName')
            seat = player.get('systemSeatId')
            if (not isinstance(name, str) or not name.strip() or
                    type(seat) is not int or seat not in (1, 2)):
                return None
            if name.strip().casefold() == self.screen_name:
                matches.append(seat)
            valid_seats.append(seat)
        if len(matches) != 1 or set(valid_seats) != {1, 2}:
            return None
        return matches[0]

    def observe(self, payload: dict[str, Any]) -> int | None:
        """Observe a parsed Arena event and return the exact mapped seat, if any."""
        if not isinstance(payload, dict):
            return self.seat
        auth = payload.get('authenticateResponse')
        if isinstance(auth, dict) and 'screenName' in auth:
            name = auth.get('screenName')
            self.screen_name = name.strip().casefold() if isinstance(name, str) and name.strip() else None
            self.seat = self._resolve(self.players)
        room = self._room_players(payload)
        if room is not None:
            match_id, players = room
            if match_id is not None and match_id != self.match_id:
                self.reset()
            if match_id is not None:
                self.match_id = match_id
            if players is not None:
                self.players = players
            self.seat = self._resolve(self.players)
        return self.seat
