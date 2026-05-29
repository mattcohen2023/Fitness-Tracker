"""
notion_client.py
----------------
Notion API wrapper for the Whoop Recovery Log database.

Exposes a single public method:
    upsert_recovery_day(record) -> Literal["created", "updated", "skipped"]

The upsert is fully idempotent: re-running the sync for any date range
is always safe. Placeholder rows (no Whoop data) are only written if no
row already exists for that date, protecting real data from being
overwritten with blanks.

The DailyRecord dataclass is defined here because it represents the exact
shape of a row in the Notion database. sync_whoop.py imports it from here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

from dotenv import load_dotenv
from notion_client import Client as NotionSDKClient
from notion_client.errors import APIResponseError

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notion property name constants
#
# These must exactly match the column names in your Whoop Recovery Log
# database. If you rename a column in Notion, update the value here.
# ---------------------------------------------------------------------------

# The default Notion title property (always present in every database).
# Notion requires one title property; we populate it with the date string
# ("2025-01-15") so rows are identifiable in the Notion UI.
# To find yours: query the database and look for the property with "id": "title".
NOTION_TITLE_PROPERTY = "Day"

PROPERTY_NAMES: dict[str, str] = {
    "date":             "Date",
    "recovery_score":   "Recovery Score",
    "hrv":              "HRV",
    "resting_hr":       "Resting Heart Rate",
    "sleep_score":      "Sleep Score",
    "sleep_duration":   "Sleep Duration",
    "sleep_debt":       "Sleep Debt",
    "day_strain":       "Day Strain",
    "workout_strain":   "Workout Strain",
    "workout_duration": "Workout Duration",
    "active_calories":  "Active Calories",
    "total_calories":   "Total Calories",
    "respiratory_rate": "Respiratory Rate",
    "sync_timestamp":   "Sync Timestamp",
    "notes":            "Notes",
}


# ---------------------------------------------------------------------------
# DailyRecord — the internal representation of one day's data
# ---------------------------------------------------------------------------

@dataclass
class DailyRecord:
    """
    One day's worth of Whoop data, ready to write to Notion.

    Fields that are None are written as blank cells in Notion (not 0).
    This is intentional: a rest day has no workout strain, and a night
    without a Whoop produces a placeholder with most fields as None.

    Attributes:
        date:                 Calendar date this record covers.
        recovery_score:       Whoop recovery score, 0–100.
        hrv:                  Heart rate variability in milliseconds (rmssd).
        resting_hr:           Resting heart rate in bpm.
        sleep_score:          Sleep performance percentage, 0–100.
        sleep_duration_hrs:   Total sleep time in hours (decimal).
        sleep_debt_hrs:       Accumulated sleep debt in hours.
        day_strain:           Whoop day strain score, 0–21.
        workout_strain:       Sum of all workout strain scores. None on rest days.
        workout_duration_min: Total workout duration in minutes. None on rest days.
        active_calories:      Kilocalories burned during workouts.
        total_calories:       Total daily energy expenditure in kilocalories.
        respiratory_rate:     Average respiratory rate in breaths per minute.
        sync_timestamp:       UTC datetime when this record was last synced.
        notes:                Optional free-text notes. Always None for auto-synced rows.
        is_placeholder:       True when Whoop returned no data for this date.
    """

    date: date
    recovery_score: float | None = None
    hrv: float | None = None
    resting_hr: int | None = None
    sleep_score: float | None = None
    sleep_duration_hrs: float | None = None
    sleep_debt_hrs: float | None = None
    day_strain: float | None = None
    workout_strain: float | None = None
    workout_duration_min: float | None = None
    active_calories: float | None = None
    total_calories: float | None = None
    respiratory_rate: float | None = None
    sync_timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    notes: str | None = None
    is_placeholder: bool = False


# ---------------------------------------------------------------------------
# Notion client
# ---------------------------------------------------------------------------

class NotionRecoveryClient:
    """
    Thin wrapper around the Notion SDK for the Whoop Recovery Log database.

    All write operations go through upsert_recovery_day(), which guarantees
    idempotency: running the same sync twice will update the existing row
    rather than creating a duplicate.
    """

    # Notion API version to use. Pinned to 2022-06-28 (the last stable release
    # before breaking changes in 2025-09-03 that dropped databases.query() and
    # changed other endpoint behaviours). Update only after testing.
    NOTION_API_VERSION = "2022-06-28"

    def __init__(self, api_key: str, database_id: str) -> None:
        self._client = NotionSDKClient(
            auth=api_key,
            notion_version=self.NOTION_API_VERSION,
        )
        self._database_id = database_id

    @classmethod
    def from_env(cls) -> "NotionRecoveryClient":
        """
        Create a NotionRecoveryClient from environment variables.

        Required env vars: NOTION_API_KEY, WHOOP_RECOVERY_DB_ID.
        """
        api_key = os.environ.get("NOTION_API_KEY", "")
        database_id = os.environ.get("WHOOP_RECOVERY_DB_ID", "")

        if not api_key:
            raise ValueError("NOTION_API_KEY must be set in your .env file.")
        if not database_id:
            raise ValueError("WHOOP_RECOVERY_DB_ID must be set in your .env file.")

        return cls(api_key, database_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_recovery_day(
        self,
        record: DailyRecord,
    ) -> Literal["created", "updated", "skipped"]:
        """
        Insert or update a recovery row in the Whoop Recovery Log database.

        Behavior:
          - Real data, no existing row  → CREATE new row
          - Real data, row exists       → UPDATE existing row
          - Placeholder, no existing row → CREATE placeholder row
          - Placeholder, row exists     → SKIP (never overwrite real data with blanks)

        Args:
            record: The DailyRecord to write.

        Returns:
            "created", "updated", or "skipped".

        Raises:
            NotionSyncError: If the Notion API call fails.
        """
        existing_page = self._find_page_for_date(record.date)

        if existing_page is not None:
            if record.is_placeholder:
                logger.info(
                    "Skipped %s — placeholder not written because a row already exists",
                    record.date,
                )
                return "skipped"

            # Real data: update the existing row in-place
            self._update_page(existing_page["id"], record)
            logger.info("Updated %s (page %s)", record.date, existing_page["id"][:8])
            return "updated"

        # No existing row: create it (real data or placeholder)
        self._create_page(record)
        action = "placeholder" if record.is_placeholder else "row"
        logger.info("Created %s %s", action, record.date)
        return "created"

    # ------------------------------------------------------------------
    # Private: Notion query helpers
    # ------------------------------------------------------------------

    def _find_page_for_date(self, target_date: date) -> dict[str, Any] | None:
        """
        Query the database for an existing row matching target_date.

        Uses client.request() directly because databases.query() was removed
        from the notion-client SDK wrapper in v2.x. The underlying REST
        endpoint (POST /databases/{id}/query) is stable Notion API v1.

        Returns the first matching page dict, or None if no row exists.
        """
        date_str = target_date.isoformat()  # "YYYY-MM-DD"
        try:
            response = self._client.request(
                path=f"databases/{self._database_id}/query",
                method="POST",
                body={
                    "filter": {
                        "property": PROPERTY_NAMES["date"],
                        "date": {"equals": date_str},
                    }
                },
            )
        except APIResponseError as exc:
            raise NotionSyncError(
                f"Failed to query Notion database {self._database_id[:8]}... "
                f"for date {date_str}: {exc}"
            ) from exc

        results = response.get("results", [])
        if len(results) > 1:
            logger.warning(
                "Found %d rows for %s — expected 1. Using the first.",
                len(results),
                date_str,
            )
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Private: Notion write helpers
    # ------------------------------------------------------------------

    def _create_page(self, record: DailyRecord) -> None:
        """Create a new page (row) in the recovery database."""
        try:
            self._client.pages.create(
                parent={"database_id": self._database_id},
                properties=self._build_properties(record),
            )
        except APIResponseError as exc:
            self._raise_property_error(exc, record.date)

    def _update_page(self, page_id: str, record: DailyRecord) -> None:
        """Update an existing page with fresh data."""
        try:
            self._client.pages.update(
                page_id=page_id,
                properties=self._build_properties(record),
            )
        except APIResponseError as exc:
            self._raise_property_error(exc, record.date)

    def _build_properties(self, record: DailyRecord) -> dict[str, Any]:
        """
        Convert a DailyRecord into a Notion properties payload.

        Only non-None fields are included in the payload. Omitting a key
        entirely leaves the Notion cell blank (not 0), which is the desired
        behaviour for rest days and placeholder rows.
        """
        p = PROPERTY_NAMES  # local alias for brevity

        # Title property: always set so the row has a visible name in Notion
        props: dict[str, Any] = {
            NOTION_TITLE_PROPERTY: {
                "title": [{"text": {"content": record.date.isoformat()}}]
            },
        }

        # Date property (the primary identifier used for upsert queries)
        props[p["date"]] = {"date": {"start": record.date.isoformat()}}

        # Sync Timestamp (always written so we know when the row was last touched)
        props[p["sync_timestamp"]] = {
            "date": {"start": record.sync_timestamp.isoformat()}
        }

        # Numeric fields — only written if we actually have a value
        numeric_fields: list[tuple[str, float | int | None]] = [
            (p["recovery_score"],   record.recovery_score),
            (p["hrv"],              record.hrv),
            (p["resting_hr"],       record.resting_hr),
            (p["sleep_score"],      record.sleep_score),
            (p["sleep_duration"],   record.sleep_duration_hrs),
            (p["sleep_debt"],       record.sleep_debt_hrs),
            (p["day_strain"],       record.day_strain),
            (p["workout_strain"],   record.workout_strain),
            (p["workout_duration"], record.workout_duration_min),
            (p["active_calories"],  record.active_calories),
            (p["total_calories"],   record.total_calories),
            (p["respiratory_rate"], record.respiratory_rate),
        ]
        for notion_name, value in numeric_fields:
            if value is not None:
                props[notion_name] = {"number": round(value, 2)}

        # Notes (rich text) — only written if present
        if record.notes:
            props[p["notes"]] = {
                "rich_text": [{"text": {"content": record.notes}}]
            }

        return props

    @staticmethod
    def _raise_property_error(exc: APIResponseError, record_date: date) -> None:
        """
        Re-raise an APIResponseError with an actionable message.

        Notion returns 400 when a property name doesn't match the schema,
        which is the most common setup mistake.
        """
        if exc.status == 400:
            raise NotionSyncError(
                f"Notion returned 400 for {record_date}. This usually means a "
                f"property name in PROPERTY_NAMES doesn't match your database schema.\n"
                f"Check that every key in notion_client.PROPERTY_NAMES exactly matches "
                f"a column name in your Whoop Recovery Log database (including "
                f"capitalisation and spacing).\n"
                f"Original error: {exc}"
            ) from exc
        raise NotionSyncError(
            f"Notion API error for {record_date}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class NotionSyncError(Exception):
    """Raised when a Notion read or write operation fails."""
