"""
Custom MCP stdio server providing filtered HomeAssistant state queries and device control.

Uses HA REST API directly instead of the built-in MCP server's GetLiveContext,
which always dumps all entities (~19KB+) regardless of what is needed.

Tools:
  - list_entities: Filter entities by domain and/or state via POST /api/template
  - get_entity_state: Get a single entity's full state via GET /api/states/{entity_id}
  - get_entity_history: Get historical states for an entity via GET /api/history/period
  - get_automation_config: Get an automation's full config (triggers, conditions, actions)
  - call_service: Call any HA service via POST /api/services/{domain}/{service}
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

server = Server("ha-rest-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15) as client:
        if name == "list_entities":
            return await _list_entities(client, arguments)
        elif name == "get_entity_state":
            return await _get_entity_state(client, arguments)
        elif name == "get_entity_history":
            return await _get_entity_history(client, arguments)
        elif name == "get_automation_config":
            return await _get_automation_config(client, arguments)
        elif name == "call_service":
            return await _call_service(client, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")


async def _list_entities(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    domain = args.get("domain")
    state_filter = args.get("state")
    name_filter = args.get("name_filter")

    if domain and not state_filter and not name_filter:
        template = "{{ states." + domain + " | map(attribute='as_dict') | list | tojson }}"
    else:
        loop_src = ("states." + domain) if domain else "states"

        conditions = []
        if state_filter:
            conditions.append("s.state == '" + state_filter + "'")
        if name_filter:
            kw = name_filter.lower().replace("'", "").replace("{", "").replace("}", "")
            conditions.append(
                "('" + kw + "' in (s.name | lower) or '" + kw + "' in (s.entity_id | lower))"
            )

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

    response = await client.post(
        f"{HA_URL}/api/template",
        json={"template": template},
    )
    response.raise_for_status()

    raw = response.text.strip()
    try:
        entities = json.loads(raw)
        result = json.dumps(entities, indent=2)
    except json.JSONDecodeError:
        result = raw

    return [types.TextContent(type="text", text=result)]


async def _get_entity_state(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    entity_id = args["entity_id"]
    response = await client.get(f"{HA_URL}/api/states/{entity_id}")
    response.raise_for_status()
    result = json.dumps(response.json(), indent=2)
    return [types.TextContent(type="text", text=result)]


async def _get_entity_history(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    entity_id = args["entity_id"]
    hours_ago = args.get("hours_ago", 24)
    significant_only = args.get("significant_changes_only", True)

    start_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    start_str = start_time.isoformat()

    params = {
        "filter_entity_id": entity_id,
        "minimal_response": "true",
    }
    if significant_only:
        params["significant_changes_only"] = "true"

    response = await client.get(
        f"{HA_URL}/api/history/period/{start_str}",
        params=params,
    )
    response.raise_for_status()

    data = response.json()
    # API returns a list of lists (one per entity); flatten to the entity's list
    history = data[0] if data else []
    result = json.dumps(history, indent=2)
    return [types.TextContent(type="text", text=result)]


async def _get_automation_config(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    entity_id = args["entity_id"]

    state_response = await client.get(f"{HA_URL}/api/states/{entity_id}")
    state_response.raise_for_status()
    state = state_response.json()

    automation_id = state.get("attributes", {}).get("id")
    if not automation_id:
        return [types.TextContent(
            type="text",
            text=f"Could not find internal config ID for {entity_id}. "
                 f"Available attributes: {json.dumps(state.get('attributes', {}), indent=2)}",
        )]

    config_response = await client.get(f"{HA_URL}/api/config/automation/config/{automation_id}")
    config_response.raise_for_status()
    result = json.dumps(config_response.json(), indent=2)
    return [types.TextContent(type="text", text=result)]


async def _call_service(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    domain = args.pop("domain")
    service = args.pop("service")
    response = await client.post(
        f"{HA_URL}/api/services/{domain}/{service}",
        json=args,
    )
    response.raise_for_status()
    body = response.text.strip()
    result = json.dumps(response.json(), indent=2) if body else "Service called successfully."
    return [types.TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
