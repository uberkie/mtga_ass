# MTGA Stage 7 Local Desktop Interface Implementation Plan

> **Superseded UI target (2026-10-18):** Do not use this plan to expose the standalone multi-tab window as the primary application. The active target is the Arena overlay documented in `docs/superpowers/specs/2026-10-18-mtga-game-overlay-design.md` and its implementation plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local desktop window that exposes the existing read-only match, review, deck, draft and data workflows.

**Architecture:** A Tkinter/ttk UI builds argument lists for the existing CLI and starts one child process at a time with `shell=False`. A worker thread forwards bounded output to the UI thread; the interface stores no log, path, account or command history.

**Tech Stack:** Python 3.10+, standard-library `tkinter`, `subprocess`, `threading`, and `queue`; no new package dependencies.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-desktop-interface-design.md`.

## Global Constraints

- The UI wraps existing commands and must not add claims about rules, legality, accuracy or strategy.
- Real match workflows require the user to select seat 1 or 2 explicitly.
- Paths are separate process arguments; every child launches with `shell=False`.
- Local Ollama remains opt-in and uses the existing loopback-only integration.
- Arena remains read-only; no client input automation, network access, or persistent match history is added.
- Output shown by the UI is capped at 1,048,576 Unicode characters, and only one child process may run at a time.
- The workspace has no existing commit; preserve the current uncommitted-work convention and do not create an initial commit.

## Review Focus

- A path containing spaces, quotes or shell metacharacters must remain one argument and never execute as shell syntax; pin with `test_build_command_keeps_path_as_one_argument`.
- A missing or invalid seat must prevent a real-match child from starting; pin with `test_build_command_requires_explicit_valid_seat`.
- A large or continuous watch stream must not grow UI memory without bound; pin with `test_bounded_output_keeps_only_recent_tail` and `test_runner_queue_drops_oldest_when_full`.
- Tk initialization can fail on a headless or incomplete Python install; pin with `test_gui_launch_reports_unavailable_display_without_traceback`.
- Closing while watch is active must stop and reap the child; pin with `test_runner_stop_terminates_active_child`.

## Tasks

### Task 1: Workflow command builder and output bound

**Files:**
- Create: `mtga_assistant/desktop.py`
- Create: `tests/test_desktop.py`

**Interfaces:**
- Produces `build_command(workflow: str, values: Mapping[str, str], python_executable: str = sys.executable) -> list[str]` and `CommandInputError`.
- Produces `bounded_output(current: str, incoming: str, limit: int = 1_048_576) -> tuple[str, bool]`.
- Supports workflow keys `watch`, `replay`, `review`, `deck-audit`, `deck-suggest`, `draft-rank`, `draft-stats-import`, and `cards-import`.

- [x] **Step 1: Write failing tests** for each workflow's argument list, required fields/enumerations, explicit seat, optional model/catalog/stats inputs, shell-metacharacter path preservation, and bounded tail output.
- [x] **Step 2: Run `python3 -m unittest tests.test_desktop`** and confirm failures are missing functions or rejected expectations, not test errors.
- [x] **Step 3: Implement** command construction using a Python argv list only; validate format and suggestion-goal choices against `mtga_assistant.decks`. Implement tail retention with a visible truncation marker.
- [x] **Step 4: Run `python3 -m unittest tests.test_desktop`** and confirm all builder and output-bound tests pass.

### Task 2: Bounded child-process runner

**Files:**
- Modify: `mtga_assistant/desktop.py`
- Modify: `tests/test_desktop.py`

**Interfaces:**
- Produces `RunnerMessage(kind: str, text: str = "", exit_code: int | None = None)`.
- Produces `CommandRunner.start(args: Sequence[str]) -> None`, `stop(timeout_seconds: float = 2.0) -> None`, `poll() -> list[RunnerMessage]`, and read-only `active: bool`.

- [x] **Step 1: Write failing tests** for combined stdout/stderr capture, clean and nonzero exit status, bounded message queue behavior, stop/reap, and `Popen(..., shell=False)`.
- [x] **Step 2: Run the focused tests** and confirm failures identify missing runner behavior.
- [x] **Step 3: Implement** one child process, daemon reader thread, bounded queue that drops oldest output messages under pressure, safe launch errors, and terminate-then-kill shutdown.
- [x] **Step 4: Run `python3 -m unittest tests.test_desktop`** and confirm runner tests pass without a leaked child.

### Task 3: Tkinter workflow window

**Files:**
- Modify: `mtga_assistant/desktop.py`
- Modify: `tests/test_desktop.py`

**Interfaces:**
- Produces `launch_gui() -> int`.
- Builds Match, Deck, Draft and Data tabs as defined in the spec, plus one shared 1 MiB output panel and status line.

- [x] **Step 1: Write failing tests** for missing Tk/display reporting, lazy UI initialization and dispatch of each workflow button through `build_command`/`CommandRunner`.
- [x] **Step 2: Run the focused tests** and confirm expected failures before implementing widgets.
- [x] **Step 3: Implement** the Tk root, file pickers, explicit seat selector, optional catalog/model fields, workflow controls, shared output/status, UI-thread queue polling, Stop, Clear and close cleanup.
- [x] **Step 4: Run `python3 -m unittest tests.test_desktop`** and confirm UI wiring tests pass headlessly.
- [x] **Step 5: Run a graphical smoke if a desktop display is available; otherwise run the controlled headless-error check and record the missing display.**

### Task 4: CLI launch, documentation and final validation

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_desktop.py`
- Modify: `README.md`
- Modify: `docs/validation.md`
- Modify: `.superpowers/sdd/2026-10-14-mtga-desktop-interface/progress.md`

- [x] **Step 1: Write failing tests** for `gui` dispatch, lazy import, and controlled unavailable-display errors.
- [x] **Step 2: Run the focused tests** and confirm expected failure.
- [x] **Step 3: Add** a `gui` command that imports the desktop launcher only when selected; document launch, workflows, read-only boundaries and headless limitations.
- [x] **Step 4: Run the full suite** with `python3 -m unittest discover -s tests` and compile with `python3 -m compileall -q mtga_assistant tests`.
- [x] **Step 5: Record** test results, headless/desktop smoke status, and unchanged local input data in validation and the SDD ledger.
