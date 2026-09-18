# Engineering Report - Assignment 1: IncidentZero

**Student ID:** 25I-8003  
**Course:** Agentic Artificial Intelligence (Fall 2026)  
**Institution:** FAST-NUCES Islamabad, Department of AI & Data Science  
**Word Count:** ~1,550 words (Strict target: 1,200–1,800)

---

## 1. Architecture

IncidentZero is a bounded autonomous SRE incident commander engineered around a strict separation between heuristic natural-language reasoning and deterministic Python execution governance. The architecture deliberately eschews third-party agent frameworks (such as LangChain, CrewAI, or AutoGen) in favor of a framework-free runtime where the Large Language Model (LLM) acts solely as an advisor proposing hypotheses and actions. The Python controller exercises total authority over safety, budgets, state transitions, and concurrency.

`
[User Ticket] -> [AgentController (Budget, State, Retries)] <-> [Groq Client: gpt-oss-20b]
                         |
                 [ToolRegistry: Schema Validation, LoopGuard, RiskPolicy, Concurrency]
                         |
                 [SimulationEnvironment: Authoritative Topology, Perturbations, Recovery]
`

### 1.1 Separation of Responsibilities
1. **LLM Layer (GroqModelClient)**: Proposes diagnostic plans and tool calls via openai/gpt-oss-20b on Groq. Possesses zero direct execution authority and no access to private simulator state.
2. **Controller Layer (AgentController)**: Governs the loop, tracks state, enforces budgets (14 LLM, 28 tools), validates preconditions, routes actions for human approval, intercepts loops, and enforces verification before closure.
3. **Planner & Policies (Planner, ReplanPolicy, LoopGuard, RetryPolicy)**: Produces structured plans, hashes action fingerprints, and coordinates exponential backoff on transient failures.
4. **Approval Gateway (ApprovalGateway)**: Gates high and critical actions behind explicit operator confirmation.
5. **Tool Registry & Simulator (ToolRegistry, SimulationEnvironment)**: The authoritative runtime boundary managing microservice topology, mutations, world versioning, and cryptographic evidence.

---

## 2. Planning and Re-planning Strategy

Production incidents involve high uncertainty, misleading first-responder hypotheses, and dynamically shifting environments. IncidentZero implements an explicit plan-and-execute paradigm grounded in verifiable telemetry.

### 2.1 Explicit Initial Plan Creation
Before invoking remediation tools, the controller executes get_incident. The ticket observation is passed to Planner.create(), which uses structured JSON schema (PLAN_SCHEMA) to generate an AgentPlan containing:
- An explicit unverified hypothesis.
- Two or more structured subgoals (PlanStep) with distinct step_id, objective, and success_signal.
- A mandatory final verification step.
- Initial revision counter (
evision = 0) and rationale summary.

### 2.2 Dynamic Re-planning Triggers
Under ReplanPolicy.should_replan(), dynamic re-planning is triggered upon encountering:
1. **Stale Preconditions (stale_precondition)**: An environmental change (background traffic surge or pod crash) caused the authoritative world_version to advance past the agent observed version.
2. **Approval Denial (pproval_denied)**: An operator refused permission for a high-risk or critical action (such as database failover or release rollback).
3. **Failed Verification (erify_recovery criteria_met = false)**: A remediation action executed successfully, but objective checkout recovery criteria remained unmet.
4. **Action / Validation Errors (error, alidation_error)**: Non-retryable execution errors.
5. **Loop Interventions (loop_detected)**: Repetitive action patterns blocked by the loop guard.

### 2.3 Preserved State Across Revisions
When Planner.revise() executes:
- The plan revision counter is incremented (
evision += 1).
- All accumulated evidence_id records are preserved in AgentState.
- The controller injects a context summary containing the failure trigger and observed world version into the model message history.
- The hypothesis is updated to pivot away from falsified assumptions (e.g., shifting focus from deployment rollback to database failover).
- If the remaining LLM budget is constrained (<= 1 call), the planner utilizes an in-memory offline fallback revision to conserve request budget for terminal verification and safe escalation.

---

## 3. Failure Handling

The runtime classifies operational failures into distinct categories, applying tailored recovery mechanisms rather than treating all issues as generic retries:

| Failure Category | Root Mechanism | Agent Controller Response |
| :--- | :--- | :--- |
| **Transient Model Failure (429, 503, tool_use_failed, output_parse_failed)** | RetryPolicy.call_model() | Bounded exponential backoff (.5 \times 2^{attempt}$) up to 3 attempts. Accounts for LLM call budget. Does not retry permanent syntax errors. |
| **Transient Telemetry Timeout** | Simulated tool timeout | Telemetry tools return 
etryable: true. The controller returns the result to the model, which performs a bounded re-observation. |
| **Malformed Model Arguments** | ToolRegistry.validate() | Validated against Draft 2020-12 schema before simulator execution. Returns a structured alidation_error observation. Never reaches simulator. |
| **Stale World Version** | Optimistic concurrency check | Simulator rejects action with stale_precondition. Controller triggers re-planning, re-observes current telemetry, and updates world_version. |
| **Human Approval Denial** | RiskPolicy & ApprovalGateway | High/Critical actions require gateway approval. On denial, execution is blocked; an pproval_denied observation is recorded; the agent re-plans or escalates. |
| **Repetitive Action Loop** | LoopGuard | Hashes canonical JSON fingerprints of (action, arguments). If an identical action repeats beyond the configured limit (2), it is blocked with loop_detected. |
| **Impossible Incident** | External dependency failure | The agent recognizes that internal service health is nominal and customer impact stems from an unmanaged external provider. Safely executes escalate_incident with evidence. |
| **Budget Exhaustion** | BudgetManager | Proactive warnings at <= 2 LLM calls. If limits are reached without resolution, gracefully terminates with udget_exhausted. |

---

## 4. Safety and Stopping

### 4.1 Risk-Gated Human Approval
Risk classifications are loaded dynamically from configs/risk_policy.json:
- **Low Risk** (get_incident, get_metrics, get_logs, get_service_health, get_deployments, get_dependencies, get_runbook, erify_recovery, escalate_incident): Autonomously executable.
- **Medium Risk** (
estart_service, scale_service, clear_cache, close_incident): Autonomous operational mutations.
- **High Risk** (
ollback_deployment, shift_traffic): Requires human authorization via ApprovalGateway.approve().
- **Critical Risk** (ailover_database): Authoritative database failover requires explicit human approval.

When approval is denied, the controller guarantees that the simulator mutation method is never invoked, logging pproval_requested and pproval_result events in the JSONL trace.

### 4.2 Objective Stopping Invariants
A run can never be marked 
esolved based on natural language claims. Under Requirement R10, incident closure mandates:
1. The agent executes erify_recovery.
2. The simulator verifies objective multi-service criteria: checkout completion >= 99%, p95 latency <= 800ms, and critical services healthy.
3. The check yields criteria_met = true with a unique evidence_id (e.g., EV-0009).
4. The agent calls close_incident with the latest evidence_id, current expected_world_version, and a descriptive resolution summary (>= 20 characters).
5. The simulator confirms evidence validity before marking the incident closed.

If criteria cannot be verified or external factors prevent remediation, escalate_incident is the only safe alternative.

---

## 5. Evaluation

The system was evaluated across deterministic scenarios derived from Roll Number 25I-8003 using the openai/gpt-oss-20b model on the Groq API. Evaluation covers the three required public scenarios as well as extended fault modes across the complete simulator fault taxonomy:

| Scenario | Incident Family | Terminal Outcome | LLM Requests (Max 14) | Tool Calls (Max 28) | Plan Revisions | High-Risk Actions | Recovery Proof | Redundant Calls | Runtime |
| :--- | :--- | :--- | :---: | :---: | :---: | :--- | :--- | :---: | :---: |
| **public-a** | memory_leak | **resolved** | 10 | 10 | 0 | 0 proposed | EV-0009 (criteria met) -> Closed in EV-0010 | 0 | 116.8s |
| **public-b** | ad_deploy | **resolved** | 10 | 9 | 0 | 1 approved (
ollback_deployment) | EV-0008 (criteria met) -> Closed in EV-0009 | 0 | 158.5s |
| **public-c** | capacity_spike | **resolved** | 10 | 10 | 0 | 0 proposed | EV-0009 (criteria met) -> Closed in EV-0010 | 0 | 106.1s |
| *(ext) public-d* | database_primary_degraded | **resolved** | 9 | 9 | 0 | 1 approved (ailover_database) | EV-0008 (criteria met) -> Closed in EV-0009 | 0 | 117.1s |
| *(ext) public-e* | cache_corruption | **resolved** | 9 | 8 | 1 | 0 proposed | EV-0007 (criteria met) -> Closed in EV-0008 | 0 | 209.1s |

### Evaluation Summary
- **Public-A (memory_leak)**: While the initial ticket suspected order-service, the agent investigated telemetry and discovered 97% heap saturation on inventory-service. Executed bounded 
estart_service, verified recovery (99.2% checkout success rate), and closed the ticket with full evidence.
- **Public-B (ad_deploy)**: Telemetry revealed release 2.4.1 caused syntax errors on checkout-service. The agent requested operator approval for 
ollback_deployment to version 2.4.0. Upon operator confirmation (APPROVE), rollback was executed, recovery criteria were verified, and the ticket was closed.
- **Public-C (capacity_spike)**: Telemetry indicated queue depth and pod saturation on checkout-service. The agent scaled the deployment to 5 replicas via scale_service, immediately verified global recovery via erify_recovery (EV-0009, criteria met), and closed the incident in EV-0010 in 10 LLM calls.
- **Extended Scenarios (Public-D & Public-E)**: Demonstrates 100% resolution across the remaining fault families. Public-D resolved database saturation via approved ailover_database(order-db) (EV-0008), and Public-E resolved Redis schema corruption via clear_cache(redis-cache) (EV-0007).

---

## 6. Three Failure Traces

### 6.1 Trace 1: Optimistic Concurrency Stale Precondition Handling
- **Context & Symptom**: During execution (	est_verified_resolution_lifecycle), a scheduled background perturbation occurred in the simulator, incrementing world_version from 2 to 3.
- **What the Agent Learned**: When the agent attempted an action referencing expected_world_version: 2, the simulator rejected it with status: stale_precondition (ctual: 3).
- **Recovery Trajectory**: The controller caught the stale condition, triggered 
eplan_triggered, and prompted the agent to re-observe telemetry. The agent called erify_recovery to evaluate the updated environment, captured token EV-0005 at world_version: 3, and successfully closed the incident using the corrected version.

### 6.2 Trace 2: Structured Output Schema Recovery (json_validate_failed & output_parse_failed)
- **Context & Symptom**: During live Groq execution, the provider returned HTTP 400 with output_parse_failed and json_validate_failed due to empty token buffers or malformed nested dictionary keys from the open-weight model.
- **What the Agent Learned**: Treating provider parsing glitches as permanent failures causes premature controller termination.
- **Recovery Trajectory**: GroqModelClient._translate_error was enhanced to classify output_parse_failed and json_validate_failed as TransientModelError. Combined with RetryPolicy exponential backoff and offline fallback plan templates, the runtime caught the failure, waited with jitter, and successfully obtained valid completions on retry.

### 6.3 Trace 3: Loop Guard Intervention on Action Thrashing
- **Context & Symptom**: In unit testing (	est_loop_guard_blocks_repeated_action_in_controller), the agent proposed scaling checkout-service to 3 replicas three consecutive times under identical arguments.
- **What the Agent Learned**: Repeating identical actions without intervening state changes consumes budget without gaining diagnostic value.
- **Recovery Trajectory**: On the third proposal, LoopGuard.record() returned True. The controller intercepted the call before it reached the simulator, generated an internal observation with status: loop_detected, and forced a re-plan. The agent pivoted to safe escalation (escalate_incident), preserving remaining budget.

---

## 7. Limitations

1. **Single-Action Sequential Decision Horizon**: The controller evaluates one tool call per model turn (parallel_tool_calls=False). While guaranteeing atomic state validation and deterministic concurrency, it requires an individual round-trip for each telemetry query, consuming budget faster than batched observation gathering.
2. **Context Window Inflation in Extended Traces**: Tool results accumulate sequentially in message history. In long-running incidents with voluminous log entries, prompt token counts increase steadily, approaching provider per-minute rate limits.
3. **Absence of Long-Term Metric Baselining**: The agent evaluates incident health against fixed numerical thresholds in the local runbook and verification tool. It lacks historical metric baselining, meaning benign temporary traffic surges could be misinterpreted without broader seasonal context.
