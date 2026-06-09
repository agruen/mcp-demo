# CPS AI Guidebook — MCP Server

This project takes the **Chicago Public Schools AI Guidebook** (a 52-page PDF, Version 5.0) and makes it available as a **Model Context Protocol (MCP)** server. MCP is an open standard that lets AI assistants (Claude, ChatGPT, Gemini, etc.) access structured data through tool calls in real time.

The core idea: instead of a parent, teacher, or administrator reading a dense policy PDF, they can ask an AI assistant a plain-language question like *"Can my 10-year-old use ChatGPT at school?"* and get a precise, sourced answer pulled directly from official CPS policy.

**Live at:** `https://cps-ai.workingpaper.co/mcp/`

## Why This Matters

- **Making policy accessible** — Turns a static PDF that most people won't read into something interactive that meets people where they are (in their AI assistant)
- **MCP as a distribution channel** — The same structured data serves Claude, ChatGPT, and Gemini through one endpoint
- **Attribution by design** — Every response cites the source. The AI can't hallucinate policy because it's reading structured data, not generating from memory
- **Low infrastructure** — Runs on a Raspberry Pi. The entire data layer is a single JSON file. No database, no vector store, no embeddings
- **Real audience** — CPS is the 3rd largest school district in the US (~320,000 students). This is real policy that real parents need to understand

## Architecture

```
PDF (source of truth)
  ↓  manually extracted into
JSON data file (cps-ai-guidebook.json)
  ↓  loaded by
Python MCP server (FastAPI + FastMCP)
  ↓  exposes tools via
MCP protocol (streamable HTTP)
  ↓  consumed by
AI assistants (Claude, ChatGPT, Gemini, etc.)
```

## What's in the Guidebook Data

The JSON captures the full structure of the 52-page PDF:

- **Section I: Introduction** — Purpose, Scope, Vision, 5 AI Principles, AI Literacy, AI Basics
- **Section II: GenAI Guidance** — Stakeholder-specific guidance for students, parents & guardians, educators & staff, administrators, ITS, and vendors
- **Section III: Approved Tools** — References the CPS Ed Tech Catalog
- **Section IV: Professional Development** — 3-tier badge pathway
- **Section V: Conclusion**
- **Section VI: Appendix** — Steering Committee, 33-term glossary, version history

## MCP Tools

The server exposes ~15 tools that AI assistants can call:

| Tool | Description |
|---|---|
| `guidebook.get_parent_guidance` | All parent-specific guidance in one call |
| `guidebook.get_stakeholder_guidance(stakeholder)` | Guidance for any role: parents, students, educators, administrators, ITS, vendors |
| `guidebook.get_age_restrictions` | Which AI tools kids can use at what age |
| `guidebook.get_classroom_examples(grade_level, subject)` | Side-by-side "without AI" vs "with AI" examples |
| `guidebook.get_ai_principles` | CPS's 5 AI principles with commitments |
| `guidebook.get_positive_uses` | Approved ways students can use GenAI |
| `guidebook.get_glossary_term(term)` | Look up any of 33 AI terms in plain language |
| `guidebook.search(query)` | Full-text search across the entire guidebook |
| `guidebook.list_sections` / `list_topics` / `get_topic` | Hierarchical browsing |

Every tool response includes attribution metadata citing the source document.

## Setup

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (for containerized deployment)

### Environment Variables

Copy the example and configure:

```bash
cd guidebook
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `PUBLIC_HOST` | Domain for reverse proxy (leave blank for local dev) | — |
| `MCP_API_KEY` | Optional Bearer token for OAuth auth | — |
| `MCP_PORT` | Server port | `8080` |
| `DASHBOARD_PORT` | Analytics dashboard port | `8050` |

### Run Locally

```bash
pip install -r guidebook/requirements.txt

cd guidebook
export CPS_GUIDEBOOK_JSON_PATH=./data/cps-ai-guidebook.json
gunicorn server:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080
```

The server will be available at `http://localhost:8080`. The root URL serves a documentation page with live stats, connection instructions, and example questions.

### Run with Docker

```bash
cd guidebook
cp .env.example .env    # edit as needed
docker compose up --build -d
```

This starts the MCP server on port 8080 with an analytics dashboard at `/reporting/`.

### Production Deployment

The included `Caddyfile` configures a reverse proxy with automatic TLS via Let's Encrypt. The project runs on a Raspberry Pi behind Caddy.

## Connecting an AI Assistant

### Claude Desktop

Add to your Claude Desktop MCP settings:

```json
{
  "mcpServers": {
    "cps-ai-guidebook": {
      "url": "https://cps-ai.workingpaper.co/mcp/"
    }
  }
}
```

### ChatGPT / Gemini

These assistants can connect via the standard MCP connection flow using the same endpoint URL. The server supports OAuth 2.1 with PKCE for authentication.

## Project Structure

```
guidebook/              ← The CPS AI Guidebook MCP server
  data/
    cps-ai-guidebook.json   ← Structured guidebook data
  tools.py                  ← MCP tool definitions (~770 lines)
  mcp_server.py             ← FastMCP configuration
  server.py                 ← FastAPI app, OAuth, docs page
  activity_logger.py        ← Usage logging
  costing.py                ← Token cost estimation
  Dockerfile
  docker-compose.yml
  Caddyfile

example/                ← Reference implementation (Carnegie Skills Framework)
  A separate MCP server showing the same pattern
  applied to a different dataset

cps-ai-guidebook.pdf    ← Original 52-page source PDF
cps-ai-guidebook.txt    ← Text extraction of the PDF
BRIEF.md                ← Detailed project brief
```

## License

The CPS AI Guidebook content is published by Chicago Public Schools. This server implementation makes that content accessible via MCP.
