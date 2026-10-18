"""Local desktop workflow helpers; Tk is loaded only when the UI is launched."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import codecs
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError

from mtga_assistant.decks import FORMATS, SUGGESTION_GOALS
from mtga_assistant.opening import OPENING_REVIEW_LIFECYCLES
from mtga_assistant.paths import default_arena_log
from mtga_assistant.ollama import (
    list_installed_models, unload_model, DECISION_INFERENCE_SECONDS, DECISION_DISPLAY_SECONDS,
)
from mtga_assistant.preferences import load_match_model, save_match_model
from mtga_assistant.voice import (
    SpeechAdapter, SpeechOptions, VoiceAnswer, VoiceSessionBusyError,
    VoiceSessionController, VoiceStateUnavailableError, VoiceTimeoutError,
    VoiceUnavailableError,
)


class CommandInputError(ValueError):
    """A desktop workflow form is missing or contains an unsupported value."""


OUTPUT_LIMIT = 1_048_576
OUTPUT_TRUNCATION_MARKER = '\n[Earlier output discarded to keep display bounded.]\n'
DESKTOP_EVENT_PREFIX = '@@MTGA_UI_EVENT@@'
MAX_DESKTOP_EVENT_BYTES = 8192
TYPED_ONLY = 'Typed questions only'


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CommandInputError('Complete the required fields before running this workflow.')
    return value


def _optional(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key, '')
    if not isinstance(value, str):
        raise CommandInputError('Invalid workflow field.')
    return value if value.strip() else None


def build_command(
    workflow: str,
    values: Mapping[str, str],
    python_executable: str = sys.executable,
) -> list[str]:
    """Build one local CLI argv, keeping all user-provided paths as single args."""
    if not isinstance(python_executable, str) or not python_executable.strip():
        raise CommandInputError('Python executable is unavailable.')
    args = [python_executable, '-m', 'mtga_assistant']

    if workflow in ('watch', 'replay', 'review'):
        seat_value = _required(values, 'seat').strip()
        seat = ('auto' if seat_value.casefold() in ('auto', 'auto-detect') else seat_value)
        allowed_seats = ('auto', '1', '2') if workflow == 'watch' else ('1', '2')
        if seat not in allowed_seats:
            if workflow in ('replay', 'review') and seat == 'auto':
                raise CommandInputError('Replay and review need a confirmed seat; choose seat 1 or 2.')
            raise CommandInputError('Choose automatic detection or a valid seat.')
        args.extend([workflow, '--seat', seat, '--log', _required(values, 'log')])
        cards_db = _optional(values, 'cards_db')
        model = _optional(values, 'model')
        opponent_data = _optional(values, 'opponent_data') if workflow in ('watch', 'replay') else None
        if opponent_data and not cards_db:
            raise CommandInputError('Choose a local card catalog for opponent estimates.')
        if cards_db:
            args.extend(['--cards-db', cards_db])
        if model:
            args.extend(['--model', model.strip()])
        if opponent_data:
            args.extend(['--opponent-data', opponent_data])
        if workflow == 'watch':
            args.append('--desktop-events')
    elif workflow in ('model-warm', 'model-unload'):
        action = 'warm' if workflow == 'model-warm' else 'unload'
        args.extend(['model', action, '--model', _required(values, 'model').strip()])
    elif workflow == 'deck-audit':
        deck_format = _required(values, 'format').strip()
        if deck_format not in FORMATS:
            raise CommandInputError('Choose a supported format.')
        args.extend([
            'deck', 'audit', '--list', _required(values, 'deck_list'),
            '--format', deck_format, '--cards-db', _required(values, 'cards_db'),
        ])
        collection = _optional(values, 'collection_json')
        if collection:
            args.extend(['--collection-json', collection])
    elif workflow == 'deck-suggest':
        deck_format = _required(values, 'format').strip()
        goal = _required(values, 'goal').strip()
        if deck_format not in FORMATS:
            raise CommandInputError('Choose a supported format.')
        if goal not in SUGGESTION_GOALS:
            raise CommandInputError('Choose a supported suggestion goal.')
        args.extend([
            'deck', 'suggest', '--list', _required(values, 'deck_list'),
            '--format', deck_format, '--cards-db', _required(values, 'cards_db'),
            '--collection-json', _required(values, 'collection_json'), '--goal', goal,
        ])
    elif workflow == 'draft-rank':
        args.extend([
            'draft', 'rank', '--state', _required(values, 'state'),
            '--cards-db', _required(values, 'cards_db'),
        ])
        stats_db = _optional(values, 'stats_db')
        if stats_db:
            args.extend(['--stats-db', stats_db])
    elif workflow == 'draft-stats-import':
        args.extend([
            'draft', 'stats', 'import', '--source', _required(values, 'source'),
            '--database', _required(values, 'database'), '--set', _required(values, 'set_code'),
            '--event-type', _required(values, 'event_type'),
        ])
    elif workflow == 'cards-import':
        args.extend([
            'cards', 'import', '--source', _required(values, 'source'),
            '--database', _required(values, 'database'),
        ])
    elif workflow == 'opponent-import':
        args.extend([
            'opponent', 'import', '--manifest', _required(values, 'manifest'),
            '--cards-db', _required(values, 'cards_db'), '--output', _required(values, 'output'),
        ])
    else:
        raise CommandInputError('Choose a supported workflow.')
    return args


def bounded_output(current: str, incoming: str, limit: int = OUTPUT_LIMIT) -> tuple[str, bool]:
    """Append output while retaining only a recent tail within ``limit`` chars."""
    if not isinstance(current, str) or not isinstance(incoming, str) or limit < 1:
        raise ValueError('Invalid output buffer')
    combined = current + incoming
    if len(combined) <= limit:
        return combined, False
    if limit <= len(OUTPUT_TRUNCATION_MARKER):
        return combined[-limit:], True
    tail_size = limit - len(OUTPUT_TRUNCATION_MARKER)
    return OUTPUT_TRUNCATION_MARKER + combined[-tail_size:], True


def update_ai_panel(current: str, status: str, detail: str,
                    heading: str = 'COMPARISON') -> str:
    """Replace the obsolete pending paragraph; never leave contradictory status."""
    separator = '\n\nAI '
    cut = current.rfind(separator)
    base = current[:cut] if cut >= 0 else current
    # A new game-state summary resets the panel via _set_match_summary.
    return (base.rstrip() + '\n\nAI ' + heading + ' ' + status + '\n' +
            detail.rstrip() + '\nRecommendations are unverified; '
            'opponent hidden cards remain unknown.')


@dataclass(frozen=True)
class RunnerMessage:
    kind: str
    text: str = ''
    exit_code: int | None = None


def _validated_desktop_event(raw: object) -> dict | None:
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_DESKTOP_EVENT_BYTES:
        return None
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get('type')
    revision = event.get('revision')
    if kind == 'match_summary':
        expected = {'type', 'revision', 'turn', 'phase', 'integrity', 'my_life',
                    'opp_life', 'hand', 'battlefield', 'opp_battlefield',
                    'arena_choice_present', 'decision_kind', 'choices', 'notes', 'lifecycle'}
        if set(event) != expected or type(revision) is not int or revision < 0:
            return None
        for key in ('turn', 'my_life', 'opp_life', 'hand', 'battlefield', 'opp_battlefield'):
            val = event[key]
            if val is not None and (type(val) is not int or abs(val) > 1_000_000):
                return None
        if (not isinstance(event['lifecycle'], str) or len(event['lifecycle']) > 40 or
                not isinstance(event['phase'], str) or len(event['phase']) > 90 or
                type(event['arena_choice_present']) is not bool or
                event['integrity'] not in ('waiting', 'partial', 'consistent', 'inconsistent') or
                (event['decision_kind'] is not None and
                 (not isinstance(event['decision_kind'], str) or len(event['decision_kind']) > 90))):
            return None
        for key, limit, maxlen in [('choices', 12, 150), ('notes', 6, 220)]:
            values = event[key]
            if not isinstance(values, list) or len(values) > limit or any(
                    not isinstance(item, str) or len(item) > maxlen or
                    any(not (ch.isprintable() or ch == '\n') for ch in item) for item in values):
                return None
        return event
    if kind == 'state':
        if (set(event) != {'type', 'revision'} or type(revision) is not int or revision < 0):
            return None
        return {'type': 'state', 'revision': revision}
    if kind == 'decision_progress':
        if (set(event) != {'type', 'revision', 'status'} or
                type(revision) is not int or revision < 0 or
                event.get('status') not in {
                    'pending', 'skipped', 'review_pending', 'review_skipped',
                }):
            return None
        return {'type': 'decision_progress', 'revision': revision, 'status': event['status']}
    if kind == 'model_status':
        status = event.get('status')
        if set(event) != {'type', 'status'} or not isinstance(status, str) or status not in {
                'warming', 'ready', 'unavailable', 'unloaded'}:
            return None
        return {'type': 'model_status', 'status': status}
    fields = {
        'type', 'revision', 'response_kind', 'observed_at', 'inference_deadline', 'deadline_at',
        'completed_at', 'elapsed_ms', 'legal_status', 'text', 'error_code',
    }
    if kind != 'decision_result' or set(event) != fields:
        return None
    if type(revision) is not int or revision < 0:
        return None
    response_kind = event.get('response_kind')
    if response_kind not in {'comparison', 'state_review'}:
        return None
    observed = event.get('observed_at')
    inference_deadline = event.get('inference_deadline')
    deadline = event.get('deadline_at')
    completed = event.get('completed_at')
    try:
        times_valid = all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) for value in (observed, inference_deadline, deadline, completed)
        )
    except OverflowError:
        times_valid = False
    if not times_valid:
        return None
    if (not math.isclose(inference_deadline, observed + DECISION_INFERENCE_SECONDS, abs_tol=0.001) or
            not math.isclose(deadline, observed + DECISION_DISPLAY_SECONDS, abs_tol=0.001)):
        return None
    elapsed = event.get('elapsed_ms')
    text = event.get('text')
    error = event.get('error_code')
    if (type(elapsed) is not int or not 0 <= elapsed <= 60_000 or
            event.get('legal_status') != 'unverified' or not isinstance(text, str) or
            len(text) > 4000 or any(not (char.isprintable() or char in '\n\t') for char in text) or
            (error is not None and (not isinstance(error, str) or error not in {
                'timeout', 'unavailable', 'invalid_response', 'expired', 'prompt_too_large'}))):
        return None
    if error is None and (not text.strip() or completed >= inference_deadline):
        return None
    return {
        'type': 'decision_result', 'revision': revision,
        'response_kind': response_kind,
        'observed_at': float(observed), 'inference_deadline': float(inference_deadline),
        'deadline_at': float(deadline), 'completed_at': float(completed),
        'elapsed_ms': elapsed, 'legal_status': 'unverified', 'text': text,
        'error_code': error,
    }


class DesktopEventDecoder:
    """Separate bounded control lines from ordinary child output."""

    def __init__(self):
        self._buffer = ''
        self._ordinary_line = False
        self._discarding_event = False

    def _control(self, line: str) -> RunnerMessage | None:
        if not line.startswith(DESKTOP_EVENT_PREFIX):
            return None
        event = _validated_desktop_event(line[len(DESKTOP_EVENT_PREFIX):])
        if event is None:
            return None
        return RunnerMessage('ui_event', json.dumps(event, separators=(',', ':')))

    def feed(self, text: str, *, final: bool = False) -> list[RunnerMessage]:
        if not isinstance(text, str):
            raise TypeError('Child output must be text')
        self._buffer += text
        messages: list[RunnerMessage] = []
        while self._buffer:
            if self._discarding_event:
                newline = self._buffer.find('\n')
                if newline < 0:
                    self._buffer = ''
                    break
                self._buffer = self._buffer[newline + 1:]
                self._discarding_event = False
                self._ordinary_line = False
                continue
            if self._ordinary_line:
                newline = self._buffer.find('\n')
                if newline >= 0:
                    completed_line = self._buffer[:newline + 1]
                    while len(completed_line) > 8192:
                        messages.append(RunnerMessage('output', completed_line[:8192]))
                        completed_line = completed_line[8192:]
                    if completed_line:
                        messages.append(RunnerMessage('output', completed_line))
                    self._buffer = self._buffer[newline + 1:]
                    self._ordinary_line = False
                    continue
                if len(self._buffer) > 8192:
                    messages.append(RunnerMessage('output', self._buffer[:8192]))
                    self._buffer = self._buffer[8192:]
                break
            newline = self._buffer.find('\n')
            if newline >= 0:
                line = self._buffer[:newline].removesuffix('\r')
                self._buffer = self._buffer[newline + 1:]
                control = self._control(line)
                if control is not None:
                    messages.append(control)
                elif not line.startswith(DESKTOP_EVENT_PREFIX):
                    messages.append(RunnerMessage('output', line + '\n'))
                continue
            if self._buffer.startswith(DESKTOP_EVENT_PREFIX):
                if len(self._buffer.encode('utf-8')) > MAX_DESKTOP_EVENT_BYTES:
                    self._buffer = ''
                    self._discarding_event = True
                break
            if DESKTOP_EVENT_PREFIX.startswith(self._buffer):
                break
            self._ordinary_line = True
        if final:
            if self._buffer and self._ordinary_line:
                messages.append(RunnerMessage('output', self._buffer))
            elif self._buffer and not self._buffer.startswith(DESKTOP_EVENT_PREFIX) and \
                    not DESKTOP_EVENT_PREFIX.startswith(self._buffer):
                messages.append(RunnerMessage('output', self._buffer))
            elif self._buffer.startswith(DESKTOP_EVENT_PREFIX):
                control = self._control(self._buffer)
                if control is not None:
                    messages.append(control)
            self._buffer = ''
            self._ordinary_line = False
            self._discarding_event = False
        return messages


class CommandRunner:
    """Run one local CLI child and relay bounded output without touching Tk."""

    def __init__(self, max_messages: int = 256):
        if not isinstance(max_messages, int) or max_messages < 3:
            raise ValueError('Message queue must hold at least three messages')
        self._max_messages = max_messages
        self._messages: queue.Queue[RunnerMessage] = queue.Queue(maxsize=max_messages)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _enqueue(self, message: RunnerMessage) -> None:
        with self._lock:
            try:
                self._messages.put_nowait(message)
                return
            except queue.Full:
                pass
            queued = []
            while True:
                try:
                    queued.append(self._messages.get_nowait())
                except queue.Empty:
                    break
            queued = [item for item in queued if item.kind != 'truncated']
            keep_count = max(0, self._max_messages - 2)
            if keep_count:
                queued = queued[-keep_count:]
            else:
                queued = []
            rebuilt = queued + [RunnerMessage(
                'truncated', 'Earlier process output was dropped to keep the queue bounded.'
            ), message]
            for item in rebuilt[-self._max_messages:]:
                self._messages.put_nowait(item)

    def start(self, args: Sequence[str]) -> None:
        if isinstance(args, (str, bytes)) or not args or any(not isinstance(arg, str) for arg in args):
            raise ValueError('A command argument list is required')
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError('A local workflow is already running')
            while True:
                try:
                    self._messages.get_nowait()
                except queue.Empty:
                    break
            try:
                child_env = os.environ.copy()
                child_env['PYTHONIOENCODING'] = 'utf-8:replace'
                process = subprocess.Popen(
                    list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, shell=False, bufsize=-1, env=child_env,
                )
            except OSError:
                self._process = None
                self._reader = None
                self._enqueue(RunnerMessage('error', 'Unable to start the local workflow.'))
                return
            self._process = process
            self._reader = threading.Thread(
                target=self._read_output, args=(process,), name='mtga-desktop-output', daemon=True,
            )
            self._reader.start()

    def _read_output(self, process: subprocess.Popen[bytes]) -> None:
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        event_decoder = DesktopEventDecoder()
        try:
            if process.stdout is not None:
                while chunk := process.stdout.read1(8192):
                    text = decoder.decode(chunk)
                    for message in event_decoder.feed(text):
                        self._enqueue(message)
                tail = decoder.decode(b'', final=True)
                for message in event_decoder.feed(tail, final=True):
                    self._enqueue(message)
        except (OSError, ValueError):
            self._enqueue(RunnerMessage('error', 'Unable to read local workflow output.'))
        finally:
            if process.stdout is not None:
                process.stdout.close()
        exit_code = process.wait()
        self._enqueue(RunnerMessage('finished', exit_code=exit_code))

    def poll(self) -> list[RunnerMessage]:
        messages = []
        with self._lock:
            while True:
                try:
                    messages.append(self._messages.get_nowait())
                except queue.Empty:
                    break
        return messages

    def stop(self, timeout_seconds: float = 2.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError('Stop timeout must be finite and nonnegative')
        with self._lock:
            process, reader = self._process, self._reader
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
            process.wait()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=timeout_seconds + 1.0)


_WORKFLOW_FIELDS = {
    'watch': {
        'log': 'match_log', 'seat': 'match_seat', 'cards_db': 'match_cards_db',
        'model': 'match_model', 'opponent_data': 'match_opponent_data',
    },
    'replay': {
        'log': 'match_log', 'seat': 'match_seat', 'cards_db': 'match_cards_db',
        'model': 'match_model', 'opponent_data': 'match_opponent_data',
    },
    'review': {'log': 'match_log', 'seat': 'match_seat', 'cards_db': 'match_cards_db', 'model': 'match_model'},
    'model-warm': {'model': 'match_model'},
    'model-unload': {'model': 'match_model'},
    'deck-audit': {
        'deck_list': 'deck_list', 'format': 'deck_format', 'cards_db': 'deck_cards_db',
        'collection_json': 'deck_collection',
    },
    'deck-suggest': {
        'deck_list': 'deck_list', 'format': 'deck_format', 'cards_db': 'deck_cards_db',
        'collection_json': 'deck_collection', 'goal': 'deck_goal',
    },
    'draft-rank': {
        'state': 'draft_state', 'cards_db': 'draft_cards_db', 'stats_db': 'draft_rank_stats_db',
    },
    'draft-stats-import': {
        'source': 'draft_stats_source', 'database': 'draft_stats_destination',
        'set_code': 'draft_set_code', 'event_type': 'draft_event_type',
    },
    'cards-import': {'source': 'catalog_source', 'database': 'catalog_destination'},
    'opponent-import': {
        'manifest': 'opponent_manifest', 'cards_db': 'opponent_cards_db',
        'output': 'opponent_destination',
    },
}


def start_workflow(
    workflow: str,
    values: Mapping[str, str],
    runner: CommandRunner,
    python_executable: str = sys.executable,
) -> list[str]:
    """Validate and start one supported workflow through the child runner."""
    command = build_command(workflow, values, python_executable)
    runner.start(command)
    return command


def build_voice_session(values: Mapping[str, str]) -> tuple[str, int, str, str | None, str | None, str | None]:
    """Validate explicit, ephemeral inputs for one local voice session."""
    log_path = _required(values, 'log')
    seat_text = _required(values, 'seat').strip()
    if seat_text not in ('1', '2'):
        raise CommandInputError('Choose seat 1 or 2 explicitly.')
    model = _required(values, 'model').strip()
    culture_value = values.get('culture', '')
    if not isinstance(culture_value, str):
        raise CommandInputError('Choose an installed recognition language or typed questions.')
    culture_value = culture_value.strip()
    culture = None if culture_value in ('', TYPED_ONLY) else culture_value
    if len(model) > 128 or any(not ch.isprintable() for ch in model):
        raise CommandInputError('Choose a valid local model.')
    if culture is not None and not re.fullmatch(r'[a-z]{2,3}-[A-Z]{2,4}', culture):
        raise CommandInputError('Choose an installed recognition language.')
    voice_name = _optional(values, 'voice')
    if culture is None:
        voice_name = None
    if voice_name is not None and (len(voice_name) > 128 or
                                   any(not ch.isprintable() for ch in voice_name)):
        raise CommandInputError('Choose an installed speech voice.')
    return log_path, int(seat_text), model, culture, voice_name, _optional(values, 'cards_db')


def voice_platform_status(adapter: SpeechAdapter | None = None) -> str:
    adapter = adapter or SpeechAdapter()
    if not adapter.available():
        return 'Speech capture and spoken answers need native Windows speech; typed questions work here.'
    return 'Load the installed speech languages and voices to begin.'


def voice_state_status(snapshot: object) -> str:
    integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
    state = integrity.get('status') if isinstance(integrity, dict) else None
    if state == 'consistent':
        return 'Live game state is ready for a question.'
    if state == 'partial':
        return 'Game state is partial; any answer will include uncertainty.'
    if state == 'stale':
        return 'The selected log is stale; live advice is paused until new log data arrives.'
    if state == 'uncertain':
        return 'The log has a continuity or data problem; live advice is paused.'
    if state == 'ended':
        return 'This game has ended; live advice is paused.'
    return 'Waiting for live game data in the selected log.'


def validate_voice_question(value: object) -> str:
    if not isinstance(value, str):
        raise CommandInputError('Enter a question before Ask.')
    question = value.strip()
    if not question:
        raise CommandInputError('Enter a question before Ask.')
    if len(question) > 500:
        raise CommandInputError('A voice question can contain at most 500 characters.')
    return question


@dataclass(frozen=True)
class VoiceUiMessage:
    kind: str
    text: str = ''
    value: object | None = None
    generation: int | None = None
    revision: int | None = None


class AssistantWindow:
    """Tk window exposing existing local commands without owning game logic."""

    def __init__(self, root, tk, ttk, filedialog):
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.runner = CommandRunner()
        self.voice_controller = VoiceSessionController()
        self.voice_messages: queue.Queue[VoiceUiMessage] = queue.Queue(maxsize=32)
        self._voice_queue_lock = threading.Lock()
        self._model_list_messages: queue.Queue[tuple[str, ...] | None] = queue.Queue(maxsize=1)
        self._model_list_loading = False
        self._voice_active = False
        self._voice_operation_busy = False
        self._voice_stopping = False
        self._last_voice_revision = None
        self._voice_supported = self.voice_controller.adapter.available()
        self.variables = {}
        self._form_rows = {}
        self._browse_specs = {}
        self._choice_widgets = {}
        self._run_buttons = []
        self._voice_buttons = {}
        self._busy = False
        self._closing = False
        self._user_stopped = False
        self._active_workflow = None
        self._active_model = None
        self._latest_snapshot_revision = None
        self._last_match_summary_revision = None

        root.title('MTGA Assistant  |  Arena Companion')
        root.geometry('1230x825')
        root.minsize(940, 630)
        root.configure(bg='#0B1018')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._pending_watch_values = None
        self._last_model_error = ''
        self._configure_theme()

        outer = ttk.Frame(root, padding=(20, 16, 20, 16))
        outer.pack(fill='both', expand=True)
        heading = ttk.Frame(outer)
        heading.pack(fill='x', pady=(0, 14))
        ttk.Label(heading, text='◆  ARENA COMPANION', style='Hero.TLabel').pack(side='left')
        ttk.Label(heading, text='READ ONLY  •  LOCAL AI  •  STRATEGY UNVERIFIED',
                  style='Muted.TLabel').pack(side='right', padx=6)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill='both', expand=True)
        match_tab = ttk.Frame(notebook, padding=12)
        deck_tab = ttk.Frame(notebook, padding=14)
        draft_tab = ttk.Frame(notebook, padding=14)
        data_tab = ttk.Frame(notebook, padding=14)
        voice_tab = ttk.Frame(notebook, padding=14)
        diagnostics_tab = ttk.Frame(notebook, padding=12)
        notebook.add(match_tab, text='  LIVE MATCH  ')
        notebook.add(deck_tab, text='  DECKS  ')
        notebook.add(draft_tab, text='  DRAFT  ')
        notebook.add(data_tab, text='  DATA  ')
        notebook.add(voice_tab, text='  VOICE  ')
        notebook.add(diagnostics_tab, text='  DIAGNOSTICS  ')

        match_tab.columnconfigure(0, weight=0, minsize=410)
        match_tab.columnconfigure(1, weight=1)
        match_tab.rowconfigure(0, weight=1)
        setup_holder = ttk.Frame(match_tab)
        setup_holder.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        setup = ttk.LabelFrame(setup_holder, text='  CONNECTION & MODEL  ', padding=12)
        setup.pack(fill='x')
        ttk.Label(setup, text='1  •  CONNECT ARENA', style='Section.TLabel').grid(
            row=self._next_row(setup), column=0, columnspan=3, sticky='w', pady=(0, 8))
        self._add_path(setup, 'match_log', 'Player.log',
                       (('Arena log', '*.log'), ('All files', '*.*')))
        self.variables['match_log'].set(str(default_arena_log()))
        self._add_choice(setup, 'match_seat', 'Your seat', ('Auto-detect', '1', '2'))
        self.variables['match_seat'].set('Auto-detect')
        ttk.Separator(setup, orient='horizontal').grid(
            row=self._next_row(setup), column=0, columnspan=3, sticky='ew', pady=12)
        ttk.Label(setup, text='2  •  CHOOSE LOCAL AI', style='Section.TLabel').grid(
            row=self._next_row(setup), column=0, columnspan=3, sticky='w', pady=(0, 8))
        self._add_model_picker(setup, 'Ollama')
        self._add_path(setup, 'match_cards_db', 'Optional catalog override (auto-detected by default)',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_path(setup, 'match_opponent_data', 'Opponent data (optional)',
                       (('JSON files', '*.json'), ('All files', '*.*')))
        self.model_status = tk.StringVar(master=root, value='Model: checking local Ollama…')
        ttk.Label(setup, textvariable=self.model_status, style='Model.TLabel',
                  wraplength=370).grid(row=self._next_row(setup), column=0,
                                       columnspan=3, sticky='ew', padx=4, pady=(10, 4))
        self._add_actions(setup, (('▶  START WATCH', 'watch'),
                                  ('PRELOAD', 'model-warm'), ('RELEASE', 'model-unload')))
        ttk.Label(setup_holder, text='MODEL OPTIONAL  •  WITHOUT AI, RULE-BASED OBSERVATIONS STILL WORK',
                  style='Muted.TLabel', wraplength=380).pack(anchor='w', pady=12)
        ttk.Label(setup_holder, text='The assistant never sends clicks, keys, or moves to Arena.',
                  style='Muted.TLabel', wraplength=380).pack(anchor='w')

        dashboard = ttk.Frame(match_tab)
        dashboard.grid(row=0, column=1, sticky='nsew')
        dashboard.columnconfigure(0, weight=1)
        dashboard.rowconfigure(3, weight=1)
        ttk.Label(dashboard, text='LIVE MATCH OVERVIEW', style='Section.TLabel').grid(
            row=0, column=0, sticky='w', pady=(2, 10))
        self.match_phase = tk.StringVar(master=root, value='NO MATCH DETECTED   •   WAITING FOR LOG DATA')
        ttk.Label(dashboard, textvariable=self.match_phase, style='Hud.TLabel',
                  wraplength=600).grid(row=1, column=0, sticky='ew', pady=(0, 9))
        self.match_stats = tk.StringVar(master=root, value='YOUR LIFE   —      OPPONENT LIFE   —\nHAND   —      BATTLEFIELD   —')
        ttk.Label(dashboard, textvariable=self.match_stats, style='Stats.TLabel',
                  justify='left').grid(row=2, column=0, sticky='ew', pady=(0, 12))
        recommendation = ttk.LabelFrame(dashboard, text='  CURRENT CHOICE & COACHING  ', padding=12)
        recommendation.grid(row=3, column=0, sticky='nsew')
        recommendation.columnconfigure(0, weight=1)
        recommendation.rowconfigure(0, weight=1)
        self.match_advice = tk.Text(recommendation, wrap='word', height=14, state='disabled',
                                    bg='#131C2B', fg='#E7EAF1', insertbackground='#E7EAF1',
                                    font=('Segoe UI', 11), relief='flat', padx=14, pady=12,
                                    borderwidth=0, highlightthickness=0)
        self.match_advice.grid(row=0, column=0, sticky='nsew')
        advice_scroll = ttk.Scrollbar(recommendation, command=self.match_advice.yview)
        advice_scroll.grid(row=0, column=1, sticky='ns')
        self.match_advice.configure(yscrollcommand=advice_scroll.set)
        self._set_match_advice('Waiting for a live game. Start Watch to see Arena-presented\n'
                               'choices and evidence-based observations here.\n\n'
                               'A recommendation is not proof that an action is legal.')

        self._add_path(deck_tab, 'deck_list', 'Arena deck export',
                       (('Text files', '*.txt'), ('All files', '*.*')))
        self._add_choice(deck_tab, 'deck_format', 'Format', tuple(sorted(FORMATS)))
        self._add_path(deck_tab, 'deck_cards_db', 'Local card catalog',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_path(deck_tab, 'deck_collection', 'Collection JSON (optional for audit)',
                       (('JSON files', '*.json'), ('All files', '*.*')))
        self._add_choice(deck_tab, 'deck_goal', 'Suggestion goal', tuple(sorted(SUGGESTION_GOALS)))
        self._add_actions(deck_tab, (('Audit deck', 'deck-audit'), ('Suggest candidates', 'deck-suggest')))

        self._add_path(draft_tab, 'draft_state', 'Current pack/pool JSON',
                       (('JSON files', '*.json'), ('All files', '*.*')))
        self._add_path(draft_tab, 'draft_cards_db', 'Local card catalog',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_path(draft_tab, 'draft_rank_stats_db', 'Draft statistics database (optional)',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_actions(draft_tab, (('Rank current pack', 'draft-rank'),))
        self._add_path(draft_tab, 'draft_stats_source', '17Lands CSV or gzip CSV',
                       (('CSV files', '*.csv *.csv.gz'), ('All files', '*.*')))
        self._add_path(draft_tab, 'draft_stats_destination', 'Save draft statistics database',
                       (('SQLite database', '*.sqlite3'), ('All files', '*.*')), save=True)
        self._add_text(draft_tab, 'draft_set_code', 'Set code')
        self._add_text(draft_tab, 'draft_event_type', 'Event type')
        self._add_actions(draft_tab, (('Import draft statistics', 'draft-stats-import'),))

        self._add_path(data_tab, 'catalog_source', 'Card JSONL or gzip JSONL',
                       (('JSONL files', '*.jsonl *.jsonl.gz'), ('All files', '*.*')))
        self._add_path(data_tab, 'catalog_destination', 'Save local card catalog',
                       (('SQLite database', '*.sqlite3'), ('All files', '*.*')), save=True)
        self._add_actions(data_tab, (('Import card catalog', 'cards-import'),))
        self._add_path(data_tab, 'opponent_manifest', 'Opponent reference manifest JSON',
                       (('JSON files', '*.json'), ('All files', '*.*')))
        self._add_path(data_tab, 'opponent_cards_db', 'Local card catalog for reference import',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_path(data_tab, 'opponent_destination', 'Save opponent reference JSON',
                       (('JSON files', '*.json'), ('All files', '*.*')), save=True)
        self._add_actions(data_tab, (('Import opponent references', 'opponent-import'),))

        self._add_path(voice_tab, 'voice_log', 'Arena log',
                       (('Arena log', '*.log'), ('All files', '*.*')))
        self._add_choice(voice_tab, 'voice_seat', 'Your seat', ('1', '2'))
        self._add_path(voice_tab, 'voice_cards_db', 'Local card catalog (optional)',
                       (('SQLite database', '*.sqlite3 *.db'), ('All files', '*.*')))
        self._add_text(voice_tab, 'voice_model', 'Local Ollama model')
        self._add_choice(voice_tab, 'voice_culture', 'Recognition language', (TYPED_ONLY,))
        self.variables['voice_culture'].set(TYPED_ONLY)
        self._add_choice(voice_tab, 'voice_name', 'Spoken voice', ())
        voice_actions = self.ttk.Frame(voice_tab)
        voice_actions.grid(row=self._next_row(voice_tab), column=0, columnspan=3,
                           sticky='w', padx=4, pady=(8, 4))
        for name, label, action in (
            ('options','Load speech options',self.load_voice_options),
            ('start','Start session',self.start_voice),
            ('listen','Listen for one question',self.listen_voice),
            ('ask','Ask',self.ask_voice), ('stop','Stop',self.stop_voice),
            ('clear','Clear',self.clear_voice)):
            widget = self.ttk.Button(voice_actions, text=label, command=action)
            widget.pack(side='left', padx=(0, 6))
            self._voice_buttons[name] = widget
        self.voice_status = tk.StringVar(master=root, value=voice_platform_status(self.voice_controller.adapter))
        self.ttk.Label(voice_tab, textvariable=self.voice_status, wraplength=780).grid(
            row=self._next_row(voice_tab), column=0, columnspan=3, sticky='w', padx=4, pady=4)
        transcript_row = self._next_row(voice_tab)
        self.ttk.Label(voice_tab, text='Transcript (edit before Ask)').grid(
            row=transcript_row, column=0, columnspan=3, sticky='w', padx=4, pady=(8, 2))
        self.voice_transcript = self.tk.Text(voice_tab, wrap='word', height=3,
                                            bg='#131C2B', fg='#E7EAF1', insertbackground='#E7EAF1')
        self.voice_transcript.grid(row=transcript_row + 1, column=0, columnspan=3,
                                   sticky='ew', padx=4, pady=(0, 6))
        self.voice_transcript.bind('<<Modified>>', self._on_voice_transcript_modified)
        answer_row = transcript_row + 2
        self.ttk.Label(voice_tab, text='Answer').grid(
            row=answer_row, column=0, columnspan=3, sticky='w', padx=4, pady=(4, 2))
        self.voice_answer = self.tk.Text(voice_tab, wrap='word', height=4, state='disabled',
                                        bg='#131C2B', fg='#E7EAF1')
        self.voice_answer.grid(row=answer_row + 1, column=0, columnspan=3,
                               sticky='ew', padx=4, pady=(0, 4))
        voice_tab.columnconfigure(1, weight=1)

        output_frame = ttk.LabelFrame(diagnostics_tab, text='  ENGINE EVENT LOG  ', padding=8)
        output_frame.pack(fill='both', expand=True)
        output_bar = ttk.Frame(output_frame)
        output_bar.pack(fill='x')
        self.status = tk.StringVar(master=root, value='Select a model (optional) and start Watch.')
        ttk.Label(output_bar, textvariable=self.status, style='Muted.TLabel').pack(
            side='left', fill='x', expand=True)
        ttk.Button(output_bar, text='Clear', command=self.clear_output).pack(side='right', padx=(4, 0))
        text_frame = ttk.Frame(output_frame)
        text_frame.pack(fill='both', expand=True, pady=(6, 0))
        self.output = tk.Text(text_frame, wrap='word', height=18, state='disabled',
                              bg='#0C1320', fg='#CFD8E7', relief='flat', borderwidth=0,
                              highlightthickness=0, font=('Consolas', 10))
        scrollbar = ttk.Scrollbar(text_frame, orient='vertical', command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(12, 0))
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel',
                  wraplength=1020).pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(footer, text='■  STOP', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=(12, 0))
        self._sync_voice_controls()
        self.root.after(100, self._poll)
        self.root.after(150, self.refresh_model_list)

    def _configure_theme(self):
        style = self.ttk.Style(self.root)
        style.theme_use('clam')
        bg, panel, text, muted, gold = '#0B1018', '#131C2B', '#E7EAF1', '#9CAABF', '#F2C77D'
        style.configure('.', background=bg, foreground=text, font=('Segoe UI', 10))
        style.configure('TFrame', background=bg)
        style.configure('TLabel', background=bg, foreground=text)
        style.configure('Hero.TLabel', font=('Segoe UI Semibold', 18), foreground=gold)
        style.configure('Section.TLabel', font=('Segoe UI Semibold', 10), foreground=gold)
        style.configure('Muted.TLabel', font=('Segoe UI', 9), foreground=muted)
        style.configure('Hud.TLabel', font=('Segoe UI Semibold', 12), foreground=gold)
        style.configure('Stats.TLabel', font=('Consolas', 12), foreground=text)
        style.configure('Model.TLabel', font=('Segoe UI Semibold', 10), foreground='#A4E5C7')
        style.configure('TLabelframe', background=bg, bordercolor='#364456', borderwidth=1)
        style.configure('TLabelframe.Label', background=bg, foreground=gold,
                        font=('Segoe UI Semibold', 10))
        style.configure('TNotebook', background=bg, borderwidth=0)
        style.configure('TNotebook.Tab', background='#1B2534', foreground=muted, padding=(14, 10))
        style.map('TNotebook.Tab', background=[('selected', '#2A394D')],
                  foreground=[('selected', gold)])
        style.configure('TButton', padding=(12, 8), background='#26394E',
                        foreground=text, bordercolor='#3A5066', font=('Segoe UI Semibold', 9))
        style.map('TButton', background=[('active', '#385571'), ('disabled', '#182330')],
                  foreground=[('disabled', '#647287')])
        style.configure('TEntry', fieldbackground=panel, foreground=text,
                        insertcolor=text, bordercolor='#364456', padding=6)
        style.configure('TCombobox', fieldbackground=panel, background=panel,
                        foreground=text, arrowcolor=gold, bordercolor='#364456', padding=6)
        style.map('TCombobox', fieldbackground=[('readonly', panel)],
                  foreground=[('readonly', text)])

    def _set_match_advice(self, value):
        self.match_advice.configure(state='normal')
        self.match_advice.delete('1.0', 'end')
        self.match_advice.insert('1.0', value)
        self.match_advice.configure(state='disabled')

    def _set_match_summary(self, event):
        new_revision = event['revision'] != self._last_match_summary_revision
        self._last_match_summary_revision = event['revision']
        turn = '?' if event['turn'] is None else str(event['turn'])
        phase = event['phase'] or 'Unknown phase'
        self.match_phase.set(f'{event["lifecycle"]}   •   TURN {turn}   •   {phase}   •   {event["integrity"].upper()}')
        def shown(val):
            return '—' if val is None else str(val)
        self.match_stats.set(
            f'YOUR LIFE    {shown(event["my_life"]):<8} OPPONENT    {shown(event["opp_life"])}\n'
            f'HAND         {shown(event["hand"]):<8} BATTLEFIELD  {shown(event["battlefield"])}  '
            f'(OPP {shown(event["opp_battlefield"])})'
        )
        choices = event['choices']
        notes = event['notes']
        sections = []
        if event['decision_kind']:
            sections.append('ARENA-PRESENTED CHOICE  /  ' + event['decision_kind'].upper())
            sections.append('\n'.join(f'  {i}. {line}' for i, line in enumerate(choices, 1))
                            if choices else 'No supported choices extracted.')
        elif event['arena_choice_present']:
            sections.append('ARENA CHOICE DETAILS UNAVAILABLE\n'
                            'Arena requested a choice, but its options could not be safely analyzed.')
        else:
            sections.append('NO ACTIVE ARENA CHOICE')
        if notes:
            sections.append('RULE-BASED OBSERVATIONS\n' + '\n'.join('• ' + note for note in notes))
        if any('Unknown visible ' in choice or 'Card #' in choice for choice in choices):
            if not self.variables['match_cards_db'].get().strip():
                sections.append('CARD IDENTITIES NOT RESOLVED\n'
                                'Automatic Arena database discovery is enabled. '
                                'Check that the local Raw_CardDatabase is accessible; '
                                'some cards may not have a complete instance mapping.')
            else:
                sections.append('CARD IDENTITIES INCOMPLETE\n'
                                'Some objects lack a matching visible card ID or catalog entry. '
                                'Check the catalog and Diagnostics.')
        if not self._active_model:
            ai_status = 'AI COMPARISON OFF\nNo Ollama model is selected; only rule-based observations are running.'
        elif event['lifecycle'] in OPENING_REVIEW_LIFECYCLES:
            ai_status = ('AI OPENING-HAND REVIEW\nThe model may review supported visible-hand evidence; '
                         'unresolved cards stay unknown, and it will not create or rank an Arena choice.')
        elif not event['decision_kind']:
            ai_status = 'AI COMPARISON IDLE\nWaiting for a current Arena-presented decision.'
        else:
            ai_status = ('AI COMPARISON PENDING\nWaiting for the selected Ollama model. '
                         'The decision budget is 8 seconds; late/stale replies are discarded.')
        sections.append(ai_status +
                        '\nRecommendations are unverified; opponent hidden cards remain unknown.')
        if new_revision:
            self._set_match_advice('\n\n'.join(sections))

    def _next_row(self, frame) -> int:
        row = self._form_rows.get(frame, 0)
        self._form_rows[frame] = row + 1
        return row

    def _add_path(self, frame, key, label, filetypes, save=False):
        row = self._next_row(frame)
        variable = self.tk.StringVar(master=self.root, value='')
        self.variables[key] = variable
        self._browse_specs[key] = (save, filetypes)
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
        self.ttk.Entry(frame, textvariable=variable, width=32).grid(
            row=row, column=1, sticky='ew', padx=4, pady=4,
        )
        self.ttk.Button(frame, text='Browse…', command=lambda field=key: self._browse(field)).grid(
            row=row, column=2, padx=4, pady=4,
        )
        frame.columnconfigure(1, weight=1)

    def _add_text(self, frame, key, label):
        row = self._next_row(frame)
        initial_value = load_match_model() if key == 'match_model' else ''
        variable = self.tk.StringVar(master=self.root, value=initial_value)
        self.variables[key] = variable
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
        self.ttk.Entry(frame, textvariable=variable, width=32).grid(
            row=row, column=1, columnspan=2, sticky='ew', padx=4, pady=4,
        )
        frame.columnconfigure(1, weight=1)

    def _add_model_picker(self, frame, label):
        row = self._next_row(frame)
        variable = self.tk.StringVar(master=self.root, value=load_match_model())
        self.variables['match_model'] = variable
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
        picker = self.ttk.Combobox(frame, textvariable=variable, values=(), width=32)
        picker.grid(row=row, column=1, sticky='ew', padx=4, pady=4)
        self._choice_widgets['match_model'] = picker
        self._model_refresh_button = self.ttk.Button(
            frame, text='Refresh models', command=self.refresh_model_list,
        )
        self._model_refresh_button.grid(row=row, column=2, padx=4, pady=4)
        frame.columnconfigure(1, weight=1)

    def refresh_model_list(self):
        if self._closing or self._model_list_loading:
            return
        self._model_list_loading = True
        self._model_refresh_button.configure(state='disabled')
        self.status.set('Loading installed local models…')

        def fetch_models():
            try:
                models = tuple(list_installed_models())
            except (HTTPException, OSError, RuntimeError, URLError, ValueError,
                    UnicodeError, RecursionError):
                models = None
            self._model_list_messages.put(models)

        threading.Thread(target=fetch_models, name='mtga-model-list', daemon=True).start()

    def _apply_model_list_result(self, models):
        self._model_list_loading = False
        self._model_refresh_button.configure(state='normal')
        picker = self._choice_widgets['match_model']
        picker.configure(values=() if models is None else models)
        if models is None:
            self.status.set('Ollama unreachable. Start the service or configure MTGA_OLLAMA_URL (see README).')
            self.model_status.set('Ollama offline — deterministic Watch is available.')
        elif not models:
            self.status.set('No installed Ollama models found. Run: ollama list')
            self.model_status.set('No models installed — deterministic Watch is available.')
        else:
            selected = self.variables['match_model'].get().strip()
            if selected and selected not in models:
                self.status.set('Saved model is not installed. Select an available model.')
                self.model_status.set('Model not installed — select one from dropdown.')
            else:
                self.status.set(f'Found {len(models)} installed local model(s). Start Watch to prepare the selected model.')
                self.model_status.set('Model selected — Start Watch will load it automatically.' if selected else 'No model selected — deterministic Watch available.')

    def _remember_ready_model(self):
        variable = self.variables.get('match_model')
        if variable is not None:
            save_match_model(variable.get())

    def _add_choice(self, frame, key, label, choices):
        row = self._next_row(frame)
        variable = self.tk.StringVar(master=self.root, value='')
        self.variables[key] = variable
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=4)
        widget = self.ttk.Combobox(frame, textvariable=variable, values=choices, state='readonly')
        widget.grid(
            row=row, column=1, columnspan=2, sticky='ew', padx=4, pady=4,
        )
        self._choice_widgets[key] = widget
        frame.columnconfigure(1, weight=1)

    def _add_actions(self, frame, actions):
        row = self._next_row(frame)
        actions_frame = self.ttk.Frame(frame)
        actions_frame.grid(row=row, column=0, columnspan=3, sticky='w', padx=4, pady=(8, 4))
        for label, workflow in actions:
            button = self.ttk.Button(
                actions_frame, text=label,
                command=lambda action=workflow: self.run_workflow(action),
            )
            button.pack(side='left', padx=(0, 6))
            self._run_buttons.append(button)

    def _browse(self, key):
        save, filetypes = self._browse_specs[key]
        dialog = self.filedialog.asksaveasfilename if save else self.filedialog.askopenfilename
        path = dialog(title='Choose a local file', filetypes=filetypes)
        if path:
            self.variables[key].set(path)

    def _values(self, workflow):
        mapping = _WORKFLOW_FIELDS[workflow]
        return {name: self.variables[key].get() for name, key in mapping.items()}

    def run_workflow(self, workflow):
        if self._voice_active or self._voice_operation_busy:
            self.status.set('Stop the voice session before starting another workflow.')
            return
        values = self._values(workflow)
        try:
            if workflow == 'watch':
                # Validate the eventual command before launching the preload subprocess.
                build_command('watch', values)
                if not Path(values['log']).is_file():
                    raise CommandInputError('Arena Player.log was not found. Enable Detailed Logs and check the path.')
                model = values.get('model', '').strip()
                if model:
                    start_workflow('model-warm', {'model': model}, self.runner)
                    self._pending_watch_values = dict(values)
                    self._active_workflow = 'model-warm'
                    self._active_model = model
                    self.model_status.set('Loading model into Ollama…')
                    self.status.set('Loading selected model before starting Watch…')
                else:
                    start_workflow('watch', values, self.runner)
                    self._active_workflow = 'watch'
                    self._active_model = None
                    self.model_status.set('No model selected — rule-based coaching only.')
                    self.status.set('Watching Arena Player.log read-only (without Ollama).')
            else:
                start_workflow(workflow, values, self.runner)
                self._active_workflow = workflow
                self._active_model = values.get('model') if workflow in (
                    'model-warm', 'model-unload'
                ) else None
        except CommandInputError as exc:
            self.status.set(str(exc))
            return
        except RuntimeError:
            self.status.set('A local workflow is already running.')
            return
        self._user_stopped = False
        self._last_model_error = ''
        self._set_busy(True)
        if workflow == 'watch':
            self.clear_output()
            self._latest_snapshot_revision = None
            self._last_match_summary_revision = None
            self.match_phase.set('WAITING FOR LIVE MATCH')
            self.match_stats.set('YOUR LIFE   —      OPPONENT LIFE   —\nHAND   —      BATTLEFIELD   —')
            self._set_match_advice('Waiting for Arena game state.\n\n'
                                   'The AI will compare only choices present in the log.')
        elif workflow == 'model-warm':
            self.model_status.set('Loading model into Ollama…')
            self.status.set('Preparing selected Ollama model…')
        elif workflow == 'model-unload':
            self.model_status.set('Releasing model…')
            self.status.set('Releasing model from Ollama…')
        else:
            self.status.set('Running local workflow…')

    def _set_busy(self, busy):
        self._busy = busy
        voice_busy = (self._voice_active or self._voice_operation_busy or
                      getattr(self, '_voice_stopping', False) or
                      getattr(self.voice_controller, 'stopping', False))
        for button in self._run_buttons:
            button.configure(state='disabled' if (busy or voice_busy) else 'normal')
        self.stop_button.configure(state='normal' if (busy or self._voice_active or
                                                       self._voice_operation_busy) else 'disabled')
        self._sync_voice_controls()

    def _sync_voice_controls(self):
        if not hasattr(self, '_voice_buttons'):
            return
        stopping = getattr(self, '_voice_stopping', False) or getattr(
            self.voice_controller, 'stopping', False,
        )
        busy = self._busy or self._voice_active or self._voice_operation_busy or stopping
        if 'options' in self._voice_buttons:
            self._voice_buttons['options'].configure(
                state='normal' if not busy and self._voice_supported else 'disabled')
        if 'start' in self._voice_buttons:
            self._voice_buttons['start'].configure(
                state='normal' if not busy else 'disabled')
        if 'listen' in self._voice_buttons:
            self._voice_buttons['listen'].configure(state=(
                'normal' if self._voice_active and not self._voice_operation_busy and
                self.voice_controller.speech_enabled else 'disabled'
            ))
        if 'ask' in self._voice_buttons:
            self._voice_buttons['ask'].configure(
                state='normal' if self._voice_active and not self._voice_operation_busy else 'disabled')
        if 'stop' in self._voice_buttons:
            self._voice_buttons['stop'].configure(
                state='normal' if ((self._voice_active or self._voice_operation_busy) and
                                   not stopping) else 'disabled')
        if 'clear' in self._voice_buttons:
            self._voice_buttons['clear'].configure(state='normal')

    def _voice_values(self):
        return {
            'log': self.variables['voice_log'].get(),
            'seat': self.variables['voice_seat'].get(),
            'cards_db': self.variables['voice_cards_db'].get(),
            'model': self.variables['voice_model'].get(),
            'culture': self.variables['voice_culture'].get(),
            'voice': self.variables['voice_name'].get(),
        }

    def _enqueue_voice_message(self, message: VoiceUiMessage):
        if not isinstance(message, VoiceUiMessage):
            raise TypeError('Invalid voice UI message.')
        with self._voice_queue_lock:
            try:
                self.voice_messages.put_nowait(message)
                return
            except queue.Full:
                try:
                    self.voice_messages.get_nowait()
                except queue.Empty:
                    pass
                self.voice_messages.put_nowait(message)

    def _drain_voice_messages(self):
        messages = []
        with self._voice_queue_lock:
            while True:
                try:
                    messages.append(self.voice_messages.get_nowait())
                except queue.Empty:
                    return messages

    def _start_voice_task(self, operation, action):
        if self._voice_operation_busy:
            self.voice_status.set('A voice operation is already running.')
            return
        self._voice_operation_busy = True
        self._sync_voice_controls()
        self._set_busy(self._busy)
        started_generation = self.voice_controller.generation

        def work():
            try:
                value = action()
            except (CommandInputError, VoiceSessionBusyError, VoiceStateUnavailableError,
                    VoiceTimeoutError, VoiceUnavailableError, ValueError) as exc:
                self._enqueue_voice_message(VoiceUiMessage(
                    'operation_error', str(exc)[:240], operation, started_generation,
                ))
            except Exception:
                self._enqueue_voice_message(VoiceUiMessage(
                    'operation_error', 'The local voice operation failed.', operation,
                    started_generation,
                ))
            else:
                result_generation = (self.voice_controller.generation if operation == 'start'
                                     else started_generation)
                self._enqueue_voice_message(VoiceUiMessage(
                    'operation_done', operation, value, result_generation,
                ))

        threading.Thread(target=work, name='mtga-voice-ui', daemon=True).start()

    def load_voice_options(self):
        self._start_voice_task('options', self.voice_controller.adapter.options)

    def start_voice(self):
        if self._busy:
            self.voice_status.set('Stop the current workflow before starting voice.')
            return
        try:
            log_path, seat, model, culture, voice_name, cards_db = build_voice_session(
                self._voice_values(),
            )
        except CommandInputError as exc:
            self.voice_status.set(str(exc))
            return
        self._start_voice_task('start', lambda: self.voice_controller.start(
            Path(log_path), seat, model, culture, voice_name,
            Path(cards_db) if cards_db else None,
        ))

    def listen_voice(self):
        if not self._voice_active:
            self.voice_status.set('Start a voice session first.')
            return
        self._start_voice_task('listen', self.voice_controller.listen_once)

    def ask_voice(self):
        if not self._voice_active:
            self.voice_status.set('Start a voice session first.')
            return
        try:
            question = validate_voice_question(self.voice_transcript.get('1.0', 'end-1c'))
        except CommandInputError as exc:
            self.voice_status.set(str(exc))
            return

        def answer_and_speak():
            answer = self.voice_controller.answer(question)
            if answer is None:
                return 'The game state changed; check it and ask again.'
            self._enqueue_voice_message(VoiceUiMessage(
                'answer', answer.text, generation=answer.generation,
                revision=answer.revision,
            ))
            self.voice_controller.speak(answer)
            return 'Answer ready.' if self.voice_controller.speech_enabled else 'Answer ready as text.'

        self._start_voice_task('answering', answer_and_speak)

    def _on_voice_transcript_modified(self, _event=None):
        widget = self.voice_transcript
        if not widget.edit_modified():
            return
        text = widget.get('1.0', 'end-1c')
        if len(text) > 500:
            widget.delete('1.0', 'end')
            widget.insert('1.0', text[:500])
        widget.edit_modified(False)

    def clear_voice(self):
        self.voice_transcript.delete('1.0', 'end')
        self.voice_answer.configure(state='normal')
        self.voice_answer.delete('1.0', 'end')
        self.voice_answer.configure(state='disabled')

    def stop_voice(self):
        self.voice_controller.stop(wait=False)
        self._voice_stopping = True
        self._voice_active = False
        self._last_voice_revision = None
        self.clear_voice()
        if hasattr(self, 'voice_status'):
            self.voice_status.set('Stopping voice session…')
        self._set_busy(self._busy)

    def _apply_voice_message(self, message: VoiceUiMessage):
        if message.kind == 'operation_done':
            operation, value = message.text, message.value
            self._voice_operation_busy = False
            if (message.generation is not None and
                    message.generation != self.voice_controller.generation):
                self._set_busy(self._busy)
                return
            if operation == 'options' and isinstance(value, SpeechOptions):
                cultures, voices = value.recognizers, value.voices
                self._choice_widgets['voice_culture'].configure(values=(TYPED_ONLY, *cultures))
                self._choice_widgets['voice_name'].configure(values=voices)
                if cultures:
                    self.variables['voice_culture'].set('en-US' if 'en-US' in cultures else cultures[0])
                if voices:
                    self.variables['voice_name'].set(voices[0])
                self.voice_status.set('Speech options ready; choose a log, seat and local model.')
            elif operation == 'start':
                self._voice_active = True
                self._last_voice_revision = None
                session_mode = ('Speech enabled.' if self.voice_controller.speech_enabled
                                else 'Type questions into the transcript box.')
                self.voice_status.set(f'Session started. {session_mode} Waiting for supported game state.')
            elif operation == 'listen':
                if not self._voice_active:
                    pass
                elif isinstance(value, str) and value:
                    self.voice_transcript.delete('1.0', 'end')
                    self.voice_transcript.insert('1.0', value)
                    self.voice_status.set('Review or edit the transcript, then choose Ask.')
                else:
                    self.voice_status.set('No speech was recognized. You can type a question.')
            elif operation == 'answering' and isinstance(value, str) and self._voice_active:
                self.voice_status.set(value)
            self._set_busy(self._busy)
        elif message.kind == 'operation_error':
            self._voice_operation_busy = False
            if (message.generation is not None and
                    message.generation != self.voice_controller.generation):
                self._set_busy(self._busy)
                return
            if message.value == 'start':
                self._voice_active = False
            self.voice_status.set(message.text or 'The local voice operation failed.')
            self._set_busy(self._busy)
        elif message.kind == 'answer':
            if (not self._voice_active or
                    (message.generation is not None and
                     message.generation != self.voice_controller.generation)):
                return
            snapshot = self.voice_controller.current_snapshot()
            integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
            if (not isinstance(snapshot, dict) or snapshot.get('revision') != message.revision or
                    not isinstance(integrity, dict) or
                    integrity.get('status') not in ('consistent', 'partial')):
                return
            self.voice_answer.configure(state='normal')
            self.voice_answer.delete('1.0', 'end')
            self.voice_answer.insert('1.0', message.text)
            self.voice_answer.configure(state='disabled')

    def _apply_status(self, message):
        if message.kind in ('output', 'truncated'):
            self._handle_output_text(message.text)
        elif message.kind == 'ui_event':
            try:
                event = json.loads(message.text)
            except (ValueError, TypeError):
                return
            self._apply_ui_event(event)
        elif message.kind == 'error':
            self.status.set(message.text)
            self._pending_watch_values = None
            self._active_workflow = None
            self._active_model = None
            self._set_busy(False)
        elif message.kind == 'finished':
            # Watch requested with a model: preload -> verify -> start watcher.
            if self._active_workflow == 'model-warm' and self._pending_watch_values is not None:
                pending = self._pending_watch_values
                self._pending_watch_values = None
                if message.exit_code == 0 and not self._user_stopped:
                    try:
                        start_workflow('watch', pending, self.runner)
                    except (CommandInputError, RuntimeError, OSError) as exc:
                        self._set_busy(False)
                        self.model_status.set('Model loaded; Watch could not start.')
                        self.status.set('Watch could not start: ' + str(exc)[:180])
                        self._active_workflow = None
                        self._active_model = None
                        return
                    self._active_workflow = 'watch'
                    self._active_model = pending.get('model')
                    self.model_status.set('Ready — resident in Ollama')
                    self._remember_ready_model()
                    self.status.set('Model ready. Following Player.log read-only…')
                    self._set_busy(True)
                    return
                self._set_busy(False)
                self._active_workflow = None
                self._active_model = None
                self.model_status.set('Model failed to load')
                self.status.set(self._last_model_error or
                                'Ollama model preload failed. See Diagnostics for details.')
                return
            self._set_busy(False)
            if (self._user_stopped and self._active_workflow == 'model-unload' and
                    message.exit_code != 0):
                self.status.set(
                    'Watch stopped, but model release was not confirmed. Close Ollama before restarting Arena.'
                )
            elif self._user_stopped:
                self.status.set('Stopped.')
            else:
                self.status.set(f'Finished (exit code {message.exit_code}).')
            self._active_workflow = None
            self._active_model = None

    def _append_output(self, incoming):
        current = self.output.get('1.0', 'end-1c')
        updated, _truncated = bounded_output(current, incoming)
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('end', updated)
        self.output.see('end')
        self.output.configure(state='disabled')

    def _handle_output_text(self, text):
        self._append_output(text)
        model_status = getattr(self, 'model_status', None)
        if model_status is None:
            return
        for line in text.splitlines():
            if line.startswith('Warming '):
                model_status.set('Warming')
            elif line.startswith('Ready:'):
                model_status.set('Ready')
                self._remember_ready_model()
            elif line.startswith('Unloaded '):
                model_status.set('Unavailable')
            elif line.startswith('Ollama error:'):
                model_status.set('Load failed — see Diagnostics')
                self._last_model_error = line[:400]
                self.status.set(line[:400])
            elif 'model is not ready' in line.lower():
                model_status.set('Unavailable')

    def _apply_ui_event(self, event):
        if not isinstance(event, dict):
            return
        if event.get('type') == 'state':
            validated = _validated_desktop_event(json.dumps(event))
            if validated is None:
                return
            revision = validated['revision']
            latest = self._latest_snapshot_revision
            if latest is None or revision > latest:
                self._latest_snapshot_revision = revision
            return
        if event.get('type') == 'match_summary':
            validated = _validated_desktop_event(json.dumps(event))
            if (validated is not None and
                    (self._latest_snapshot_revision is None or
                     validated['revision'] >= self._latest_snapshot_revision)):
                self._latest_snapshot_revision = validated['revision']
                self._set_match_summary(validated)
            return
        if event.get('type') == 'decision_progress':
            validated = _validated_desktop_event(json.dumps(event))
            if (validated is None or
                    validated['revision'] != self._latest_snapshot_revision):
                return
            if validated['status'] == 'review_pending':
                current = self.match_advice.get('1.0', 'end-1c')
                self._set_match_advice(update_ai_panel(
                    current, 'PENDING',
                    'Reviewing the visible opening-hand state. No Arena choice is being ranked.',
                    heading='OPENING-HAND REVIEW'))
                self.status.set('Local model is reviewing the visible opening hand.')
            elif validated['status'] == 'review_skipped':
                current = self.match_advice.get('1.0', 'end-1c')
                self._set_match_advice(update_ai_panel(
                    current, 'SKIPPED',
                    'Opening-hand state or visible card evidence is incomplete; no model request was sent.',
                    heading='OPENING-HAND REVIEW'))
                self.status.set('Opening-hand model review skipped; required evidence is incomplete.')
            elif validated['status'] == 'skipped':
                current = self.match_advice.get('1.0', 'end-1c')
                self._set_match_advice(update_ai_panel(
                    current, 'SKIPPED',
                    'This Arena choice lacks a complete supported evidence set; no model request was sent.'))
                self.status.set('AI comparison skipped; the choice evidence was incomplete.')
            return
        if event.get('type') == 'model_status':
            validated = _validated_desktop_event(json.dumps(event))
            if validated is None:
                return
            label = {
                'warming': 'Warming', 'ready': 'Ready',
                'unavailable': ('No AI model — rules only' if not self._active_model else 'Unavailable'),
                'unloaded': 'Unavailable',
            }[validated['status']]
            self.model_status.set(label)
            if validated['status'] == 'ready':
                self._remember_ready_model()
            return
        validated = _validated_desktop_event(json.dumps(event))
        if validated is None:
            return
        latest = self._latest_snapshot_revision
        if validated['revision'] != latest:
            self.status.set('Decision response belongs to an older game state; not displayed.')
            return
        if validated['error_code'] is not None:
            heading = ('OPENING-HAND REVIEW'
                       if validated['response_kind'] == 'state_review' else 'COMPARISON')
            if validated['error_code'] == 'timeout':
                self.model_status.set('Model response exceeded 8-second decision budget')
            elif validated['error_code'] == 'unavailable':
                self.model_status.set('Model inference failed — see Diagnostics')
            if time.monotonic() >= validated['deadline_at']:
                self.status.set(
                    'Opening-hand review expired; check the current hand.'
                    if validated['response_kind'] == 'state_review' else
                    'Decision response expired; check the current game state.')
            elif validated['error_code'] == 'prompt_too_large':
                self.status.set('Model request skipped because its evidence was too large; deterministic evidence remains available.')
            else:
                self.status.set('No current model response: ' + validated['error_code'] + '.')
            self._set_match_advice(update_ai_panel(
                self.match_advice.get('1.0', 'end-1c'), 'UNAVAILABLE',
                validated['error_code'].replace('_', ' ') +
                f" ({validated['elapsed_ms']} ms).", heading=heading))
            return
        if (time.monotonic() >= validated['deadline_at'] or
                validated['completed_at'] >= validated['inference_deadline']):
            self.status.set(
                'Opening-hand review arrived too late; check the current hand.'
                if validated['response_kind'] == 'state_review' else
                'Decision response arrived too late; check the current game state.')
            self._set_match_advice(update_ai_panel(
                self.match_advice.get('1.0', 'end-1c'), 'EXPIRED',
                'Strategy was not shown because the game state may have changed.',
                heading=('OPENING-HAND REVIEW'
                         if validated['response_kind'] == 'state_review' else 'COMPARISON')))
            return
        self._append_output(
            validated['text'] + f"\nModel response time: {validated['elapsed_ms']} ms.\n",
        )
        current = self.match_advice.get('1.0', 'end-1c')
        is_state_review = validated['response_kind'] == 'state_review'
        self._set_match_advice(update_ai_panel(
            current, 'COMPLETE', validated['text'],
            heading='OPENING-HAND REVIEW' if is_state_review else 'COMPARISON'))
        self.model_status.set('Ready')
        self.status.set('Opening-hand state review ready; strategy is unverified.'
                        if is_state_review else
                        'Decision response ready; strategy is unverified.')

    def _poll(self):
        if self._closing:
            if self._shutdown_done.is_set():
                self.root.destroy()
            else:
                self.root.after(50, self._poll)
            return
        output_chunks = []
        for message in self.runner.poll():
            if message.kind in ('output', 'truncated'):
                output_chunks.append(message.text)
            elif message.kind == 'ui_event':
                if output_chunks:
                    self._handle_output_text(''.join(output_chunks))
                    output_chunks.clear()
                self._apply_status(message)
            else:
                if output_chunks:
                    self._handle_output_text(''.join(output_chunks))
                    output_chunks.clear()
                self._apply_status(message)
        if output_chunks:
            self._handle_output_text(''.join(output_chunks))
        while True:
            try:
                model_list_result = self._model_list_messages.get_nowait()
            except queue.Empty:
                break
            self._apply_model_list_result(model_list_result)
        for message in self._drain_voice_messages():
            self._apply_voice_message(message)
        if (getattr(self, '_voice_active', False) and
                not getattr(self, '_voice_operation_busy', False)):
            snapshot = self.voice_controller.current_snapshot()
            revision = snapshot.get('revision') if isinstance(snapshot, dict) else None
            integrity = snapshot.get('integrity') if isinstance(snapshot, dict) else None
            status = integrity.get('status') if isinstance(integrity, dict) else None
            limitations = tuple(integrity.get('limitations', ())) if isinstance(integrity, dict) else ()
            state_key = (revision, status, limitations)
            if state_key != self._last_voice_revision:
                self.voice_status.set(voice_state_status(snapshot))
                self._last_voice_revision = state_key
        if (getattr(self, '_voice_stopping', False) and
                not getattr(self.voice_controller, 'stopping', False)):
            self._voice_stopping = False
            self.voice_status.set('Voice session stopped.')
            self._set_busy(self._busy)
        self.root.after(100, self._poll)

    def clear_output(self):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.configure(state='disabled')

    def stop(self):
        if self._voice_active or self._voice_operation_busy:
            self.stop_voice()
        self._pending_watch_values = None
        if self.runner.active:
            workflow = self._active_workflow
            model = self._active_model
            self._user_stopped = True
            self.status.set('Stopping local workflow…')
            self.runner.stop()
            if workflow in ('watch', 'model-warm') and model:
                self.model_status.set('Unavailable')
                try:
                    start_workflow('model-unload', {'model': model}, self.runner)
                except (CommandInputError, OSError, RuntimeError, ValueError):
                    self._active_workflow = None
                    self._active_model = None
                    self._set_busy(False)
                    self.status.set(
                        'Watch stopped, but model release could not be started. Close Ollama before restarting Arena.'
                    )
                else:
                    self._active_workflow = 'model-unload'
                    self._active_model = model
                    self._set_busy(True)
                    self.status.set('Watch stopped; releasing local model memory…')

    def close(self):
        if self._closing:
            return
        self._closing = True
        self._pending_watch_values = None
        self._voice_active = False
        self._last_voice_revision = None
        self.clear_voice()
        self._shutdown_done = threading.Event()
        model_to_release = (
            self._active_model
            if self._active_workflow in ('watch', 'model-warm', 'model-unload') else None
        )

        def shutdown():
            try:
                self.voice_controller.stop(wait=True)
                self.runner.stop()
                if model_to_release:
                    try:
                        unload_model(model_to_release, timeout=5.0)
                    except (HTTPException, OSError, RuntimeError, URLError, ValueError,
                            UnicodeError, RecursionError):
                        pass
            finally:
                self._shutdown_done.set()

        threading.Thread(target=shutdown, name='mtga-desktop-shutdown', daemon=True).start()
        self.root.after(50, self._poll)


def launch_gui() -> int:
    """Compatibility entry point; the UI target is the Arena game overlay."""
    from mtga_assistant.overlay import launch_overlay
    return launch_overlay()
