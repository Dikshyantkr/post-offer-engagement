"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { apiGet, apiMutate, errorMessage } from "@/lib/api";
import { formatDate, titleCase } from "@/lib/format";
import type {
  AutomationRunResponse,
  Candidate,
  FollowUpAction,
  FollowUpPriority,
  Paginated,
} from "@/lib/types";
import { useActor } from "./Providers";
import { Card, Chip, Empty, ErrorNotice, Loading } from "./ui";

const PRIORITY_ORDER: Record<FollowUpPriority, number> = {
  urgent: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const PRIORITY_STYLE: Record<FollowUpPriority, string> = {
  urgent: "bg-red-100 text-red-900 border-red-300",
  high: "bg-orange-100 text-orange-900 border-orange-300",
  medium: "bg-amber-100 text-amber-900 border-amber-300",
  low: "bg-slate-100 text-slate-800 border-slate-300",
};

export function ActionQueue() {
  const { actor } = useActor();
  const queryClient = useQueryClient();
  const [sweep, setSweep] = useState<AutomationRunResponse | null>(null);

  const actions = useQuery({
    queryKey: ["open-actions"],
    queryFn: () =>
      apiGet<Paginated<FollowUpAction>>("/follow-up-actions?status=open&limit=100"),
  });

  // Names for the candidate each action belongs to; the action payload carries
  // only the id.
  const candidates = useQuery({
    queryKey: ["candidates", "all-for-actions"],
    queryFn: () => apiGet<Paginated<Candidate>>("/candidates?limit=100"),
    staleTime: 60_000,
  });

  const nameFor = (id: string) =>
    candidates.data?.items.find((c) => c.id === id)?.name ?? "Candidate";

  const resolve = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "done" | "dismissed" }) =>
      apiMutate(`/follow-up-actions/${id}`, "PATCH", actor, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["open-actions"] }),
  });

  const runSweep = useMutation({
    mutationFn: () => apiMutate<AutomationRunResponse>("/automation/run", "POST", actor),
    onSuccess: (data) => {
      setSweep(data);
      queryClient.invalidateQueries({ queryKey: ["open-actions"] });
    },
  });

  // Priority first, then due date — the order a recruiter works the queue in.
  const sorted = useMemo(() => {
    const items = [...(actions.data?.items ?? [])];
    items.sort((a, b) => {
      const byPriority = PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
      if (byPriority !== 0) return byPriority;
      return (a.due_date ?? "9999-12-31").localeCompare(b.due_date ?? "9999-12-31");
    });
    return items;
  }, [actions.data]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Action queue</h1>
          <p className="text-sm text-slate-600">
            {actions.isLoading ? "Loading…" : `${sorted.length} open across all candidates`}
          </p>
        </div>
        <button
          type="button"
          onClick={() => runSweep.mutate()}
          disabled={runSweep.isPending}
          className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
        >
          {runSweep.isPending ? "Sweeping — this calls the model…" : "Run automation sweep"}
        </button>
      </div>

      {runSweep.isError && (
        <ErrorNotice message={errorMessage(runSweep.error)} retry={() => runSweep.mutate()} />
      )}

      {sweep && (
        <Card title="Last sweep">
          <div className="flex flex-wrap gap-2 text-sm">
            <Chip>{sweep.candidates_scanned} candidates scanned</Chip>
            <Chip>{sweep.actions_created} actions created</Chip>
            {Object.entries(sweep.rules).map(([rule, outcome]) => (
              <Chip key={rule}>
                {rule}: {outcome.matched} matched, {outcome.actions_created} created,{" "}
                {outcome.skipped_existing_action} skipped
              </Chip>
            ))}
            <Chip>{sweep.ai_calls} AI calls</Chip>
            <Chip>{sweep.ai_fallbacks} fallbacks</Chip>
            <Chip>{sweep.messages_simulated} messages drafted</Chip>
            <Chip>{sweep.errors} errors</Chip>
            <Chip>{sweep.duration_ms}ms</Chip>
          </div>
          {sweep.actions_created === 0 && (
            <p className="mt-2 text-sm text-slate-600">
              Nothing new — the sweep will not raise a second action for the same candidate
              and rule inside 24 hours.
            </p>
          )}
        </Card>
      )}

      {actions.isError ? (
        <ErrorNotice message={errorMessage(actions.error)} retry={() => actions.refetch()} />
      ) : actions.isLoading ? (
        <div className="rounded border border-slate-300 bg-white">
          <Loading label="Loading actions…" />
        </div>
      ) : sorted.length === 0 ? (
        <div className="rounded border border-slate-300 bg-white">
          <Empty label="No open actions. Run the sweep to look for candidates needing attention." />
        </div>
      ) : (
        <ul className="space-y-3">
          {sorted.map((action) => (
            <li key={action.id} className="rounded border border-slate-300 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className={`rounded border px-2 py-0.5 text-xs font-medium ${PRIORITY_STYLE[action.priority]}`}
                    >
                      {action.priority.toUpperCase()}
                    </span>
                    <Link
                      href={`/candidates/${action.candidate_id}`}
                      className="font-medium text-blue-700 underline"
                    >
                      {nameFor(action.candidate_id)}
                    </Link>
                    {action.rule_key && <Chip>{action.rule_key}</Chip>}
                    <Chip>{titleCase(action.source)}</Chip>
                    {action.due_date && <Chip>due {formatDate(action.due_date)}</Chip>}
                  </div>
                  <p className="mt-2 font-medium">{action.title}</p>
                </div>

                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate({ id: action.id, status: "done" })}
                    className="rounded border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100 disabled:opacity-40"
                  >
                    Mark done
                  </button>
                  <button
                    type="button"
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate({ id: action.id, status: "dismissed" })}
                    className="rounded border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100 disabled:opacity-40"
                  >
                    Dismiss
                  </button>
                </div>
              </div>

              {action.description && (
                <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">
                  {action.description}
                </p>
              )}

              {action.generated_message && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-sm font-medium text-slate-800">
                    Drafted message — review and send yourself
                  </summary>
                  <pre className="mt-2 whitespace-pre-wrap rounded border border-slate-300 bg-slate-50 p-3 font-sans text-sm">
                    {action.generated_message}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}

      {resolve.isError && <ErrorNotice message={errorMessage(resolve.error)} />}
    </div>
  );
}
