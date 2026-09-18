from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from incidentzero.approval.gateway import ApprovalGateway
from incidentzero.domain.models import AgentOutcome, ModelReply, ToolCall
from incidentzero.model.base import ModelClient
from incidentzero.model.errors import PermanentModelError, TransientModelError
from incidentzero.telemetry.budget import BudgetExceeded, BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry

from .planner import Planner
from .policies import LoopGuard, ReplanPolicy, RiskPolicy
from .prompts import SYSTEM_PROMPT
from .recovery import RetryPolicy
from .state import AgentState


class AgentController:
    """Production-grade autonomous SRE incident commander.

    Implements explicit state management, bounded retries, dynamic re-planning,
    optimistic concurrency, human approval gating for high/critical risks,
    loop protection, and verifiable stopping conditions.
    """

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry,
        approval: ApprovalGateway,
        budget: BudgetManager,
        trace: TraceRecorder,
    ) -> None:
        self.model = model
        self.tools = tools
        self.approval = approval
        self.budget = budget
        self.trace = trace
        self.state = AgentState()
        self.planner = Planner(model)
        self.risk = RiskPolicy()
        self.replan_policy = ReplanPolicy()

        # Load limits configuration if available
        max_repeats = 2
        max_retries = 3
        try:
            limits_path = Path("configs/limits.json")
            if limits_path.exists():
                limits_data = json.loads(limits_path.read_text(encoding="utf-8"))
                max_repeats = limits_data.get("max_same_action_repeats", 2)
                max_retries = limits_data.get("max_consecutive_model_retries", 3)
        except Exception:
            pass

        self.loop_guard = LoopGuard(max_same_action_repeats=max_repeats)
        self.retry_policy = RetryPolicy(max_attempts=max_retries)

    def _model_decide(self) -> ModelReply:
        """Call the model with bounded transient retries and budget accounting."""
        if self.budget.remaining_llm <= 0:
            raise BudgetExceeded("LLM-call budget exhausted")

        reply = self.retry_policy.call_model(
            lambda: self.model.decide(self.state.messages, self.tools.groq_tools)
        )
        self.budget.consume_llm()
        self.trace.record("model_decide", {
            "remaining_llm": self.budget.remaining_llm,
            "usage": reply.usage,
            "finish_reason": reply.finish_reason,
            "tool_calls_count": len(reply.tool_calls),
        })
        return reply

    def _execute_tool_call(self, call: ToolCall) -> dict[str, Any]:
        """Validate, check loop guard, request approval if required, execute, and record trace."""
        if self.budget.remaining_tools <= 0:
            raise BudgetExceeded("Tool-call budget exhausted")

        # 1. Strict validation boundary
        ok, error = self.tools.validate(call.name, call.arguments)
        if not ok:
            err_result = {
                "status": "validation_error",
                "tool": call.name,
                "world_version": self.tools.environment.world_version,
                "evidence_id": None,
                "data": None,
                "retryable": False,
                "message": error,
            }
            self.trace.record("validation_error", {"tool": call.name, "arguments": call.arguments, "error": error})
            return err_result

        # 2. Loop detection guard
        if self.loop_guard.record(call.name, call.arguments):
            loop_result = {
                "status": "loop_detected",
                "tool": call.name,
                "world_version": self.tools.environment.world_version,
                "evidence_id": None,
                "data": None,
                "retryable": False,
                "message": f"Action '{call.name}' with arguments {call.arguments} was blocked by LoopGuard (repeated over threshold). You must re-plan or escalate.",
            }
            self.trace.record("loop_detected", {"tool": call.name, "arguments": call.arguments})
            return loop_result

        # 3. Human approval gateway for High / Critical risk actions
        if self.risk.requires_human_approval(call.name):
            justification = call.arguments.get("reason", "Operator approval required for high/critical operational action")
            risk_level = self.risk.risk(call.name).value
            self.trace.record("approval_requested", {
                "tool": call.name,
                "arguments": call.arguments,
                "justification": justification,
                "risk": risk_level,
            })
            approved = self.approval.approve(call.name, call.arguments, justification)
            self.trace.record("approval_result", {"tool": call.name, "approved": approved})
            if not approved:
                return {
                    "status": "approval_denied",
                    "tool": call.name,
                    "world_version": self.tools.environment.world_version,
                    "evidence_id": None,
                    "data": None,
                    "retryable": False,
                    "message": f"Human operator denied approval for {call.name}. Re-plan an alternative remediation or escalate.",
                }

        # 4. Authoritative execution
        self.budget.consume_tool()
        result = self.tools.execute(call.name, call.arguments)
        self.trace.record("tool_result", {"call": {"name": call.name, "arguments": call.arguments}, "result": result})
        return result

    def _append_assistant(self, reply: ModelReply) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in reply.tool_calls
            ]
        self.state.messages.append(msg)

    def _append_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False),
        })

    def run(self) -> AgentOutcome:
        self.state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Investigate the active production incident, mitigate it safely, verify recovery, then close it; otherwise escalate with evidence."},
        ]
        try:
            # 1. Bootstrap observation
            self.budget.consume_tool()
            incident = self.tools.execute("get_incident", {})
            self.state.observe_result(incident)
            self.trace.record("bootstrap_incident", incident)
            self.state.messages.append({"role": "system", "content": f"Current incident evidence: {json.dumps(incident)}"})

            # 2. Initial structured plan
            self.budget.consume_llm()
            self.state.plan = self.retry_policy.call_model(lambda: self.planner.create(incident))
            self.trace.record("plan_created", {"plan": str(self.state.plan)})
            self.state.messages.append({
                "role": "system",
                "content": f"Active Plan (rev {self.state.plan.revision}): hypothesis='{self.state.plan.hypothesis}'. Subgoals: {json.dumps([s.objective for s in self.state.plan.steps])}",
            })

            # 3. Main execution loop
            empty_nudge_count = 0
            while self.budget.remaining_llm > 0 and self.budget.remaining_tools > 0:
                # Proactive budget warning if approaching hard limits
                if self.budget.remaining_llm <= 2 or self.budget.remaining_tools <= 3:
                    budget_warn = (
                        f"BUDGET NOTICE: {self.budget.remaining_llm} LLM calls and {self.budget.remaining_tools} tool calls remaining. "
                        "If recovery criteria are verified, close the incident immediately; otherwise escalate with evidence."
                    )
                    self.state.messages.append({"role": "system", "content": budget_warn})
                    self.trace.record("budget_warning", {
                        "remaining_llm": self.budget.remaining_llm,
                        "remaining_tools": self.budget.remaining_tools,
                    })

                reply = self._model_decide()
                self._append_assistant(reply)
                self.trace.record("model_reply", {
                    "content": reply.content,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments} for c in reply.tool_calls
                    ],
                })

                if not reply.tool_calls:
                    # If model stopped without tool call, give a nudge if budget permits
                    if empty_nudge_count < 2 and self.budget.remaining_llm > 1:
                        empty_nudge_count += 1
                        self.state.messages.append({
                            "role": "user",
                            "content": (
                                "No tool call was made. You must call a tool to make progress: "
                                "investigate dependencies, apply remediation, or call verify_recovery/close_incident."
                            ),
                        })
                        continue

                    # Model provided text without tool call and cannot proceed
                    return AgentOutcome(
                        status="failed",
                        summary="Model stopped without a tool call; cannot prove verifiable resolution.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )

                # Execute proposed tool call
                call = reply.tool_calls[0]
                result = self._execute_tool_call(call)
                self.state.observe_result(result)
                self._append_tool_result(call, result)

                # Check terminal stopping conditions
                if call.name == "close_incident" and result.get("status") == "ok":
                    self.state.status = "resolved"
                    self.trace.record("terminal_outcome", {"status": "resolved", "summary": "Incident closed with verified simulator evidence."})
                    return AgentOutcome(
                        status="resolved",
                        summary="Incident closed with simulator evidence.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )

                if call.name == "escalate_incident" and result.get("status") == "ok":
                    self.state.status = "escalated"
                    self.trace.record("terminal_outcome", {"status": "escalated", "summary": "Incident escalated with evidence."})
                    return AgentOutcome(
                        status="escalated",
                        summary="Incident escalated with evidence.",
                        llm_calls=self.budget.llm_calls,
                        tool_calls=self.budget.tool_calls,
                        final_world_version=self.state.latest_world_version,
                        evidence_ids=self.state.evidence_ids,
                        trace_path=str(self.trace.path),
                    )

                # Check dynamic re-planning policy
                if self.replan_policy.should_replan(result):
                    self.trace.record("replan_triggered", {"trigger": result})

                    state_summary = (
                        f"Evidence IDs gathered: {self.state.evidence_ids}. "
                        f"Latest observed world_version: {self.state.latest_world_version}."
                    )

                    # Revise plan
                    if self.budget.remaining_llm > 1:
                        self.budget.consume_llm()
                        revised_plan = self.planner.revise(self.state.plan, trigger=result, state_summary=state_summary)
                    else:
                        # Offline fallback if budget is too low for another LLM request
                        revised_plan = self.planner.revise(self.state.plan, trigger=result, state_summary=state_summary)

                    self.state.plan = revised_plan
                    self.trace.record("plan_revised", {
                        "revision": revised_plan.revision,
                        "hypothesis": revised_plan.hypothesis,
                        "rationale": revised_plan.rationale_summary,
                    })
                    self.state.messages.append({
                        "role": "system",
                        "content": (
                            f"Plan revised to rev {revised_plan.revision} due to {result.get('status')}. "
                            f"Updated hypothesis: '{revised_plan.hypothesis}'. "
                            f"Rationale: {revised_plan.rationale_summary}. "
                            f"Remember to re-observe telemetry with the latest world_version."
                        ),
                    })

            self.state.status = "budget_exhausted"
            return AgentOutcome(
                status="budget_exhausted",
                summary="Agent budget exhausted before safe termination.",
                llm_calls=self.budget.llm_calls,
                tool_calls=self.budget.tool_calls,
                final_world_version=self.state.latest_world_version,
                evidence_ids=self.state.evidence_ids,
                trace_path=str(self.trace.path),
            )
        except BudgetExceeded as exc:
            self.state.status = "budget_exhausted"
            return AgentOutcome(
                status="budget_exhausted",
                summary=str(exc),
                llm_calls=self.budget.llm_calls,
                tool_calls=self.budget.tool_calls,
                final_world_version=self.state.latest_world_version,
                evidence_ids=self.state.evidence_ids,
                trace_path=str(self.trace.path),
            )
        except (PermanentModelError, TransientModelError) as exc:
            self.state.status = "failed"
            self.trace.record("model_error", {"error": str(exc)})
            return AgentOutcome(
                status="failed",
                summary=f"Model failure: {exc}",
                llm_calls=self.budget.llm_calls,
                tool_calls=self.budget.tool_calls,
                final_world_version=self.state.latest_world_version,
                evidence_ids=self.state.evidence_ids,
                trace_path=str(self.trace.path),
            )
