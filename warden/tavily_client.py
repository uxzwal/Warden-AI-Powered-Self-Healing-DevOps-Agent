"""Tavily Search API Client for Warden.

Provides advisory technical research capabilities when diagnostic confidence is low
or when exploring external incident mitigation documentation.

SECURITY NOTICE: All external search findings are strictly treated as UNTRUSTED context.
External research is advisory only and NEVER directly executes remediation actions.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import httpx

from warden.audit import log_external_research

load_dotenv(override=False)
for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env"]:
    if p.exists():
        load_dotenv(dotenv_path=str(p), override=False)


_UNSET = object()


class TavilyClient:
    """Client for Tavily Search API with defensive handling and fallbacks."""

    def __init__(self, api_key: Any = _UNSET):
        if api_key is not _UNSET:
            self.api_key = api_key
        else:
            self.api_key = os.getenv("TAVILY_API_KEY")
        self.endpoint = "https://api.tavily.com/search"

    def is_available(self) -> bool:
        """Check if a valid, non-placeholder Tavily API key is configured."""
        if not self.api_key:
            return False
        key = self.api_key.strip()
        return bool(key) and "your_tavily_api_key" not in key.lower()

    async def search(
        self,
        query: str,
        max_results: int = 3,
        search_depth: str = "basic"
    ) -> Dict[str, Any]:
        """Search technical documentation and return structured, sanitized citations.
        
        Args:
            query: DevOps search query (e.g., 'HTTP 503 connection pool exhaustion remediation')
            max_results: Number of citations to return (default 3)
            search_depth: 'basic' or 'advanced'
            
        Returns:
            Dict containing query, results list, and summary answer.
        """
        if not self.is_available():
            return {
                "available": False,
                "query": query,
                "results": [],
                "answer": None,
                "message": "Tavily API key not configured or placeholder detected."
            }

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": True
        }

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(self.endpoint, json=payload)
                if resp.status_code != 200:
                    return {
                        "available": True,
                        "query": query,
                        "results": [],
                        "answer": None,
                        "error": f"Tavily returned HTTP {resp.status_code}: {resp.text[:200]}"
                    }
                
                data = resp.json()
                raw_results = data.get("results", [])
                sanitized_results: List[Dict[str, str]] = []

                for item in raw_results[:max_results]:
                    # Sanitize content snippets
                    snippet = item.get("content", "").strip()[:400]
                    sanitized_results.append({
                        "title": item.get("title", "Untitled Source"),
                        "url": item.get("url", ""),
                        "snippet": snippet
                    })

                # Audit log
                log_external_research(query, len(sanitized_results), sanitized_results)

                return {
                    "available": True,
                    "query": query,
                    "results": sanitized_results,
                    "answer": data.get("answer"),
                    "message": f"Successfully retrieved {len(sanitized_results)} external reference(s)."
                }
        except Exception as exc:
            return {
                "available": True,
                "query": query,
                "results": [],
                "answer": None,
                "error": f"Tavily search network error: {str(exc)}"
            }
