"""Tavily web search tool for the Research Agent."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from google.adk.tools import ToolContext

from solace_agent_mesh.common.rag_dto import create_rag_source, create_rag_search_result

log = logging.getLogger(__name__)

_TAVILY_API_URL = "https://api.tavily.com/search"
_SEARCH_TURN_STATE_KEY = "web_search_turn_counter"


def _get_next_search_turn(tool_context: Optional[ToolContext]) -> int:
    if not tool_context:
        return 0
    current_turn = tool_context.state.get(_SEARCH_TURN_STATE_KEY, 0)
    tool_context.state[_SEARCH_TURN_STATE_KEY] = current_turn + 1
    return current_turn


def _extract_domain(url: str) -> str:
    domain = url.replace("https://", "").replace("http://", "").split("/")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


async def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    tool_context: ToolContext = None,
    tool_config: Optional[Dict[str, Any]] = None,
) -> dict:
    """Search the web using Tavily. Use for current information, device specs,
    HA documentation, troubleshooting, news, and any question requiring up-to-date
    knowledge. Always cite text sources using [[cite:ID]] format from the results.
    IMPORTANT: Image results will be displayed automatically — do NOT cite images."""
    config = tool_config or {}
    api_key = config.get("tavily_api_key")
    if not api_key:
        return {"error": "tavily_api_key not configured in tool_config"}

    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    max_results = min(max(max_results, 1), 10)

    search_turn = _get_next_search_turn(tool_context)
    citation_prefix = f"s{search_turn}r"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _TAVILY_API_URL,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                    "include_answer": False,
                },
                timeout=30.0,
            )
    except httpx.TimeoutException:
        log.error("[web_search] Tavily search timed out for query: %s", query)
        return {"error": "Tavily search timed out"}
    except Exception as exc:
        log.exception("[web_search] Unexpected error calling Tavily: %s", exc)
        return {"error": f"Tavily search failed: {exc}"}

    if response.status_code != 200:
        error_msg = f"Tavily API error: {response.status_code} — {response.text[:200]}"
        log.error("[web_search] %s", error_msg)
        return {"error": error_msg}

    data = response.json()
    results = data.get("results", [])

    log.info("[web_search] Tavily returned %d results for query: %r (turn=%d)", len(results), query, search_turn)

    rag_sources = []
    valid_citation_ids = []
    formatted_lines = [
        f"=== SEARCH RESULTS (Turn {search_turn}) ===",
        f"Query: {query}",
        f"Valid citation IDs: {', '.join(f'{citation_prefix}{i}' for i in range(len(results)))}",
        "",
    ]

    for i, item in enumerate(results):
        citation_id = f"{citation_prefix}{i}"
        valid_citation_ids.append(citation_id)

        url = item.get("url", "")
        title = item.get("title", "")
        snippet = item.get("content", "")
        domain = _extract_domain(url) if url else ""

        log.debug("[web_search] Citation [[cite:%s]] -> %s | %s", citation_id, url, title[:60])

        rag_sources.append(create_rag_source(
            citation_id=citation_id,
            file_id=f"tavily_{search_turn}_{i}",
            filename=domain or title,
            title=title,
            source_url=url,
            url=url,
            content_preview=snippet,
            relevance_score=item.get("score", 1.0),
            source_type="web",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            metadata={
                "title": title,
                "link": url,
                "type": "web_search",
                "favicon": f"https://www.google.com/s2/favicons?domain={url}&sz=32" if url else "",
            },
        ))

        formatted_lines += [
            f"--- RESULT {i + 1} ---",
            f"CITATION ID: [[cite:{citation_id}]]",
            f"TITLE: {title}",
            f"URL: {url}",
            f"CONTENT: {snippet}",
            f"USE [[cite:{citation_id}]] to cite facts from THIS result only",
            "",
        ]

    formatted_lines += [
        "=== END SEARCH RESULTS ===",
        "",
        "IMPORTANT: Each citation ID is UNIQUE to its result.",
        "Only use a citation ID for facts that appear in THAT specific result's CONTENT.",
    ]

    rag_metadata = create_rag_search_result(
        query=query,
        search_type="web_search",
        timestamp=datetime.now(timezone.utc).isoformat(),
        sources=rag_sources,
    )

    return {
        "formatted_results": "\n".join(formatted_lines),
        "rag_metadata": rag_metadata,
        "valid_citation_ids": valid_citation_ids,
        "num_results": len(results),
        "search_turn": search_turn,
    }
