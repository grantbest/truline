import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { cn } from "@/lib/cn";
import { SUBSTRATE_PROXY } from "@/lib/env";
import { useAttentionItems } from "@/hooks/useAttentionItems";

// Exported so App.test.tsx can assert every `to` here resolves to a real
// route (see App.tsx's `appRoutes`), and so the same assertion covers both
// mobile surfaces this array drives: the desktop sidebar below AND the
// mobile "More" overlay drawer (the fullscreen nav rendered near the bottom
// of this file) both map over `navGroups` directly. The separate
// `primaryMobileNav` bottom-bar array is a curated top-4 shortlist and is
// NOT touched by this — a phone user reaches a new nav entry via the
// overlay, not the bottom bar, unless it is deliberately promoted into
// primaryMobileNav too.
export const navGroups = [
  {
    title: "Triage",
    items: [
      { to: "/today", label: "Today", hint: "command center" },
      { to: "/inbox", label: "Inbox", hint: "triage all insights" },
      { to: "/anomalies", label: "Anomaly Inbox", hint: "leaks & trials" },
      { to: "/vision", label: "Vision Inbox", hint: "extracted items" },
    ],
  },
  {
    title: "Finances",
    items: [
      { to: "/wealth", label: "Wealth", hint: "net worth, liabilities" },
      { to: "/ledger", label: "Ledger", hint: "transactions" },
      { to: "/bills", label: "Bills", hint: "predictive cash flow" },
      { to: "/subscriptions", label: "Subscriptions", hint: "recurring & price hikes" },
      { to: "/budget", label: "Budget", hint: "run rate & category budgets" },
    ],
  },
  {
    title: "Tools & Rules",
    items: [
      { to: "/scenarios", label: "Scenarios", hint: "cash-flow what-if" },
      { to: "/rules", label: "Rule Sandbox", hint: "auto-categorization" },
      { to: "/transfers", label: "Transfers", hint: "pair internal moves" },
      { to: "/audit", label: "Ledger Audit", hint: "balance drift" },
      { to: "/risk", label: "Merchant Risk", hint: "funding dependencies" },
      { to: "/reports", label: "Reports", hint: "spend pulse, trends" },
    ],
  },
  {
    title: "System",
    items: [
      { to: "/alpha", label: "Alpha", hint: "SDLC/ITSM graph" },
      { to: "/releases", label: "Releases", hint: "open releases & outcome delivery" },
      { to: "/factory", label: "Factory Board", hint: "dev work items" },
      { to: "/architecture", label: "Architecture", hint: "capabilities & portfolio" },
      { to: "/connect", label: "Connections", hint: "plaid health & sync" },
      { to: "/health", label: "Institution Health", hint: "freshness, per institution" },
      { to: "/beads", label: "Bead Explorer", hint: "raw substrate" },
    ],
  },
];

const nav = navGroups.flatMap((g) => g.items);

export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    if (menuOpen) {
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = ""; };
    }
  }, [menuOpen]);
  const attention = useAttentionItems();

  const badgeFor = (to: string): number =>
    to === "/today" || to === "/inbox" ? attention.total : attention.byRoute[to] ?? 0;

  // Active label for mobile top bar
  const activeNavItem = nav.find((item) => item.to === location.pathname);
  const currentTitle = activeNavItem ? activeNavItem.label : "LifeOps";

  const primaryMobileNav = [
    {
      to: "/today",
      label: "Today",
      icon: (
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2H6a2 2 0 01-2-2v-4zM14 16a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2h-2a2 2 0 01-2-2v-4z" />
        </svg>
      ),
    },
    {
      to: "/inbox",
      label: "Inbox",
      icon: (
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0a2 2 0 01-2 2H6a2 2 0 01-2-2m16 0l-8 5-8-5" />
        </svg>
      ),
    },
    {
      to: "/alpha",
      label: "Alpha",
      icon: (
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h5m6 0h5M9 7a3 3 0 106 0 3 3 0 00-6 0zM4 17h5m6 0h5M9 17a3 3 0 106 0 3 3 0 00-6 0zM12 10v4" />
        </svg>
      ),
    },
    {
      to: "/ledger",
      label: "Ledger",
      icon: (
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" />
        </svg>
      ),
    },
  ];

  return (
    <div className="flex h-full min-h-screen flex-col md:flex-row bg-bg">
      {/* Desktop Sidebar (hidden on mobile) */}
      <aside className="hidden md:flex w-56 shrink-0 border-r border-border bg-bg-panel flex-col">
        <div className="px-4 py-4 border-b border-border">
          <div className="font-mono text-sm font-semibold tracking-tight">LifeOps</div>
          <div className="text-2xs text-fg-subtle uppercase tracking-widest">Console</div>
        </div>
        <nav className="flex-1 py-2 overflow-y-auto space-y-4">
          {navGroups.map((group) => (
            <div key={group.title} className="space-y-0.5">
              <div className="px-4 py-1 text-3xs font-semibold text-fg-subtle uppercase tracking-widest">
                {group.title}
              </div>
              {group.items.map((item) => {
                const count = badgeFor(item.to);
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={({ isActive }) =>
                      cn(
                        "block px-4 py-2 text-sm border-l-2 border-transparent transition-colors",
                        isActive
                          ? "border-accent text-fg bg-bg-hover"
                          : "text-fg-muted hover:text-fg hover:bg-bg-hover/50",
                      )
                    }
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span>{item.label}</span>
                      {count > 0 ? (
                        <span className="inline-flex min-w-[1.1rem] items-center justify-center rounded-full bg-warn/15 px-1 text-2xs font-medium text-warn num">
                          {count}
                        </span>
                      ) : null}
                    </div>
                    <div className="text-2xs text-fg-subtle">{item.hint}</div>
                  </NavLink>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-border text-2xs text-fg-subtle">
          Substrate · via {SUBSTRATE_PROXY}
        </div>
      </aside>

      {/* Mobile Top Header (hidden on desktop) */}
      <header className="md:hidden flex h-14 items-center justify-between px-4 border-b border-border bg-bg-panel/90 backdrop-blur sticky top-0 z-30">
        <div>
          <span className="font-mono text-xs font-semibold tracking-tight block">LifeOps</span>
          <span className="text-2xs text-fg-subtle uppercase tracking-widest">{currentTitle}</span>
        </div>
        <div className="text-2xs text-fg-subtle">
          via {SUBSTRATE_PROXY}
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 min-w-0 overflow-y-auto pb-20 md:pb-0">
        <Outlet />
      </main>

      {/* Mobile Bottom Navigation Bar (hidden on desktop) */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 h-16 border-t border-border bg-bg-panel/95 backdrop-blur z-30 flex justify-around items-center pb-safe">
        {primaryMobileNav.map((item) => {
          const count = badgeFor(item.to);
          const isActive = location.pathname === item.to;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={cn(
                "flex flex-col items-center justify-center w-16 h-full text-2xs font-medium transition-colors relative",
                isActive ? "text-accent" : "text-fg-muted"
              )}
            >
              {item.icon}
              <span className="mt-1">{item.label}</span>
              {count > 0 ? (
                <span className="absolute top-2 right-2 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-warn px-1 text-[9px] font-bold text-bg-panel">
                  {count}
                </span>
              ) : null}
            </NavLink>
          );
        })}

        {/* More Menu Trigger */}
        <button
          type="button"
          onClick={() => setMenuOpen(true)}
          className={cn(
            "flex flex-col items-center justify-center w-16 h-full text-2xs font-medium transition-colors",
            menuOpen ? "text-accent" : "text-fg-muted"
          )}
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
          <span className="mt-1">More</span>
        </button>
      </nav>

      {/* Mobile "More" Full Screen Navigation Overlay */}
      {menuOpen && (
        <div className="fixed inset-0 bg-bg-panel/98 backdrop-blur z-50 flex flex-col md:hidden animate-fade-in">
          <div className="flex h-14 items-center justify-between px-4 border-b border-border">
            <span className="font-mono text-sm font-semibold tracking-tight">All Destinations</span>
            <button
              type="button"
              onClick={() => setMenuOpen(false)}
              className="h-9 w-9 flex items-center justify-center rounded border border-border bg-bg-subtle text-fg"
            >
              ✕
            </button>
          </div>
          <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-4">
            {navGroups.map((group) => (
              <div key={group.title} className="bg-bg-subtle/30 rounded border border-border p-2 space-y-1">
                <div className="px-2 py-1 text-3xs font-semibold text-fg-subtle uppercase tracking-widest">
                  {group.title}
                </div>
                {group.items.map((item) => {
                  const count = badgeFor(item.to);
                  const isActive = location.pathname === item.to;
                  return (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      onClick={() => setMenuOpen(false)}
                      className={cn(
                        "flex items-center justify-between px-3 py-2 rounded text-sm transition-colors min-h-[44px]",
                        isActive
                          ? "bg-bg-hover text-accent font-semibold"
                          : "text-fg hover:bg-bg-hover/50",
                      )}
                    >
                      <div className="min-w-0">
                        <div className="font-medium truncate">{item.label}</div>
                        <div className="text-2xs text-fg-subtle truncate">{item.hint}</div>
                      </div>
                      {count > 0 ? (
                        <span className="inline-flex min-w-[1.25rem] h-5 items-center justify-center rounded-full bg-warn/15 px-1.5 text-2xs font-semibold text-warn num">
                          {count}
                        </span>
                      ) : null}
                    </NavLink>
                  );
                })}
              </div>
            ))}
          </nav>
        </div>
      )}
    </div>
  );
}
