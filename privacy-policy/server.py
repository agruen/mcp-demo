import os
import secrets
import time
import hashlib
import base64
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
import contextlib
from mcp_server import mcp

MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()

_auth_codes: dict = {}
AUTH_CODE_TTL = 300


def _base_url(request: Request) -> str:
    """Public base URL of this deployment, honoring TLS-terminating proxies
    that only pass X-Forwarded-Proto (OAuth metadata must advertise https)."""
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def mcp_middleware(request, call_next):
    path = request.scope["path"]

    if path == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
        path = "/mcp/"

    if path.startswith("/mcp") and MCP_API_KEY:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {MCP_API_KEY}":
            metadata_url = f"{_base_url(request)}/.well-known/oauth-protected-resource/mcp"
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
            )

    return await call_next(request)


# CORS — claude.ai and ChatGPT run OAuth discovery and dynamic client
# registration from the browser, so the /.well-known/* and /oauth/*
# endpoints must answer preflights and carry Access-Control-Allow-Origin.
# Added after mcp_middleware so it sits outside it: preflight OPTIONS
# requests carry no Authorization header and must not hit the bearer check.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
    max_age=3600,
)


# OAuth 2.1 endpoints
#
# Only advertised when MCP_API_KEY is set. With no key the MCP endpoint is
# open, and publishing discovery metadata anyway sends clients into an OAuth
# flow the server doesn't need — claude.ai fails connector setup on it.
# Each well-known route is also served at its path-aware variant
# (RFC 9728 / RFC 8414): clients derive ".../oauth-protected-resource/mcp"
# from the resource URL ".../mcp" and try that first.

@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
@app.get("/.well-known/oauth-protected-resource/mcp/")
async def oauth_protected_resource(request: Request):
    if not MCP_API_KEY:
        return JSONResponse({"error": "not_found"}, status_code=404)
    base = _base_url(request)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }

@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
@app.get("/.well-known/oauth-authorization-server/mcp/")
async def oauth_metadata(request: Request):
    if not MCP_API_KEY:
        return JSONResponse({"error": "not_found"}, status_code=404)
    base = _base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }

@app.post("/oauth/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    # RFC 7591: omitted token_endpoint_auth_method defaults to client_secret_basic
    auth_method = body.get("token_endpoint_auth_method", "client_secret_basic")
    registration = {
        "client_id": secrets.token_urlsafe(16),
        "client_id_issued_at": int(time.time()),
        "client_name": body.get("client_name", "mcp-client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": auth_method,
    }
    # Confidential clients must receive a secret. The token endpoint doesn't
    # check it (the password IS the credential), but a registration response
    # without one is invalid and clients reject it.
    if auth_method != "none":
        registration["client_secret"] = secrets.token_urlsafe(32)
        registration["client_secret_expires_at"] = 0
    return JSONResponse(registration, status_code=201)

@app.get("/oauth/authorize")
async def oauth_authorize_get(
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
):
    if not MCP_API_KEY:
        code = secrets.token_urlsafe(32)
        _auth_codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "created_at": time.time(),
        }
        params = {"code": code}
        if state:
            params["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    html = f"""<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenAI US Privacy Policy MCP</title></head>
<body style="font-family:system-ui,sans-serif;max-width:380px;margin:80px auto;padding:0 20px">
<h2>OpenAI US Privacy Policy MCP</h2>
<p>Enter the access password to connect:</p>
<form method="POST" action="/oauth/authorize">
  <input type="hidden" name="response_type" value="{response_type}">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="password" name="password" placeholder="Password" autofocus
         style="padding:10px;width:100%;box-sizing:border-box;margin-bottom:12px;font-size:16px;border:1px solid #ccc;border-radius:4px">
  <button type="submit"
          style="padding:10px 28px;font-size:16px;cursor:pointer;border:none;border-radius:4px;background:#0f766e;color:#fff">
    Login</button>
</form>
</body></html>"""
    return HTMLResponse(html)

@app.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    form = await request.form()
    password = form.get("password", "")
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    code_challenge = str(form.get("code_challenge", ""))

    if password != MCP_API_KEY:
        return HTMLResponse(
            "<html><body style='font-family:system-ui,sans-serif;max-width:380px;margin:80px auto;padding:0 20px'>"
            "<h2>Wrong password</h2><p><a href='javascript:history.back()'>Try again</a></p>"
            "</body></html>",
            status_code=401,
        )

    now = time.time()
    expired = [k for k, v in _auth_codes.items() if now - v["created_at"] > AUTH_CODE_TTL]
    for k in expired:
        _auth_codes.pop(k, None)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "created_at": now,
    }

    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

@app.post("/oauth/token")
async def oauth_token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "client_credentials":
        client_secret = form.get("client_secret", "")
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                _, client_secret = decoded.split(":", 1)
            except Exception:
                pass
        if MCP_API_KEY and client_secret != MCP_API_KEY:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        return {"access_token": MCP_API_KEY or secrets.token_urlsafe(24), "token_type": "bearer"}

    elif grant_type == "authorization_code":
        code = form.get("code", "")
        stored = _auth_codes.pop(code, None)
        if not stored:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if time.time() - stored["created_at"] > AUTH_CODE_TTL:
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        code_verifier = form.get("code_verifier", "")
        if stored.get("code_challenge") and code_verifier:
            expected = (
                base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if expected != stored["code_challenge"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE failed"}, status_code=400)

        # With no key configured the middleware accepts any request, but the
        # token must still be non-empty or clients reject the response.
        return {"access_token": MCP_API_KEY or secrets.token_urlsafe(24), "token_type": "bearer"}

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Public documentation page (served at root)
# ---------------------------------------------------------------------------
@app.get("/")
async def mcp_docs(request: Request):
    import json
    from pathlib import Path

    # Load policy data for live stats
    policy_path = Path(os.getenv(
        "PRIVACY_POLICY_JSON_PATH",
        str(Path(__file__).resolve().parent / "data" / "openai-us-privacy-policy.json"),
    ))
    try:
        with open(policy_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}

    # The endpoint URL is derived from however the visitor reached this page,
    # so every deployment (localhost, Pi, your own domain) shows its own URL.
    mcp_url = f"{_base_url(request)}/mcp/"

    version = doc.get("semantic_version", "")
    released = doc.get("released_at", "")
    section_count = len(doc.get("sections", []))
    topic_count = sum(len(s.get("topics", [])) for s in doc.get("sections", []))
    glossary_count = sum(
        len(t.get("terms", []))
        for s in doc.get("sections", [])
        for t in s.get("topics", [])
    )
    control_count = sum(
        len(t.get("controls", []))
        for s in doc.get("sections", [])
        for t in s.get("topics", [])
    )
    retention_count = sum(
        len(t.get("retention_rules", []))
        for s in doc.get("sections", [])
        for t in s.get("topics", [])
    )

    # Data controls table
    control_rows = ""
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            for c in t.get("controls", []):
                control_rows += f"""
                <tr>
                    <td><strong>{c['control']}</strong></td>
                    <td>{c['what_it_does']}</td>
                    <td><span class="badge badge-teal">{c['where']}</span></td>
                </tr>"""

    # Retention rules table
    retention_rows = ""
    badge_for_category = {"automatic": "badge-green", "until_you_delete": "badge-amber", "extended": "badge-red"}
    label_for_category = {"automatic": "Auto-deleted", "until_you_delete": "Until you delete", "extended": "Kept longer"}
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            for r in t.get("retention_rules", []):
                cat = r.get("category", "")
                retention_rows += f"""
                <tr>
                    <td><strong>{r['data']}</strong></td>
                    <td><span class="badge {badge_for_category.get(cat, 'badge-teal')}">{label_for_category.get(cat, cat)}</span></td>
                    <td>{r['rule']}</td>
                </tr>"""

    # Build sections overview
    sections_html = ""
    for s in doc.get("sections", []):
        topic_names = ", ".join(t.get("name", "") for t in s.get("topics", []))
        sections_html += f"""
        <div class="section-row">
            <div class="section-code">{s.get('code', '')}</div>
            <div class="section-info">
                <strong>{s.get('name', '')}</strong>
                <span class="section-topics">{topic_names}</span>
            </div>
        </div>"""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAI US Privacy Policy &mdash; MCP Documentation</title>
<style>
  :root {{
    --teal: #0f766e;
    --teal-light: #14b8a6;
    --teal-pale: #ccfbf1;
    --teal-mist: #f0fdfa;
    --ink: #1f2937;
    --green: #16a34a;
    --green-pale: #dcfce7;
    --amber: #d97706;
    --amber-pale: #fef3c7;
    --red: #dc2626;
    --red-pale: #fee2e2;
    --gray-50: #f9fafb;
    --gray-100: #f3f4f6;
    --gray-200: #e5e7eb;
    --gray-400: #9ca3af;
    --gray-500: #6b7280;
    --gray-700: #374151;
    --gray-900: #111827;
    --radius: 12px;
    --radius-sm: 8px;
    --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);
    --shadow-lg: 0 10px 15px -3px rgba(0,0,0,.08), 0 4px 6px rgba(0,0,0,.04);
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: var(--gray-900);
    background: var(--gray-50);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }}

  /* ---- Hero ---- */
  .hero {{
    background: linear-gradient(135deg, var(--ink) 0%, var(--teal) 100%);
    color: #fff;
    padding: 60px 24px 48px;
    text-align: center;
  }}
  .hero-badge {{
    display: inline-block;
    background: rgba(255,255,255,.15);
    border: 1px solid rgba(255,255,255,.25);
    border-radius: 20px;
    padding: 4px 16px;
    font-size: 13px;
    letter-spacing: .5px;
    margin-bottom: 20px;
  }}
  .hero h1 {{
    font-size: clamp(28px, 5vw, 42px);
    font-weight: 700;
    line-height: 1.2;
    margin-bottom: 12px;
  }}
  .hero p {{
    font-size: 18px;
    opacity: .9;
    max-width: 640px;
    margin: 0 auto 28px;
  }}
  .hero-stats {{
    display: flex;
    justify-content: center;
    gap: 32px;
    flex-wrap: wrap;
  }}
  .hero-stat {{ text-align: center; }}
  .hero-stat .num {{
    font-size: 32px;
    font-weight: 700;
    display: block;
  }}
  .hero-stat .label {{
    font-size: 13px;
    opacity: .75;
    text-transform: uppercase;
    letter-spacing: .5px;
  }}

  .container {{
    max-width: 960px;
    margin: 0 auto;
    padding: 0 24px;
  }}

  .section-heading {{
    margin-top: 56px;
    margin-bottom: 24px;
    padding-bottom: 12px;
    border-bottom: 3px solid var(--teal);
  }}
  .section-heading h2 {{
    font-size: 26px;
    font-weight: 700;
    color: var(--teal);
  }}
  .section-heading p {{
    margin-top: 4px;
    color: var(--gray-500);
    font-size: 15px;
  }}

  .card {{
    background: #fff;
    border: 1px solid var(--gray-200);
    border-radius: var(--radius);
    padding: 28px;
    margin-bottom: 20px;
    box-shadow: var(--shadow);
  }}
  .card h3 {{
    font-size: 18px;
    margin-bottom: 12px;
    color: var(--teal);
  }}
  .card p, .card li {{
    color: var(--gray-700);
    font-size: 15px;
  }}
  .card ul {{ padding-left: 20px; margin-top: 8px; }}
  .card li {{ margin-bottom: 6px; }}
  .card li::marker {{ color: var(--teal-light); }}

  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }}
  @media (max-width: 700px) {{
    .grid-2 {{ grid-template-columns: 1fr; }}
  }}

  /* ---- Ask cards ---- */
  .ask-card {{
    background: var(--teal-mist);
    border: 1px solid var(--teal-pale);
    border-radius: var(--radius);
    padding: 20px 24px;
    margin-bottom: 16px;
    position: relative;
  }}
  .ask-card::before {{
    content: "\\201C";
    position: absolute;
    top: 8px;
    left: 12px;
    font-size: 48px;
    color: var(--teal);
    opacity: .15;
    line-height: 1;
    font-family: Georgia, serif;
  }}
  .ask-card .prompt {{
    font-size: 16px;
    font-style: italic;
    color: var(--teal);
    margin-bottom: 8px;
    padding-left: 24px;
  }}
  .ask-card .why {{
    font-size: 13px;
    color: var(--gray-500);
    padding-left: 24px;
  }}

  /* ---- Scenario cards ---- */
  .scenario {{
    background: #fff;
    border: 1px solid var(--gray-200);
    border-left: 4px solid var(--teal-light);
    border-radius: var(--radius-sm);
    padding: 20px 24px;
    margin-bottom: 16px;
    box-shadow: var(--shadow);
  }}
  .scenario .label {{
    display: inline-block;
    background: var(--amber-pale);
    color: var(--amber);
    font-size: 11px;
    font-weight: 600;
    padding: 2px 10px;
    border-radius: 10px;
    text-transform: uppercase;
    letter-spacing: .4px;
    margin-bottom: 8px;
  }}
  .scenario h4 {{
    font-size: 16px;
    margin-bottom: 8px;
    color: var(--gray-900);
  }}
  .scenario p {{ font-size: 14px; color: var(--gray-700); }}
  .scenario .try {{
    margin-top: 10px;
    padding: 10px 14px;
    background: var(--gray-50);
    border-radius: var(--radius-sm);
    font-family: "SF Mono", "Fira Code", monospace;
    font-size: 13px;
    color: var(--teal);
    border: 1px solid var(--gray-200);
  }}

  /* ---- Tables ---- */
  .data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    margin-top: 12px;
  }}
  .data-table th {{
    background: var(--teal);
    color: #fff;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
  }}
  .data-table th:first-child {{ border-radius: var(--radius-sm) 0 0 0; }}
  .data-table th:last-child {{ border-radius: 0 var(--radius-sm) 0 0; }}
  .data-table td {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);
    vertical-align: middle;
  }}
  .data-table tr:hover td {{ background: var(--gray-50); }}
  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 10px;
    font-size: 12px;
    font-weight: 500;
  }}
  .badge-green {{ background: var(--green-pale); color: var(--green); }}
  .badge-amber {{ background: var(--amber-pale); color: var(--amber); }}
  .badge-red {{ background: var(--red-pale); color: var(--red); }}
  .badge-teal {{ background: var(--teal-pale); color: var(--teal); }}

  /* ---- Section overview rows ---- */
  .section-row {{
    display: flex;
    align-items: flex-start;
    gap: 16px;
    padding: 14px 0;
    border-bottom: 1px solid var(--gray-200);
  }}
  .section-row:last-child {{ border-bottom: none; }}
  .section-code {{
    width: 40px;
    height: 40px;
    background: var(--teal-pale);
    color: var(--teal);
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 15px;
    flex-shrink: 0;
  }}
  .section-info strong {{ font-size: 15px; display: block; }}
  .section-topics {{
    font-size: 13px;
    color: var(--gray-500);
    display: block;
    margin-top: 2px;
  }}

  /* ---- Tool list ---- */
  .tool-item {{
    padding: 14px 0;
    border-bottom: 1px solid var(--gray-200);
  }}
  .tool-item:last-child {{ border-bottom: none; }}
  .tool-name {{
    font-family: "SF Mono", "Fira Code", monospace;
    font-size: 14px;
    font-weight: 600;
    color: var(--teal);
  }}
  .tool-desc {{
    font-size: 13px;
    color: var(--gray-500);
    margin-top: 2px;
  }}
  .tool-tag {{
    display: inline-block;
    background: var(--green-pale);
    color: var(--green);
    font-size: 10px;
    font-weight: 600;
    padding: 1px 8px;
    border-radius: 8px;
    text-transform: uppercase;
    letter-spacing: .3px;
    margin-left: 8px;
    vertical-align: middle;
  }}

  .footer {{
    margin-top: 64px;
    padding: 32px 24px;
    background: var(--gray-100);
    border-top: 1px solid var(--gray-200);
    text-align: center;
    color: var(--gray-500);
    font-size: 13px;
  }}
  .footer a {{ color: var(--teal); text-decoration: none; }}
  .footer a:hover {{ text-decoration: underline; }}

  .setup-nav {{
    display: flex;
    gap: 12px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }}
  .setup-nav a {{
    display: inline-block;
    padding: 10px 24px;
    background: #fff;
    border: 2px solid var(--gray-200);
    border-radius: var(--radius-sm);
    color: var(--gray-700);
    text-decoration: none;
    font-weight: 600;
    font-size: 15px;
    transition: all .15s ease;
  }}
  .setup-nav a:hover {{
    border-color: var(--teal);
    color: var(--teal);
    box-shadow: var(--shadow);
  }}

  code {{ font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace; }}

  .callout {{
    background: var(--amber-pale);
    border: 1px solid #fcd34d;
    border-radius: var(--radius);
    padding: 20px 24px;
    margin: 24px 0;
    font-size: 14px;
  }}
  .callout strong {{ color: var(--amber); }}
</style>
</head>
<body>

<!-- ============================== HERO ============================== -->
<div class="hero">
  <div class="hero-badge">MCP Server &middot; Unofficial &middot; Policy updated {released}</div>
  <h1>OpenAI US Privacy Policy</h1>
  <p>An interactive, AI-accessible version of OpenAI's US Privacy Policy &mdash; ask your AI assistant what OpenAI knows about you, and what you can do about it.</p>
  <div class="hero-stats">
    <div class="hero-stat"><span class="num">{section_count}</span><span class="label">Sections</span></div>
    <div class="hero-stat"><span class="num">{topic_count}</span><span class="label">Topics</span></div>
    <div class="hero-stat"><span class="num">{control_count}</span><span class="label">Data Controls</span></div>
    <div class="hero-stat"><span class="num">{retention_count}</span><span class="label">Retention Rules</span></div>
    <div class="hero-stat"><span class="num">{glossary_count}</span><span class="label">Glossary Terms</span></div>
  </div>
</div>

<div class="container">

<!-- ============================== WHAT IS THIS ============================== -->
<div class="section-heading">
  <h2>What Is This?</h2>
  <p>How this server works and what it makes possible</p>
</div>

<div class="card">
  <h3>A Privacy Policy You Can Actually Question</h3>
  <p>This server takes the <strong>OpenAI US Privacy Policy</strong> (updated {released}) and makes it available through the <strong>Model Context Protocol (MCP)</strong> &mdash; an open standard that lets AI assistants like Claude, ChatGPT, and Gemini access structured data in real time.</p>
  <p style="margin-top:12px">Instead of scrolling a legal document, you <strong>ask questions in plain language</strong> &mdash; <em>"Does OpenAI use my chats to train its models?"</em> &mdash; and get precise answers drawn from the policy itself, with attribution on every response.</p>
</div>

<div class="callout">
  <strong>Unofficial.</strong> This is a teaching demo built for a class on making MCP servers. It is not affiliated with or endorsed by OpenAI. The policy text belongs to OpenAI &mdash; always confirm details against the <a href="https://openai.com/policies/us-privacy-policy/" style="color:var(--amber)">official policy</a>.
</div>

<div class="grid-2">
  <div class="card">
    <h3>For ChatGPT Users</h3>
    <ul>
      <li>Find out whether your chats train OpenAI's models &mdash; and switch it off</li>
      <li>Learn what "delete" actually deletes, and on what timeline</li>
      <li>See every privacy setting in your account and where it lives</li>
      <li>Understand Temporary Chats, Memory, and data export</li>
    </ul>
  </div>
  <div class="card">
    <h3>For Parents &amp; Privacy-Curious People</h3>
    <ul>
      <li>Check the age rules: under-13 ban, teen parental permission, account linking</li>
      <li>Learn whether data is sold or shared for advertising &mdash; and how to opt out</li>
      <li>Exercise your state-law rights: access, correction, deletion, appeals</li>
      <li>Decode the jargon with a {glossary_count}-term plain-language glossary</li>
    </ul>
  </div>
</div>

<!-- ============================== GET STARTED ============================== -->
<div class="section-heading">
  <h2>Get Started</h2>
  <p>Connect this policy to your AI assistant &mdash; pick any platform</p>
</div>

<div class="card" style="text-align:center; padding:32px 28px;">
  <p style="font-size:15px; color:var(--gray-700); margin-bottom:16px">All platforms connect to the same MCP endpoint:</p>
  <div id="mcp-url-box" style="background: var(--gray-100); padding: 16px 20px; border-radius: var(--radius-sm); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 17px; color: var(--teal); border: 1px solid var(--gray-200); display:inline-block; cursor:pointer; user-select:all; position:relative;" onclick="navigator.clipboard.writeText('{mcp_url}');var el=document.getElementById('copy-toast');el.style.opacity=1;setTimeout(function(){{el.style.opacity=0}},1500);">
    {mcp_url}
    <span id="copy-toast" style="position:absolute;top:-32px;left:50%;transform:translateX(-50%);background:var(--teal);color:#fff;padding:4px 14px;border-radius:6px;font-size:12px;font-family:system-ui,sans-serif;opacity:0;transition:opacity .2s;pointer-events:none;">Copied!</span>
  </div>
  <p style="margin-top:10px; font-size:13px; color:var(--gray-400)">Click to copy</p>
</div>

<div class="setup-nav">
  <a href="#setup-chatgpt">ChatGPT</a>
  <a href="#setup-claude">Claude</a>
  <a href="#setup-gemini">Gemini</a>
</div>

<!-- ---- ChatGPT ---- -->
<div class="card" id="setup-chatgpt">
  <h3>ChatGPT</h3>
  <p style="margin-bottom:12px; font-size:14px; color:var(--gray-500)">Plus, Team, and Enterprise plans</p>
  <ol style="padding-left:20px; font-size:14px; color:var(--gray-700)">
    <li style="margin-bottom:6px">Go to <a href="https://chatgpt.com" style="color:var(--teal)">chatgpt.com</a> &rarr; <strong>Settings</strong> &rarr; <strong>Connected apps</strong></li>
    <li style="margin-bottom:6px">Click <strong>Add connection</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">{mcp_url}</code></li>
    <li style="margin-bottom:6px">Complete any authentication prompts</li>
    <li>Start a new chat &mdash; the policy tools appear when you click the tools icon</li>
  </ol>
  <details style="margin-top:16px; font-size:13px; color:var(--gray-500)">
    <summary style="cursor:pointer; font-weight:600; color:var(--gray-700)">Developer: OpenAI Responses API</summary>
    <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5; margin-top:10px">import openai

client = openai.OpenAI()
resp = client.responses.create(
    model="gpt-4.1",
    input="Does OpenAI use my chats to train models?",
    tools=[{{
        "type": "mcp",
        "server_label": "openai-privacy-policy",
        "server_url": "{mcp_url}",
        "require_approval": "never",
    }}],
)</div>
  </details>
</div>

<!-- ---- Claude ---- -->
<div class="card" id="setup-claude">
  <h3>Claude</h3>
  <p style="margin-bottom:12px; font-size:14px; color:var(--gray-500)">claude.ai, Desktop app, or Claude Code</p>
  <ol style="padding-left:20px; font-size:14px; color:var(--gray-700)">
    <li style="margin-bottom:6px">Go to <a href="https://claude.ai" style="color:var(--teal)">claude.ai</a> &rarr; click your name &rarr; <strong>Settings</strong> &rarr; <strong>Integrations</strong></li>
    <li style="margin-bottom:6px">Click <strong>Add Integration</strong> &rarr; choose <strong>MCP</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">{mcp_url}</code></li>
    <li style="margin-bottom:6px">If prompted, enter the server password</li>
    <li>Start a new chat &mdash; Claude will automatically use the policy tools</li>
  </ol>
  <details style="margin-top:16px; font-size:13px; color:var(--gray-500)">
    <summary style="cursor:pointer; font-weight:600; color:var(--gray-700)">Claude Desktop or Claude Code</summary>
    <div style="margin-top:10px">
      <p style="margin-bottom:8px"><strong>Desktop app</strong> &mdash; add to your config file (<code style="font-size:12px">~/Library/Application Support/Claude/claude_desktop_config.json</code>):</p>
      <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5">{{
  "mcpServers": {{
    "openai-privacy-policy": {{
      "type": "streamable-http",
      "url": "{mcp_url}"
    }}
  }}
}}</div>
      <p style="margin-top:14px; margin-bottom:8px"><strong>Claude Code CLI</strong>:</p>
      <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5">claude mcp add openai-privacy-policy \\
  --transport streamable-http \\
  {mcp_url}</div>
    </div>
  </details>
</div>

<!-- ---- Gemini ---- -->
<div class="card" id="setup-gemini">
  <h3>Gemini</h3>
  <p style="margin-bottom:12px; font-size:14px; color:var(--gray-500)">Google AI Studio or Gemini API</p>
  <ol style="padding-left:20px; font-size:14px; color:var(--gray-700)">
    <li style="margin-bottom:6px">Go to <a href="https://aistudio.google.com" style="color:var(--teal)">aistudio.google.com</a> &rarr; open a prompt</li>
    <li style="margin-bottom:6px">In the left panel, click <strong>Tools</strong> &rarr; <strong>Add tool</strong> &rarr; <strong>MCP Server</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">{mcp_url}</code></li>
    <li style="margin-bottom:6px">Complete authentication if prompted</li>
    <li>The policy tools appear in your tool list &mdash; start asking questions</li>
  </ol>
</div>

<div class="callout" style="margin-top:24px;">
  <strong>That's it!</strong> Once connected on any platform, just type a question in plain English like
  <em>"What happens when I delete a ChatGPT conversation?"</em>
  &mdash; the AI will automatically look up the answer from the policy.
</div>

<!-- ============================== WHAT'S INSIDE ============================== -->
<div class="section-heading">
  <h2>What's Inside</h2>
  <p>The full policy, structured and searchable</p>
</div>

<div class="card">
  <h3>Policy Sections</h3>
  {sections_html}
</div>

<div class="card">
  <h3>Your Data Controls</h3>
  <p style="margin-bottom:8px; color: var(--gray-500); font-size:14px">Every privacy setting the policy describes, and where to find it:</p>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Control</th>
        <th>What It Does</th>
        <th>Where</th>
      </tr></thead>
      <tbody>{control_rows}</tbody>
    </table>
  </div>
</div>

<div class="card">
  <h3>How Long Your Data Is Kept</h3>
  <p style="margin-bottom:8px; color: var(--gray-500); font-size:14px">What deletion actually does, item by item:</p>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Data</th>
        <th>Retention</th>
        <th>Rule</th>
      </tr></thead>
      <tbody>{retention_rows}</tbody>
    </table>
  </div>
</div>

<!-- ============================== USER GUIDE ============================== -->
<div class="section-heading">
  <h2>A Guide for Users</h2>
  <p>Questions worth asking once you're connected</p>
</div>

<div class="callout">
  <strong>How does this work?</strong> When you connect an AI assistant to this server, it can read the OpenAI US Privacy Policy as structured data and answer your questions from the actual policy text. You ask in plain English &mdash; the AI looks up the answer for you, with a citation on every response.
</div>

<div class="grid-2">
  <div class="ask-card">
    <div class="prompt">"Does OpenAI use my ChatGPT conversations to train its models?"</div>
    <div class="why">Pulls the model-training topic and the opt-out steps &mdash; the 'Improve the model for everyone' setting, plus when Temporary Chat is the better tool.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"What actually happens when I delete a chat? Is it really gone?"</div>
    <div class="why">Returns the retention rules: 30-day deletion window, the legal/safety exceptions, and the audit record OpenAI keeps of your deletion request.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"Does OpenAI sell my data?"</div>
    <div class="why">Returns the disclosure section: no 'selling', but limited sharing with marketing partners that counts as 'targeted advertising' under state law &mdash; and how to opt out, including via Global Privacy Control.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"My 12-year-old wants to use ChatGPT. Is that allowed?"</div>
    <div class="why">Pulls the children policy: under 13 not permitted, 13&ndash;17 need parental permission, and how parent-teen account linking works.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"How do I get a copy of everything OpenAI has about me?"</div>
    <div class="why">Returns the export control plus your statutory access and portability rights, and the request channels (privacy.openai.com, dsar@openai.com).</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"What's the difference between Memory and Temporary Chat?"</div>
    <div class="why">Pulls both glossary terms and the data controls table &mdash; what each feature stores, for how long, and where to turn them on or off.</div>
  </div>
</div>

<div style="margin-top: 24px;">
  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>You told ChatGPT something sensitive and want it gone</h4>
    <p>You want to understand deletion, training, and what you can still control. Ask:</p>
    <div class="try">"I shared medical information in a chat. How do I delete it, will it be used for training, and what should I do differently next time?"</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">The AI will pull the retention rules (30-day deletion), the training opt-out, and Temporary Chat guidance for future sensitive conversations.</p>
  </div>

  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>You're a parent setting up ChatGPT with your teenager</h4>
    <p>You want the actual rules, not vibes. Ask:</p>
    <div class="try">"What are OpenAI's age rules? What can I see and control if I link accounts with my 15-year-old?"</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">Returns the age rules table, parental permission requirement, account linking (settings management + safety alerts), and the fact that targeted-ad sharing isn't done for known under-18 users.</p>
  </div>

  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>You hit a wall of jargon &mdash; "de-identified", "data controller", "GPC"</h4>
    <p>The policy is full of terms of art. Ask:</p>
    <div class="try">"What does 'de-identified' mean in OpenAI's privacy policy? And who is the data controller for US users?"</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">Pulls plain-language definitions from the {glossary_count}-term glossary, written for normal humans, grounded in the policy text.</p>
  </div>
</div>

<!-- ============================== TOOLS REFERENCE ============================== -->
<div class="section-heading">
  <h2>Available Tools</h2>
  <p>What AI assistants can access through this server</p>
</div>

<div class="card">
  <div class="tool-item">
    <span class="tool-name">policy.get_privacy_essentials</span>
    <span class="tool-tag">Start Here</span>
    <div class="tool-desc">The whole policy in one call: what's collected, model training and opt-out, every data control, retention basics, key contacts, and questions to ask next.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_training_optout</span>
    <div class="tool-desc">The most-asked question, answered precisely: how Content trains models, the 'Improve the model for everyone' setting, and Temporary Chats.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_data_controls</span>
    <div class="tool-desc">All {control_count} privacy settings &mdash; what each does and where it lives.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_retention_rules</span>
    <div class="tool-desc">How long each kind of data is kept: the 30-day deletion window, auto-deleted data, and the legal/safety exceptions.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_your_rights</span>
    <div class="tool-desc">Statutory rights (access, correction, deletion, portability), state-law disclosures, targeted-ad opt-out, and how to file requests.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_children_policy</span>
    <div class="tool-desc">Age rules: under-13 ban, teen parental permission, parent-teen account linking, and how to report underage use.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_concern_guidance</span>
    <div class="tool-desc">Answer a specific worry: training, deletion, ads, sharing, children, security, rights, or collection.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_data_collected</span>
    <div class="tool-desc">What OpenAI collects, by stream: data you provide, data from usage, data from other sources &mdash; plus the state-law category table.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_glossary_term</span>
    <div class="tool-desc">Look up any term &mdash; Personal Data, Temporary Chat, GPC, DSAR, data controller, and more. Plain language.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.search</span>
    <div class="tool-desc">Full-text search across every section, topic, control, retention rule, and glossary term.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.list_sections / list_topics / get_topic</span>
    <div class="tool-desc">Hierarchical browsing of all {section_count} sections and {topic_count} topics.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.list_glossary</span>
    <div class="tool-desc">List all {glossary_count} glossary terms.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_version_info</span>
    <div class="tool-desc">Policy version, effective date, scope note, and disclaimer.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.get_usage_guide</span>
    <div class="tool-desc">Navigation help for AI assistants &mdash; explains the structure and recommended workflows.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">policy.log_activity</span>
    <div class="tool-desc">Self-reporting hook: assistants log what they helped with, powering the reporting dashboard at /reporting/.</div>
  </div>
</div>

<div class="card">
  <h3>Data Source &amp; Attribution</h3>
  <p>All data served by this MCP comes directly from the <strong>OpenAI US Privacy Policy</strong>, updated {released}.</p>
  <p style="margin-top:8px; font-size:14px; color: var(--gray-500)">
    The policy is published by OpenAI. This server is an unofficial teaching demo and is not affiliated with or endorsed by OpenAI.
    Every tool response includes an attribution line linking back to the source document.
  </p>
  <p style="margin-top:8px; font-size:14px">
    For the official document, visit <a href="https://openai.com/policies/us-privacy-policy/" style="color: var(--teal)">openai.com/policies/us-privacy-policy</a>.
  </p>
  <p style="margin-top:12px; font-size:13px; color: var(--gray-500)">
    <strong>Telemetry:</strong> to power the usage dashboard at <a href="/reporting/" style="color: var(--teal)">/reporting/</a>, this server logs tool calls (tool name and arguments)
    and summaries that AI assistants write about what they helped with. No account identity or IP addresses are recorded in those analytics logs &mdash;
    but assistant-written summaries may echo details a user shared in conversation, so don't share anything here you wouldn't put in a question to a stranger.
  </p>
</div>

</div><!-- /container -->

<div class="footer">
  <p>
    OpenAI US Privacy Policy MCP (unofficial) &middot; Policy updated {released} &middot;
    Source: <a href="https://openai.com/policies/us-privacy-policy/">OpenAI US Privacy Policy</a>
  </p>
  <p style="margin-top:8px">
    Built with the <a href="https://modelcontextprotocol.io">Model Context Protocol</a> &middot;
    Usage analytics at <a href="/reporting/">/reporting/</a>
  </p>
</div>

</body>
</html>"""
    return HTMLResponse(page)


# MCP mount
mcp_http_app = mcp.streamable_http_app()
app.mount("/mcp", mcp_http_app)


# Reporting dashboard (embedded — no separate container needed)
try:
    from starlette.middleware.wsgi import WSGIMiddleware
    from dashboard.app import app as dash_app
    app.mount("/reporting", WSGIMiddleware(dash_app.server))
except Exception:
    pass  # dash/plotly not installed — dashboard disabled


# Health endpoint
@app.get("/healthz")
async def healthz():
    return {"ok": True}
