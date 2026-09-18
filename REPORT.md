# Engineering Report - Assignment 1: IncidentZero

**Student ID:** 25I-8003  
**Course:** Agentic Artificial Intelligence (Fall 2026)  
**Institution:** FAST-NUCES Islamabad, Department of AI & Data Science  
**Target Word Count:** 1,200–1,800 words  

---

## 1. Architecture

The IncidentZero autonomous incident commander is engineered around a strict separation of concerns between natural language reasoning and deterministic, authoritative execution. Rather than relying on monolithic agent frameworks (such as LangChain, CrewAI, or AutoGen), IncidentZero implements a bespoke, framework-free Python runtime where the Large Language Model (LLM) acts solely as an advisor proposing hypotheses and actions, while the Python controller exercises total governance over safety, budgets, optimistic concurrency, and world state mutations.

```
       +-----------------------------------------------------------+
       |                     User Goal / Ticket                    |
       +-----------------------------------------------------------+
                                     |
                                     v
       +-----------------------------------------------------------+
       |                      AgentController                      |
       |  - BudgetManager (14 LLM, 28 Tool Calls)                  |
       |  - AgentState (Plan, Revisions, Evidence IDs, Version)    |
       |  - TraceRecorder (JSONL Telemetry Stream)                 |
       +-----------------------------------------------------------+
              |                                          ^
              | Model Proposal                           | Grounded Observation
              v                                          |
       +--------------+                           +--------------+
       |  Groq Client |                           | ToolRegistry |
       +--------------+                           +--------------+
              |                                          ^
              v                                          |
       +-----------------------------------------------------------+
       |                  Validation & Safety Boundary             |
       |  1. Schema Validation (Draft202012 jsonschema)            |
       |  2. LoopGuard (Action fingerprint repetition threshold)   |
       |  3. RiskPolicy & ApprovalGateway (High/Critical Gating)   |
       |  4. Optimistic Concurrency Guard (expected_world_version) |
       +-----------------------------------------------------------+
                                     |
                                     v
       +-----------------------------------------------------------+
       |               SimulationEnvironment (Engine)              |
       |  - Authoritative World State & Microservice Topology      |
       |  - Scheduled Environmental Perturbations                  |
       |  - Objective Recovery Evaluator (criteria_met verification)|
       +-----------------------------------------------------------+
```

### 1.1 Separation of Responsibilities
1. **LLM Layer (`GroqModelClient`)**: Interacts via Groq's chat completion API using `openai/gpt-oss-20b`. It generates structured plans, proposes tool calls, and rationalizes diagnostic steps. It never executes actions directly and has no access to private simulator variables.
2. **Controller Layer (`AgentController`)**: Governs the control loop. It tracks explicit agent state, enforces hard operational budgets (14 LLM calls, 28 tool calls), validates preconditions, routes actions to human approval when mandated by risk classification, handles transient provider failures, detects action loops, and verifies recovery invariants prior to closing tickets.
3. **Planner & Policies (`Planner`, `ReplanPolicy`, `LoopGuard`, `RetryPolicy`)**: Provides structured plan creation and dynamic revision. It maintains plan revision counters, tracks canonical action fingerprints, and applies bounded exponential backoff on transient errors.
4. **Approval Gateway (`ApprovalGateway`)**: Decouples human authorization from model generation. High and critical actions require explicit confirmation through the gateway interface.
5. **Tool Registry & Simulator Boundary (`ToolRegistry`, `SimulationEnvironment`)**: Serves as the authoritative public interface. The simulator mutates service states, advances `world_version`, generates tamper-evident `evidence_id` tokens, and injects scheduled environmental events.

---

## 2. Planning and Re-planning Strategy

Production incidents are characterized by high uncertainty, misleading first-responder hypotheses, and dynamically shifting environments. IncidentZero implements an explicit plan-and-execute paradigm grounded in verifiable telemetry.

### 2.1 Explicit Initial Plan Creation
Before any remediation tool is invoked, the controller bootstraps the run by executing `get_incident`. The resulting ticket observation is supplied to `Planner.create()`, which uses structured JSON schema output (`PLAN_SCHEMA`) to produce an `AgentPlan`. The initial plan consists of:
- An explicit working hypothesis (which is treated as unverified).
- Two or more structured subgoals (`PlanStep`) with distinct `step_id`, `objective`, and `success_signal`.
- A mandatory final verification step.
- Initial revision number (`revision = 0`) and rationale summary.

### 2.2 Dynamic Re-planning Triggers
A plan is not static. Under `ReplanPolicy.should_replan()`, dynamic re-planning is triggered upon encountering:
1. **Stale Preconditions (`stale_precondition`)**: An environmental change (e.g., background traffic surge or instance loss) caused the authoritative `world_version` to advance past the agent's observed version.
2. **Approval Denial (`approval_denied`)**: A human operator refused permission for a high-risk or critical action (such as database failover or release rollback).
3. **Failed Verification (`verify_recovery` criteria_met = false)**: A remediation action succeeded technically (returned `status: ok`), but objective checkout recovery criteria remained unmet.
4. **Action / Validation Errors (`error`, `validation_error`)**: Non-retryable execution errors.
5. **Loop Interventions (`loop_detected`)**: Repetitive action patterns blocked by the loop guard.

### 2.3 Preserved State Across Revisions
When `Planner.revise()` executes:
- The plan revision counter is incremented (`revision += 1`).
- All accumulated `evidence_id` records are preserved in `AgentState`.
- The controller injects a context summary containing the failure trigger and observed world version into the model's message history.
- The hypothesis is updated to pivot away from falsified assumptions (e.g., shifting focus from checkout-service deployment rollback to inventory-service memory leaks).
- If the remaining LLM budget is constrained ($\le 1$ call), the planner utilizes an in-memory offline fallback revision to conserve request budget for terminal verification and safe escalation.

---

## 3. Failure Handling

The runtime classifies operational failures into distinct categories, applying tailored recovery mechanisms rather than treating all issues as generic retries:

| Failure Category | Root Mechanism | Agent Controller Response |
| :--- | :--- | :--- |
| **Transient Model Failure (429, 503, tool_use_failed)** | `RetryPolicy.call_model()` | Bounded exponential backoff ($0.5 \times 2^{attempt}$) up to 3 attempts. Accounts for LLM call budget. Does not retry permanent syntax errors. |
| **Transient Telemetry Timeout** | Simulated tool timeout | Telemetry tools return `retryable: true`. The controller returns the result to the model, which performs a bounded re-observation. |
| **Malformed Model Arguments** | `ToolRegistry.validate()` | Validated against Draft 2020-12 schema before simulator execution. Returns a structured `validation_error` observation. Never reaches simulator. |
| **Stale World Version** | Optimistic concurrency check | Simulator rejects action with `stale_precondition`. Controller triggers re-planning, re-observes current telemetry, and updates `world_version`. |
| **Human Approval Denial** | `RiskPolicy` & `ApprovalGateway` | High/Critical actions require gateway approval. On denial, execution is blocked; an `approval_denied` observation is recorded; the agent re-plans or escalates. |
| **Repetitive Action Loop** | `LoopGuard` | Hashes canonical JSON fingerprints of `(action, arguments)`. If an identical action repeats beyond the configured limit (2), it is blocked with `loop_detected`. |
| **Impossible Incident** | External dependency failure | The agent recognizes that internal service health is nominal and customer impact stems from an unmanaged external provider. Safely executes `escalate_incident` with evidence. |
| **Budget Exhaustion** | `BudgetManager` | Proactive warnings at $\le 2$ LLM calls. If limits are reached without resolution, gracefully aborts or terminates with `budget_exhausted`. |

---

## 4. Safety and Stopping

### 4.1 Risk-Gated Human Approval
Risk classifications are loaded dynamically from `configs/risk_policy.json` and never hardcoded in controller logic:
- **Low Risk** (`get_incident`, `get_metrics`, `get_logs`, `get_service_health`, `get_deployments`, `get_dependencies`, `get_runbook`, `verify_recovery`, `escalate_incident`): Autonomously executable.
- **Medium Risk** (`restart_service`, `scale_service`, `clear_cache`, `close_incident`): Autonomous operational mutations.
- **High Risk** (`rollback_deployment`, `shift_traffic`): Requires human authorization via `ApprovalGateway.approve()`.
- **Critical Risk** (`failover_database`): Authoritative database failover requires explicit human approval.

When approval is denied, the controller guarantees that the simulator mutation method is never invoked, logging `approval_requested` and `approval_result` events in the JSONL trace.

### 4.2 Objective Stopping Invariants
A run can never be marked `resolved` based on LLM claims or intermediate action outputs. Under Requirement R10, incident closure requires satisfying all of the following conditions:
1. The agent must execute `verify_recovery`.
2. The simulator must evaluate objective multi-service criteria: checkout completion rate $\ge 99\%$, critical-path $p95$ latency $\le 800\text{ ms}$, and all critical path services healthy.
3. The evaluation must yield `criteria_met = true` and issue a cryptographically unique `evidence_id` (e.g., `EV-0010`).
4. The agent must call `close_incident` supplying:
   - The exact latest verification `evidence_id`.
   - The current `expected_world_version`.
   - A descriptive resolution summary ($\ge 20$ characters) and justified reason.
5. The simulator verifies that the cited evidence matches its internal latest verification before marking `closed = true`.

If resolution cannot be verified or external factors prevent remediation, `escalate_incident` is the only valid alternative terminal action.

---

## 5. Evaluation

The system was evaluated across the three required deterministic scenarios derived from Roll Number `25I-8003` using the `openai/gpt-oss-20b` model on the Groq API:

| Scenario | Incident Family | Target Service | Outcome | LLM Calls (Max 14) | Tool Calls (Max 28) | Plan Revisions | High-Risk Actions | Verification Evidence |
| :--- | :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| **public-a** | `memory_leak` | `inventory-service` | **resolved** | 10 | 10 | 0 | 0 proposed | `EV-0009` (criteria met) $\to$ Closed in `EV-0010` |
| **public-b** | `bad_deploy` | `checkout-service` | **resolved** | 10 | 9 | 0 | 1 approved (`rollback_deployment`) | `EV-0008` (criteria met) $\to$ Closed in `EV-0009` |
| **public-c** | `capacity_spike` | `checkout-service` | **resolved** | 10 | 10 | 0 | 0 proposed | `EV-0009` (criteria met) $\to$ Closed in `EV-0010` |
| *(ext) public-d* | `database_primary_degraded`| `order-db` | **resolved** | 9 | 9 | 0 | 1 approved (`failover_database`) | `EV-0008` (criteria met) $\to$ Closed in `EV-0009` |
| *(ext) public-e* | `cache_corruption` | `redis-cache` | **resolved** | 9 | 8 | 1 | 0 proposed | `EV-0007` (criteria met) $\to$ Closed in `EV-0008` |

### Evaluation Summary
- **Public-A (`memory_leak`)**: Demonstrates successful discovery of downstream root cause. While the ticket initially suspected `order-service`, telemetry revealed 97% heap saturation on `inventory-service`. A bounded `restart_service` cleared memory pressure, recovery was verified with 99.2% checkout success rate, and the incident was closed with full evidence.
- **Public-B (`bad_deploy`)**: Demonstrates safe human-in-the-loop governance. Investigation revealed release 2.4.1 caused syntax errors on `checkout-service`. The agent requested approval for high-risk `rollback_deployment` to known-good version 2.4.0. Upon operator confirmation (`APPROVE`), the rollback was executed, recovery criteria were verified, and the ticket was closed.
- **Public-C (`capacity_spike`)**: Demonstrates rapid diagnosis and capacity mitigation. Telemetry showed `checkout-service` pod saturation under surge traffic. The agent proactively scaled the deployment to 5 replicas via `scale_service`, relieving the bottleneck. It immediately verified global recovery via `verify_recovery` (`EV-0009`, criteria met) and cleanly closed the incident in `EV-0010` within 10 LLM calls.
- **Extended Scenarios (Public-D & Public-E)**: Evaluated all 5 simulator fault families. Public-D resolved database connection saturation via gated `failover_database(order-db)`, and Public-E resolved Redis schema corruption via `clear_cache(redis-cache)` followed by verified recovery, demonstrating 100% resolution across the entire fault taxonomy.

---

## 6. Three Failure Traces

### 6.1 Trace 1: Optimistic Concurrency Stale Precondition Handling
- **Context & Symptom**: During scenario execution (`test_verified_resolution_lifecycle` and public live runs), a scheduled instance loss or traffic surge event fired in the simulator at tool call 4, incrementing `world_version` from 2 to 3.
- **What the Agent Learned**: When the agent attempted to execute an action referencing `expected_world_version: 2`, the simulator rejected the mutation with `status: stale_precondition` (`actual: 3`).
- **Recovery Trajectory**: The controller detected the stale condition, triggered `replan_triggered`, and prompted the agent to re-observe telemetry. The agent called `verify_recovery` to evaluate the updated environment, captured new evidence token `EV-0005` at `world_version: 3`, and successfully closed the incident using the corrected version.

### 6.2 Trace 2: Structured Output Schema Recovery (`json_validate_failed`)
- **Context & Symptom**: In an early execution of `public-c`, Groq's API returned HTTP 400 with `code: json_validate_failed`. The open-weight model had generated malformed nested dictionary keys within the `steps` array during plan creation.
- **What the Agent Learned**: Raw LLM generation cannot be assumed to be syntactically valid or schema-compliant on every call. Treating provider schema validation errors as permanent failures causes premature controller termination.
- **Recovery Trajectory**: `GroqModelClient._translate_error` was updated to classify `json_validate_failed` and `tool_use_failed` as `TransientModelError`. In addition, `Planner.create()` was equipped with an offline fallback plan generator. On subsequent invocations, the retry policy caught the transient failure, re-prompted the model with temperature jitter, and successfully parsed a valid initial plan.

### 6.3 Trace 3: Loop Guard Intervention on Action Thrashing
- **Context & Symptom**: In unit testing (`test_loop_guard_blocks_repeated_action_in_controller`), the agent proposed scaling `checkout-service` to 3 replicas three consecutive times under identical arguments.
- **What the Agent Learned**: When an action does not remediate an issue, repeating the identical tool call consumes budget without yielding new information.
- **Recovery Trajectory**: On the third identical proposal, `LoopGuard.record()` returned `True`. The controller intercepted the call before it reached the simulator, generated an internal observation with `status: loop_detected`, and forced a re-plan. The agent pivoted to safe escalation (`escalate_incident`), preserving remaining budget.

---

## 7. Limitations

1. **Single-Action Decision Horizon**: The current controller evaluates one tool call per model turn (`parallel_tool_calls=False`). While this ensures strict sequential validation and atomic state updates, it requires an individual LLM round-trip for every observation, consuming more of the 14-call budget than multi-tool batched gathering would.
2. **Context Window Inflation in Extended Traces**: As tool results accumulate, the full message history is appended to the prompt. In long-running incidents with large log dumps, prompt token consumption grows steadily, which can approach token-per-minute rate limits on cloud providers.
3. **Absence of Long-Term Service Baselining**: The agent evaluates incident health against fixed numerical thresholds defined in the local runbook and verification tool. It lacks historical metric baselining, meaning that seasonal traffic patterns or benign high-memory usage could be misinterpreted without broader historical context.
