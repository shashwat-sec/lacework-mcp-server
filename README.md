# Lacework Alerts MCP Server

An MCP (Model Context Protocol) server built with **FastMCP** that exposes Lacework API v2 alert operations as tools for AI agents and LLM integrations.

## Quick Start (New Machine Setup)

```bash
# 1. Clone the repo
git clone <repo-url>
cd lacework_mcp_server

# 2. Create a virtual environment (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -e .
# or manually:
# pip install fastmcp httpx

# 4. Configure Lacework credentials (pick one)

# Option A – Config file
cat > ~/.lacework.json <<'EOF'
{
  "account": "yourcompany.lacework.net",
  "keyId": "YOUR_ACCESS_KEY_ID",
  "secret": "YOUR_SECRET_KEY"
}
EOF

# Option B – Environment variables
export LACEWORK_ACCOUNT="yourcompany"
export LACEWORK_KEY_ID="YOUR_ACCESS_KEY_ID"
export LACEWORK_SECRET="YOUR_SECRET_KEY"

# Environment variables take precedence over the config file.

# 5. Run the server
python lacework_mcp_server.py
```

## Tools

| Tool | Description |
|------|-------------|
| `list_alerts` | List alerts within an optional time range (supports relative times like `2h`, `last 2 hours`) |
| `search_alerts` | Search alerts with filters (severity, status, alert type) and flexible time inputs (`30m`, `last 2 hours`, `2024-06-01`) |
| `get_alert_details` | Get detailed info for a specific alert (Details, Investigation, Events, RelatedAlerts, Integrations, Timeline, ObservationTimeline) |
| `get_alert_timeline` | Shortcut – get the timeline for an alert |
| `get_alert_investigation` | Shortcut – get investigation details for an alert |
| `get_alert_entities` | List entities (machines, IPs) associated with an alert |
| `get_alert_entity_details` | Get enriched context for a specific entity (VirusTotal, network activity, etc.) |
| `post_alert_comment` | Post a comment on an alert's timeline |
| `close_alert` | Close an alert with a reason code |


## Running

### Standalone (stdio – local)

```bash
source .venv/bin/activate
python lacework_mcp_server.py
```

### Remote (SSE / Streamable HTTP)

Run the server on a remote host so AI agents can connect over HTTP and pass credentials per-request:

```bash
# SSE transport (default host 0.0.0.0, port 8000)
python lacework_mcp_server.py --transport sse --port 8000

# Streamable HTTP transport
python lacework_mcp_server.py --transport streamable-http --host 0.0.0.0 --port 9000
```

When running remotely, callers pass Lacework credentials as **tool parameters** instead of relying on server-side config:

```json
{
  "name": "search_alerts",
  "arguments": {
    "start_time": "last 2 hours",
    "severity": "Critical",
    "lacework_account": "mycompany",
    "lacework_key_id": "MY_KEY_ID",
    "lacework_secret": "MY_SECRET"
  }
}
```

All three credential fields (`lacework_account`, `lacework_key_id`, `lacework_secret`) are optional on every tool. When omitted, the server falls back to its local config (env vars / `~/.lacework.json`). Clients for different Lacework accounts are cached so tokens are reused across calls.

### With Claude Desktop / VS Code

Add to your MCP settings (e.g. `~/.claude/claude_desktop_config.json` or `.vscode/mcp.json`):

**Local (with `~/.lacework.json` present):**

```json
{
  "mcpServers": {
    "lacework": {
      "command": "/path/to/lacework_mcp_server/.venv/bin/python",
      "args": [
        "/path/to/lacework_mcp_server/lacework_mcp_server.py"
      ]
    }
  }
}
```

**Local (without `~/.lacework.json` – pass creds via env):**

```json
{
  "mcpServers": {
    "lacework": {
      "command": "/path/to/lacework_mcp_server/.venv/bin/python",
      "args": [
        "/path/to/lacework_mcp_server/lacework_mcp_server.py"
      ],
      "env": {
        "LACEWORK_ACCOUNT": "yourcompany",
        "LACEWORK_KEY_ID": "YOUR_KEY_ID",
        "LACEWORK_SECRET": "YOUR_SECRET"
      }
    }
  }
}
```

**Remote (server running elsewhere via SSE):**

```json
{
  "mcpServers": {
    "lacework": {
      "url": "http://your-server-host:8000/sse"
    }
  }
}
```

> For remote servers, credentials are passed as tool parameters on each call (`lacework_account`, `lacework_key_id`, `lacework_secret`).

## API Reference

Based on the [Lacework API v2 documentation](https://api.lacework.net/api/v2/docs):

- **Authentication**: Uses `POST /api/v2/access/tokens` with automatic token refresh
- **Alerts**: Full CRUD via `/api/v2/Alerts` endpoints
- **Rate limits**: 480 requests/hour per functionality
- **Time ranges**: Max 7 days per request; default is last 24 hours
