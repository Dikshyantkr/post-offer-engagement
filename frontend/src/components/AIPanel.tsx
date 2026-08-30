"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiMutate, errorMessage } from "@/lib/api";
import { formatDateTime, titleCase } from "@/lib/format";
import type {
  AIAnalysis,
  AssessRiskResponse,
  DraftMessageResponse,
  RecommendActionResponse,
  SummarizeResponse,
} from "@/lib/types";
import { useActor } from "./Providers";
import { Card, Chip, ErrorNotice, FallbackNotice, RiskBadge } from "./ui";

type Action = "assess-risk" | "summarize" | "recommend-action" | "draft-message";

type Result =
  | ({ kind: "assess-risk" } & AssessRiskResponse)
  | ({ kind: "summarize" } & SummarizeResponse)
  | ({ kind: "recommend-action" } & RecommendActionResponse)
  | ({ kind: "draft-message" } & DraftMessageResponse);

const LABELS: Record<Action, string> = {
  "assess-risk": "Assess risk",
  summarize: "Summarize",
  "recommend-action": "Recommend action",
  "draft-message": "Draft message",
};

export function AIPanel({
  candidateId,
  latest,
}: {
  candidateId: string;
  latest: AIAnalysis | null;
}) {
  const { actor } = useActor();
  const router = useRouter();
  const [result, setResult] = useState<Result | null>(null);
  const [channel, setChannel] = useState<"email" | "whatsapp">("email");

  const mutation = useMutation({
    mutationFn: async (action: Action) => {
      const body =
        action === "draft-message"
          ? {
              channel,
              intent:
                "Check in on how the notice period and handover are going, and offer a short call.",
              tone: "warm",
            }
          : undefined;
      const data = await apiMutate<Record<string, unknown>>(
        `/ai/candidates/${candidateId}/${action}`,
        "POST",
        actor,
        body,
      );
      return { kind: action, ...data } as Result;
    },
    onSuccess: (data) => {
      setResult(data);
      // Assessing risk can change the candidate's stored level. The page is a
      // Server Component, so refresh() is what re-renders it with the new
      // badge — without it the header would keep showing the old level.
      if (data.kind === "assess-risk") router.refresh();
    },
  });

  const pending = mutation.isPending;

  return (
    <Card
      title="AI analysis"
      right={
        latest && !result ? (
          <span className="text-xs text-slate-500">
            last: {titleCase(latest.analysis_type)} · {formatDateTime(latest.created_at)}
          </span>
        ) : null
      }
    >
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {(Object.keys(LABELS) as Action[]).map((action) => (
            <button
              key={action}
              type="button"
              disabled={pending}
              onClick={() => mutation.mutate(action)}
              className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50"
            >
              {pending && mutation.variables === action ? "Working…" : LABELS[action]}
            </button>
          ))}
        </div>

        <label className="flex items-center gap-2 text-xs text-slate-600">
          Draft channel
          <select
            value={channel}
            onChange={(e) => setChannel(e.target.value as "email" | "whatsapp")}
            className="rounded border border-slate-300 px-2 py-1"
          >
            <option value="email">email</option>
            <option value="whatsapp">whatsapp</option>
          </select>
        </label>

        {/* These calls take 2-4 seconds against the real provider. Saying so
            is the difference between "thinking" and "broken". */}
        {pending && (
          <p role="status" className="text-sm text-slate-600">
            Calling the model — this usually takes 2–4 seconds…
          </p>
        )}

        {mutation.isError && (
          <ErrorNotice
            message={errorMessage(mutation.error)}
            retry={() => mutation.variables && mutation.mutate(mutation.variables)}
          />
        )}

        {result ? <ResultView result={result} /> : <LatestView latest={latest} />}
      </div>
    </Card>
  );
}

/** What the server rendered — the most recent stored analysis of any type. */
function LatestView({ latest }: { latest: AIAnalysis | null }) {
  if (!latest) {
    return (
      <p className="text-sm text-slate-600">
        No analysis stored for this candidate yet. Run one above.
      </p>
    );
  }

  const output = latest.parsed_output as Record<string, unknown>;
  const signals = Array.isArray(output.signals) ? (output.signals as string[]) : [];
  const reasoning = typeof output.reasoning === "string" ? output.reasoning : null;
  const summary = typeof output.summary === "string" ? output.summary : null;

  return (
    <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3 text-sm">
      <Meta
        type={latest.analysis_type}
        model={latest.model_name}
        confidence={latest.confidence}
        latency={latest.latency_ms}
        status={latest.validation_status}
      />
      {latest.was_fallback && <FallbackNotice caveat={reasoning ?? summary ?? undefined} />}
      {latest.risk_level && <RiskBadge level={latest.risk_level} />}
      {reasoning && !latest.was_fallback && <p className="whitespace-pre-wrap">{reasoning}</p>}
      {summary && !latest.was_fallback && <p className="whitespace-pre-wrap">{summary}</p>}
      {signals.length > 0 && <Evidence signals={signals} />}
    </div>
  );
}

function ResultView({ result }: { result: Result }) {
  const meta = result.meta;
  const caveat =
    result.kind === "assess-risk"
      ? result.assessment.reasoning
      : result.kind === "summarize"
        ? result.summary.summary
        : result.kind === "recommend-action"
          ? result.recommendation.rationale
          : undefined;

  return (
    <div className="space-y-3 rounded border border-slate-200 bg-slate-50 p-3 text-sm">
      <Meta
        type={meta.analysis_type}
        model={meta.model_name}
        confidence={meta.confidence}
        latency={meta.latency_ms}
        status={meta.validation_status}
      />

      {meta.was_fallback && <FallbackNotice caveat={caveat} />}

      {result.kind === "assess-risk" && (
        <>
          {/* max(base, ai) spelled out: what the rules said, what the model
              said, and which one won. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs uppercase text-slate-500">Rules</span>
            <RiskBadge level={result.risk.rule_floor_level} />
            <span className="text-slate-400">→</span>
            <span className="text-xs uppercase text-slate-500">AI</span>
            <RiskBadge level={result.risk.ai_level} />
            <span className="text-slate-400">→</span>
            <span className="text-xs uppercase text-slate-500">Final</span>
            <RiskBadge level={result.risk.final_level} source={result.risk.risk_source} />
          </div>
          <p className="text-slate-700">{result.risk.note}</p>
          <p className="whitespace-pre-wrap">{result.assessment.reasoning}</p>
          <div className="flex flex-wrap gap-2">
            <Chip>concern: {titleCase(result.assessment.concern_category)}</Chip>
            <Chip>confidence: {result.assessment.confidence}</Chip>
          </div>
          <Evidence signals={result.assessment.signals} />
        </>
      )}

      {result.kind === "summarize" && (
        <>
          <p className="whitespace-pre-wrap">{result.summary.summary}</p>
          <Chip>sentiment: {titleCase(result.summary.sentiment)}</Chip>
          <List title="Key concerns" items={result.summary.key_concerns} />
          <List title="Unresolved" items={result.summary.unresolved_items} />
        </>
      )}

      {result.kind === "recommend-action" && (
        <>
          <div className="flex flex-wrap gap-2">
            <Chip>{titleCase(result.recommendation.action_type)}</Chip>
            <Chip>via {result.recommendation.channel}</Chip>
            <Chip>urgency: {result.recommendation.urgency}</Chip>
            <Chip>in {result.recommendation.suggested_timing_days} days</Chip>
          </div>
          <p className="whitespace-pre-wrap">{result.recommendation.rationale}</p>
        </>
      )}

      {result.kind === "draft-message" && (
        <>
          <p className="text-xs text-slate-600">
            A draft for you to edit and send. Nothing has been sent to the candidate.
          </p>
          {result.draft.subject && (
            <p className="font-medium">Subject: {result.draft.subject}</p>
          )}
          <pre className="whitespace-pre-wrap rounded border border-slate-300 bg-white p-3 font-sans">
            {result.draft.body}
          </pre>
          <List title="Personalisation used" items={result.draft.personalization_used} />
          {result.guardrails_removed.length > 0 && (
            <div className="rounded border border-amber-400 bg-amber-50 p-2 text-xs text-amber-900">
              <p className="font-medium">
                Guardrails removed {result.guardrails_removed.length} fragment(s):
              </p>
              <ul className="mt-1 list-disc pl-4">
                {result.guardrails_removed.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * The evidence quotes, rendered as a list.
 *
 * CLAUDE.md is explicit that a risk badge without visible reasoning reads as
 * magic. These are the candidate's own words, quoted from the interaction log,
 * and they are the thing that lets a recruiter decide whether to trust the
 * level at all.
 */
function Evidence({ signals }: { signals: string[] }) {
  if (signals.length === 0) return null;
  return (
    <div>
      <p className="text-xs font-semibold uppercase text-slate-500">Evidence</p>
      <ul className="mt-1 space-y-1">
        {signals.map((signal) => (
          <li
            key={signal}
            className="border-l-2 border-slate-400 pl-2 text-slate-800"
          >
            {signal}
          </li>
        ))}
      </ul>
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="text-xs font-semibold uppercase text-slate-500">{title}</p>
      <ul className="mt-1 list-disc pl-5">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function Meta({
  type,
  model,
  confidence,
  latency,
  status,
}: {
  type: string;
  model: string;
  confidence: number;
  latency: number;
  status: string;
}) {
  return (
    <div className="flex flex-wrap gap-2 text-xs text-slate-500">
      <span className="font-medium text-slate-700">{titleCase(type)}</span>
      <span>{model}</span>
      <span>confidence {confidence}</span>
      <span>{latency}ms</span>
      <span>{status}</span>
    </div>
  );
}
