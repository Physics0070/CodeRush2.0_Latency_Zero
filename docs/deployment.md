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

So there are two supported shapes.

---

## Option A — single origin (simplest, and what the demo uses)

FastAPI serves the built frontend and the API from one process on port 7860.
Nothing to configure: `VITE_API_BASE` stays blank and the frontend calls
relative `/api/...` paths.

```bash
cd frontend && npm run build && cd ..
uvicorn backend.main:app --host 0.0.0.0 --port 7860
```

This is exactly what the Docker image does, and what HF Spaces runs.

Verified locally:

```bash
docker build -t aco .
docker run -d --name aco-test -p 7861:7860 --env-file .env \
  --add-host=host.docker.internal:host-gateway \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 aco:latest
curl localhost:7861/health      # {"status":"ok","version":"0.1.0"}
```

`--add-host` is what lets the container reach an Ollama running on the host.
Without it `ollama:*` models are unreachable from inside the container and only
hosted providers answer — which is also the situation on HF Spaces.

### Hugging Face Spaces

1. New Space → SDK **Docker** → hardware **CPU basic (free)**
2. Settings → *Variables and secrets*. Add as **secrets**, not variables:
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SECRET_KEY`, and `GROQ_API_KEY`
3. `git remote add space https://huggingface.co/spaces/<user>/<space>`
4. `git push space main`
5. Watch the build to completion, then `curl https://<user>-<space>.hf.space/health`

**Ollama is not available on a Space.** There is no way to run `ollama serve`
plus multi-GB model files on the free tier. Set `GROQ_API_KEY` so the deployed
instance has a working provider — the model string is per agent, so nothing else
changes. This is the same swap as demo step 8.

Known issues:

- **Cold start.** Hit the URL five minutes before demoing.
- **Semantic metrics are opt-in.** `torch` and `sentence-transformers` live in
  `requirements-embeddings.txt`, not `requirements.txt`. They are ~1GB and are
  served from `download.pytorch.org`, which stalls or refuses connections on
  some networks — measured here as zero bytes in 120s. The CPU index also
  carries no cp311 linux wheel above 2.6.0, so an exact pin that resolves on
  Windows is unsatisfiable inside the image.

  The default build omits them and finishes in about two minutes:

  ```bash
  docker build -t aco .                                # 863 MB, ~2 min
  docker build --build-arg WITH_EMBEDDINGS=1 -t aco .  # adds MIG + duplicates
  ```

  Without them the app is fully functional. The metrics response reports
  `embeddings_available: false` and omits marginal information gain and
  duplicate pairs rather than guessing them. Chat, council, replay, permissions
  and every timing and cost number are unaffected.

---

## Option B — split hosting (frontend on Vercel/Netlify)

Use this if you specifically want the UI on a static host.

**Frontend** — `vercel.json` and `netlify.toml` are already in `frontend/`.

- Vercel: import the repo, set **Root Directory** to `frontend`
- Netlify: `base` is already set to `frontend` in `netlify.toml`
- Set the build env var:

```
VITE_API_BASE=https://<user>-<space>.hf.space
```

It is baked in at build time, so changing it requires a redeploy.

**Backend** — deploy Option A somewhere that keeps a process alive: HF Spaces
Docker, Render, Railway or Fly.io.

**Then allow the frontend's origin**, or every request fails CORS:

```
CORS_ORIGINS=https://your-app.vercel.app,https://your-app.netlify.app
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
| `SUPABASE_URL` | backend | Required |
| `SUPABASE_SERVICE_KEY` | backend | **Secret.** Server-side only, never in the bundle |
| `SECRET_KEY` | backend | Prod refuses to boot without it |
| `GROQ_API_KEY` | backend | **Secret.** Needed on any host without Ollama |
| `CORS_ORIGINS` | backend | Explicit list; `*` is rejected |
| `APP_ENV=prod` | backend | Disables `/docs` |
| `VITE_API_BASE` | frontend | Blank for single-origin; full URL for split |

Rotate `SUPABASE_SERVICE_KEY` and `SECRET_KEY` before any public deployment.

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
