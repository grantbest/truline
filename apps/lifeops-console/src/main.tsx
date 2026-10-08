import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import "./index.css";

// Only Connections and FactoryBoard set a refetchInterval; every other route —
// the whole finance surface included — refetches on observer mount and nothing
// else. With focus refetching off, a tab parked on one route served the data as
// of mount for as long as it stayed open, which is how this console is actually
// used on mobile. That is what let the transaction ledger sit on a stale
// "latest transaction" with nothing on screen to say so.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
