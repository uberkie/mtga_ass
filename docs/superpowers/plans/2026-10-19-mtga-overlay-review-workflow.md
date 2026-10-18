# MTGA Overlay Post-game Review Workflow

> Execution: Native implementation under the user's standing authorization. Do not run tests or compile checks; source-review the changed flow and record runtime limits.

**Goal:** Expose the existing privacy-safe post-game review command inside the external Arena overlay.

**Design:** Add a Review page beside Match and Deck. Require a user-selected log and explicit seat; allow an optional local card catalog and an opt-in local model reflection using the selected installed model. Reuse `build_command`, `start_workflow`, `CommandRunner`, bounded panel output, cancellation and the existing foreground-only overlay body. Do not persist review inputs or output.

**Files:** `mtga_assistant/overlay.py`, `README.md`, `docs/validation.md`.

## Tasks

- [x] Add a Review page with explicit log/seat fields, optional local card catalog, opt-in model checkbox, local file dialogs and bounded report output.
- [x] Route the `review` workflow through the existing validated runner; prevent overlap, stream output to the Review page, expose cancellation and report completion/failure.
- [x] Document how to use the page and source-review the arguments, runner lifecycle, output cap and privacy hiding. Do not run tests, compile checks, model inference or the overlay.
