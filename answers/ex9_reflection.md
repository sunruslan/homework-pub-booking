# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your answer

In my `make ex7-real` run (`sess_710ea14ce8d1`, 2026-05-24), the planner’s decision to involve the structured half shows up in ticket **tk_473e0ed1** (`planner.plan`). After `planner.called` (trace line 2), it emitted two subgoals (trace line 3: `num_subgoals: 2`).

The handoff intent is **sg_2** in `logs/tickets/tk_473e0ed1/raw_output.json`:

```json
{
  "id": "sg_2",
  "description": "Hand off the booking details to structured for confirmation.",
  "success_criterion": "handoff_to_structured is successfully called with valid data including venue_id, date, time, party_size, and deposit.",
  "depends_on": ["sg_1"],
  "assigned_half": "loop"
}
```

Note that `assigned_half` is `"loop"`, not `"structured"`. That matches our `SESSION.md` instruction: *“Do NOT create planner subgoals with assigned_half ‘structured’.”* Earlier broken runs assigned sg_2 to `"structured"`, which made `LoopHalf` auto-hand off with empty booking data. Here the planner kept both subgoals on the loop half but dedicated sg_2 solely to calling `handoff_to_structured` after research.

**What signal caused the decision?** The task text wired into the session. `SESSION.md` (and the `task_preview` on trace line 2) requires a two-step workflow: (1) `venue_search`, (2) mandatory `handoff_to_structured` with a complete `data` object (`venue_id`, `date`, `time`, `party_size`, `deposit`). The planner mirrored that split: sg_1 searches Haymarket/Old Town for 12 guests; sg_2 confirms via the structured half once a venue exists.

Execution followed the plan. sg_1’s executor found **The Royal Oak** in Old Town (trace lines 4–5). sg_2’s executor then called `handoff_to_structured` with `royal_oak`, `2026-04-25`, `19:30`, `party_size: 12` (trace line 9; `tk_58e62e53/raw_output.json`). The bridge wrote `ipc/handoff_to_structured.json` and moved `loop → structured` (trace line 10). Rasa rejected the booking with `party_too_large` (trace line 11), triggering round 2.

So the planner’s “handoff decision” is prose in the subgoal plan (sg_2’s description and success criterion), driven by explicit Ex7 task wording—not by assigning a subgoal to the structured half in `assigned_half`.

### Citations

- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/SESSION.md` (lines 17–19: required `handoff_to_structured` workflow)
- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/logs/trace.jsonl` (lines 2–3: planner invoked; lines 9–11: handoff tool and Rasa rejection)
- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/logs/tickets/tk_473e0ed1/raw_output.json` (sg_2 handoff subgoal)
- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/logs/tickets/tk_473e0ed1/manifest.json` (ticket `tk_473e0ed1`, model `Qwen/Qwen3-Next-80B-A3B-Thinking`)
- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/logs/tickets/tk_58e62e53/raw_output.json` (executor `handoff_to_structured` call)
- `sessions/examples/ex7-handoff-bridge/sess_710ea14ce8d1/ipc/handoff_to_structured.json` (forward handoff payload to Rasa)

---

## Q2 — Dataflow integrity catch

### Your answer

I ran `make ex5` (session **sess_635e16ae529d**) and then deliberately corrupted the flyer to simulate an LLM (or human editor) “fixing” the numbers to look policy-compliant.

**Setup.** The executor called `calculate_cost(haymarket_tap, party_size=6, duration_hours=3, catering_tier='bar_snacks')`, which logged **total £556** and **deposit £111** (trace line 5). The scripted `generate_flyer` call still passed `total_gbp: 540` and `deposit_required_gbp: 0` into the HTML, so the published flyer already disagreed with pricing tools—a realistic failure mode.

**Planted fabrication.** I copied `workspace/flyer.html` to `workspace/flyer_fabricated.html` and changed only the cost lines:

- `data-testid="total_gbp"`: **£540 → £560**
- `data-testid="deposit_required_gbp"`: **£0 → £112**

A human reviewer would likely accept this: **£112 is exactly 20% of £560**, which matches `sample_data/catering.json` deposit policy for totals between £300 and £1000. Nothing looks absurd; you would need to re-run `calculate_cost` or grep the trace to spot the lie.

**Integrity result.** After replaying the three read tools to populate `_TOOL_CALL_LOG` (same inputs as the session), `verify_dataflow` on the fabricated HTML returned `ok=False` with `unverified_facts` including **`£560`** and **`£112`**—neither value appears in any tool output (ground truth remains **556 / 111**). Venue, weather, and party facts still verified.

**How to reproduce the test case.**

1. `cd homework-pub-booking && make ex5` — note session id under `sessions/examples/ex5-edinburgh-research/`.
2. Confirm trace line for `calculate_cost` shows £556 / £111.
3. In that session’s `workspace/flyer.html`, set Total to **£560** and Deposit to **£112** (or use the saved `flyer_fabricated.html`).
4. In Python: `clear_log()`, then call `venue_search('Haymarket', 6, 800)`, `get_weather('edinburgh', '2026-04-25')`, `calculate_cost('haymarket_tap', 6, 3, 'bar_snacks')`, then `verify_dataflow(flyer_text)`.
5. Expect `ok=False` and unverified `£560`, `£112`.

The check wins because it diffs flyer facts against `_TOOL_CALL_LOG`, not against “does this obey the deposit formula.”

### Citations

- `sessions/examples/ex5-edinburgh-research/sess_635e16ae529d/logs/trace.jsonl` (line 5: `calculate_cost` → £556 / £111; line 6: `generate_flyer` still used 540 / 0)
- `sessions/examples/ex5-edinburgh-research/sess_635e16ae529d/workspace/flyer.html` (original totals £540 / £0)
- `sessions/examples/ex5-edinburgh-research/sess_635e16ae529d/workspace/flyer_fabricated.html` (planted £560 / £112)
- `sessions/examples/ex5-edinburgh-research/sess_635e16ae529d/logs/integrity_fabrication_probe.json` (probe output: `integrity_ok: false`, unverified money facts)
- `starter/edinburgh_research/sample_data/catering.json` (deposit_policy explaining why £112 “looks right”)

---

## Q3 — First production failure

### Your answer

**Primitive:** **IPC atomic rename** (forward handoff written to `ipc/handoff_to_structured.json` as the sole contract between loop and structured halves).

**Failure mode:** **Semantically empty forward handoff** — the file is valid JSON and atomically complete, but `data` carries `venue_search` output (`search_results`, `suggested_actions`) instead of the confirm-booking shape Rasa expects (`venue_id`, `date`, `time`, `party_size`, `deposit`). A human skimming the handoff sees plausible Haymarket Tap metadata and assumes “the agent did the research”; the structured half cannot confirm anything.

**Why this hits first in production.** Our Ex7 task is built around large Friday parties (12 guests) where Haymarket venues cap at 8 seats. Under real Nebius traffic we already saw the loop “escalate” by dumping search blobs into `handoff_to_structured` rather than picking a `venue_id` (session **sess_fae375db36bd**: trace lines 12–14 show `data` with only `party_size`, `area`, `time`; Rasa returns `normalisation failed: missing venue_id` three times and the bridge ends at `max_rounds`).

**Simulated reproduction.** I ran `uv run python scripts/simulate_q3_ipc_failure.py`, which scripts the same mistake offline. Session **sess_23ce0baaa5ca** exhausts three rounds: each round calls `venue_search`, then `handoff_to_structured` with `received_keys: ["search_results", "suggested_actions"]` only. The bridge now blocks before Rasa (`bridge.handoff_rejected` on trace lines 6, 13, 20), but the on-disk IPC file still shows the bad payload (ops would open it during an incident). Outcome: `state: failed`, `max_rounds=3 exceeded`.

**Implementation gap.** `validate_booking_handoff()` in `starter/handoff_bridge/bridge.py` catches missing fields, yet nothing stops the executor tool from writing a full IPC file first; without schema enforcement at the tool boundary, production load will keep producing readable but unusable handoffs until someone audits `ipc/handoff_to_structured.json`.

### Citations

- `sessions/examples/ex7-handoff-bridge/sess_23ce0baaa5ca/logs/trace.jsonl` (lines 5–6, 20–21: bad handoff + `bridge.handoff_rejected`; session ends failed)
- `sessions/examples/ex7-handoff-bridge/sess_23ce0baaa5ca/ipc/handoff_to_structured.json` (`data.search_results` instead of booking fields)
- `sessions/examples/ex7-handoff-bridge/sess_23ce0baaa5ca/session.json` (`state: failed`, `max_rounds=3 exceeded`)
- `sessions/examples/ex7-handoff-bridge/sess_fae375db36bd/logs/trace.jsonl` (lines 12–14, 25: real LLM run — incomplete `data`, `missing venue_id` rejections)
- `sessions/examples/ex7-handoff-bridge/sess_fae375db36bd/logs/tickets/tk_70e67e52/raw_output.json` (sg_2 `assigned_half: "structured"` — alternate trigger for empty auto-handoff)
- `starter/handoff_bridge/bridge.py` (`validate_booking_handoff`, `write_handoff`)
- `scripts/simulate_q3_ipc_failure.py` (reproducible failure script)
