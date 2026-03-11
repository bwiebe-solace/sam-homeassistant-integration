"""
MCP stdio server providing Pi-hole management tools.

Supports both Pi-hole v5 (legacy api.php) and v6 (REST API). The API version
is auto-detected at startup by probing /api/info/version; if that fails, v5 is
assumed.

Tools:
  - get_pihole_status:  Blocking status and basic query/block statistics
  - enable_pihole:      Enable Pi-hole DNS blocking
  - disable_pihole:     Disable Pi-hole DNS blocking (optionally for N seconds)
  - get_pihole_stats:   Detailed statistics (top domains, clients, query types)
  - list_domains:       List whitelist or blacklist entries
  - add_domain:         Add a domain to the whitelist or blacklist
  - remove_domain:      Remove a domain from the whitelist or blacklist
"""

import asyncio
import json
import os
from typing import Any

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

PIHOLE_HOST = os.environ.get("PIHOLE_HOST", "").rstrip("/")
PIHOLE_API_KEY = os.environ.get("PIHOLE_API_KEY", "")

server = Server("pihole-tools")

_api_version: int | None = None
_v6_sid: str | None = None


async def _detect_api_version() -> int:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{PIHOLE_HOST}/api/info/version", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return 6
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return 5


async def _ensure_api_version() -> int:
    global _api_version
    if _api_version is None:
        _api_version = await _detect_api_version()
    return _api_version


async def _v6_authenticate(session: aiohttp.ClientSession) -> str:
    global _v6_sid
    if _v6_sid:
        return _v6_sid
    async with session.post(
        f"{PIHOLE_HOST}/api/auth",
        json={"password": PIHOLE_API_KEY},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        _v6_sid = data["session"]["sid"]
        return _v6_sid


def _v6_headers(sid: str) -> dict[str, str]:
    return {"X-FTL-SID": sid}


async def _v5_get(session: aiohttp.ClientSession, params: dict[str, Any]) -> Any:
    params["auth"] = PIHOLE_API_KEY
    async with session.get(
        f"{PIHOLE_HOST}/admin/api.php",
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_pihole_status",
            description=(
                "Get the current Pi-hole blocking status and basic statistics: "
                "whether blocking is enabled, total queries today, total blocked, "
                "and the percentage of queries blocked."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="enable_pihole",
            description="Enable Pi-hole DNS blocking.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="disable_pihole",
            description=(
                "Disable Pi-hole DNS blocking. If duration_seconds is 0 or omitted, "
                "blocking is disabled indefinitely. Otherwise it is disabled for the "
                "specified number of seconds before automatically re-enabling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": (
                            "How long to disable blocking, in seconds. "
                            "0 or omitted means indefinite."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_pihole_stats",
            description=(
                "Get detailed Pi-hole statistics including top queried domains, "
                "top blocked domains, top clients, and query type breakdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="list_domains",
            description="List all domains on the Pi-hole whitelist or blacklist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "list_type": {
                        "type": "string",
                        "enum": ["whitelist", "blacklist"],
                        "description": "Which list to retrieve.",
                    },
                },
                "required": ["list_type"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="add_domain",
            description="Add a domain to the Pi-hole whitelist or blacklist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The domain to add (e.g. 'example.com').",
                    },
                    "list_type": {
                        "type": "string",
                        "enum": ["whitelist", "blacklist"],
                        "description": "Which list to add the domain to.",
                    },
                },
                "required": ["domain", "list_type"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="remove_domain",
            description="Remove a domain from the Pi-hole whitelist or blacklist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The domain to remove.",
                    },
                    "list_type": {
                        "type": "string",
                        "enum": ["whitelist", "blacklist"],
                        "description": "Which list to remove the domain from.",
                    },
                },
                "required": ["domain", "list_type"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        version = await _ensure_api_version()
        if name == "get_pihole_status":
            result = await _get_pihole_status(version)
        elif name == "enable_pihole":
            result = await _enable_pihole(version)
        elif name == "disable_pihole":
            result = await _disable_pihole(version, arguments)
        elif name == "get_pihole_stats":
            result = await _get_pihole_stats(version)
        elif name == "list_domains":
            result = await _list_domains(version, arguments)
        elif name == "add_domain":
            result = await _add_domain(version, arguments)
        elif name == "remove_domain":
            result = await _remove_domain(version, arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return [types.TextContent(type="text", text=json.dumps({"error": str(exc)}))]


# ── v5 implementations ────────────────────────────────────────────────────────

async def _v5_get_status() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"summary": ""})
    return {
        "api_version": 5,
        "blocking_enabled": data.get("status") == "enabled",
        "status": data.get("status"),
        "dns_queries_today": data.get("dns_queries_today"),
        "ads_blocked_today": data.get("ads_blocked_today"),
        "ads_percentage_today": data.get("ads_percentage_today"),
        "unique_domains": data.get("unique_domains"),
        "queries_forwarded": data.get("queries_forwarded"),
        "queries_cached": data.get("queries_cached"),
    }


async def _v5_enable() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"enable": ""})
    return {"api_version": 5, "status": data.get("status")}


async def _v5_disable(duration: int) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        params: dict[str, Any] = {}
        if duration > 0:
            params["disable"] = duration
        else:
            params["disable"] = ""
        data = await _v5_get(session, params)
    return {"api_version": 5, "status": data.get("status")}


async def _v5_get_stats() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"topItems": "", "getQueryTypes": "", "getClientNames": ""})
    return {"api_version": 5, **data}


async def _v5_list_domains(list_type: str) -> dict[str, Any]:
    v5_list = "white" if list_type == "whitelist" else "black"
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"list": v5_list})
    return {"api_version": 5, "list_type": list_type, "domains": data}


async def _v5_add_domain(domain: str, list_type: str) -> dict[str, Any]:
    v5_list = "white" if list_type == "whitelist" else "black"
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"list": v5_list, "add": domain})
    return {"api_version": 5, "domain": domain, "list_type": list_type, "result": data}


async def _v5_remove_domain(domain: str, list_type: str) -> dict[str, Any]:
    v5_list = "white" if list_type == "whitelist" else "black"
    async with aiohttp.ClientSession() as session:
        data = await _v5_get(session, {"list": v5_list, "sub": domain})
    return {"api_version": 5, "domain": domain, "list_type": list_type, "result": data}


# ── v6 implementations ────────────────────────────────────────────────────────

async def _v6_get_status() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        headers = _v6_headers(sid)
        async with session.get(
            f"{PIHOLE_HOST}/api/dns/blocking",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            blocking_data = await resp.json()
        async with session.get(
            f"{PIHOLE_HOST}/api/stats/summary",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            summary = await resp.json()
    return {
        "api_version": 6,
        "blocking_enabled": blocking_data.get("blocking"),
        "status": "enabled" if blocking_data.get("blocking") else "disabled",
        "dns_queries_today": summary.get("queries", {}).get("total"),
        "ads_blocked_today": summary.get("queries", {}).get("blocked"),
        "ads_percentage_today": summary.get("queries", {}).get("percent_blocked"),
        "unique_domains": summary.get("queries", {}).get("unique_domains"),
        "queries_forwarded": summary.get("queries", {}).get("forwarded"),
        "queries_cached": summary.get("queries", {}).get("cached"),
    }


async def _v6_enable() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        async with session.post(
            f"{PIHOLE_HOST}/api/dns/blocking",
            headers=_v6_headers(sid),
            json={"blocking": True},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return {"api_version": 6, "blocking": data.get("blocking"), "status": "enabled"}


async def _v6_disable(duration: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"blocking": False}
    if duration > 0:
        payload["timer"] = duration
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        async with session.post(
            f"{PIHOLE_HOST}/api/dns/blocking",
            headers=_v6_headers(sid),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return {"api_version": 6, "blocking": data.get("blocking"), "status": "disabled", "timer": duration if duration > 0 else None}


async def _v6_get_stats() -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        headers = _v6_headers(sid)
        async with session.get(
            f"{PIHOLE_HOST}/api/stats/summary",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            summary = await resp.json()
        async with session.get(
            f"{PIHOLE_HOST}/api/stats/top_domains",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            top_domains_data = await resp.json() if resp.status == 200 else {}
        async with session.get(
            f"{PIHOLE_HOST}/api/stats/top_clients",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            top_clients_data = await resp.json() if resp.status == 200 else {}
    return {
        "api_version": 6,
        "summary": summary,
        "top_domains": top_domains_data,
        "top_clients": top_clients_data,
    }


async def _v6_list_domains(list_type: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        async with session.get(
            f"{PIHOLE_HOST}/api/domains",
            headers=_v6_headers(sid),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    v6_type = "allow" if list_type == "whitelist" else "deny"
    domains = [
        entry.get("domain")
        for entry in data.get("domains", [])
        if entry.get("type") == v6_type
    ]
    return {"api_version": 6, "list_type": list_type, "domains": domains}


async def _v6_add_domain(domain: str, list_type: str) -> dict[str, Any]:
    v6_type = "allow" if list_type == "whitelist" else "deny"
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        async with session.post(
            f"{PIHOLE_HOST}/api/domains/{v6_type}/exact",
            headers=_v6_headers(sid),
            json={"domain": domain, "comment": "", "enabled": True},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return {"api_version": 6, "domain": domain, "list_type": list_type, "result": data}


async def _v6_remove_domain(domain: str, list_type: str) -> dict[str, Any]:
    v6_type = "allow" if list_type == "whitelist" else "deny"
    async with aiohttp.ClientSession() as session:
        sid = await _v6_authenticate(session)
        async with session.delete(
            f"{PIHOLE_HOST}/api/domains/{v6_type}/exact/{domain}",
            headers=_v6_headers(sid),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json() if resp.content_length else {}
    return {"api_version": 6, "domain": domain, "list_type": list_type, "result": data}


# ── Dispatch helpers ──────────────────────────────────────────────────────────

async def _get_pihole_status(version: int) -> dict[str, Any]:
    if version == 6:
        return await _v6_get_status()
    return await _v5_get_status()


async def _enable_pihole(version: int) -> dict[str, Any]:
    if version == 6:
        return await _v6_enable()
    return await _v5_enable()


async def _disable_pihole(version: int, args: dict[str, Any]) -> dict[str, Any]:
    duration = int(args.get("duration_seconds") or 0)
    if version == 6:
        return await _v6_disable(duration)
    return await _v5_disable(duration)


async def _get_pihole_stats(version: int) -> dict[str, Any]:
    if version == 6:
        return await _v6_get_stats()
    return await _v5_get_stats()


async def _list_domains(version: int, args: dict[str, Any]) -> dict[str, Any]:
    list_type = args["list_type"]
    if version == 6:
        return await _v6_list_domains(list_type)
    return await _v5_list_domains(list_type)


async def _add_domain(version: int, args: dict[str, Any]) -> dict[str, Any]:
    domain = args["domain"]
    list_type = args["list_type"]
    if version == 6:
        return await _v6_add_domain(domain, list_type)
    return await _v5_add_domain(domain, list_type)


async def _remove_domain(version: int, args: dict[str, Any]) -> dict[str, Any]:
    domain = args["domain"]
    list_type = args["list_type"]
    if version == 6:
        return await _v6_remove_domain(domain, list_type)
    return await _v5_remove_domain(domain, list_type)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
