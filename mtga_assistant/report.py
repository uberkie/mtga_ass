"""Category-only replay evidence; successful parsing never verifies a real match."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from mtga_assistant.state import GameState, SUPPORTED_MESSAGES, DIAGNOSTICS
from mtga_assistant.stream import StreamRecord


@dataclass
class ValidationReport:
    fixture_provenance: str = 'user_log_unverified'
    events: int = 0
    applied_updates: int = 0
    resets: int = 0
    diagnostics: Counter = field(default_factory=Counter)
    supported_messages: Counter = field(default_factory=Counter)
    limitations_seen: set[str] = field(default_factory=set)
    final_integrity: dict[str, Any] = field(default_factory=lambda: {'status': 'waiting', 'limitations': []})

    def __post_init__(self) -> None:
        if self.fixture_provenance not in ('synthetic', 'user_log_unverified'):
            raise ValueError('Independent evidence required for verified provenance')

    def record(self, record: StreamRecord, state: GameState, applied: int) -> None:
        self.applied_updates += applied
        if record.kind == 'reset':
            self.resets += 1
        elif record.kind == 'diagnostic':
            code = record.code if record.code in DIAGNOSTICS else 'invalid_schema'
            self.diagnostics[code] += 1
        elif record.kind == 'event':
            self.events += 1
            payload = record.payload if isinstance(record.payload, dict) else {}
            event = payload.get('greToClientEvent', {})
            messages = event.get('greToClientMessages', []) if isinstance(event, dict) else []
            if isinstance(messages, list):
                for message in messages:
                    kind = message.get('type') if isinstance(message, dict) else None
                    if isinstance(kind, str) and kind in SUPPORTED_MESSAGES:
                        self.supported_messages[kind] += 1
        # Reducer diagnostics are session history, not current integrity. An
        # authoritative full state later in this same batch may recover state,
        # but must not erase evidence of unsupported/malformed earlier messages.
        self.diagnostics = Counter(state.diagnostic_counts)
        self.limitations_seen.update(state.diagnostic_counts)
        self.final_integrity = state.integrity()
        self.limitations_seen.update(self.final_integrity['limitations'])

    def as_dict(self) -> dict[str, Any]:
        return {
            'fixture_provenance': self.fixture_provenance,
            'independent_validation': 'pending',
            'events': self.events,
            'applied_updates': self.applied_updates,
            'resets': self.resets,
            'diagnostics': dict(sorted(self.diagnostics.items())),
            'supported_messages': dict(sorted(self.supported_messages.items())),
            'limitations_seen': sorted(self.limitations_seen),
            'final_integrity': self.final_integrity,
        }
