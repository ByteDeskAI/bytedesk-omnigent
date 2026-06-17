# ByteDesk Omnigent Agent Workflow Suite

> Generated for **BDP-2180** (epic) from the design workflow `wf_387899a3-7cd`, grounded in
> the live agent roster, the ByteDesk Business Context, and the existing OpenClaw plugins.
> 45 workflows across 9 departments. Each workflow is an omnigent **orchestrator agent**
> (`deploy/bytedesk/agents/<id>/config.yaml`) that delegates to the team agents as sub-agents
> via the **inbox pattern**; deterministic tool steps are gated (commented) until the omnigent
> tool layer is wired (BDP-2182 platform/team MCP, BDP-2183 Google Workspace MCP, BDP-2184 flip-on).
> Run any of them: `omnigent run deploy/bytedesk/agents/<id> --server $OMNIGENT_SERVER_URL -p "..."`
> or from the SPA picker.

# ByteDesk Omnigent Agent-Workflow Master Catalog & Build Plan

**Mission:** Multi-tenant "digital success" SaaS for SMBs + the agencies serving them (GoHighLevel category), owning its data plane (CNPG Postgres + ByteDesk.Realtime) as the moat. Two sharpest wedges: **DevProjects** (AI web-app builder) + **SEO/GEO tooling**. Stage: pre-revenue ($0 MRR, 0 customers). 30-day objective: **land the first paying customer** via DevProjects + SEO and get new signups to first value.

**Omnigent model (no DSL/engine):** every "workflow" is an **orchestrator agent bundle** (`deploy/bytedesk/agents/<id>/config.yaml`, `spec_version 1`, harness `claude-sdk`) whose prompt **decomposes** a task and **delegates** to sub-agents via the **inbox pattern** (`sys_session_send` to fan out in parallel, end the turn, get woken, `sys_read_inbox` to drain, then synthesize). Deterministic steps are **tools** (`type:mcp` or `type:function`). Guardrails are **nessie policies** (`allowed_subagents`, `spawn_bounds`, `blast_radius`).

**Grounding (verified in-repo):** all 31 bundles are already scaffolded under `deploy/bytedesk/agents/*/config.yaml` but are skeletons — orchestration prompts, sub-agent allow-lists, and tool blocks are mostly unwritten. The chief-of-staff bundle already carries the platform-action `type:mcp` block **commented out**, gated behind `${BYTEDESK_MCP_TOKEN}` because the omnigent parser **raises on an unresolved `${VAR}` at deploy**. `ByteDesk.OpenClawMcpProxy` (BDP-447) exists as the proven per-agent-JWT template, and `McpOAuthApplicationSeeder` lives in `ByteDesk.Identity`.

---

## Department: Revenue

### `target-account-intel-pack` — Target-Account Intelligence Pack
- **One-liner:** Marshal Client Research to produce sales-ready, ICP-scored account dossiers (fit, pains, DevProjects/SEO hook, contacts) that feed the demo pipeline.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** client-research-lead, icp-scoring-analyst, outreach-intelligence-specialist, prospect-source-mapper
- **Tools:** prospect_research_config_materialize, office_customer_lookup, GWS Sheets create/values-update, GWS Docs create, Drive share-internal, Confluence page_create, Jira issue_create, team find_specialist
- **Trigger:** weekly pipeline-replenish, or on-demand when the demo pipeline drops below 5
- **Cadence:** weekly
- **Mission tie-in:** feeds the 5 DevProjects/SEO demos-or-trials with qualified, ICP-fit accounts.
- **Deliverable:** ranked target-account Sheet + per-account dossier Docs + Confluence index; top accounts auto-create Jira deal-prep tasks.

### `demo-to-close-pipeline` — Demo-to-Close Pipeline (DevProjects + SEO Wedge)
- **One-liner:** Turn a researched, ICP-scored account into a booked demo, a grounded proposal, objection-handling answers, and the first charged Stripe subscription — in one orchestrated motion.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** icp-scoring-analyst, product-ops-director, seo-geo-growth-lead, lifecycle-email-marketing-lead, revops-and-kpi-manager, customer-success-and-support-lead
- **Tools:** office_customer_lookup, GWS calendar_freebusy / meeting_schedule / meet_space_create / slides_create / docs_template_merge / gmail_draft_create, Jira issue_create/transition, Confluence page_create, platform-actions (Stripe), ops_ledger_query
- **Trigger:** an account reaches qualified status (or Marcus/Maya flags a hot inbound)
- **Cadence:** event
- **Mission tie-in:** directly moves first-customer=1 and MRR~$100; produces the demos/trials.
- **Deliverable:** per-account deal packet — scheduled Meet demo + tailored Slides deck, product-truth-grounded proposal Doc, objection Q&A Confluence page, Jira deal issue, drafted Stripe-checkout follow-up email.

### `revops-kpi-cockpit` — RevOps KPI Cockpit (30-Day Single-Source-of-Truth)
- **One-liner:** Adrian compiles one trustworthy daily dashboard of the exact 30-day numbers and flags drift before it costs the close.
- **Orchestrator:** revops-and-kpi-manager (Adrian Cross)
- **Sub-agents:** icp-scoring-analyst, customer-success-and-support-lead, lifecycle-email-marketing-lead, delivery-manager
- **Tools:** ops_scoreboard_get, ops_ledger_query, office_customer_lookup, platform-actions, GWS Sheets create/values-update, GWS chat_send_internal, office_dm_post, Jira issue_create, Confluence page_create
- **Trigger:** daily scheduled (and manual before any founder review)
- **Cadence:** daily
- **Mission tie-in:** makes every 30-day metric crisply defined and trusted so the team steers by signal.
- **Deliverable:** single-source KPI Sheet + daily Confluence snapshot vs target + Chat/DM digest to Marcus and founders + auto-filed Jira task for any drift.

### `stripe-go-live-billing` — Stripe Go-Live & First-Charge Readiness
- **One-liner:** Get billing live end to end so the moment a demo converts there is zero friction between yes and a charged subscription.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** revops-and-kpi-manager, backend-development-lead, quality-and-release-lead, customer-success-and-support-lead
- **Tools:** platform-actions (Stripe plan/price/checkout), Jira issue_create/transition/comment, repo pr_view/pr_list, Confluence page_create, GWS Docs create, native delegate (sys_session_send)
- **Trigger:** run once now as a hard go-live gate; re-run when a deal nears close
- **Cadence:** on-demand
- **Mission tie-in:** unblocks first-customer=1 and MRR~$100 by making "Stripe live" true. **Doubly gated:** billing integration must exist AND the MCP must expose plan/price/checkout.
- **Deliverable:** verified live Stripe path (plan/price confirmed via platform MCP, working checkout link, Confluence billing runbook, QA-signed first-charge smoke, Jira go-live ticket -> Done with evidence).

### `trial-activation-first-value` — Trial Activation & First-Value Saver
- **One-liner:** Watch every new signup/trial, shepherd to first value (deployed DevProject or real SEO win) inside week one, intervene on stalls so activation clears 50% with zero early churn.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** customer-success-and-support-lead, delivery-manager, devprojects-support-resolver, lifecycle-email-marketing-lead, revops-and-kpi-manager
- **Tools:** office_customer_lookup, platform-actions, devproject_work_submit, ops_scoreboard_get, GWS gmail_draft_create / calendar_event_create, office_dm_post, Jira issue_create/transition, Confluence page_create
- **Trigger:** each new trial signup + daily sweep of trials with no first-value milestone
- **Cadence:** daily
- **Mission tie-in:** week-1 activation >=50%, 0 early churn, deploy success >=80%; converts trials to first paid.
- **Deliverable:** per-trial activation tracker, personalized first-value nudge email + check-in, auto-filed Jira task for any stall routed to the right resolver, weekly Confluence activation-cohort report.

---

## Department: Client Research

### `icp-prospect-pipeline` — ICP-Scored Prospect Pipeline
- **One-liner:** Owen fans source mapping, ICP scoring, and outreach intel in parallel to turn raw account lists into a ranked, wedge-matched shortlist with evidence.
- **Orchestrator:** client-research-lead (Owen Carter)
- **Sub-agents:** prospect-source-mapper, icp-scoring-analyst, outreach-intelligence-specialist
- **Tools:** prospect_research_config_materialize, office_customer_lookup, GWS Sheets create/values-update, Drive share-internal, ops_ledger_query
- **Trigger:** weekly cadence, or on-demand when Marcus requests a fresh batch for a wedge
- **Cadence:** weekly
- **Mission tie-in:** feeds the 5 demos-or-trials with qualified wedge-matched accounts.
- **Deliverable:** ranked ICP shortlist (transparent per-account scores, evidence vs assumptions vs unknowns) materialized to a Sheet and logged to the ops ledger, handed to Revenue.

### `wedge-fit-account-brief` — Wedge-Fit Sales-Ready Account Brief
- **One-liner:** For each top-scored account, deep wedge diagnosis (weak/missing site -> DevProjects, poor discoverability -> SEO) plus a reachable-buyer map, synthesized to a one-page brief.
- **Orchestrator:** client-research-lead (Owen Carter)
- **Sub-agents:** icp-scoring-analyst, outreach-intelligence-specialist, seo-geo-growth-lead
- **Tools:** office_customer_lookup, GWS docs_template_merge / docs_create, drive_replicate_template / drive_share_internal, office_dm_post
- **Trigger:** an account crosses the ICP threshold, or Marcus flags one before a demo
- **Cadence:** event
- **Mission tie-in:** arms the demos/trials motion with the exact wedge + proof per account.
- **Deliverable:** one-page sales-ready brief per account from a Doc template, DM'd to Marcus/Owen.

### `source-map-coverage-sweep` — Prospect Source-Map Coverage Sweep
- **One-liner:** Lena-led discovery of new prospect source lists/channels, deduped against existing customers and prior research, with coverage gaps recorded honestly.
- **Orchestrator:** client-research-lead (Owen Carter)
- **Sub-agents:** prospect-source-mapper, outreach-intelligence-specialist
- **Tools:** prospect_research_config_materialize, office_customer_lookup, ops_ledger_query, GWS Sheets create/values-update, Confluence page_create
- **Trigger:** start-of-week planning, or when the ICP pipeline runs low
- **Cadence:** weekly
- **Mission tie-in:** keeps top-of-funnel supplied with net-new deduped candidate accounts.
- **Deliverable:** updated source-map Sheet of net-new candidates + Confluence coverage-and-gaps note recording where data is missing rather than fabricated.

### `research-triage-handoff` — Research-to-Revenue Triage & Handoff
- **One-liner:** Owen converts the qualified shortlist into tracked Jira opportunities, links each to its brief, posts a structured handoff to Marcus so Revenue acts the same day.
- **Orchestrator:** client-research-lead (Owen Carter)
- **Sub-agents:** icp-scoring-analyst, sales-enablement-lead
- **Tools:** Jira issue_create/link/board, office_dm_post, drive_search, ops_scoreboard_get
- **Trigger:** completion of an ICP pipeline run or after a batch of briefs
- **Cadence:** weekly
- **Mission tie-in:** closes the loop to Revenue with tracked, linked opportunities.
- **Deliverable:** one Jira opportunity Task per qualified account (linked to brief, labeled by wedge), a board pipeline view, a DM handoff summary to Marcus.

### `wedge-strategy-deliberation` — Quarterly Wedge & ICP Deliberation
- **One-liner:** Structured multi-agent debate on whether ICP weights + wedge targeting point outreach at converting accounts; synthesized decision published as the research playbook.
- **Orchestrator:** client-research-lead (Owen Carter)
- **Sub-agents:** icp-scoring-analyst, prospect-source-mapper, outreach-intelligence-specialist, revops-and-kpi-manager
- **Tools:** **deliberation = native inbox** (parallel debate), ops_scoreboard_get, ops_ledger_query, Confluence page_create (publish), office_dm_post
- **Trigger:** monthly strategy review, or when conversion data shows misdirected ICP weights
- **Cadence:** event
- **Mission tie-in:** continuously retargets the lean team at the highest-converting accounts.
- **Deliverable:** evidence-grounded decision on ICP weighting + wedge focus, published as a Confluence research playbook, DM'd to Marcus and Maya.

---

## Department: Customer Success

### `onboarding-to-first-value` — Onboarding-to-First-Value Activation Drive
- **One-liner:** On every new signup, stand up the onboarding kit, drive to first DevProjects deploy or SEO win, confirm week-1 activation.
- **Orchestrator:** customer-success-and-support-lead (Tessa Morgan)
- **Sub-agents:** delivery-manager, lifecycle-email-marketing-lead, devprojects-support-resolver
- **Tools:** office_customer_lookup, platform-actions (Capabilities + DataCatalog), office_dm_post, GWS drive_replicate_template / docs_template_merge / calendar_event_create / meet_space_create, Jira issue_create/transition, Confluence page_create
- **Trigger:** new customer/signup event (customer.created) or a founder hand-off
- **Cadence:** event
- **Mission tie-in:** week-1 activation >=50% and deploy success >=80%.
- **Deliverable:** per-customer onboarding kit (welcome Doc from Drive template, scheduled kickoff Meet, first-value checklist as a Jira task) + confirmed first-value milestone logged to Confluence; Nina briefed for the day-0/3/7 sequence.

### `support-triage-resolution` — Support Triage & Bounded Resolution Loop
- **One-liner:** Tessa triages every inbound support signal, parallel-dispatches Devon to resolve within the allowed delivery intent, never crosses the production-release boundary without human approval.
- **Orchestrator:** customer-success-and-support-lead (Tessa Morgan)
- **Sub-agents:** devprojects-support-resolver, delivery-manager, bytedesk-platform-developer
- **Tools:** office_customer_lookup, Jira issue_create/view/transition/comment/link, platform-actions (DataCatalog + DevDocs), sandbox_shell, repo pr_view, office_dm_post, GWS chat_send_internal
- **Trigger:** new support ticket, a DevProjects deploy failure, or an inbound DM tagged as a problem
- **Cadence:** event
- **Mission tie-in:** 0 early churn and deploy success >=80%.
- **Deliverable:** triaged Jira support ticket (bytedesk-support + source:* + support:* + delivery:* labels) with root-cause trail, fix landed at the correct delivery intent, closed-resolved or a clean human-approval escalation when a production release is required.

### `devproject-change-request` — Customer DevProject Site-Change Submit -> Preview -> Approve
- **One-liner:** Turn a customer's plain-language site-change request into a previewed DevProjects change, never published live without explicit confirmation.
- **Orchestrator:** delivery-manager (Jordan Blake)
- **Sub-agents:** devprojects-support-resolver, customer-success-and-support-lead, web-design-director
- **Tools:** office_customer_lookup, devproject_work_submit / request_changes / approve, platform-actions (DevDocs + DataCatalog), office_dm_post, Jira issue_create/transition, GWS docs_create
- **Trigger:** customer requests a change to their own DevProjects site
- **Cadence:** event
- **Mission tie-in:** deploy success >=80% and 0 early churn — the wedge feels responsive and safe.
- **Deliverable:** submitted change rendered to a preview URL, written change summary for explicit go/no-go, published only on confirmed approval; full submit/preview/approve trail on a Jira delivery ticket. **Preserves the ADR-0055 risk-tier confirm ceremony.**

### `retention-risk-radar` — Retention-Risk Radar & First-Customer Save
- **One-liner:** Continuously scan activation/usage/support signals for the first account(s), flag churn risk early, trigger a coordinated save play.
- **Orchestrator:** customer-success-and-support-lead (Tessa Morgan)
- **Sub-agents:** delivery-manager, revops-and-kpi-manager, lifecycle-email-marketing-lead, product-ops-director
- **Tools:** office_customer_lookup, platform-actions (DataCatalog + Capabilities), ops_ledger_query, ops_scoreboard_get, GWS Sheets create/values-update, office_dm_post, Jira issue_create, Confluence page_create
- **Trigger:** daily scan + event re-scan on stalled onboarding / unresolved escalation / usage drop
- **Cadence:** daily
- **Mission tie-in:** 0 early churn (zero logo churn through day 30).
- **Deliverable:** daily retention-risk register (Sheet + Confluence) scoring each account green/amber/red with the driving signal + auto-opened save-play Jira task for any amber/red.

### `delivery-sequencing-standup` — Delivery Sequencing & Blocker-Clearing Standup
- **One-liner:** Jordan keeps the first-customer delivery path (onboarding, change requests, support) sequenced, dependency-tracked, unblocked.
- **Orchestrator:** delivery-manager (Jordan Blake)
- **Sub-agents:** customer-success-and-support-lead, devprojects-support-resolver, quality-and-release-lead, platform-architect
- **Tools:** Jira board/view/transition/comment/link/resync, ops_scoreboard_get, office_dm_post, GWS chat_send_internal, GWS docs_create
- **Trigger:** daily standup timer + event re-run on a new blocker or state change
- **Cadence:** daily
- **Mission tie-in:** week-1 activation >=50%, deploy success >=80%, 0 early churn.
- **Deliverable:** daily dependency-ordered delivery plan (blockers named with owners + ETAs) posted to the team and reconciled into the Jira board; at-risk handoffs escalated before they bite.

---

## Department: Marketing

### `devprojects-seo-positioning-engine` — DevProjects + SEO Positioning & Message Engine
- **One-liner:** Claire runs a multi-agent deliberation to lock DevProjects + SEO positioning vs real ICP + product reality, publishes a single source-of-truth messaging house every asset inherits.
- **Orchestrator:** marketing-director (Claire Donovan)
- **Sub-agents:** brand-and-creative-director, content-marketing-lead, client-research-lead, product-ops-director
- **Tools:** **deliberation = native inbox** + publish/close function tool, marketing_profile_materialize, office_customer_lookup, GWS docs_create / docs_template_merge, Confluence page_create / parent_index_refresh, Jira issue_create
- **Trigger:** founder/Claire kickoff, or when the wedge or ICP shifts
- **Cadence:** on-demand
- **Mission tie-in:** positioning a stranger repeats correctly is the precondition for qualified demos/trials.
- **Deliverable:** published "DevProjects + SEO Messaging House" Confluence page (value prop, 3 proof pillars, objection map, GHL-displacement narrative) + a materialized marketing profile the other workflows read + a Jira adoption-tracking task.

### `geo-cited-content-pipeline` — GEO-First Content That Ranks and Gets Cited
- **One-liner:** Iris + Sofia co-produce ICP-targeted articles engineered for SEO rankings AND AI-search citations, schema-marked, shipped to the site backlog past a citability gate.
- **Orchestrator:** content-marketing-lead (Sofia Reed)
- **Sub-agents:** seo-geo-growth-lead, brand-and-creative-director, web-development-lead
- **Tools:** marketing_profile_materialize, GWS docs_create / docs_batch_update / drive_search / sheets_values_update, devproject_work_submit, repo pr_view, Jira issue_create/transition, Confluence search
- **Trigger:** weekly content cadence, or a keyword/GEO-gap surfaced by the SEO program
- **Cadence:** weekly
- **Mission tie-in:** citable ranking content is the top-of-funnel that feeds demos/trials and seeds activation by demonstrating the SEO wedge in public.
- **Deliverable:** 1-3 publish-ready articles in Drive with JSON-LD schema and an above-gate AI-citability score, submitted as a DevProject/site PR, with a SEO/GEO tracking Sheet row + Jira rework tasks for below-gate pieces.

### `signup-to-first-value-lifecycle` — Signup-to-First-Value Lifecycle Activation
- **One-liner:** Nina drives an event-triggered onboarding/lifecycle email program walking every signup to first DevProjects deploy or SEO audit, with churn-risk detection + CS handoff.
- **Orchestrator:** lifecycle-email-marketing-lead (Nina Park)
- **Sub-agents:** content-marketing-lead, customer-success-and-support-lead, revops-and-kpi-manager
- **Tools:** office_customer_lookup, ops_ledger_query, GWS docs_create / gmail_draft_create / sheets create+values-update, office_dm_post, Jira issue_create, Confluence page_create
- **Trigger:** lifecycle event (new signup, first deploy attempt, audit started, N days idle)
- **Cadence:** event
- **Mission tie-in:** owns week-1 activation >=50% and 0 early churn; converts trials toward first paid.
- **Deliverable:** live multi-step onboarding/activation email sequence (Gmail drafts per cohort) + activation/churn-risk Sheet + auto-filed CS handoff DMs/Jira tasks for idle trials.

### `seo-geo-growth-program` — SEO/GEO Growth Program & Live Demo-Bait Audits
- **One-liner:** Iris runs the recurring SEO/GEO program (keyword/citation gap mapping, technical+content audits) and on-demand prospect-site GEO audit reports as live demo bait.
- **Orchestrator:** seo-geo-growth-lead (Iris Kim)
- **Sub-agents:** content-marketing-lead, client-research-lead, web-development-lead, sales-enablement-lead
- **Tools:** prospect_research_config_materialize, office_customer_lookup, GWS sheets create+values-update / docs_create / drive_share_internal, devproject_work_submit, repo pr_view, Jira issue_create, Confluence page_create, office_dm_post
- **Trigger:** weekly program run + on-demand when Research/Revenue flags a poor-discoverability prospect
- **Cadence:** weekly
- **Mission tie-in:** generates the SEO half of the 5 demos-or-trials and arms sales with a tangible "here's your AI-visibility gap and the fix."
- **Deliverable:** standing SEO/GEO scorecard Sheet + on-demand client-ready prospect GEO audit report in Drive shared to Revenue as demo bait + site-fix PRs and Jira tasks for owned issues.

### `demo-video-and-launch-asset-factory` — Demo Video & Wedge Launch Asset Factory
- **One-liner:** Leo + Mara turn a shipped capability into a coordinated launch bundle (demo video script, brand creative, slide deck, landing narrative).
- **Orchestrator:** video-content-producer (Leo Hart)
- **Sub-agents:** brand-and-creative-director, content-marketing-lead, product-ops-director, sales-enablement-lead
- **Tools:** repo pr_view/pr_diff, marketing_profile_materialize, GWS slides_create / docs_create / drive_file_create / drive_share_internal / drive_replicate_template, Jira issue_create/transition, Confluence page_create, office_dm_post
- **Trigger:** a DevProjects/SEO capability merges, or a launch is scheduled
- **Cadence:** event
- **Mission tie-in:** keeps the 5 demos-or-trials backed by current on-brand demo video + deck proof.
- **Deliverable:** per-launch asset bundle in a replicated Drive launch folder (video script + storyboard, brand creative set, slide deck, landing narrative doc) shared to Revenue/lifecycle + a Jira launch task.

---

## Department: Engineering

### `devprojects-deploy-reliability-watch` — DevProjects Deploy Reliability Watch
- **One-liner:** Watches DevProjects build/deploy outcomes, triages every failure to a root-cause owner, ships a fix PR — keeping deploy success above 80%.
- **Orchestrator:** platform-architect (Elias Mercer)
- **Sub-agents:** bytedesk-platform-developer, backend-development-lead, web-development-lead, quality-and-release-lead, devprojects-support-resolver
- **Tools:** devproject_work_submit / request_changes, sandbox_shell, ops_ledger_query, ops_scoreboard_get, find_specialist, Jira issue_create/transition, repo pr_view / repo_diff, platform-actions
- **Trigger:** a DevProjects build/deploy fails or daily deploy-success dips below 80%
- **Cadence:** event
- **Mission tie-in:** deploy success >=80% — protects the wedge the first paying customer depends on.
- **Deliverable:** triaged Jira task per failure cluster with root cause + owner + a merged fix PR (or escalation) + updated deploy-reliability ledger entry.

### `stripe-billing-go-live-gate` — Stripe Billing Go-Live Gate
- **One-liner:** Drives Stripe billing to safe-to-charge: backend implements, quality runs the production-safety gate, refuses go-live until idempotency/webhooks/rollback are green.
- **Orchestrator:** platform-architect (Elias Mercer)
- **Sub-agents:** backend-development-lead, quality-and-release-lead, bytedesk-platform-developer, revops-and-kpi-manager
- **Tools:** sandbox_shell, repo pr_view / repo_diff / pr_merge, Jira issue_create/transition/comment, Confluence page_create, platform-actions, office_dm_post
- **Trigger:** Stripe integration branch up, or a founder/Maya requests the go-live call
- **Cadence:** on-demand
- **Mission tie-in:** Stripe live + MRR~$100 + first customer=1.
- **Deliverable:** go/no-go readiness assessment (Confluence runbook + verification evidence), merged billing PR on green, live-billing flag flipped only when the gate passes.

### `weekly-architecture-audit` — Weekly Architecture & Drift Audit
- **One-liner:** Elias audits the platform for drift/coupling/missing seams vs the first-sales critical path, converts findings into prioritized owner-assigned Jira work.
- **Orchestrator:** platform-architect (Elias Mercer)
- **Sub-agents:** backend-development-lead, web-development-lead, bytedesk-platform-developer, quality-and-release-lead, devex-and-docs-lead
- **Tools:** repo list / repo_diff / repo_fetch, sandbox_shell, **deliberation = native inbox** + publish, Jira issue_create / board, Confluence page_create, platform-actions
- **Trigger:** weekly schedule, or a major architectural change landing on develop
- **Cadence:** weekly
- **Mission tie-in:** weekly shippable-PR cadence without debt.
- **Deliverable:** published architecture-audit report (Confluence) with a ranked findings list, each a scheduled Jira task assigned to the right engineer.

### `release-readiness-quality-gate` — Release Readiness Quality Gate
- **One-liner:** Elena gates every release on the first-sales path (parallel PR review + regression/test-gap + verification evidence), emits go/no-go with a known rollback before shipping.
- **Orchestrator:** quality-and-release-lead (Elena Torres)
- **Sub-agents:** backend-development-lead, web-development-lead, bytedesk-platform-developer, devex-and-docs-lead
- **Tools:** repo pr_list/pr_view/repo_diff/pr_merge, sandbox_shell, Jira issue_view/transition/comment, Confluence page_create, office_dm_post, GWS chat_send_internal
- **Trigger:** a release branch is cut, or a first-sales-path PR is marked ready-to-ship
- **Cadence:** event
- **Mission tie-in:** 0 early churn + deploy success >=80% + Stripe live.
- **Deliverable:** release-readiness assessment with go/no-go, reproducible verification evidence, documented rollback plan to founders + Confluence; merge only on green. **pr_merge respects gitflow — PRs to develop only; main via release/hotfix through TeamCity, never auto-merge to main.**

### `engineering-runbook-and-mcp-docs-factory` — Engineering Runbook & MCP Docs Factory
- **One-liner:** Samir keeps DevProjects, Stripe, and per-integration MCP-server docs/runbooks current as features land.
- **Orchestrator:** devex-and-docs-lead (Samir Patel)
- **Sub-agents:** bytedesk-platform-developer, backend-development-lead, web-development-lead, devprojects-support-resolver
- **Tools:** repo repo_diff / pr_view, Confluence search / page_create / parent_index_refresh, GWS docs_create / docs_template_merge / drive_share_internal, Jira issue_create, platform-actions (DevDocs)
- **Trigger:** a first-sales-path PR merges to develop, or a new integration MCP server ships
- **Cadence:** event
- **Mission tie-in:** week-1 activation >=50% + 5 demos/trials — first-value docs let signups and support self-serve.
- **Deliverable:** updated Confluence runbook + customer/internal Google Doc per shipped capability, Confluence parent index refreshed, doc-gaps filed as Jira tasks.

---

## Department: Product

### `activation-flow-spec-pipeline` — Activation-Flow Spec Pipeline
- **One-liner:** Turn a fuzzy "make the activation path convert" request into an engineer-buildable spec (acceptance criteria, designed states, smallest first-value slice) with a paired UX design-intent doc, then file the Jira work.
- **Orchestrator:** product-ops-director (Dana Whitaker)
- **Sub-agents:** web-design-director, platform-architect, seo-geo-growth-lead
- **Tools:** Confluence search / page_create, Jira issue_create / link / board, platform-actions (DevDocs search), GWS docs_create / docs_batch_update / drive_share_internal
- **Trigger:** a founder/Maya strategy note, or Dana picks up an activation gap
- **Cadence:** on-demand
- **Mission tie-in:** week-1 activation >=50% and deploy success >=80%.
- **Deliverable:** Confluence spec page (problem, first-value slice, acceptance criteria, empty/loading/error states) + a Doc design-intent companion + a parent Jira Task with linked child slices, anchored to the activation metric.

### `first-customer-roadmap-triage` — First-Customer Roadmap Triage
- **One-liner:** Continuously re-prioritize the backlog toward the single 30-day metric, cutting everything that does not move first-paying-customer.
- **Orchestrator:** product-ops-director (Dana Whitaker)
- **Sub-agents:** revops-and-kpi-manager, customer-success-and-support-lead, seo-geo-growth-lead
- **Tools:** goal proposals list / triage / measure / clusters, ops_scoreboard_get, Jira board / issue_transition, Confluence page_create, office_dm_post
- **Trigger:** weekly cadence + event when a goal cluster or a deploy/activation metric crosses threshold
- **Cadence:** weekly
- **Mission tie-in:** first customer=1 and 5 demos/trials — protects scarce build capacity.
- **Deliverable:** prioritized near-term roadmap on Confluence (ranked slices + explicit "not this cycle" cuts) + Jira board re-sequenced + a DM digest to Maya/founders.

### `design-system-activation-review` — Design-System Activation Review
- **One-liner:** Avery audits live DevProjects + SEO activation screens for clarity/states/a11y/design-system compliance, emits implementation-ready design-intent specs.
- **Orchestrator:** web-design-director (Avery Brooks)
- **Sub-agents:** brand-and-creative-director, web-development-lead, product-ops-director
- **Tools:** platform-actions (DevDocs search), repo_fetch, marketing_profile_materialize, GWS docs_create / docs_template_merge / slides_create / drive_share_internal, Jira issue_create / comment
- **Trigger:** a design-pass request, a new/changed activation screen landing, or a weekly polish cadence
- **Cadence:** weekly
- **Mission tie-in:** week-1 activation >=50% and 0 early churn — clarity is the conversion lever for an SMB owner.
- **Deliverable:** design-intent spec (components, states, layout rules, a11y/responsive criteria) as a Doc + a visual slide walkthrough + Jira Tasks for Nolan's team.

### `demo-experience-builder` — Sales-Demo Experience Builder
- **One-liner:** On a hot prospect, spin a tailored, confidence-inspiring demo flow spec (sample app, SEO audit framing, activation walkthrough) so Revenue runs a converting demo.
- **Orchestrator:** product-ops-director (Dana Whitaker)
- **Sub-agents:** web-design-director, sales-enablement-lead, seo-geo-growth-lead, client-research-lead
- **Tools:** office_customer_lookup, prospect_research_config_materialize, Confluence search, GWS docs_create / slides_create / drive_replicate_template / calendar_event_create / meeting_schedule, Jira issue_create
- **Trigger:** Marcus or Maya flags a qualified DevProjects/SEO prospect ready for demo/trial
- **Cadence:** event
- **Mission tie-in:** 5 demos/trials and first customer=1.
- **Deliverable:** prospect-tailored demo script + Slides deck + Drive demo folder from template + scheduled demo Meet + a Jira demo-to-trial follow-through task.

### `request-intake-spec-synthesizer` — Request-Intake Spec Synthesizer
- **One-liner:** Standing intake catching inbound asks (support/sales/founder), runs a fast multi-agent deliberation on scope vs the 30-day metric, emits either a tight spec or a documented "not now."
- **Orchestrator:** product-ops-director (Dana Whitaker)
- **Sub-agents:** web-design-director, customer-success-and-support-lead, sales-enablement-lead, platform-architect
- **Tools:** **deliberation = native inbox** + publish, Confluence search / page_create, Jira issue_create / link, office_dm_post
- **Trigger:** daily intake sweep + event when a high-signal request arrives
- **Cadence:** daily
- **Mission tie-in:** first customer=1 and 0 early churn — only first-value-moving requests get spec'd.
- **Deliverable:** published deliberation verdict per request — a Confluence spec + linked Jira Task, or a documented "parked / not this cycle" note DM'd back.

---

## Department: People Operations

### `agent-org-health-review` — Weekly Agent Org-Health Review
- **One-liner:** Vivian fans a full-roster health sweep (activity, workload balance, ownership clarity), synthesizes one founder-facing org-health report flagging coordination risk.
- **Orchestrator:** hr-org-designer (Vivian Cole)
- **Sub-agents:** chief-of-staff, platform-architect, marketing-director, sales-enablement-lead, customer-success-and-support-lead, revops-and-kpi-manager
- **Tools:** team roster_get / org_context / activity_snapshot, ops_scoreboard_get, ops_ledger_query, Confluence page_create, office_dm_post, GWS sheets create+values-update
- **Trigger:** scheduled weekly (Mon 08:00) or Maya/founder request
- **Cadence:** weekly
- **Mission tie-in:** protects weekly shippable output by catching mis-scoped/overloaded agents.
- **Deliverable:** Confluence org-health page + backing Sheet scoreboard (per-agent activity/workload/ownership, RAG at-risk list, prioritized fixes) + a founder summary DM to Maya.

### `coordination-failure-detector` — Coordination-Failure Detector & Triage
- **One-liner:** Vivian mines the ops ledger + Jira for stuck handoffs, delegates root-cause reads to the two implicated leads in parallel, files fix tickets.
- **Orchestrator:** hr-org-designer (Vivian Cole)
- **Sub-agents:** chief-of-staff, platform-architect, sales-enablement-lead, customer-success-and-support-lead, client-research-lead, product-ops-director
- **Tools:** ops_ledger_query, team activity_snapshot / org_context, Jira board / issue_view / issue_create / comment / link, office_dm_post
- **Trigger:** ops-ledger anomaly (delegation timeout / stalled run / re-dispatch loop) or daily Jira sweep of items stuck >48h
- **Cadence:** daily
- **Mission tie-in:** removes silent handoff failures that drop deploy-success and demo follow-through.
- **Deliverable:** per-incident BDP fix Task (root cause + owning agent + corrective action) linked to the implicated work items + a daily coordination-risk digest to Maya.

### `new-agent-onboarding` — New-Agent Onboarding & Role Provisioning
- **One-liner:** On a confirmed recurring capability gap, Vivian runs the change-proposal gate, scaffolds the new agent's identity/role doc, provisions its Workspace footprint, debate-pressure-tests the role before adding it.
- **Orchestrator:** hr-org-designer (Vivian Cole)
- **Sub-agents:** chief-of-staff, platform-architect, product-ops-director, devex-and-docs-lead
- **Tools:** team find_specialist / change_propose / change_approve / change_apply, **deliberation = native inbox**, Confluence page_create, GWS docs_template_merge / drive_share_internal / directory_user_get, Jira issue_create, office_dm_post
- **Trigger:** a confirmed recurring capability gap an existing agent cannot own
- **Cadence:** on-demand
- **Mission tie-in:** adds leverage only where it moves DevProjects/SEO/sales throughput; the deliberation gate prevents headcount bloat.
- **Deliverable:** approved-and-applied team change (propose -> approve -> apply, nessie blast_radius gated) + Confluence role-charter + ownership map + provisioned Workspace footprint + a BDP onboarding Task.

### `role-clarity-ownership-map` — Role-Clarity & Ownership-Map Refresh
- **One-liner:** Vivian rebuilds the live ownership map across the DevProjects + SEO + sales path, leads confirm/contest boundaries in parallel, deliberation resolves overlaps/gaps, publishes the canonical RACI.
- **Orchestrator:** hr-org-designer (Vivian Cole)
- **Sub-agents:** chief-of-staff, platform-architect, marketing-director, sales-enablement-lead, customer-success-and-support-lead, client-research-lead, product-ops-director, revops-and-kpi-manager
- **Tools:** team roster_get / org_context / member_get, **deliberation = native inbox** + synthesize + publish, Confluence page_create / parent_index_refresh, GWS sheets_create, office_dm_post
- **Trigger:** scheduled (every other week) or after an applied team change / detected overlap
- **Cadence:** weekly
- **Mission tie-in:** clean ownership removes overlap/gap friction dragging activation and demo conversion.
- **Deliverable:** published canonical RACI/ownership map on Confluence (lead-confirmed, conflicts resolved by deliberation) + a backing Sheet + overlap/gap deltas DM'd to Maya + role changes handed to onboarding.

### `agent-workload-rebalance` — Workload Review & Rebalance
- **One-liner:** Vivian reads the ops scoreboard/ledger to find overloaded vs idle agents on the first-customer path, managers confirm capacity in parallel, proposes concrete re-delegations.
- **Orchestrator:** hr-org-designer (Vivian Cole)
- **Sub-agents:** chief-of-staff, platform-architect, marketing-director, sales-enablement-lead, customer-success-and-support-lead
- **Tools:** ops_scoreboard_get, ops_ledger_query, team activity_snapshot / find_specialist, Jira board / issue_view / transition / comment, GWS sheets_values_update, office_dm_post
- **Trigger:** scheduled weekly (mid-week) or event when an agent's open-work/stalled-run count crosses the overload threshold
- **Cadence:** weekly
- **Mission tie-in:** keeps critical-path agents from becoming the single bottleneck.
- **Deliverable:** workload-rebalance proposal (overloaded/idle deltas + concrete re-delegations) reflected as manager-confirmed Jira reassignments + an updated capacity Sheet + a DM to Maya.

---

## Department: Operations

### `first-customer-warroom` — First-Customer War Room
- **One-liner:** Maya runs a daily cross-department push converting the hottest DevProjects/SEO prospect into a demo, trial, and first charged Stripe subscription.
- **Orchestrator:** chief-of-staff (Maya Chen)
- **Sub-agents:** sales-enablement-lead, client-research-lead, seo-geo-growth-lead, product-ops-director, platform-architect, customer-success-and-support-lead
- **Tools:** office_customer_lookup, ops_scoreboard_get, goal_proposals_list, Jira issue_create / transition, office_dm_post, GWS sheets_values_update / chat_send_internal, platform-actions
- **Trigger:** daily 08:00 founder pulse, a founder DM naming a target account, or a detected trial signup
- **Cadence:** daily
- **Mission tie-in:** first customer=1 and MRR~$100 — drives the single hottest deal to a charged sub each day.
- **Deliverable:** daily war-room brief (Sheet row + founder Chat/DM) naming the #1 deal, its blocking task per department, owner, next step to a Stripe close — with blocking Jira tasks created/transitioned.

### `weekly-business-review` — Weekly Business Review (WBR) Pack
- **One-liner:** Every Monday Maya fans data pulls to RevOps/Marketing/CS/Eng in parallel, then synthesizes one decision-ready deck against the 30-day metrics.
- **Orchestrator:** chief-of-staff (Maya Chen)
- **Sub-agents:** revops-and-kpi-manager, marketing-director, customer-success-and-support-lead, platform-architect, sales-enablement-lead
- **Tools:** ops_scoreboard_get, ops_ledger_query, goal_proposals_measure, Jira board, GWS sheets_create / slides_create / docs_create / calendar_event_create / meet_space_create, Confluence page_create
- **Trigger:** cron Monday 07:00
- **Cadence:** weekly
- **Mission tie-in:** tracks all 30-day targets at once so founders steer weekly.
- **Deliverable:** WBR Slides deck + Confluence page (metrics vs targets, RAG status, top 3 decisions) + a scheduled Meet review with founders.

### `goal-triage-router` — Goal Triage & Mission Router
- **One-liner:** Maya triages incoming goal proposals + founder requests, scores each against the first-customer objective, routes survivors to the owning department as live Jira work, kills/parks off-mission.
- **Orchestrator:** chief-of-staff (Maya Chen)
- **Sub-agents:** product-ops-director, sales-enablement-lead, marketing-director, platform-architect
- **Tools:** goal proposals list / triage / clusters / merge / approve / decline, team find_specialist, Jira issue_create / link, office_dm_post
- **Trigger:** on-demand when a founder posts a request + a scheduled queue sweep
- **Cadence:** on-demand
- **Mission tie-in:** protects focus — only first-customer-moving work becomes active effort.
- **Deliverable:** triaged proposal queue (each approved+routed to an owner as a linked Jira Task, merged into a cluster, or declined with rationale) + a disposition DM to the requesting founder.

### `wedge-bet-deliberation` — Strategic Wedge Deliberation
- **One-liner:** For high-stakes founder bets (which wedge to push, first-deal pricing, what to demo), Maya convenes a structured cross-director debate, publishes one founder-ready recommendation with dissent on record.
- **Orchestrator:** chief-of-staff (Maya Chen)
- **Sub-agents:** sales-enablement-lead, product-ops-director, marketing-director, platform-architect, customer-success-and-support-lead
- **Tools:** **deliberation = native inbox** + publish/close, Confluence page_create, Jira issue_create, GWS docs_create
- **Trigger:** a founder asks a strategic either/or, or Goal Triage flags a too-consequential decision
- **Cadence:** event
- **Mission tie-in:** sharpens the DevProjects-vs-SEO-vs-pricing bets that determine whether first paid + ~$100 MRR land inside 30 days.
- **Deliverable:** published deliberation decision (Confluence + synthesized doc) with the recommendation, the strongest counter-case, and a follow-up Jira Task executing the chosen path.

### `escalation-incident-bridge` — Escalation & Incident Bridge
- **One-liner:** On a deal/deploy/customer-blocking escalation, Maya triages severity, dispatches the right specialist team in parallel, keeps founders + customer informed until clear.
- **Orchestrator:** chief-of-staff (Maya Chen)
- **Sub-agents:** customer-success-and-support-lead, devprojects-support-resolver, platform-architect, quality-and-release-lead, delivery-manager
- **Tools:** office_customer_lookup, ops_ledger_query, team find_specialist, native delegate (sys_session_send), Jira issue_create / transition, office_dm_post, GWS chat_send_internal / meet_space_create, platform-actions
- **Trigger:** a customer escalation, a failed DevProjects deploy, a TeamCity/release break, or a founder flag
- **Cadence:** event
- **Mission tie-in:** defends 0 early churn and deploy success >=80% by clearing blockers fast.
- **Deliverable:** resolved (or owned-with-ETA) incident — triage record in the ops ledger, an escalation Jira Task driven to Done, the customer updated, a founder status note, a bridge Meet only for sev-1.

---

## Department: Integrations (cross-department reuse)

### `client-onboarding-drive-provisioning` — Client Onboarding & Drive Workspace Provisioning
- **One-liner:** On a won deal or new trial, stamp out a full Drive client folder, seed kickoff docs, open the Jira/Confluence delivery surface in one pass (BDP-1679 replicate-template).
- **Orchestrator:** delivery-manager (Jordan Blake)
- **Sub-agents:** customer-success-and-support-lead, sales-enablement-lead, devex-and-docs-lead
- **Tools:** GWS drive_replicate_template / drive_file_create / drive_share_internal / docs_template_merge / docs_template_seed / sheets_create, office_customer_lookup, Jira issue_create, Confluence page_create, office_dm_post
- **Trigger:** deal -> Won / a trial activated (customer.activated) / Maya hands off a new client
- **Cadence:** event
- **Mission tie-in:** get new signups to first value + week-1 activation >=50%.
- **Deliverable:** provisioned per-client Drive folder (replicated from Active Clients template) with merged kickoff Doc, tracker Sheet, internal-shared + a linked Jira delivery Task + a Confluence client-context page + a DM to the account owner.

### `devprojects-demo-asset-factory` — DevProjects/SEO Demo Asset Factory
- **One-liner:** Turn a qualified prospect into a personalized demo kit (Slides deck, one-pager Doc, booked Meet) to run 5 demos this month.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** client-research-lead, brand-and-creative-director, seo-geo-growth-lead, revops-and-kpi-manager
- **Tools:** office_customer_lookup, prospect_research_config_materialize, GWS slides_create / docs_create / docs_batch_update / drive_share_internal / meeting_schedule / meet_space_create / calendar_freebusy, Jira issue_create, office_dm_post
- **Trigger:** a prospect reaches demo-ready, or the revenue lead requests a demo kit
- **Cadence:** on-demand
- **Mission tie-in:** moves the 5 demos-or-trials + first paying customer.
- **Deliverable:** branded prospect-personalized Slides deck + leave-behind one-pager Doc shared to the deal owner + a scheduled freebusy-deconflicted Meet + a Jira demo-prep Task.

### `outreach-drafting-pipeline` — Personalized Outreach Drafting Pipeline
- **One-liner:** From a scored lead list, draft personalized Gmail outreach grounded in research + DevProjects/SEO value props — reviewable drafts, never auto-sent.
- **Orchestrator:** sales-enablement-lead (Marcus Grant)
- **Sub-agents:** outreach-intelligence-specialist, icp-scoring-analyst, content-marketing-lead, lifecycle-email-marketing-lead
- **Tools:** prospect_research_config_materialize, marketing_profile_materialize, office_customer_lookup, GWS people_search / gmail_search / gmail_thread_read / gmail_draft_create / sheets_values_update, Jira issue_create, office_dm_post
- **Trigger:** a new batch of ICP-scored leads, or the revenue lead requests an outreach wave
- **Cadence:** daily
- **Mission tie-in:** feeds top-of-funnel toward first customer + 5 demos/trials; prior-thread-aware so it never duplicates contact.
- **Deliverable:** per-prospect personalized Gmail drafts (no auto-send) parked in the shared sending mailbox + a status Sheet + a Jira outreach-wave Task.

### `weekly-mission-kpi-reporting` — Weekly Mission KPI Reporting & Confluence Digest
- **One-liner:** Pull the 30-day mission metrics from Jira + the platform, build a Sheets dashboard, publish a Confluence digest founders read every Monday.
- **Orchestrator:** revops-and-kpi-manager (Adrian Cross)
- **Sub-agents:** sales-enablement-lead, customer-success-and-support-lead, marketing-director, seo-geo-growth-lead
- **Tools:** Jira board / resync, ops_scoreboard_get, ops_ledger_query, office_customer_lookup, GWS sheets_create / sheets_values_update, Confluence page_create / parent_index_refresh, office_dm_post
- **Trigger:** scheduled Monday 07:00 + on-demand founder request
- **Cadence:** weekly
- **Mission tie-in:** keeps the team honest against ALL 30-day targets.
- **Deliverable:** refreshed mission-metrics Sheets dashboard (current/goal/delta per target) + a published Confluence weekly digest + a founder DM linking both.

### `support-to-delivery-ticket-bridge` — Support-to-Delivery Ticket & Knowledge Bridge
- **One-liner:** Convert a DevProjects support signal into a triaged Jira ticket, draft the customer reply, capture the fix as a reusable Confluence runbook.
- **Orchestrator:** customer-success-and-support-lead (Tessa Morgan)
- **Sub-agents:** devprojects-support-resolver, delivery-manager, backend-development-lead
- **Tools:** office_customer_lookup, office_dm_post, Jira issue_create / transition / comment / link, find_specialist, GWS gmail_draft_create, Confluence search / page_create, devproject_work_submit
- **Trigger:** a DevProjects support signal (deploy failure, portal report, CS inbound) or Tessa flags an at-risk customer
- **Cadence:** event
- **Mission tie-in:** protects 0 early churn + deploy success >=80% — every signal becomes a tracked, owned, customer-acknowledged resolution with a reusable runbook.
- **Deliverable:** a labeled Jira support Task routed via find_specialist + a drafted Gmail reply + a linked fix/work item + a Confluence runbook page.

---

## Tool Layer

Omnigent re-exposable kinds: **`type:mcp`** (HTTP/SSE — `transport: http`, `url`, `headers`; `${ENV}` expansion happens ONLY for operator-authored template agents, not tenant bundles, and the parser **raises on an unresolved `${VAR}` at deploy**) and **`type:function`** (in-process Python). There is no workflow DSL. Multi-agent debate and delegation are **native** (`sys_session_send` / `sys_read_inbox`), not tools.

| Tool group | Kind | Auth model | Buildable now? | Notes |
|---|---|---|---|---|
| Jira (issue CRUD/transition/link/comment, board, resync) | mcp | EXISTING Atlassian Rovo MCP over its own OAuth (cloudId bytedesk.atlassian.net, project BDP) | **Yes** | Wire as `type:mcp` in Wave 1. Local gateway defaults to BDPDEV — pass project/space explicitly. Keep only resync/board-label helpers as thin function tools if Rovo lacks them. |
| Confluence (page create/search, parent-index refresh) | mcp | Atlassian Rovo MCP (spaceId 491524) over its OAuth | **Yes** | `parent_index_refresh` may need a small function tool if Rovo can't do it. |
| Deliberation (debate: start/round/synthesize/publish/run/close) | function/native | Native inbox; publish/close = thin function tool writing Confluence | **Yes** | Do NOT re-expose as opaque tools — the inbox IS multi-agent debate. Only the durable artifact (publish/close) is a function tool. |
| Shell / Release (sandbox shell, release shell) | function | Native `sys_os_shell` in the bwrap/caller_process sandbox; release via gated function tool over platform DevDeployment MCP | **Yes** (sandbox) | `release_shell` stays human-gated — TeamCity is the only prod deploy path, no break-glass. nessie `gate_pushes:true` on both. |
| Platform-actions (ByteDesk.Mcp: Capabilities, DataCatalog, DevDocs, identity/customers/sales/dev/tools, Stripe) | mcp | **NEW confidential client-credentials client** (extend McpOAuthApplicationSeeder) + BYTEDESK_MCP_TOKEN in Infisical, OR copy OpenClawMcpProxy (BDP-447) | **No** | The interactive `authenticate`/`complete_authentication` 2-step is a human-PKCE dance unusable by an autonomous server — **single biggest auth gap.** |
| Office (customer lookup, DM post) | mcp | Platform MCP behind omnigent client-credentials OAuth | **No** | `dm_post` reuses the planned Office<->omnigent SSE->SignalR bridge / ByteDesk.Realtime, not a second write path. |
| Team/Org (roster, member, org-context, find-specialist, activity-snapshot, change propose/approve/apply) | mcp | Companion bytedesk-team-mcp behind platform OAuth | **No** | Re-home team-plugin TS lib into ByteDesk.Mcp [McpServerTool]. find_specialist reads roster+ops scoreboards (deterministic). change_apply nessie-gated. **Do NOT port team_delegate** — native `sys_session_send` replaces it. |
| Ops (ledger query, scoreboard get) | mcp | Companion bytedesk-team-mcp; durable platform DB store | **No** | The OpenClaw OpsStore (JSONL+snapshot in the PV) doesn't exist for omnigent — re-home into a platform DB table for one source of truth. |
| Goals (proposals list/triage/measure/approve/decline/merge/clusters/cadence) | mcp | Companion bytedesk-team-mcp behind platform OAuth | **No** | Goal-pipeline state in a durable platform DB store. approve/decline stay founder-gated where workflows require. |
| Research/Marketing config materialize | mcp | Companion bytedesk-team-mcp; DB-backed tenant-config catalog | **No** | Already DB-shaped — cleanest port to [McpServerTool]. Deterministic fast tool, not an agent turn. |
| DevProject work (submit/approve/request-changes) | mcp | ByteDesk.AI.Development action surface behind omnigent OAuth, customer-scoped | **No** | Preserve submit->preview->approve + risk-tier confirm (ADR-0055 ledger). Publish behind human/customer go/no-go; nessie `gate_pushes:true`. |
| Repo (PR view/diff/list/merge, fetch) | function | GitHub token from Infisical (gh/REST) | **No** | Reads buildable once the GH token is wired (dev pod lacks Sandbox__GitHubToken). `pr_merge` respects gitflow — PRs to develop only; main via release/hotfix through TeamCity, never auto-merge to main. nessie-gated. |
| Google Drive / Docs / Gmail / Calendar+Meet / Sheets+Slides+Forms / People+Directory+Audit / Chat+Tasks+Keep | mcp | **Standalone GWS HTTP MCP** — keyless WIF + IAM signJwt DWD, subject openclaw@bytedesk.ai, domain bytedesk.ai; **NEW omnigent WIF identity + DWD grant in the Workspace admin console** | **No** | Lift the google-workspace plugin auth.ts (least gateway-coupled). All ~32 GWS tools on one identity + one server. Gmail defaults to draft-only; send_internal domain-restricted. Real Google admin action — cannot be faked. |

---

## Build Waves

### Wave 1 — First-customer orchestrator bundles (ship now, no new auth)
Author the orchestration prompts + inbox delegation + nessie `allowed_subagents`/`spawn_bounds`/`blast_radius` for the workflows that move the first-paying-customer objective. Wire the **Atlassian Rovo MCP live** as the only enabled tool block; declare platform/team/GWS blocks but keep them **commented/gated** (the parser raises on unresolved `${VAR}`). The decompose -> delegate -> synthesize loop is fully exercisable with stub sub-agent returns.

Order: `infra-bundle-authoring-contract` -> `tool-atlassian-rovo-wire` -> the lead-gen/research/demo/close/activation/ops-routing bundles (`target-account-intel-pack`, `icp-prospect-pipeline`, `wedge-fit-account-brief`, `demo-to-close-pipeline`, `demo-experience-builder`, `devprojects-demo-asset-factory`, `seo-geo-growth-program`, `onboarding-to-first-value`, `trial-activation-first-value`, `first-customer-warroom`, `goal-triage-router`, `first-customer-roadmap-triage`).

### Wave 2 — Omnigent tool layer + the real auth/identity provisioning
The load-bearing infra that unblocks every gated tool block:
1. `infra-omnigent-mcp-oauth-client` (confidential client-credentials client in ByteDesk.Identity) -> `infra-bytedesk-mcp-token-infisical` (BYTEDESK_MCP_TOKEN, Infisical Operator) — **critical path.**
2. `tool-platform-actions-mcp` -> the platform-backed MCP groups: `tool-office-mcp`, `tool-team-mcp`, `tool-ops-mcp`, `tool-goals-mcp`, `tool-research-marketing-config-mcp`, `tool-devproject-mcp`.
3. `infra-gws-wif-identity` (new WIF identity + Workspace DWD grant — real Google admin action) -> `tool-gws-mcp` (one server, all ~32 GWS tools).
4. `tool-repo-function` (after the GH token) and `tool-shell-release-function` (native shell + gated release).
5. `tool-deliberation-native-publish` (publish/close artifact tool; debate stays native).

### Wave 3 — Flip gated tools on + author the remaining department workflows
`infra-flip-gated-blocks-wave1` uncomments the Wave-1 bundle tool blocks now that the tokens/MCPs resolve. Then author the remaining workflows on the now-live tool layer: the full Engineering set (`devprojects-deploy-reliability-watch`, `stripe-billing-go-live-gate`, `release-readiness-quality-gate`, `weekly-architecture-audit`, `engineering-runbook-and-mcp-docs-factory`), Revenue (`stripe-go-live-billing` — doubly gated on billing + MCP surfaces; `revops-kpi-cockpit`), the rest of Customer Success/Integrations (`support-triage-resolution`, `devproject-change-request`, `retention-risk-radar`, `delivery-sequencing-standup`, `client-onboarding-drive-provisioning`, `outreach-drafting-pipeline`, `weekly-mission-kpi-reporting`, `support-to-delivery-ticket-bridge`), the remaining Marketing/Product/Research deliberation+content workflows, the Operations cadence workflows (`weekly-business-review`, `escalation-incident-bridge`, `wedge-bet-deliberation`), and the full People Ops set.

**Honest gating summary:** Bundles + Atlassian-MCP wiring + nessie + native shell/inbox delegation are buildable now. The platform-action MCP (needs the client-credentials client + BYTEDESK_MCP_TOKEN), the team-tool MCP (needs the re-home/port to a durable DB store), and the GWS MCP (needs a new WIF identity + Workspace DWD grant) are real auth/infra that cannot be faked. **Stripe go-live is doubly gated:** the billing integration itself must exist AND the MCP must expose its plan/price/checkout surfaces.