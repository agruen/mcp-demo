# CPS AI Guidebook MCP Server — Project Brief

## What this is

This project takes the **Chicago Public Schools AI Guidebook** (a 52-page PDF, Version 5.0) and makes it available as a **Model Context Protocol (MCP)** server. MCP is an open standard that lets AI assistants (Claude, ChatGPT, Gemini, etc.) access structured data through tool calls in real time.

The core idea: instead of a parent, teacher, or administrator reading a dense policy PDF, they can ask an AI assistant a plain-language question like *"Can my 10-year-old use ChatGPT at school?"* and get a precise, sourced answer pulled directly from official CPS policy.

## How it works

### Architecture

```
PDF (source of truth)
  ↓  manually extracted into
JSON data file (cps-ai-guidebook.json, ~900 lines)
  ↓  loaded by
Python MCP server (FastAPI + FastMCP)
  ↓  exposes tools via
MCP protocol (streamable HTTP)
  ↓  consumed by
AI assistants (Claude, ChatGPT, Gemini, etc.)
```

### Key files

| File | Purpose |
|---|---|
| `guidebook/data/cps-ai-guidebook.json` | The entire guidebook as structured JSON — sections, topics, subtopics, recommendations, glossary, classroom examples, age restrictions, committee membership, version history |
| `guidebook/tools.py` | ~770 lines. Defines all MCP tools as Python functions. Each tool loads the JSON, extracts the relevant slice, and returns it with attribution metadata |
| `guidebook/mcp_server.py` | Configures `FastMCP` (the MCP SDK), sets up transport security and allowed hosts |
| `guidebook/server.py` | FastAPI app. Serves: (1) the MCP endpoint at `/mcp/`, (2) OAuth 2.1 auth flow, (3) a documentation web page at `/` |
| `guidebook/Dockerfile` + `docker-compose.yml` | Containerized deployment. Runs gunicorn + uvicorn on port 8080, with an analytics dashboard sidecar on 8050 |

### The MCP tools

The server exposes ~15 tools that AI assistants can call. The important ones:

- **`guidebook.get_parent_guidance`** — All parent-specific guidance in one call (the "start here" for parents)
- **`guidebook.get_stakeholder_guidance(stakeholder)`** — Guidance for any role: parents, students, educators, administrators, ITS, vendors
- **`guidebook.get_age_restrictions`** — Which AI tools kids can use at what age (ChatGPT, Claude, Gemini, Copilot, Perplexity)
- **`guidebook.get_classroom_examples(grade_level, subject)`** — Side-by-side "without AI" vs "with AI" examples for Elementary/Middle/High across 4 subjects
- **`guidebook.get_ai_principles`** — CPS's 5 AI principles with commitments
- **`guidebook.get_positive_uses`** — Approved ways students can use GenAI (collaboration, creativity, learning)
- **`guidebook.get_glossary_term(term)`** — Look up any of 33 AI terms in plain language
- **`guidebook.search(query)`** — Full-text search across the entire guidebook
- **`guidebook.list_sections`** / **`list_topics`** / **`get_topic`** — Hierarchical browsing

Every tool response includes:
- The requested data
- An attribution line (`"Attribution: Chicago Public Schools AI Guidebook v5.0.0 (CPS) · cps.edu/aiguidebook"`)
- Source metadata (publisher, copyright, version)

### What's in the guidebook data

The JSON captures the full structure of the 52-page PDF:

- **Section I: Introduction** — Purpose, Scope, Vision, 5 AI Principles, AI Literacy (5 pillars + 4 reasons it matters), AI Basics
- **Section II: GenAI Guidance** — Stakeholder-specific guidance for 6 groups:
  - General (privacy, verification, bias, approved tools)
  - Students (approved tools, academic integrity, ethical use, positive uses, monitoring)
  - Parents & Guardians (age-appropriate tools, responsible use, opt-out options)
  - Educators & Staff (ethical use, tool approval, age restrictions table, monitoring, classroom examples for 3 grade levels × 4 subjects)
  - Administrators (enforcement, incident management, support)
  - ITS (design/development, deployment, management, monitoring/noncompliance)
  - Vendors (tool approval, privacy, ethics, support/collaboration)
- **Section III: Approved Tools** — References the CPS Ed Tech Catalog
- **Section IV: Professional Development** — 3-tier badge pathway (Foundations → Explorer → Innovator), Professional Learning Communities
- **Section V: Conclusion**
- **Section VI: Appendix** — Steering Committee + 4 subcommittees (89 members total), 33-term glossary, version history (v1.0–v5.0)

### Auth & deployment

- Optional API key auth via `MCP_API_KEY` env var
- Full OAuth 2.1 flow with PKCE support (so ChatGPT and Claude can authenticate via their standard MCP connection flows)
- Runs on a Raspberry Pi behind Caddy (reverse proxy), deployed as Docker containers
- Live at `https://cps-ai.workingpaper.co/mcp/`

### The web page

The root URL (`/`) serves a self-contained documentation page (generated dynamically from the JSON data) that:
- Shows live stats (sections, topics, recommendations, glossary terms, classroom examples)
- Explains what MCP is and how to connect from ChatGPT, Claude, or Gemini (with code snippets)
- Displays the AI Principles and age restriction table
- Provides example questions and scenarios for parents
- Lists all available tools

## Why this is interesting as an example

1. **Making policy accessible** — It turns a static PDF that most people won't read into something interactive that meets people where they are (in their AI assistant)
2. **MCP as a distribution channel** — The same structured data serves Claude, ChatGPT, and Gemini through one endpoint. The protocol handles the plumbing.
3. **Attribution by design** — Every response cites the source. The AI can't hallucinate policy because it's reading structured data, not generating from memory.
4. **Low infrastructure** — Runs on a Raspberry Pi. The entire data layer is a single JSON file. No database, no vector store, no embeddings.
5. **Real audience** — CPS is the 3rd largest school district in the US (~320,000 students). This is real policy that real parents need to understand.
