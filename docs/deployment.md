# Deployment

## The constraint that decides the architecture

**The backend cannot run on Vercel or Netlify.** Both are serverless. This app
is not.

| What the app does | Why serverless breaks it |
|---|---|
| SSE event stream (`/api/runs/{id}/stream`) | A function returns and the connection dies. Vercel Hobby caps at 10s, Netlify at 10s. |
| Runs execute in `asyncio.create_task` | The process is frozen or reclaimed once the response is sent, so the run never finishes. |
| Council compile = 4+ sequential model calls | Minutes. Two orders of magnitude past the limit. |
| torch + sentence-transformers | ~1 GB installed. Vercel's bundle limit is 250 MB unzipped. |
| Ollama local models | Needs several GB resident on a host that stays up. |

So there are three supported shapes.

---

## Option C — Render, no Docker (what is actually deployed)

Two free Render services, no Dockerfile involved: a native Python web service
for the backend, a static site for the built frontend. `render.yaml` at the
repo root defines both - connect the repo in the Render dashboard, "New +
Blueprint", and it provisions them from that file.

- **Backend** (`aco-backend`): `runtime: python`, `pip install -r
  requirements.txt`, `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`.
  A real persistent process, not a serverless function, so the SSE stream
  holds open for as long as an answer takes. Free tier sleeps after 15 minutes
  idle, ~1 minute cold start on the next request.
- **Frontend** (`aco-frontend`): `runtime: static`, `npm ci && npm run build`
  in `frontend/`, published from `frontend/dist`. Free, no sleep, served from
  Render's CDN. `VITE_API_BASE` points it at the backend service's URL - the
  same split-hosting mechanism Option B below uses.

Secrets go in the Render dashboard per service, never in `render.yaml`
(`sync: false` there means "prompt me, don't commit it"):
`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `SECRET_KEY`, and `OPENROUTER_API_KEY`
(or `GROQ_API_KEY` / `GEMINI_API_KEY` as a fallback - see
`backend/api/routes.py::_default_models`).

---

## Option A — single origin (simplest, and what the demo uses)

FastAPI serves the built frontend and the API from one process on port 7860.
Nothing to configure: `VITE_API_BASE` stays blank and the frontend calls
relative `/api/...` paths.

```bash
cd frontend && npm run build && cd ..
uvicorn backend.main:app --host 0.0.0.0 --port 7860
```

Verified locally:

```bash
curl localhost:7860/health      # {"status":"ok","version":"0.1.0"}
```

---

## Option B — split hosting (frontend on Netlify)

Use this if you specifically want the UI on a static host.

**Frontend** — `netlify.toml` is already in `frontend/` (`base` is already set
to `frontend`).

- Set the build env var:

```
VITE_API_BASE=https://your-backend-host.example.com
```

It is baked in at build time, so changing it requires a redeploy.

**Backend** — deploy Option A somewhere that keeps a process alive: Render,
Railway or Fly.io.

**Then allow the frontend's origin**, or every request fails CORS:

```
CORS_ORIGINS=https://your-app.netlify.app
```

The allow-list is explicit by design — `CORS_ORIGINS=*` raises at startup.

### The one thing to test after splitting

SSE across origins. `EventSource` sends no custom headers, so it should work,
but confirm the trace viewer actually streams before you rely on it live. If it
does not, the run still completes — the log is in Postgres, and
`GET /api/runs/{id}/events` returns it in full.

---

## Environment

| Variable | Where | Notes |
|---|---|---|
| `TURSO_DATABASE_URL` | backend | Required |
| `TURSO_AUTH_TOKEN` | backend | **Secret.** Server-side only, never in the bundle |
| `SECRET_KEY` | backend | Prod refuses to boot without it |
| `OPENROUTER_API_KEY` | backend | **Secret.** Preferred - one key, three model families, $0. No local-model fallback in prod |
| `GROQ_API_KEY` | backend | **Secret.** Fallback if `OPENROUTER_API_KEY` is unset |
| `GEMINI_API_KEY` | backend | **Secret.** Fallback if `OPENROUTER_API_KEY` is unset |
| `CORS_ORIGINS` | backend | Explicit list; `*` is rejected |
| `APP_ENV=prod` | backend | Disables `/docs` |
| `VITE_API_BASE` | frontend | Blank for single-origin; full URL for split |

Rotate `TURSO_AUTH_TOKEN` and `SECRET_KEY` before any public deployment.

---

## Pre-deploy checklist

```bash
pytest -m "not slow"                      # 60 passed
ruff check .
pip-audit -r requirements.txt --strict    # no known vulnerabilities
cd frontend && npm run build              # tsc clean
python -m backend.demo                    # PASS, replay diff zero
git ls-files | grep -x ".env"             # must return nothing
```
