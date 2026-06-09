import os
import secrets
import time
import hashlib
import base64
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
import contextlib
from mcp_server import mcp

MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()

_auth_codes: dict = {}
AUTH_CODE_TTL = 300


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
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

    return await call_next(request)


# OAuth 2.1 endpoints
@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "resource": base,
        "authorization_servers": [base],
    }

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }

@app.post("/oauth/register")
async def oauth_register(request: Request):
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    return JSONResponse({
        "client_id": client_id,
        "client_name": body.get("client_name", "mcp-client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "client_secret_post"),
    }, status_code=201)

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
<head><meta name="viewport" content="width=device-width,initial-scale=1"><title>CPS AI Guidebook</title></head>
<body style="font-family:system-ui,sans-serif;max-width:380px;margin:80px auto;padding:0 20px">
<h2>CPS AI Guidebook</h2>
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
          style="padding:10px 28px;font-size:16px;cursor:pointer;border:none;border-radius:4px;background:#1a3d7c;color:#fff">
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
        if client_secret != MCP_API_KEY:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        return {"access_token": MCP_API_KEY, "token_type": "bearer"}

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

        return {"access_token": MCP_API_KEY, "token_type": "bearer"}

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Public documentation page (served at root)
# ---------------------------------------------------------------------------
@app.get("/")
async def mcp_docs():
    import json
    from pathlib import Path

    # Load guidebook data for live stats
    guidebook_path = Path(os.getenv(
        "CPS_GUIDEBOOK_JSON_PATH",
        str(Path(__file__).resolve().parent / "data" / "cps-ai-guidebook.json"),
    ))
    try:
        with open(guidebook_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}

    version = doc.get("semantic_version", "5.0.0")
    section_count = len(doc.get("sections", []))
    topic_count = sum(len(s.get("topics", [])) for s in doc.get("sections", []))
    glossary_count = sum(
        len(t.get("terms", []))
        for s in doc.get("sections", [])
        for t in s.get("topics", [])
    )

    # Count classroom examples
    example_count = 0
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            for sub in t.get("subtopics", []):
                for cex in sub.get("classroom_examples", []):
                    example_count += len(cex.get("examples", []))

    # Collect all recommendations count
    rec_count = 0
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            rec_count += len(t.get("recommendations", []))
            for sub in t.get("subtopics", []):
                rec_count += len(sub.get("recommendations", []))

    # Get principles
    principles_html = ""
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            if t.get("id") == "introduction_principles":
                for p in t.get("principles", []):
                    principles_html += f"""
                    <div class="principle-card">
                        <div class="principle-number">{p['number']}</div>
                        <div>
                            <strong>{p['name']}</strong>
                            <p class="principle-desc">{p['description']}</p>
                        </div>
                    </div>"""

    # Get age restrictions table
    age_rows = ""
    for s in doc.get("sections", []):
        for t in s.get("topics", []):
            for sub in t.get("subtopics", []):
                for ar in sub.get("age_restrictions", []):
                    consent_class = ""
                    if "no access" in ar.get("parental_consent", "").lower():
                        consent_class = "badge-red"
                    elif "parental consent" in ar.get("parental_consent", "").lower():
                        consent_class = "badge-amber"
                    else:
                        consent_class = "badge-green"

                    age_rows += f"""
                    <tr>
                        <td><strong>{ar['tool']}</strong><span class="company">{ar['company']}</span></td>
                        <td>{ar['no_access']}</td>
                        <td><span class="badge {consent_class}">{ar['parental_consent']}</span></td>
                        <td>{ar['no_permission_needed']}</td>
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
<title>CPS AI Guidebook &mdash; MCP Documentation</title>
<style>
  :root {{
    --blue: #1a3d7c;
    --blue-light: #2b5ea7;
    --blue-pale: #e8f0fe;
    --blue-mist: #f0f5ff;
    --gold: #c8952e;
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
    background: linear-gradient(135deg, var(--blue) 0%, var(--blue-light) 100%);
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
  .hero-stat {{
    text-align: center;
  }}
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

  /* ---- Container ---- */
  .container {{
    max-width: 960px;
    margin: 0 auto;
    padding: 0 24px;
  }}

  /* ---- Section headings ---- */
  .section-heading {{
    margin-top: 56px;
    margin-bottom: 24px;
    padding-bottom: 12px;
    border-bottom: 3px solid var(--blue);
  }}
  .section-heading h2 {{
    font-size: 26px;
    font-weight: 700;
    color: var(--blue);
  }}
  .section-heading p {{
    margin-top: 4px;
    color: var(--gray-500);
    font-size: 15px;
  }}

  /* ---- Cards ---- */
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
    color: var(--blue);
  }}
  .card p, .card li {{
    color: var(--gray-700);
    font-size: 15px;
  }}
  .card ul {{
    padding-left: 20px;
    margin-top: 8px;
  }}
  .card li {{
    margin-bottom: 6px;
  }}
  .card li::marker {{
    color: var(--blue-light);
  }}

  /* ---- Grid ---- */
  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }}
  @media (max-width: 700px) {{
    .grid-2 {{ grid-template-columns: 1fr; }}
  }}

  /* ---- Ask cards (parent prompts) ---- */
  .ask-card {{
    background: var(--blue-mist);
    border: 1px solid var(--blue-pale);
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
    color: var(--blue);
    opacity: .15;
    line-height: 1;
    font-family: Georgia, serif;
  }}
  .ask-card .prompt {{
    font-size: 16px;
    font-style: italic;
    color: var(--blue);
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
    border-left: 4px solid var(--gold);
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
  .scenario p {{
    font-size: 14px;
    color: var(--gray-700);
  }}
  .scenario .try {{
    margin-top: 10px;
    padding: 10px 14px;
    background: var(--gray-50);
    border-radius: var(--radius-sm);
    font-family: "SF Mono", "Fira Code", monospace;
    font-size: 13px;
    color: var(--blue);
    border: 1px solid var(--gray-200);
  }}

  /* ---- Principles ---- */
  .principle-card {{
    display: flex;
    align-items: flex-start;
    gap: 16px;
    padding: 16px 0;
    border-bottom: 1px solid var(--gray-200);
  }}
  .principle-card:last-child {{ border-bottom: none; }}
  .principle-number {{
    width: 36px;
    height: 36px;
    background: var(--blue);
    color: #fff;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 16px;
    flex-shrink: 0;
    margin-top: 2px;
  }}
  .principle-desc {{
    font-size: 14px;
    color: var(--gray-500);
    margin-top: 4px;
  }}

  /* ---- Age table ---- */
  .age-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    margin-top: 12px;
  }}
  .age-table th {{
    background: var(--blue);
    color: #fff;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
  }}
  .age-table th:first-child {{ border-radius: var(--radius-sm) 0 0 0; }}
  .age-table th:last-child {{ border-radius: 0 var(--radius-sm) 0 0; }}
  .age-table td {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);
    vertical-align: middle;
  }}
  .age-table tr:hover td {{ background: var(--gray-50); }}
  .company {{
    display: block;
    font-size: 12px;
    color: var(--gray-400);
    font-weight: 400;
  }}
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
    background: var(--blue-pale);
    color: var(--blue);
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
    color: var(--blue);
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

  /* ---- Footer ---- */
  .footer {{
    margin-top: 64px;
    padding: 32px 24px;
    background: var(--gray-100);
    border-top: 1px solid var(--gray-200);
    text-align: center;
    color: var(--gray-500);
    font-size: 13px;
  }}
  .footer a {{
    color: var(--blue);
    text-decoration: none;
  }}
  .footer a:hover {{
    text-decoration: underline;
  }}

  /* ---- Setup nav ---- */
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
    border-color: var(--blue);
    color: var(--blue);
    box-shadow: var(--shadow);
  }}

  code {{
    font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
  }}

  /* ---- Callout ---- */
  .callout {{
    background: var(--amber-pale);
    border: 1px solid #fcd34d;
    border-radius: var(--radius);
    padding: 20px 24px;
    margin: 24px 0;
    font-size: 14px;
  }}
  .callout strong {{
    color: var(--amber);
  }}
</style>
</head>
<body>

<!-- ============================== HERO ============================== -->
<div class="hero">
  <div class="hero-badge">MCP Server &middot; Version {version}</div>
  <h1>CPS AI Guidebook</h1>
  <p>An interactive, AI-accessible version of the Chicago Public Schools AI Guidebook &mdash; helping parents, educators, and the community navigate AI in education.</p>
  <div class="hero-stats">
    <div class="hero-stat"><span class="num">{section_count}</span><span class="label">Sections</span></div>
    <div class="hero-stat"><span class="num">{topic_count}</span><span class="label">Topics</span></div>
    <div class="hero-stat"><span class="num">{rec_count}</span><span class="label">Recommendations</span></div>
    <div class="hero-stat"><span class="num">{glossary_count}</span><span class="label">Glossary Terms</span></div>
    <div class="hero-stat"><span class="num">{example_count}</span><span class="label">Classroom Examples</span></div>
  </div>
</div>

<div class="container">

<!-- ============================== WHAT IS THIS ============================== -->
<div class="section-heading">
  <h2>What Is This?</h2>
  <p>How this server works and what it makes possible</p>
</div>

<div class="card">
  <h3>The CPS AI Guidebook, Made Interactive</h3>
  <p>This server takes the official <strong>Chicago Public Schools AI Guidebook (Version {version})</strong> and makes it available through the <strong>Model Context Protocol (MCP)</strong> &mdash; an open standard that lets AI assistants like Claude, ChatGPT, and others access structured data in real time.</p>
  <p style="margin-top:12px">Instead of reading a 52-page PDF, parents can <strong>ask questions in plain language</strong> and get precise, sourced answers drawn directly from the official CPS guidelines. Every response includes attribution back to the guidebook.</p>
</div>

<div class="grid-2">
  <div class="card">
    <h3>For Parents &amp; Guardians</h3>
    <ul>
      <li>Understand what AI tools your child can use at school</li>
      <li>Learn about age restrictions on tools like ChatGPT, Claude, and Gemini</li>
      <li>Know your opt-out rights</li>
      <li>Get conversation starters to talk to your kids about AI</li>
      <li>See exactly how AI is being used in classrooms</li>
    </ul>
  </div>
  <div class="card">
    <h3>For Educators &amp; Staff</h3>
    <ul>
      <li>Find specific guidance for your role quickly</li>
      <li>Look up classroom integration examples by grade level and subject</li>
      <li>Search for policies on academic integrity, monitoring, and approved tools</li>
      <li>Reference AI terminology in the built-in glossary</li>
    </ul>
  </div>
</div>

<!-- ============================== GET STARTED ============================== -->
<div class="section-heading">
  <h2>Get Started</h2>
  <p>Connect this guidebook to your AI assistant &mdash; pick any platform</p>
</div>

<div class="card" style="text-align:center; padding:32px 28px;">
  <p style="font-size:15px; color:var(--gray-700); margin-bottom:16px">All platforms connect to the same MCP endpoint:</p>
  <div id="mcp-url-box" style="background: var(--gray-100); padding: 16px 20px; border-radius: var(--radius-sm); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 17px; color: var(--blue); border: 1px solid var(--gray-200); display:inline-block; cursor:pointer; user-select:all; position:relative;" onclick="navigator.clipboard.writeText('https://cps-ai.workingpaper.co/mcp/');var el=document.getElementById('copy-toast');el.style.opacity=1;setTimeout(function(){{el.style.opacity=0}},1500);">
    https://cps-ai.workingpaper.co/mcp/
    <span id="copy-toast" style="position:absolute;top:-32px;left:50%;transform:translateX(-50%);background:var(--blue);color:#fff;padding:4px 14px;border-radius:6px;font-size:12px;font-family:system-ui,sans-serif;opacity:0;transition:opacity .2s;pointer-events:none;">Copied!</span>
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
    <li style="margin-bottom:6px">Go to <a href="https://chatgpt.com" style="color:var(--blue)">chatgpt.com</a> &rarr; <strong>Settings</strong> &rarr; <strong>Connected apps</strong></li>
    <li style="margin-bottom:6px">Click <strong>Add connection</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">https://cps-ai.workingpaper.co/mcp/</code></li>
    <li style="margin-bottom:6px">Complete any authentication prompts</li>
    <li>Start a new chat &mdash; the guidebook tools appear when you click the tools icon</li>
  </ol>
  <details style="margin-top:16px; font-size:13px; color:var(--gray-500)">
    <summary style="cursor:pointer; font-weight:600; color:var(--gray-700)">Developer: OpenAI Responses API</summary>
    <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5; margin-top:10px">import openai

client = openai.OpenAI()
resp = client.responses.create(
    model="gpt-4.1",
    input="What AI tools can my 10-year-old use?",
    tools=[{{
        "type": "mcp",
        "server_label": "cps-guidebook",
        "server_url": "https://cps-ai.workingpaper.co/mcp/",
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
    <li style="margin-bottom:6px">Go to <a href="https://claude.ai" style="color:var(--blue)">claude.ai</a> &rarr; click your name &rarr; <strong>Settings</strong> &rarr; <strong>Integrations</strong></li>
    <li style="margin-bottom:6px">Click <strong>Add Integration</strong> &rarr; choose <strong>MCP</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">https://cps-ai.workingpaper.co/mcp/</code></li>
    <li style="margin-bottom:6px">If prompted, enter the server password</li>
    <li>Start a new chat &mdash; Claude will automatically use the guidebook tools</li>
  </ol>
  <details style="margin-top:16px; font-size:13px; color:var(--gray-500)">
    <summary style="cursor:pointer; font-weight:600; color:var(--gray-700)">Claude Desktop or Claude Code</summary>
    <div style="margin-top:10px">
      <p style="margin-bottom:8px"><strong>Desktop app</strong> &mdash; add to your config file (<code style="font-size:12px">~/Library/Application Support/Claude/claude_desktop_config.json</code>):</p>
      <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5">{{
  "mcpServers": {{
    "cps-guidebook": {{
      "type": "streamable-http",
      "url": "https://cps-ai.workingpaper.co/mcp/"
    }}
  }}
}}</div>
      <p style="margin-top:14px; margin-bottom:8px"><strong>Claude Code CLI</strong>:</p>
      <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5">claude mcp add cps-guidebook \
  --transport streamable-http \
  https://cps-ai.workingpaper.co/mcp/</div>
    </div>
  </details>
</div>

<!-- ---- Gemini ---- -->
<div class="card" id="setup-gemini">
  <h3>Gemini</h3>
  <p style="margin-bottom:12px; font-size:14px; color:var(--gray-500)">Google AI Studio or Gemini API</p>
  <ol style="padding-left:20px; font-size:14px; color:var(--gray-700)">
    <li style="margin-bottom:6px">Go to <a href="https://aistudio.google.com" style="color:var(--blue)">aistudio.google.com</a> &rarr; open a prompt</li>
    <li style="margin-bottom:6px">In the left panel, click <strong>Tools</strong> &rarr; <strong>Add tool</strong> &rarr; <strong>MCP Server</strong></li>
    <li style="margin-bottom:6px">Paste the URL: <code style="background:var(--gray-100);padding:2px 6px;border-radius:4px;font-size:13px">https://cps-ai.workingpaper.co/mcp/</code></li>
    <li style="margin-bottom:6px">Complete authentication if prompted</li>
    <li>The guidebook tools appear in your tool list &mdash; start asking questions</li>
  </ol>
  <details style="margin-top:16px; font-size:13px; color:var(--gray-500)">
    <summary style="cursor:pointer; font-weight:600; color:var(--gray-700)">Developer: Gemini API (Python)</summary>
    <div style="background:var(--gray-100); padding:14px 18px; border-radius:var(--radius-sm); font-family:'SF Mono','Fira Code',monospace; font-size:13px; color:var(--gray-900); border:1px solid var(--gray-200); overflow-x:auto; white-space:pre; line-height:1.5; margin-top:10px">from google import genai

client = genai.Client()
tools = genai.types.Tool(
    mcp_servers=[genai.types.McpServer(
        url="https://cps-ai.workingpaper.co/mcp/",
        tool_filter=["guidebook/*"],
    )]
)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="What AI tools can my child use?",
    config=genai.types.GenerateContentConfig(
        tools=[tools],
    ),
)</div>
  </details>
</div>

<div class="callout" style="margin-top:24px;">
  <strong>That's it!</strong> Once connected on any platform, just type a question in plain English like
  <em>"What does CPS say about my child using ChatGPT?"</em>
  &mdash; the AI will automatically look up the answer from the guidebook.
</div>

<!-- ============================== WHAT'S INSIDE ============================== -->
<div class="section-heading">
  <h2>What's Inside</h2>
  <p>The full guidebook, structured and searchable</p>
</div>

<div class="card">
  <h3>Guidebook Sections</h3>
  {sections_html}
</div>

<div class="card">
  <h3>CPS AI Principles</h3>
  <p style="margin-bottom:12px; color: var(--gray-500); font-size:14px">These five principles guide every decision CPS makes about AI in schools:</p>
  {principles_html}
</div>

<div class="card">
  <h3>GenAI Tool Age Restrictions</h3>
  <p style="margin-bottom:8px; color: var(--gray-500); font-size:14px">What CPS says about which tools your child can use, based on age:</p>
  <div style="overflow-x:auto">
    <table class="age-table">
      <thead><tr>
        <th>Tool</th>
        <th>No Access</th>
        <th>Parental Consent</th>
        <th>Free to Use</th>
      </tr></thead>
      <tbody>{age_rows}</tbody>
    </table>
  </div>
</div>

<!-- ============================== PARENT GUIDE ============================== -->
<div class="section-heading">
  <h2>A Guide for Parents</h2>
  <p>How to use this tool to help your student</p>
</div>

<div class="callout">
  <strong>How does this work?</strong> When you connect an AI assistant (like Claude or ChatGPT) to this server, it can read the CPS AI Guidebook data and answer your questions using official CPS guidance. You ask in plain English &mdash; the AI looks up the answer from the guidebook for you.
</div>

<p style="font-size:15px; color: var(--gray-700); margin-bottom: 20px;">Here are real questions you can ask. Each one will pull answers directly from the official CPS guidebook:</p>

<div class="grid-2">
  <div class="ask-card">
    <div class="prompt">"My 10-year-old says their teacher is using ChatGPT in class. Is that allowed?"</div>
    <div class="why">Pulls age restrictions and approved tools guidance. ChatGPT has no access under 13, so you'll learn exactly what the rules are.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"What are the approved AI tools my child can use for homework?"</div>
    <div class="why">Returns CPS's guidance on approved tools, the Ed Tech Catalog, and the requirement that students get teacher permission first.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"I don't want my kid using AI at school. Can I opt out?"</div>
    <div class="why">Pulls the opt-out guidance &mdash; yes, schools must provide opt-out procedures and communicate about GenAI tools used in classrooms.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"How should my child cite AI when they use it for a school paper?"</div>
    <div class="why">Returns academic integrity guidelines &mdash; students must cite GenAI use, specify how they used it, and submit fundamentally their own work.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"What does CPS say about AI bias? I'm worried about fairness."</div>
    <div class="why">Returns the bias and fairness guidelines plus CPS's Equity principle. Includes the Office of Equity contact for concerns.</div>
  </div>
  <div class="ask-card">
    <div class="prompt">"Show me examples of how AI is being used in my child's 5th grade class."</div>
    <div class="why">Pulls classroom examples for elementary school across Literacy, Math, Science, and Social Science &mdash; with before/after comparisons.</div>
  </div>
</div>

<div style="margin-top: 24px;">
  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>Your high schooler used ChatGPT for a history essay and got in trouble</h4>
    <p>You want to understand the rules so you can talk to the teacher and your child. Ask:</p>
    <div class="try">"What are the academic integrity rules about AI? What happens if a student doesn't cite it? What's the review process?"</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">The AI will pull the student guidance on academic integrity, the school review process (gathering information, contacting parents, determining consequences), and positive alternative uses.</p>
  </div>

  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>You want to help your middle schooler use AI responsibly at home</h4>
    <p>You're not against AI but want to set ground rules. Ask:</p>
    <div class="try">"What does CPS recommend as positive ways for students to use AI? Give me conversation starters for talking to my 7th grader about AI."</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">Returns approved use categories (collaboration, creativity, learning) with specific examples, plus conversation starters designed for parents.</p>
  </div>

  <div class="scenario">
    <span class="label">Scenario</span>
    <h4>You're not sure what "GenAI" or "hallucination" means</h4>
    <p>The school sent home a letter full of AI jargon. Ask:</p>
    <div class="try">"What does hallucination mean when talking about AI? And what's the difference between AI and generative AI?"</div>
    <p style="margin-top:8px; font-size:13px; color: var(--gray-500);">Pulls clear, CPS-approved definitions from the 33-term glossary. Written for a general audience, not technologists.</p>
  </div>
</div>

<!-- ============================== TOOLS REFERENCE ============================== -->
<div class="section-heading">
  <h2>Available Tools</h2>
  <p>What AI assistants can access through this server</p>
</div>

<div class="card">
  <div class="tool-item">
    <span class="tool-name">guidebook.get_parent_guidance</span>
    <span class="tool-tag">Start Here</span>
    <div class="tool-desc">All parent-specific guidance in one call: recommendations, age restrictions, opt-out info, conversation starters, and key contacts.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_age_restrictions</span>
    <div class="tool-desc">Age restriction table for ChatGPT, Claude, Gemini, Copilot, and Perplexity &mdash; who can use what, and when parental consent is needed.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_classroom_examples</span>
    <div class="tool-desc">Side-by-side "without AI" vs "with AI" examples across Literacy, Math, Science, and Social Science for Elementary, Middle, and High School.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_positive_uses</span>
    <div class="tool-desc">CPS-approved ways students can use GenAI: brainstorming, study guides, creative projects, research, and more.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_ai_principles</span>
    <div class="tool-desc">CPS's five AI principles: Equitable, Ethical, Human-Centered, Innovative, and Accountable.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_glossary_term</span>
    <div class="tool-desc">Look up any AI term &mdash; hallucination, LLM, prompt, bias, and 29 more. Written in plain language.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_stakeholder_guidance</span>
    <div class="tool-desc">Get guidance for any role: parents, students, educators, administrators, ITS, or vendors.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.search</span>
    <div class="tool-desc">Full-text search across every section, topic, recommendation, and glossary term in the guidebook.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.list_sections</span>
    <div class="tool-desc">Browse all six major sections of the guidebook.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.list_topics</span>
    <div class="tool-desc">Drill into topics within any section.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_topic</span>
    <div class="tool-desc">Get the full content of any topic, including all subtopics, recommendations, and special data.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.list_glossary</span>
    <div class="tool-desc">List all {glossary_count} terms in the CPS AI glossary.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_version_info</span>
    <div class="tool-desc">Document version, publisher, release date, and CPS mission statement.</div>
  </div>
  <div class="tool-item">
    <span class="tool-name">guidebook.get_usage_guide</span>
    <div class="tool-desc">Navigation help for AI assistants &mdash; explains the structure and recommended workflows.</div>
  </div>
</div>

<div class="card">
  <h3>Data Source &amp; Attribution</h3>
  <p>All data served by this MCP comes directly from the <strong>Chicago Public Schools AI Guidebook, Version {version}</strong>, last updated August 2025.</p>
  <p style="margin-top:8px; font-size:14px; color: var(--gray-500)">
    The guidebook is published by Chicago Public Schools and issued by the Office of Teaching and Learning and the Department of Information and Technology Services.
    Every tool response includes an attribution line linking back to the source document.
  </p>
  <p style="margin-top:8px; font-size:14px">
    For the full original document, visit <a href="https://cps.edu/aiguidebook" style="color: var(--blue)">cps.edu/aiguidebook</a>.
  </p>
</div>

</div><!-- /container -->

<div class="footer">
  <p>
    CPS AI Guidebook MCP &middot; Version {version} &middot;
    Data source: <a href="https://cps.edu/aiguidebook">Chicago Public Schools AI Guidebook</a>
  </p>
  <p style="margin-top:8px">
    Built with the <a href="https://modelcontextprotocol.io">Model Context Protocol</a>
  </p>
</div>

</body>
</html>"""
    return HTMLResponse(page)


# MCP mount
mcp_http_app = mcp.streamable_http_app()
app.mount("/mcp", mcp_http_app)


# Analytics dashboard (embedded — no separate container needed)
try:
    from starlette.middleware.wsgi import WSGIMiddleware
    from dashboard.app import app as dash_app
    app.mount("/reporting", WSGIMiddleware(dash_app.server))
except Exception:
    pass  # dash/plotly not installed — dashboard disabled


# Health / debug endpoints
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/debug/ping")
async def debug_ping():
    return {"server_py_loaded": True}

@app.get("/debug/tools")
async def debug_tools():
    for attr in ("_tool_manager", "tool_manager", "_tools", "tools"):
        if hasattr(mcp, attr):
            val = getattr(mcp, attr)
            return {"attr": attr, "value": str(val)}
    return {"msg": "No obvious tool manager attrs found on mcp", "mcp_type": str(type(mcp))}

@app.get("/debug/tools2")
async def debug_tools2():
    if hasattr(mcp, "list_tools"):
        tools = await mcp.list_tools()
        out = []
        for t in tools:
            if hasattr(t, "model_dump"):
                out.append(t.model_dump())
            elif hasattr(t, "dict"):
                out.append(t.dict())
            else:
                out.append(str(t))
        return {"tools": out}
    return {"msg": "mcp.list_tools() not available"}
