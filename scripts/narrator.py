"""narrator.py — turn a session directory into a story.

Two modes:
  narrator.py --session <id>        # post-hoc: read trace.jsonl, narrate
  narrator.py --live <path>         # tail trace.jsonl as it's written

The narrator is the pedagogy layer. Students run their scenario
(`make ex5-real`), then run `make narrate SESSION=<id>` and see
what the agent actually did, in English, with icons and timings.

Narration is just templating over trace events. Every event type
has a one-line narrator template. Tool calls get per-tool templates
that unpack their specific arguments and outputs meaningfully.

If we don't have a template for an event, we print a dim one-liner
and move on. Never raise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


class _C:
    _on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls._on else s

    @classmethod
    def g(cls, s: str) -> str:
        return cls._w("32", s)

    @classmethod
    def r(cls, s: str) -> str:
        return cls._w("31", s)

    @classmethod
    def y(cls, s: str) -> str:
        return cls._w("33", s)

    @classmethod
    def b(cls, s: str) -> str:
        return cls._w("36", s)  # cyan for "bold-ish"

    @classmethod
    def d(cls, s: str) -> str:
        return cls._w("2", s)

    @classmethod
    def bold(cls, s: str) -> str:
        return cls._w("1", s)


# ─────────────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────────────


def _fmt_time(ts: str) -> str:
    """Convert ISO timestamp to HH:MM:SS."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return "--:--:--"


def _narrate_tool_call(event: dict) -> list[str]:
    """Return a list of lines narrating a single tool call event."""
    payload = event.get("payload", {})
    tool = payload.get("tool", "?")
    args = payload.get("arguments", {}) or {}
    success = payload.get("success", True)
    summary = payload.get("summary", "")

    lines: list[str] = []

    if tool == "venue_search":
        near = args.get("near", "?")
        party = args.get("party_size", "?")
        lines.append(f"  🔍  {_C.bold('venue_search')} " + _C.d(f"near={near!r}, party={party}"))
    elif tool == "get_weather":
        city = args.get("city", "?")
        date = args.get("date", "?")
        lines.append(f"  🌤️   {_C.bold('get_weather')} " + _C.d(f"city={city!r}, date={date!r}"))
    elif tool == "calculate_cost":
        venue = args.get("venue_id", "?")
        party = args.get("party_size", "?")
        lines.append(
            f"  💷  {_C.bold('calculate_cost')} " + _C.d(f"venue={venue!r}, party={party}")
        )
    elif tool == "generate_flyer":
        details = args.get("event_details", {})
        venue_name = details.get("venue_name", "?")
        total = details.get("total_gbp", "?")
        lines.append(
            f"  ✍️   {_C.bold('generate_flyer')} " + _C.d(f"venue={venue_name!r}, total=£{total}")
        )
    elif tool == "handoff_to_structured":
        lines.append(f"  🤝  {_C.bold('handoff_to_structured')} ← passing to Rasa")
    elif tool == "complete_task":
        lines.append(f"  🏁  {_C.bold('complete_task')} ← agent says it's done")
    elif tool == "pub_search":
        lines.append(
            f"  🍺  {_C.bold('pub_search')} "
            + _C.d(f"city={args.get('city', '?')!r}, near={args.get('near', '?')!r}")
        )
    elif tool == "pub_availability":
        lines.append(
            f"  📅  {_C.bold('pub_availability')} "
            + _C.d(f"pub={args.get('pub_id', '?')!r}, party={args.get('party', '?')}")
        )
    elif tool in ("write_file", "read_file", "list_files"):
        lines.append(f"  📄  {_C.bold(tool)} " + _C.d(str(args)[:80]))
    else:
        lines.append(f"  🔧  {_C.bold(tool)} " + _C.d(str(args)[:80]))

    if summary:
        lines.append(f"      {_C.d('→ ' + summary[:100])}")
    if not success:
        lines.append(f"      {_C.r('✗ tool reported failure')}")
    return lines


def _narrate_event(event: dict) -> list[str]:
    """Narrate ONE trace event. Return a list of lines."""
    etype = event.get("event_type", "")
    ts = _fmt_time(event.get("timestamp", ""))
    payload = event.get("payload", {}) or {}

    if etype == "session.created":
        scenario = payload.get("scenario", "?")
        return [f"{_C.d(ts)}  📖  Session opened for scenario {_C.bold(scenario)}"]

    if etype == "planner.called":
        return [f"{_C.d(ts)}  🧠  Planner is thinking about how to break this down..."]

    if etype == "planner.produced_subgoals":
        n = payload.get("num_subgoals", payload.get("count", "?"))
        return [f"{_C.d(ts)}  📋  Planner produced {_C.bold(str(n))} subgoal(s)"]

    if etype == "executor.turn_started":
        turn = payload.get("turn", "?")
        return [f"{_C.d(ts)}  ⚙️   Executor turn {turn}"]

    if etype == "executor.tool_called":
        lines = [f"{_C.d(ts)}  — tool call —"]
        lines.extend(_narrate_tool_call(event))
        return lines

    if etype == "executor.complete":
        reason = payload.get("reason", "?")
        return [f"{_C.d(ts)}  🏁  Executor finished. Reason: {_C.bold(reason)}"]

    if etype == "session.state_changed":
        from_s = payload.get("from", "?")
        to_s = payload.get("to", "?")
        return [
            f"{_C.d(ts)}  ↪️   State: {_C.d(from_s)} → {_C.b(to_s)}"
            + (_C.d(f"  (round {payload['round']})") if "round" in payload else "")
        ]

    if etype == "bridge.round_start":
        rnd = payload.get("round", "?")
        half = payload.get("half", "?")
        return [f"{_C.d(ts)}  🔁  Bridge round {_C.bold(str(rnd))} — starting {_C.b(half)} half"]

    if etype == "voice.utterance_in":
        text = payload.get("text", "")[:80]
        return [f"{_C.d(ts)}  🗣️   You said: {_C.bold(text)!r}"]

    if etype == "voice.utterance_out":
        text = payload.get("text", "")[:80]
        return [f"{_C.d(ts)}  🤖  Agent said: {_C.bold(text)!r}"]

    if etype == "session.completed":
        return [f"{_C.d(ts)}  {_C.g('✓')}  Session {_C.bold('completed')} cleanly"]

    if etype == "session.failed":
        return [
            f"{_C.d(ts)}  {_C.r('✗')}  Session {_C.bold('failed')}: "
            + _C.d(payload.get("reason", "(no reason)")[:100])
        ]

    # Default: dim one-liner so unknown events don't disappear silently
    return [_C.d(f"{ts}  {etype}")]


# ─────────────────────────────────────────────────────────────────────
# Session discovery
# ─────────────────────────────────────────────────────────────────────


def _platform_data_dir() -> Path:
    """Where sovereign-agent's example_sessions_dir writes on this OS."""
    if override := os.environ.get("SOVEREIGN_AGENT_DATA_DIR"):
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "sovereign-agent"
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "sovereign-agent"
    return (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        / "sovereign-agent"
    )


def find_session(session_id_or_path: str) -> Path | None:
    """Resolve a session ID (or partial prefix) to its directory.

    Search order:
      1. Absolute/relative path
      2. ./sessions/<id>
      3. <platform data dir>/examples/*/<id>
    """
    cand = Path(session_id_or_path)
    if cand.is_absolute() and cand.exists():
        return cand
    if cand.exists() and cand.is_dir():
        return cand.resolve()

    # Local sessions/
    local = Path("sessions") / session_id_or_path
    if local.exists():
        return local.resolve()
    for sub in (
        Path("sessions").glob(f"*{session_id_or_path}*") if Path("sessions").exists() else []
    ):
        if sub.is_dir():
            return sub.resolve()

    # Platform user-data dir
    data_root = _platform_data_dir()
    if data_root.exists():
        for ex_dir in data_root.glob("examples/*"):
            for sub in ex_dir.glob(f"*{session_id_or_path}*"):
                if sub.is_dir():
                    return sub.resolve()

    return None


# ─────────────────────────────────────────────────────────────────────
# Narration drivers
# ─────────────────────────────────────────────────────────────────────


def narrate_session(session_dir: Path) -> int:
    """Post-hoc narration of a completed session."""
    trace = session_dir / "logs" / "trace.jsonl"
    if not trace.exists():
        print(_C.r(f"✗ no trace at {trace}"))
        return 1

    print()
    print(_C.b("━" * 72))
    print(f"  📖  {_C.bold(session_dir.name)}")
    print(_C.d(f"       {session_dir}"))
    print(_C.b("━" * 72))
    print()

    for line in trace.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for out_line in _narrate_event(event):
            print(out_line)

    print()
    print(_C.b("━" * 72))
    workspace = session_dir / "workspace"
    if workspace.exists():
        artifacts = sorted(p for p in workspace.iterdir() if p.is_file())
        if artifacts:
            print(_C.bold("  Artifacts"))
            for p in artifacts:
                print(f"    {_C.g('📄')} {p.relative_to(session_dir)} ({p.stat().st_size} bytes)")

    print()
    print(_C.d("  🔬 Inspect the raw trace:"))
    print(_C.d(f"       cat {trace}"))
    print(_C.d("  📂 Browse the whole session:"))
    print(_C.d(f"       ls -R {session_dir}"))
    print()
    return 0


def narrate_live(session_dir: Path, timeout_s: float = 120.0) -> int:
    """Tail trace.jsonl and narrate as events land."""
    trace = session_dir / "logs" / "trace.jsonl"
    deadline = time.monotonic() + timeout_s

    print()
    print(_C.b("━" * 72))
    print(f"  📖 (live) {_C.bold(session_dir.name)}")
    print(_C.d(f"       tailing {trace}"))
    print(_C.b("━" * 72))
    print()

    seen = 0
    while time.monotonic() < deadline:
        if not trace.exists():
            time.sleep(0.3)
            continue
        lines = trace.read_text(encoding="utf-8").splitlines()
        while seen < len(lines):
            line = lines[seen].strip()
            seen += 1
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("event_type", "")
            for out_line in _narrate_event(event):
                print(out_line)
            if etype in ("session.completed", "session.failed"):
                return 0
        time.sleep(0.3)

    print(_C.y(f"⏱  live narration timed out after {timeout_s:.0f}s"))
    return 0


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="Narrate a sovereign-agent session.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--session", help="session id or directory to narrate (post-hoc)")
    grp.add_argument("--live", help="tail a session dir as it's written to")
    grp.add_argument(
        "--latest",
        action="store_true",
        help="narrate the most recent session from this repo (searches sessions/ + platform data dir)",
    )
    p.add_argument("--timeout", type=float, default=120.0, help="live mode timeout (seconds)")
    args = p.parse_args()

    if args.latest:
        # Find the newest session across sessions/ and the platform data dir
        candidates: list[Path] = []
        if Path("sessions").exists():
            candidates.extend(Path("sessions").glob("sess_*"))
        data_root = _platform_data_dir()
        if data_root.exists():
            candidates.extend(data_root.glob("examples/*/sess_*"))
        candidates = [c for c in candidates if c.is_dir()]
        if not candidates:
            print(_C.r("✗ no sessions found. Run a scenario first (e.g. make ex5-real)."))
            return 1
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return narrate_session(candidates[0])

    if args.session:
        resolved = find_session(args.session)
        if resolved is None:
            print(_C.r(f"✗ session {args.session!r} not found."))
            print(_C.d("  Try: ls sessions/  or  make narrate-latest"))
            return 1
        return narrate_session(resolved)

    if args.live:
        resolved = find_session(args.live)
        if resolved is None:
            print(_C.r(f"✗ {args.live!r} not found (yet?) — retrying"))
            resolved = Path(args.live).resolve()  # tail the path anyway
        return narrate_live(resolved, timeout_s=args.timeout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
