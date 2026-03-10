"""
Custom MCP stdio server providing filtered HomeAssistant state queries and device control.

Uses HA REST API directly instead of the built-in MCP server's GetLiveContext,
which always dumps all entities (~19KB+) regardless of what is needed.

Tools:
  - list_entities: Filter entities by domain and/or state via POST /api/template
  - get_entity_state: Get a single entity's full state via GET /api/states/{entity_id}
  - call_service: Call any HA service via POST /api/services/{domain}/{service}
"""

import asyncio
import json
import os
import sys

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
                "List HomeAssistant entities filtered by domain and/or state. "
                "Returns entity IDs, friendly names, and current states. "
                "Use this instead of GetLiveContext when you need entities "
                "from a specific domain (e.g. light, switch, sensor) or with "
                "a specific state (e.g. on, off)."
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
        elif name == "call_service":
            return await _call_service(client, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")


async def _list_entities(
    client: httpx.AsyncClient, args: dict
) -> list[types.TextContent]:
    domain = args.get("domain")
    state_filter = args.get("state")

    if domain and state_filter:
        template = (
            f"{{% set ns = namespace(items=[]) %}}"
            f"{{% for s in states.{domain} %}}"
            f"{{% if s.state == '{state_filter}' %}}"
            f"{{% set ns.items = ns.items + [dict(entity_id=s.entity_id, name=s.name, state=s.state, attributes=s.attributes)] %}}"
            f"{{% endif %}}"
            f"{{% endfor %}}"
            f"{{{{ ns.items | tojson }}}}"
        )
    elif domain:
        template = (
            f"{{{{ states.{domain} | map(attribute='as_dict') | list | tojson }}}}"
        )
    elif state_filter:
        template = (
            f"{{% set ns = namespace(items=[]) %}}"
            f"{{% for s in states %}}"
            f"{{% if s.state == '{state_filter}' %}}"
            f"{{% set ns.items = ns.items + [dict(entity_id=s.entity_id, name=s.name, state=s.state)] %}}"
            f"{{% endif %}}"
            f"{{% endfor %}}"
            f"{{{{ ns.items | tojson }}}}"
        )
    else:
        template = (
            "{% set ns = namespace(items=[]) %}"
            "{% for s in states %}"
            "{% set ns.items = ns.items + [dict(entity_id=s.entity_id, name=s.name, state=s.state)] %}"
            "{% endfor %}"
            "{{ ns.items | tojson }}"
        )

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
