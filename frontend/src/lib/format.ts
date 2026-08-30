import type { EngagementStatus, RiskLevel } from "./types";

/** Whole days from today to `date`. Negative once the date has passed. */
export function daysUntil(date: string): number {
  const target = new Date(`${date}T00:00:00Z`).getTime();
  const today = new Date();
  const todayUtc = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((target - todayUtc) / 86_400_000);
}

/**
 * Days of silence, matching risk_service.days_since_contact: a candidate
 * nobody has ever contacted counts from their offer date, because the clock
 * starts when we made the offer, not when we first bothered to call.
 * Showing a dash there would hide the worst cases.
 */
export function daysSinceContact(
  lastInteractionAt: string | null,
  offerDate: string,
): number {
  const from = lastInteractionAt ? new Date(lastInteractionAt) : new Date(`${offerDate}T00:00:00Z`);
  const today = new Date();
  const todayUtc = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  const fromUtc = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate());
  return Math.max(0, Math.round((todayUtc - fromUtc) / 86_400_000));
}

/**
 * The band a rule-floor score falls in.
 *
 * A knowing duplication of risk_service.BAND_RANGES (LOW 0-39, MEDIUM 40-69,
 * HIGH 70-100). The API returns risk_score_base as a bare number, so without
 * this the detail page can show "rule floor 50.1" but not "rule floor:
 * medium" — and the whole point of displaying the floor beside the badge is to
 * make `final = max(base, ai)` readable at a glance. The alternative is a new
 * backend field; this is three lines and the bands have never moved.
 */
export function bandForScore(score: number): RiskLevel {
  if (score >= 70) return "high";
  if (score >= 40) return "medium";
  return "low";
}

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  return d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  });
}

export function titleCase(value: string): string {
  return value
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export const ENGAGEMENT_STATUSES: EngagementStatus[] = [
  "offer_accepted",
  "welcome_sent",
  "documentation",
  "manager_intro",
  "team_context",
  "relocation_check",
  "pre_joining_checkin",
  "joined",
  "dropped_out",
];

export const RISK_LEVELS: RiskLevel[] = ["low", "medium", "high"];

/** Months from three back to nine ahead, for the joining-month filter. */
export function joiningMonthOptions(): string[] {
  const now = new Date();
  const months: string[] = [];
  for (let offset = -3; offset <= 9; offset += 1) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + offset, 1));
    months.push(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  return months;
}
