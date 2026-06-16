"""FastAPI application: serves the training agent UI and SSE /api/chat endpoint."""

import base64
import json
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend.agent import run_agent
from backend.auth.okta_sts import clear_cached_token
from backend.config import get_settings

_settings = get_settings()

app = FastAPI(title="My Training Agent")

# In-memory conversation history keyed by user sub.
# Each value is a flat list of {"role": "user"|"assistant", "content": str} dicts.
_history: dict[str, list[dict[str, Any]]] = {}
MAX_HISTORY_TURNS = 20  # keep last N user+assistant pairs

_session_key = _settings.session_secret or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_session_key, https_only=False)

STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    message: str


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification (token arrived directly from Okta)."""
    try:
        part = token.split(".")[1]
        pad = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui(request: Request):
    if not request.session.get("id_token"):
        return RedirectResponse("/auth/signin")
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/auth/signin", response_class=HTMLResponse)
async def serve_signin():
    return HTMLResponse((STATIC_DIR / "signin.html").read_text(encoding="utf-8"))


# ── Auth flow ─────────────────────────────────────────────────────────────────

@app.get("/auth/start")
async def auth_start(request: Request):
    s = get_settings()
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    params = urllib.parse.urlencode({
        "client_id":     s.okta_client_id,
        "response_type": "code",
        "scope":         "openid email profile",
        "redirect_uri":  s.okta_redirect_uri,
        "state":         state,
    })
    return RedirectResponse(f"{s.okta_issuer}/v1/authorize?{params}")


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
):
    if error:
        msg = urllib.parse.quote(error_description or error)
        return RedirectResponse(f"/auth/signin?error={msg}")

    saved_state = request.session.pop("oauth_state", None)
    if not state or state != saved_state:
        return RedirectResponse("/auth/signin?error=State+mismatch+%E2%80%94+please+try+again")

    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{s.okta_issuer}/v1/token",
            data={
                "grant_type":    "authorization_code",
                "client_id":     s.okta_client_id,
                "client_secret": s.okta_client_secret,
                "redirect_uri":  s.okta_redirect_uri,
                "code":          code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if not resp.is_success:
        msg = urllib.parse.quote(resp.text[:300])
        return RedirectResponse(f"/auth/signin?error={msg}")

    tokens = resp.json()
    id_token = tokens.get("id_token")
    if not id_token:
        return RedirectResponse("/auth/signin?error=No+id_token+in+Okta+response")

    payload = _decode_jwt_payload(id_token)
    request.session["id_token"] = id_token
    request.session["user"] = {
        "email": payload.get("email", ""),
        "name":  payload.get("name") or payload.get("preferred_username") or payload.get("email", ""),
        "sub":   payload.get("sub", ""),
    }
    return RedirectResponse("/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    sub = (request.session.get("user") or {}).get("sub")
    id_token = request.session.get("id_token", "")
    if sub:
        _history.pop(sub, None)
        clear_cached_token(sub)
    request.session.clear()

    s = get_settings()
    if id_token and s.okta_end_session_url and s.okta_post_logout_redirect_uri:
        params = urllib.parse.urlencode({
            "id_token_hint":            id_token,
            "post_logout_redirect_uri": s.okta_post_logout_redirect_uri,
        })
        return RedirectResponse(f"{s.okta_end_session_url}?{params}")

    return RedirectResponse("/auth/signin")


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def api_me(request: Request):
    user = request.session.get("user")
    if not user:
        return JSONResponse(
            {"authenticated": False, "email": "", "name": "", "sub": ""},
            status_code=401,
        )
    id_token = request.session.get("id_token", "")
    id_token_claims: dict = {}
    if id_token:
        raw = _decode_jwt_payload(id_token)
        id_token_claims = {
            "iss": raw.get("iss", ""),
            "aud": raw.get("aud", ""),
            "sub": raw.get("sub", ""),
            "iat": raw.get("iat"),
            "exp": raw.get("exp"),
            "ver": raw.get("ver", ""),
        }
    return JSONResponse({"authenticated": True, **user, "id_token_claims": id_token_claims})


@app.post("/api/chat")
async def chat(request: Request, body: ChatRequest):
    user_id_token = request.session.get("id_token")
    if not user_id_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    sub = (request.session.get("user") or {}).get("sub") or "anonymous"
    history = list(_history.get(sub, []))

    async def event_stream():
        text_chunks: list[str] = []
        async for chunk in run_agent(
            body.message,
            user_id_token=user_id_token,
            cache_key=sub,
            history=history,
        ):
            # Accumulate assistant text to update history after the turn completes.
            if chunk.startswith("event: text\n"):
                try:
                    data_line = next(l for l in chunk.split("\n") if l.startswith("data: "))
                    text_chunks.append(json.loads(data_line[6:]).get("text", ""))
                except Exception:
                    pass
            yield chunk

        assistant_text = "".join(text_chunks)
        if assistant_text:
            new_history = history + [
                {"role": "user", "content": body.message},
                {"role": "assistant", "content": assistant_text},
            ]
            # Trim to MAX_HISTORY_TURNS pairs (2 messages each).
            if len(new_history) > MAX_HISTORY_TURNS * 2:
                new_history = new_history[-(MAX_HISTORY_TURNS * 2):]
            _history[sub] = new_history

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/clear")
async def chat_clear(request: Request):
    """Clear conversation history for the current user."""
    sub = (request.session.get("user") or {}).get("sub")
    if sub:
        _history.pop(sub, None)
    return JSONResponse({"ok": True})
