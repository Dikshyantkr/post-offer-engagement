import Link from "next/link";
import { notFound } from "next/navigation";

import { AIPanel } from "@/components/AIPanel";
import { InteractionForm } from "@/components/InteractionForm";
import { NotesForm } from "@/components/NotesForm";
import { OverrideControl } from "@/components/OverrideControl";
import { Card, Chip, RiskBadge } from "@/components/ui";
import { ApiError, serverFetch } from "@/lib/api";
import {
  bandForScore,
  daysSinceContact,
  daysUntil,
  formatDate,
  formatDateTime,
  titleCase,
} from "@/lib/format";
import type { CandidateDetail, Paginated, Recruiter } from "@/lib/types";

// Rendered per request. Without this `next build` tries to prerender the route
// and fails, because the API is not running during the image build.
export const dynamic = "force-dynamic";

export default async function CandidatePage({ params }: { params: { id: string } }) {
  let candidate: CandidateDetail;
  try {
    candidate = await serverFetch<CandidateDetail>(`/candidates/${params.id}`);
  } catch (error) {
    // A missing candidate is a 404 page, not the generic error boundary.
    // Anything else rethrows and error.tsx shows the API's own message.
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const recruiters = await serverFetch<Paginated<Recruiter>>("/recruiters?limit=100");
  const recruiter = recruiters.items.find((r) => r.id === candidate.recruiter_id);

  const toJoin = daysUntil(candidate.joining_date);
  const silent = daysSinceContact(candidate.last_interaction_at, candidate.offer_date);
  const floorBand = bandForScore(candidate.risk_score_base);
  const noticeDays = Math.round(
    (new Date(`${candidate.joining_date}T00:00:00Z`).getTime() -
      new Date(`${candidate.offer_date}T00:00:00Z`).getTime()) /
      86_400_000,
  );

  return (
    <div className="space-y-4">
      <div>
        <Link href="/" className="text-sm text-blue-700 underline">
          ← Back to dashboard
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{candidate.name}</h1>
          <RiskBadge level={candidate.risk_level} source={candidate.risk_source} />
          <OverrideControl
            candidateId={candidate.id}
            currentLevel={candidate.risk_level}
            currentSource={candidate.risk_source}
          />
        </div>
        <p className="mt-1 text-sm text-slate-600">
          {candidate.role} · {candidate.department} · {candidate.location}
        </p>
      </div>

      {/* The rule floor beside the final badge, so `final = max(base, ai)` is
          legible without opening an analysis. */}
      <div className="rounded border border-slate-300 bg-white p-4">
        <div className="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm">
          <div>
            <div className="text-xs uppercase text-slate-500">Final risk</div>
            <RiskBadge level={candidate.risk_level} source={candidate.risk_source} />
          </div>
          <div>
            <div className="text-xs uppercase text-slate-500">Rule floor</div>
            <span>
              <RiskBadge level={floorBand} /> <span className="text-slate-600">score {candidate.risk_score_base}</span>
            </span>
          </div>
          <p className="max-w-xl text-slate-600">
            {candidate.risk_source === "hr_override"
              ? "A human set this level. It overrides both the rules and the AI."
              : candidate.risk_source === "ai"
                ? "The AI read this candidate's messages and raised the level above the rule floor. It can never lower it."
                : "The deterministic rules set this level. An AI assessment could raise it, never lower it."}
          </p>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card title="Offer details">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
              <Detail label="Email" value={candidate.email} />
              <Detail label="Phone" value={candidate.phone ?? "—"} />
              <Detail label="Recruiter" value={recruiter?.name ?? "—"} />
              <Detail label="Offer date" value={formatDate(candidate.offer_date)} />
              <Detail label="Joining date" value={formatDate(candidate.joining_date)} />
              <Detail label="Notice period" value={`${noticeDays} days`} />
              <Detail label="Days to joining" value={String(toJoin)} />
              <Detail
                label="Days since contact"
                value={
                  candidate.last_interaction_at
                    ? String(silent)
                    : `${silent} (never contacted)`
                }
              />
              <Detail label="Stage" value={titleCase(candidate.engagement_status)} />
              <Detail label="Outcome" value={titleCase(candidate.final_outcome)} />
            </dl>
          </Card>

          <Card title="Stage timeline">
            <ol className="space-y-2">
              {candidate.stages.map((stage) => {
                const overdue =
                  stage.status === "pending" && daysUntil(stage.due_date) < 0;
                return (
                  <li
                    key={stage.id}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-100 pb-2 text-sm last:border-0"
                  >
                    <span className="w-6 text-xs text-slate-400">{stage.sequence_order}</span>
                    <span className="min-w-52 font-medium">{stage.stage_label}</span>
                    <span className="text-slate-600">due {formatDate(stage.due_date)}</span>
                    <Chip>{titleCase(stage.status)}</Chip>
                    {overdue && (
                      <span className="text-xs font-medium text-red-700">
                        overdue {Math.abs(daysUntil(stage.due_date))} days
                      </span>
                    )}
                    {stage.completed_by && (
                      <span className="text-xs text-slate-500">by {stage.completed_by}</span>
                    )}
                  </li>
                );
              })}
            </ol>
          </Card>

          <Card title={`Conversation history (${candidate.interactions.length})`}>
            {candidate.interactions.length === 0 ? (
              <p className="text-sm text-slate-600">
                No interactions logged. Nobody has been in touch with this candidate.
              </p>
            ) : (
              <ul className="space-y-3">
                {candidate.interactions.map((i) => {
                  const inbound = i.direction === "inbound";
                  return (
                    <li
                      key={i.id}
                      className={`rounded border p-3 text-sm ${
                        inbound
                          ? "border-blue-300 bg-blue-50"
                          : "border-slate-200 bg-slate-50 ml-8"
                      }`}
                    >
                      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-slate-600">
                        <span className="font-semibold uppercase">
                          {inbound ? "Candidate" : "Recruiter"}
                        </span>
                        <span>{titleCase(i.channel)}</span>
                        <span>{formatDateTime(i.occurred_at)}</span>
                        <span>· {i.created_by}</span>
                      </div>
                      <p className="whitespace-pre-wrap">{i.content}</p>

                      {/* Call notes carry the recruiter's structured read. It
                          only exists for calls, so it is only rendered there. */}
                      {i.channel === "call" && (
                        <div className="mt-2 flex flex-wrap gap-2 border-t border-slate-200 pt-2">
                          <Chip>
                            blocker:{" "}
                            {i.blocker_raised && i.blocker_category
                              ? titleCase(i.blocker_category)
                              : "none"}
                          </Chip>
                          {i.recruiter_read && (
                            <Chip>read: {titleCase(i.recruiter_read)}</Chip>
                          )}
                          {i.date_confirmed !== null && (
                            <Chip>
                              start date confirmed: {i.date_confirmed ? "yes" : "no"}
                            </Chip>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          <AIPanel candidateId={candidate.id} latest={candidate.latest_ai_analysis} />

          <Card title="Open actions">
            {candidate.open_actions.length === 0 ? (
              <p className="text-sm text-slate-600">No open actions.</p>
            ) : (
              <ul className="space-y-2 text-sm">
                {candidate.open_actions.map((a) => (
                  <li key={a.id} className="rounded border border-slate-200 p-2">
                    <div className="font-medium">{a.title}</div>
                    <div className="mt-1 text-xs uppercase text-slate-500">
                      {a.priority} · {a.rule_key ?? a.source}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <NotesForm candidateId={candidate.id} notes={candidate.notes} />
          <InteractionForm candidateId={candidate.id} />
        </div>
      </div>
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase text-slate-500">{label}</dt>
      <dd className="break-words">{value}</dd>
    </div>
  );
}
