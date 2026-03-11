"""
Custom MCP stdio server providing filtered HomeAssistant state queries and device control.

Uses HA REST API directly instead of the built-in MCP server's GetLiveContext,
which always dumps all entities (~19KB+) regardless of what is needed.

Tools:
  - list_entities:            Filter entities by domain/state/name via POST /api/template
  - get_entity_state:         Full state+attributes for one entity via GET /api/states/{id}
  - get_entity_history:       Historical state changes via GET /api/history/period
  - get_automation_config:    Automation triggers/conditions/actions via /api/config/automation/config
  - get_script_config:        Script action sequence via /api/config/script/config
  - get_logbook:              Human-readable activity log via GET /api/logbook
  - get_calendar_events:      Calendar events via GET /api/calendars
  - list_services:            Available service domains and parameters via GET /api/services
  - get_system_info:          HA version, timezone, location via GET /api/config
  - get_error_log:            Recent HA error log via GET /api/error_log
  - get_camera_snapshot_url:  Viewable snapshot URL for a camera entity
  - check_config:             Validate configuration.yaml via POST /api/config/core/check_config
  - create_automation:        Create or update an automation via POST /api/config/automation/config
  - delete_automation:        Delete an automation via DELETE /api/config/automation/config/{id}
  - create_script:            Create or update a script via POST /api/config/script/config/{id}
  - delete_script:            Delete a script via DELETE /api/config/script/config/{id}
  - create_helper:            Create or update an input_* helper via POST /api/config/{type}/config/{id}
  - call_service:             Call any HA service via POST /api/services/{domain}/{service}
  - list_dashboards:          List all Lovelace dashboards via WS lovelace/dashboards/list
  - get_dashboard_config:     Get Lovelace dashboard config via WS lovelace/config
  - update_dashboard_config:  Write Lovelace dashboard config via WS lovelace/config/save
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
import websockets
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ── Permission flags (read once at startup from env) ──────────────────────────
_READONLY = os.environ.get("HA_READONLY", "false").lower() == "true"
_ALLOW_DEVICE_CONTROL = (
    not _READONLY
    and os.environ.get("HA_ALLOW_DEVICE_CONTROL", "true").lower() == "true"
)
_ALLOW_SCRIPT_EXECUTION = (
    _ALLOW_DEVICE_CONTROL
    and os.environ.get("HA_ALLOW_SCRIPT_EXECUTION", "true").lower() == "true"
)
_ALLOW_CONFIG_WRITE = (
    not _READONLY
    and os.environ.get("HA_ALLOW_CONFIG_WRITE", "false").lower() == "true"
)
_ALLOW_DELETES = (
    not _READONLY
    and os.environ.get("HA_ALLOW_DELETES", "false").lower() == "true"
)

_ENABLED_TOOLS: frozenset[str] = frozenset(
    {
        # Read tools — always available
        "list_entities", "get_entity_state", "get_entity_history",
        "get_automation_config", "get_script_config", "get_logbook",
        "get_calendar_events", "list_services", "get_system_info",
        "get_error_log", "get_camera_snapshot_url", "check_config",
        "list_dashboards", "get_dashboard_config",
    }
    | ({"call_service"} if _ALLOW_DEVICE_CONTROL else set())
    | ({"create_automation", "create_script", "create_helper", "update_dashboard_config"} if _ALLOW_CONFIG_WRITE else set())
    | ({"delete_automation", "delete_script"} if _ALLOW_DELETES else set())
)

_SCRIPT_DOMAINS = {"script", "automation"}
_DOMAIN_RE = re.compile(r"^[a-z_]+$")


def _check_permission(name: str, arguments: dict[str, Any]) -> str | None:
    """Return an error string if the call is not permitted, else None."""
    if name not in _ENABLED_TOOLS:
        return f"'{name}' is disabled by the deployment configuration."
    if name == "call_service" and not _ALLOW_SCRIPT_EXECUTION:
        if arguments.get("domain") in _SCRIPT_DOMAINS:
            return (
                f"Triggering '{arguments.get("domain")}' services is disabled "
                "by the deployment configuration."
            )
    return None


def _http_error(response: httpx.Response, context: str = "") -> list[types.TextContent]:
    """Return a TextContent describing an HTTP error from HA, including the response body."""
    detail = response.text.strip() or "(no body)"
    prefix = f"{context}: " if context else ""
    return [types.TextContent(
        type="text",
        text=f"{prefix}HA returned HTTP {response.status_code}: {detail}",
    )]

# ─────────────────────────────────────────────────────────────────────────────

server = Server("ha-rest-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    all_tools = [
        types.Tool(
            name="list_entities",
            description=(
                "List HomeAssistant entities filtered by domain, state, and/or name keyword. "
                "Returns entity IDs, friendly names, current states, domain, device_class, and is_group. "
                "Use name_filter to discover entities by keyword when you don't know the entity_id "
                "(e.g. name_filter='garbage' or name_filter='bin' to find waste collection entities, "
                "name_filter='thermostat' to find climate controls). "
                "Use this instead of GetLiveContext for all state queries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Entity domain to filter by, e.g. 'light', 'switch', "
                            "'sensor', 'climate', 'media_player'. Omit to include all domains."
                        ),
                    },
                    "state": {
                        "type": "string",
                        "description": (
                            "State value to filter by, e.g. 'on', 'off', 'playing', 'idle'. "
                            "Omit to include all states."
                        ),
                    },
                    "name_filter": {
                        "type": "string",
                        "description": (
                            "Case-insensitive keyword to match against entity names and entity IDs. "
                            "Use this to discover entities for a topic when you don't know the exact "
                            "entity_id (e.g. 'garbage', 'bin', 'recycling', 'front door', 'basement'). "
                            "Can be combined with domain and state."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_entity_state",
            description=(
                "Get the full state and attributes of a single HomeAssistant entity by its entity_id. "
                "Use this when you need detailed information (e.g. brightness, colour, temperature) "
                "for a specific entity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity ID, e.g. 'light.living_room' or 'sensor.temperature'.",
                    }
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_entity_history",
            description=(
                "Get the historical state changes for a HomeAssistant entity over a time window. "
                "Use this to answer questions about recent activity: whether an appliance ran, "
                "when a sensor last changed, how a value trended over time, etc. "
                "Returns a list of state entries with timestamps, each showing the value at that time. "
                "Only significant state changes are returned by default (not every poll)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity ID to retrieve history for, e.g. 'sensor.laundry_vibration_sensor_x_axis'.",
                    },
                    "hours_ago": {
                        "type": "number",
                        "description": "How many hours back to retrieve history for. Defaults to 24. Use a larger value for longer lookback.",
                    },
                    "significant_changes_only": {
                        "type": "boolean",
                        "description": "If true (default), only return entries where the state value actually changed. Set to false to see every recorded data point.",
                    },
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_automation_config",
            description=(
                "Get the full internal configuration of a HomeAssistant automation: "
                "its triggers, conditions, and actions. Use this when the user asks "
                "what an automation does, when it runs, or how it is configured. "
                "Provide the automation's entity_id (e.g. 'automation.morning_lights')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The automation entity ID, e.g. 'automation.morning_lights'.",
                    },
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_script_config",
            description=(
                "Get the full action sequence of a HomeAssistant script. "
                "Use this when the user asks what a script does or how it is configured. "
                "Provide the script's entity_id (e.g. 'script.bedtime_routine'). "
                "Use list_entities(domain='script') to find the entity_id if unknown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The script entity ID, e.g. 'script.bedtime_routine'.",
                    },
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_logbook",
            description=(
                "Get human-readable logbook entries describing what happened in the home: "
                "device state changes, automation triggers, script runs, etc. "
                "Use this to answer questions like 'when did the front door last open?', "
                "'what triggered the hallway light?', 'what happened at 2am?'. "
                "Filter to a specific entity with entity_id, or omit for a broad overview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Optional entity ID to narrow logbook entries to one entity.",
                    },
                    "hours_ago": {
                        "type": "number",
                        "description": "How many hours back to query. Defaults to 24.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_calendar_events",
            description=(
                "Get upcoming events from a HomeAssistant calendar entity. "
                "Use this for questions about schedules, reminders, or upcoming events "
                "('what's on my calendar this week?', 'when is bin collection?', 'any events today?'). "
                "Omit calendar_entity_id to list all available calendars first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "calendar_entity_id": {
                        "type": "string",
                        "description": (
                            "Calendar entity ID, e.g. 'calendar.home'. "
                            "Omit to list all available calendars."
                        ),
                    },
                    "days_ahead": {
                        "type": "number",
                        "description": "Number of days ahead to query. Defaults to 7.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="list_services",
            description=(
                "List available HomeAssistant service domains and their services, including "
                "accepted parameters. Use this when you need to know what services are available "
                "for a domain, or what parameters a service accepts before calling it. "
                "Provide domain to limit results (e.g. 'climate'), or omit to see all domains."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain to filter by, e.g. 'light', 'climate'. Omit to list all domains.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_system_info",
            description=(
                "Get HomeAssistant system information: version, home name, timezone, "
                "location coordinates, unit system (metric/imperial), and loaded integrations. "
                "Use this for questions like 'what version of HA is running?', "
                "'what timezone is the house in?', 'where is the house located?'."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_error_log",
            description=(
                "Get recent entries from the HomeAssistant error log. "
                "Use this when the user asks about errors, failures, or unexpected behaviour — "
                "e.g. 'why did that automation fail?', 'are there any HA errors?'. "
                "Increase tail_lines if the problem may have occurred earlier in the session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tail_lines": {
                        "type": "number",
                        "description": "Number of lines to return from the end of the log. Defaults to 50.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_camera_snapshot_url",
            description=(
                "Get a viewable snapshot URL for a HomeAssistant camera entity. "
                "Returns a URL the user can open in their browser to see the current camera image. "
                "Use list_entities(domain='camera') to find camera entity IDs if unknown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Camera entity ID, e.g. 'camera.front_door'.",
                    },
                },
                "required": ["camera_entity_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="check_config",
            description=(
                "Validate the HomeAssistant configuration.yaml. "
                "Returns 'valid' or 'invalid' with any error details. "
                "Use this when the user asks if their config is valid, or after they report "
                "that HA restarted with errors."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="create_automation",
            description=(
                "Create a new HomeAssistant automation, or update an existing one. "
                "Provide a config dict with at minimum: alias (string), trigger (list), action (list). "
                "Optional fields: description (string), condition (list), mode (single/restart/queued/parallel). "
                "To update an existing automation, provide automation_id (the internal ID from get_automation_config). "
                "The automation is activated automatically after creation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "object",
                        "description": (
                            "Automation configuration object. Required keys: alias (string), "
                            "trigger (list of trigger dicts), action (list of action dicts). "
                            "Optional: description (string), condition (list), "
                            "mode ('single'|'restart'|'queued'|'parallel', default 'single')."
                        ),
                    },
                    "automation_id": {
                        "type": "string",
                        "description": (
                            "Internal automation ID to update an existing automation. "
                            "Obtain from get_automation_config. Omit to create a new automation."
                        ),
                    },
                },
                "required": ["config"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="delete_automation",
            description=(
                "Delete a HomeAssistant automation by its internal config ID. "
                "Obtain the config ID from get_automation_config (the 'id' field in the state attributes, "
                "not the entity_id). The entity_id is 'automation.xxx'; the config ID is a string like '1a2b3c'. "
                "This cannot be undone."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "string",
                        "description": "Internal automation config ID (not the entity_id). Obtain from get_automation_config.",
                    },
                },
                "required": ["automation_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="create_script",
            description=(
                "Create a new HomeAssistant script, or update an existing one. "
                "The script_id becomes the entity ID key (e.g. 'bedtime_routine' → 'script.bedtime_routine'). "
                "The config must include alias and sequence (list of actions). "
                "The script is activated automatically after creation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_id": {
                        "type": "string",
                        "description": "Unique script identifier (snake_case), e.g. 'bedtime_routine'. Becomes the entity_id key.",
                    },
                    "config": {
                        "type": "object",
                        "description": (
                            "Script configuration. Required: alias (string), sequence (list of action dicts). "
                            "Optional: description (string), mode ('single'|'restart'|'queued'|'parallel')."
                        ),
                    },
                },
                "required": ["script_id", "config"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="delete_script",
            description=(
                "Delete a HomeAssistant script by its script_id. "
                "The script_id is the entity_id key without the 'script.' prefix "
                "(e.g. for 'script.bedtime_routine', use script_id='bedtime_routine'). "
                "This cannot be undone."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_id": {
                        "type": "string",
                        "description": "Script ID without 'script.' prefix, e.g. 'bedtime_routine'.",
                    },
                },
                "required": ["script_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="create_helper",
            description=(
                "Create or update a HomeAssistant input helper (input_boolean, input_number, "
                "input_text, input_select, input_datetime, input_button). "
                "The helper_id becomes part of the entity_id "
                "(e.g. helper_type='input_boolean', helper_id='vacation_mode' → 'input_boolean.vacation_mode'). "
                "Note: creating brand-new helpers via the API may not always work in all HA versions; "
                "updating existing helpers is more reliable. For persistent new helpers, prefer the HA UI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "helper_type": {
                        "type": "string",
                        "description": (
                            "Helper type. One of: 'input_boolean', 'input_number', 'input_text', "
                            "'input_select', 'input_datetime', 'input_button'."
                        ),
                    },
                    "helper_id": {
                        "type": "string",
                        "description": "Unique identifier (snake_case), e.g. 'vacation_mode'.",
                    },
                    "config": {
                        "type": "object",
                        "description": (
                            "Helper configuration. Always include 'name' (friendly name). "
                            "input_boolean: {name, icon?, initial?}. "
                            "input_number: {name, min, max, step?, initial?, unit_of_measurement?, mode?}. "
                            "input_text: {name, initial?, min?, max?, pattern?, mode?}. "
                            "input_select: {name, options (list of strings), initial?}. "
                            "input_datetime: {name, has_date?, has_time?, initial?}. "
                            "input_button: {name, icon?}."
                        ),
                    },
                },
                "required": ["helper_type", "helper_id", "config"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="call_service",
            description=(
                "Call a HomeAssistant service to control any device. "
                "Works for ALL entities regardless of voice-assistant exposure settings. "
                "Use list_entities first to find the entity_id, then call the service.\n"
                "Common examples:\n"
                "  Turn off light:    domain='light',   service='turn_off',          entity_id='light.coffee_bar_light'\n"
                "  Turn on light:     domain='light',   service='turn_on',           entity_id='light.living_room'\n"
                "  Set brightness:    domain='light',   service='turn_on',           entity_id='light.xxx', brightness_pct=80\n"
                "  Set color temp:    domain='light',   service='turn_on',           entity_id='light.xxx', color_temp=300\n"
                "  Toggle switch:     domain='switch',  service='toggle',            entity_id='switch.xxx'\n"
                "  Set thermostat:    domain='climate', service='set_temperature',   entity_id='climate.xxx', temperature=21, hvac_mode='heat'\n"
                "  Pause media:       domain='media_player', service='media_pause',  entity_id='media_player.xxx'\n"
                "  Set volume:        domain='media_player', service='volume_set',   entity_id='media_player.xxx', volume_level=0.5\n"
                "  Run script:        domain='script',  service='turn_on',           entity_id='script.xxx'\n"
                "  Trigger automation: domain='automation', service='trigger',       entity_id='automation.xxx'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain, e.g. 'light', 'switch', 'climate', 'media_player', 'script', 'automation'.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Service name, e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature', 'media_pause'.",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity ID, e.g. 'light.coffee_bar_light'. Use list_entities to find this if unknown.",
                    },
                },
                "required": ["domain", "service", "entity_id"],
                "additionalProperties": True,
            },
        ),
        types.Tool(
            name="list_dashboards",
            description=(
                "List all Lovelace dashboards configured in HomeAssistant. "
                "Returns each dashboard's url_path, title, icon, and whether it requires admin access. "
                "The default dashboard has url_path=null. "
                "Use this before get_dashboard_config or update_dashboard_config to discover available dashboards."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_dashboard_config",
            description=(
                "Get the full Lovelace configuration for a HomeAssistant dashboard. "
                "Returns the dashboard config as a JSON object containing views, cards, and layout. "
                "Always call this before update_dashboard_config so you can base changes on the existing config. "
                "The config can be large for complex dashboards. "
                "Omit url_path to get the default dashboard, or provide the url_path for a named dashboard. "
                "The url_path is the segment after the HA base URL in the browser address bar — "
                "e.g. for 'https://homeassistant.local/dashboard-custom/0' the url_path is 'dashboard-custom' "
                "(the trailing '/0' is a view index, not part of the url_path)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url_path": {
                        "type": "string",
                        "description": (
                            "The dashboard url_path from list_dashboards, e.g. 'lovelace-mobile'. "
                            "Omit to get the default dashboard."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="update_dashboard_config",
            description=(
                "Write a new Lovelace configuration for a HomeAssistant dashboard. "
                "This performs a FULL REPLACEMENT of the dashboard config — partial updates are not supported by the HA API. "
                "You MUST call get_dashboard_config first to read the current config, then modify it before writing. "
                "The config must be a valid Lovelace config object with at minimum a 'views' key containing a list of view objects. "
                "Omit url_path for the default dashboard, or provide it for a named dashboard."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "object",
                        "description": (
                            "The complete Lovelace dashboard config to write. Must include 'views' (list). "
                            "Each view has: title (string), path (string, optional), icon (string, optional), "
                            "cards (list of card objects). "
                            "This replaces the entire dashboard config."
                        ),
                    },
                    "url_path": {
                        "type": "string",
                        "description": (
                            "The dashboard url_path from list_dashboards, e.g. 'lovelace-mobile'. "
                            "Omit to update the default dashboard."
                        ),
                    },
                },
                "required": ["config"],
                "additionalProperties": False,
            },
        ),
    ]
    return [t for t in all_tools if t.name in _ENABLED_TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    error = _check_permission(name, arguments)
    if error:
        return [types.TextContent(type="text", text=error)]

    async with httpx.AsyncClient(headers=_HEADERS, timeout=30) as client:
        if name == "list_entities":
            return await _list_entities(client, arguments)
        elif name == "get_entity_state":
            return await _get_entity_state(client, arguments)
        elif name == "get_entity_history":
            return await _get_entity_history(client, arguments)
        elif name == "get_automation_config":
            return await _get_automation_config(client, arguments)
        elif name == "get_script_config":
            return await _get_script_config(client, arguments)
        elif name == "get_logbook":
            return await _get_logbook(client, arguments)
        elif name == "get_calendar_events":
            return await _get_calendar_events(client, arguments)
        elif name == "list_services":
            return await _list_services(client, arguments)
        elif name == "get_system_info":
            return await _get_system_info(client, arguments)
        elif name == "get_error_log":
            return await _get_error_log(client, arguments)
        elif name == "get_camera_snapshot_url":
            return await _get_camera_snapshot_url(client, arguments)
        elif name == "check_config":
            return await _check_config(client, arguments)
        elif name == "create_automation":
            return await _create_automation(client, arguments)
        elif name == "delete_automation":
            return await _delete_automation(client, arguments)
        elif name == "create_script":
            return await _create_script(client, arguments)
        elif name == "delete_script":
            return await _delete_script(client, arguments)
        elif name == "create_helper":
            return await _create_helper(client, arguments)
        elif name == "call_service":
            return await _call_service(client, arguments)
        elif name == "list_dashboards":
            return await _list_dashboards(client, arguments)
        elif name == "get_dashboard_config":
            return await _get_dashboard_config(client, arguments)
        elif name == "update_dashboard_config":
            return await _update_dashboard_config(client, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")


async def _list_entities(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    domain = args.get("domain")
    state_filter = args.get("state")
    name_filter = args.get("name_filter")

    if domain and not _DOMAIN_RE.fullmatch(domain):
        return [types.TextContent(type="text", text=f"Invalid domain '{domain}': must contain only lowercase letters and underscores.")]

    loop_src = f"states.{domain}" if domain else "states"
    conditions = []
    variables: dict[str, Any] = {}

    if state_filter:
        variables["state_filter"] = state_filter
        conditions.append("s.state == state_filter")
    if name_filter:
        variables["kw"] = name_filter.lower()
        conditions.append("(kw in (s.name | lower) or kw in (s.entity_id | lower))")

    parts = [
        "{% set ns = namespace(items=[]) %}",
        "{% for s in " + loop_src + " %}",
    ]
    if conditions:
        parts.append("{% if " + " and ".join(conditions) + " %}")
    parts += [
        "{% set dc = s.attributes.get('device_class', none) %}",
        "{% set is_grp = s.attributes.get('entity_id') is not none %}",
        "{% set ns.items = ns.items + [dict(entity_id=s.entity_id, name=s.name, state=s.state, domain=s.domain, device_class=dc, is_group=is_grp)] %}",
    ]
    if conditions:
        parts.append("{% endif %}")
    parts += [
        "{% endfor %}",
        "{{ ns.items | tojson }}",
    ]
    template = "".join(parts)

    payload: dict[str, Any] = {"template": template}
    if variables:
        payload["variables"] = variables

    response = await client.post(f"{HA_URL}/api/template", json=payload)
    if response.is_error:
        return _http_error(response, "list_entities")

    raw = response.text.strip()
    try:
        entities = json.loads(raw)
        result = json.dumps(entities, indent=2)
    except json.JSONDecodeError:
        result = raw

    return [types.TextContent(type="text", text=result)]


async def _get_entity_state(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    entity_id = args["entity_id"]
    response = await client.get(f"{HA_URL}/api/states/{entity_id}")
    if response.is_error:
        return _http_error(response, "get_entity_state")
    return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]


async def _get_entity_history(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    entity_id = args["entity_id"]
    hours_ago = args.get("hours_ago", 24)
    significant_only = args.get("significant_changes_only", True)

    start_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)

    params: dict[str, Any] = {
        "filter_entity_id": entity_id,
        "minimal_response": "true",
    }
    if significant_only:
        params["significant_changes_only"] = "true"

    response = await client.get(
        f"{HA_URL}/api/history/period/{start_time.isoformat()}",
        params=params,
    )
    if response.is_error:
        return _http_error(response, "get_entity_history")

    data = response.json()
    # API returns a list of lists (one per entity); flatten to the entity's list
    history = data[0] if data else []
    return [types.TextContent(type="text", text=json.dumps(history, indent=2))]


async def _get_automation_config(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    entity_id = args["entity_id"]

    state_response = await client.get(f"{HA_URL}/api/states/{entity_id}")
    if state_response.is_error:
        return _http_error(state_response, "get_automation_config")
    state = state_response.json()

    automation_id = state.get("attributes", {}).get("id")
    if not automation_id:
        return [types.TextContent(
            type="text",
            text=f"Could not find internal config ID for {entity_id}. "
                 f"Available attributes: {json.dumps(state.get("attributes", {}), indent=2)}",
        )]

    config_response = await client.get(f"{HA_URL}/api/config/automation/config/{automation_id}")
    if config_response.is_error:
        return _http_error(config_response, "get_automation_config")
    return [types.TextContent(type="text", text=json.dumps(config_response.json(), indent=2))]


async def _get_script_config(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    entity_id = args["entity_id"]

    if not entity_id.startswith("script."):
        return [types.TextContent(
            type="text",
            text=f"Expected a script entity_id (e.g. 'script.bedtime_routine'), got: {entity_id}",
        )]

    # Script config endpoint uses the slug after 'script.' directly — no separate id attribute
    script_id = entity_id[len("script."):]
    config_response = await client.get(f"{HA_URL}/api/config/script/config/{script_id}")
    if config_response.is_error:
        return _http_error(config_response, "get_script_config")
    return [types.TextContent(type="text", text=json.dumps(config_response.json(), indent=2))]


async def _get_logbook(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    entity_id = args.get("entity_id")
    hours_ago = args.get("hours_ago", 24)

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_ago)

    params: dict[str, Any] = {"end_time": now.isoformat()}
    if entity_id:
        params["entity_id"] = entity_id

    response = await client.get(f"{HA_URL}/api/logbook/{start.isoformat()}", params=params)
    if response.is_error:
        return _http_error(response, "get_logbook")
    return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]


async def _get_calendar_events(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    calendar_entity_id = args.get("calendar_entity_id")
    days_ahead = args.get("days_ahead", 7)

    if not calendar_entity_id:
        response = await client.get(f"{HA_URL}/api/calendars")
        if response.is_error:
            return _http_error(response, "get_calendar_events")
        return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]

    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days_ahead)

    response = await client.get(
        f"{HA_URL}/api/calendars/{calendar_entity_id}",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    if response.is_error:
        return _http_error(response, "get_calendar_events")
    return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]


async def _list_services(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    domain = args.get("domain")

    response = await client.get(f"{HA_URL}/api/services")
    if response.is_error:
        return _http_error(response, "list_services")
    data = response.json()

    if domain:
        data = [entry for entry in data if entry.get("domain") == domain]

    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def _get_system_info(  # pylint: disable=unused-argument
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    response = await client.get(f"{HA_URL}/api/config")
    if response.is_error:
        return _http_error(response, "get_system_info")
    return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]


async def _get_error_log(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    tail_lines = max(1, int(args.get("tail_lines", 50)))

    response = await client.get(f"{HA_URL}/api/error_log")
    if response.is_error:
        return _http_error(response, "get_error_log")

    lines = response.text.splitlines()
    tail = "\n".join(lines[-tail_lines:])
    return [types.TextContent(type="text", text=tail)]


async def _get_camera_snapshot_url(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    camera_entity_id = args["camera_entity_id"]

    state_response = await client.get(f"{HA_URL}/api/states/{camera_entity_id}")
    if state_response.is_error:
        return _http_error(state_response, "get_camera_snapshot_url")
    state = state_response.json()

    access_token = state.get("attributes", {}).get("access_token")
    if access_token:
        url = f"{HA_URL}/api/camera_proxy/{camera_entity_id}?token={access_token}"
    else:
        url = f"{HA_URL}/api/camera_proxy/{camera_entity_id}"

    return [types.TextContent(
        type="text",
        text=f"Camera snapshot URL (open in browser): {url}\nCurrent state: {state.get("state")}",
    )]


async def _check_config(  # pylint: disable=unused-argument
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    response = await client.post(f"{HA_URL}/api/config/core/check_config")
    if response.is_error:
        return _http_error(response, "check_config")
    return [types.TextContent(type="text", text=json.dumps(response.json(), indent=2))]


async def _create_automation(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    config = args["config"]
    automation_id = args.get("automation_id")

    url = (
        f"{HA_URL}/api/config/automation/config/{automation_id}"
        if automation_id
        else f"{HA_URL}/api/config/automation/config"
    )
    response = await client.post(url, json=config)
    if response.is_error:
        return _http_error(response, "create_automation")
    create_result = response.json()

    reload_response = await client.post(f"{HA_URL}/api/services/automation/reload")
    result_text = json.dumps(create_result, indent=2)
    if reload_response.is_error:
        result_text += (
            f"\n\nWarning: automation reload failed (HTTP {reload_response.status_code}). "
            "The automation was saved but may not be active yet."
        )
    return [types.TextContent(type="text", text=result_text)]


async def _delete_automation(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    automation_id = args["automation_id"]

    response = await client.delete(f"{HA_URL}/api/config/automation/config/{automation_id}")
    if response.is_error:
        return _http_error(response, "delete_automation")

    reload_response = await client.post(f"{HA_URL}/api/services/automation/reload")
    result_text = f"Automation '{automation_id}' deleted successfully."
    if reload_response.is_error:
        result_text += (
            f"\n\nWarning: automation reload failed (HTTP {reload_response.status_code})."
        )
    return [types.TextContent(type="text", text=result_text)]


async def _create_script(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    script_id = args["script_id"]
    config = args["config"]

    response = await client.post(f"{HA_URL}/api/config/script/config/{script_id}", json=config)
    if response.is_error:
        return _http_error(response, "create_script")
    create_result = response.json()

    reload_response = await client.post(f"{HA_URL}/api/services/script/reload")
    result_text = json.dumps(create_result, indent=2)
    if reload_response.is_error:
        result_text += (
            f"\n\nWarning: script reload failed (HTTP {reload_response.status_code}). "
            "The script was saved but may not be active yet."
        )
    return [types.TextContent(type="text", text=result_text)]


async def _delete_script(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    script_id = args["script_id"]

    response = await client.delete(f"{HA_URL}/api/config/script/config/{script_id}")
    if response.is_error:
        return _http_error(response, "delete_script")

    reload_response = await client.post(f"{HA_URL}/api/services/script/reload")
    result_text = f"Script '{script_id}' deleted successfully."
    if reload_response.is_error:
        result_text += (
            f"\n\nWarning: script reload failed (HTTP {reload_response.status_code})."
        )
    return [types.TextContent(type="text", text=result_text)]


async def _create_helper(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    helper_type = args["helper_type"]
    helper_id = args["helper_id"]
    config = args["config"]

    valid_types = {"input_boolean", "input_number", "input_text", "input_select", "input_datetime", "input_button"}
    if helper_type not in valid_types:
        return [types.TextContent(
            type="text",
            text=f"Invalid helper_type '{helper_type}'. Must be one of: {", ".join(sorted(valid_types))}",
        )]

    response = await client.post(f"{HA_URL}/api/config/{helper_type}/config/{helper_id}", json=config)
    if response.is_error:
        return _http_error(response, "create_helper")
    create_result = response.json()

    reload_response = await client.post(f"{HA_URL}/api/services/{helper_type}/reload")
    result_text = json.dumps(create_result, indent=2)
    if reload_response.is_error:
        result_text += (
            f"\n\nWarning: {helper_type} reload failed (HTTP {reload_response.status_code}). "
            "The helper was saved but may not be active yet."
        )
    return [types.TextContent(type="text", text=result_text)]


async def _call_service(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    domain = args["domain"]
    service = args["service"]
    payload = {k: v for k, v in args.items() if k not in ("domain", "service")}

    response = await client.post(f"{HA_URL}/api/services/{domain}/{service}", json=payload)
    if response.is_error:
        return _http_error(response, "call_service")

    try:
        result = json.dumps(response.json(), indent=2)
    except Exception:  # pylint: disable=broad-exception-caught
        result = response.text.strip() or "Service called successfully."
    return [types.TextContent(type="text", text=result)]


async def _ha_ws_command(command: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Make a single authenticated command call to the HA WebSocket API."""
    ws_url = HA_URL.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/api/websocket"

    async with websockets.connect(ws_url) as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {msg}")

        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"Authentication failed: {msg.get("message", msg)}")

        await ws.send(json.dumps({"id": 1, **command}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == 1:
                return msg


async def _list_dashboards(  # pylint: disable=unused-argument
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    try:
        result = await _ha_ws_command({"type": "lovelace/dashboards/list"})
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return [types.TextContent(type="text", text=f"list_dashboards error: {exc}")]
    if not result.get("success"):
        return [types.TextContent(type="text", text=f"list_dashboards failed: {result.get("error", result)}")]
    return [types.TextContent(type="text", text=json.dumps(result.get("result", []), indent=2))]


async def _get_dashboard_config(  # pylint: disable=unused-argument
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    url_path = args.get("url_path")
    cmd: dict[str, Any] = {"type": "lovelace/config"}
    if url_path:
        cmd["url_path"] = url_path
    try:
        result = await _ha_ws_command(cmd)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return [types.TextContent(type="text", text=f"get_dashboard_config error: {exc}")]
    if not result.get("success"):
        return [types.TextContent(type="text", text=f"get_dashboard_config failed: {result.get("error", result)}")]
    return [types.TextContent(type="text", text=json.dumps(result.get("result", {}), indent=2))]


async def _update_dashboard_config(  # pylint: disable=unused-argument
    client: httpx.AsyncClient, args: dict[str, Any]
) -> list[types.TextContent]:
    config = args["config"]
    url_path = args.get("url_path")
    cmd: dict[str, Any] = {"type": "lovelace/config/save", "config": config}
    if url_path:
        cmd["url_path"] = url_path
    try:
        result = await _ha_ws_command(cmd)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return [types.TextContent(type="text", text=f"update_dashboard_config error: {exc}")]
    if not result.get("success"):
        return [types.TextContent(type="text", text=f"update_dashboard_config failed: {result.get("error", result)}")]
    dashboard_label = url_path or "default"
    return [types.TextContent(
        type="text",
        text=f"Dashboard '{dashboard_label}' updated successfully.",
    )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
