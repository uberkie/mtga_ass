"""Read-only, ordered JSON extraction with bounded buffers and safe diagnostics."""
from __future__ import annotations
import codecs
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterator


@dataclass(frozen=True)
class StreamRecord:
    kind: str
    payload: dict[str, Any] | None = None
    code: str | None = None
    position: int | None = None


class JsonObjectScanner:
    """Extract JSON across chunks; max_bytes is a legacy name for a character cap.

    Oversized records discard storage but retain lexical depth until the original
    record ends. A nested fragment cannot become a new top-level event.
    """
    def __init__(self, max_bytes: int = 12_000_000):
        if max_bytes < 1:
            raise ValueError('Invalid scanner limit')
        self.max_bytes = max_bytes
        self.malformed = 0
        self.discarded_oversize = 0
        self.position = 0
        self.reset()

    def reset(self) -> None:
        self._buffer: list[str] = []
        self._depth = 0
        self._string = False
        self._escape = False
        self._discard = False
        self._start = self.position

    def feed_records(self, text: str) -> list[StreamRecord]:
        result = []
        for ch in text:
            self.position += 1
            if not self._depth:
                if ch != '{':
                    continue
                self._start = self.position - 1
                self._buffer = ['{']
                self._depth = 1
                continue
            if not self._discard:
                self._buffer.append(ch)
            if self._string:
                if self._escape:
                    self._escape = False
                elif ch == '\\':
                    self._escape = True
                elif ch == '"':
                    self._string = False
            elif ch == '"':
                self._string = True
            elif ch == '{':
                self._depth += 1
            elif ch == '}':
                self._depth -= 1
            if not self._discard and len(self._buffer) > self.max_bytes:
                self.discarded_oversize += 1
                self._discard = True
                self._buffer.clear()
                result.append(StreamRecord('diagnostic', code='oversized_json', position=self._start))
            if not self._depth:
                if not self._discard:
                    try:
                        obj = json.loads(''.join(self._buffer))
                        if isinstance(obj, dict):
                            result.append(StreamRecord('event', payload=obj, position=self._start))
                    except (ValueError, TypeError, RecursionError):
                        self.malformed += 1
                        result.append(StreamRecord('diagnostic', code='malformed_json', position=self._start))
                self.reset()
        return result

    def feed(self, text: str) -> list[dict[str, Any]]:
        return [r.payload for r in self.feed_records(text) if r.kind == 'event']

    def finish(self) -> list[StreamRecord]:
        result = [StreamRecord('diagnostic', code='incomplete_json', position=self._start)] if self._depth else []
        self.reset()
        return result


def replay_records(path: Path) -> Iterator[StreamRecord]:
    scanner = JsonObjectScanner()
    with Path(path).open('r', encoding='utf-8-sig', errors='replace') as source:
        while chunk := source.read(65536):
            yield from scanner.feed_records(chunk)
    yield from scanner.finish()


def replay_file(path: Path, scanner: JsonObjectScanner | None = None) -> Iterator[dict[str, Any]]:
    scanner = scanner or JsonObjectScanner()
    with Path(path).open('r', encoding='utf-8-sig', errors='replace') as source:
        while chunk := source.read(65536):
            yield from scanner.feed(chunk)
    scanner.finish()


def follow_records(path: Path, poll_seconds: float = 0.25) -> Iterator[StreamRecord]:
    """Read appended chunks; reset on loss of source continuity, retry read races.

    The trailing anchor catches observed truncate/rewrite even if a writer has
    already grown the replacement beyond the previous offset. Filesystem tests
    establish observed behavior, not detection of every unobservable rewrite.
    """
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError('Invalid poll interval')
    path = Path(path)
    scanner = JsonObjectScanner()
    decoder = codecs.getincrementaldecoder('utf-8-sig')(errors='replace')
    handle = None
    identity = None
    anchor = b''
    try:
        while True:
            try:
                stat = path.stat()
                new_identity = (stat.st_dev, stat.st_ino)
                changed = False
                if handle is not None:
                    position = handle.tell()
                    changed = new_identity != identity or stat.st_size < position
                    if not changed and anchor:
                        handle.seek(position - len(anchor))
                        changed = handle.read(len(anchor)) != anchor
                        handle.seek(position)
                if changed:
                    handle.close()
                    handle = None
                    scanner.reset()
                    decoder.reset()
                    anchor = b''
                    yield StreamRecord('reset', code='source_changed')
                if handle is None:
                    handle = path.open('rb', buffering=0)
                    opened = os.fstat(handle.fileno())
                    identity = (opened.st_dev, opened.st_ino)
                    if identity != new_identity:
                        raise OSError('Source changed during open')
                raw = handle.read(65536)
                if raw:
                    anchor = (anchor + raw)[-128:]
                    yield from scanner.feed_records(decoder.decode(raw))
                    continue
            except OSError:
                if handle is not None:
                    handle.close()
                    handle = None
                    scanner.reset()
                    decoder.reset()
                    anchor = b''
                    yield StreamRecord('reset', code='source_unavailable')
            yield StreamRecord('idle')
            time.sleep(poll_seconds)
    finally:
        if handle is not None:
            handle.close()


def follow_file(path: Path, poll_seconds: float = 0.25) -> Iterator[dict[str, Any] | None]:
    for record in follow_records(path, poll_seconds):
        if record.kind == 'event':
            yield record.payload
        elif record.kind == 'reset':
            yield None
