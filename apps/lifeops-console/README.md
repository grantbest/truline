# lifeops-console

Internal "financial-terminal" UI on top of Substrate. Three views:

- **Bead Explorer** — unified ledger across all namespaces, filters, semantic search, JSON inspector, event log.
- **Wealth Dashboard** — net worth, accounts, liabilities (APR / principal / next payment), top spend by merchant.
- **Vision Inbox** — approve/reject pending `vision` extractions.

## Stack

Vite + React + TypeScript, Tailwind, Refine (headless), TanStack Query/Table. Production image: multi-stage Node + nginx.

## How auth works (important)

**The browser never holds the Substrate API key.** All Substrate calls flow through a same-origin path (`/substrate/*`) that a server-side proxy injects `X-API-Key` into:

```
browser  ──fetch /substrate/beads──▶  nginx (this container)
                                       │
                                       ▼  proxy_pass
                                     mcp-hub /substrate/{path}
                                       │
                                       ▼  injects X-API-Key
                                     substrate /beads
```

In production, Cloudflare Access on `console.example.org` authenticates the user; nginx forwards the authenticated session to mcp-hub; mcp-hub turns it into a Substrate call. The API key only ever lives in the mcp-hub pod's secret mount.

In dev, Vite's dev server replaces the nginx step — see [vite.config.ts](vite.config.ts).

## Setup (dev)

```bash
cd apps/lifeops-console
cp .env.example .env.local   # optional; defaults are fine for the standard flow
pnpm install

# In another shell, expose mcp-hub locally:
kubectl -n platform-mcp port-forward svc/mcp-hub 8000:8000

pnpm dev                     # http://localhost:5173
```

The Vite dev server proxies `/substrate/*` → `http://localhost:8000` (override with `VITE_DEV_MCP_HUB` if mcp-hub runs elsewhere).

Build:

```bash
pnpm build       # → dist/
pnpm preview     # serve dist/
pnpm typecheck
```

Production image:

```bash
docker build -t lifeops-console:latest .
```

## Environment

| Var | Required | Notes |
| --- | --- | --- |
| `VITE_SUBSTRATE_PROXY` | no | Defaults to `/substrate`. Override only if you need to point the build at a non-default proxy path (rare). |
| `VITE_DEV_MCP_HUB` | no | Dev only. Where the Vite dev server should proxy `/substrate/*` to. Defaults to `http://localhost:8000`. |

There is no `VITE_SUBSTRATE_API_KEY`. If you find yourself wanting one, the deploy is wrong — fix the proxy instead.

## Substrate API contract

The data provider in [src/providers/data-provider.ts](src/providers/data-provider.ts) maps Refine resources to the Substrate routes defined in [`apps/substrate/src/routes.py`](../substrate/src/routes.py), via the proxy:

| Refine call | Path on the proxy |
| --- | --- |
| `getList` | `GET /substrate/beads?namespace=&type=&state=&trust_tier=&limit=&offset=` |
| `getOne` | `GET /substrate/beads/{id}` |
| `create` | `POST /substrate/beads` |
| `update` | `PATCH /substrate/beads/{id}` |
| `deleteOne` | `DELETE /substrate/beads/{id}` |
| `custom search` | `POST /substrate/beads/search` (vector) |
| `custom events/{id}` | `GET /substrate/beads/{id}/events` |

Substrate doesn't return a row count, so pagination requests `limit+1` and uses the extra row to detect `hasMore`. Don't rely on the synthetic `total` Refine reports.

## Vision Inbox producer contract

The Inbox shows beads where `namespace="vision"` and `state="pending"`. Approving transitions state to `approved`; rejecting transitions to `rejected`. Downstream materialization (creating `personal.todo`, `personal.event`, or `finance.expense` beads from the extraction payload) is the producer's responsibility — the Console only owns the human-in-the-loop transition.

Expected `content` shape (matches `apps/mcp-hub/src/tools/vision.py::VisionExtraction`):

```jsonc
{
  "summary": "string",
  "todos": ["string", ...],
  "events": [{ "title": "string", "date": "ISO date" }],
  "expenses": [{ "amount": 0, "vendor": "string", "date": "ISO date" }]
}
```

`mcp-hub`'s `/vision/extract` endpoint persists these beads automatically; the materialization workflow design is documented at the bottom of [`apps/mcp-hub/src/tools/vision.py`](../mcp-hub/src/tools/vision.py).

## Deploy (Cloudflare Access)

- `console.example.org` → Cloudflare Tunnel → traefik → this container (nginx) on port 80.
- Cloudflare Access (Google SSO) gates the host — see [infrastructure/terraform/locals.tf](../../infrastructure/terraform/locals.tf).
- nginx serves `dist/` for the SPA and proxies `/substrate/*` to mcp-hub via cluster DNS.
