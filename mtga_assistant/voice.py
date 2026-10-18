"""Local Windows speech I/O for the optional voice assistant."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

from mtga_assistant.cards import CatalogError, load_card_names
from mtga_assistant.ollama import answer_question
from mtga_assistant.state import GameState
from mtga_assistant.stream import StreamRecord, follow_records


MAX_VOICE_QUESTION_CHARS = 500
MAX_VOICE_ANSWER_CHARS = 600
MAX_SPEECH_OPTIONS = 32
MAX_SPEECH_STDOUT_BYTES = 65_536
_CULTURE = re.compile(r'[a-z]{2,3}-[A-Z]{2,4}\Z')


class VoiceUnavailableError(RuntimeError):
    """A local speech engine or device is unavailable."""


class VoiceTimeoutError(VoiceUnavailableError):
    """A bounded speech operation did not finish before its deadline."""


@dataclass(frozen=True)
class SpeechOptions:
    recognizers: tuple[str, ...]
    voices: tuple[str, ...]


@dataclass(frozen=True)
class VoiceAnswer:
    text: str
    generation: int
    revision: int


class VoiceStateUnavailableError(RuntimeError):
    """No reliable active public game snapshot is available for a question."""


class VoiceSessionBusyError(RuntimeError):
    """A voice question is already being answered."""


class LiveStateMonitor:
    """Reduce an explicitly selected log while retaining only its latest snapshot."""

    def __init__(self, log_path: Path, seat: int,
                 card_names: Mapping[int, str] | None = None, poll_seconds: float = 0.25,
                 stale_after_seconds: float = 120.0):
        if type(seat) is not int or seat not in (1, 2):
            raise ValueError('Choose seat 1 or 2 explicitly.')
        if (type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds)
                or poll_seconds <= 0):
            raise ValueError('Invalid log polling interval.')
        if (type(stale_after_seconds) not in (int, float) or
                not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0):
            raise ValueError('Invalid log freshness interval.')
        self.log_path = Path(log_path)
        self.seat = seat
        self._card_names = dict(card_names or {})
        self._poll_seconds = float(poll_seconds)
        self._stale_after_seconds = float(stale_after_seconds)
        self._state = GameState(seat)
        self._snapshot_lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = self._state.public_snapshot(self._card_names)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._source_identity: tuple[int, int] | None = None
        self._source_size = 0
        self._last_activity = 0.0
        self._initial_replay = True

    def start(self) -> None:
        try:
            stat = self.log_path.stat()
        except OSError:
            raise VoiceUnavailableError('Choose an existing Arena log file.') from None
        if not self.log_path.is_file():
            raise VoiceUnavailableError('Choose an existing Arena log file.')
        with self._snapshot_lock:
            if self._thread is not None and self._thread.is_alive():
                raise VoiceSessionBusyError('The live state monitor is already running.')
            self._source_identity = (stat.st_dev, stat.st_ino)
            self._source_size = stat.st_size
            age = max(0.0, time.time() - stat.st_mtime)
            self._last_activity = time.monotonic() - age
            self._initial_replay = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._follow, name='mtga-voice-state', daemon=True,
            )
            self._thread.start()

    def _publish(self) -> None:
        snapshot = self._state.public_snapshot(self._card_names)
        with self._snapshot_lock:
            self._snapshot = snapshot

    def _follow(self) -> None:
        records = follow_records(self.log_path, poll_seconds=self._poll_seconds)
        try:
            for record in records:
                if self._stop_event.is_set():
                    break
                self._refresh_source_activity()
                if record.kind == 'idle':
                    self._initial_replay = False
                    continue
                revision_before = self._state.revision
                self._state.consume_record(record, card_names=self._card_names)
                integrity = self._state.integrity()
                if (not self._initial_replay and record.kind == 'event' and
                        self._state.revision != revision_before and
                        integrity.get('status') in ('consistent', 'partial')):
                    with self._snapshot_lock:
                        self._last_activity = time.monotonic()
                # Publish a waiting snapshot immediately on a match/source reset,
                # and an uncertain one immediately after a parser diagnostic.
                self._publish()
        except (OSError, ValueError):
            # The reducer's public state reports uncertainty without retaining
            # raw exception details or source lines.
            self._state.invalidate('invalid_schema')
            self._publish()
        finally:
            records.close()

    def current_snapshot(self) -> dict[str, Any] | None:
        with self._snapshot_lock:
            snapshot = deepcopy(self._snapshot)
            if not isinstance(snapshot, dict):
                return snapshot
            integrity = snapshot.get('integrity')
            if not isinstance(integrity, dict) or integrity.get('status') not in ('consistent', 'partial'):
                return snapshot
            if time.monotonic() - self._last_activity <= self._stale_after_seconds:
                return snapshot
            integrity['status'] = 'stale'
            limitations = integrity.get('limitations')
            limitations = list(limitations) if isinstance(limitations, list) else []
            if 'stale_log' not in limitations:
                limitations.append('stale_log')
            integrity['limitations'] = sorted(limitations)
            return snapshot

    def _refresh_source_activity(self) -> None:
        try:
            stat = self.log_path.stat()
        except OSError:
            with self._snapshot_lock:
                self._last_activity = time.monotonic() - self._stale_after_seconds - 1.0
            return
        identity = (stat.st_dev, stat.st_ino)
        now = time.monotonic()
        with self._snapshot_lock:
            if self._source_identity != identity or stat.st_size < self._source_size:
                # A replacement or truncation starts an unconfirmed source. Its
                # existing bytes are replayed, but do not count as live activity.
                self._source_identity = identity
                self._source_size = stat.st_size
                self._last_activity = now - self._stale_after_seconds - 1.0
            elif stat.st_size > self._source_size:
                self._source_size = stat.st_size

    def stop(self, timeout_seconds: float = 2.0) -> None:
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds < 0):
            raise ValueError('Invalid monitor stop timeout.')
        self._stop_event.set()
        with self._snapshot_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_seconds)


class VoiceSessionController:
    """Coordinate one explicit question against the latest reliable public state."""

    def __init__(self, adapter: SpeechAdapter | None = None):
        self.adapter = adapter or SpeechAdapter()
        self._lock = threading.RLock()
        self._answer_lock = threading.Lock()
        self._monitor: LiveStateMonitor | None = None
        self._model: str | None = None
        self._culture: str | None = None
        self._voice_name: str | None = None
        self._generation = 0
        self._starting = False
        self._stopping = False
        self._stop_complete = threading.Event()
        self._stop_complete.set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active(self) -> bool:
        with self._lock:
            return self._monitor is not None

    @property
    def speech_enabled(self) -> bool:
        with self._lock:
            return self._monitor is not None and self._culture is not None

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def current_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            monitor = self._monitor
        snapshot = monitor.current_snapshot() if monitor is not None else None
        return deepcopy(snapshot)

    def start(self, log_path: Path, seat: int, model: str, culture: str | None,
              voice_name: str | None = None, cards_db: Path | None = None) -> None:
        if type(seat) is not int or seat not in (1, 2):
            raise ValueError('Choose seat 1 or 2 explicitly.')
        if (not isinstance(model, str) or not model.strip() or len(model) > 128
                or any(not ch.isprintable() for ch in model)):
            raise ValueError('Choose a valid local model.')
        if culture == '':
            culture = None
        if culture is not None and (not isinstance(culture, str) or not _CULTURE.fullmatch(culture)):
            raise VoiceUnavailableError(
                'Choose an installed recognition language, or use typed questions.'
            )
        if culture is None and voice_name is not None:
            raise VoiceUnavailableError('Choose typed questions only or select a recognition language.')
        with self._lock:
            if self._monitor is not None or self._starting or self._stopping:
                raise VoiceSessionBusyError('Stop the current voice session first.')
            start_generation = self._generation
            self._starting = True
        monitor: LiveStateMonitor | None = None
        try:
            if culture is not None:
                if not self.adapter.available():
                    raise VoiceUnavailableError(
                        'Speech capture is unavailable here; choose typed questions only.'
                    )
                options = self.adapter.options(
                    should_continue=lambda: self._start_current(start_generation),
                )
                if culture not in options.recognizers:
                    raise VoiceUnavailableError('Choose an installed recognition language.')
                if voice_name is not None and voice_name not in options.voices:
                    raise VoiceUnavailableError('Choose an installed speech voice.')
            card_names: Mapping[int, str] | None = None
            if cards_db is not None:
                try:
                    card_names = load_card_names(Path(cards_db))
                except CatalogError:
                    raise VoiceUnavailableError('The selected local card catalog could not be read.') from None
            monitor = LiveStateMonitor(Path(log_path), seat, card_names)
            monitor.start()
            with self._lock:
                cancelled = self._generation != start_generation or not self._starting
                if not cancelled:
                    self._generation += 1
                    self._model = model.strip()
                    self._culture = culture
                    self._voice_name = voice_name
                    self._monitor = monitor
                self._starting = False
            if cancelled:
                raise VoiceUnavailableError('The voice session start was cancelled.')
        except Exception:
            with self._lock:
                if self._generation == start_generation:
                    self._starting = False
            if monitor is not None and monitor is not self._monitor:
                monitor.stop()
            raise

    def listen_once(self) -> str | None:
        with self._lock:
            monitor, generation, culture = self._monitor, self._generation, self._culture
        if monitor is None:
            raise VoiceStateUnavailableError('Start a voice session first.')
        if culture is None:
            raise VoiceUnavailableError('Speech recognition is off; type a question instead.')
        transcript = self.adapter.listen_once(
            culture, should_continue=lambda: self._operation_current(monitor, generation),
        )
        with self._lock:
            if generation != self._generation or monitor is not self._monitor:
                return None
        return transcript

    def answer(self, question: str) -> VoiceAnswer | None:
        if not self._answer_lock.acquire(blocking=False):
            raise VoiceSessionBusyError('A voice question is already being answered.')
        try:
            with self._lock:
                monitor, generation, model = self._monitor, self._generation, self._model
            if monitor is None or model is None:
                raise VoiceStateUnavailableError('Start a voice session first.')
            snapshot = monitor.current_snapshot()
            integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
            status = integrity.get('status') if isinstance(integrity, dict) else None
            if status not in ('consistent', 'partial'):
                raise VoiceStateUnavailableError(
                    'A reliable active game snapshot is not available yet.'
                )
            revision = snapshot.get('revision')
            if type(revision) is not int:
                raise VoiceStateUnavailableError('The current game snapshot is unavailable.')
            answer = answer_question(snapshot, question, model)
            with self._lock:
                if generation != self._generation or monitor is not self._monitor:
                    return None
            latest = monitor.current_snapshot()
            if (not isinstance(latest, dict) or latest.get('revision') != revision or
                    latest.get('integrity', {}).get('status') not in ('consistent', 'partial')):
                return None
            return VoiceAnswer(answer, generation, revision)
        finally:
            self._answer_lock.release()

    def speak(self, answer: VoiceAnswer) -> None:
        if not isinstance(answer, VoiceAnswer) or not isinstance(answer.text, str):
            raise VoiceStateUnavailableError('The answer is no longer available.')
        with self._lock:
            monitor, generation, voice_name = self._monitor, self._generation, self._voice_name
            culture = self._culture
        if monitor is None or answer.generation != generation:
            raise VoiceStateUnavailableError('The answer is no longer available.')
        snapshot = monitor.current_snapshot()
        integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
        if (not isinstance(snapshot, dict) or snapshot.get('revision') != answer.revision or
                not isinstance(integrity, dict) or
                integrity.get('status') not in ('consistent', 'partial')):
            raise VoiceStateUnavailableError('The game state changed; ask again for a current answer.')
        with self._lock:
            if monitor is not self._monitor or generation != self._generation:
                raise VoiceStateUnavailableError('The voice session has stopped.')
        if culture is None:
            return
        self.adapter.speak(
            answer.text, voice_name,
            should_continue=lambda: self._answer_current(monitor, generation, answer.revision),
        )

    def _operation_current(self, monitor: LiveStateMonitor, generation: int) -> bool:
        with self._lock:
            return self._monitor is monitor and self._generation == generation

    def _start_current(self, generation: int) -> bool:
        with self._lock:
            return (self._generation == generation and self._starting and
                    not self._stopping)

    def _answer_current(self, monitor: LiveStateMonitor, generation: int, revision: int) -> bool:
        if not self._operation_current(monitor, generation):
            return False
        snapshot = monitor.current_snapshot()
        integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
        return (isinstance(snapshot, dict) and snapshot.get('revision') == revision and
                isinstance(integrity, dict) and
                integrity.get('status') in ('consistent', 'partial'))

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._stopping:
                completion = self._stop_complete
                monitor = None
                already_stopping = True
            else:
                completion = self._stop_complete
                completion.clear()
                self._stopping = True
                already_stopping = False
                monitor = self._monitor
                self._monitor = None
                self._starting = False
                self._model = None
                self._culture = None
                self._voice_name = None
                self._generation += 1

        if already_stopping:
            if wait:
                completion.wait()
            return

        def teardown() -> None:
            try:
                try:
                    self.adapter.stop()
                finally:
                    if monitor is not None:
                        monitor.stop()
            finally:
                with self._lock:
                    self._stopping = False
                completion.set()

        if wait:
            teardown()
        else:
            threading.Thread(target=teardown, name='mtga-voice-stop', daemon=True).start()


def _supports_windows_voice() -> bool:
    return os.name == 'nt'


_OPTIONS_SCRIPT = r'''
$null = Add-Type -AssemblyName System.Speech
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$recognizers = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
    ForEach-Object { $_.Culture.Name })
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
    @{ recognizers = $recognizers; voices = $voices } | ConvertTo-Json -Compress
} finally { $synth.Dispose() }
'''.strip()

_LISTEN_SCRIPT = r'''
$null = Add-Type -AssemblyName System.Speech
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = ConvertFrom-Json ([Console]::In.ReadToEnd())
$info = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
    Where-Object { $_.Culture.Name -eq $request.culture } | Select-Object -First 1
if ($null -eq $info) { exit 3 }
$recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine($info)
try {
    $recognizer.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
    $recognizer.InitialSilenceTimeout = [TimeSpan]::FromSeconds(4)
    $recognizer.BabbleTimeout = [TimeSpan]::FromSeconds(4)
    $recognizer.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.7)
    $recognizer.SetInputToDefaultAudioDevice()
    $result = $recognizer.Recognize([TimeSpan]::FromSeconds([double]$request.timeout_seconds))
    if ($null -eq $result) { @{ text = $null } | ConvertTo-Json -Compress }
    else { @{ text = $result.Text } | ConvertTo-Json -Compress }
} finally { $recognizer.Dispose() }
'''.strip()

_SPEAK_SCRIPT = r'''
$null = Add-Type -AssemblyName System.Speech
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = ConvertFrom-Json ([Console]::In.ReadToEnd())
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($request.voice_name) { $synth.SelectVoice([string]$request.voice_name) }
    $synth.SetOutputToDefaultAudioDevice()
    $synth.Speak([string]$request.text)
} finally { $synth.Dispose() }
'''.strip()


def _option_list(value: Any, *, cultures: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_SPEECH_OPTIONS:
        raise VoiceUnavailableError('Invalid local speech configuration.')
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128 or any(not ch.isprintable() for ch in item):
            raise VoiceUnavailableError('Invalid local speech configuration.')
        if cultures and not _CULTURE.fullmatch(item):
            raise VoiceUnavailableError('Invalid local speech configuration.')
        if item not in result:
            result.append(item)
    return tuple(result)


def _clean_text(value: Any, maximum: int, *, required: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('Invalid voice text.')
    cleaned = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in value).split())
    if required and not cleaned:
        raise ValueError('Invalid voice text.')
    return cleaned


class SpeechAdapter:
    """Run one bounded System.Speech operation through a fixed PowerShell script."""

    def __init__(self, powershell_executable: str = 'powershell.exe'):
        if (not isinstance(powershell_executable, str) or not powershell_executable.strip()
                or len(powershell_executable) > 260 or not powershell_executable.isprintable()):
            raise ValueError('Invalid local speech executable.')
        self.powershell_executable = powershell_executable
        self._options_cache: SpeechOptions | None = None
        self._lock = threading.RLock()
        self._active_process: subprocess.Popen[bytes] | None = None

    def available(self) -> bool:
        return _supports_windows_voice()

    def _run(self, script: str, *, input_value: dict[str, Any] | None,
             timeout_seconds: float,
             should_continue: Callable[[], bool] | None = None) -> bytes:
        if not self.available():
            raise VoiceUnavailableError('Voice is available only in the native Windows desktop app.')
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Invalid speech timeout.')
        input_bytes = (json.dumps(input_value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                       if input_value is not None else None)
        with self._lock:
            if should_continue is not None and not should_continue():
                raise VoiceUnavailableError('The local speech operation was cancelled.')
            if self._active_process is not None and self._active_process.poll() is None:
                raise VoiceUnavailableError('A speech operation is already running.')
            try:
                process = subprocess.Popen(
                    [self.powershell_executable, '-NoProfile', '-NonInteractive',
                     '-Command', script],
                    stdin=subprocess.PIPE if input_value is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except OSError:
                raise VoiceUnavailableError('Unable to start the local Windows speech engine.') from None
            self._active_process = process
        captured = bytearray()
        oversized = threading.Event()

        def drain_stdout() -> None:
            try:
                while chunk := process.stdout.read(8192):
                    if len(captured) + len(chunk) > MAX_SPEECH_STDOUT_BYTES:
                        oversized.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                    elif not oversized.is_set():
                        captured.extend(chunk)
            except (OSError, ValueError):
                return

        reader = threading.Thread(target=drain_stdout, name='mtga-speech-output', daemon=True)
        reader.start()
        try:
            try:
                if input_bytes is not None:
                    try:
                        process.stdin.write(input_bytes)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        process.stdin.close()
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._kill_and_reap(process)
                reader.join(1.0)
                raise VoiceTimeoutError('The local speech operation timed out.') from None
            reader.join(1.0)
            if reader.is_alive():
                self._kill_and_reap(process)
                reader.join(1.0)
            if process.returncode != 0 or oversized.is_set() or reader.is_alive():
                raise VoiceUnavailableError('The local Windows speech engine returned an invalid result.')
            return bytes(captured)
        except OSError:
            self._kill_and_reap(process)
            raise VoiceUnavailableError('Unable to complete the local Windows speech operation.') from None
        finally:
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            except OSError:
                pass
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

    @staticmethod
    def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def stop(self, timeout_seconds: float = 1.0) -> None:
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds < 0):
            raise ValueError('Invalid speech stop timeout.')
        with self._lock:
            process = self._active_process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def options(self, should_continue: Callable[[], bool] | None = None) -> SpeechOptions:
        if not self.available():
            raise VoiceUnavailableError('Voice is available only in the native Windows desktop app.')
        if should_continue is not None and not should_continue():
            raise VoiceUnavailableError('The local speech operation was cancelled.')
        if self._options_cache is not None:
            return self._options_cache
        raw = self._run(
            _OPTIONS_SCRIPT, input_value=None, timeout_seconds=8.0,
            should_continue=should_continue,
        )
        try:
            value = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError, RecursionError):
            raise VoiceUnavailableError('The local Windows speech configuration could not be read.') from None
        if not isinstance(value, dict) or set(value) != {'recognizers', 'voices'}:
            raise VoiceUnavailableError('The local Windows speech configuration could not be read.')
        options = SpeechOptions(
            recognizers=_option_list(value['recognizers'], cultures=True),
            voices=_option_list(value['voices'], cultures=False),
        )
        self._options_cache = options
        return options

    def listen_once(self, culture: str, timeout_seconds: float = 12.0,
                    should_continue: Callable[[], bool] | None = None) -> str | None:
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 1.0 <= timeout_seconds <= 12.0):
            raise ValueError('Invalid speech timeout.')
        if should_continue is not None and not should_continue():
            raise VoiceUnavailableError('The local speech operation was cancelled.')
        options = self.options()
        if not isinstance(culture, str) or culture not in options.recognizers:
            raise VoiceUnavailableError('Choose an installed recognition language.')
        hard_timeout = max(3.0, timeout_seconds)
        raw = self._run(_LISTEN_SCRIPT, input_value={
            'culture': culture, 'timeout_seconds': timeout_seconds,
        }, timeout_seconds=hard_timeout, should_continue=should_continue)
        try:
            value = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError, RecursionError):
            raise VoiceUnavailableError('The local speech engine returned an invalid transcript.') from None
        if not isinstance(value, dict) or set(value) != {'text'}:
            raise VoiceUnavailableError('The local speech engine returned an invalid transcript.')
        if value['text'] is None:
            return None
        try:
            transcript = _clean_text(value['text'], MAX_VOICE_QUESTION_CHARS, required=True)
        except ValueError:
            raise VoiceUnavailableError('The recognized question was empty or too long.') from None
        return transcript or None

    def speak(self, text: str, voice_name: str | None = None,
              timeout_seconds: float = 15.0,
              should_continue: Callable[[], bool] | None = None) -> None:
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 1.0 <= timeout_seconds <= 30.0):
            raise ValueError('Invalid speech timeout.')
        speech = _clean_text(text, MAX_VOICE_ANSWER_CHARS, required=True)
        if should_continue is not None and not should_continue():
            raise VoiceUnavailableError('The local speech operation was cancelled.')
        options = self.options()
        if voice_name is not None and (not isinstance(voice_name, str) or voice_name not in options.voices):
            raise VoiceUnavailableError('Choose an installed speech voice.')
        self._run(_SPEAK_SCRIPT, input_value={
            'text': speech, 'voice_name': voice_name,
        }, timeout_seconds=timeout_seconds, should_continue=should_continue)
