"""
sync_whoop.py
-------------
Main entry point for the Whoop → Notion recovery sync.

Fetches recovery, sleep, cycle, and workout data from the Whoop API for a
given date range, merges them into one DailyRecord per calendar day, and
upserts each record into the Whoop Recovery Log Notion database.

Usage:
    # Sync yesterday (default)
    python -m src.sync_whoop

    # Sync a specific date range
    python -m src.sync_whoop --start 2025-01-01 --end 2025-01-07

    # Verbose / debug logging
    python -m src.sync_whoop --verbose

Date alignment:
    Whoop organises data around "cycles" — a ~24h window that typically
    starts in the afternoon and ends the following afternoon. Recovery and
    sleep records are both linked to a cycle via cycle_id. This script uses
    the cycle's start date (UTC) as the canonical calendar date for that
    day's data, so all four data types (recovery, sleep, cycle, workouts)
    land on the same row in Notion.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from src.notion_client import DailyRecord, NotionRecoveryClient
from src.whoop_client import WhoopClient

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unit-conversion helpers
# ---------------------------------------------------------------------------

def _ms_to_hrs(ms: float | None) -> float | None:
    """Convert milliseconds to hours, or return None if input is None."""
    if ms is None:
        return None
    return round(ms / 3_600_000, 2)


def _kj_to_kcal(kj: float | None) -> float | None:
    """Convert kilojoules to kilocalories, or return None if input is None."""
    if kj is None:
        return None
    return round(kj * 0.239006, 1)


def _duration_min(start_ts: str | None, end_ts: str | None) -> float | None:
    """
    Calculate the duration in minutes between two Whoop timestamp strings.

    Returns None if either timestamp is missing or unparseable.
    """
    if not start_ts or not end_ts:
        return None
    try:
        start_dt = _parse_timestamp(start_ts)
        end_dt = _parse_timestamp(end_ts)
        return round((end_dt - start_dt).total_seconds() / 60, 1)
    except (ValueError, TypeError):
        logger.debug("Could not parse duration from %s / %s", start_ts, end_ts)
        return None


def _total_sleep_ms(stage_summary: dict[str, Any]) -> float | None:
    """
    Calculate total sleep time in milliseconds from a Whoop stage_summary dict.

    The Whoop v2 API does not provide a 'total_sleep_time_milli' field directly.
    Total sleep is the sum of the three sleep stages (light + slow-wave + REM),
    which excludes time spent awake in bed.

    Returns None only if all three stage fields are absent.
    """
    light = stage_summary.get("total_light_sleep_time_milli")
    sws   = stage_summary.get("total_slow_wave_sleep_time_milli")
    rem   = stage_summary.get("total_rem_sleep_time_milli")
    if light is None and sws is None and rem is None:
        return None
    return (light or 0.0) + (sws or 0.0) + (rem or 0.0)


def _add_to_running_total(
    current: float | None,
    addition: float | None,
) -> float | None:
    """
    Add a value to a running total, treating None as 'not yet started'.

    - (None, None)  → None   (no workouts at all)
    - (None, 5.0)   → 5.0   (first workout)
    - (3.0,  5.0)   → 8.0   (subsequent workout, sum)
    """
    if addition is None:
        return current
    return (current or 0.0) + addition


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_timestamp(ts: str) -> datetime:
    """
    Parse a Whoop ISO 8601 timestamp into a timezone-aware UTC datetime.

    Whoop returns timestamps like "2025-01-15T17:00:00.000Z". Python 3.9's
    datetime.fromisoformat() does not support the trailing 'Z', so we
    normalise it to '+00:00' before parsing.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _date_from_ts(ts: str | None) -> date | None:
    """Extract the UTC calendar date from a Whoop timestamp string."""
    if not ts:
        return None
    try:
        return _parse_timestamp(ts).astimezone(timezone.utc).date()
    except (ValueError, AttributeError):
        logger.debug("Could not parse date from timestamp: %s", ts)
        return None


# ---------------------------------------------------------------------------
# Core data-merging logic
# ---------------------------------------------------------------------------

def build_daily_records(
    whoop: WhoopClient,
    start: date,
    end: date,
) -> list[DailyRecord]:
    """
    Fetch all Whoop data for [start, end] and merge into one DailyRecord per day.

    Steps:
      1. Fetch cycles, recovery, sleep, and workouts from the Whoop API.
      2. Build a cycle_id → date lookup from cycles (the date anchor).
      3. Merge recovery, sleep, and cycle data into per-day records.
      4. Group workouts by start date; sum strain and duration across sessions.
      5. For any day in the range with no Whoop data, emit a placeholder record.

    Args:
        whoop: Authenticated WhoopClient instance.
        start: First date to include (inclusive).
        end:   Last date to include (inclusive).

    Returns:
        List of DailyRecord objects, one per calendar day in [start, end],
        sorted in chronological order.
    """
    logger.info("Fetching Whoop data for %s → %s", start, end)

    # Fetch all four data types in parallel conceptually; sequentially here
    # since we need cycles first to build the date-lookup index.
    cycles    = whoop.get_cycles(start, end)
    recoveries = whoop.get_recovery(start, end)
    sleeps    = whoop.get_sleep(start, end)
    workouts  = whoop.get_workouts(start, end)

    logger.debug(
        "Raw counts — cycles: %d, recovery: %d, sleep: %d, workouts: %d",
        len(cycles), len(recoveries), len(sleeps), len(workouts),
    )

    # ------------------------------------------------------------------
    # Step 1: build the cycle_id → calendar date index
    # The cycle's start timestamp determines the canonical date for that
    # day's recovery and sleep records.
    # ------------------------------------------------------------------
    cycle_date: dict[int, date] = {}
    records: dict[date, DailyRecord] = {}

    for cycle in cycles:
        cycle_id = cycle.get("id")
        d = _date_from_ts(cycle.get("start"))
        if cycle_id is None or d is None:
            logger.debug("Skipping cycle with missing id or start: %s", cycle)
            continue

        cycle_date[cycle_id] = d
        score = cycle.get("score") or {}

        if cycle.get("score_state") != "SCORED":
            logger.debug(
                "Cycle %s on %s has score_state=%s — numeric fields may be absent",
                cycle_id, d, cycle.get("score_state"),
            )

        records[d] = DailyRecord(
            date=d,
            day_strain=score.get("strain"),
            total_calories=_kj_to_kcal(score.get("kilojoule")),
        )

    # ------------------------------------------------------------------
    # Step 2: merge recovery data (hrv, resting hr, recovery score)
    # ------------------------------------------------------------------
    for rec in recoveries:
        cycle_id = rec.get("cycle_id")
        d = cycle_date.get(cycle_id) if cycle_id else None

        # Fall back to created_at date if the cycle isn't in our range
        if d is None:
            d = _date_from_ts(rec.get("created_at"))
        if d is None:
            logger.debug("Skipping recovery record with no resolvable date: %s", rec)
            continue

        score = rec.get("score") or {}
        if rec.get("score_state") != "SCORED":
            logger.debug("Recovery for %s has score_state=%s", d, rec.get("score_state"))

        existing = records.get(d, DailyRecord(date=d))
        existing.recovery_score = score.get("recovery_score")
        existing.hrv            = score.get("hrv_rmssd_milli")
        existing.resting_hr     = score.get("resting_heart_rate")
        records[d] = existing

    # ------------------------------------------------------------------
    # Step 3: merge sleep data (sleep score, duration, debt, resp. rate)
    # ------------------------------------------------------------------
    for sleep in sleeps:
        cycle_id = sleep.get("cycle_id")
        d = cycle_date.get(cycle_id) if cycle_id else None

        # Fall back to the end timestamp (when you woke up)
        if d is None:
            d = _date_from_ts(sleep.get("end") or sleep.get("start"))
        if d is None:
            logger.debug("Skipping sleep record with no resolvable date: %s", sleep)
            continue

        score = sleep.get("score") or {}
        if sleep.get("score_state") != "SCORED":
            logger.debug("Sleep for %s has score_state=%s", d, sleep.get("score_state"))

        stage  = score.get("stage_summary") or {}
        needed = score.get("sleep_needed") or {}

        existing = records.get(d, DailyRecord(date=d))
        existing.sleep_score        = score.get("sleep_performance_percentage")
        existing.sleep_duration_hrs = _ms_to_hrs(_total_sleep_ms(stage))
        # Whoop v2 field is 'need_from_sleep_debt_milli', not 'sleep_debt_milli'
        existing.sleep_debt_hrs     = _ms_to_hrs(needed.get("need_from_sleep_debt_milli"))
        existing.respiratory_rate   = score.get("respiratory_rate")
        records[d] = existing

    # ------------------------------------------------------------------
    # Step 4: merge workout data — sum strain, duration, active calories
    # across all sessions in the same calendar day.
    # ------------------------------------------------------------------
    for workout in workouts:
        d = _date_from_ts(workout.get("start"))
        if d is None:
            logger.debug("Skipping workout with no start timestamp: %s", workout)
            continue

        score = workout.get("score") or {}
        if workout.get("score_state") != "SCORED":
            logger.debug("Workout on %s has score_state=%s", d, workout.get("score_state"))

        strain   = score.get("strain")
        kj       = score.get("kilojoule")
        duration = _duration_min(workout.get("start"), workout.get("end"))

        existing = records.get(d, DailyRecord(date=d))
        existing.workout_strain      = _add_to_running_total(existing.workout_strain, strain)
        existing.workout_duration_min = _add_to_running_total(existing.workout_duration_min, duration)
        existing.active_calories     = _add_to_running_total(existing.active_calories, _kj_to_kcal(kj))
        records[d] = existing

    # ------------------------------------------------------------------
    # Step 5: build the final list — one entry per day in the range.
    # Days with no Whoop data at all become placeholder records.
    # ------------------------------------------------------------------
    total_days = (end - start).days + 1
    result: list[DailyRecord] = []

    for offset in range(total_days):
        d = start + timedelta(days=offset)
        if d in records:
            result.append(records[d])
        else:
            logger.debug("No Whoop data for %s — will write placeholder", d)
            result.append(DailyRecord(date=d, is_placeholder=True))

    return result


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def sync_date_range(
    whoop: WhoopClient,
    notion: NotionRecoveryClient,
    start: date,
    end: date,
) -> dict[str, int]:
    """
    Build DailyRecords for [start, end] and upsert each one into Notion.

    Args:
        whoop:  Authenticated WhoopClient instance.
        notion: NotionRecoveryClient instance.
        start:  First date to sync (inclusive).
        end:    Last date to sync (inclusive).

    Returns:
        Dict with counts: {"created": n, "updated": n, "skipped": n, "errors": n}
    """
    records = build_daily_records(whoop, start, end)
    counts: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    for record in records:
        try:
            result = notion.upsert_recovery_day(record)
            counts[result] += 1

            if not record.is_placeholder:
                logger.info(
                    "Synced %s — recovery=%s  strain=%s  sleep=%s  [%s]",
                    record.date,
                    f"{record.recovery_score:.0f}" if record.recovery_score is not None else "—",
                    f"{record.day_strain:.1f}"     if record.day_strain     is not None else "—",
                    f"{record.sleep_score:.0f}"    if record.sleep_score    is not None else "—",
                    result,
                )
            else:
                logger.info("Placeholder %s [%s]", record.date, result)

        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.error("Failed to sync %s: %s", record.date, exc)

    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync_whoop",
        description=(
            "Sync Whoop recovery data to a Notion database. "
            "Defaults to yesterday if no date range is given."
        ),
    )
    parser.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="First date to sync (inclusive). Defaults to yesterday.",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Last date to sync (inclusive). Defaults to --start (or yesterday).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args()


def main() -> None:
    """Parse arguments, authenticate clients, and run the sync."""
    args = _parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    # Resolve date range
    yesterday = date.today() - timedelta(days=1)
    try:
        start = date.fromisoformat(args.start) if args.start else yesterday
        end   = date.fromisoformat(args.end)   if args.end   else start
    except ValueError as exc:
        logger.error("Invalid date format: %s  (expected YYYY-MM-DD)", exc)
        sys.exit(1)

    if start > end:
        logger.error("--start (%s) must be on or before --end (%s)", start, end)
        sys.exit(1)

    total_days = (end - start).days + 1
    logger.info(
        "Starting Whoop → Notion sync  |  %s → %s  (%d day%s)",
        start, end, total_days, "s" if total_days != 1 else "",
    )

    # Initialise clients
    whoop  = WhoopClient.from_env()
    notion = NotionRecoveryClient.from_env()

    # Run the sync
    sync_start = datetime.now(timezone.utc)
    counts = sync_date_range(whoop, notion, start, end)
    elapsed = (datetime.now(timezone.utc) - sync_start).total_seconds()

    # Summary
    logger.info(
        "Sync complete in %.1fs — created: %d  updated: %d  skipped: %d  errors: %d",
        elapsed,
        counts["created"],
        counts["updated"],
        counts["skipped"],
        counts["errors"],
    )

    if counts["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
