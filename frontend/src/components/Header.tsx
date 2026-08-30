"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/lib/api";
import type { Paginated, Recruiter } from "@/lib/types";
import { useActor } from "./Providers";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/actions", label: "Action queue" },
  { href: "/analytics", label: "Analytics" },
];

export function Header() {
  const pathname = usePathname();
  const { actor, setActor } = useActor();

  const { data, isError } = useQuery({
    queryKey: ["recruiters"],
    queryFn: () => apiGet<Paginated<Recruiter>>("/recruiters?limit=100"),
    staleTime: 5 * 60_000,
  });

  return (
    <header className="border-b border-slate-300 bg-white">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-3">
        <span className="font-semibold text-slate-900">Post-Offer Engagement</span>

        <nav className="flex gap-1">
          {NAV.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded px-3 py-1.5 text-sm ${
                  active
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <label htmlFor="actor" className="text-sm text-slate-600">
            Acting as
          </label>
          <select
            id="actor"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
            className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="system">system</option>
            {data?.items.map((recruiter) => (
              <option key={recruiter.id} value={recruiter.email}>
                {recruiter.name}
              </option>
            ))}
          </select>
          {isError && (
            <span className="text-xs text-red-700" role="status">
              recruiters unavailable
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
