# CLAUDE.md — Post-Offer Engagement Platform

Read this file before every task. Do not deviate from the schema, endpoint
contracts, or AI output shapes defined here. If something is genuinely
underspecified, ask me before inventing an answer.

## The problem, in one paragraph

A candidate accepts an offer, then serves 30–90 days of notice before they
actually join. In that gap nothing binds them to the company, and a large
share simply never show up. The recruiter usually can't see it coming,
because the candidate went quiet and nobody noticed the silence. We are
building a triage system: it turns 40 pending joiners into a ranked list of
the five worth a phone call this morning, with the reasoning attached.

**The AI never contacts the candidate.** It decides who needs attention,
explains why using the candidate's own words as evidence, and drafts a
message the recruiter edits and sends. The human stays in the loop
deliberately — an obviously automated nudge to a wavering candidate makes
things worse, not better.

## Grading reality

Backend 25, AI 25, Frontend 20, HR/Product 15, Analytics 10, Maturity 5.
UI polish is explicitly secondary — but Frontend's 20 points cover
usability, state/API handling and component structure, which are not
aesthetics. Never spend effort on animation or custom design systems.

## Stack (locked — do not substitute)

- Backend: Python 3.11, FastAPI, SQLAlchemy 2.x, Pydantic v2
- DB: PostgreSQL 16. Schema via `Base.metadata.create_all()` on startup
  (README notes Alembic as the production choice)
- Frontend: Next.js App Router, TypeScript, Tailwind (default palette only)
- Data fetching: Server Components + plain fetch for analytics and candidate
  detail; client components + TanStack Query for the dashboard and all AI
  mutations (needs cached filter state and `keepPreviousData`)
- LLM: Gemini Flash via `google-genai`, behind an `LLMProvider` interface so
  Groq can be swapped via env var
- Scheduler: APScheduler in-process
- Auth: **none**. A recruiter-switcher dropdown in the header sets the
  `actor` field for the audit log. No login, no JWT.
- Docker Compose: api, db, web

## Module 1 — Data foundation

UUID primary keys. `created_at` and `updated_at` on every table.

**recruiters** — `id, name, email`

**candidates**
`id, name, email, phone, role, department, location, offer_date,
joining_date, recruiter_id, engagement_status, last_interaction_at,
risk_level, risk_source, risk_score_base, notes, final_outcome`

- `engagement_status` enum: `offer_accepted, welcome_sent, documentation,
  manager_intro, team_context, relocation_check, pre_joining_checkin,
  joined, dropped_out`
- `risk_level` enum: `low, medium, high`
- `risk_source` enum: `rule, ai, hr_override`
- `final_outcome` enum: `pending, joined, dropped_out`

**journey_stages** — the workflow template, seeded, not hardcoded.
`id, key, label, sequence_order, anchor, offset_days, is_active`

- `anchor` enum: `offer` | `joining`

Seeded rows (this exact set — six from the brief plus two we added to cover
the dead zone in the middle of the notice period):

| order | key | label | anchor | offset |
|---|---|---|---|---|
| 1 | offer_accepted | Offer accepted | offer | 0 |
| 2 | welcome | Welcome | offer | 1 |
| 3 | documentation | Documentation | offer | 3 |
| 4 | manager_intro | Manager introduction | offer | 21 |
| 5 | team_context | Team & role context | offer | 35 |
| 6 | relocation_check | Relocation & logistics check | joining | -25 |
| 7 | pre_joining_checkin | Pre-joining check-in | joining | -10 |
| 8 | joining | Joining | joining | 0 |

**candidate_stages** — materialised per candidate on creation.
`id, candidate_id, stage_id, due_date, status, completed_at, completed_by`

- `status` enum: `pending, in_progress, completed, skipped`
- Unique on `(candidate_id, stage_id)`

**Due-date computation.** Offer-anchored stages compute from `offer_date`,
joining-anchored from `joining_date`. On a short notice period these
collide — a 30-day notice makes `team_context` (offer+35) land after
`relocation_check` (joining-25). Detect the collision and compress the
offer-anchored stages proportionally into the available window, preserving
order. Never emit a stage sequence whose due dates go backwards. Put this
in a pure, unit-tested function.

**interactions**
`id, candidate_id, channel, direction, content, occurred_at, created_by,
blocker_raised, blocker_category, date_confirmed, recruiter_read`

- `channel` enum: `email, whatsapp, call, in_person`
- `direction` enum: `inbound, outbound`
- The last four fields apply to call notes: the recruiter's structured read
  captured at the only moment it exists, right after the call.
  `recruiter_read` enum: `on_track, unsure, worried`, nullable.
  `blocker_category` enum: `relocation, notice_period, counter_offer,
  compensation, role_scope, personal, none`, nullable.
- Writing an interaction updates `candidates.last_interaction_at` in the
  same transaction.

**ai_analyses** — every call persisted, never just returned.
`id, candidate_id, analysis_type, model_name, prompt_version, raw_response,
parsed_output (JSONB), risk_level, confidence, validation_status,
latency_ms, was_fallback, created_at`

- `validation_status` enum: `valid, repaired, failed`

**follow_up_actions**
`id, candidate_id, title, description, due_date, priority, status, source,
generated_message, rule_key, created_at, completed_at`

- `priority` enum: `low, medium, high, urgent`; `status` enum: `open, done,
  dismissed`; `source` enum: `automation, ai, manual`

**audit_log**
`id, entity_type, entity_id, action, actor, before (JSONB), after (JSONB),
created_at`. Written on every candidate update, stage transition, and AI
override.

Indexes: `candidates(joining_date)`, `candidates(risk_level)`,
`candidates(recruiter_id)`, `candidates(engagement_status)`,
`interactions(candidate_id, occurred_at DESC)`.

## Module 2 — Core APIs

All under `/api/v1`. List endpoints paginate and return
`{items, total, limit, offset}`. No business logic in routers — routers call
services.

```
GET    /candidates          filters: joining_month, recruiter_id, role,
                            risk_level, engagement_status, search,
                            joining_within_days
POST   /candidates          materialises the 8 candidate_stages rows
GET    /candidates/{id}     candidate + stages + interactions + latest AI
                            analysis + open actions
PATCH  /candidates/{id}     partial; risk_level here sets risk_source='hr_override'
GET    /candidates/{id}/stages
POST   /candidates/{id}/stages/{stage_id}/complete
GET    /candidates/{id}/interactions
POST   /candidates/{id}/interactions
GET    /recruiters
GET    /follow-up-actions   filters: status, recruiter_id
PATCH  /follow-up-actions/{id}
```

One global exception handler returning `{error: {code, message, details}}`.
422 validation, 404 missing, 503 LLM provider failure. Never leak stack traces.

## Module 3 — Interaction log

Covered by the endpoints above, but note the intent: this is the raw
material the AI reads. Seed data quality here determines whether the whole
demo works.

## Module 4 — Risk engine

Two layers. Rules set a floor, AI may raise it, HR overrides everything.

```
base = f(days_to_joining, days_since_contact, stage_lag)   # SQL, deterministic
final = max(base, ai_assessment)                           # AI can only raise
```

Rules catch the silent candidates even when the API is down. The AI catches
the ones who reply promptly and are still leaving. Store `risk_score_base`
so the rule floor is visible and explainable in the UI.

## Module 5 — AI service

Four functions in `ai/engine.py`: `assess_risk`, `summarize_interactions`,
`recommend_next_action`, `draft_message`. Each one:

1. Builds a prompt from a versioned template with real context — role,
   days to joining, stage progress, last N interactions.
2. Requests JSON only via the provider's structured-output mode.
3. Parses into a Pydantic contract.
4. On `ValidationError`: retry **once** with the error appended as a repair
   instruction. Mark `validation_status='repaired'`.
5. On second failure: return a deterministic rule-based fallback, set
   `was_fallback=True`, return 200 with the flag. **The app must never break
   because the LLM misbehaved.**
6. Persist every attempt to `ai_analyses` either way.

Prompt must weight verbatim inbound candidate messages above recruiter call
notes — the latter are second-hand paraphrase.

Contracts (`ai/contracts.py`):

```python
class RiskAssessment(BaseModel):
    risk_level: Literal["low","medium","high"]
    confidence: float = Field(ge=0, le=1)
    signals: list[str] = Field(max_length=5)   # evidence quoted from interactions
    reasoning: str = Field(max_length=500)
    concern_category: Literal["relocation","notice_period","counter_offer",
                              "compensation","role_scope","personal","none"]

class NextAction(BaseModel):
    action_type: Literal["send_message","schedule_call","escalate_to_manager",
                         "send_documents","no_action_needed"]
    channel: Literal["email","whatsapp","call"]
    urgency: Literal["low","medium","high"]
    rationale: str
    suggested_timing_days: int = Field(ge=0, le=30)

class DraftedMessage(BaseModel):
    channel: Literal["email","whatsapp"]
    subject: str | None      # required if email, must be None if whatsapp
    body: str
    tone: Literal["warm","formal","casual"]
    personalization_used: list[str]

class InteractionSummary(BaseModel):
    summary: str = Field(max_length=800)
    key_concerns: list[str]
    sentiment: Literal["positive","neutral","concerned","negative"]
    unresolved_items: list[str]
```

Add a model validator enforcing the subject/channel rule and a WhatsApp
body length cap. Guardrails: strip anything the model invents about
compensation or start-date changes, cap output tokens, log `prompt_version`.

## Module 6 — Automation

`automation_service.run_engagement_sweep()`, nightly via APScheduler plus
`POST /automation/run` so it is demoable.

- **Rule `imminent_silence`**: `final_outcome='pending'` AND
  `joining_date <= now + 7d` AND (`last_interaction_at <= now - 5d` OR NULL)
  → raise risk, run assessment, draft message, create follow-up action.
- **Rule `stage_stall`**: a `candidate_stages` row past its `due_date` still
  `pending` → flag stall, create action.
- Idempotent: no second open action for the same candidate + `rule_key`
  within 24h.
- Sending is simulated: store `generated_message`, log it, do not send.

## Module 7 — Recruiter interface

- **Dashboard**: table of candidates, five filters, risk badge, days to
  joining, days since contact, next action. Client component, TanStack Query.
- **Candidate detail**: offer details, stage timeline with due dates,
  conversation history, AI summary **with its evidence quotes visible**,
  current risk with the rule floor shown alongside, override control sitting
  next to the badge, recruiter notes. A risk badge without visible reasoning
  reads as magic — show the evidence.
- **Action queue**: open follow-ups across all candidates.

## Module 8 — Analytics

All aggregation in SQL, not Python loops.

- `GET /analytics/overview` — total offered, joined, dropped, offer-to-join
  conversion %, joining in next 7/15/30 days, high-risk count, average days
  between interactions
- `GET /analytics/pipeline` — per-stage counts and drop-off. Define drop-off
  as candidates who reached the stage and did not advance; state the
  definition in the README.
- `GET /analytics/recruiters` — per-recruiter offers, joins, conversion rate,
  average engagement frequency

## Seed data

50+ candidates. Realistic or the analytics look fake:
- Joining dates spread across the next 60 days, some past
- Notice periods varying 30/60/90 days so the stage-compression path is
  actually exercised
- Stage progress consistent with how long ago the offer was
- 8–10 clearly high-risk candidates with **hand-written** inbound messages
  covering each blocker category — relocation, notice-period dispute,
  counter-offer, hedging language, and total silence
- 5–6 recruiters, 8–10 roles, 4–5 locations
- Some `joined`, a few `dropped_out`, so conversion is not 100%
- 2–6 interactions each, timestamps consistent with `last_interaction_at`

## Non-negotiables

- Secrets via env vars. Commit `.env.example`, never `.env`.
- `docker compose up` brings up db + api + web and seeds automatically.
- Type hints everywhere. Pydantic validation on every request body.
- Tests: stage due-date computation including the compression edge case,
  AI contract validation + repair + fallback, the automation date logic,
  the conversion-rate calculation. Depth over count.

## README must answer

Architecture and schema · AI flow and how structured output is validated ·
how risk classification works **and its limitations** (no ground-truth
labels so accuracy is unmeasurable; silence is ambiguous; candidates are
strategically polite; the strongest predictor — a competing offer — is
invisible; tuned to over-flag deliberately because a false positive costs
one phone call and a false negative costs a hire) · how the automation
works · why two stages were added to the journey · key trade-offs · what
changes at 1 million candidates (partition by joining_date, read replicas,
AI calls to a queue with workers, analytics in a rollup table, batched
nightly sweep, cursor pagination).
