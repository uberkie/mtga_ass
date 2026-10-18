"""Read-only Windows overlay for current MTG Arena match state."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

from mtga_assistant.desktop import (
    CommandInputError, CommandRunner, RunnerMessage, _validated_desktop_event,
    build_command, start_workflow,
)
from mtga_assistant.ollama import list_installed_models, list_loaded_models
from mtga_assistant.paths import default_arena_log
from mtga_assistant.preferences import load_match_model, save_match_model


class OverlayUnavailableError(RuntimeError):
    """The native Windows overlay could not be created."""


@dataclass(frozen=True)
class ArenaWindowState:
    hwnd: int
    pid: int
    bounds: tuple[int, int, int, int]
    foreground: bool


class ArenaWindowTracker:
    """Find MTGA.exe's visible top-level window without reading its memory."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        if os.name != 'nt':
            raise OverlayUnavailableError('The Arena overlay is available in native Windows only.')
        self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        from ctypes import wintypes
        self._wintypes = wintypes
        self._enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM,
        )
        self._user32.EnumWindows.argtypes = [
            self._enum_callback_type, wintypes.LPARAM,
        ]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        ]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _process_name(self, pid: int) -> str:
        handle = self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not handle:
            return ''
        try:
            capacity = self._wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(capacity)):
                return ''
            return Path(buffer.value).name.casefold()
        finally:
            self._kernel32.CloseHandle(handle)

    def find(self) -> ArenaWindowState | None:
        candidates: list[ArenaWindowState] = []
        foreground = self._user32.GetForegroundWindow()
        foreground_pid = self._wintypes.DWORD(0)
        if foreground:
            self._user32.GetWindowThreadProcessId(
                foreground, ctypes.byref(foreground_pid),
            )

        def inspect(hwnd, _parameter):
            if not self._user32.IsWindowVisible(hwnd) or self._user32.IsIconic(hwnd):
                return True
            pid = self._wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or self._process_name(pid.value) != 'mtga.exe':
                return True
            rectangle = self._wintypes.RECT()
            if not self._user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
                return True
            bounds = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
            if bounds[2] - bounds[0] < 320 or bounds[3] - bounds[1] < 240:
                return True
            candidates.append(ArenaWindowState(
                int(hwnd), int(pid.value), bounds, pid.value == foreground_pid.value,
            ))
            return True

        callback = self._enum_callback_type(inspect)
        if not self._user32.EnumWindows(callback, 0):
            return None
        if not candidates:
            return None
        return next((candidate for candidate in candidates if candidate.foreground), candidates[0])


def _set_process_dpi_awareness() -> None:
    """Keep Win32 window bounds and Tk geometry in the same coordinate space."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    set_context = getattr(user32, 'SetProcessDpiAwarenessContext', None)
    if set_context is not None:
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):
            return
    set_aware = getattr(user32, 'SetProcessDPIAware', None)
    if set_aware is not None:
        set_aware.restype = ctypes.c_bool
        set_aware()


class OverlayWindow:
    """Compact current-match HUD attached to the foreground Arena window."""

    WIDTH = 430
    FULL_HEIGHT = 570
    POLL_MS = 80
    WINDOW_POLL_SECONDS = 0.45
    STALE_SECONDS = 120.0

    def __init__(self, root, tk, ttk, filedialog):
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.tracker = ArenaWindowTracker()
        self.runner = CommandRunner()
        self._closing = False
        self._user_collapsed = False
        self._active_workflow: str | None = None
        self._pending_watch_values: dict[str, str] | None = None
        self._active_model = ''
        self._model_names: tuple[str, ...] = ()
        self._loaded_model_names: tuple[str, ...] = ()
        self._model_refreshing = False
        self._model_results: queue.Queue[
            tuple[tuple[str, ...], tuple[str, ...], str | None]
        ] = queue.Queue()
        self._latest_revision: int | None = None
        self._latest_lifecycle: str | None = None
        self._latest_decision_kind: str | None = None
        self._last_summary_revision: int | None = None
        self._last_summary_at: float | None = None
        self._last_window_poll = 0.0
        self._window_state: ArenaWindowState | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._manual_offset: tuple[int, int] | None = None

        self.status_var = tk.StringVar(master=root, value='Waiting for Arena')
        self.model_status_var = tk.StringVar(master=root, value='Checking local model list…')
        self.model_var = tk.StringVar(master=root, value=load_match_model())
        self.log_var = tk.StringVar(master=root, value=str(default_arena_log()))
        self.seat_var = tk.StringVar(master=root, value='Auto-detect')
        self.summary_var = tk.StringVar(master=root, value='Waiting for a supported game state.')
        self.choice_var = tk.StringVar(master=root, value='No active Arena choice.')
        self.advice_var = tk.StringVar(
            master=root,
            value='Recommendations are unverified. Opponent hidden cards remain unknown.',
        )

        self._build_widgets()
        self._configure_native_window()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self._refresh_models()
        self.root.after(self.POLL_MS, self._poll)

    def _build_widgets(self) -> None:
        self.root.title('MTGA Assistant Overlay')
        self.root.overrideredirect(True)
        self.root.geometry(f'{self.WIDTH}x{self.FULL_HEIGHT}+80+80')
        self.root.configure(bg='#0B1018')
        try:
            self.root.attributes('-topmost', True)
            self.root.attributes('-alpha', 0.96)
        except self.tk.TclError:
            pass

        style = self.ttk.Style(self.root)
        style.configure('Overlay.TFrame', background='#0B1018')
        style.configure('Overlay.TLabel', background='#0B1018', foreground='#E7EAF1',
                        font=('Segoe UI', 10))
        style.configure('OverlayTitle.TLabel', background='#101827', foreground='#F4F6FA',
                        font=('Segoe UI Semibold', 11))
        style.configure('OverlayMuted.TLabel', background='#0B1018', foreground='#AAB4C3',
                        font=('Segoe UI', 9))
        style.configure('OverlayStatus.TLabel', background='#101827', foreground='#92D5B1',
                        font=('Segoe UI', 9))
        style.configure('Overlay.TButton', padding=(8, 4))

        self.header = self.ttk.Frame(self.root, padding=(12, 8), style='Overlay.TFrame')
        self.header.pack(fill='x')
        self.title_label = self.ttk.Label(
            self.header, text='MTGA ARENA OVERLAY  ·  READ ONLY', style='OverlayTitle.TLabel',
        )
        self.title_label.pack(side='left')
        self.collapse_button = self.ttk.Button(
            self.header, text='−', width=3, command=self.toggle_collapse,
        )
        self.collapse_button.pack(side='right', padx=(4, 0))
        self.close_button = self.ttk.Button(self.header, text='×', width=3, command=self.close)
        self.close_button.pack(side='right', padx=(4, 0))
        self.stop_button = self.ttk.Button(
            self.header, text='STOP', width=6, command=self.stop_watch, state='disabled',
        )
        self.stop_button.pack(side='right', padx=(4, 0))
        self.status_label = self.ttk.Label(
            self.root, textvariable=self.status_var, style='OverlayStatus.TLabel',
            padding=(12, 5),
        )
        self.status_label.pack(fill='x')

        self.body = self.ttk.Frame(self.root, padding=(12, 8, 12, 12), style='Overlay.TFrame')
        self.body.pack(fill='both', expand=True)
        self.body.columnconfigure(1, weight=1)

        self.ttk.Label(self.body, text='LOCAL MODEL', style='OverlayMuted.TLabel').grid(
            row=0, column=0, sticky='w', padx=(0, 8), pady=3,
        )
        self.model_picker = self.ttk.Combobox(
            self.body, textvariable=self.model_var, state='readonly', width=32,
        )
        self.model_picker.grid(row=0, column=1, sticky='ew', pady=3)
        self.model_picker.bind('<<ComboboxSelected>>', self._model_selection_changed)
        self.refresh_button = self.ttk.Button(
            self.body, text='↻', width=3, command=self._refresh_models,
        )
        self.refresh_button.grid(row=0, column=2, sticky='e', padx=(5, 0), pady=3)
        self.model_status_label = self.ttk.Label(
            self.body, textvariable=self.model_status_var, style='OverlayMuted.TLabel',
        )
        self.model_status_label.grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 7))

        self.ttk.Label(self.body, text='PLAYER.LOG', style='OverlayMuted.TLabel').grid(
            row=2, column=0, sticky='w', padx=(0, 8), pady=3,
        )
        self.log_entry = self.ttk.Entry(self.body, textvariable=self.log_var, width=32)
        self.log_entry.grid(row=2, column=1, sticky='ew', pady=3)
        self.browse_button = self.ttk.Button(
            self.body, text='…', width=3, command=self._browse_log,
        )
        self.browse_button.grid(row=2, column=2, sticky='e', padx=(5, 0), pady=3)

        self.ttk.Label(self.body, text='YOUR SEAT', style='OverlayMuted.TLabel').grid(
            row=3, column=0, sticky='w', padx=(0, 8), pady=3,
        )
        self.seat_picker = self.ttk.Combobox(
            self.body, textvariable=self.seat_var, state='readonly',
            values=('Auto-detect', '1', '2'), width=32,
        )
        self.seat_picker.grid(row=3, column=1, sticky='ew', pady=3)
        self.start_button = self.ttk.Button(
            self.body, text='START WATCH', style='Overlay.TButton', command=self.start_watch,
        )
        self.start_button.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(7, 10))

        self.ttk.Separator(self.body).grid(row=5, column=0, columnspan=3, sticky='ew', pady=4)
        self.ttk.Label(self.body, text='CURRENT MATCH', style='OverlayTitle.TLabel').grid(
            row=6, column=0, columnspan=3, sticky='w', pady=(7, 4),
        )
        self.summary_label = self.ttk.Label(
            self.body, textvariable=self.summary_var, style='Overlay.TLabel',
            wraplength=390, justify='left',
        )
        self.summary_label.grid(row=7, column=0, columnspan=3, sticky='ew', pady=2)
        self.choice_label = self.ttk.Label(
            self.body, textvariable=self.choice_var, style='OverlayMuted.TLabel',
            wraplength=390, justify='left',
        )
        self.choice_label.grid(row=8, column=0, columnspan=3, sticky='ew', pady=(3, 2))
        self.notes_text = self.tk.Text(
            self.body, height=5, wrap='word', relief='flat', borderwidth=0,
            bg='#131C2B', fg='#E7EAF1', insertbackground='#E7EAF1',
            font=('Segoe UI', 9), padx=8, pady=6, state='disabled',
        )
        self.notes_text.grid(row=9, column=0, columnspan=3, sticky='ew', pady=(4, 7))
        self.ttk.Label(self.body, text='AI  ·  STRATEGY UNVERIFIED',
                       style='OverlayTitle.TLabel').grid(
            row=10, column=0, columnspan=3, sticky='w', pady=(2, 4),
        )
        self.advice_label = self.ttk.Label(
            self.body, textvariable=self.advice_var, style='Overlay.TLabel',
            wraplength=390, justify='left',
        )
        self.advice_label.grid(row=11, column=0, columnspan=3, sticky='ew', pady=(0, 5))
        self.ttk.Label(
            self.body,
            text='Read-only. Arena choices are not independently rules-verified.',
            style='OverlayMuted.TLabel', wraplength=390,
        ).grid(row=12, column=0, columnspan=3, sticky='w', pady=(4, 0))

        self.title_label.bind('<ButtonPress-1>', self._begin_drag)
        self.title_label.bind('<B1-Motion>', self._drag)

    def _configure_native_window(self) -> None:
        self.root.update_idletasks()
        hwnd = self.root.winfo_id()
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        from ctypes import wintypes
        get_style = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
        set_style = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        style = get_style(hwnd, -20)
        # WS_EX_TOOLWINDOW keeps a taskbar button out of the way; NOACTIVATE
        # lets the user click overlay controls without taking focus from Arena.
        set_style(hwnd, -20, style | 0x00000080 | 0x08000000)
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos(
            hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010 | 0x0020,
        )
        self._native_hwnd = hwnd
        self._native_user32 = user32
        self._show_without_activation()

    def _show_without_activation(self) -> None:
        self._native_user32.ShowWindow(self._native_hwnd, 4)  # SW_SHOWNOACTIVATE
        self._native_user32.SetWindowPos(
            self._native_hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010,
        )

    def _place(self, bounds: tuple[int, int, int, int] | None) -> None:
        self.root.update_idletasks()
        width = max(self.WIDTH, self.root.winfo_reqwidth())
        height = max(56, self.root.winfo_reqheight())
        if bounds is None:
            x, y = 30, 50
        else:
            left, top, right, bottom = bounds
            if self._manual_offset is None:
                x = max(left + 8, right - width - 16)
                y = max(top + 8, min(top + 16, bottom - height - 8))
            else:
                x, y = left + self._manual_offset[0], top + self._manual_offset[1]
        self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self._show_without_activation()

    def _begin_drag(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root,
                             self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        x = window_x + event.x_root - start_x
        y = window_y + event.y_root - start_y
        if self._window_state is not None:
            left, top, _right, _bottom = self._window_state.bounds
            self._manual_offset = (x - left, y - top)
        width = max(self.WIDTH, self.root.winfo_width())
        height = max(56, self.root.winfo_height())
        self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self._show_without_activation()

    def toggle_collapse(self) -> None:
        self._user_collapsed = not self._user_collapsed
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        active = bool(self._window_state and self._window_state.foreground)
        show_details = active and not self._user_collapsed
        if show_details:
            if not self.body.winfo_manager():
                self.body.pack(fill='both', expand=True)
            self.collapse_button.configure(text='−')
            activity = {
                'model-warm': 'Arena active · warming model',
                'watch': 'Arena active · Watch running',
                'model-unload': 'Arena active · releasing model',
            }.get(self._active_workflow, 'Arena active · Watch stopped')
            self.status_var.set(activity)
        else:
            if self.body.winfo_manager():
                self.body.pack_forget()
            self.collapse_button.configure(text='+' if active else '·')
            if self._window_state is None:
                self.status_var.set('Waiting for Arena. Match details are hidden.')
            elif not active:
                self.status_var.set('Arena in background. Match details hidden for privacy.')
            else:
                self.status_var.set('Arena active. Overlay collapsed.')

    def _sync_arena_window(self) -> None:
        now = time.monotonic()
        if now - self._last_window_poll < self.WINDOW_POLL_SECONDS:
            return
        self._last_window_poll = now
        try:
            state = self.tracker.find()
        except (OSError, ValueError):
            state = None
            self.status_var.set('Arena window status unavailable.')
        changed = (state != self._window_state)
        self._window_state = state
        if state is not None:
            self._place(state.bounds)
        if changed:
            self._apply_visibility()
        elif state is not None and not state.foreground:
            self._apply_visibility()
        if state is not None and state.foreground and self._last_summary_at is not None:
            age = now - self._last_summary_at
            if age >= self.STALE_SECONDS:
                self.summary_var.set('Game state is stale; waiting for a new Arena update.')
                self.choice_var.set('No current choice can be confirmed.')
                self.advice_var.set('Advice hidden because the last supported update is over two minutes old.')
                self._set_notes([])

    def _refresh_models(self) -> None:
        if self._model_refreshing:
            return
        self._model_refreshing = True
        self.refresh_button.configure(state='disabled')
        self.model_status_var.set('Loading installed models from local Ollama…')

        def fetch():
            try:
                models = tuple(list_installed_models(timeout=2.0))
            except Exception:
                self._model_results.put(((), (), 'Local Ollama model list is unavailable.'))
                return
            try:
                loaded = tuple(list_loaded_models(timeout=2.0))
            except Exception:
                loaded = ()
            self._model_results.put((models, loaded, None))

        threading.Thread(target=fetch, name='mtga-overlay-model-list', daemon=True).start()

    def _apply_model_list(self, models: tuple[str, ...], loaded: tuple[str, ...],
                          error: str | None) -> None:
        self._model_refreshing = False
        self.refresh_button.configure(state='normal')
        self._model_names = models
        self._loaded_model_names = loaded
        self.model_picker.configure(values=models)
        current = self.model_var.get().strip()
        saved = load_match_model()
        if current in models:
            selected = current
        elif saved in models:
            selected = saved
        else:
            ready = [name for name in loaded if name in models]
            selected = ready[0] if len(ready) == 1 else ''
        self.model_var.set(selected)
        if error:
            self.model_status_var.set(error)
        elif models:
            if selected and selected in loaded:
                self.model_status_var.set(
                    'Selected the model already loaded in Ollama; Watch will verify readiness.'
                )
            elif selected:
                self.model_status_var.set(
                    f'Selected saved model from {len(models)} installed model(s); Watch will load it.'
                )
            else:
                self.model_status_var.set(f'{len(models)} installed model(s); choose one for AI advice.')
        else:
            self.model_status_var.set('No local models found; rules-based Watch is available.')

    def _model_selection_changed(self, _event=None) -> None:
        selected = self.model_var.get().strip()
        if not selected:
            self.model_status_var.set(
                'Choose an installed model for AI advice, or leave blank for rules-based Watch.'
            )
        elif selected in self._loaded_model_names:
            self.model_status_var.set(
                f'{selected} is loaded; Watch will verify readiness before starting.'
            )
        else:
            self.model_status_var.set(
                f'{selected} selected; Watch will load it and verify readiness.'
            )

    def _browse_log(self) -> None:
        selected = self.filedialog.askopenfilename(
            title='Choose MTG Arena Player.log',
            filetypes=(('Arena log', 'Player.log'), ('Log files', '*.log'), ('All files', '*.*')),
        )
        if selected:
            self.log_var.set(selected)

    def _watch_values(self) -> dict[str, str]:
        return {
            'log': self.log_var.get(),
            'seat': self.seat_var.get(),
            'model': self.model_var.get(),
            'cards_db': '',
            'opponent_data': '',
        }

    def start_watch(self) -> None:
        if self.runner.active or self._closing:
            return
        values = self._watch_values()
        try:
            build_command('watch', values)
        except CommandInputError as exc:
            self.advice_var.set(str(exc))
            return
        model = values['model'].strip()
        if model and model not in self._model_names:
            self.model_status_var.set('Refresh the local model list and choose an installed model.')
            return
        self.start_button.configure(state='disabled')
        if model:
            self._pending_watch_values = values
            self._active_model = model
            self._active_workflow = 'model-warm'
            self.stop_button.configure(state='normal')
            self.status_var.set('Preparing Watch: warming the selected model…')
            self.model_status_var.set('Warming model and initializing text generation…')
            try:
                start_workflow('model-warm', {'model': model}, self.runner)
            except (CommandInputError, OSError, RuntimeError, ValueError):
                self._active_workflow = None
                self._pending_watch_values = None
                self.start_button.configure(state='normal')
                self.model_status_var.set('Model preload could not be started.')
        else:
            self._start_watcher(values)

    def _start_watcher(self, values: dict[str, str]) -> None:
        self._latest_revision = None
        self._latest_lifecycle = None
        self._latest_decision_kind = None
        self._last_summary_revision = None
        self.summary_var.set('Waiting for a supported game state.')
        self.choice_var.set('No active Arena choice.')
        self._set_notes([])
        self.advice_var.set('Waiting for the current game state and model advice.')
        try:
            start_workflow('watch', values, self.runner)
        except (CommandInputError, OSError, RuntimeError, ValueError):
            self._active_workflow = None
            self.start_button.configure(state='normal')
            self.status_var.set('Read-only Watch could not be started.')
            return
        self._active_workflow = 'watch'
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.status_var.set('Following Arena log read-only…')
        if not values['model']:
            self.model_status_var.set('Rules-based Watch; no model selected.')

    def stop_watch(self) -> None:
        if self._closing:
            return
        self._pending_watch_values = None
        if self.runner.active:
            self.runner.stop(timeout_seconds=2.0)
        self._active_workflow = None
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        if self._begin_model_release():
            self.status_var.set('Watch stopped; releasing local model…')
        else:
            self.status_var.set('Watch stopped.')

    def _begin_model_release(self) -> bool:
        if not self._active_model:
            return False
        self._active_workflow = 'model-unload'
        self.model_status_var.set('Releasing local model…')
        try:
            start_workflow('model-unload', {'model': self._active_model}, self.runner)
        except (CommandInputError, OSError, RuntimeError, ValueError):
            self._active_workflow = None
            self.model_status_var.set('Could not confirm model release.')
            return False
        return True

    def _set_notes(self, notes: list[str]) -> None:
        self.notes_text.configure(state='normal')
        self.notes_text.delete('1.0', 'end')
        self.notes_text.insert('1.0', '\n'.join('• ' + note for note in notes[:6]))
        self.notes_text.configure(state='disabled')

    def _apply_match_summary(self, event: dict[str, Any]) -> None:
        if self._latest_revision is not None and event['revision'] < self._latest_revision:
            return
        new_revision = event['revision'] != self._last_summary_revision
        self._latest_revision = event['revision']
        self._latest_lifecycle = event['lifecycle']
        self._latest_decision_kind = event['decision_kind']
        self._last_summary_revision = event['revision']
        self._last_summary_at = time.monotonic()
        turn = f"Turn {event['turn']}" if event['turn'] is not None else 'Opening hand'
        phase = event['phase'] or event['lifecycle'].replace('_', ' ').title()
        life = (f"Life  You {event['my_life']}  ·  Opponent {event['opp_life']}"
                if event['my_life'] is not None and event['opp_life'] is not None
                else 'Life totals incomplete')
        board = (f"Board  You {event['battlefield']}  ·  Opponent {event['opp_battlefield']}"
                 if event['battlefield'] is not None and event['opp_battlefield'] is not None
                 else 'Board counts incomplete')
        hand = f"Your hand: {event['hand']} card(s)" if event['hand'] is not None else 'Your hand count unknown'
        self.summary_var.set(
            f"{turn}  ·  {phase}\n{life}\n{board}  ·  {hand}\nState: {event['integrity']}"
        )
        if event['arena_choice_present']:
            if event['choices']:
                labels = event['choices'][:5]
                suffix = f"\n+{len(event['choices']) - len(labels)} more" if len(event['choices']) > len(labels) else ''
                self.choice_var.set('ARENA-PRESENTED CHOICES  ·  legality unverified\n' +
                                    '\n'.join(f'{index + 1}. {label}'
                                              for index, label in enumerate(labels)) + suffix)
            else:
                self.choice_var.set('ARENA CHOICE DETAILS UNAVAILABLE\nOptions could not be safely analyzed.')
        else:
            self.choice_var.set('NO ACTIVE ARENA CHOICE')
        self._set_notes(event['notes'])
        if new_revision:
            self.advice_var.set('Waiting for current model advice. Recommendations remain unverified.')

    def _apply_event(self, event: dict[str, Any]) -> None:
        if event['type'] == 'state':
            if self._latest_revision is None or event['revision'] > self._latest_revision:
                self._latest_revision = event['revision']
                self._latest_lifecycle = None
                self._latest_decision_kind = None
                self.summary_var.set('Updating current Arena state…')
                self.choice_var.set('No current choice confirmed yet.')
                self._set_notes([])
                self.advice_var.set('Game state changed; waiting for current analysis.')
            return
        if event['type'] == 'match_summary':
            self._apply_match_summary(event)
            return
        if event['type'] == 'model_status':
            labels = {
                'warming': 'Warming selected model…', 'ready': 'Ready — model resident in Ollama.',
                'unavailable': 'Model unavailable; deterministic observations remain active.',
                'unloaded': 'Model released.',
            }
            self.model_status_var.set(labels[event['status']])
            return
        if event['type'] == 'decision_progress':
            if event['revision'] != self._latest_revision:
                return
            messages = {
                'pending': ('Reviewing the current Arena candidates and selection details…'
                            if self._latest_decision_kind in {'attackers', 'blockers', 'targets', 'payment'} else
                            'Comparing Arena-presented choices; legality is unverified.'),
                'review_pending': 'Reviewing known opening-hand evidence; unknown cards stay unknown.',
                'skipped': 'Choice evidence is incomplete; no model comparison was sent.',
                'review_skipped': 'Visible opening-hand evidence is incomplete; no model request was sent.',
            }
            self.advice_var.set(messages[event['status']])
            return
        if event['type'] != 'decision_result' or event['revision'] != self._latest_revision:
            return
        if time.monotonic() >= event['deadline_at']:
            self.advice_var.set('Response expired; check the current Arena state.')
        elif event['error_code']:
            if event['error_code'] == 'timeout':
                self.model_status_var.set('The last model response missed its deadline.')
            self.advice_var.set(
                f"AI {event['error_code'].replace('_', ' ')} ({event['elapsed_ms']} ms). "
                'Deterministic observations remain available.'
            )
        else:
            self.model_status_var.set(f"Ready — last response {event['elapsed_ms']} ms.")
            if event['response_kind'] == 'state_review':
                heading = ('COMBAT REVIEW' if self._latest_decision_kind in {'attackers', 'blockers'}
                           else 'TARGET REVIEW' if self._latest_decision_kind == 'targets'
                           else 'PAYMENT REVIEW' if self._latest_decision_kind == 'payment'
                           else 'BOTTOM-CARD REVIEW' if self._latest_lifecycle == 'BOTTOM_CARDS'
                           else 'OPENING-HAND REVIEW')
            else:
                heading = 'AI COMPARISON'
            self.advice_var.set(
                f"{heading}  ·  {event['elapsed_ms']} ms\n{event['text']}\n\n"
                'Strategy is unverified; legality is not established.'
            )

    def _finish_workflow(self, exit_code: int | None) -> None:
        workflow = self._active_workflow
        if workflow == 'model-warm':
            pending = self._pending_watch_values
            self._pending_watch_values = None
            if exit_code == 0 and pending is not None and not self._closing:
                save_match_model(pending['model'])
                self.model_status_var.set('Ready; starting read-only Watch…')
                self._start_watcher(pending)
            else:
                self.start_button.configure(state='normal')
                self.model_status_var.set('Model did not become ready; Watch was not started.')
                if not self._begin_model_release() and self._closing:
                    self.root.after(0, self.root.destroy)
            return
        if workflow == 'watch':
            self._active_workflow = None
            self.start_button.configure(state='normal')
            self.stop_button.configure(state='disabled')
            if exit_code == 0:
                self.status_var.set('Watch stopped.')
            else:
                self.status_var.set('Watch ended; check Arena and the local log connection.')
            if self._active_model and not self._closing:
                self._begin_model_release()
            return
        if workflow == 'model-unload':
            self._active_workflow = None
            self._active_model = ''
            self.model_status_var.set(
                'Model released.' if exit_code == 0 else 'Model release could not be verified.'
            )
            if self._closing:
                self.root.after(0, self.root.destroy)
            return
        if self._closing:
            self.root.after(0, self.root.destroy)

    def _process_message(self, message: RunnerMessage) -> None:
        if message.kind == 'ui_event':
            try:
                event = _validated_desktop_event(message.text)
            except (ValueError, TypeError):
                event = None
            if event is not None:
                self._apply_event(event)
        elif message.kind == 'error':
            self.status_var.set('The local Watch/preload operation could not start.')
            workflow = self._active_workflow
            self._active_workflow = None
            self._pending_watch_values = None
            self.start_button.configure(state='normal')
            self.stop_button.configure(state='disabled')
            if workflow == 'model-unload':
                self.model_status_var.set('Model release could not be verified.')
                self._active_model = ''
                if self._closing:
                    self.root.after(0, self.root.destroy)
            elif workflow == 'model-warm' and self._active_model:
                self.model_status_var.set('Model preload failed; attempting cleanup…')
                if not self._begin_model_release() and self._closing:
                    self.root.after(0, self.root.destroy)
            elif workflow == 'watch' and self._active_model and not self._closing:
                self._begin_model_release()
            elif self._closing:
                self.root.after(0, self.root.destroy)
        elif message.kind == 'finished':
            self._finish_workflow(message.exit_code)

    def _poll(self) -> None:
        if self._closing and not self.runner.active and self._active_workflow is None:
            self.root.destroy()
            return
        while True:
            try:
                model_result = self._model_results.get_nowait()
            except queue.Empty:
                break
            self._apply_model_list(*model_result)
        for message in self.runner.poll():
            self._process_message(message)
        self._sync_arena_window()
        if self._last_summary_at is not None and time.monotonic() - self._last_summary_at >= self.STALE_SECONDS:
            if self._window_state is not None and self._window_state.foreground:
                self.summary_var.set('Game state is stale; waiting for a new Arena update.')
                self.choice_var.set('No current choice can be confirmed.')
                self.advice_var.set('Advice hidden because the last supported update is over two minutes old.')
                self._set_notes([])
        self.root.after(self.POLL_MS, self._poll)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._pending_watch_values = None
        if self.runner.active:
            self.runner.stop(timeout_seconds=2.0)
        self._active_workflow = None
        if self._begin_model_release():
            self.status_var.set('Closing overlay after model release…')
            return
        self.root.after(0, self.root.destroy)


def launch_overlay() -> int:
    """Launch the external read-only Arena overlay on native Windows."""
    if os.name != 'nt':
        print('The Arena overlay requires native Windows Python; the read-only CLI remains available.')
        return 2
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except ImportError:
        print('The Arena overlay requires a Windows Python installation with Tk support.')
        return 2
    try:
        _set_process_dpi_awareness()
        root = tk.Tk()
        OverlayWindow(root, tk, ttk, filedialog)
        root.mainloop()
    except (OverlayUnavailableError, OSError, RuntimeError, tk.TclError) as exc:
        print(str(exc))
        return 2
    return 0
