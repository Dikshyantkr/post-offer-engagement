"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import { apiGet, errorMessage } from "@/lib/api";
import {
  ENGAGEMENT_STATUSES,
  RISK_LEVELS,
  daysSinceContact,
  daysUntil,
  formatDate,
  joiningMonthOptions,
  titleCase,
} from "@/lib/format";
import type { Candidate, FollowUpAction, Paginated, Recruiter } from "@/lib/types";
import { Empty, ErrorNotice, Loading, RiskBadge } from "./ui";

const PAGE_SIZE = 20;

// Risk first by default. The product's claim is "forty pending joiners turned
// into the five worth a phone call this morning" — opening on a date-ordered
// list would bury exactly those five.
const DEFAULT_SORT = "risk";

const FILTER_KEYS = [
  "joining_month",
  "recruiter_id",
  "role",
  "risk_level",
  "engagement_status",
  "search",
  "sort",
  "offset",
] as const;

export function Dashboard() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  // Filter state lives in the URL, not component state, so a filtered view is
  // a shareable link — "the five I'm calling today" can be pasted to a
  // colleague and survives a refresh or a back button.
  const get = useCallback((key: string) => params.get(key) ?? "", [params]);
  const sort = get("sort") || DEFAULT_SORT;
  const offset = Number(get("offset") || 0);

  const setParams = useCallback(
    (updates: Record<string, string>) => {
      const next = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(updates)) {
        if (value) next.set(key, value);
        else next.delete(key);
      }
      // Any filter change invalidates the current page number; staying on
      // page 3 of a newly filtered list usually lands on nothing.
      if (!("offset" in updates)) next.delete("offset");
      router.replace(`${pathname}?${next.toString()}`, { scroll: false });
    },
    [params, pathname, router],
  );

  const query = useMemo(() => {
    const q = new URLSearchParams();
    q.set("limit", String(PAGE_SIZE));
    q.set("offset", String(offset));
    q.set("sort", sort);
    for (const key of FILTER_KEYS) {
      if (key === "sort" || key === "offset") continue;
      const value = params.get(key);
      if (value) q.set(key, value);
    }
    return q.toString();
  }, [params, offset, sort]);

  const candidates = useQuery({
    queryKey: ["candidates", query],
    queryFn: () => apiGet<Paginated<Candidate>>(`/candidates?${query}`),
    // Without this the table unmounts to a spinner on every keystroke and
    // every filter change, which reads as the app losing your data.
    placeholderData: keepPreviousData,
  });

  const recruiters = useQuery({
    queryKey: ["recruiters"],
    queryFn: () => apiGet<Paginated<Recruiter>>("/recruiters?limit=100"),
    staleTime: 5 * 60_000,
  });

  // Roles are a free-text column with no dedicated endpoint, so the option
  // list is derived from the candidates themselves. One request at limit=100
  // covers the whole seeded set; at real scale this wants a
  // GET /candidates/roles endpoint rather than a bigger page.
  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: async () => {
      const page = await apiGet<Paginated<Candidate>>("/candidates?limit=100");
      return Array.from(new Set(page.items.map((c) => c.role))).sort();
    },
    staleTime: 5 * 60_000,
  });

  // The candidate list carries no next-action field, so open actions are
  // fetched once and indexed by candidate. Fine for 54 candidates and 7 open
  // actions; at scale this belongs on the list response.
  const actions = useQuery({
    queryKey: ["open-actions"],
    queryFn: () =>
      apiGet<Paginated<FollowUpAction>>("/follow-up-actions?status=open&limit=100"),
  });

  const actionByCandidate = useMemo(() => {
    const map = new Map<string, FollowUpAction>();
    for (const action of actions.data?.items ?? []) {
      if (!map.has(action.candidate_id)) map.set(action.candidate_id, action);
    }
    return map;
  }, [actions.data]);

  const recruiterName = useCallback(
    (id: string) => recruiters.data?.items.find((r) => r.id === id)?.name ?? "—",
    [recruiters.data],
  );

  const total = candidates.data?.total ?? 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const activeFilters = FILTER_KEYS.filter(
    (k) => k !== "sort" && k !== "offset" && params.get(k),
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Candidates</h1>
          <p className="text-sm text-slate-600">
            {candidates.isLoading ? "Loading…" : `${total} matching`}
            {candidates.isPlaceholderData && " · updating…"}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-sm text-slate-600">Sort</span>
          <div className="flex overflow-hidden rounded border border-slate-300">
            {(
              [
                ["risk", "Risk"],
                ["joining_date", "Joining date"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setParams({ sort: value })}
                className={`px-3 py-1.5 text-sm ${
                  sort === value ? "bg-slate-900 text-white" : "bg-white hover:bg-slate-100"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Five filters plus search, per CLAUDE.md. */}
      <div className="grid grid-cols-1 gap-3 rounded border border-slate-300 bg-white p-3 sm:grid-cols-2 lg:grid-cols-6">
        <Field label="Search">
          <input
            type="search"
            value={get("search")}
            onChange={(e) => setParams({ search: e.target.value })}
            placeholder="Name, email, role"
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>

        <Field label="Joining month">
          <Select value={get("joining_month")} onChange={(v) => setParams({ joining_month: v })}>
            <option value="">Any</option>
            {joiningMonthOptions().map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Recruiter">
          <Select value={get("recruiter_id")} onChange={(v) => setParams({ recruiter_id: v })}>
            <option value="">Any</option>
            {recruiters.data?.items.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Role">
          <Select value={get("role")} onChange={(v) => setParams({ role: v })}>
            <option value="">Any</option>
            {roles.data?.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Risk">
          <Select value={get("risk_level")} onChange={(v) => setParams({ risk_level: v })}>
            <option value="">Any</option>
            {RISK_LEVELS.map((r) => (
              <option key={r} value={r}>
                {titleCase(r)}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Stage">
          <Select
            value={get("engagement_status")}
            onChange={(v) => setParams({ engagement_status: v })}
          >
            <option value="">Any</option>
            {ENGAGEMENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {titleCase(s)}
              </option>
            ))}
          </Select>
        </Field>

        {activeFilters.length > 0 && (
          <div className="lg:col-span-6">
            <button
              type="button"
              onClick={() =>
                setParams(Object.fromEntries(activeFilters.map((k) => [k, ""])) as Record<string, string>)
              }
              className="rounded border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100"
            >
              Clear {activeFilters.length} filter{activeFilters.length > 1 ? "s" : ""}
            </button>
          </div>
        )}
      </div>

      {candidates.isError ? (
        <ErrorNotice message={errorMessage(candidates.error)} retry={() => candidates.refetch()} />
      ) : candidates.isLoading ? (
        <div className="rounded border border-slate-300 bg-white">
          <Loading label="Loading candidates…" />
        </div>
      ) : (candidates.data?.items.length ?? 0) === 0 ? (
        <div className="rounded border border-slate-300 bg-white">
          <Empty label="No candidates match these filters." />
        </div>
      ) : (
        <div className="overflow-x-auto rounded border border-slate-300 bg-white">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-600">
              <tr>
                <Th>Name</Th>
                <Th>Role</Th>
                <Th>Recruiter</Th>
                <Th>Joining</Th>
                <Th>Days to join</Th>
                <Th>Days silent</Th>
                <Th>Stage</Th>
                <Th>Risk</Th>
                <Th>Next action</Th>
              </tr>
            </thead>
            <tbody>
              {candidates.data?.items.map((c) => {
                const toJoin = daysUntil(c.joining_date);
                const silent = daysSinceContact(c.last_interaction_at, c.offer_date);
                const action = actionByCandidate.get(c.id);
                return (
                  <tr key={c.id} className="border-b border-slate-100 align-top hover:bg-slate-50">
                    <Td>
                      <Link
                        href={`/candidates/${c.id}`}
                        className="font-medium text-blue-700 underline"
                      >
                        {c.name}
                      </Link>
                      <div className="text-xs text-slate-500">{c.location}</div>
                    </Td>
                    <Td>{c.role}</Td>
                    <Td>{recruiterName(c.recruiter_id)}</Td>
                    <Td>{formatDate(c.joining_date)}</Td>
                    <Td>
                      <span className={toJoin <= 7 ? "font-semibold text-red-700" : ""}>
                        {toJoin}
                      </span>
                    </Td>
                    <Td>
                      <span className={silent >= 10 ? "font-semibold text-red-700" : ""}>
                        {silent}
                      </span>
                      {!c.last_interaction_at && (
                        <div className="text-xs text-slate-500">never contacted</div>
                      )}
                    </Td>
                    <Td>{titleCase(c.engagement_status)}</Td>
                    <Td>
                      <RiskBadge level={c.risk_level} source={c.risk_source} />
                      <div className="mt-1 text-xs text-slate-500">
                        floor {c.risk_score_base}
                      </div>
                    </Td>
                    <Td>
                      {action ? (
                        <span className="text-xs">
                          <span className="font-medium uppercase">{action.priority}</span>{" "}
                          {action.title}
                        </span>
                      ) : (
                        <span className="text-xs text-slate-400">—</span>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-600">
          Page {page} of {pageCount}
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={offset <= 0}
            onClick={() => setParams({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40"
          >
            Previous
          </button>
          <button
            type="button"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setParams({ offset: String(offset + PAGE_SIZE) })}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
    </label>
  );
}

function Select({
  value,
  onChange,
  children,
}: {
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
    >
      {children}
    </select>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-medium">{children}</th>;
}

function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-3 py-2">{children}</td>;
}
