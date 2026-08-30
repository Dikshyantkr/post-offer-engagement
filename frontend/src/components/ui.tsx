import type { RiskLevel } from "@/lib/types";
import { titleCase } from "@/lib/format";

// Shared presentational pieces. Server-safe (no "use client") so both Server
// and Client Components can use them.

const RISK_STYLES: Record<RiskLevel, string> = {
  high: "bg-red-100 text-red-900 border-red-300",
  medium: "bg-amber-100 text-amber-900 border-amber-300",
  low: "bg-green-100 text-green-900 border-green-300",
};

export function RiskBadge({ level, source }: { level: RiskLevel; source?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs font-medium ${RISK_STYLES[level]}`}
    >
      {level.toUpperCase()}
      {source && <span className="font-normal opacity-75">({source})</span>}
    </span>
  );
}

/** Neutral chip. Class names are spelled out rather than interpolated —
 *  Tailwind scans source text and cannot see a dynamically built class. */
export function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700">{children}</span>
  );
}

export function StatusText({ value }: { value: string }) {
  return <span className="text-sm text-slate-700">{titleCase(value)}</span>;
}

export function Card({
  title,
  children,
  right,
}: {
  title: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <section className="rounded border border-slate-300 bg-white">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {right}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function ErrorNotice({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div
      role="alert"
      className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900"
    >
      <p className="font-medium">Something went wrong</p>
      <p className="mt-1">{message}</p>
      {retry && (
        <button
          type="button"
          onClick={retry}
          className="mt-3 rounded border border-red-400 px-3 py-1 text-sm hover:bg-red-100"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="p-4 text-sm text-slate-600">
      {label}
    </p>
  );
}

export function Empty({ label }: { label: string }) {
  return <p className="p-4 text-sm text-slate-600">{label}</p>;
}

/**
 * Banner shown whenever an AI response came back was_fallback=true.
 *
 * The caveat text is the fallback's own reasoning, not something invented
 * here: the point is that a recruiter must never mistake the deterministic
 * rule floor for the model having read the candidate's messages.
 */
export function FallbackNotice({ caveat }: { caveat?: string }) {
  return (
    <div className="rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900">
      <p className="font-medium">Rule-based fallback — the AI provider did not answer.</p>
      {caveat && <p className="mt-1 whitespace-pre-wrap">{caveat}</p>}
    </div>
  );
}
