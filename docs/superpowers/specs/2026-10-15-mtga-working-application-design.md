# MTGA Stage 1: Timed Live Assistant

> **UI target updated (2026-10-18):** The state-to-model-to-UI acceptance goal remains active, but the target surface is now the game overlay, not the desktop Match tab. Use `2026-10-18-mtga-game-overlay-design.md` for the current interface design.

Date: 2026-10-15 (Africa/Johannesburg)
Status: User-directed scope for the first implementation milestone.

## Purpose and acceptance target

Make the existing desktop app usable during an Arena match. For a supported, current Arena-presented decision, the app must give the local model the current sanitized public game state and the decision evidence, receive a structurally valid bounded response, and display it in the GUI within a conservative five-second response target after the decision state is recognized. The response remains unverified strategy and never claims independent legality.

The five-second target is an application service target, not a claim about an official phase timer. MTGA timing varies by event and priority state; tournament challenges also have a cumulative player match clock. The app therefore reserves time for the player to act instead of using the whole available rope. If a selected model is cold, loading time counts toward the first response unless the user waits for the GUI's explicit **Ready** state before relying on it. Cold-load time, warm inference time, and full state-to-visible-GUI time must be measured and reported separately. The acceptance timer begins immediately before a synthetic decision event is supplied and stops only after the matching current valid response is visibly rendered in the Match output widget.

## Scope and sequence

This stage is limited to live match state, optional local model readiness, current-decision response timing, and display in the existing desktop Match tab. It does not implement Stage 12 card catalog enrichment, or expand deck, draft, review, opponent, or voice features. Those remain deferred until this milestone passes.

The app continues to observe Arena's local log only. It must not click, type into, control, or otherwise send input to Arena. The model is optional for deterministic state viewing; selecting a model enables this timed path. While live watch is active, generation is limited to validated Arena-presented decisions so a slower background explanation cannot occupy the single model worker when a decision arrives. Deterministic state remains available on every update; existing non-live explanation workflows are unchanged.

## Model runtime and readiness

All inference stays on the existing loopback Ollama service at `127.0.0.1:11434`. The app offers Match-tab **Preload model** and **Unload model** actions. Preload sends Ollama's documented empty-prompt load request with a bounded context size and indefinite session keep-alive, then confirms the exact selected model appears in `/api/ps`. It reports the full warm-action wall time and Ollama's load duration when available. Unload releases the model from VRAM/RAM when the user is done. The GUI distinguishes **Warming**, **Ready**, and **Unavailable**. A model is never described as ready merely because it is installed or Ollama responds to a health check.

When a model is selected, live watching requires that the exact model is already loaded; otherwise it fails quickly with a message to preload it first. This prevents an unexpected cold start from being charged to a decision. The user must wait for **Ready** before relying on a response. Each live decision request includes an explicit keep-alive and bounded context/output settings so the daemon default cannot silently evict the selected model between decisions. The app does not alter global Ollama environment settings.

Before any model generation or comparison benchmark, record GPU name and free VRAM, GPU utilization, available RAM, free disk, Ollama version/reachability, installed/loaded models, and active GPU work. Abort that benchmark if the configured model plus context cannot fit with headroom. Use one model at a time. Never start a model download until the same storage and VRAM preflight has passed.

## Decision input and output

For an Arena-presented decision, `ExplanationWorker` passes both the already-validated decision analysis and `compact_live_snapshot(snapshot)` into the comparison request. This preserves the current snapshot fingerprint check and fixed choice ordering while giving the model the phase, step, turn, life totals, visible zones, and current decision that are absent from the existing analysis-only prompt.

The model request remains under the existing 64 KiB payload limit. It contains no raw log text, Arena protocol identifiers, filesystem paths, account data, hidden card identities, or model-generated history. It stays on loopback. The model can order only Arena-presented options and cite only validated fact codes; the response does not add options or assert legality. If the state changes, a response for an obsolete revision is discarded.

Current-decision requests bypass the ordinary explanation interval and start as soon as the model is ready and the state is valid. The hard application deadline is five seconds from CLI recognition of the supported decision to insertion of its matching valid response in the Match output widget; widget insertion is the measurable GUI endpoint and must occur strictly before the deadline. Model inference has an earlier four-second cutoff from that same recognition timestamp, reserving the final second for response validation, subprocess delivery, and GUI insertion. The worker derives the remaining inference budget from the original monotonic recognition time, bounds the Ollama request timeout by that budget, and discards any result that completes at or after the four-second cutoff. A result that completed before four seconds may be validated, delivered, and inserted during the reserved final second; it is still discarded if the state becomes stale or it is not inserted before five seconds. CLI and GUI processes exchange a bounded desktop event containing the snapshot revision (the same integer `revision` bound to the worker's analysis/fingerprint) and monotonic deadline; the GUI rechecks the deadline and latest snapshot revision immediately before widget insertion. A newer state event clears any prior recommendation, so an old result is never left presented as current. An expired, malformed, stale, or unavailable response displays a controlled status; deterministic state and choices remain visible. Each displayed model result includes its elapsed time and an explicit unverified-strategy label.

## GUI behavior

The Match tab lets the user select a local model, preload it, then watch the Arena log. The interface shows startup readiness and the result/latency in its existing output area. A warm-up or service failure is visible and does not block state capture. For live model decisions, the GUI consumes bounded timestamped desktop events, checks the latest revision and monotonic deadline on its UI thread immediately before insertion, and clears the previous recommendation when a newer state arrives. The GUI must not imply that a response is timely while the model is warming or running on an unverified fallback.

The selected model stays resident while Watch is active to avoid charging cold-load time to a decision. When automatic seat identity becomes ambiguous, Watch is stopped, or the GUI closes, the assistant closes decision work and requests a verified unload from Ollama. If unload cannot be confirmed, the interface reports that state and advises closing Ollama before restarting Arena. A fresh Watch requires the user to preload and verify readiness again.

## Model choice and evidence

The existing local 4B Qwen 3.5 coder model is the baseline. A smaller official Qwen 3.5 2B model is a latency candidate, not assumed to be equally capable. Compare baseline and candidate with the same synthetic public-state decision fixtures, context/output bounds, one warm-up, and repeated warm requests. Report cold load separately. Record response validity, whether each response used the included state without violating the fixed schema, warm latency, and VRAM residency. A smaller model is recommended only if it has materially lower measured warm latency and passes the same response-validity checks; this is not a broad claim of strategic equivalence.

## Validation and release gate

Automated tests must prove prompt input contains the current sanitized snapshot and matching decision evidence, hidden-identity and protocol-ID sentinels are absent, model readiness is verified from `/api/ps`, decision requests bypass throttling, stale replies disappear, and UI output includes the recommendation/status and latency. Desktop-event tests must show a valid response completed before four seconds can be inserted during the reserved final second, that insertion at or after five seconds is rejected, and that a newer state revision prevents an older result from being inserted.

Run the existing full test suite and a synthetic end-to-end Match-tab smoke with the local model. For runtime evidence, preflight resources immediately before the run and preload the chosen model, measuring cold load separately. Then perform at least ten sequential synthetic decision events. For each event, measure from immediately before appending it to the watched synthetic log until its matching current valid response is inserted in the Match output widget; label this event-supply-to-render time. Every one of the ten end-to-visible-render times must be at most five seconds; every model inference must complete before its four-second cutoff. A result may be rendered during the reserved final second only if inference completed before four seconds and the state remains current. CLI-only timing is diagnostic evidence and cannot pass the GUI release gate. If the candidate or baseline fails, do not claim the milestone; keep the feature visibly unavailable or use a measured faster candidate.

Real-log parsing compatibility is reported separately from synthetic/model timing. Do not use a historical log's generation time as proof of live-match performance, and do not claim direct current-match validation unless an active match was actually observed.

## Environment preflight evidence at design time

On 2026-10-15 10:37 SAST, the local Ollama 0.35.0 API responded on loopback; no model was loaded. The NVIDIA GeForce RTX 3080 reported 10,240 MiB total and 7,334 MiB free, with 1% utilization. Linux reported 22 GiB available RAM and 813 GiB free workspace disk. Installed choices included a 3.3 GB 4B Qwen 3.5 coder model and two 9B Qwen distills. This confirms room for a bounded local candidate at that moment, not that a live Arena session leaves the same VRAM headroom or that any model meets the five-second target. Recheck immediately before inference.

Ollama documents loading through an empty `/api/generate` prompt and controlling residency with `keep_alive`; its API also supports a bounded `num_ctx` option. See [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md), [Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx), and [Ollama model options](https://github.com/ollama/ollama/blob/main/docs/modelfile.mdx).
