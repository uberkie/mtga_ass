# MTGA Overlay Draft Workflow

> Execution: Native implementation under the user's standing authorization. Do not run tests or compile checks; source-review the changed flow and record runtime limits.

**Goal:** Expose the existing offline pack ranking and local draft-statistics import in the Arena overlay.

**Design:** Add a Draft page beside Match, Deck and Review. Rank only an explicit local pack/pool snapshot against the card catalog with optional local statistics. Import only a user-obtained 17Lands CSV/gzip CSV to an explicitly selected local SQLite destination. Reuse validated workflow construction, the shared child runner, cancellation, bounded output, and the foreground-only overlay body. Do not add live pack extraction or model/network requests.

**Files:** `mtga_assistant/overlay.py`, `README.md`, `docs/validation.md`.

## Tasks

- [x] Add local file pickers and forms for pack ranking and statistics import.
- [x] Route both existing draft workflows through the shared runner and output/cancel lifecycle.
- [x] Document manual local inputs and unverified native UI behavior; source-review only.
