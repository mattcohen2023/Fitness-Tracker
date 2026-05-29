"""
whoop_client.py
---------------
Whoop API v2 wrapper.

Handles the full OAuth 2.0 lifecycle:
  - First run: opens browser, catches callback on localhost:8080, saves tokens
  - Subsequent runs: loads saved tokens, silently refreshes when near expiry

All API endpoint URLs are defined in ENDPOINTS so a Whoop path change
requires editing exactly one place.

Usage:
    client = WhoopClient.from_env()
    workouts = client.get_workouts(date(2025, 1, 1), date(2025, 1, 7))
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — update these if Whoop changes their API paths
# ---------------------------------------------------------------------------

BASE_URL = "https://api.prod.whoop.com/developer"

ENDPOINTS: dict[str, str] = {
    "authorize": "https://api.prod.whoop.com/oauth/oauth2/auth",
    "token":     "https://api.prod.whoop.com/oauth/oauth2/token",
    "recovery":  f"{BASE_URL}/v2/recovery",
    "sleep":     f"{BASE_URL}/v2/activity/sleep",    # Note: not /v2/sleep
    "cycle":     f"{BASE_URL}/v2/cycle",
    "workout":   f"{BASE_URL}/v2/activity/workout",  # Note: not /v2/workout
}

SCOPES = "read:recovery read:sleep read:cycles read:workout offline"

# Token file lives next to this source file's project root (gitignored)
_REPO_ROOT = Path(__file__).parent.parent
TOKEN_FILE = _REPO_ROOT / ".whoop_credentials.json"

# Refresh a token if it expires within this many seconds
TOKEN_EXPIRY_BUFFER_SECS = 60

# Retry settings for rate-limit (429) responses
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER_SECS = 60


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WhoopError(Exception):
    """Base class for all Whoop client errors."""


class WhoopAuthError(WhoopError):
    """Raised when authentication or token refresh fails."""


class WhoopAPIError(WhoopError):
    """Raised when a Whoop API call returns an unexpected error."""


# ---------------------------------------------------------------------------
# OAuth callback server (used only on first run)
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth callback code."""

    auth_code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = b"<h2>Authorization successful! You can close this tab.</h2>"
        elif "error" in params:
            _CallbackHandler.error = params["error"][0]
            body = b"<h2>Authorization failed. Check the terminal for details.</h2>"
        else:
            body = b"<h2>Unexpected callback. Check the terminal.</h2>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # noqa: ANN401
        """Suppress default access log output."""


def _run_callback_server(port: int) -> HTTPServer:
    """Start the OAuth callback server in a background thread.

    Returns the server instance so the caller can shut it down.
    """
    server = HTTPServer(("localhost", port), _CallbackHandler)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Token persistence helpers
# ---------------------------------------------------------------------------

def _load_tokens() -> dict[str, Any] | None:
    """Load saved tokens from disk. Returns None if file doesn't exist."""
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read token file %s: %s", TOKEN_FILE, exc)
        return None


def _save_tokens(tokens: dict[str, Any]) -> None:
    """Persist tokens to disk."""
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    logger.debug("Tokens saved to %s", TOKEN_FILE)


def _tokens_are_valid(tokens: dict[str, Any]) -> bool:
    """Return True if the access token is still valid (with buffer)."""
    expires_at = tokens.get("expires_at", 0)
    return time.time() < (expires_at - TOKEN_EXPIRY_BUFFER_SECS)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class WhoopClient:
    """
    Whoop API v2 client.

    Instantiate with WhoopClient.from_env() to load credentials from the
    environment automatically. The client handles token refresh transparently.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._access_token: str | None = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "WhoopClient":
        """
        Create a WhoopClient from environment variables.

        Required env vars: WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET,
        WHOOP_REDIRECT_URI (defaults to http://localhost:8080/callback).
        """
        client_id = os.environ.get("WHOOP_CLIENT_ID", "")
        client_secret = os.environ.get("WHOOP_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8080/callback")

        if not client_id or not client_secret:
            raise WhoopAuthError(
                "WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be set in your .env file."
            )

        return cls(client_id, client_secret, redirect_uri)

    # ------------------------------------------------------------------
    # Auth & token management
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Ensure a valid access token is available.

        - If a saved token exists and is valid: use it.
        - If a saved refresh token exists: silently refresh.
        - Otherwise: run the full browser-based OAuth flow.
        """
        tokens = _load_tokens()

        if tokens and _tokens_are_valid(tokens):
            logger.debug("Using cached access token (expires in >%ds)", TOKEN_EXPIRY_BUFFER_SECS)
            self._access_token = tokens["access_token"]
            return

        if tokens and tokens.get("refresh_token"):
            logger.debug("Access token expired or near expiry — refreshing")
            self._refresh_access_token(tokens["refresh_token"])
            return

        logger.info("No saved credentials found — starting OAuth flow")
        self._run_oauth_flow()

    def _run_oauth_flow(self) -> None:
        """
        Run the full browser-based OAuth 2.0 authorization code flow.

        Opens the Whoop authorization page, waits for the callback on
        localhost:8080, then exchanges the code for tokens.
        """
        # Reset any state from a previous (possibly failed) run
        _CallbackHandler.auth_code = None
        _CallbackHandler.error = None

        redirect_port = int(urllib.parse.urlparse(self._redirect_uri).port or 8080)

        params = {
            "client_id":     self._client_id,
            "redirect_uri":  self._redirect_uri,
            "response_type": "code",
            "scope":         SCOPES,
            "state":         "whoop_fitness_tracker",
        }
        auth_url = ENDPOINTS["authorize"] + "?" + urllib.parse.urlencode(params)

        print("\n" + "=" * 60)
        print("Whoop OAuth — one-time setup")
        print("=" * 60)
        print("Opening your browser to authorize this application.")
        print("If the browser doesn't open, visit this URL manually:\n")
        print(f"  {auth_url}\n")

        server = _run_callback_server(redirect_port)
        webbrowser.open(auth_url)

        # Wait up to 120 seconds for the user to authorize
        timeout = 120
        elapsed = 0
        while _CallbackHandler.auth_code is None and _CallbackHandler.error is None:
            if elapsed >= timeout:
                server.server_close()
                raise WhoopAuthError(
                    f"OAuth callback not received within {timeout}s. "
                    "Did the browser open? Try running again."
                )
            time.sleep(1)
            elapsed += 1

        server.server_close()

        if _CallbackHandler.error:
            raise WhoopAuthError(f"Whoop authorization denied: {_CallbackHandler.error}")

        logger.debug("Received auth code — exchanging for tokens")
        self._exchange_code_for_tokens(_CallbackHandler.auth_code)  # type: ignore[arg-type]
        print("Authorization successful! Tokens saved.\n")

    def _exchange_code_for_tokens(self, code: str) -> None:
        """Exchange an authorization code for access + refresh tokens."""
        payload = {
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  self._redirect_uri,
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
        }
        tokens = self._post_token_endpoint(payload)
        self._store_tokens(tokens)

    def _refresh_access_token(self, refresh_token: str) -> None:
        """Use a refresh token to obtain a new access token."""
        payload = {
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
            "scope":         SCOPES,
        }
        try:
            tokens = self._post_token_endpoint(payload)
            self._store_tokens(tokens)
        except WhoopAuthError:
            logger.warning("Token refresh failed — re-running OAuth flow")
            self._run_oauth_flow()

    def _post_token_endpoint(self, payload: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint and return the parsed JSON response."""
        try:
            resp = self._session.post(ENDPOINTS["token"], data=payload, timeout=30)
        except requests.RequestException as exc:
            raise WhoopAuthError(f"Network error during token request: {exc}") from exc

        if not resp.ok:
            raise WhoopAuthError(
                f"Token endpoint returned {resp.status_code}: {resp.text[:300]}"
            )

        return resp.json()

    def _store_tokens(self, token_response: dict[str, Any]) -> None:
        """Parse a token response, compute expiry, and persist."""
        expires_in = int(token_response.get("expires_in", 3600))
        tokens = {
            "access_token":  token_response["access_token"],
            "refresh_token": token_response.get("refresh_token", ""),
            "expires_at":    time.time() + expires_in,
        }
        self._access_token = tokens["access_token"]
        _save_tokens(tokens)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """
        Authenticated GET with automatic 401 retry (one refresh attempt)
        and 429 back-off.
        """
        if self._access_token is None:
            self.authenticate()

        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._access_token}"}
            try:
                resp = self._session.get(url, headers=headers, params=params, timeout=30)
            except requests.RequestException as exc:
                raise WhoopAPIError(f"Network error calling {url}: {exc}") from exc

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401:
                if attempt == 0:
                    logger.debug("401 received — attempting token refresh")
                    saved = _load_tokens()
                    if saved and saved.get("refresh_token"):
                        self._refresh_access_token(saved["refresh_token"])
                    else:
                        self._run_oauth_flow()
                    continue
                raise WhoopAuthError(
                    "401 after token refresh — re-run the script to re-authorize."
                )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", DEFAULT_RETRY_AFTER_SECS))
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Rate limited (429) — waiting %ds before retry %d/%d",
                        retry_after, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                    continue
                raise WhoopAPIError(
                    f"Rate limit exceeded after {MAX_RETRIES} retries. "
                    "Wait a minute and try again."
                )

            raise WhoopAPIError(
                f"Unexpected response from {url}: "
                f"HTTP {resp.status_code} — {resp.text[:300]}"
            )

        raise WhoopAPIError(f"Exhausted retries for {url}")  # should not reach here

    def _fetch_paginated(
        self,
        endpoint: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """
        Fetch all pages from a Whoop v2 list endpoint for the given date range.

        Whoop uses a 'nextToken' cursor for pagination. This method follows
        the cursor until all records are collected.

        Args:
            endpoint: Full URL for the Whoop endpoint.
            start:    Inclusive start date.
            end:      Exclusive end date (Whoop convention: end is exclusive).

        Returns:
            Flat list of all record dicts across all pages.
        """
        # Whoop expects ISO 8601 with timezone; we use UTC midnight
        def _to_iso(d: date) -> str:
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()

        params: dict[str, str] = {
            "start": _to_iso(start),
            "end":   _to_iso(end + timedelta(days=1)),  # end is exclusive in Whoop API
            "limit": "25",
        }

        records: list[dict[str, Any]] = []

        while True:
            data = self._get(endpoint, params=params)
            records.extend(data.get("records", []))

            next_token = data.get("next_token")
            if not next_token:
                break

            params["nextToken"] = next_token
            logger.debug("Fetching next page (token=%s...)", next_token[:12])

        logger.debug("Fetched %d records from %s", len(records), endpoint)
        return records

    # ------------------------------------------------------------------
    # Public data-fetching methods
    # ------------------------------------------------------------------

    def get_recovery(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        Fetch recovery records for the given date range.

        Each record includes: recovery_score, hrv_rmssd_milli,
        resting_heart_rate, user_calibrating, cycle_id, created_at.

        Args:
            start: First date to include (inclusive).
            end:   Last date to include (inclusive).

        Returns:
            List of raw recovery record dicts from the Whoop API.
        """
        self.authenticate()
        return self._fetch_paginated(ENDPOINTS["recovery"], start, end)

    def get_sleep(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        Fetch sleep records for the given date range.

        Each record includes: sleep_performance_percentage, respiratory_rate,
        stage_summary (total_sleep_time_milli, total_in_bed_time_milli),
        sleep_needed (baseline_milli, sleep_debt_milli).

        Args:
            start: First date to include (inclusive).
            end:   Last date to include (inclusive).

        Returns:
            List of raw sleep record dicts from the Whoop API.
        """
        self.authenticate()
        return self._fetch_paginated(ENDPOINTS["sleep"], start, end)

    def get_cycles(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        Fetch physiological cycle records for the given date range.

        Each record includes: strain, kilojoule (total energy expenditure),
        average_heart_rate, max_heart_rate, and the cycle start/end timestamps.

        Args:
            start: First date to include (inclusive).
            end:   Last date to include (inclusive).

        Returns:
            List of raw cycle record dicts from the Whoop API.
        """
        self.authenticate()
        return self._fetch_paginated(ENDPOINTS["cycle"], start, end)

    def get_workouts(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        Fetch workout records for the given date range.

        Each record includes: strain, kilojoule (workout energy expenditure),
        sport_id, and the workout start/end timestamps.

        Args:
            start: First date to include (inclusive).
            end:   Last date to include (inclusive).

        Returns:
            List of raw workout record dicts from the Whoop API.
        """
        self.authenticate()
        return self._fetch_paginated(ENDPOINTS["workout"], start, end)
