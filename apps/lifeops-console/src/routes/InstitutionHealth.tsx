import { PageHeader } from "@/components/PageHeader";
import { InstitutionHealthBoard } from "@/components/InstitutionHealthBoard";
import { institutionHealthIsUnavailable, useInstitutionHealth } from "@/hooks/useInstitutionHealth";

// Track A / A1, exposure half. Renders what S24-P1's mcp-hub API (predecessor,
// measurement half) reports: per-institution freshness health, the learned
// SLO, and the newest-transaction date — never rolled up into one verdict.
export function InstitutionHealthRoute() {
  const query = useInstitutionHealth();
  const isUnavailable = institutionHealthIsUnavailable(query);

  return (
    <>
      <PageHeader
        title="Institution Health"
        subtitle="Whether money data is arriving, per institution — not whether the provider is reachable"
      />
      <div className="p-4 space-y-6">
        <InstitutionHealthBoard
          institutions={query.data?.institutions}
          isLoading={query.isLoading}
          isUnavailable={isUnavailable}
        />
      </div>
    </>
  );
}
