"""
Lacework MCP Server - Fetch Alert Details via Lacework API v2
=============================================================
An MCP server built with FastMCP that exposes Lacework alert operations
as tools for AI agents and LLM integrations.

Endpoints covered (from https://api.lacework.net/api/v2/docs):
  - POST /api/v2/access/tokens          (auth)
  - GET  /api/v2/Alerts                 (list alerts)
  - POST /api/v2/Alerts/search          (search alerts)
  - GET  /api/v2/Alerts/{alertId}       (alert details by scope)
  - GET  /api/v2/Alerts/Entities/{id}   (alert entities)
  - GET  /api/v2/Alerts/EntityDetails/{id}  (entity details)
  - POST /api/v2/Alerts/{alertId}/comment   (post comment)
  - POST /api/v2/Alerts/{alertId}/close     (close alert)
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("lacework-mcp")

# ============================================================================
# CONFIGURATION
# ============================================================================

# Lacework credentials – read from env vars or ~/.lacework.json
LACEWORK_ACCOUNT = os.environ.get("LACEWORK_ACCOUNT", "")
LACEWORK_KEY_ID = os.environ.get("LACEWORK_KEY_ID", "")
LACEWORK_SECRET = os.environ.get("LACEWORK_SECRET", "")

# Optionally load from config file if env vars are empty
if not all([LACEWORK_ACCOUNT, LACEWORK_KEY_ID, LACEWORK_SECRET]):
    config_path = os.path.expanduser("~/.lacework.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            LACEWORK_ACCOUNT = LACEWORK_ACCOUNT or cfg.get("account", "")
            LACEWORK_KEY_ID = LACEWORK_KEY_ID or cfg.get("keyId", "")
            LACEWORK_SECRET = LACEWORK_SECRET or cfg.get("secret", "")
            logger.info("Loaded Lacework credentials from ~/.lacework.json")
        except Exception as e:
            logger.warning(f"Failed to read ~/.lacework.json: {e}")

# Normalise account name (strip scheme / domain suffix)
LACEWORK_ACCOUNT = (
    LACEWORK_ACCOUNT.replace("https://", "")
    .replace("http://", "")
    .split(".")[0]
)

API_BASE = f"https://{LACEWORK_ACCOUNT}.lacework.net/api/v2"
HTTP_TIMEOUT = int(os.environ.get("LACEWORK_TIMEOUT", "60"))

# ============================================================================
# TIME PARSING HELPERS
# ============================================================================

# Regex for relative durations: "2h", "30m", "1d", "7d", "1w"
_RELATIVE_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)

# Regex for natural language: "last 2 hours", "past 30 minutes", "last 1 day"
_NATURAL_RE = re.compile(
    r"^(?:last|past)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|wks?)$",
    re.IGNORECASE,
)

_UNIT_MAP = {
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
}


def parse_time_input(value: str) -> Optional[str]:
    """Parse a flexible time input into an ISO-8601 UTC string.

    Accepted formats:
      - ISO-8601:         '2024-06-01T00:00:00Z'
      - Date only:        '2024-06-01'  (interpreted as start of that day UTC)
      - Relative short:   '2h', '30m', '1d', '7d', '1w'
      - Natural language:  'last 2 hours', 'past 30 minutes', 'last 1 day'

    Returns None if the input is empty/blank.
    """
    if not value or not value.strip():
        return None

    text = value.strip()

    # 1) Already ISO-8601 with time component
    if "T" in text:
        return text

    # 2) Date-only  e.g. '2024-06-01'
    date_match = re.match(r"^\d{4}-\d{2}-\d{2}$", text)
    if date_match:
        return f"{text}T00:00:00Z"

    # 3) Relative shorthand  e.g. '2h', '30m'
    rel = _RELATIVE_RE.match(text)
    if rel:
        amount = int(rel.group(1))
        unit = _UNIT_MAP.get(rel.group(2).lower())
        if unit:
            delta = timedelta(**{unit: amount})
            return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 4) Natural language  e.g. 'last 2 hours'
    nat = _NATURAL_RE.match(text)
    if nat:
        amount = int(nat.group(1))
        unit = _UNIT_MAP.get(nat.group(2).lower())
        if unit:
            delta = timedelta(**{unit: amount})
            return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 5) Fallback – return as-is and let the API decide
    return text

# ============================================================================
# LACEWORK API CLIENT
# ============================================================================


class LaceworkClient:
    """Lightweight async Lacework API v2 client with automatic token management."""

    def __init__(
        self,
        account: str = "",
        key_id: str = "",
        secret: str = "",
    ) -> None:
        self._account = (
            account.replace("https://", "").replace("http://", "").split(".")[0]
            if account else LACEWORK_ACCOUNT
        )
        self._key_id = key_id or LACEWORK_KEY_ID
        self._secret = secret or LACEWORK_SECRET
        self._api_base = f"https://{self._account}.lacework.net/api/v2"
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, refreshing if needed."""
        now = datetime.now(timezone.utc)
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token

        url = f"{self._api_base}/access/tokens"
        headers = {
            "X-LW-UAKS": self._secret,
            "Content-Type": "application/json",
        }
        body = {"keyId": self._key_id, "expiryTime": 3600}

        resp = await self._http.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()

        # Token may be at top level or nested under data[0]
        if "token" in data:
            self._token = data["token"]
        elif "data" in data and len(data["data"]) > 0:
            self._token = data["data"][0].get("token")
        else:
            raise ValueError(f"Unexpected auth response: {json.dumps(data)}")

        self._token_expiry = now + timedelta(seconds=3500)  # small buffer
        logger.info(f"Lacework access token refreshed (account={self._account})")
        return self._token

    async def _auth_headers(self) -> Dict[str, str]:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        headers = await self._auth_headers()
        resp = await self._http.get(
            f"{self._api_base}{path}", headers=headers, params=params
        )
        resp.raise_for_status()
        if resp.status_code == 204:
            return {"data": [], "message": "No data found"}
        return resp.json()

    async def _post(self, path: str, body: Optional[Dict] = None) -> Any:
        headers = await self._auth_headers()
        resp = await self._http.post(
            f"{self._api_base}{path}", headers=headers, json=body or {}
        )
        resp.raise_for_status()
        if resp.status_code == 204:
            return {"message": "Success (No Content)"}
        return resp.json()

    # ------------------------------------------------------------------
    # Alerts API methods
    # ------------------------------------------------------------------

    async def list_alerts(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Any:
        """GET /api/v2/Alerts with optional time range."""
        params: Dict[str, str] = {}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._get("/Alerts", params=params)

    async def search_alerts(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        filters: Optional[List[Dict]] = None,
        returns: Optional[List[str]] = None,
    ) -> Any:
        """POST /api/v2/Alerts/search with optional filters."""
        body: Dict[str, Any] = {}
        if start_time or end_time:
            tf: Dict[str, str] = {}
            if start_time:
                tf["startTime"] = start_time
            if end_time:
                tf["endTime"] = end_time
            body["timeFilter"] = tf
        if filters:
            body["filters"] = filters
        if returns:
            body["returns"] = returns
        return await self._post("/Alerts/search", body)

    async def get_alert_details(self, alert_id: str, scope: str) -> Any:
        """GET /api/v2/Alerts/{alertId}?scope={scope}"""
        return await self._get(f"/Alerts/{alert_id}", params={"scope": scope})

    async def get_alert_entities(self, alert_id: str) -> Any:
        """GET /api/v2/Alerts/Entities/{alertId}"""
        return await self._get(f"/Alerts/Entities/{alert_id}")

    async def get_alert_entity_details(
        self,
        alert_id: str,
        context_entity_type: str,
        entity_value: str,
    ) -> Any:
        """GET /api/v2/Alerts/EntityDetails/{alertId}"""
        return await self._get(
            f"/Alerts/EntityDetails/{alert_id}",
            params={
                "contextEntityType": context_entity_type,
                "entityValue": entity_value,
            },
        )

    async def post_comment(self, alert_id: str, comment: str) -> Any:
        """POST /api/v2/Alerts/{alertId}/comment"""
        return await self._post(f"/Alerts/{alert_id}/comment", {"comment": comment})

    async def close_alert(
        self, alert_id: str, reason: int, comment: Optional[str] = None
    ) -> Any:
        """POST /api/v2/Alerts/{alertId}/close"""
        body: Dict[str, Any] = {"reason": reason}
        if comment:
            body["comment"] = comment
        return await self._post(f"/Alerts/{alert_id}/close", body)


# Singleton default client (uses env/config credentials)
_default_client = LaceworkClient()

# Cache of per-credential clients keyed by (account, key_id) to reuse tokens
_client_cache: Dict[tuple, LaceworkClient] = {}


def _get_client(
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> LaceworkClient:
    """Return a LaceworkClient – the default one or a per-credential instance.

    When credentials are provided (for remote/multi-tenant use), a cached
    client is returned so tokens are reused across calls with the same creds.
    """
    if not any([lacework_account, lacework_key_id, lacework_secret]):
        return _default_client

    account = (
        lacework_account.replace("https://", "").replace("http://", "").split(".")[0]
        if lacework_account else LACEWORK_ACCOUNT
    )
    key_id = lacework_key_id or LACEWORK_KEY_ID
    cache_key = (account, key_id)

    if cache_key not in _client_cache:
        _client_cache[cache_key] = LaceworkClient(
            account=lacework_account,
            key_id=lacework_key_id,
            secret=lacework_secret,
        )
        logger.info(f"Created new Lacework client for account={account}")

    return _client_cache[cache_key]


# ============================================================================
# MCP SERVER
# ============================================================================

mcp = FastMCP(
    "Lacework Alerts MCP Server",
    instructions=(
        "MCP server for Lacework API v2 – list, search, inspect, comment on, "
        "and close security alerts from your Lacework instance."
    ),
)


# ------------------------------------------------------------------
# Tool: list_alerts
# ------------------------------------------------------------------
@mcp.tool()
async def list_alerts(
    start_time: str = "",
    end_time: str = "",
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """List Lacework alerts within an optional time range.

    Args:
        start_time: When to start listing. Accepts multiple formats:
                    - ISO-8601: '2024-01-01T00:00:00Z'
                    - Date only: '2024-01-01'
                    - Relative shorthand: '2h', '30m', '1d', '7d'
                    - Natural language: 'last 2 hours', 'past 30 minutes'
                    Defaults to last 24 hours if omitted.
        end_time:   End of the time range. Same formats as start_time.
                    Defaults to current time if omitted.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with alert list and pagination info.
    """
    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        parsed_start = parse_time_input(start_time)
        parsed_end = parse_time_input(end_time)
        result = await client.list_alerts(
            start_time=parsed_start,
            end_time=parsed_end,
        )
        alerts = result.get("data", [])
        paging = result.get("paging", {})
        summary = {
            "total_alerts": paging.get("totalRows", len(alerts)),
            "returned": paging.get("rows", len(alerts)),
            "alerts": alerts[:50],  # cap to avoid huge payloads
        }
        if paging.get("urls", {}).get("nextPage"):
            summary["next_page_url"] = paging["urls"]["nextPage"]
        return json.dumps(summary, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: search_alerts
# ------------------------------------------------------------------
@mcp.tool()
async def search_alerts(
    start_time: str = "",
    end_time: str = "",
    severity: str = "",
    status: str = "",
    alert_type: str = "",
    returns: str = "",
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Search Lacework alerts with filters and flexible time inputs.

    Supports filtering by severity, status, and alert type.
    Maximum time range per request is 7 days.

    Args:
        start_time: When to start searching. Accepts multiple formats:
                    - ISO-8601: '2024-06-01T00:00:00Z'
                    - Date only: '2024-06-01'
                    - Relative shorthand: '2h', '30m', '1d', '7d'
                    - Natural language: 'last 2 hours', 'past 30 minutes'
                    Default: last 24 hours.
        end_time:   End of time range. Same formats as start_time.
                    Default: now.
        severity:   Filter by severity – Critical, High, Medium, Low, Info.
        status:     Filter by status – Open, Closed.
        alert_type: Filter by alertType (e.g. 'SuspiciousUserFailedLogin').
        returns:    Comma-separated list of fields to return
                    (e.g. 'alertId,alertName,severity,status').
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with matching alerts.
    """
    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        parsed_start = parse_time_input(start_time)
        parsed_end = parse_time_input(end_time)

        filters: List[Dict[str, str]] = []
        if severity:
            filters.append({"field": "severity", "expression": "eq", "value": severity})
        if status:
            filters.append({"field": "status", "expression": "eq", "value": status})
        if alert_type:
            filters.append({"field": "alertType", "expression": "eq", "value": alert_type})

        returns_list = [r.strip() for r in returns.split(",") if r.strip()] if returns else None

        result = await client.search_alerts(
            start_time=parsed_start,
            end_time=parsed_end,
            filters=filters or None,
            returns=returns_list,
        )
        alerts = result.get("data", [])
        paging = result.get("paging", {})
        summary = {
            "total_alerts": paging.get("totalRows", len(alerts)),
            "returned": paging.get("rows", len(alerts)),
            "alerts": alerts[:50],
        }
        if paging.get("urls", {}).get("nextPage"):
            summary["next_page_url"] = paging["urls"]["nextPage"]
        return json.dumps(summary, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: get_alert_details
# ------------------------------------------------------------------
@mcp.tool()
async def get_alert_details(
    alert_id: str,
    scope: str = "Details",
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Get detailed information about a specific Lacework alert.

    Args:
        alert_id: The numeric alert ID (e.g. '813628').
        scope:    The detail scope to retrieve. One of:
                  Details, Investigation, Events, RelatedAlerts,
                  Integrations, Timeline, ObservationTimeline.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with alert detail data for the chosen scope.
    """
    valid_scopes = {
        "Details",
        "Investigation",
        "Events",
        "RelatedAlerts",
        "Integrations",
        "Timeline",
        "ObservationTimeline",
    }
    if scope not in valid_scopes:
        return json.dumps({"error": f"Invalid scope '{scope}'. Must be one of: {sorted(valid_scopes)}"})

    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.get_alert_details(alert_id, scope)
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: get_alert_timeline
# ------------------------------------------------------------------
@mcp.tool()
async def get_alert_timeline(
    alert_id: str,
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Get the timeline of events for a specific Lacework alert.

    Shortcut that calls get_alert_details with scope=Timeline.

    Args:
        alert_id: The numeric alert ID.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with the alert timeline.
    """
    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.get_alert_details(alert_id, "Timeline")
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: get_alert_investigation
# ------------------------------------------------------------------
@mcp.tool()
async def get_alert_investigation(
    alert_id: str,
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Get investigation details for a specific Lacework alert.

    Shortcut that calls get_alert_details with scope=Investigation.

    Args:
        alert_id: The numeric alert ID.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with investigation data.
    """
    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.get_alert_details(alert_id, "Investigation")
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: get_alert_entities
# ------------------------------------------------------------------
@mcp.tool()
async def get_alert_entities(
    alert_id: str,
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """List all entities (machines, IPs, etc.) associated with a Lacework alert.

    Args:
        alert_id: The numeric alert ID.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with entity list.
    """
    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.get_alert_entities(alert_id)
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: get_alert_entity_details
# ------------------------------------------------------------------
@mcp.tool()
async def get_alert_entity_details(
    alert_id: str,
    context_entity_type: str,
    entity_value: str,
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Get detailed context about a specific entity from a Lacework alert.

    Args:
        alert_id:            The numeric alert ID.
        context_entity_type: The entity type – 'IpAddress' or 'Machine'.
        entity_value:        The entity value (e.g. IP address or machine ID).
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string with entity context (VirusTotal, Lacework Labs,
        network activity, etc.).
    """
    if context_entity_type not in ("IpAddress", "Machine"):
        return json.dumps({"error": "context_entity_type must be 'IpAddress' or 'Machine'"})

    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.get_alert_entity_details(
            alert_id, context_entity_type, entity_value
        )
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: post_alert_comment
# ------------------------------------------------------------------
@mcp.tool()
async def post_alert_comment(
    alert_id: str,
    comment: str,
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Post a comment on an alert's timeline in Lacework.

    Args:
        alert_id: The numeric alert ID.
        comment:  The comment text to post.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string confirming the comment was posted.
    """
    if not comment.strip():
        return json.dumps({"error": "comment must not be empty"})

    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.post_comment(alert_id, comment)
        return json.dumps(result, indent=2, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Tool: close_alert
# ------------------------------------------------------------------
@mcp.tool()
async def close_alert(
    alert_id: str,
    reason: int,
    comment: str = "",
    lacework_account: str = "",
    lacework_key_id: str = "",
    lacework_secret: str = "",
) -> str:
    """Close a Lacework alert with a reason.

    A closed alert cannot be reopened.

    Args:
        alert_id: The numeric alert ID.
        reason:   Reason code for closing:
                  0 = Other (comment is required)
                  1 = False positive
                  2 = Not enough information
                  3 = Malicious and have resolution in place
                  4 = Expected because of routine testing
                  5 = Expected behavior
        comment:  Required when reason=0.  Optional otherwise.
        lacework_account: (Remote only) Lacework account name. Omit to use server defaults.
        lacework_key_id:  (Remote only) Lacework API key ID. Omit to use server defaults.
        lacework_secret:  (Remote only) Lacework API secret. Omit to use server defaults.

    Returns:
        JSON string confirming the alert was closed.
    """
    if reason not in range(6):
        return json.dumps({"error": f"reason must be 0-5, got {reason}"})
    if reason == 0 and not comment.strip():
        return json.dumps({"error": "comment is required when reason=0 (Other)"})

    try:
        client = _get_client(lacework_account, lacework_key_id, lacework_secret)
        result = await client.close_alert(alert_id, reason, comment or None)
        return json.dumps({"status": "closed", "alert_id": alert_id, "detail": result}, default=str)
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}", "detail": e.response.text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ============================================================================
# ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Lacework MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport to use (default: stdio)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind for sse/streamable-http (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind for sse/streamable-http (default: 8000)"
    )
    args = parser.parse_args()

    logger.info(
        f"Starting Lacework MCP Server (account={LACEWORK_ACCOUNT}, transport={args.transport})"
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
