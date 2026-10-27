# My Training Agent

An AI-powered training assistant that connects to your Strava data via the Strava MCP server, secured by **Okta for AI Agents (O4AA)** brokered consent.

## Architecture

```
Browser ──OIDC──► Okta (Web App)
                        │
                        │ id_token
                        ▼
FastAPI ──RFC 8693 STS──► Okta Token Exchange
  │         (APP_INSTANCE)        │
  │                               │ Strava access token
  │                               ▼
  └──────────────────────► Strava MCP Server
                           (mcp.strava.com/mcp)
                                  │
                             MCP tools
                                  ▼
                          Claude claude-opus-4-7
                          (agentic loop, SSE stream)
```

### Key concepts

| Component | Role |
|-----------|------|
| Okta OIDC Web App | Authenticates the user; issues an `id_token` |
| Okta AI Agent (Directory → AI Agents) | The agent's machine identity; linked to the Web App with the same Client ID and client secret auth |
| Okta APP_INSTANCE connection | Brokers consent between the agent and Strava; resource indicator is an ORN |
| RFC 8693 Token Exchange | Trades the user `id_token` for a Strava `access_token` |
| Strava MCP Server | Exposes Strava data as MCP tools |
| FastAPI + SSE | Streams Claude's agentic responses to the browser |

---

## Okta Setup

> **Prerequisite:** The `SECURE_AI_OAUTH_STS` feature flag must be enabled on your Okta org. Contact Okta support if needed.

### Step 1 — AI Agent and linked Web App

The AI Agent object and its OIDC Web App definition are linked: they share one Client ID and must use the same authentication method (client secret). The app uses this single credential for user login, the STS token exchange and token revocation.

1. Okta Admin → **Directory → AI Agents → Create AI Agent** and name it e.g. "My Training Agent"
2. Configure the linked Web App with **Sign-in redirect URI** `http://localhost:8000/auth/callback` and **Sign-out redirect URI** `http://localhost:8000/`
3. Use **Client secret** as the authentication method
4. Note the **Client ID** → `OKTA_CLIENT_ID` and **Client Secret** → `OKTA_CLIENT_SECRET`

### Step 2 — Configure the Strava Resource Server Connector

1. Okta Admin → **Applications → Applications**
2. Find the Strava app integration (search the App Integration Catalog with the **Brokered Consent** filter if not already added)
3. Open the app and go to the **Resource server** tab
4. Enter the **Client ID** and **Client secret** for your Strava API application
5. Click **Add** to add each required scope. For a read-only training assistant, add:
   - `read` — public segments, routes, profile, posts, events, leaderboards
   - `read_all` — private routes, private segments, private events
   - `profile:read_all` — full profile regardless of visibility setting
   - `activity:read_all` — all activities including privacy zones and private-visibility activities
6. Click **Save**

### Step 3 — Okta APP_INSTANCE Managed Connection

1. Okta Admin → **AI → Managed Connections**
2. Create a new connection:
   - **Connection type**: `APP_INSTANCE`
   - **Resource**: link to the Strava app's resource server connector from Step 2
   - **Associated AI Agent**: the AI Agent from Step 1
3. Copy the **resource indicator ORN** (format: `orn:okta:idp:{orgId}:client_auth_settings:{rscId}`) → `OKTA_STRAVA_RESOURCE_INDICATOR`

### Step 4 — User consent (first use)

On the first request, the STS exchange returns `interaction_required`. The UI displays a consent link — the user clicks it, approves Strava access in Okta, then retries. Subsequent requests use the cached token.

---

## Local Development

```bash
# 1. Clone and install
git clone https://github.com/<you>/stsDemo-Strava
cd stsDemo-Strava
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — fill in all values

# 3. Run
uvicorn backend.main:app --reload

# 4. Open
open http://localhost:8000
```

---

## Render Deployment

1. Push to GitHub (see `render.yaml` in repo root)
2. Render Dashboard → **New → Web Service → Connect repo**
3. Render auto-detects `render.yaml`; set all `sync: false` env vars in the Render dashboard
4. Update `OKTA_REDIRECT_URI` and `OKTA_POST_LOGOUT_REDIRECT_URI` to your Render URL
5. Add the Render redirect URI to your Okta Web App's allowed redirect URIs

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API key |
| `SESSION_SECRET` | ✓ | Random hex string for session signing |
| `OKTA_CLIENT_ID` | ✓ | AI Agent / linked Web App client ID (used for login and STS) |
| `OKTA_CLIENT_SECRET` | ✓ | Client secret for the same app |
| `OKTA_DOMAIN` | ✓ | e.g. `your-org.okta.com` |
| `OKTA_REDIRECT_URI` | ✓ | e.g. `http://localhost:8000/auth/callback` |
| `OKTA_POST_LOGOUT_REDIRECT_URI` | | Post-logout redirect (optional) |
| `OKTA_STRAVA_RESOURCE_INDICATOR` | ✓ | ORN for the APP_INSTANCE Strava connection |

---

## Token Exchange Flow

```
POST /oauth2/default/v1/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<user id_token>
subject_token_type=urn:ietf:params:oauth:token-type:id_token
requested_token_type=urn:okta:params:oauth:token-type:oauth-sts
resource=orn:okta:idp:{orgId}:client_auth_settings:{rscId}
client_id=<OKTA_CLIENT_ID>
client_secret=<OKTA_CLIENT_SECRET>
```

Success → `200 { "access_token": "...", "expires_in": 3600 }`  
First use → `400 { "error": "interaction_required", "interaction_uri": "https://..." }`
