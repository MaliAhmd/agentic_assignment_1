from __future__ import annotations

import json
from typing import Any

from incidentzero.domain.models import AgentPlan, PlanStep
from incidentzero.model.base import ModelClient


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "rationale_summary": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "objective": {"type": "string"},
                    "success_signal": {"type": "string"},
                },
                "required": ["step_id", "objective", "success_signal"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["hypothesis", "rationale_summary", "steps"],
    "additionalProperties": False,
}


class Planner:
    def __init__(self, model: ModelClient) -> None:
        self.model = model

    def create(self, incident_observation: dict[str, Any], context: list[dict[str, Any]] | None = None) -> AgentPlan:
        """Create an explicit initial plan based on environment bootstrap evidence."""
        messages = [
            {
                "role": "system",
                "content": (
                    "Create a short SRE investigation-and-remediation plan. "
                    "Do not assume the ticket's suspected root cause is correct. "
                    "Include at least two subgoals and a final verification step."
                ),
            },
            {"role": "user", "content": f"Incident observation: {incident_observation}"},
        ]
        try:
            raw = self.model.structured(messages, "incident_plan", PLAN_SCHEMA)
            steps = [PlanStep(**row) for row in raw["steps"]]
            return AgentPlan(
                hypothesis=raw["hypothesis"],
                steps=steps,
                revision=0,
                rationale_summary=raw["rationale_summary"],
            )
        except Exception:
            title = incident_observation.get("data", {}).get("title") if isinstance(incident_observation.get("data"), dict) else "active incident"
            return AgentPlan(
                hypothesis=f"Investigate and remediate {title}",
                steps=[
                    PlanStep(step_id="step_1", objective="Inspect health and metrics of critical path services", success_signal="Telemetry gathered"),
                    PlanStep(step_id="step_2", objective="Apply bounded remediation to identified root cause", success_signal="Remediation applied"),
                    PlanStep(step_id="step_3", objective="Verify objective criteria and close incident", success_signal="Criteria met is true"),
                ],
                revision=0,
                rationale_summary="Initial plan grounded in incident ticket; will refine dynamically as evidence accumulates.",
            )

    def revise(self, current: AgentPlan, trigger: dict[str, Any], state_summary: str) -> AgentPlan:
        """Revise the active plan when observations, approvals, or failures contradict it.

        Increments revision, preserves accumulated evidence context, and adapts
        the hypothesis and remaining subgoals.
        """
        new_revision = current.revision + 1
        trigger_status = trigger.get("status", "unknown")
        trigger_tool = trigger.get("tool", "unknown")
        trigger_msg = trigger.get("message", "")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are revising an existing SRE incident remediation plan. "
                    "A plan revision was triggered because previous actions failed, state was stale, "
                    "approval was denied, or verification indicated the incident is not yet resolved. "
                    "Preserve relevant completed findings and evidence, update the working hypothesis, "
                    "and specify the updated remaining steps or safe escalation subgoals."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Current plan (rev {current.revision}):\n"
                    f"Hypothesis: {current.hypothesis}\n"
                    f"Previous rationale: {current.rationale_summary}\n\n"
                    f"Revision trigger: status={trigger_status}, tool={trigger_tool}, message={trigger_msg}\n"
                    f"Trigger details: {json.dumps(trigger, ensure_ascii=False, default=str)}\n"
                    f"State and evidence summary: {state_summary}\n\n"
                    "Provide a revised plan with updated hypothesis, rationale, and remaining steps."
                ),
            },
        ]

        try:
            raw = self.model.structured(messages, "incident_plan_revision", PLAN_SCHEMA)
            steps = [PlanStep(**row) for row in raw["steps"]]
            return AgentPlan(
                hypothesis=raw["hypothesis"],
                steps=steps,
                revision=new_revision,
                rationale_summary=raw["rationale_summary"],
            )
        except Exception:
            # Resilient fallback: ensure offline tests or structured output failures gracefully adapt
            fallback_rationale = (
                f"Revised due to {trigger_status} on {trigger_tool}. "
                f"Preserving gathered context and adapting remaining steps."
            )
            fallback_hypothesis = current.hypothesis
            if trigger_status == "approval_denied":
                fallback_hypothesis = f"Alternative mitigation required: {trigger_tool} denied by operator."
            elif trigger_status == "stale_precondition":
                fallback_hypothesis = f"Environment state advanced; re-observing metrics before acting."
            elif trigger_tool == "verify_recovery":
                fallback_hypothesis = f"Previous action did not achieve recovery criteria; re-evaluating dependencies."

            remaining_steps = [
                PlanStep(
                    step_id="reobserve_state",
                    objective=f"Re-observe telemetry following {trigger_status} on {trigger_tool}",
                    success_signal="Recent metrics gathered",
                ),
                PlanStep(
                    step_id="remediate_or_escalate",
                    objective="Execute alternative remediation or safely escalate if unrecoverable",
                    success_signal="Incident recovery verified or escalated",
                ),
                PlanStep(
                    step_id="verify_and_close",
                    objective="Verify checkout criteria and close incident",
                    success_signal="Recovery criteria_met is true",
                ),
            ]
            return AgentPlan(
                hypothesis=fallback_hypothesis,
                steps=remaining_steps,
                revision=new_revision,
                rationale_summary=fallback_rationale,
            )
