"""Minimal MCP client for ArchHarness (in-container).

Talks to FastMCP's HTTP Streamable transport at /mcp. Implements only
what ArchHarness needs: initialize, tools/list (optional), tools/call.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import httpx


class MCPClient:
    def __init__(self, url: str, timeout: float = 1800.0):
        # FastMCP exposes Streamable HTTP at this base.
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0
        self._client = httpx.Client(timeout=timeout)
        self._initialize()

    def _jid(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, body: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = self._client.post(self.url, content=json.dumps(body), headers=headers)
        # FastMCP sets session id on first response
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid and not self.session_id:
            self.session_id = sid
        resp.raise_for_status()
        # Notifications: HTTP 202 Accepted with empty body — no response to parse.
        if resp.status_code == 202 or not resp.content:
            return {}
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # Parse SSE: pick the last `data: {...}` payload
            data_lines = [
                ln[len("data: "):] for ln in resp.text.splitlines()
                if ln.startswith("data: ")
            ]
            if not data_lines:
                return {}
            return json.loads(data_lines[-1])
        return resp.json()

    def _initialize(self) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": self._jid(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "archharness", "version": "v6"},
            },
        }
        self._post(body)
        # notifications/initialized (no response expected, but send for
        # protocol compliance)
        try:
            self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
        except httpx.HTTPStatusError:
            pass

    def list_tools(self) -> list[dict]:
        body = {"jsonrpc": "2.0", "id": self._jid(), "method": "tools/list"}
        resp = self._post(body)
        return (resp.get("result") or {}).get("tools", [])

    def call(self, name: str, arguments: Dict[str, Any]) -> dict:
        body = {
            "jsonrpc": "2.0",
            "id": self._jid(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self._post(body)
        result = resp.get("result") or {}
        # Shape: {"content": [{"type":"text","text":"..."}], "structuredContent": {...}, "isError": bool}
        texts = []
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text", ""))
        return {
            "text": "".join(texts),
            "structured": result.get("structuredContent") or {},
            "is_error": result.get("isError", False),
            "raw": result,
        }

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
