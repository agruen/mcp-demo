# costing.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None

DEFAULT_PRICE_TABLE: Dict[str, Dict[str, float]] = {
    "gpt-5.2": {"per_1k_input": 0.0, "per_1k_output": 0.0},
    "claude-sonnet-4.5": {"per_1k_input": 0.0, "per_1k_output": 0.0},
}

MODEL_ALIASES: Dict[str, str] = {
    "gpt-5.2": "gpt-5.2",
    "gpt-5.2-thinking": "gpt-5.2",
    "claude-sonnet-4.5": "claude-sonnet-4.5",
    "sonnet-4.5": "claude-sonnet-4.5",
    "claude-4.5-sonnet": "claude-sonnet-4.5",
}


def _load_price_table() -> Dict[str, Dict[str, float]]:
    raw = os.getenv("MCP_PRICE_TABLE_JSON", "").strip()
    if not raw:
        return DEFAULT_PRICE_TABLE.copy()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return DEFAULT_PRICE_TABLE.copy()
        out: Dict[str, Dict[str, float]] = DEFAULT_PRICE_TABLE.copy()
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, dict):
                p_in = float(v.get("per_1k_input", out.get(k, {}).get("per_1k_input", 0.0)))
                p_out = float(v.get("per_1k_output", out.get(k, {}).get("per_1k_output", 0.0)))
                out[k] = {"per_1k_input": p_in, "per_1k_output": p_out}
        return out
    except Exception:
        return DEFAULT_PRICE_TABLE.copy()


def _normalize_model(model: Optional[str]) -> str:
    if not model:
        return "unknown"
    m = model.strip()
    return MODEL_ALIASES.get(m, m)


def _safe_json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def estimate_tokens(data: Any, encoding_name: str = "cl100k_base") -> int:
    s = _safe_json_dumps(data)
    if tiktoken is not None:
        try:
            enc = tiktoken.get_encoding(encoding_name)
            n = len(enc.encode(s))
            return max(1, int(n))
        except Exception:
            pass
    return max(1, len(s) // 4)


@dataclass
class CostEstimate:
    model_assumed: str
    input_tokens_est: int
    output_tokens_est: int
    per_1k_input: float
    per_1k_output: float
    input_usd_est: float
    output_usd_est: float
    usd_estimate: float
    scope: str = "mcp_tool_io"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "model_assumed": self.model_assumed,
            "input_tokens_est": self.input_tokens_est,
            "output_tokens_est": self.output_tokens_est,
            "pricing": {
                "per_1k_input": self.per_1k_input,
                "per_1k_output": self.per_1k_output,
            },
            "input_usd_est": self.input_usd_est,
            "output_usd_est": self.output_usd_est,
            "usd_estimate": self.usd_estimate,
        }


def estimate_cost_usd(
    *,
    model: str,
    input_obj: Any,
    output_obj: Any,
    encoding_name: str = "cl100k_base",
) -> CostEstimate:
    price_table = _load_price_table()
    norm_model = _normalize_model(model)
    pricing = price_table.get(norm_model) or {"per_1k_input": 0.0, "per_1k_output": 0.0}
    p_in = float(pricing.get("per_1k_input", 0.0))
    p_out = float(pricing.get("per_1k_output", 0.0))

    input_tokens = estimate_tokens(input_obj, encoding_name=encoding_name)
    output_tokens = estimate_tokens(output_obj, encoding_name=encoding_name)

    input_usd = (input_tokens / 1000.0) * p_in
    output_usd = (output_tokens / 1000.0) * p_out
    total = input_usd + output_usd

    return CostEstimate(
        model_assumed=norm_model,
        input_tokens_est=input_tokens,
        output_tokens_est=output_tokens,
        per_1k_input=p_in,
        per_1k_output=p_out,
        input_usd_est=round(input_usd, 6),
        output_usd_est=round(output_usd, 6),
        usd_estimate=round(total, 6),
    )


def _ensure_payload_shape(resp: Any) -> Dict[str, Any]:
    if isinstance(resp, dict):
        if "ok" in resp and ("data" in resp or "error" in resp):
            resp.setdefault("meta", {})
            return resp
        resp.setdefault("meta", {})
        return resp
    return {"ok": True, "data": resp, "meta": {}}


def estimate_and_log_cost(
    *,
    tool_name: str,
    request_data: Dict[str, Any],
    response_data: Any,
    latency_ms: float,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    payload = _ensure_payload_shape(response_data)
    model_used = model or os.getenv("MCP_COST_MODEL", "gpt-5.2")

    est = estimate_cost_usd(
        model=model_used,
        input_obj={"tool": tool_name, "arguments": request_data},
        output_obj=payload,
    )

    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    meta["latency_ms"] = round(float(latency_ms), 3)
    meta["tool_name"] = tool_name
    meta["cost_estimate"] = est.to_dict()

    payload["meta"] = meta

    if isinstance(payload.get("data"), dict):
        payload["data"].setdefault(
            "cost_line",
            f"Estimated cost: ${est.usd_estimate} (in: ${est.input_usd_est}, out: ${est.output_usd_est})"
        )

    return payload
