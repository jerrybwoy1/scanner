# QikReach

QikReach is a browser-based public-source search and contact-enrichment application.

## Architecture

- Cloudflare Worker `scanner` serves `public/index.html` and proxies `/api/*`.
- FastAPI runs on Render at `https://qikreach-python-backend.onrender.com`.
- The browser calls only the Worker routes.
- There is no local vault or D1 persistence in this version.

The public API contract is:

- `GET /api/health`
- `POST /api/search`
- `POST /api/search/stream`
- `POST /api/batch`

## Search providers

Discovery uses the first available provider and falls back automatically:

1. Gemini 2.5 Flash with Google Search grounding when `GEMINI_API_KEY` is configured.
2. Groq Compound web search when `GROQ_API_KEY` is configured.
3. Tavily when `TAVILY_API_KEY` is configured.
4. DDGS Brave and DuckDuckGo backends as best-effort, keyless fallbacks.

Configure provider keys only in the Render environment. Never expose them in the Worker or browser source.

Required Render environment variable:

```text
APP_ORIGIN=https://scanner.jerrylang.workers.dev
```

Recommended free provider variable:

```text
GEMINI_API_KEY=your-server-side-key
```

Optional fallback:

```text
GROQ_API_KEY=your-server-side-key
TAVILY_API_KEY=your-server-side-key
```

## Local verification

```bash
npm install
npx wrangler dev
```

Run the backend separately from `backend/`:

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

## Deployment

Render deploys the backend from `main` using `render.yaml`. Deploy the existing Worker with:

```bash
npx wrangler deploy --dry-run
npm run deploy
```
