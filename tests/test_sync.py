"""
test_sync.py
------------
Unit tests for the Whoop → Notion sync logic.

All tests use hardcoded mock data — no live API calls are made. This lets us
verify the merge/mapping logic in isolation and catch regressions quickly.

Run with:
    python -m pytest tests/ -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.notion_client import DailyRecord
from src.sync_whoop import (
    _add_to_running_total,
    _date_from_ts,
    _duration_min,
    _kj_to_kcal,
    _ms_to_hrs,
    _parse_timestamp,
    _total_sleep_ms,
    build_daily_records,
)


# ---------------------------------------------------------------------------
# Helper factories — build minimal Whoop API record dicts
# ---------------------------------------------------------------------------

def _cycle(
    cycle_id: int,
    start: str,
    *,
    strain: float | None = 10.0,
    kilojoule: float | None = 8000.0,
) -> dict[str, Any]:
    return {
        "id": cycle_id,
        "start": start,
        "end": None,
        "score_state": "SCORED",
        "score": {"strain": strain, "kilojoule": kilojoule},
    }


def _recovery(
    cycle_id: int,
    *,
    recovery_score: float | None = 75.0,
    hrv: float | None = 55.0,
    resting_hr: int | None = 54,
    created_at: str = "2025-01-15T10:00:00.000Z",
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "created_at": created_at,
        "score_state": "SCORED",
        "score": {
            "recovery_score": recovery_score,
            "hrv_rmssd_milli": hrv,
            "resting_heart_rate": resting_hr,
        },
    }


def _sleep(
    cycle_id: int,
    *,
    sleep_perf: float | None = 80.0,
    # 7.5 hours split across three stages (matches Whoop v2 actual field names)
    light_ms: float | None = 9_000_000,   # 2.5 h
    sws_ms: float | None   = 9_000_000,   # 2.5 h
    rem_ms: float | None   = 9_000_000,   # 2.5 h  → total 7.5 h
    sleep_debt_ms: float | None = 1_800_000,   # 0.5 hours
    resp_rate: float | None = 15.2,
    start: str = "2025-01-14T23:00:00.000Z",
    end: str = "2025-01-15T07:00:00.000Z",
) -> dict[str, Any]:
    # Mirrors the actual Whoop v2 /activity/sleep response structure.
    # Note: there is no 'total_sleep_time_milli' field — it must be summed
    # from the three stage fields. Sleep debt is 'need_from_sleep_debt_milli'.
    return {
        "cycle_id": cycle_id,
        "start": start,
        "end": end,
        "score_state": "SCORED",
        "score": {
            "sleep_performance_percentage": sleep_perf,
            "respiratory_rate": resp_rate,
            "stage_summary": {
                "total_light_sleep_time_milli":      light_ms,
                "total_slow_wave_sleep_time_milli":  sws_ms,
                "total_rem_sleep_time_milli":        rem_ms,
                "total_in_bed_time_milli":           (light_ms or 0) + (sws_ms or 0) + (rem_ms or 0) + 600_000,
                "total_awake_time_milli":            600_000,
            },
            "sleep_needed": {
                "baseline_milli":               28_800_000,
                "need_from_sleep_debt_milli":   sleep_debt_ms,
                "need_from_recent_strain_milli": 0,
                "need_from_recent_nap_milli":    0,
            },
        },
    }


def _workout(
    start: str,
    end: str,
    *,
    strain: float | None = 10.0,
    kilojoule: float | None = 1200.0,
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "score_state": "SCORED",
        "score": {"strain": strain, "kilojoule": kilojoule},
    }


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_ms_to_hrs_converts_correctly(self) -> None:
        assert _ms_to_hrs(3_600_000) == 1.0
        assert _ms_to_hrs(7_200_000) == 2.0
        assert _ms_to_hrs(27_000_000) == 7.5

    def test_ms_to_hrs_none_passthrough(self) -> None:
        assert _ms_to_hrs(None) is None

    def test_kj_to_kcal_converts_correctly(self) -> None:
        result = _kj_to_kcal(4184.0)
        assert result == pytest.approx(999.9, abs=1.0)

    def test_kj_to_kcal_none_passthrough(self) -> None:
        assert _kj_to_kcal(None) is None

    def test_duration_min_standard(self) -> None:
        result = _duration_min(
            "2025-01-15T10:00:00.000Z",
            "2025-01-15T11:08:00.000Z",
        )
        assert result == 68.0

    def test_duration_min_missing_start(self) -> None:
        assert _duration_min(None, "2025-01-15T11:00:00.000Z") is None

    def test_duration_min_missing_end(self) -> None:
        assert _duration_min("2025-01-15T10:00:00.000Z", None) is None

    def test_add_to_running_total_both_none(self) -> None:
        assert _add_to_running_total(None, None) is None

    def test_add_to_running_total_first_workout(self) -> None:
        assert _add_to_running_total(None, 5.0) == 5.0

    def test_add_to_running_total_subsequent_workout(self) -> None:
        assert _add_to_running_total(3.0, 5.0) == 8.0

    def test_parse_timestamp_z_suffix(self) -> None:
        dt = _parse_timestamp("2025-01-15T17:00:00.000Z")
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo is not None

    def test_parse_timestamp_offset_suffix(self) -> None:
        dt = _parse_timestamp("2025-01-15T17:00:00+00:00")
        assert dt.date() == date(2025, 1, 15)

    def test_date_from_ts_extracts_utc_date(self) -> None:
        assert _date_from_ts("2025-01-15T17:00:00.000Z") == date(2025, 1, 15)

    def test_date_from_ts_none(self) -> None:
        assert _date_from_ts(None) is None

    def test_date_from_ts_invalid(self) -> None:
        assert _date_from_ts("not-a-timestamp") is None

    def test_total_sleep_ms_sums_three_stages(self) -> None:
        stage = {
            "total_light_sleep_time_milli":     7_280_240,
            "total_slow_wave_sleep_time_milli":  6_752_260,
            "total_rem_sleep_time_milli":        6_210_370,
        }
        assert _total_sleep_ms(stage) == pytest.approx(20_242_870)

    def test_total_sleep_ms_all_none_returns_none(self) -> None:
        assert _total_sleep_ms({}) is None

    def test_total_sleep_ms_partial_fields(self) -> None:
        # If only some stages are present, sum what's there
        stage = {"total_light_sleep_time_milli": 5_000_000}
        assert _total_sleep_ms(stage) == pytest.approx(5_000_000)


# ---------------------------------------------------------------------------
# Unit tests: build_daily_records — record structure and field mapping
# ---------------------------------------------------------------------------

class TestBuildDailyRecordsNormalDay:
    """A typical day: one cycle, one recovery, one sleep, one workout."""

    @pytest.fixture
    def records(self) -> list[DailyRecord]:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value    = [_cycle(1, "2025-01-15T17:00:00.000Z")]
        mock_whoop.get_recovery.return_value  = [_recovery(1)]
        mock_whoop.get_sleep.return_value     = [_sleep(1)]
        mock_whoop.get_workouts.return_value  = [
            _workout("2025-01-15T10:00:00.000Z", "2025-01-15T11:08:00.000Z")
        ]
        return build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))

    def test_returns_one_record(self, records: list[DailyRecord]) -> None:
        assert len(records) == 1

    def test_date_is_correct(self, records: list[DailyRecord]) -> None:
        assert records[0].date == date(2025, 1, 15)

    def test_recovery_fields_populated(self, records: list[DailyRecord]) -> None:
        r = records[0]
        assert r.recovery_score == 75.0
        assert r.hrv == 55.0
        assert r.resting_hr == 54

    def test_sleep_fields_populated(self, records: list[DailyRecord]) -> None:
        r = records[0]
        assert r.sleep_score == 80.0
        assert r.sleep_duration_hrs == 7.5
        assert r.sleep_debt_hrs == 0.5
        assert r.respiratory_rate == 15.2

    def test_workout_fields_populated(self, records: list[DailyRecord]) -> None:
        r = records[0]
        assert r.workout_strain == 10.0
        assert r.workout_duration_min == 68.0

    def test_is_not_placeholder(self, records: list[DailyRecord]) -> None:
        assert records[0].is_placeholder is False


# ---------------------------------------------------------------------------
# Unit tests: rest day — workout fields must be None (not 0)
# ---------------------------------------------------------------------------

class TestRestDay:
    """A day with cycle + recovery + sleep but no workouts."""

    @pytest.fixture
    def record(self) -> DailyRecord:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value    = [_cycle(1, "2025-01-15T17:00:00.000Z")]
        mock_whoop.get_recovery.return_value  = [_recovery(1)]
        mock_whoop.get_sleep.return_value     = [_sleep(1)]
        mock_whoop.get_workouts.return_value  = []
        return build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))[0]

    def test_workout_strain_is_none_not_zero(self, record: DailyRecord) -> None:
        assert record.workout_strain is None, (
            "workout_strain must be None on rest days — writing 0 to Notion "
            "would suggest a 0-strain workout was recorded."
        )

    def test_workout_duration_is_none_not_zero(self, record: DailyRecord) -> None:
        assert record.workout_duration_min is None

    def test_active_calories_is_none_not_zero(self, record: DailyRecord) -> None:
        assert record.active_calories is None

    def test_recovery_still_populated(self, record: DailyRecord) -> None:
        assert record.recovery_score == 75.0


# ---------------------------------------------------------------------------
# Unit tests: multi-workout day — strain and duration must be summed
# ---------------------------------------------------------------------------

class TestMultiWorkoutDay:
    """Two workouts in the same day: strain and duration should be summed."""

    @pytest.fixture
    def record(self) -> DailyRecord:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value    = [_cycle(1, "2025-01-15T17:00:00.000Z")]
        mock_whoop.get_recovery.return_value  = [_recovery(1)]
        mock_whoop.get_sleep.return_value     = [_sleep(1)]
        mock_whoop.get_workouts.return_value  = [
            # Morning lift: 68 min, strain 12.0
            _workout(
                "2025-01-15T08:00:00.000Z",
                "2025-01-15T09:08:00.000Z",
                strain=12.0,
                kilojoule=1500.0,
            ),
            # Evening run: 45 min, strain 8.5
            _workout(
                "2025-01-15T18:00:00.000Z",
                "2025-01-15T18:45:00.000Z",
                strain=8.5,
                kilojoule=900.0,
            ),
        ]
        return build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))[0]

    def test_strain_is_summed(self, record: DailyRecord) -> None:
        assert record.workout_strain == pytest.approx(20.5)

    def test_duration_is_summed(self, record: DailyRecord) -> None:
        # 68 min + 45 min = 113 min
        assert record.workout_duration_min == pytest.approx(113.0)

    def test_active_calories_are_summed(self, record: DailyRecord) -> None:
        expected = _kj_to_kcal(1500.0) + _kj_to_kcal(900.0)  # type: ignore[operator]
        assert record.active_calories == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Unit tests: no-data day — placeholder behaviour
# ---------------------------------------------------------------------------

class TestNoDataDay:
    """A day where Whoop returned no records at all."""

    @pytest.fixture
    def record(self) -> DailyRecord:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value    = []
        mock_whoop.get_recovery.return_value  = []
        mock_whoop.get_sleep.return_value     = []
        mock_whoop.get_workouts.return_value  = []
        return build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))[0]

    def test_is_placeholder(self, record: DailyRecord) -> None:
        assert record.is_placeholder is True

    def test_date_is_correct(self, record: DailyRecord) -> None:
        assert record.date == date(2025, 1, 15)

    def test_all_numeric_fields_are_none(self, record: DailyRecord) -> None:
        """Every numeric field must be None — never 0 — so Notion cells stay blank."""
        assert record.recovery_score    is None
        assert record.hrv               is None
        assert record.resting_hr        is None
        assert record.sleep_score       is None
        assert record.sleep_duration_hrs is None
        assert record.sleep_debt_hrs    is None
        assert record.day_strain        is None
        assert record.workout_strain    is None
        assert record.workout_duration_min is None
        assert record.active_calories   is None
        assert record.total_calories    is None
        assert record.respiratory_rate  is None


# ---------------------------------------------------------------------------
# Unit tests: missing score (Whoop hasn't processed the day yet)
# ---------------------------------------------------------------------------

class TestMissingScore:
    """Whoop returns records but score is None — common for recent days."""

    def test_null_score_does_not_crash(self) -> None:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value = [{
            "id": 1,
            "start": "2025-01-15T17:00:00.000Z",
            "score_state": "PENDING_ALGO",
            "score": None,  # Whoop hasn't scored this cycle yet
        }]
        mock_whoop.get_recovery.return_value  = []
        mock_whoop.get_sleep.return_value     = []
        mock_whoop.get_workouts.return_value  = []

        records = build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))
        assert len(records) == 1
        assert records[0].day_strain is None
        assert records[0].total_calories is None

    def test_null_recovery_score_does_not_crash(self) -> None:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value = [_cycle(1, "2025-01-15T17:00:00.000Z")]
        mock_whoop.get_recovery.return_value = [{
            "cycle_id": 1,
            "created_at": "2025-01-15T10:00:00.000Z",
            "score_state": "PENDING_SLEEP",
            "score": None,
        }]
        mock_whoop.get_sleep.return_value    = []
        mock_whoop.get_workouts.return_value = []

        records = build_daily_records(mock_whoop, date(2025, 1, 15), date(2025, 1, 15))
        assert records[0].recovery_score is None
        assert records[0].hrv is None


# ---------------------------------------------------------------------------
# Unit tests: multi-day range
# ---------------------------------------------------------------------------

class TestMultiDayRange:
    """Requesting 3 days where only 2 have data — the third is a placeholder."""

    def test_range_produces_correct_count(self) -> None:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value = [
            _cycle(1, "2025-01-13T17:00:00.000Z"),
            _cycle(2, "2025-01-14T17:00:00.000Z"),
            # Jan 15 has no data
        ]
        mock_whoop.get_recovery.return_value  = [_recovery(1), _recovery(2)]
        mock_whoop.get_sleep.return_value     = [_sleep(1), _sleep(2)]
        mock_whoop.get_workouts.return_value  = []

        records = build_daily_records(
            mock_whoop, date(2025, 1, 13), date(2025, 1, 15)
        )
        assert len(records) == 3

    def test_records_are_in_chronological_order(self) -> None:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value = [
            _cycle(2, "2025-01-14T17:00:00.000Z"),  # intentionally out of order
            _cycle(1, "2025-01-13T17:00:00.000Z"),
        ]
        mock_whoop.get_recovery.return_value  = []
        mock_whoop.get_sleep.return_value     = []
        mock_whoop.get_workouts.return_value  = []

        records = build_daily_records(
            mock_whoop, date(2025, 1, 13), date(2025, 1, 14)
        )
        assert records[0].date == date(2025, 1, 13)
        assert records[1].date == date(2025, 1, 14)

    def test_missing_day_is_placeholder(self) -> None:
        mock_whoop = MagicMock()
        mock_whoop.get_cycles.return_value = [
            _cycle(1, "2025-01-13T17:00:00.000Z"),
            # Jan 14 missing
            _cycle(3, "2025-01-15T17:00:00.000Z"),
        ]
        mock_whoop.get_recovery.return_value  = []
        mock_whoop.get_sleep.return_value     = []
        mock_whoop.get_workouts.return_value  = []

        records = build_daily_records(
            mock_whoop, date(2025, 1, 13), date(2025, 1, 15)
        )
        assert records[1].date == date(2025, 1, 14)
        assert records[1].is_placeholder is True
