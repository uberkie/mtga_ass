# MTGA Overlay Deck Workflow Implementation Plan

> **For agentic workers:** Native execution is selected under the user's standing authorization. Implement tasks in order; preserve the user instruction not to run tests unless their success is guaranteed.

**Goal:** Add offline deck audit and suggestion workflows to the existing Arena overlay.

**Architecture:** Add a Match/Deck page switcher to the same overlay window. Reuse the validated `desktop.build_command` and `start_workflow` paths, relay bounded child output into the Deck page, and preserve the overlay's existing background privacy hiding.

**Tech Stack:** Python, Tkinter/ttk, existing `CommandRunner`, existing deck CLI.

**Spec:** `docs/superpowers/specs/2026-10-19-mtga-overlay-deck-workflow-design.md`

## Global Constraints

- Keep the primary UI as the native external Arena overlay.
- Keep all workflows local and read-only; do not send input to Arena.
- Hide the overlay body when Arena is not foreground.
- Reuse existing deck command validation, reports and output bounds.
- Do not add model requests, network requests or dependencies.
- Do not run tests or compile checks unless success can be guaranteed.

## Review Focus

- Missing/invalid required file or format: reject before child launch and show a safe message.
- Suggestion without a collection or goal: require both before launch.
- Deck workflow attempted while Watch owns the child runner: refuse without stopping Watch.
- Child emits large or non-UTF-8 output: retain the existing runner and UI bounds.
- Arena loses foreground while the Deck page is open: hide paths and output with the existing body.

---

### Task 1: Add page navigation and Deck form

**Files:**
- Modify: `mtga_assistant/overlay.py`

**Interfaces:**
- Use existing imports `build_command`, `start_workflow`, `CommandInputError`, `FORMATS`, and `SUGGESTION_GOALS`.
- Store deck list, format, catalog, optional collection, and suggestion goal in transient Tk variables.

- [x] Add Match/Deck navigation inside the current overlay body; default to Match.
- [x] Move existing match widgets under a Match page without changing their state or event bindings.
- [x] Add Deck inputs, local file pickers, audit/suggest buttons, and bounded output text.
- [x] Keep both pages under the existing body so `_apply_visibility()` hides them together.

### Task 2: Run deck commands through the existing child runner

**Files:**
- Modify: `mtga_assistant/overlay.py`

**Interfaces:**
- Call `start_workflow('deck-audit', values, self.runner)` or `start_workflow('deck-suggest', values, self.runner)`.
- Handle `RunnerMessage('output')`, `RunnerMessage('error')`, and `RunnerMessage('finished')` for these two workflow names.

- [x] Validate command inputs with `build_command` before launch.
- [x] Reject attempts while any other child workflow is active; leave Watch and model lifecycle unchanged.
- [x] Disable Deck run buttons during the command; expose cancellation through the existing header Stop control with workflow-specific status.
- [x] Append output within a fixed 64 KiB visible-text cap and preserve a truncation marker.
- [x] Restore buttons and show exit success/failure when the child finishes.

### Task 3: Document and source-review the integrated flow

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`

- [x] Document how to open the Deck page and the required inputs for audit versus suggestions.
- [x] Record that implementation was source-reviewed and distinguish it from unperformed native Windows and live-overlay validation.
- [x] Review page routing, command arguments, foreground hiding, cancellation and output bounds by reading the affected source. Do not launch the model or app.
