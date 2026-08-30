import { Card } from "@/components/ui";
import { serverFetch } from "@/lib/api";
import type {
  AnalyticsOverview,
  AnalyticsPipeline,
  AnalyticsRecruiters,
} from "@/lib/types";

// Server Component with plain fetch, per CLAUDE.md's data-fetching split.
// Rendered per request so the numbers are live, not baked into the image.
export const dynamic = "force-dynamic";

export default async function AnalyticsPage() {
  // Three independent aggregates — fetched in parallel rather than awaited in
  // sequence, so the page costs one round trip rather than three.
  const [overview, pipeline, recruiters] = await Promise.all([
    serverFetch<AnalyticsOverview>("/analytics/overview"),
    serverFetch<AnalyticsPipeline>("/analytics/pipeline"),
    serverFetch<AnalyticsRecruiters>("/analytics/recruiters"),
  ]);

  const pct = (value: number | null) => (value === null ? "—" : `${value}%`);
  const num = (value: number | null) => (value === null ? "—" : String(value));

  // Widest stage sets the bar scale, so the funnel is comparable across rows.
  const widest = Math.max(
    1,
    ...pipeline.items.map((s) => s.completed + s.pending),
  );

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Analytics</h1>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">Overview</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          <Metric label="Total offered" value={String(overview.total_offered)} />
          <Metric label="Joined" value={String(overview.joined)} />
          <Metric label="Dropped out" value={String(overview.dropped_out)} />
          <Metric label="Pending" value={String(overview.pending)} />
          <Metric
            label="Offer → join conversion"
            value={pct(overview.offer_to_join_conversion_pct)}
            hint="Resolved candidates only (joined + dropped out). Pending are excluded."
          />
          <Metric label="Joining ≤ 7 days" value={String(overview.joining_next_7_days)} />
          <Metric label="Joining ≤ 15 days" value={String(overview.joining_next_15_days)} />
          <Metric label="Joining ≤ 30 days" value={String(overview.joining_next_30_days)} />
          <Metric label="High risk" value={String(overview.high_risk_count)} />
          <Metric label="Medium risk" value={String(overview.medium_risk_count)} />
          <Metric
            label="Avg days between interactions"
            value={num(overview.avg_days_between_interactions)}
            hint="Across pending candidates with at least two interactions."
          />
          <Metric label="Open actions" value={String(overview.open_follow_up_actions)} />
        </div>
      </section>

      <Card title="Pipeline">
        <p className="mb-3 text-sm text-slate-600">
          Drop-off counts candidates whose <strong>furthest completed stage</strong> was this
          one and who then dropped out — where in the journey we lost them. Each dropped-out
          candidate is counted once. Completed, pending and stalled count stage rows, so they
          are not comparable to drop-off.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-600">
              <tr>
                <th className="px-2 py-2">#</th>
                <th className="px-2 py-2">Stage</th>
                <th className="px-2 py-2">Completed</th>
                <th className="px-2 py-2">Pending</th>
                <th className="px-2 py-2">Stalled</th>
                <th className="px-2 py-2">Drop-off</th>
                <th className="px-2 py-2 w-1/3">Progress</th>
              </tr>
            </thead>
            <tbody>
              {pipeline.items.map((stage) => (
                <tr key={stage.stage_key} className="border-b border-slate-100">
                  <td className="px-2 py-2 text-slate-500">{stage.sequence_order}</td>
                  <td className="px-2 py-2 font-medium">{stage.stage_label}</td>
                  <td className="px-2 py-2">{stage.completed}</td>
                  <td className="px-2 py-2">{stage.pending}</td>
                  <td className="px-2 py-2">
                    {stage.stalled > 0 ? (
                      <span className="font-semibold text-red-700">{stage.stalled}</span>
                    ) : (
                      0
                    )}
                  </td>
                  <td className="px-2 py-2">{stage.drop_off}</td>
                  <td className="px-2 py-2">
                    {/* Plain CSS bars — no charting library, per the brief. */}
                    <div className="flex h-4 w-full overflow-hidden rounded bg-slate-100">
                      <div
                        className="bg-green-500"
                        style={{ width: `${(stage.completed / widest) * 100}%` }}
                        title={`${stage.completed} completed`}
                      />
                      <div
                        className="bg-slate-400"
                        style={{ width: `${(stage.pending / widest) * 100}%` }}
                        title={`${stage.pending} pending`}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-slate-600">
          {pipeline.total_dropped_out} dropped out in total;{" "}
          {pipeline.dropped_out_before_any_stage} of them before completing any stage.
        </p>
      </Card>

      <Card title="Recruiters">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-600">
              <tr>
                <th className="px-2 py-2">Recruiter</th>
                <th className="px-2 py-2">Offers</th>
                <th className="px-2 py-2">Joined</th>
                <th className="px-2 py-2">Dropped</th>
                <th className="px-2 py-2">Pending</th>
                <th className="px-2 py-2">Conversion</th>
                <th className="px-2 py-2">High risk</th>
                <th className="px-2 py-2">Avg days since contact</th>
              </tr>
            </thead>
            <tbody>
              {recruiters.items.map((r) => (
                <tr key={r.recruiter_id} className="border-b border-slate-100">
                  <td className="px-2 py-2 font-medium">{r.recruiter_name}</td>
                  <td className="px-2 py-2">{r.total_offers}</td>
                  <td className="px-2 py-2">{r.joined}</td>
                  <td className="px-2 py-2">{r.dropped_out}</td>
                  <td className="px-2 py-2">{r.pending_count}</td>
                  <td className="px-2 py-2">
                    {r.conversion_pct === null ? (
                      <span className="text-slate-500" title="Nothing has resolved yet">
                        no data
                      </span>
                    ) : (
                      `${r.conversion_pct}%`
                    )}
                  </td>
                  <td className="px-2 py-2">{r.high_risk_count}</td>
                  <td className="px-2 py-2">{num(r.avg_days_since_last_contact)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded border border-slate-300 bg-white p-3">
      <div className="text-xs uppercase text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}
