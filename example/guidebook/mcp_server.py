from __future__ import annotations

import os
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()

allowed_hosts = ["localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"]
allowed_origins = ["http://localhost", "http://127.0.0.1"]

if PUBLIC_HOST:
    allowed_hosts += [
        PUBLIC_HOST,
        f"{PUBLIC_HOST}:443",
        f"{PUBLIC_HOST}:80",
    ]
    allowed_origins += [
        f"https://{PUBLIC_HOST}",
        f"http://{PUBLIC_HOST}",
    ]

mcp = FastMCP(
    name="CPS AI Guidebook",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ),
)


from tools import TOOL_REGISTRY

import functools
import inspect
import time
from costing import estimate_and_log_cost
from activity_logger import log_tool_call, log_info


def _register_tool_registry_tools() -> None:
    for tool_name, tool_info in TOOL_REGISTRY.items():
        if not tool_name.startswith("guidebook."):
            continue

        fn = tool_info.get("function")
        if not callable(fn):
            continue

        description = tool_info.get("description") or f"CPS Guidebook tool: {tool_name}"
        mcp_name = tool_name.replace(".", "__")

        def make_wrapper(_fn, _mcp_name):
            @functools.wraps(_fn)
            def wrapper(**kwargs) -> Any:
                start_time = time.time()
                response_ok = True
                try:
                    response = _fn(**kwargs)
                except Exception as exc:
                    response_ok = False
                    latency_ms = (time.time() - start_time) * 1000
                    try:
                        log_tool_call(
                            tool_name=_mcp_name,
                            arguments=kwargs,
                            response_ok=False,
                            latency_ms=latency_ms,
                        )
                    except Exception:
                        pass
                    raise

                latency_ms = (time.time() - start_time) * 1000

                response = estimate_and_log_cost(
                    tool_name=_mcp_name,
                    request_data=kwargs,
                    response_data=response,
                    latency_ms=latency_ms,
                )

                try:
                    cost_est = (response.get("meta") or {}).get("cost_estimate") or {}
                    log_tool_call(
                        tool_name=_mcp_name,
                        arguments=kwargs,
                        response_ok=bool(response.get("ok")),
                        latency_ms=latency_ms,
                        input_tokens_est=cost_est.get("input_tokens_est", 0),
                        output_tokens_est=cost_est.get("output_tokens_est", 0),
                    )
                except Exception:
                    pass

                return response

            wrapper.__name__ = _mcp_name
            wrapper.__signature__ = inspect.signature(_fn)
            wrapper.__annotations__ = getattr(_fn, "__annotations__", {})

            return wrapper

        wrapper = make_wrapper(fn, mcp_name)
        mcp.tool(description=description)(wrapper)


_register_tool_registry_tools()


@mcp.tool(description="Hello World tool — verify connectivity to CPS AI Guidebook MCP", annotations={"read_only": True})
def hello(name: str = "world") -> str:
    return f"Hello, {name}! Welcome to the CPS AI Guidebook."

@mcp.tool(description="Search the CPS AI Guidebook", annotations={"read_only": True})
def search(query: str, limit: int = 10) -> Dict[str, Any]:
    from tools import guidebook_search
    return guidebook_search(query=query, limit=limit)

@mcp.tool(description="Fetch a guidebook item by ID", annotations={"read_only": True})
def fetch(id: str) -> Dict[str, Any]:
    from tools import guidebook_get_topic
    return guidebook_get_topic(topic_id=id)
