"""
OpenAI US Privacy Policy MCP — Reporting Dashboard

Reads NDJSON log files written by the MCP server and displays
real-time insights about tool usage and what people ask the policy.

Embedded in the MCP server at /reporting/ (or runs standalone on :8050).
"""

import gzip
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html

# ---------------------
# Configuration
# ---------------------
LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/var/log/mcp"))
TOOL_LOG = LOG_DIR / "tool_calls.jsonl"
ACTIVITY_LOG = LOG_DIR / "activities.jsonl"
REFRESH_INTERVAL_MS = 30_000
MAX_LOG_LINES = int(os.getenv("MAX_LOG_LINES", "10000"))

COLORS = {
    "teal": "#0f766e",
    "green": "#16a34a",
    "orange": "#ea580c",
    "purple": "#9333ea",
    "red": "#dc2626",
    "blue": "#2563eb",
    "pink": "#db2777",
    "gray": "#6b7280",
    "light_bg": "#f8fafc",
    "border": "#e5e7eb",
}

CONCERN_COLORS = {
    "training": "#0f766e",
    "deletion": "#ea580c",
    "ads": "#9333ea",
    "sharing": "#2563eb",
    "children": "#16a34a",
    "security": "#dc2626",
    "rights": "#db2777",
    "collection": "#6b7280",
}

CHART_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=40, r=20, t=30, b=40),
    height=300,
    font=dict(family="system-ui, -apple-system, sans-serif"),
)

TALL_CHART_LAYOUT = {**CHART_LAYOUT, "height": 400}

TOOL_PREFIX = "policy__"


# ---------------------
# Data loading
# ---------------------
def read_jsonl_tail(filepath: Path, max_lines: int = MAX_LOG_LINES,
                    chunk_size: int = 65536) -> list[dict]:
    if not filepath.exists():
        return []
    try:
        file_size = filepath.stat().st_size
    except OSError:
        return []
    if file_size == 0:
        return []

    if file_size <= chunk_size:
        with open(filepath, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return _parse_lines(all_lines[-max_lines:])

    lines: list[str] = []
    with open(filepath, "rb") as f:
        remaining = file_size
        fragment = b""
        while remaining > 0 and len(lines) < max_lines:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size) + fragment
            parts = chunk.split(b"\n")
            fragment = parts[0]
            for part in reversed(parts[1:]):
                decoded = part.decode("utf-8", errors="replace").strip()
                if decoded:
                    lines.append(decoded)
                if len(lines) >= max_lines:
                    break
        if remaining == 0 and fragment:
            decoded = fragment.decode("utf-8", errors="replace").strip()
            if decoded and len(lines) < max_lines:
                lines.append(decoded)

    lines.reverse()
    return _parse_lines(lines)


def _parse_lines(lines: list[str]) -> list[dict]:
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _discover_archives(base_filepath: Path) -> list[Path]:
    stem = base_filepath.stem
    return sorted(base_filepath.parent.glob(f"{stem}.*.jsonl.gz"))


def _read_jsonl_gz(filepath: Path) -> list[dict]:
    records = []
    with gzip.open(filepath, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def read_all_history(base_filepath: Path) -> list[dict]:
    all_records: list[dict] = []
    for archive in _discover_archives(base_filepath):
        all_records.extend(_read_jsonl_gz(archive))
    if base_filepath.exists():
        with open(base_filepath, "r", encoding="utf-8") as f:
            all_records.extend(_parse_lines(f.readlines()))
    return all_records


def safe_df(records: list[dict], date_col: str = "timestamp") -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce", utc=True)
    return df


def _no_data(msg: str = "No data yet") -> go.Figure:
    return go.Figure().update_layout(
        **CHART_LAYOUT,
        annotations=[dict(text=msg, showarrow=False, font=dict(size=14, color=COLORS["gray"]))],
    )


# ---------------------
# Derived data helpers
# ---------------------
def extract_concern_from_args(args: dict) -> str:
    for key in ("concern", "category"):
        val = args.get(key, "")
        if val:
            return str(val).lower()
    return ""


def extract_search_queries(tool_df: pd.DataFrame) -> pd.DataFrame:
    if tool_df.empty or "arguments" not in tool_df.columns:
        return pd.DataFrame()
    search_tools = [f"{TOOL_PREFIX}search"]
    mask = tool_df["tool_name"].isin(search_tools)
    if not mask.any():
        return pd.DataFrame()
    searches = tool_df[mask].copy()
    searches["query"] = searches["arguments"].apply(lambda a: a.get("query", "") if isinstance(a, dict) else "")
    searches = searches[searches["query"].str.strip() != ""]
    return searches


def infer_user_goal(row) -> dict:
    """Infer user goal and detail from a tool call."""
    tool = row.get("tool_name", "")
    args = row.get("arguments", {}) if isinstance(row.get("arguments"), dict) else {}
    short_tool = tool.replace(TOOL_PREFIX, "")

    goal_map = {
        "get_privacy_essentials": "Get the plain-language privacy overview",
        "get_training_optout": "Find out about model training and how to opt out",
        "get_data_controls": "Find privacy settings and controls",
        "get_retention_rules": "Understand how long data is kept / what deletion does",
        "get_your_rights": "Understand legal privacy rights",
        "get_children_policy": "Check rules for children and teens",
        "get_concern_guidance": "Answer a specific privacy concern",
        "get_data_collected": "See what data OpenAI collects",
        "get_glossary_term": "Look up a privacy term",
        "list_glossary": "Browse the glossary",
        "get_topic": "Read a policy topic",
        "list_sections": "Browse policy sections",
        "list_topics": "Browse topics in a section",
        "get_version_info": "Check policy version",
        "get_usage_guide": "Learn how to navigate the policy",
        "list_tools": "Discover available tools",
        "search": "Search the policy",
    }
    user_goal = goal_map.get(short_tool, short_tool)

    detail_parts = []
    if args.get("query"):
        detail_parts.append(f'"{args["query"]}"')
    if args.get("term"):
        detail_parts.append(f'"{args["term"]}"')
    if args.get("topic_id"):
        detail_parts.append(str(args["topic_id"]).replace("_", " "))
    if args.get("section_id"):
        detail_parts.append(str(args["section_id"]).replace("_", " "))
    if args.get("concern"):
        detail_parts.append(str(args["concern"]))
    if args.get("category"):
        detail_parts.append(str(args["category"]))
    detail = ", ".join(detail_parts)

    return {"user_goal": user_goal, "detail": detail}


def classify_exploration_depth(tool_df: pd.DataFrame) -> dict:
    """Classify tool calls by how deep users explore the policy.

    Browsing: list_sections, list_topics, list_tools, get_usage_guide, get_version_info
    Targeted: search, get_topic, get_concern_guidance, get_privacy_essentials, list_glossary
    Deep: get_data_controls, get_retention_rules, get_your_rights, get_children_policy,
          get_training_optout, get_glossary_term, get_data_collected
    """
    if tool_df.empty or "tool_name" not in tool_df.columns:
        return {"Browsing": 0, "Targeted lookup": 0, "Deep exploration": 0}

    shallow = {
        f"{TOOL_PREFIX}list_sections", f"{TOOL_PREFIX}list_topics",
        f"{TOOL_PREFIX}list_tools", f"{TOOL_PREFIX}get_usage_guide",
        f"{TOOL_PREFIX}get_version_info",
    }
    medium = {
        f"{TOOL_PREFIX}search", f"{TOOL_PREFIX}get_topic",
        f"{TOOL_PREFIX}get_concern_guidance", f"{TOOL_PREFIX}get_privacy_essentials",
        f"{TOOL_PREFIX}list_glossary",
    }
    deep = {
        f"{TOOL_PREFIX}get_data_controls", f"{TOOL_PREFIX}get_retention_rules",
        f"{TOOL_PREFIX}get_your_rights", f"{TOOL_PREFIX}get_children_policy",
        f"{TOOL_PREFIX}get_training_optout", f"{TOOL_PREFIX}get_glossary_term",
        f"{TOOL_PREFIX}get_data_collected",
    }

    counts = {"Browsing": 0, "Targeted lookup": 0, "Deep exploration": 0}
    counts["Deep exploration"] = len(tool_df[tool_df["tool_name"].isin(deep)])
    counts["Targeted lookup"] = len(tool_df[tool_df["tool_name"].isin(medium)])
    counts["Browsing"] = len(tool_df[tool_df["tool_name"].isin(shallow)])
    return counts


def compute_tool_journeys(tool_df: pd.DataFrame) -> list[dict]:
    """Find common tool call sequences within 5-minute windows."""
    if tool_df.empty or "timestamp" not in tool_df.columns:
        return []

    df = tool_df.sort_values("timestamp").copy()
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")

    df["window"] = df["timestamp"].dt.floor("5min")
    journeys = []
    for window, group in df.groupby("window"):
        tools = group["tool_name"].tolist()
        if len(tools) >= 2:
            deduped = [tools[0]]
            for t in tools[1:]:
                if t != deduped[-1]:
                    deduped.append(t)
            journey = " -> ".join(t.replace(TOOL_PREFIX, "") for t in deduped)
            journeys.append({"window": window, "journey": journey, "steps": len(deduped)})

    return journeys


def compute_selfreport_compliance(tool_df: pd.DataFrame, act_df: pd.DataFrame) -> float:
    """What percentage of tool-using sessions also got a log_activity call?"""
    if tool_df.empty:
        return 0.0

    data_tools = tool_df[tool_df["tool_name"] != f"{TOOL_PREFIX}log_activity"].copy()
    if data_tools.empty:
        return 0.0
    data_tools["window"] = data_tools["timestamp"].dt.floor("5min")
    data_windows = set(data_tools["window"].unique())

    log_tools = tool_df[tool_df["tool_name"] == f"{TOOL_PREFIX}log_activity"].copy()
    if log_tools.empty:
        return 0.0
    log_tools["window"] = log_tools["timestamp"].dt.floor("5min")
    log_windows = set(log_tools["window"].unique())

    if not data_windows:
        return 0.0
    return len(data_windows & log_windows) / len(data_windows) * 100


# ---------------------
# Layout helpers
# ---------------------
def _card(title: str, value: str, subtitle: str = "", color: str = COLORS["teal"]) -> html.Div:
    children = [
        html.Div(title, style={"fontSize": "13px", "color": COLORS["gray"], "marginBottom": "4px"}),
        html.Div(value, style={"fontSize": "28px", "fontWeight": "bold", "color": color}),
    ]
    if subtitle:
        children.append(html.Div(subtitle, style={"fontSize": "11px", "color": COLORS["gray"], "marginTop": "4px"}))
    return html.Div(
        style={
            "background": COLORS["light_bg"],
            "border": f"2px solid {color}",
            "borderRadius": "12px",
            "padding": "16px 20px",
            "minWidth": "140px",
            "flex": "1",
        },
        children=children,
    )


def _section(title: str, children) -> html.Div:
    return html.Div(
        style={"marginBottom": "40px"},
        children=[
            html.H2(title, style={"fontSize": "20px", "marginBottom": "8px", "borderBottom": f"2px solid {COLORS['teal']}", "paddingBottom": "6px"}),
            *children,
        ],
    )


def _table(headers: list[str], rows: list, compact: bool = False) -> html.Table:
    pad = "6px 8px" if compact else "8px 12px"
    font = "13px" if compact else "14px"
    return html.Table(
        style={"width": "100%", "borderCollapse": "collapse", "fontSize": font},
        children=[
            html.Thead(html.Tr([
                html.Th(h, style={"padding": pad, "borderBottom": f"2px solid {COLORS['teal']}", "textAlign": "left"})
                for h in headers
            ])),
            html.Tbody(rows),
        ],
    )


def _td(text: str, mono: bool = False, muted: bool = False, nowrap: bool = False) -> html.Td:
    style = {"padding": "6px 8px", "borderBottom": f"1px solid {COLORS['border']}"}
    if mono:
        style["fontFamily"] = "monospace"
    if muted:
        style["color"] = COLORS["gray"]
        style["fontSize"] = "12px"
    if nowrap:
        style["whiteSpace"] = "nowrap"
    return html.Td(str(text), style=style)


# ---------------------
# Dashboard Layout
# ---------------------
# When embedded in FastAPI via WSGIMiddleware mounted at /reporting,
# the mount strips the prefix so Flask routes see requests at /.
# But the browser needs to send requests to /reporting/ (public URL).
# routes_pathname_prefix = what Flask sees (/)
# requests_pathname_prefix = what the browser sends (/reporting/)
EMBEDDED = os.getenv("DASH_EMBEDDED", "").lower() in ("1", "true", "yes")

if EMBEDDED:
    app = Dash(
        __name__,
        routes_pathname_prefix="/",
        requests_pathname_prefix="/reporting/",
    )
else:
    URL_BASE = os.getenv("DASH_URL_BASE", "/reporting/")
    if not URL_BASE.startswith("/"):
        URL_BASE = "/" + URL_BASE
    if not URL_BASE.endswith("/"):
        URL_BASE = URL_BASE + "/"
    app = Dash(__name__, url_base_pathname=URL_BASE)
app.title = "OpenAI Privacy Policy MCP Analytics"

app.layout = html.Div(
    style={"fontFamily": "system-ui, -apple-system, sans-serif", "margin": "0 auto", "maxWidth": "1200px", "padding": "20px"},
    children=[
        html.H1("OpenAI Privacy Policy MCP Analytics", style={"borderBottom": f"3px solid {COLORS['teal']}", "paddingBottom": "10px", "marginBottom": "4px"}),
        html.P("Usage insights for the OpenAI US Privacy Policy MCP server — what people ask, what they worry about, and how AI assistants navigate the policy.", style={"color": COLORS["gray"], "marginBottom": "16px"}),

        html.Div(
            style={
                "display": "flex", "alignItems": "center", "gap": "12px",
                "marginBottom": "20px", "padding": "10px 16px",
                "background": COLORS["light_bg"], "borderRadius": "8px",
                "border": f"1px solid {COLORS['border']}",
            },
            children=[
                dcc.Checklist(
                    id="history-toggle",
                    options=[{"label": " Include all archived history", "value": "all"}],
                    value=[],
                    style={"fontSize": "14px"},
                ),
            ],
        ),

        dcc.Interval(id="refresh", interval=REFRESH_INTERVAL_MS, n_intervals=0),
        html.Div(id="report-output"),
    ],
)


# ---------------------
# Main callback
# ---------------------
@app.callback(
    Output("report-output", "children"),
    Input("refresh", "n_intervals"),
    Input("history-toggle", "value"),
    prevent_initial_call=False,
)
def update_dashboard(_n, history_toggle):
    include_all = "all" in (history_toggle or [])

    if include_all:
        tool_records = read_all_history(TOOL_LOG)
        act_records = read_all_history(ACTIVITY_LOG)
    else:
        tool_records = read_jsonl_tail(TOOL_LOG)
        act_records = read_jsonl_tail(ACTIVITY_LOG)

    tool_archives = len(_discover_archives(TOOL_LOG))
    act_archives = len(_discover_archives(ACTIVITY_LOG))
    archive_note = ""
    if tool_archives or act_archives:
        archive_note = f"  ({tool_archives} archived log file{'s' if tool_archives != 1 else ''})"

    if include_all:
        status_msg = f"Showing all history: {len(tool_records):,} tool calls, {len(act_records):,} activities{archive_note}"
    else:
        status_msg = f"Showing recent: {len(tool_records):,} tool calls, {len(act_records):,} activities{archive_note}"

    tool_df = safe_df(tool_records)
    act_df = safe_df(act_records)

    sections = []

    # =====================================================
    # REPORT 1: Overview / Summary Cards
    # =====================================================
    total_activities = len(act_df)
    data_calls = tool_df[tool_df["tool_name"] != f"{TOOL_PREFIX}log_activity"] if not tool_df.empty and "tool_name" in tool_df.columns else tool_df
    avg_latency = f"{data_calls['latency_ms'].mean():.0f}ms" if not data_calls.empty and "latency_ms" in data_calls.columns else "N/A"
    compliance = compute_selfreport_compliance(tool_df, act_df) if not tool_df.empty else 0
    unique_goals = act_df["user_goal"].nunique() if not act_df.empty and "user_goal" in act_df.columns else 0

    # Count high-intent tool usage (the questions people actually came to ask)
    high_intent_tools = {
        f"{TOOL_PREFIX}get_privacy_essentials", f"{TOOL_PREFIX}get_training_optout",
        f"{TOOL_PREFIX}get_your_rights", f"{TOOL_PREFIX}get_retention_rules",
        f"{TOOL_PREFIX}get_data_controls", f"{TOOL_PREFIX}get_children_policy",
    }
    high_intent_calls = len(tool_df[tool_df["tool_name"].isin(high_intent_tools)]) if not tool_df.empty and "tool_name" in tool_df.columns else 0

    cards = html.Div(
        style={"display": "flex", "gap": "16px", "marginBottom": "30px", "flexWrap": "wrap"},
        children=[
            _card("Tool Calls", str(len(data_calls)), "excludes log_activity"),
            _card("High-Intent Queries", str(high_intent_calls), "essentials, opt-out, rights...", COLORS["green"]),
            _card("Activities Logged", str(total_activities), f"{unique_goals} unique goals", COLORS["orange"]),
            _card("Avg Latency", avg_latency, "per tool call", COLORS["purple"]),
            _card("Self-Report Rate", f"{compliance:.0f}%", "sessions with log_activity", COLORS["blue"]),
        ],
    )
    sections.append(cards)

    # =====================================================
    # REPORT 2: What Are People Asking About?
    # =====================================================
    report2_children = []

    # 2a: Search queries
    searches = extract_search_queries(tool_df)
    if not searches.empty:
        query_counts = searches["query"].value_counts().head(15).reset_index()
        query_counts.columns = ["query", "count"]
        fig_queries = px.bar(
            query_counts, x="count", y="query", orientation="h",
            color_discrete_sequence=[COLORS["teal"]],
        )
        fig_queries.update_layout(**CHART_LAYOUT, yaxis=dict(autorange="reversed"))
        report2_children.append(html.Div(
            style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px"},
            children=[
                html.Div([
                    html.H3("Most Common Search Queries", style={"fontSize": "16px", "marginBottom": "4px"}),
                    html.P("What people literally typed when searching the policy.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                    dcc.Graph(figure=fig_queries, config={"displayModeBar": False}),
                ]),
                # Placeholder for concern interest
                html.Div(id="concern-interest-section"),
            ],
        ))
    else:
        report2_children.append(html.P("No search queries logged yet.", style={"color": COLORS["gray"]}))

    # 2b: Concern interest from all tool calls
    concern_section = html.Div()
    if not tool_df.empty and "arguments" in tool_df.columns:
        tool_df_temp = tool_df.copy()
        tool_df_temp["concern_hit"] = tool_df_temp["arguments"].apply(
            lambda a: extract_concern_from_args(a) if isinstance(a, dict) else ""
        )
        concern_hits = tool_df_temp[tool_df_temp["concern_hit"] != ""]["concern_hit"].value_counts().reset_index()
        concern_hits.columns = ["concern", "count"]
        if not concern_hits.empty:
            fig_concerns = px.bar(
                concern_hits, x="concern", y="count",
                color="concern", color_discrete_map=CONCERN_COLORS,
            )
            fig_concerns.update_layout(**CHART_LAYOUT, showlegend=False)
            concern_section = html.Div([
                html.H3("Concern Interest", style={"fontSize": "16px", "marginBottom": "4px"}),
                html.P("Which privacy concerns are queried most often (training, deletion, ads...).", style={"fontSize": "12px", "color": COLORS["gray"]}),
                dcc.Graph(figure=fig_concerns, config={"displayModeBar": False}),
            ])

    # Patch concern section into report 2
    if searches.empty:
        if hasattr(concern_section, "children") and concern_section.children:
            report2_children.append(concern_section)
    else:
        if report2_children and hasattr(report2_children[0], "children"):
            try:
                report2_children[0].children[1] = concern_section
            except (IndexError, TypeError):
                report2_children.append(concern_section)

    sections.append(_section("What Are People Asking About?", report2_children))

    # =====================================================
    # REPORT 3: What Are Assistants Doing With the Policy?
    # =====================================================
    report3_children = []

    if not act_df.empty:
        row3_charts = []

        # 3a: Interaction types
        if "artifact_type" in act_df.columns:
            at = act_df["artifact_type"].value_counts().reset_index()
            at.columns = ["type", "count"]
            at = at[at["type"].str.strip() != ""]
            if not at.empty:
                fig_artifacts = px.pie(
                    at, values="count", names="type",
                    color_discrete_sequence=px.colors.qualitative.Set2,
                    hole=0.4,
                )
                fig_artifacts.update_layout(**CHART_LAYOUT)
                fig_artifacts.update_traces(textposition="inside", textinfo="label+percent")
                row3_charts.append(html.Div([
                    html.H3("Interaction Types", style={"fontSize": "16px", "marginBottom": "4px"}),
                    html.P("What kinds of help LLMs report giving (lookups, opt-out help, rights requests...).", style={"fontSize": "12px", "color": COLORS["gray"]}),
                    dcc.Graph(figure=fig_artifacts, config={"displayModeBar": False}),
                ]))

        # 3b: User type x Concern heatmap
        if "user_type" in act_df.columns and "concern" in act_df.columns:
            cross = act_df.copy()
            cross = cross[(cross["user_type"].str.strip() != "") & (cross["concern"].str.strip() != "")]
            if len(cross) >= 3:
                pivot = cross.groupby(["user_type", "concern"]).size().reset_index(name="count")
                pivot_wide = pivot.pivot_table(index="user_type", columns="concern", values="count", fill_value=0)
                fig_heat = px.imshow(
                    pivot_wide,
                    color_continuous_scale="Teal",
                    aspect="auto",
                    labels=dict(color="uses"),
                )
                fig_heat.update_layout(**{**CHART_LAYOUT, "height": 350})
                row3_charts.append(html.Div([
                    html.H3("Who Asks About What", style={"fontSize": "16px", "marginBottom": "4px"}),
                    html.P("User type x concern. Darker = more usage.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                    dcc.Graph(figure=fig_heat, config={"displayModeBar": False}),
                ]))
            else:
                ut = act_df["user_type"].value_counts().reset_index()
                ut.columns = ["user_type", "count"]
                ut = ut[ut["user_type"].str.strip() != ""]
                cn = act_df["concern"].value_counts().reset_index()
                cn.columns = ["concern", "count"]
                cn = cn[cn["concern"].str.strip() != ""]
                sub_charts = []
                if not ut.empty:
                    fig_ut = px.bar(ut.head(10), x="count", y="user_type", orientation="h", color_discrete_sequence=[COLORS["green"]])
                    fig_ut.update_layout(**CHART_LAYOUT)
                    sub_charts.append(html.Div([html.H4("User Types"), dcc.Graph(figure=fig_ut, config={"displayModeBar": False})]))
                if not cn.empty:
                    fig_cn = px.bar(cn.head(10), x="count", y="concern", orientation="h", color_discrete_sequence=[COLORS["purple"]])
                    fig_cn.update_layout(**CHART_LAYOUT)
                    sub_charts.append(html.Div([html.H4("Concerns"), dcc.Graph(figure=fig_cn, config={"displayModeBar": False})]))
                if sub_charts:
                    row3_charts.append(html.Div(
                        style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px"},
                        children=sub_charts,
                    ))

        if row3_charts:
            cols = "1fr 1fr" if len(row3_charts) >= 2 else "1fr"
            report3_children.append(html.Div(
                style={"display": "grid", "gridTemplateColumns": cols, "gap": "20px"},
                children=row3_charts,
            ))

        # 3c: Recent activity log
        recent = act_df.sort_values("timestamp", ascending=False).head(25)
        rows = []
        for _, r in recent.iterrows():
            ts = r.get("timestamp", "")
            if hasattr(ts, "strftime"):
                ts = ts.strftime("%b %d %H:%M")
            rows.append(html.Tr([
                _td(ts, nowrap=True),
                _td(r.get("user_goal", "")),
                _td(r.get("artifact_type", ""), mono=True),
                _td(r.get("artifact_summary", ""), muted=True),
                _td(r.get("user_type", "")),
                _td(r.get("concern", "")),
            ]))
        if rows:
            report3_children.append(html.Div(
                style={"marginTop": "20px"},
                children=[
                    html.H3("Recent Activity Detail", style={"fontSize": "16px", "marginBottom": "8px"}),
                    html.Div(
                        style={"overflowX": "auto"},
                        children=[_table(["Time", "User Goal", "Type", "Summary", "User", "Concern"], rows, compact=True)],
                    ),
                ],
            ))
    else:
        report3_children.append(html.Div([
            html.P("No activities logged yet.", style={"color": COLORS["gray"]}),
            html.P(
                "Activities appear when LLMs call policy.log_activity after helping a user. "
                "This is the self-reporting tier — the LLM reports what it helped with.",
                style={"color": COLORS["gray"], "fontSize": "13px"},
            ),
        ]))

    sections.append(_section("What Are Assistants Doing With the Policy?", report3_children))

    # =====================================================
    # REPORT 4: How Are People Navigating the Policy?
    # =====================================================
    report4_children = []

    if not tool_df.empty and "tool_name" in tool_df.columns:
        r4_charts = []

        # 4a: Exploration depth
        depth = classify_exploration_depth(data_calls)
        if any(v > 0 for v in depth.values()):
            depth_df = pd.DataFrame([{"level": k, "calls": v} for k, v in depth.items() if v > 0])
            fig_depth = px.bar(
                depth_df, x="level", y="calls",
                color="level",
                color_discrete_map={"Browsing": COLORS["blue"], "Targeted lookup": COLORS["teal"], "Deep exploration": COLORS["purple"]},
            )
            fig_depth.update_layout(**CHART_LAYOUT, showlegend=False, xaxis_title="", yaxis_title="tool calls")
            r4_charts.append(html.Div([
                html.H3("Exploration Depth", style={"fontSize": "16px", "marginBottom": "4px"}),
                html.P("Browsing = listing sections/tools. Targeted = searching/fetching topics. Deep = controls, retention, rights, training.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                dcc.Graph(figure=fig_depth, config={"displayModeBar": False}),
            ]))

        # 4b: Tool usage breakdown
        tool_counts = data_calls["tool_name"].value_counts().reset_index()
        tool_counts.columns = ["tool", "count"]
        tool_counts["tool"] = tool_counts["tool"].str.replace(TOOL_PREFIX, "", regex=False)
        fig_tools = px.bar(tool_counts, x="count", y="tool", orientation="h", color_discrete_sequence=[COLORS["teal"]])
        fig_tools.update_layout(**CHART_LAYOUT, yaxis=dict(autorange="reversed"))
        r4_charts.append(html.Div([
            html.H3("Tool Usage Breakdown", style={"fontSize": "16px", "marginBottom": "4px"}),
            html.P("Which tools are called most frequently.", style={"fontSize": "12px", "color": COLORS["gray"]}),
            dcc.Graph(figure=fig_tools, config={"displayModeBar": False}),
        ]))

        if r4_charts:
            report4_children.append(html.Div(
                style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px"},
                children=r4_charts,
            ))

        # 4c: Common tool journeys
        journeys = compute_tool_journeys(data_calls)
        if journeys:
            journey_counts = Counter(j["journey"] for j in journeys).most_common(10)
            rows = []
            for journey, count in journey_counts:
                rows.append(html.Tr([
                    _td(journey, mono=True),
                    _td(str(count)),
                ]))
            report4_children.append(html.Div(
                style={"marginTop": "20px"},
                children=[
                    html.H3("Common Tool Sequences", style={"fontSize": "16px", "marginBottom": "4px"}),
                    html.P("Patterns of tools called together within 5-minute windows. Shows how assistants navigate the policy.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                    _table(["Sequence", "Times seen"], rows, compact=True),
                ],
            ))
    else:
        report4_children.append(html.P("No tool calls logged yet.", style={"color": COLORS["gray"]}))

    sections.append(_section("How Are People Navigating the Policy?", report4_children))

    # =====================================================
    # REPORT 5: Usage Over Time
    # =====================================================
    report5_children = []

    if not tool_df.empty and "timestamp" in tool_df.columns:
        r5_charts = []

        # 5a: Tool calls over time with activities overlay
        ts_range = tool_df["timestamp"].max() - tool_df["timestamp"].min()
        resample_rule = "1D" if ts_range > timedelta(days=3) else "1h"
        label = "calls/day" if resample_rule == "1D" else "calls/hour"

        tl = data_calls.set_index("timestamp").resample(resample_rule).size().reset_index(name="calls")
        fig_timeline = px.area(tl, x="timestamp", y="calls", color_discrete_sequence=[COLORS["teal"]])
        fig_timeline.update_layout(**CHART_LAYOUT, xaxis_title="", yaxis_title=label)

        # Activities overlay
        if not act_df.empty and "timestamp" in act_df.columns:
            act_tl = act_df.set_index("timestamp").resample(resample_rule).size().reset_index(name="activities")
            fig_timeline.add_trace(
                go.Scatter(x=act_tl["timestamp"], y=act_tl["activities"], mode="lines",
                           name="activities", line=dict(color=COLORS["green"], dash="dot"))
            )
            fig_timeline.update_layout(showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))

        r5_charts.append(html.Div([
            html.H3("Volume Over Time", style={"fontSize": "16px", "marginBottom": "4px"}),
            html.P("Tool calls (solid) and LLM-reported activities (dotted) over time.", style={"fontSize": "12px", "color": COLORS["gray"]}),
            dcc.Graph(figure=fig_timeline, config={"displayModeBar": False}),
        ]))

        # 5b: Day-of-week heatmap
        if len(data_calls) >= 10:
            dow = data_calls.copy()
            dow["day"] = dow["timestamp"].dt.day_name()
            dow["hour"] = dow["timestamp"].dt.hour
            heat_data = dow.groupby(["day", "hour"]).size().reset_index(name="calls")
            day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            heat_pivot = heat_data.pivot_table(index="day", columns="hour", values="calls", fill_value=0)
            heat_pivot = heat_pivot.reindex(day_order)
            heat_pivot = heat_pivot.fillna(0)

            fig_dow = px.imshow(
                heat_pivot,
                color_continuous_scale="Teal",
                aspect="auto",
                labels=dict(x="Hour (UTC)", y="Day", color="calls"),
            )
            fig_dow.update_layout(**{**CHART_LAYOUT, "height": 280})
            r5_charts.append(html.Div([
                html.H3("When People Use It", style={"fontSize": "16px", "marginBottom": "4px"}),
                html.P("Day and hour heatmap (UTC). Darker = more tool calls.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                dcc.Graph(figure=fig_dow, config={"displayModeBar": False}),
            ]))

        if r5_charts:
            cols = "1fr 1fr" if len(r5_charts) >= 2 else "1fr"
            report5_children.append(html.Div(
                style={"display": "grid", "gridTemplateColumns": cols, "gap": "20px"},
                children=r5_charts,
            ))
    else:
        report5_children.append(html.P("No data yet.", style={"color": COLORS["gray"]}))

    sections.append(_section("Usage Over Time", report5_children))

    # =====================================================
    # REPORT 6: Server Health
    # =====================================================
    report6_children = []

    if not tool_df.empty:
        r6_charts = []

        # 6a: Latency per tool
        if "latency_ms" in data_calls.columns and "tool_name" in data_calls.columns:
            latency_df = data_calls.copy()
            latency_df["tool"] = latency_df["tool_name"].str.replace(TOOL_PREFIX, "", regex=False)
            fig_lat = px.box(
                latency_df, x="tool", y="latency_ms",
                color_discrete_sequence=[COLORS["orange"]],
            )
            fig_lat.update_layout(**{**CHART_LAYOUT, "height": 320}, xaxis_title="", yaxis_title="ms")
            r6_charts.append(html.Div([
                html.H3("Latency by Tool", style={"fontSize": "16px", "marginBottom": "4px"}),
                html.P("Distribution of response times. Box = interquartile range, whiskers = full range.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                dcc.Graph(figure=fig_lat, config={"displayModeBar": False}),
            ]))

        # 6b: Error rate
        if "response_ok" in tool_df.columns:
            ok_count = tool_df["response_ok"].sum()
            err_count = len(tool_df) - ok_count
            err_pct = (err_count / len(tool_df) * 100) if len(tool_df) > 0 else 0
            fig_err = go.Figure(go.Indicator(
                mode="gauge+number",
                value=100 - err_pct,
                number={"suffix": "%"},
                title={"text": "Success Rate"},
                gauge={
                    "axis": {"range": [0, 100]},
                    "bar": {"color": COLORS["green"] if err_pct < 5 else COLORS["orange"] if err_pct < 20 else COLORS["red"]},
                    "steps": [
                        {"range": [0, 80], "color": "#fee2e2"},
                        {"range": [80, 95], "color": "#fef3c7"},
                        {"range": [95, 100], "color": "#dcfce7"},
                    ],
                },
            ))
            fig_err.update_layout(**{**CHART_LAYOUT, "height": 280})
            r6_charts.append(html.Div([
                html.H3("Reliability", style={"fontSize": "16px", "marginBottom": "4px"}),
                html.P(f"{ok_count} successful, {err_count} errors out of {len(tool_df)} total calls.", style={"fontSize": "12px", "color": COLORS["gray"]}),
                dcc.Graph(figure=fig_err, config={"displayModeBar": False}),
            ]))

        if r6_charts:
            report6_children.append(html.Div(
                style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "20px"},
                children=r6_charts,
            ))

        # 6c: Token usage summary
        if "input_tokens_est" in tool_df.columns and "output_tokens_est" in tool_df.columns:
            total_in = tool_df["input_tokens_est"].sum()
            total_out = tool_df["output_tokens_est"].sum()
            report6_children.append(html.Div(
                style={"marginTop": "16px", "display": "flex", "gap": "16px"},
                children=[
                    _card("Est. Input Tokens", f"{total_in:,.0f}", "across all tool calls", COLORS["blue"]),
                    _card("Est. Output Tokens", f"{total_out:,.0f}", "across all tool calls", COLORS["pink"]),
                    _card("Total Calls", str(len(tool_df)), f"including {len(tool_df) - len(data_calls)} log_activity", COLORS["gray"]),
                ],
            ))
    else:
        report6_children.append(html.P("No data yet.", style={"color": COLORS["gray"]}))

    sections.append(_section("Server Health", report6_children))

    # Prepend status bar
    status_bar = html.Div(
        status_msg,
        style={"fontSize": "12px", "color": COLORS["gray"], "marginBottom": "16px", "fontStyle": "italic"},
    )
    sections.insert(0, status_bar)

    return sections


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
