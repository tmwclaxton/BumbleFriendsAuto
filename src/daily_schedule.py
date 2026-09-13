"""The intended phone jobs for today's Europe/London calendar.

Cron ticks every five minutes; this module stores the real work: recapture,
jittered fast scans, LinkedIn reply checks / feed reacts, session, backfill.
Hinge is an accepted channel for the jobs API but has no cron slots yet.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.phones import DEFAULT_PHONE_ID, expand_phone_ids, phone_ids

LONDON = ZoneInfo("Europe/London")
PLAN_NAME = "daily_phone_plan.json"
ACCEPTED_CHANNELS = ("bumble", "linkedin", "hinge")
FAST_SCAN_START_HOUR = 8
FAST_SCAN_END_HOUR = 21
FAST_SCAN_MIN_GAP = 30 * 60
FAST_SCAN_MAX_GAP = 90 * 60
LINKEDIN_SCAN_START_HOUR = 8
LINKEDIN_SCAN_END_HOUR = 23
LINKEDIN_SCAN_MIN_GAP = 30 * 60
LINKEDIN_SCAN_MAX_GAP = 90 * 60
LINKEDIN_FEED_START_HOUR = 9
LINKEDIN_FEED_END_HOUR = 21
LINKEDIN_FEED_MIN_GAP = 90 * 60
LINKEDIN_FEED_MAX_GAP = 180 * 60


def _iso(value: datetime) -> str:
    return value.isoformat()


def _today(value: datetime, day_start: datetime) -> bool:
    return day_start <= value < day_start + timedelta(days=1)


def _plan_path() -> Path:
    from src.config import ROOT

    return ROOT / "data" / PLAN_NAME


def _configured_phone_ids() -> list[str]:
    return phone_ids() or [DEFAULT_PHONE_ID]


def _duration_seconds(kind: str, phone_id: str, recorded: object = None) -> float:
    from src.phone_queue import _DEFAULT_DURATION_SECONDS, _MIN_DURATION_SECONDS, average_duration_seconds

    try:
        raw = float(recorded) if recorded not in (None, "") else 0.0
    except (TypeError, ValueError):
        raw = 0.0
    if raw >= _MIN_DURATION_SECONDS:
        return raw
    expected = average_duration_seconds(kind, phone_id)
    if expected < _MIN_DURATION_SECONDS:
        return _DEFAULT_DURATION_SECONDS
    return expected


def _entry(
    kind: str,
    phone_id: str,
    starts: datetime,
    *,
    title: str,
    source: str,
    detail: str = "",
    channel: str = "bumble",
    duration_seconds: float | None = None,
) -> dict:
    from src.phone_queue import job_channel

    expected = _duration_seconds(kind, phone_id, duration_seconds)
    return {
        "kind": kind,
        "phone_id": phone_id,
        "channel": channel or job_channel(kind),
        "title": title,
        "source": source,
        "detail": detail,
        "status": "planned",
        "scheduled_at": _iso(starts),
        "intended_at": _iso(starts),
        "expected_duration_seconds": expected,
        "projected_start_at": _iso(starts),
        "projected_end_at": _iso(starts + timedelta(seconds=expected)),
    }


def _space_items(items: list[dict], *, gap_seconds: int = 60) -> list[dict]:
    """Push colliding per-phone jobs after the earlier block so order is visible."""
    from src.phone_queue import _DEFAULT_DURATION_SECONDS, _MIN_DURATION_SECONDS

    grouped: dict[str, list[dict]] = {}
    for row in items:
        grouped.setdefault(str(row.get("phone_id") or ""), []).append(row)
    out: list[dict] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: (str(row.get("scheduled_at") or ""), str(row.get("kind") or "")))
        cursor: datetime | None = None
        for row in rows:
            try:
                start = datetime.fromisoformat(str(row["scheduled_at"]))
            except (KeyError, TypeError, ValueError):
                out.append(row)
                continue
            try:
                expected = float(row.get("expected_duration_seconds") or 0)
            except (TypeError, ValueError):
                expected = 0.0
            if expected < _MIN_DURATION_SECONDS:
                expected = _DEFAULT_DURATION_SECONDS
            if cursor is not None and start < cursor:
                start = cursor
            end = start + timedelta(seconds=expected)
            row["scheduled_at"] = _iso(start)
            row["projected_start_at"] = _iso(start)
            row["projected_end_at"] = _iso(end)
            row["expected_duration_seconds"] = expected
            cursor = end + timedelta(seconds=gap_seconds)
            out.append(row)
    out.sort(key=lambda row: (str(row.get("scheduled_at") or ""), str(row.get("phone_id") or "")))
    return out


def _load_raw_plan() -> dict:
    path = _plan_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_plan(plan: dict) -> None:
    path = _plan_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def _has_slots(row: dict, key: str) -> bool:
    slots = row.get(key)
    return isinstance(slots, list) and bool(slots)


def _jitter_times(
    day_start: datetime,
    *,
    now: datetime,
    next_due: float,
    rng: random.Random,
    start_hour: int,
    end_hour: int,
    min_gap: int,
    max_gap: int,
) -> list[float]:
    start = day_start.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = day_start.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    anchor: datetime | None = None
    if next_due > 0:
        due_at = datetime.fromtimestamp(next_due, LONDON)
        if start <= due_at < end:
            anchor = due_at

    slots: list[datetime] = []
    cursor = start
    if anchor is not None:
        while True:
            nxt = cursor + timedelta(seconds=rng.randint(min_gap, max_gap))
            if nxt >= anchor:
                break
            slots.append(cursor)
            cursor = nxt
        slots.append(anchor)
        cursor = anchor + timedelta(seconds=rng.randint(min_gap, max_gap))
    else:
        slots.append(start)
        cursor = start + timedelta(seconds=rng.randint(min_gap, max_gap))

    while cursor < end:
        slots.append(cursor)
        cursor = cursor + timedelta(seconds=rng.randint(min_gap, max_gap))

    if now < start and start not in slots:
        slots.insert(0, start)
    return [dt.timestamp() for dt in slots]


def _fast_scan_times(
    day_start: datetime,
    *,
    now: datetime,
    next_due: float,
    rng: random.Random,
) -> list[float]:
    return _jitter_times(
        day_start,
        now=now,
        next_due=next_due,
        rng=rng,
        start_hour=FAST_SCAN_START_HOUR,
        end_hour=FAST_SCAN_END_HOUR,
        min_gap=FAST_SCAN_MIN_GAP,
        max_gap=FAST_SCAN_MAX_GAP,
    )


def _slot_rows(times: list[float], *, pending: float, duration_seconds: float = 600.0) -> list[dict]:
    expected = duration_seconds if duration_seconds >= 60 else 600.0
    # Never mark a slot consumed just because the plan was created later in
    # the day — that burned LinkedIn reply checks without touching the phone.
    _ = pending
    return [
        {
            "at": ts,
            "consumed": False,
            "expected_duration_seconds": expected,
        }
        for ts in times
    ]


def _pending_for(times: list[float], *, due: float, current: float, window_start: float, window_end: float) -> float:
    if window_start <= due < window_end:
        return due
    if due <= current:
        return next((ts for ts in reversed(times) if ts <= current), times[0] if times else 0.0)
    return 0.0


def _offset_from(times: list[float], existing: list[float], rng: random.Random, *, pad: int = 12 * 60) -> list[float]:
    """Nudge new slots away from an existing series so the calendar interleaves."""
    taken = sorted(existing)
    out: list[float] = []
    for ts in times:
        stamp = ts
        for other in taken:
            if abs(stamp - other) < pad:
                stamp = other + pad + rng.randint(60, 9 * 60)
        out.append(stamp)
        taken.append(stamp)
        taken.sort()
    return out


def ensure_day_plan(
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    initialize_due: bool = True,
) -> dict:
    """Create or reuse today's date-keyed phone plan."""
    stamp = (now or datetime.now(LONDON)).astimezone(LONDON)
    day_key = stamp.date().isoformat()
    raw = _load_raw_plan()
    stored = raw.get(day_key) if isinstance(raw.get(day_key), dict) else None
    row = dict(stored) if stored else {}
    dice = rng or random.Random()
    day_start = datetime.combine(stamp.date(), time.min, tzinfo=LONDON)
    current = stamp.timestamp()
    changed = False

    if not _has_slots(row, "fast_scan"):
        from src.jobs.fast_scan_cron import next_due_at

        due = next_due_at()
        times = _fast_scan_times(day_start, now=stamp, next_due=due, rng=dice)
        window_start = day_start.replace(hour=FAST_SCAN_START_HOUR).timestamp()
        window_end = day_start.replace(hour=FAST_SCAN_END_HOUR).timestamp()
        row["fast_scan"] = _slot_rows(
            times,
            pending=_pending_for(
                times, due=due, current=current, window_start=window_start, window_end=window_end
            ),
        )
        changed = True

    if not _has_slots(row, "linkedin_scan"):
        from src.jobs.linkedin_cron import next_scan_due_at

        due = next_scan_due_at()
        times = _jitter_times(
            day_start,
            now=stamp,
            next_due=due,
            rng=dice,
            start_hour=LINKEDIN_SCAN_START_HOUR,
            end_hour=LINKEDIN_SCAN_END_HOUR,
            min_gap=LINKEDIN_SCAN_MIN_GAP,
            max_gap=LINKEDIN_SCAN_MAX_GAP,
        )
        window_start = day_start.replace(hour=LINKEDIN_SCAN_START_HOUR).timestamp()
        window_end = day_start.replace(hour=LINKEDIN_SCAN_END_HOUR).timestamp()
        row["linkedin_scan"] = _slot_rows(
            times,
            pending=_pending_for(
                times, due=due, current=current, window_start=window_start, window_end=window_end
            ),
        )
        changed = True

    if not _has_slots(row, "linkedin_feed"):
        from src.jobs.linkedin_cron import next_feed_due_at

        due = next_feed_due_at()
        times = _jitter_times(
            day_start,
            now=stamp,
            next_due=due,
            rng=dice,
            start_hour=LINKEDIN_FEED_START_HOUR,
            end_hour=LINKEDIN_FEED_END_HOUR,
            min_gap=LINKEDIN_FEED_MIN_GAP,
            max_gap=LINKEDIN_FEED_MAX_GAP,
        )
        reply_ats = []
        for slot in row.get("linkedin_scan") or []:
            if isinstance(slot, dict):
                try:
                    reply_ats.append(float(slot.get("at") or 0))
                except (TypeError, ValueError):
                    continue
        times = _offset_from(times, reply_ats, dice)
        window_start = day_start.replace(hour=LINKEDIN_FEED_START_HOUR).timestamp()
        window_end = day_start.replace(hour=LINKEDIN_FEED_END_HOUR).timestamp()
        row["linkedin_feed"] = _slot_rows(
            times,
            pending=_pending_for(
                times, due=due, current=current, window_start=window_start, window_end=window_end
            ),
        )
        changed = True

    row.setdefault("timezone", "Europe/London")
    if _ensure_slot_durations(row):
        changed = True
    if changed:
        packed = {k: v for k, v in raw.items() if isinstance(v, dict)}
        packed[day_key] = row
        _save_plan(packed)
    return {"date": day_key, **row}


def _kind_duration(kind: str) -> float:
    phones = _configured_phone_ids()
    pid = phones[0] if phones else DEFAULT_PHONE_ID
    return _duration_seconds(kind, pid)


def _ensure_slot_durations(row: dict) -> bool:
    changed = False
    for key, kind in (
        ("fast_scan", "fast_scan"),
        ("linkedin_scan", "linkedin_scan"),
        ("linkedin_feed", "linkedin_feed"),
    ):
        expected = _kind_duration(kind)
        for slot in row.get(key) or []:
            if not isinstance(slot, dict):
                continue
            try:
                current = float(slot.get("expected_duration_seconds") or 0)
            except (TypeError, ValueError):
                current = 0.0
            if current < 60:
                slot["expected_duration_seconds"] = expected
                changed = True
    return changed


def next_unconsumed_slot(key: str, *, now: datetime | None = None) -> float:
    plan = ensure_day_plan(now=now)
    for slot in plan.get(key) or []:
        if isinstance(slot, dict) and not slot.get("consumed"):
            try:
                return float(slot.get("at") or 0)
            except (TypeError, ValueError):
                continue
    return 0.0


def next_unconsumed_fast_scan(*, now: datetime | None = None) -> float:
    return next_unconsumed_slot("fast_scan", now=now)


def consume_plan_slot(
    key: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    min_gap: int = FAST_SCAN_MIN_GAP,
    max_gap: int = FAST_SCAN_MAX_GAP,
) -> float:
    """Mark the current due slot consumed and return the next planned time."""
    stamp = (now or datetime.now(LONDON)).astimezone(LONDON)
    plan = ensure_day_plan(now=stamp, rng=rng)
    day_key = plan["date"]
    current = stamp.timestamp()
    consumed = False
    for slot in plan.get(key) or []:
        if not isinstance(slot, dict) or slot.get("consumed"):
            continue
        try:
            at = float(slot.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if at <= current:
            slot["consumed"] = True
            consumed = True
            break
    if not consumed:
        for slot in plan.get(key) or []:
            if isinstance(slot, dict) and not slot.get("consumed"):
                slot["consumed"] = True
                break
    nxt = 0.0
    for slot in plan.get(key) or []:
        if isinstance(slot, dict) and not slot.get("consumed"):
            try:
                nxt = float(slot.get("at") or 0)
            except (TypeError, ValueError):
                nxt = 0.0
            break
    if nxt <= 0:
        nxt = current + (rng or random.Random()).randint(min_gap, max_gap)
    raw = _load_raw_plan()
    raw[day_key] = {k: v for k, v in plan.items() if k != "date"}
    _save_plan(raw)
    return nxt


def consume_fast_scan_slot(
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> float:
    nxt = consume_plan_slot(
        "fast_scan",
        now=now,
        rng=rng,
        min_gap=FAST_SCAN_MIN_GAP,
        max_gap=FAST_SCAN_MAX_GAP,
    )
    from src.jobs.fast_scan_cron import write_next_due

    write_next_due(nxt)
    return nxt


def consume_linkedin_scan_slot(
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> float:
    nxt = consume_plan_slot(
        "linkedin_scan",
        now=now,
        rng=rng,
        min_gap=LINKEDIN_SCAN_MIN_GAP,
        max_gap=LINKEDIN_SCAN_MAX_GAP,
    )
    from src.jobs.linkedin_cron import write_scan_next_due

    write_scan_next_due(nxt)
    return nxt


def consume_linkedin_feed_slot(
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> float:
    nxt = consume_plan_slot(
        "linkedin_feed",
        now=now,
        rng=rng,
        min_gap=LINKEDIN_FEED_MIN_GAP,
        max_gap=LINKEDIN_FEED_MAX_GAP,
    )
    from src.jobs.linkedin_cron import write_feed_next_due

    write_feed_next_due(nxt)
    return nxt


def today_schedule(*, now: datetime | None = None, initialize_due: bool = True) -> dict:
    """Return intended work, not the five-minute cron ticks that check for it."""
    stamp = (now or datetime.now(LONDON)).astimezone(LONDON)
    day_start = datetime.combine(stamp.date(), time.min, tzinfo=LONDON)
    targets = _configured_phone_ids()
    items: list[dict] = []

    at_eight = day_start.replace(hour=8)
    for pid in targets:
        items.append(
            _entry(
                "recapture_all",
                pid,
                at_eight,
                title="Refresh all chats",
                source="docker/crontab",
                detail="Daily full inbox refresh",
                channel="bumble",
            )
        )

    plan = ensure_day_plan(now=stamp, initialize_due=initialize_due)
    for slot in plan.get("fast_scan") or []:
        if not isinstance(slot, dict):
            continue
        try:
            starts = datetime.fromtimestamp(float(slot.get("at") or 0), LONDON)
        except (TypeError, ValueError, OSError):
            continue
        if not _today(starts, day_start):
            continue
        for pid in targets:
            items.append(
                _entry(
                    "fast_scan",
                    pid,
                    starts,
                    title="Fast reply scan",
                    source=PLAN_NAME,
                    detail="Jittered 30–90m scans; cron ticks are not shown",
                    channel="bumble",
                    duration_seconds=slot.get("expected_duration_seconds"),
                )
            )

    for slot in plan.get("linkedin_scan") or []:
        if not isinstance(slot, dict):
            continue
        try:
            starts = datetime.fromtimestamp(float(slot.get("at") or 0), LONDON)
        except (TypeError, ValueError, OSError):
            continue
        if not _today(starts, day_start):
            continue
        for pid in targets:
            items.append(
                _entry(
                    "linkedin_scan",
                    pid,
                    starts,
                    title="LinkedIn reply check",
                    source=PLAN_NAME,
                    detail="Jittered inbox checks; cron ticks are not shown",
                    channel="linkedin",
                    duration_seconds=slot.get("expected_duration_seconds"),
                )
            )

    for slot in plan.get("linkedin_feed") or []:
        if not isinstance(slot, dict):
            continue
        try:
            starts = datetime.fromtimestamp(float(slot.get("at") or 0), LONDON)
        except (TypeError, ValueError, OSError):
            continue
        if not _today(starts, day_start):
            continue
        for pid in targets:
            items.append(
                _entry(
                    "linkedin_feed",
                    pid,
                    starts,
                    title="LinkedIn feed react",
                    source=PLAN_NAME,
                    detail="Short Home scroll and a few reactions",
                    channel="linkedin",
                    duration_seconds=slot.get("expected_duration_seconds"),
                )
            )

    from src.jobs.linkedin_session_cron import due_map as session_due_map
    from src.jobs.linkedin_session_cron import ensure_due as ensure_session_due
    from src.linkedin_session import load_prefs

    prefs = load_prefs()
    if prefs.get("daily_auto"):
        session_targets = expand_phone_ids(str(prefs.get("phone_id") or "all"))
        dues = session_due_map()
        for pid in session_targets:
            due = float(dues.get(pid) or 0)
            if due <= 0 and initialize_due:
                due = ensure_session_due(pid, now=stamp.timestamp())
            starts = datetime.fromtimestamp(due, LONDON) if due > 0 else None
            if starts is not None and _today(starts, day_start):
                items.append(
                    _entry(
                        "linkedin_session",
                        pid,
                        starts,
                        title="LinkedIn Home session",
                        source="linkedin_session_due.json",
                        detail="Daily auto preference is on",
                        channel="linkedin",
                    )
                )

    from src.jobs.linkedin_backfill_cron import due_map as backfill_due_map
    from src.jobs.linkedin_backfill_cron import ensure_due as ensure_backfill_due

    dues = backfill_due_map()
    for pid in targets:
        due = float(dues.get(pid) or 0)
        if due <= 0 and initialize_due:
            due = ensure_backfill_due(pid, now=stamp.timestamp())
        starts = datetime.fromtimestamp(due, LONDON) if due > 0 else None
        if starts is not None and _today(starts, day_start):
            items.append(
                _entry(
                    "linkedin_backfill",
                    pid,
                    starts,
                    title="LinkedIn older chats",
                    source="linkedin_backfill_due.json",
                    detail="One quiet-evening import",
                    channel="linkedin",
                )
            )

    items = _space_items(items)
    return {
        "date": stamp.date().isoformat(),
        "timezone": "Europe/London",
        "channels": list(ACCEPTED_CHANNELS),
        "day_start": _iso(day_start),
        "day_end": _iso(day_start + timedelta(days=1)),
        "items": items,
    }
