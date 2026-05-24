"""Ex5 — reference solution for integrity.py.

verify_dataflow's job: for every concrete fact in the flyer, confirm
that some tool call in the session actually produced that value. If
a fact exists in the flyer but not in any tool output, it's fabrication.

Two competing failure modes to balance:
  - Too lenient → misses fabrications (grader plants £9999; must catch it)
  - Too strict → rejects legitimate flyers (fails the "accepts real flyer" test)

This implementation leans slightly strict but uses the scalar-matching
`fact_appears_in_log` helper provided in the starter to tolerate common
variations (leading £, trailing C, case differences).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ToolCallRecord:
    tool_name: str
    arguments: dict
    output: dict
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


_TOOL_CALL_LOG: list[ToolCallRecord] = []


def record_tool_call(tool_name: str, arguments: dict, output: dict) -> None:
    _TOOL_CALL_LOG.append(
        ToolCallRecord(tool_name=tool_name, arguments=dict(arguments), output=dict(output))
    )


def clear_log() -> None:
    _TOOL_CALL_LOG.clear()


@dataclass
class IntegrityResult:
    ok: bool
    unverified_facts: list[str] = field(default_factory=list)
    verified_facts: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "unverified_facts": self.unverified_facts,
            "verified_facts": self.verified_facts,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_money_facts(text: str) -> list[str]:
    """Find all £<number> occurrences, HTML tags stripped or not."""
    # Strip HTML tags first so e.g. <dd>£540</dd> matches cleanly.
    stripped = re.sub(r"<[^>]+>", " ", text)
    return re.findall(r"£\d+(?:\.\d+)?", stripped)


def extract_temperature_facts(text: str) -> list[str]:
    """Find temperature mentions (number followed by °C or C), with optional leading words."""
    stripped = re.sub(r"<[^>]+>", " ", text)
    found: list[str] = []
    for m in re.finditer(r"(\b(?:\w+\s+){0,3}\d+\s*°?\s*[Cc]\b)", stripped):
        found.append(m.group(1).strip())
    # Fallback: bare numbers still checked against tool logs.
    for m in re.finditer(r"(\d+)\s*°?\s*[Cc]\b", stripped):
        token = m.group(1)
        if token not in found and not any(token in f for f in found):
            found.append(token)
    return list(dict.fromkeys(found))


def extract_condition_facts(text: str) -> list[str]:
    """Find weather condition keywords."""
    stripped = re.sub(r"<[^>]+>", " ", text)
    tl = stripped.lower()
    known = ("sunny", "rainy", "cloudy", "partly_cloudy", "partly cloudy")
    return [c for c in known if c in tl]


def extract_testid_facts(text: str) -> dict[str, str]:
    """For HTML flyers that use data-testid, extract {testid: value} pairs.

    This is the preferred path for HTML — it gives us structured facts
    (e.g. {'total': '£540', 'deposit': '£0'}) instead of loose regex
    matches. The solution flyer ships with data-testid on every fact.
    """
    pattern = re.compile(
        r'<[^>]+data-testid="([^"]+)"[^>]*>([^<]+)</[^>]+>',
        re.IGNORECASE,
    )
    return {m.group(1): m.group(2).strip() for m in pattern.finditer(text)}


def extract_venue_facts(text: str) -> list[str]:
    """Find venue names from plain-text flyers (e.g. 'Venue: Haymarket Tap')."""
    stripped = re.sub(r"<[^>]+>", " ", text)
    names: list[str] = []
    for m in re.finditer(r"(?i)\bvenue\s*:\s*(.+?)(?:\.\s|\n|$)", stripped):
        name = m.group(1).strip()
        if name and len(name) > 2:
            names.append(name)
    return names


def _normalise_fact(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().strip("£°c "))


def fact_appears_in_log(fact: Any, log: list[ToolCallRecord] | None = None) -> bool:
    records = log if log is not None else _TOOL_CALL_LOG
    target = _normalise_fact(str(fact))
    if not target:
        return False

    if " " in target.strip():
        def _scan_substring(obj: Any) -> bool:
            if isinstance(obj, str):
                hay = _normalise_fact(obj)
                return target in hay or hay in target
            if isinstance(obj, dict):
                return any(_scan_substring(v) for v in obj.values())
            if isinstance(obj, (list, tuple, set)):
                return any(_scan_substring(v) for v in obj)
            return False

        return any(_scan_substring(r.output) or _scan_substring(r.arguments) for r in records)

    def _scan(obj: Any) -> bool:
        if isinstance(obj, (str, int, float)):
            candidate = _normalise_fact(str(obj))
            if candidate == target:
                return True
            return candidate.replace("_", " ") == target.replace("_", " ")
        if isinstance(obj, dict):
            return any(_scan(v) for v in obj.values())
        if isinstance(obj, (list, tuple, set)):
            return any(_scan(v) for v in obj)
        return False

    return any(_scan(r.output) or _scan(r.arguments) for r in records)


def _collect_facts(flyer_content: str) -> list[str]:
    facts: list[str] = []

    testid_facts = extract_testid_facts(flyer_content)
    if testid_facts:
        for key, value in testid_facts.items():
            if key in {"title", "venue_name"} and value:
                facts.append(value)
            elif key in {"total_gbp", "deposit_required_gbp"}:
                facts.extend(extract_money_facts(value))
            elif key == "temperature_c":
                facts.extend(extract_temperature_facts(value))
            elif key == "condition":
                facts.append(value.replace("_", " "))
            elif key == "party_size" and value.strip().isdigit():
                facts.append(value.strip())
            elif key in {"date", "time", "venue_address"} and value.strip():
                facts.append(value.strip())
    else:
        facts.extend(extract_venue_facts(flyer_content))

    facts.extend(extract_money_facts(flyer_content))
    facts.extend(extract_temperature_facts(flyer_content))
    facts.extend(extract_condition_facts(flyer_content))

    seen: set[str] = set()
    deduped: list[str] = []
    for fact in facts:
        key = _normalise_fact(fact)
        if key and key not in seen:
            seen.add(key)
            deduped.append(fact)
    return deduped


# ---------------------------------------------------------------------------
# verify_dataflow — the main check
# ---------------------------------------------------------------------------
def verify_dataflow(flyer_content: str) -> IntegrityResult:
    if not flyer_content or not flyer_content.strip():
        return IntegrityResult(ok=True, summary="no facts to verify (empty flyer)")

    deduped = _collect_facts(flyer_content)

    if not deduped:
        return IntegrityResult(
            ok=True, summary="no extractable facts in flyer (verified vacuously)"
        )

    verified: list[str] = []
    unverified: list[str] = []
    for fact in deduped:
        if fact_appears_in_log(fact):
            verified.append(fact)
        else:
            unverified.append(fact)

    if unverified:
        return IntegrityResult(
            ok=False,
            unverified_facts=unverified,
            verified_facts=verified,
            summary=(
                f"dataflow FAIL: {len(unverified)} unverified fact(s): "
                f"{unverified[:5]}" + ("..." if len(unverified) > 5 else "")
            ),
        )

    return IntegrityResult(
        ok=True,
        verified_facts=verified,
        summary=f"dataflow OK: verified {len(verified)} fact(s) against tool outputs",
    )


__all__ = [
    "IntegrityResult",
    "ToolCallRecord",
    "_TOOL_CALL_LOG",
    "clear_log",
    "extract_condition_facts",
    "extract_money_facts",
    "extract_temperature_facts",
    "extract_testid_facts",
    "extract_venue_facts",
    "fact_appears_in_log",
    "record_tool_call",
    "verify_dataflow",
]
