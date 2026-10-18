# Agent instructions

## 2026-10-13 — Workspace instructions supplied by the user

- Read the root `AGENTS.md` before working in this workspace. If it is missing, create it.
- Append additional instructions with a date and timestamp.
- When a new instruction contradicts an earlier instruction, explain the change and remove the superseded instruction or mark it **Not Applicable** so future agents are not confused.

This file was created because no root `AGENTS.md` existed. No earlier instructions were superseded.

## 2026-10-14 21:43:58 SAST — Workspace direction supplied by the user

- Continue the approved read-only MTGA assistant work toward the complete assistant capabilities described in the project request; do not stop at the current prototype or claim feature parity while documented gaps remain.
- Preserve the read-only boundary: do not automate or send input to Arena. Keep legal-move claims separate from unverified strategic guidance, and record validation limits in project documentation.

## 2026-10-14 22:02:24 SAST — Eventual feature-parity goal

- Continue toward the complete approved scope: reliable real-match state and evidence-grounded coaching, public-information-only opponent analysis, rules-grounded advice, deckbuilding, draft assistance, post-game review, voice interaction and a desktop interface.
- **Not Applicable as the UI target from 2026-10-18 16:17:57 SAST.** The user changed the interface requirement to a game overlay, not a standalone desktop app. The named capability scope remains active.
- Preserve privacy, local-first behavior, explicit uncertainty and no Arena input automation. Validate each stage and report synthetic tests, real-log compatibility and actual model/runtime evidence as separate results.
- **Not Applicable as the immediate work order from 2026-10-15 10:37:58 SAST.** The user now requires the working-application milestone below before any other feature. Keep this broader scope as the eventual goal after that milestone; do not execute later features before then.

## 2026-10-15 10:37:58 SAST — Working-application milestone and model preflight

- First establish a working application: the local model must receive the current sanitized game state, return a valid response within the available MTGA decision window, and have that response displayed in the GUI.
- Do not implement or resume any other feature until this milestone is demonstrated. In particular, Stage 12 visible-card catalog enrichment is deferred; the existing approved spec and plan are not authorization to execute it before the milestone.
- Before any model inference or model benchmark, inspect available VRAM, system RAM, disk space, Ollama reachability, installed/loaded models, context size, and concurrent GPU use. Record the result before starting inference.
- Count cold model loading in end-to-end decision latency unless the selected model is loaded and verified ready before the decision request. The GUI must show warming versus ready; an unready model must not be represented as capable of meeting the decision window.
- Keep model work local and bounded. Limit context/output sizes, use a finite decision timeout, and do not add Arena input automation. Compare faster candidates on both latency and representative decision response validity; model size alone is not evidence of equivalent capability.
- Keep synthetic correctness, real-log compatibility, cold-load time, warm inference time, and GUI display latency as separate evidence. Do not claim the milestone until state-to-GUI behavior is verified within the decision deadline.

## 2026-10-15 11:34:02 SAST — Model latency questions supplied by the user

- Before model-backed work, state the current resource/readiness preflight and whether the chosen run is within available VRAM, RAM, disk and deadline limits.
- Explicitly explain when cold loading counts toward decision latency, whether the exact model is preloaded and verified Ready before a request, and what happens if it unloads.
- Compare lower-latency candidates only with measured timing and structured-response validity on the same representative request. Do not claim similar coaching capability without a quality evaluation.

## 2026-10-15 13:50:16 SAST — Test-run discipline supplied by the user (**Not Applicable from 13:56:55 SAST**)

- **Not Applicable:** Superseded by the user's 13:56:55 SAST instruction not to add or run tests or compile checks for this work.

## 2026-10-15 13:52:24 SAST — User clarification on confidence

- **Not Applicable:** The prior sentence that reframed the 100% confidence requirement as only the strongest practical review and rejected literal certainty is superseded. The user clarified that confidence is their stated criterion and must not be conflated with correctness or challenged.
- Follow the user's confidence requirement as stated; do not tell the user that 100% confidence is impossible.

## 2026-10-15 13:56:55 SAST — User direction: no tests (**Not Applicable as the test-running condition from 2026-10-18 19:04:46 SAST**)

- **Not Applicable:** The 13:50:16 instruction about when to run tests is superseded for this work by the user's newer instruction, “i said no tests.”
- **Not Applicable as the test-running condition:** The blanket ban was later amended and is now superseded by the success-guarantee condition below. The prohibition on compile checks remains in force unless the user explicitly changes it. Review changes by reading the affected code when tests are not authorized.

## 2026-10-18 16:17:57 SAST — Game overlay replaces the standalone desktop app

- **Not Applicable:** The 2026-10-14 22:02:24 SAST phrase “and a desktop interface” as the UI target. The user now directs that the application be a game overlay, not a desktop app; preserve the rest of that feature-parity scope.
- The primary Windows interface must attach a compact, borderless, always-on-top overlay to the MTGA window, show full game details only while Arena is foreground, and expose a privacy-safe collapsed status bar otherwise. Require borderless/windowed Arena mode when exclusive fullscreen prevents an external overlay.
- Keep the overlay external and read-only. Do not inject DLLs, hook Arena graphics or input, send mouse/keyboard/game actions, or read hidden card identities.
- The `overlay` command is the primary launch path; `gui` may remain an alias only if it launches the same overlay. Do not expose the former multi-tab desktop window as the primary app.
- Keep the working-match milestone first. After state-to-model-to-overlay behavior is established, continue migrating the approved deck, draft, review, opponent, voice, and data capabilities into the overlay.
- **Not Applicable from 2026-10-18 19:04:46 SAST:** The confidence-only condition for running tests is superseded by the user's success-guarantee condition below. The change is necessary because the user explicitly distinguished confidence from guaranteed test success. Do not claim verification that was not performed.

## 2026-10-18 19:04:46 SAST — User clarification: guaranteed test success

- The user directed: “i said no tests unless you can garuntee success.”
- Do not add or run tests unless success can be guaranteed. Confidence alone does not satisfy this condition.
- When this condition is not met, continue source inspection and inspection of existing runtime evidence without running tests. Do not describe the older blanket ban or confidence-only rule as the current test-running condition.
