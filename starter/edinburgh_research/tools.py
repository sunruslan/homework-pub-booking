"""Ex5 tools. Five scenario tools plus sovereign-agent builtins for Edinburgh booking.

Tool order (Ex5 scenario):
  0. remind_session_task — call FIRST (and again if you forget constraints); reads SESSION.md
  1. venue_search — find candidate pubs (parallel-safe read)
  2. get_weather — weather for the event date (parallel-safe read)
  3. calculate_cost — price the chosen venue (parallel-safe read; needs venue_id)
  4. generate_flyer — write workspace/flyer.html (NOT parallel-safe; run alone after 1–3)
  5. complete_task — built-in; only after the flyer exists

Each research tool logs to _TOOL_CALL_LOG via record_tool_call() for verify_dataflow().
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import Any

from sovereign_agent.errors import ToolError
from sovereign_agent.session.directory import Session
from sovereign_agent.tools.registry import ToolRegistry, ToolResult, _RegisteredTool

from starter.edinburgh_research.integrity import (
    _TOOL_CALL_LOG,
    ToolCallRecord,
    record_tool_call,
)

_SAMPLE_DATA = Path(__file__).parent / "sample_data"
_VENUES_PATH = _SAMPLE_DATA / "venues.json"
_WEATHER_PATH = _SAMPLE_DATA / "weather.json"
_CATERING_PATH = _SAMPLE_DATA / "catering.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VALID_TIERS = frozenset({"drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal"})
_FLYER_REQUIRED_KEYS = (
    "venue_name",
    "venue_address",
    "date",
    "time",
    "party_size",
    "condition",
    "temperature_c",
    "total_gbp",
    "deposit_required_gbp",
)
_MAX_VENUE_SEARCH_CALLS = 3


def _venue_floor_gbp(venue: dict) -> int:
    return int(venue.get("hire_fee_gbp", 0) or 0) + int(venue.get("min_spend_gbp", 0) or 0)


def _prior_successful_venue_search() -> ToolCallRecord | None:
    """Most recent venue_search in this session that returned at least one venue."""
    for record in reversed(_TOOL_CALL_LOG):
        if record.tool_name != "venue_search":
            continue
        if record.output.get("error") or record.output.get("reused_from_previous_search"):
            continue
        results = record.output.get("results")
        if isinstance(results, list) and len(results) > 0:
            return record
    return None


def _open_venues(venues_raw: list) -> list[dict]:
    return [v for v in venues_raw if isinstance(v, dict) and v.get("open_now")]


def _venues_in_area(venues: list[dict], near_lower: str) -> list[dict]:
    return [v for v in venues if near_lower in str(v.get("area", "")).lower()]


def _filter_by_party_and_budget(
    venues: list[dict], party: int, budget: int
) -> list[dict]:
    results: list[dict] = []
    for venue in venues:
        seats = venue.get("seats_available_evening", 0)
        if not isinstance(seats, int) or seats < party:
            continue
        if _venue_floor_gbp(venue) > budget:
            continue
        results.append(venue)
    return results


def _describe_area_venues(venues: list[dict], party: int, budget: int) -> list[dict]:
    """Per-venue availability in an area when party/budget filters removed everyone."""
    options: list[dict] = []
    for venue in venues:
        seats = venue.get("seats_available_evening", 0)
        if not isinstance(seats, int):
            seats = 0
        floor = _venue_floor_gbp(venue)
        blocked: list[str] = []
        if seats < party:
            blocked.append(f"party_size (max {seats} seats)")
        if floor > budget:
            blocked.append(f"budget (venue floor £{floor})")
        options.append(
            {
                "id": venue.get("id"),
                "name": venue.get("name"),
                "seats_available_evening": seats,
                "venue_floor_gbp": floor,
                "blocked_by": blocked,
            }
        )
    return options


def _format_area_options_summary(area: str, options: list[dict]) -> str:
    parts: list[str] = []
    for opt in options:
        name = opt.get("name") or opt.get("id") or "?"
        seats = opt.get("seats_available_evening", "?")
        floor = opt.get("venue_floor_gbp", "?")
        blocked = opt.get("blocked_by") or []
        if blocked:
            parts.append(f"{name} ({seats} seats, floor £{floor}; blocked: {', '.join(blocked)})")
        else:
            parts.append(f"{name} ({seats} seats, floor £{floor})")
    return f"In {area}: " + "; ".join(parts)


def _tool_failure(
    *,
    summary: str,
    code: str,
    message: str,
    context: dict | None = None,
    output: dict | None = None,
) -> ToolResult:
    err = ToolError(code=code, message=message, context=context or {})
    return ToolResult(
        success=False,
        output=output or {"error_code": code, **(context or {})},
        summary=summary,
        error=err,
    )


def _load_json_fixture(path: Path, label: str) -> tuple[dict | list | None, ToolResult | None]:
    if not path.is_file():
        return None, _tool_failure(
            summary=(
                f"{label}: fixture missing at {path.name}. "
                "Restore sample_data/ from the homework repo; the grader relies on these files."
            ),
            code="SA_TOOL_DEPENDENCY_MISSING",
            message=f"fixture not found: {path}",
            context={"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, _tool_failure(
            summary=(
                f"{label}: {path.name} is not valid JSON ({exc}). "
                "Do not edit sample_data/; re-checkout the starter fixtures."
            ),
            code="SA_TOOL_DEPENDENCY_MISSING",
            message=f"fixture corrupt: {path}",
            context={"path": str(path), "detail": str(exc)},
        )
    return data, None


def _coerce_positive_int(
    value: Any,
    *,
    name: str,
    minimum: int = 1,
) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, f"{name} must be an integer, not a boolean."
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        return None, f"{name} must be a positive integer (got {type(value).__name__})."
    if n < minimum:
        return None, f"{name} must be >= {minimum} (got {n})."
    return n, None


def _coerce_non_negative_int(value: Any, *, name: str) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, f"{name} must be an integer, not a boolean."
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        return None, f"{name} must be a non-negative integer (got {type(value).__name__})."
    if n < 0:
        return None, f"{name} must be >= 0 (got {n})."
    return n, None


def _normalise_near(near: Any) -> tuple[str | None, str | None]:
    if not isinstance(near, str):
        return None, "near must be a non-empty string (e.g. 'Haymarket')."
    cleaned = near.strip()
    if not cleaned:
        return None, "near must be a non-empty string after trimming whitespace."
    return cleaned, None


def _normalise_city(city: Any) -> tuple[str | None, str | None]:
    if not isinstance(city, str):
        return None, "city must be a non-empty string (e.g. 'edinburgh')."
    cleaned = city.strip()
    if not cleaned:
        return None, "city must be a non-empty string after trimming whitespace."
    return cleaned.lower(), None


def _normalise_date(date: Any) -> tuple[str | None, str | None]:
    if not isinstance(date, str):
        return None, "date must be a string in YYYY-MM-DD format."
    cleaned = date.strip()
    if not _DATE_RE.match(cleaned):
        return None, f"date must match YYYY-MM-DD (got {date!r})."
    return cleaned, None


def _deposit_for_total(total_gbp: int, policy: dict) -> int:
    if total_gbp < 300:
        return 0
    if total_gbp <= 1000:
        return round(total_gbp * 0.20)
    return round(total_gbp * 0.30)


def _parse_task_from_session_md(content: str) -> str:
    """Extract the task description block from SESSION.md."""
    match = re.search(
        r"## Task description\s*\n+(.*?)(?:\n## |\Z)",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return content.strip()


def remind_session_task(session: Session) -> ToolResult:
    """Read the authoritative task from this session's SESSION.md.

    Call this FIRST at the start of Ex5, before venue_search or any other
    research tool. Call it again whenever you are unsure of party size, area,
    dates, budget, or the required tool sequence — the executor subgoal text
    may not repeat the full homework brief.

    Reads ``<session.directory>/SESSION.md`` (same file written by create_session).
    """
    args: dict = {}
    session_md_path = session.session_md_path

    if not session_md_path.is_file():
        output = {
            "error": "session_md_missing",
            "path": str(session_md_path),
            "session_id": session.session_id,
        }
        record_tool_call("remind_session_task", args, output)
        return _tool_failure(
            summary=(
                f"remind_session_task: SESSION.md not found at {session_md_path}. "
                "The session may be corrupt — check session.directory."
            ),
            code="SA_TOOL_DEPENDENCY_MISSING",
            message="SESSION.md not found",
            context=output,
            output=output,
        )

    try:
        raw_md = session_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        output = {"error": str(exc), "path": str(session_md_path)}
        record_tool_call("remind_session_task", args, output)
        return _tool_failure(
            summary=f"remind_session_task: could not read SESSION.md — {exc}",
            code="SA_TOOL_EXECUTION_FAILED",
            message="failed to read SESSION.md",
            context=output,
            output=output,
        )

    task_text = _parse_task_from_session_md(raw_md)
    if not task_text or task_text == "(no task description provided)":
        output = {
            "error": "empty_task_description",
            "path": "SESSION.md",
            "session_id": session.session_id,
        }
        record_tool_call("remind_session_task", args, output)
        return _tool_failure(
            summary=(
                "remind_session_task: SESSION.md has no task description. "
                "Check create_session(task=...) in run.py."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="empty task in SESSION.md",
            output=output,
        )

    output = {
        "path": "SESSION.md",
        "session_id": session.session_id,
        "scenario": session.state.scenario,
        "task": task_text,
        "char_count": len(task_text),
    }
    record_tool_call("remind_session_task", args, output)

    preview = task_text.replace("\n", " ")[:120]
    if len(task_text) > 120:
        preview += "..."
    summary = (
        f"remind_session_task: loaded SESSION.md for {session.session_id} "
        f"({len(task_text)} chars). Follow this task exactly: {preview}"
    )
    return ToolResult(success=True, output=output, summary=summary)


def venue_search(near: str, party_size: int, budget_max_gbp: int = 1000) -> ToolResult:
    """Search Edinburgh venues near *near* that can seat the party within budget.

    Call remind_session_task first if you do not remember party size, area, or budget.
    Use after remind_session_task in the Ex5 sequence (may run in parallel with
    get_weather once you have a venue_id for calculate_cost). Reads sample_data/venues.json.

    If a prior venue_search already returned matches this session, reuses that
    result instead of searching again.
    """
    args = {"near": near, "party_size": party_size, "budget_max_gbp": budget_max_gbp}

    prior = _prior_successful_venue_search()
    if prior is not None:
        prior_output = dict(prior.output)
        prior_output["reused_from_previous_search"] = True
        prior_near = prior_output.get("near", "?")
        prior_count = prior_output.get("count", 0)
        record_tool_call("venue_search", args, prior_output)
        names = ", ".join(
            v.get("name", v.get("id", "?")) for v in prior_output.get("results", [])[:3]
        )
        extra = f" ({names})" if names else ""
        return ToolResult(
            success=True,
            output=prior_output,
            summary=(
                f"venue_search: results already produced earlier near {prior_near!r} "
                f"({prior_count} venue(s){extra}). Use those — do not call venue_search again."
            ),
        )

    search_count = sum(1 for r in _TOOL_CALL_LOG if r.tool_name == "venue_search")
    if search_count >= _MAX_VENUE_SEARCH_CALLS:
        output = {
            "error": "too_many_searches",
            "count": search_count,
            "hint": "Use results from your earlier venue_search call(s).",
        }
        record_tool_call("venue_search", args, output)
        return _tool_failure(
            summary=(
                f"venue_search: STOP — already called {search_count} times. "
                "Pick a venue from prior results and call get_weather / calculate_cost."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="venue_search called too many times in this session",
            context=output,
            output=output,
        )

    near_norm, near_err = _normalise_near(near)
    if near_err:
        output = {"error": near_err}
        record_tool_call("venue_search", args, output)
        return _tool_failure(
            summary=f"venue_search: invalid near — {near_err} Pass a place name like 'Haymarket'.",
            code="SA_TOOL_INVALID_INPUT",
            message=near_err,
            context={"near": near},
            output=output,
        )

    party, party_err = _coerce_positive_int(party_size, name="party_size")
    if party_err:
        output = {"error": party_err}
        record_tool_call("venue_search", args, output)
        return _tool_failure(
            summary=f"venue_search: invalid party_size — {party_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=party_err,
            context={"party_size": party_size},
            output=output,
        )

    budget, budget_err = _coerce_positive_int(budget_max_gbp, name="budget_max_gbp", minimum=0)
    if budget_err:
        output = {"error": budget_err}
        record_tool_call("venue_search", args, output)
        return _tool_failure(
            summary=f"venue_search: invalid budget_max_gbp — {budget_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=budget_err,
            context={"budget_max_gbp": budget_max_gbp},
            output=output,
        )

    venues_raw, load_err = _load_json_fixture(_VENUES_PATH, "venue_search")
    if load_err:
        record_tool_call("venue_search", args, {"error": "fixture_missing"})
        return load_err

    if not isinstance(venues_raw, list):
        output = {"error": "venues.json must be a JSON array"}
        record_tool_call("venue_search", args, output)
        return _tool_failure(
            summary="venue_search: venues.json has unexpected shape (expected a list).",
            code="SA_TOOL_DEPENDENCY_MISSING",
            message="venues.json is not a list",
            output=output,
        )

    open_venues = _open_venues(venues_raw)
    available_areas = sorted(
        {str(v.get("area", "")).strip() for v in open_venues if str(v.get("area", "")).strip()}
    )

    near_lower = near_norm.lower()
    in_area = _venues_in_area(open_venues, near_lower)
    results = _filter_by_party_and_budget(in_area, party, budget)

    output: dict = {
        "near": near_norm,
        "party_size": party,
        "budget_max_gbp": budget,
        "results": results,
        "count": len(results),
    }

    if not results:
        if not in_area:
            output["failure_reason"] = "no_matching_area"
            output["available_areas"] = available_areas
            areas_text = ", ".join(available_areas) if available_areas else "(none)"
            summary = (
                f"venue_search({near_norm}, party={party}): 0 results — no open venues in that area. "
                f"Available open areas: {areas_text}. "
                "Pick one of these areas and search again (once)."
            )
        else:
            area_label = str(in_area[0].get("area", near_norm))
            area_options = _describe_area_venues(in_area, party, budget)
            output["failure_reason"] = "filtered_by_party_or_budget"
            output["area_venues"] = area_options
            options_summary = _format_area_options_summary(area_label, area_options)
            summary = (
                f"venue_search({near_norm}, party={party}, budget≤£{budget}): 0 results — "
                f"venues exist in {area_label} but none fit your constraints. "
                f"{options_summary}. "
                "Relax party_size or raise budget_max_gbp, or pick a different venue from this list."
            )
    else:
        names = ", ".join(v.get("name", v.get("id", "?")) for v in results[:3])
        extra = f" (e.g. {names})" if names else ""
        summary = f"venue_search({near_norm}, party={party}): {len(results)} result(s){extra}"

    record_tool_call("venue_search", args, output)
    return ToolResult(success=True, output=output, summary=summary)


def get_weather(city: str, date: str) -> ToolResult:
    """Look up scripted weather for *city* on *date* (YYYY-MM-DD).

    Use after venue_search (parallel with calculate_cost once venue_id is known).
    Reads sample_data/weather.json.
    """
    args = {"city": city, "date": date}

    city_norm, city_err = _normalise_city(city)
    if city_err:
        output = {"error": city_err}
        record_tool_call("get_weather", args, output)
        return _tool_failure(
            summary=f"get_weather: invalid city — {city_err} Use fixture keys like 'edinburgh'.",
            code="SA_TOOL_INVALID_INPUT",
            message=city_err,
            context={"city": city},
            output=output,
        )

    date_norm, date_err = _normalise_date(date)
    if date_err:
        output = {"error": date_err}
        record_tool_call("get_weather", args, output)
        return _tool_failure(
            summary=f"get_weather: invalid date — {date_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=date_err,
            context={"date": date},
            output=output,
        )

    weather_raw, load_err = _load_json_fixture(_WEATHER_PATH, "get_weather")
    if load_err:
        record_tool_call("get_weather", args, {"error": "fixture_missing"})
        return load_err

    if not isinstance(weather_raw, dict):
        output = {"error": "weather.json must be a JSON object"}
        record_tool_call("get_weather", args, output)
        return _tool_failure(
            summary="get_weather: weather.json has unexpected shape (expected city → dates map).",
            code="SA_TOOL_DEPENDENCY_MISSING",
            message="weather.json is not an object",
            output=output,
        )

    city_data = weather_raw.get(city_norm)
    if not isinstance(city_data, dict):
        known = sorted(k for k in weather_raw if isinstance(weather_raw[k], dict))
        output = {"error": "city_not_found", "known_cities": known}
        record_tool_call("get_weather", args, output)
        return _tool_failure(
            summary=(
                f"get_weather: no weather data for city {city_norm!r}. "
                f"Known cities: {', '.join(known) or '(none)'}."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message=f"city not in fixture: {city_norm}",
            context={"city": city_norm, "known_cities": known},
            output=output,
        )

    day = city_data.get(date_norm)
    if not isinstance(day, dict):
        known_dates = sorted(city_data.keys())
        output = {"error": "date_not_found", "known_dates": known_dates}
        record_tool_call("get_weather", args, output)
        return _tool_failure(
            summary=(
                f"get_weather: no forecast for {city_norm} on {date_norm}. "
                f"Available dates: {', '.join(known_dates) or '(none)'}."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message=f"date not in fixture: {date_norm}",
            context={"city": city_norm, "date": date_norm, "known_dates": known_dates},
            output=output,
        )

    output = {
        "city": city_norm,
        "date": date_norm,
        "condition": day.get("condition"),
        "temperature_c": day.get("temperature_c"),
        "precip_mm": day.get("precip_mm"),
        "wind_kph": day.get("wind_kph"),
    }
    record_tool_call("get_weather", args, output)

    condition = output.get("condition", "unknown")
    temp = output.get("temperature_c", "?")
    summary = f"get_weather({city_norm}, {date_norm}): {condition}, {temp}C"
    return ToolResult(success=True, output=output, summary=summary)


def calculate_cost(
    venue_id: str,
    party_size: int,
    duration_hours: int,
    catering_tier: str = "bar_snacks",
) -> ToolResult:
    """Compute booking total and deposit for a venue.

    Use after venue_search (needs venue_id from results). May run in parallel with
    get_weather. Reads sample_data/catering.json and venues.json for floor fees.
    """
    args = {
        "venue_id": venue_id,
        "party_size": party_size,
        "duration_hours": duration_hours,
        "catering_tier": catering_tier,
    }

    if not isinstance(venue_id, str) or not venue_id.strip():
        output = {"error": "venue_id must be a non-empty string"}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary="calculate_cost: venue_id must be a non-empty string (e.g. 'haymarket_tap').",
            code="SA_TOOL_INVALID_INPUT",
            message="invalid venue_id",
            context={"venue_id": venue_id},
            output=output,
        )
    venue_key = venue_id.strip()

    party, party_err = _coerce_positive_int(party_size, name="party_size")
    if party_err:
        output = {"error": party_err}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=f"calculate_cost: invalid party_size — {party_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=party_err,
            output=output,
        )

    duration, dur_err = _coerce_positive_int(duration_hours, name="duration_hours")
    if dur_err:
        output = {"error": dur_err}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=f"calculate_cost: invalid duration_hours — {dur_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=dur_err,
            output=output,
        )

    if not isinstance(catering_tier, str) or catering_tier not in _VALID_TIERS:
        output = {"error": "invalid catering_tier", "valid_tiers": sorted(_VALID_TIERS)}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=(
                f"calculate_cost: catering_tier must be one of {sorted(_VALID_TIERS)} "
                f"(got {catering_tier!r})."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="invalid catering_tier",
            context={"catering_tier": catering_tier},
            output=output,
        )

    catering_raw, cat_err = _load_json_fixture(_CATERING_PATH, "calculate_cost")
    if cat_err:
        record_tool_call("calculate_cost", args, {"error": "fixture_missing"})
        return cat_err

    venues_raw, ven_err = _load_json_fixture(_VENUES_PATH, "calculate_cost")
    if ven_err:
        record_tool_call("calculate_cost", args, {"error": "fixture_missing"})
        return ven_err

    if not isinstance(catering_raw, dict) or not isinstance(venues_raw, list):
        output = {"error": "unexpected fixture shape"}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary="calculate_cost: catering.json or venues.json has unexpected shape.",
            code="SA_TOOL_DEPENDENCY_MISSING",
            message="fixture shape invalid",
            output=output,
        )

    rates = catering_raw.get("base_rates_gbp_per_head", {})
    modifiers = catering_raw.get("venue_modifiers", {})
    service_pct = catering_raw.get("service_charge_percent", 0)
    policy = catering_raw.get("deposit_policy", {})
    min_party = catering_raw.get("minimum_party_size", 1)
    max_auto = catering_raw.get("maximum_party_size_for_auto_booking", 999)

    if party < min_party:
        output = {"error": "party_below_minimum", "minimum_party_size": min_party}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=(
                f"calculate_cost: party_size {party} is below minimum {min_party}. "
                "Increase the party size or escalate to the pub manager."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="party_size below minimum",
            context=output,
            output=output,
        )

    if party > max_auto:
        output = {"error": "party_above_auto_cap", "maximum_party_size_for_auto_booking": max_auto}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=(
                f"calculate_cost: party_size {party} exceeds auto-booking cap ({max_auto}). "
                "Reduce the party or hand off for manual approval."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="party_size above auto booking cap",
            context=output,
            output=output,
        )

    if venue_key not in modifiers:
        known = sorted(modifiers.keys())
        output = {"error": "unknown_venue_id", "known_venue_ids": known}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=(
                f"calculate_cost: unknown venue_id {venue_key!r}. "
                f"Pick an id from venue_search results: {', '.join(known)}."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message=f"venue not in modifiers: {venue_key}",
            context={"venue_id": venue_key, "known_venue_ids": known},
            output=output,
        )

    if catering_tier not in rates:
        output = {"error": "tier_not_in_rates", "catering_tier": catering_tier}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=f"calculate_cost: no rate for tier {catering_tier!r} in catering.json.",
            code="SA_TOOL_DEPENDENCY_MISSING",
            message="catering tier missing from rates",
            output=output,
        )

    venue_row = next((v for v in venues_raw if isinstance(v, dict) and v.get("id") == venue_key), None)
    if venue_row is None:
        output = {"error": "venue_not_in_venues_fixture", "venue_id": venue_key}
        record_tool_call("calculate_cost", args, output)
        return _tool_failure(
            summary=(
                f"calculate_cost: venue_id {venue_key!r} not found in venues.json. "
                "Run venue_search and use an id from its results."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="venue not in venues list",
            context={"venue_id": venue_key},
            output=output,
        )

    base_rate = int(rates[catering_tier])
    venue_mult = float(modifiers[venue_key])
    hours = max(1, duration)
    subtotal_gbp = int(base_rate * venue_mult * party * hours)
    service_gbp = round(subtotal_gbp * float(service_pct) / 100)
    hire_fee = int(venue_row.get("hire_fee_gbp", 0) or 0)
    min_spend = int(venue_row.get("min_spend_gbp", 0) or 0)
    venue_floor_gbp = hire_fee + min_spend
    total_gbp = subtotal_gbp + service_gbp + venue_floor_gbp
    deposit_required_gbp = _deposit_for_total(total_gbp, policy)

    output = {
        "venue_id": venue_key,
        "party_size": party,
        "duration_hours": hours,
        "catering_tier": catering_tier,
        "subtotal_gbp": subtotal_gbp,
        "service_gbp": service_gbp,
        "venue_floor_gbp": venue_floor_gbp,
        "total_gbp": total_gbp,
        "deposit_required_gbp": deposit_required_gbp,
    }
    record_tool_call("calculate_cost", args, output)

    summary = (
        f"calculate_cost({venue_key}, party={party}): "
        f"total £{total_gbp}, deposit £{deposit_required_gbp}"
    )
    return ToolResult(success=True, output=output, summary=summary)


def generate_flyer(session: Session, event_details: dict) -> ToolResult:
    """Write a self-contained HTML flyer to workspace/flyer.html.

    Use last, after venue_search, get_weather, and calculate_cost. NOT parallel-safe.
    Every fact in the flyer must match tool outputs (verify_dataflow checks this).
    """
    args = {"event_details": event_details}

    if not isinstance(event_details, dict):
        output = {"error": "event_details must be an object/dict"}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=(
                "generate_flyer: event_details must be a JSON object with venue, weather, "
                "and cost fields from prior tool calls."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message="event_details must be a dict",
            context={"event_details_type": type(event_details).__name__},
            output=output,
        )

    missing = [k for k in _FLYER_REQUIRED_KEYS if k not in event_details]
    if missing:
        output = {"error": "missing_fields", "missing": missing, "required": list(_FLYER_REQUIRED_KEYS)}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=(
                f"generate_flyer: missing required field(s): {', '.join(missing)}. "
                "Populate event_details from venue_search, get_weather, and calculate_cost outputs."
            ),
            code="SA_TOOL_INVALID_INPUT",
            message=f"missing fields: {missing}",
            context=output,
            output=output,
        )

    party, party_err = _coerce_positive_int(event_details.get("party_size"), name="party_size")
    if party_err:
        output = {"error": party_err}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=f"generate_flyer: invalid party_size in event_details — {party_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=party_err,
            output=output,
        )

    temp, temp_err = _coerce_positive_int(
        event_details.get("temperature_c"), name="temperature_c", minimum=-30
    )
    if temp_err:
        output = {"error": temp_err}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=f"generate_flyer: invalid temperature_c — {temp_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=temp_err,
            output=output,
        )

    total, total_err = _coerce_non_negative_int(event_details.get("total_gbp"), name="total_gbp")
    if total_err:
        output = {"error": total_err}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=f"generate_flyer: invalid total_gbp — {total_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=total_err,
            output=output,
        )

    deposit, dep_err = _coerce_non_negative_int(
        event_details.get("deposit_required_gbp"), name="deposit_required_gbp"
    )
    if dep_err:
        output = {"error": dep_err}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=f"generate_flyer: invalid deposit_required_gbp — {dep_err}",
            code="SA_TOOL_INVALID_INPUT",
            message=dep_err,
            output=output,
        )

    venue_name = str(event_details["venue_name"]).strip()
    venue_address = str(event_details["venue_address"]).strip()
    date = str(event_details["date"]).strip()
    time_str = str(event_details["time"]).strip()
    condition = str(event_details["condition"]).strip()
    condition_display = condition.replace("_", " ")

    if not venue_name or not venue_address or not date or not time_str or not condition:
        output = {"error": "empty string in required text fields"}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary="generate_flyer: venue_name, venue_address, date, time, and condition cannot be empty.",
            code="SA_TOOL_INVALID_INPUT",
            message="required text fields empty",
            output=output,
        )

    deposit_line = f'<dd data-testid="deposit_required_gbp">£{deposit}</dd>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(venue_name)} — Edinburgh booking</title>
  <style>
    body {{ font-family: Georgia, serif; margin: 2rem; color: #1a1a1a; background: #faf8f5; }}
    article {{ max-width: 36rem; margin: 0 auto; padding: 1.5rem; background: #fff; border: 1px solid #ddd; }}
    h1 {{ margin-top: 0; color: #2c5282; }}
    dl {{ display: grid; grid-template-columns: 8rem 1fr; gap: 0.35rem 1rem; }}
    dt {{ font-weight: bold; }}
    .weather {{ margin-top: 1rem; padding: 0.75rem; background: #ebf4ff; border-radius: 4px; }}
    .cost {{ margin-top: 1rem; }}
  </style>
</head>
<body>
  <article>
    <h1 data-testid="title">{escape(venue_name)}</h1>
    <dl>
      <dt>Venue</dt>
      <dd data-testid="venue_name">{escape(venue_name)}</dd>
      <dt>Address</dt>
      <dd data-testid="venue_address">{escape(venue_address)}</dd>
      <dt>Date</dt>
      <dd data-testid="date">{escape(date)}</dd>
      <dt>Time</dt>
      <dd data-testid="time">{escape(time_str)}</dd>
      <dt>Party</dt>
      <dd data-testid="party_size">{party}</dd>
    </dl>
    <section class="weather">
      <h2>Weather</h2>
      <p>
        <span data-testid="condition">{escape(condition_display)}</span>,
        <span data-testid="temperature_c">{temp}°C</span>
      </p>
    </section>
    <section class="cost">
      <h2>Cost</h2>
      <dl>
        <dt>Total</dt>
        <dd data-testid="total_gbp">£{total}</dd>
        <dt>Deposit</dt>
        {deposit_line}
      </dl>
    </section>
  </article>
</body>
</html>
"""

    flyer_path = session.workspace_dir / "flyer.html"
    try:
        session.workspace_dir.mkdir(parents=True, exist_ok=True)
        flyer_path.write_text(html, encoding="utf-8")
    except OSError as exc:
        output = {"error": str(exc), "path": "workspace/flyer.html"}
        record_tool_call("generate_flyer", args, output)
        return _tool_failure(
            summary=f"generate_flyer: could not write workspace/flyer.html — {exc}",
            code="SA_TOOL_EXECUTION_FAILED",
            message="failed to write flyer",
            context={"path": str(flyer_path)},
            output=output,
        )

    output = {"path": "workspace/flyer.html", "bytes_written": len(html.encode("utf-8"))}
    record_tool_call("generate_flyer", args, output)
    summary = f"generate_flyer: wrote workspace/flyer.html ({len(html)} chars)"
    return ToolResult(success=True, output=output, summary=summary)


# ---------------------------------------------------------------------------
# Registry builder — DO NOT MODIFY the name, signature, or registration calls.
# The grader imports and calls this to pick up your tools.
# ---------------------------------------------------------------------------
def build_tool_registry(session: Session) -> ToolRegistry:
    """Build a session-scoped tool registry with all four Ex5 tools plus
    the sovereign-agent builtins (read_file, write_file, list_files,
    handoff_to_structured, complete_task).

    DO NOT change the tool names — the tests and grader call them by name.
    """
    from sovereign_agent.tools.builtin import make_builtin_registry

    reg = make_builtin_registry(session)

    tool_order = (
        "0. remind_session_task (FIRST; call again if you forget constraints) → "
        "1. venue_search → 2. get_weather → 3. calculate_cost (1–3 may run in parallel) "
        "→ 4. generate_flyer (alone; writes file) → 5. complete_task"
    )

    def _remind_adapter() -> ToolResult:
        return remind_session_task(session)

    reg.register(
        _RegisteredTool(
            name="remind_session_task",
            description=(
                "Read the full homework task from this session's SESSION.md. "
                "Call FIRST before any research tool, and call again whenever you "
                "forget party size, area, date, budget, or tool order. "
                f"Ex5 order: {tool_order}."
            ),
            fn=_remind_adapter,
            parameters_schema={"type": "object", "properties": {}, "required": []},
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {},
                    "output": {
                        "path": "SESSION.md",
                        "task": "Research an Edinburgh pub and produce an HTML event flyer.",
                    },
                }
            ],
        )
    )

    reg.register(
        _RegisteredTool(
            name="venue_search",
            description=(
                "Search Edinburgh venues by area, party size, and max budget. "
                f"Ex5 order: {tool_order}. Reads sample_data/venues.json."
            ),
            fn=venue_search,
            parameters_schema={
                "type": "object",
                "properties": {
                    "near": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "budget_max_gbp": {"type": "integer", "default": 1000},
                },
                "required": ["near", "party_size"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {"near": "Haymarket", "party_size": 6, "budget_max_gbp": 800},
                    "output": {"count": 1, "results": [{"id": "haymarket_tap"}]},
                }
            ],
        )
    )

    reg.register(
        _RegisteredTool(
            name="get_weather",
            description=(
                "Get scripted weather for a city on YYYY-MM-DD. "
                f"Ex5 order: {tool_order}. Reads sample_data/weather.json."
            ),
            fn=get_weather,
            parameters_schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["city", "date"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {"city": "Edinburgh", "date": "2026-04-25"},
                    "output": {"condition": "cloudy", "temperature_c": 12},
                }
            ],
        )
    )

    reg.register(
        _RegisteredTool(
            name="calculate_cost",
            description=(
                "Compute total cost and deposit for a booking (needs venue_id from venue_search). "
                f"Ex5 order: {tool_order}. Reads catering.json + venues.json."
            ),
            fn=calculate_cost,
            parameters_schema={
                "type": "object",
                "properties": {
                    "venue_id": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "duration_hours": {"type": "integer"},
                    "catering_tier": {
                        "type": "string",
                        "enum": ["drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal"],
                        "default": "bar_snacks",
                    },
                },
                "required": ["venue_id", "party_size", "duration_hours"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,
            examples=[
                {
                    "input": {
                        "venue_id": "haymarket_tap",
                        "party_size": 6,
                        "duration_hours": 3,
                    },
                    "output": {"total_gbp": 540, "deposit_required_gbp": 0},
                }
            ],
        )
    )

    def _flyer_adapter(event_details: dict) -> ToolResult:
        return generate_flyer(session, event_details)

    reg.register(
        _RegisteredTool(
            name="generate_flyer",
            description=(
                "Write HTML event flyer to workspace/flyer.html (data-testid on every fact). "
                f"Ex5 order: {tool_order}. Must run after research tools; NOT parallel-safe."
            ),
            fn=_flyer_adapter,
            parameters_schema={
                "type": "object",
                "properties": {"event_details": {"type": "object"}},
                "required": ["event_details"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=False,
            examples=[
                {
                    "input": {
                        "event_details": {
                            "venue_name": "Haymarket Tap",
                            "date": "2026-04-25",
                            "party_size": 6,
                        }
                    },
                    "output": {"path": "workspace/flyer.html"},
                }
            ],
        )
    )

    return reg


__all__ = [
    "build_tool_registry",
    "remind_session_task",
    "venue_search",
    "get_weather",
    "calculate_cost",
    "generate_flyer",
]
