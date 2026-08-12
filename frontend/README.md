# Enterprise Data and Search Agent Frontend

React 18, TypeScript, and Vite frontend for the FastAPI agent backend.

## Features

- Server-Sent Events chat streaming from `POST /chat/stream`
- Per-session messages, in-flight state, stop controls, and Ontology setting
- Collapsible agent/activity panel for narration, Skills, tools, SQL, and errors
- Markdown and GitHub-flavored tables
- Citation normalization with linked and plain-text references
- Workspace Business Layer document editor
- Responsive dark interface

## Local Development

Prerequisites: Node.js 18.18+, npm, and the backend on port 8000.

```bash
npm install
npm run dev
```

The UI is available at `http://localhost:3000`. Vite proxies `/api/*` to
`http://localhost:8000/*`, so no frontend environment file is required locally.

From the repository root, `./run.sh` starts both backend and frontend.

## Production Build

```bash
npm ci
npm run build
```

The output is written to `dist/`.

The frontend API base is controlled by `VITE_API_BASE_URL` and defaults to `/api`:

```bash
VITE_API_BASE_URL=https://api.example.com npm run build
```

Prefer `/api` plus a same-origin reverse proxy in production. If the browser calls a
different origin directly, configure the backend CORS allowlist for that origin.

## Main Source Files

```text
src/
├── App.tsx                    # sessions, SSE parsing, chat, business-layer editor
├── components/ActivityPanel.tsx
├── services/api.ts           # REST API client and API base configuration
├── styles/                    # application styles
└── types/                     # chat, runtime, and activity contracts
```

Example questions are maintained in `src/App.tsx` and cover both the enterprise
knowledge index and Databricks analytics.

## API Usage

- Streaming chat uses SSE from `POST /api/chat/stream`.
- Session, configuration, Skill, stop, health, and Business Layer operations use REST.
- There is no WebSocket endpoint.

## Validation

```bash
npm run build
npm run lint
```
