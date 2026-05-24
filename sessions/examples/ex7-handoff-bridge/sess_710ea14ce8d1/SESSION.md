# Session sess_710ea14ce8d1

**Scenario:** ex7-handoff-bridge
**Created:** 2026-05-24T11:47:01.614210+00:00

## Your task

(The loop half reads this file on every turn. The initial task description
has been written below by the orchestrator when the session was created.
Additional per-session instructions — constraints, identity, voice — can
be added by the scenario author.)

## Task description

Book a venue for 12 people in Haymarket, Edinburgh, Friday 2026-04-25 at 19:30.

WORKFLOW (loop half only — every planner subgoal MUST use assigned_half: "loop"):
1. venue_search — if nothing fits 12 in Haymarket, try Old Town or reduce party_size
2. handoff_to_structured — REQUIRED; the bridge will not call Rasa without it

handoff_to_structured MUST include a "data" object with ALL of:
  - venue_id: from search (e.g. "royal_oak", "Haymarket Tap")
  - date: "2026-04-25"
  - time: "19:30"
  - party_size: int or str (<= 8 for auto-confirm)
  - deposit: "£0" or 0

Do NOT create planner subgoals with assigned_half "structured". That triggers an automatic
handoff with no booking fields and fails validation.

If structured rejects (party_too_large, deposit_too_high), search again and hand off with
a revised booking (party_size <= 8, deposit <= £300).


## Constraints

- Be honest when you do not know something.
- Prefer reading memory over guessing.
- When the task is ambiguous, ask for clarification rather than inventing an answer.
