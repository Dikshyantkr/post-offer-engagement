"use client";

import { ErrorNotice } from "@/components/ui";

/** Route-level error boundary. Without this an exception in a Server Component
 *  renders Next's default error screen, which tells a recruiter nothing. */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="py-6">
      <ErrorNotice message={error.message} retry={reset} />
    </div>
  );
}
