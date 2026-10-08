// The Console never holds the Substrate API key. All Substrate calls go
// through a same-origin path (default "/substrate") that:
//   - in prod, is proxied by the Console's nginx to mcp-hub, which injects
//     X-API-Key from its own secret;
//   - in dev, is proxied by Vite to the mcp-hub dev URL (see vite.config.ts
//     for the dev proxy block).
//
// VITE_SUBSTRATE_PROXY overrides only the path prefix. Leave it unset for
// the standard same-origin deploy.
const proxy = import.meta.env.VITE_SUBSTRATE_PROXY ?? "/substrate";

export const SUBSTRATE_PROXY: string = proxy.replace(/\/$/, "");
