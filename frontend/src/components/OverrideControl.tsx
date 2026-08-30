"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiMutate, errorMessage } from "@/lib/api";
import { RISK_LEVELS, titleCase } from "@/lib/format";
import type { RiskLevel, RiskSource } from "@/lib/types";
import { useActor } from "./Providers";

/**
 * HR override, sitting next to the badge as CLAUDE.md requires.
 *
 * Posts to the Module 5 override endpoint rather than PATCH /candidates, because
 * that endpoint records the AI level it disagreed with and the reason in the
 * audit log — which is the only correction signal this system ever gets, since
 * nobody labels which candidates were really about to drop out.
 */
export function OverrideControl({
  candidateId,
  currentLevel,
  currentSource,
}: {
  candidateId: string;
  currentLevel: RiskLevel;
  currentSource: RiskSource;
}) {
  const { actor } = useActor();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [level, setLevel] = useState<RiskLevel>(currentLevel);
  const [reason, setReason] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      apiMutate(`/ai/candidates/${candidateId}/override`, "POST", actor, {
        risk_level: level,
        reason,
      }),
    onSuccess: () => {
      setOpen(false);
      setReason("");
      router.refresh();
    },
  });

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs hover:bg-slate-100"
      >
        Override risk
      </button>
    );
  }

  return (
    <div className="w-full max-w-md rounded border border-slate-300 bg-white p-3">
      <p className="text-sm font-medium">Override risk level</p>
      <p className="mt-1 text-xs text-slate-600">
        Currently {currentLevel} ({currentSource}). A human override beats both the rules
        and the AI, and survives every recompute.
      </p>

      <div className="mt-2 flex gap-2">
        {RISK_LEVELS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setLevel(option)}
            className={`rounded border px-2 py-1 text-xs ${
              level === option ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300"
            }`}
          >
            {titleCase(option)}
          </button>
        ))}
      </div>

      <textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={2}
        placeholder="Why do you disagree? (required)"
        className="mt-2 w-full rounded border border-slate-300 p-2 text-sm"
      />

      {mutation.isError && (
        <p role="alert" className="mt-2 text-xs text-red-700">
          {errorMessage(mutation.error)}
        </p>
      )}

      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={!reason.trim() || mutation.isPending}
          onClick={() => mutation.mutate()}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white disabled:opacity-40"
        >
          {mutation.isPending ? "Saving…" : "Save override"}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded border border-slate-300 px-3 py-1 text-xs"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
