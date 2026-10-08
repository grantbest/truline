import { PageHeader } from "@/components/PageHeader";

// The Reports view is served by the finance-reporting microservice (Plotly
// Dash), not the SPA. nginx proxies /reports/ to that service; here we embed
// it in an iframe so the console chrome (nav) stays in place.
//
// Why an iframe instead of a NavLink straight to /reports/:
//   - keeps the user inside the console shell;
//   - Dash owns its own routing/asset graph under the /reports/ prefix
//     (configured via requests_pathname_prefix in app.py), which is awkward
//     to merge into the Vite SPA bundle.
//
// The SPA route is "/reports" (no trailing slash) and the iframe loads
// "/reports/" (trailing slash) so nginx's `location /reports/` matches and
// proxies to Dash rather than falling through to the SPA index.html.
const DASH_SRC = "/reports/";

export function ReportsRoute() {
  return (
    <div className="flex h-full min-h-screen flex-col">
      <PageHeader
        title="Reports"
        subtitle="Spend Pulse & category trends · served by finance-reporting (Dash)"
      />
      <iframe
        src={DASH_SRC}
        title="Finance Reporting"
        className="flex-1 w-full border-0"
      />
    </div>
  );
}
