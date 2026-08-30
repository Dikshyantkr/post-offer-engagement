import { Suspense } from "react";

import { Dashboard } from "@/components/Dashboard";
import { Loading } from "@/components/ui";

// Rendered per request. useSearchParams() opts a client component out of
// static prerendering: without this, `next build` bakes the Suspense fallback
// into the static HTML and the whole filter UI only appears after hydration.
// Marking the route dynamic lets the server render it with the real search
// params, so a shared filtered link arrives with its filters already set.
export const dynamic = "force-dynamic";

// Next still requires the boundary around a component reading search params.
export default function DashboardPage() {
  return (
    <Suspense fallback={<Loading label="Loading dashboard…" />}>
      <Dashboard />
    </Suspense>
  );
}
