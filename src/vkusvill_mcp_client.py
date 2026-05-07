from __future__ import annotations

import json
import re
from typing import Any

import httpx


VKUSVILL_MCP_URL = "https://mcp001.vkusvill.ru/mcp"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


class VkusvillMCPClient:
    def __init__(
        self,
        timeout: float = 30.0,
        mcp_url: str = VKUSVILL_MCP_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self.mcp_url = mcp_url
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._session_id: str | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self.timeout)
            self._owns_client = True
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client and not self._client.is_closed:
            self._client.close()
        if self._owns_client:
            self._client = None
        self._session_id = None

    @staticmethod
    def _session_header(headers: httpx.Headers) -> str | None:
        for key, value in headers.items():
            if key.lower() == "mcp-session-id" and value:
                return value
        return None

    def _init_session(self) -> None:
        client = self._get_client()
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "vkusvill-mcp-client", "version": "1.0"},
            },
        }
        response = client.post(
            self.mcp_url,
            json=init_payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"MCP initialize failed: HTTP {response.status_code}")

        self._session_id = self._session_header(response.headers)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        client.post(
            self.mcp_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=headers,
        )

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._session_id:
            self._init_session()

        client = self._get_client()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = client.post(
            self.mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
            headers=headers,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"MCP call failed: HTTP {response.status_code}")

        payload = response.json()
        if "error" in payload:
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP error: {message}")

        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return {"result": result}
        return result

    @staticmethod
    def _parse_result(result: dict[str, Any]) -> dict[str, Any]:
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
                if isinstance(parsed, dict):
                    return parsed
                return {"text": text, "data": parsed}
        return result

    @staticmethod
    def _normalize_sort(sort: str) -> str:
        value = (sort or "").strip().lower()
        if value in {"popular", "popularity"}:
            return "popularity"
        if value in {"new", "rating", "price_asc", "price_desc"}:
            return value
        raise ValueError(f"Unsupported sort: {sort}")

    def search_products(
        self,
        query: str,
        page: int = 1,
        per_page: int = 10,
        sort: str = "popularity",
    ) -> dict[str, Any]:
        result = self._call_tool(
            "vkusvill_products_search",
            {
                "q": query,
                "page": page,
                "per_page": per_page,
                "sort": self._normalize_sort(sort),
            },
        )
        return self._parse_result(result)

    def list_discount_products(
        self,
        page: int = 1,
        discount_type: str = "card",
        sort: str = "popularity",
        vvonly: int = 1,
    ) -> dict[str, Any]:
        result = self._call_tool(
            "vkusvill_products_discount",
            {
                "type": discount_type,
                "page": page,
                "sort": self._normalize_sort(sort),
                "vvonly": int(vvonly),
            },
        )
        return self._parse_result(result)

    def get_product_details(self, product_id: int | str) -> dict[str, Any]:
        if isinstance(product_id, str):
            product_id = int(product_id)
        result = self._call_tool("vkusvill_product_details", {"id": product_id})
        return self._parse_result(result)

    def get_product_by_url(self, url: str) -> dict[str, Any]:
        match = re.search(r"-(\d+)\.html", url)
        if not match:
            match = re.search(r"/goods/(?:xmlid/)?(\d+)", url)
        if not match:
            raise ValueError(f"Unable to extract product id from URL: {url}")
        return self.get_product_details(int(match.group(1)))

    def create_cart_link(self, items: list[dict[str, Any]]) -> str:
        products: list[dict[str, Any]] = []
        for item in items:
            product_id = item.get("xml_id") or item.get("id")
            if product_id in (None, ""):
                continue
            quantity = item.get("quantity", item.get("q", 1))
            products.append({"xml_id": int(product_id), "q": float(quantity)})

        result = self._call_tool("vkusvill_cart_link_create", {"products": products})
        parsed = self._parse_result(result)
        if isinstance(parsed, dict):
            data = parsed.get("data")
            if isinstance(data, dict):
                for key in ("link", "url"):
                    value = data.get(key)
                    if value:
                        return str(value)
            for key in ("link", "url"):
                value = parsed.get(key)
                if value:
                    return str(value)
            text = parsed.get("text")
            if isinstance(text, str) and "vkusvill.ru" in text:
                match = re.search(r"https?://[^\s<>\"]+", text)
                if match:
                    return match.group(0)
            if isinstance(text, str):
                return text
        return str(parsed)
