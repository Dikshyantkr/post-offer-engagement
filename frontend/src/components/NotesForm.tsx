"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiMutate, errorMessage } from "@/lib/api";
import { useActor } from "./Providers";
import { Card } from "./ui";

/** Recruiter notes. PATCH /candidates writes an audit_log row, so the actor
 *  from the header switcher matters here too. */
export function NotesForm({
  candidateId,
  notes,
}: {
  candidateId: string;
  notes: string | null;
}) {
  const { actor } = useActor();
  const router = useRouter();
  const [value, setValue] = useState(notes ?? "");

  const mutation = useMutation({
    mutationFn: () =>
      apiMutate(`/candidates/${candidateId}`, "PATCH", actor, { notes: value }),
    onSuccess: () => router.refresh(),
  });

  return (
    <Card title="Recruiter notes">
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <textarea
          rows={4}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Private notes about this candidate…"
          className="w-full rounded border border-slate-300 p-2 text-sm"
        />
        {mutation.isError && (
          <p role="alert" className="text-xs text-red-700">
            {errorMessage(mutation.error)}
          </p>
        )}
        {mutation.isSuccess && (
          <p role="status" className="text-xs text-green-700">
            Saved.
          </p>
        )}
        <button
          type="submit"
          disabled={mutation.isPending}
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-40"
        >
          {mutation.isPending ? "Saving…" : "Save notes"}
        </button>
      </form>
    </Card>
  );
}
