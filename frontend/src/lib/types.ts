// Mirrors the Pydantic response models in backend/app/schemas.py.
//
// Hand-written rather than generated from the OpenAPI schema: a generator is
// the right answer on a long-lived codebase, but it needs a build step that
// runs against a live API, and a frontend that cannot be built without the
// backend running is a worse trade here than a file that has to be kept in
// step by hand.

export type RiskLevel = "low" | "medium" | "high";
export type RiskSource = "rule" | "ai" | "hr_override";
export type FinalOutcome = "pending" | "joined" | "dropped_out";
export type EngagementStatus =
  | "offer_accepted"
  | "welcome_sent"
  | "documentation"
  | "manager_intro"
  | "team_context"
  | "relocation_check"
  | "pre_joining_checkin"
  | "joined"
  | "dropped_out";
export type StageStatus = "pending" | "in_progress" | "completed" | "skipped";
export type InteractionChannel = "email" | "whatsapp" | "call" | "in_person";
export type InteractionDirection = "inbound" | "outbound";
export type RecruiterRead = "on_track" | "unsure" | "worried";
export type BlockerCategory =
  | "relocation"
  | "notice_period"
  | "counter_offer"
  | "compensation"
  | "role_scope"
  | "personal"
  | "none";
export type FollowUpPriority = "low" | "medium" | "high" | "urgent";
export type FollowUpStatus = "open" | "done" | "dismissed";
export type ValidationStatus = "valid" | "repaired" | "failed";

export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Recruiter {
  id: string;
  name: string;
  email: string;
}

export interface Candidate {
  id: string;
  name: string;
  email: string;
  phone: string | null;
  role: string;
  department: string;
  location: string;
  offer_date: string;
  joining_date: string;
  recruiter_id: string;
  engagement_status: EngagementStatus;
  last_interaction_at: string | null;
  risk_level: RiskLevel;
  risk_source: RiskSource;
  risk_score_base: number;
  notes: string | null;
  final_outcome: FinalOutcome;
}

export interface CandidateStage {
  id: string;
  stage_id: string;
  stage_key: string;
  stage_label: string;
  sequence_order: number;
  anchor: "offer" | "joining";
  due_date: string;
  status: StageStatus;
  completed_at: string | null;
  completed_by: string | null;
}

export interface Interaction {
  id: string;
  channel: InteractionChannel;
  direction: InteractionDirection;
  content: string;
  occurred_at: string;
  created_by: string;
  blocker_raised: boolean;
  blocker_category: BlockerCategory | null;
  date_confirmed: boolean | null;
  recruiter_read: RecruiterRead | null;
}

export interface FollowUpAction {
  id: string;
  candidate_id: string;
  title: string;
  description: string | null;
  due_date: string | null;
  priority: FollowUpPriority;
  status: FollowUpStatus;
  source: "automation" | "ai" | "manual";
  generated_message: string | null;
  rule_key: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface AIAnalysis {
  id: string;
  analysis_type: string;
  model_name: string;
  prompt_version: string;
  parsed_output: Record<string, unknown>;
  risk_level: RiskLevel | null;
  confidence: number;
  validation_status: ValidationStatus;
  latency_ms: number;
  was_fallback: boolean;
  created_at: string;
}

export interface CandidateDetail extends Candidate {
  stages: CandidateStage[];
  interactions: Interaction[];
  latest_ai_analysis: AIAnalysis | null;
  open_actions: FollowUpAction[];
}

// --- AI contracts ----------------------------------------------------------

export interface AIMeta {
  analysis_id: string;
  analysis_type: string;
  model_name: string;
  prompt_version: string;
  validation_status: ValidationStatus;
  was_fallback: boolean;
  latency_ms: number;
  confidence: number;
  created_at: string;
}

export interface RiskAssessment {
  risk_level: RiskLevel;
  confidence: number;
  signals: string[];
  reasoning: string;
  concern_category: BlockerCategory;
}

export interface RiskApplication {
  rule_floor_level: RiskLevel;
  rule_floor_score: number;
  ai_level: RiskLevel;
  final_level: RiskLevel;
  risk_source: RiskSource;
  raised_by_ai: boolean;
  applied: boolean;
  note: string;
}

export interface InteractionSummary {
  summary: string;
  key_concerns: string[];
  sentiment: "positive" | "neutral" | "concerned" | "negative";
  unresolved_items: string[];
}

export interface NextAction {
  action_type: string;
  channel: string;
  urgency: string;
  rationale: string;
  suggested_timing_days: number;
}

export interface DraftedMessage {
  channel: "email" | "whatsapp";
  subject: string | null;
  body: string;
  tone: string;
  personalization_used: string[];
}

export interface AssessRiskResponse {
  meta: AIMeta;
  assessment: RiskAssessment;
  risk: RiskApplication;
}
export interface SummarizeResponse {
  meta: AIMeta;
  summary: InteractionSummary;
}
export interface RecommendActionResponse {
  meta: AIMeta;
  recommendation: NextAction;
}
export interface DraftMessageResponse {
  meta: AIMeta;
  draft: DraftedMessage;
  guardrails_removed: string[];
}

// --- Automation + analytics ------------------------------------------------

export interface AutomationRuleOutcome {
  matched: number;
  actions_created: number;
  skipped_existing_action: number;
}

export interface AutomationRunResponse {
  started_at: string;
  duration_ms: number;
  candidates_scanned: number;
  actions_created: number;
  rules: Record<string, AutomationRuleOutcome>;
  ai_calls: number;
  ai_fallbacks: number;
  messages_simulated: number;
  errors: number;
}

export interface AnalyticsOverview {
  total_offered: number;
  joined: number;
  dropped_out: number;
  pending: number;
  offer_to_join_conversion_pct: number | null;
  joining_next_7_days: number;
  joining_next_15_days: number;
  joining_next_30_days: number;
  high_risk_count: number;
  medium_risk_count: number;
  avg_days_between_interactions: number | null;
  open_follow_up_actions: number;
}

export interface PipelineStage {
  stage_key: string;
  stage_label: string;
  sequence_order: number;
  completed: number;
  pending: number;
  stalled: number;
  drop_off: number;
}

export interface AnalyticsPipeline {
  items: PipelineStage[];
  total_dropped_out: number;
  dropped_out_before_any_stage: number;
}

export interface RecruiterStats {
  recruiter_id: string;
  recruiter_name: string;
  total_offers: number;
  joined: number;
  dropped_out: number;
  pending_count: number;
  conversion_pct: number | null;
  high_risk_count: number;
  avg_days_since_last_contact: number | null;
}

export interface AnalyticsRecruiters {
  items: RecruiterStats[];
}
