"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiMutate, errorMessage } from "@/lib/api";
import type { BlockerCategory, InteractionChannel, RecruiterRead } from "@/lib/types";
import { useActor } from "./Providers";
import { Card } from "./ui";

const CHANNELS: InteractionChannel[] = ["email", "whatsapp", "call", "in_person"];
const BLOCKERS: BlockerCategory[] = [
  "none",
  "relocation",
  "notice_period",
  "counter_offer",
  "compensation",
  "role_scope",
  "personal",
];
const READS: RecruiterRead[] = ["on_track", "unsure", "worried"];

/**
 * Log an interaction.
 *
 * The call-note fields only appear when channel is "call". That is not a UI
 * nicety: the API rejects blocker_raised / blocker_category / date_confirmed /
 * recruiter_read on any other channel with a 422, because they are the
 * recruiter's structured read of a phone call and mean nothing on an email.
 * Showing them for an email would be offering the user a guaranteed error.
 */
export function InteractionForm({ candidateId }: { candidateId: string }) {
  const { actor } = useActor();
  const router = useRouter();

  const [channel, setChannel] = useState<InteractionChannel>("call");
  const [direction, setDirection] = useState<"inbound" | "outbound">("outbound");
  const [content, setContent] = useState("");
  const [blocker, setBlocker] = useState<BlockerCategory>("none");
  const [read, setRead] = useState<RecruiterRead | "">("");
  const [dateConfirmed, setDateConfirmed] = useState<"" | "yes" | "no">("");

  const isCall = channel === "call";

  const mutation = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { channel, direction, content };
      if (isCall) {
        body.blocker_raised = blocker !== "none";
        body.blocker_category = blocker;
        if (read) body.recruiter_read = read;
        if (dateConfirmed) body.date_confirmed = dateConfirmed === "yes";
      }
      return apiMutate(`/candidates/${candidateId}/interactions`, "POST", actor, body);
    },
    onSuccess: () => {
      setContent("");
      setBlocker("none");
      setRead("");
      setDateConfirmed("");
      // Logging contact recomputes risk in the same transaction, so the badge
      // and the timeline both need to come back from the server.
      router.refresh();
    },
  });

  return (
    <Card title="Log an interaction">
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className="flex gap-2">
          <label className="flex-1 text-xs text-slate-600">
            Channel
            <select
              value={channel}
              onChange={(e) => setChannel(e.target.value as InteractionChannel)}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            >
              {CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="flex-1 text-xs text-slate-600">
            Direction
            <select
              value={direction}
              onChange={(e) => setDirection(e.target.value as "inbound" | "outbound")}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="outbound">outbound</option>
              <option value="inbound">inbound</option>
            </select>
          </label>
        </div>

        <label className="block text-xs text-slate-600">
          Content
          <textarea
            required
            rows={3}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder={isCall ? "What was said on the call…" : "Message content…"}
            className="mt-1 w-full rounded border border-slate-300 p-2 text-sm"
          />
        </label>

        {isCall && (
          <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-2">
            <p className="text-xs font-medium text-slate-700">
              Your read of the call — captured now, while it exists
            </p>
            <label className="block text-xs text-slate-600">
              Blocker raised
              <select
                value={blocker}
                onChange={(e) => setBlocker(e.target.value as BlockerCategory)}
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
              >
                {BLOCKERS.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-xs text-slate-600">
              How did they seem?
              <select
                value={read}
                onChange={(e) => setRead(e.target.value as RecruiterRead | "")}
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
              >
                <option value="">—</option>
                {READS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-xs text-slate-600">
              Start date confirmed?
              <select
                value={dateConfirmed}
                onChange={(e) => setDateConfirmed(e.target.value as "" | "yes" | "no")}
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
              >
                <option value="">—</option>
                <option value="yes">yes</option>
                <option value="no">no</option>
              </select>
            </label>
          </div>
        )}

        {mutation.isError && (
          <p role="alert" className="text-xs text-red-700">
            {errorMessage(mutation.error)}
          </p>
        )}
        {mutation.isSuccess && (
          <p role="status" className="text-xs text-green-700">
            Logged. Risk has been recomputed.
          </p>
        )}

        <button
          type="submit"
          disabled={mutation.isPending || !content.trim()}
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-40"
        >
          {mutation.isPending ? "Saving…" : "Log interaction"}
        </button>
      </form>
    </Card>
  );
}
