import { Refine } from "@refinedev/core";
import routerProvider from "@refinedev/react-router-v6";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { substrateDataProvider } from "@/providers/data-provider";
import { Layout } from "@/components/Layout";
import { TodayRoute } from "@/routes/Today";
import { InboxRoute } from "@/routes/Inbox";
import { BeadExplorerRoute } from "@/routes/BeadExplorer";
import { WealthDashboardRoute } from "@/routes/WealthDashboard";
import { BillLedgerRoute } from "@/routes/BillLedger";
import { SubscriptionsRoute } from "@/routes/Subscriptions";
import { BudgetRoute } from "@/routes/Budget";
import { ScenariosRoute } from "@/routes/Scenarios";
import { RuleSandboxRoute } from "@/routes/RuleSandbox";
import { TransactionLedgerRoute } from "@/routes/TransactionLedger";
import { TransferLineageRoute } from "@/routes/TransferLineage";
import { LedgerAuditRoute } from "@/routes/LedgerAudit";
import { AnomalyInboxRoute } from "@/routes/AnomalyInbox";
import { MerchantRiskRoute } from "@/routes/MerchantRisk";
import { MerchantDetailRoute } from "@/routes/MerchantDetail";
import { ReportsRoute } from "@/routes/Reports";
import { VisionInboxRoute } from "@/routes/VisionInbox";
import { ConnectionsRoute } from "@/routes/Connections";
import { InstitutionHealthRoute } from "@/routes/InstitutionHealth";
import { FactoryBoardRoute } from "@/routes/FactoryBoard";
import { ArchitectureRoute } from "@/routes/Architecture";
import { ProjectAlphaRoute } from "@/routes/ProjectAlpha";
import { ReleaseIndexRoute } from "@/routes/ReleaseIndex";
import { ReleaseViewRoute } from "@/routes/ReleaseView";

// The route tree, factored out of App() so App.test.tsx can build real
// RouteObjects from it (via react-router-dom's createRoutesFromElements)
// and check what a path actually resolves to — the gap this bead exists to
// close was exactly a route a nav entry could not reach, and neither half
// alone would have caught it: a route with no nav entry silently
// unreachable, or a nav entry with no matching route silently swallowed by
// the "*" fallback below.
export const appRoutes = (
  <Route element={<Layout />}>
    <Route index element={<Navigate to="/today" replace />} />
    <Route path="/today" element={<TodayRoute />} />
    <Route path="/inbox" element={<InboxRoute />} />
    <Route path="/beads" element={<BeadExplorerRoute />} />
    <Route path="/wealth" element={<WealthDashboardRoute />} />
    <Route path="/bills" element={<BillLedgerRoute />} />
    <Route path="/subscriptions" element={<SubscriptionsRoute />} />
    <Route path="/budget" element={<BudgetRoute />} />
    <Route path="/scenarios" element={<ScenariosRoute />} />
    <Route path="/rules" element={<RuleSandboxRoute />} />
    <Route path="/ledger" element={<TransactionLedgerRoute />} />
    <Route path="/transfers" element={<TransferLineageRoute />} />
    <Route path="/audit" element={<LedgerAuditRoute />} />
    <Route path="/anomalies" element={<AnomalyInboxRoute />} />
    <Route path="/risk" element={<MerchantRiskRoute />} />
    <Route path="/merchant/:name" element={<MerchantDetailRoute />} />
    <Route path="/reports" element={<ReportsRoute />} />
    <Route path="/vision" element={<VisionInboxRoute />} />
    <Route path="/connect" element={<ConnectionsRoute />} />
    <Route path="/health" element={<InstitutionHealthRoute />} />
    <Route path="/alpha" element={<ProjectAlphaRoute />} />
    <Route path="/factory" element={<FactoryBoardRoute />} />
    <Route path="/releases" element={<ReleaseIndexRoute />} />
    <Route path="/releases/:ref" element={<ReleaseViewRoute />} />
    <Route path="/architecture" element={<ArchitectureRoute />} />
    <Route path="*" element={<Navigate to="/today" replace />} />
  </Route>
);

export function App() {
  return (
    <BrowserRouter>
      <Refine
        dataProvider={substrateDataProvider}
        routerProvider={routerProvider}
        resources={[
          { name: "today", list: "/today" },
          { name: "inbox", list: "/inbox" },
          { name: "beads", list: "/beads" },
          { name: "finance", list: "/wealth" },
          { name: "bills", list: "/bills" },
          { name: "subscriptions", list: "/subscriptions" },
          { name: "budget", list: "/budget" },
          { name: "scenarios", list: "/scenarios" },
          { name: "ledger", list: "/ledger" },
          { name: "transfers", list: "/transfers" },
          { name: "audit", list: "/audit" },
          { name: "anomalies", list: "/anomalies" },
          { name: "risk", list: "/risk" },
          { name: "reports", list: "/reports" },
          { name: "vision", list: "/vision" },
          { name: "connect", list: "/connect" },
          { name: "health", list: "/health" },
          { name: "alpha", list: "/alpha" },
          { name: "factory", list: "/factory" },
          { name: "releases", list: "/releases" },
        ]}
        options={{ disableTelemetry: true, syncWithLocation: false }}
      >
        <Routes>{appRoutes}</Routes>
      </Refine>
    </BrowserRouter>
  );
}
