# pyrefly: ignore [missing-import]
import pytest
from pathlib import Path

from incidentzero.agent.controller import AgentController
from incidentzero.agent.policies import LoopGuard, ReplanPolicy
from incidentzero.agent.recovery import RetryPolicy
from incidentzero.approval.gateway import AlwaysApproveGateway, AlwaysDenyGateway
from incidentzero.domain.models import ModelReply, ToolCall
from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.model.errors import PermanentModelError, TransientModelError
from incidentzero.model.scripted import ScriptedModelClient
from incidentzero.telemetry.budget import BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Policy & Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.student
def test_replan_policy_recognizes_stale_world():
    policy = ReplanPolicy()
    assert policy.should_replan({"status": "stale_precondition", "retryable": True}) is True


@pytest.mark.student
def test_replan_policy_recognizes_approval_denial():
    policy = ReplanPolicy()
    assert policy.should_replan({"status": "approval_denied", "retryable": False}) is True


@pytest.mark.student
def test_replan_policy_recognizes_loop_detected():
    policy = ReplanPolicy()
    assert policy.should_replan({"status": "loop_detected", "retryable": False}) is True


@pytest.mark.student
def test_replan_policy_recognizes_failed_verification():
    policy = ReplanPolicy()
    result = {
        "status": "ok",
        "tool": "verify_recovery",
        "data": {"criteria_met": False, "checkout_success_rate": 0.85},
    }
    assert policy.should_replan(result) is True


@pytest.mark.student
def test_replan_policy_recognizes_non_retryable_error():
    policy = ReplanPolicy()
    result = {"status": "error", "tool": "restart_service", "retryable": False}
    assert policy.should_replan(result) is True


@pytest.mark.student
def test_replan_policy_ignores_normal_observation():
    policy = ReplanPolicy()
    result = {"status": "ok", "tool": "get_metrics", "data": {"cpu_pct": 35.0}}
    assert policy.should_replan(result) is False


@pytest.mark.student
def test_loop_guard_detects_exact_repeat():
    guard = LoopGuard(max_same_action_repeats=2)
    args = {"service": "checkout-service", "replicas": 4}
    assert guard.record("scale_service", args) is False
    assert guard.record("scale_service", args) is False
    assert guard.record("scale_service", args) is True


@pytest.mark.student
def test_loop_guard_resets_on_demand():
    guard = LoopGuard(max_same_action_repeats=2)
    args = {"service": "checkout-service", "replicas": 4}
    assert guard.record("scale_service", args) is False
    assert guard.record("scale_service", args) is False
    guard.reset()
    assert guard.record("scale_service", args) is False


@pytest.mark.student
def test_retry_policy_retries_transient_only():
    calls = {"n": 0}
    sleeps = []
    retry = RetryPolicy(max_attempts=3, sleeper=lambda s: sleeps.append(s))

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientModelError("429")
        return "ok"

    assert retry.call_model(flaky) == "ok"
    assert calls["n"] == 3

    def permanent():
        raise PermanentModelError("bad request")

    with pytest.raises(PermanentModelError):
        retry.call_model(permanent)


@pytest.mark.student
def test_retry_policy_exhaustion_raises_transient():
    calls = {"n": 0}
    sleeps = []
    retry = RetryPolicy(max_attempts=3, sleeper=lambda s: sleeps.append(s))

    def always_fails():
        calls["n"] += 1
        raise TransientModelError("Persistent 503")

    with pytest.raises(TransientModelError):
        retry.call_model(always_fails)
    assert calls["n"] == 3
    assert len(sleeps) == 2


# ---------------------------------------------------------------------------
# Offline Scripted Controller Tests (Failure Classes & Behavioral Invariants)
# ---------------------------------------------------------------------------

def _dummy_plan_dict():
    return {
        "hypothesis": "Suspected transient regression",
        "rationale_summary": "Investigate telemetry and act if supported",
        "steps": [
            {"step_id": "step_1", "objective": "Inspect telemetry", "success_signal": "Metrics collected"},
            {"step_id": "step_2", "objective": "Verify recovery", "success_signal": "Criteria met"},
        ],
    }


@pytest.mark.student
def test_approval_denial_blocks_tool_execution(tmp_path):
    """Failure Class: Human approval denial.
    High-risk action must be blocked and return approval_denied observation.
    """
    env = SimulationEnvironment("TEST-001", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    decisions = [
        ModelReply(tool_calls=[
            ToolCall(
                id="call-1",
                name="rollback_deployment",
                arguments={
                    "service": "checkout-service",
                    "target_version": "2.4.0",
                    "expected_world_version": 1,
                    "reason": "Suspected faulty release on checkout-service",
                },
            )
        ]),
        # Following denial observation, model decides to escalate
        ModelReply(tool_calls=[
            ToolCall(
                id="call-2",
                name="escalate_incident",
                arguments={
                    "reason": "Rollback approval denied by human operator.",
                    "evidence_ids": ["EV-0001"],
                },
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysDenyGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()

    assert outcome.status == "escalated"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "approval_requested" in trace_content
    assert "approval_denied" in trace_content


@pytest.mark.student
def test_loop_guard_blocks_repeated_action_in_controller(tmp_path):
    """Failure Class: Action repetition loop.
    Repeating exact same action past threshold must be blocked by LoopGuard.
    """
    env = SimulationEnvironment("TEST-002", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    same_call = ToolCall(
        id="call-scale",
        name="scale_service",
        arguments={
            "service": "checkout-service",
            "replicas": 3,
            "expected_world_version": 1,
            "reason": "Attempting to scale to mitigate load",
        },
    )
    decisions = [
        ModelReply(tool_calls=[same_call]),
        ModelReply(tool_calls=[same_call]),
        ModelReply(tool_calls=[same_call]),  # 3rd should be blocked by LoopGuard
        ModelReply(tool_calls=[
            ToolCall(
                id="call-esc",
                name="escalate_incident",
                arguments={"reason": "Loop detected on scale_service", "evidence_ids": ["EV-0001"]},
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "escalated"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "loop_detected" in trace_content


@pytest.mark.student
def test_stale_world_version_triggers_replan(tmp_path):
    """Failure Class: Stale optimistic concurrency precondition.
    Action with stale world_version returns stale_precondition and triggers re-plan.
    """
    env = SimulationEnvironment("TEST-003", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    # Advance world_version behind the scenes by running an action
    registry.execute("restart_service", {
        "service": "auth-service",
        "expected_world_version": 1,
        "reason": "Mutate world version for concurrency testing",
    })
    assert env.world_version == 2

    # Now agent tries with world_version=1
    decisions = [
        ModelReply(tool_calls=[
            ToolCall(
                id="call-stale",
                name="restart_service",
                arguments={
                    "service": "checkout-service",
                    "expected_world_version": 1,
                    "reason": "Restart based on outdated world version 1",
                },
            )
        ]),
        ModelReply(tool_calls=[
            ToolCall(
                id="call-esc",
                name="escalate_incident",
                arguments={"reason": "Stale state handled; escalating safely", "evidence_ids": ["EV-0001"]},
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "escalated"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "stale_precondition" in trace_content
    assert "replan_triggered" in trace_content


@pytest.mark.student
def test_malformed_arguments_rejected_before_simulator(tmp_path):
    """Failure Class: Malformed tool arguments schema error.
    Invalid arguments must be caught by ToolRegistry before simulator execution.
    """
    env = SimulationEnvironment("TEST-004", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    decisions = [
        ModelReply(tool_calls=[
            ToolCall(
                id="call-bad",
                name="scale_service",
                arguments={
                    "service": "checkout-service",
                    "replicas": 99,  # Maximum is 8
                    "expected_world_version": 1,
                    "reason": "Scale beyond maximum allowed limits",
                },
            )
        ]),
        ModelReply(tool_calls=[
            ToolCall(
                id="call-esc",
                name="escalate_incident",
                arguments={"reason": "Validation error handled safely", "evidence_ids": ["EV-0001"]},
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "escalated"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "validation_error" in trace_content


@pytest.mark.student
def test_premature_close_without_verification_rejected(tmp_path):
    """Failure Class: Premature incident close attempt.
    Calling close_incident without verifying criteria must be rejected.
    """
    env = SimulationEnvironment("TEST-005", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    decisions = [
        ModelReply(tool_calls=[
            ToolCall(
                id="call-close",
                name="close_incident",
                arguments={
                    "summary": "Premature close without verification evidence",
                    "evidence_ids": ["EV-0001"],
                    "expected_world_version": 1,
                    "reason": "Attempting to close without running verify_recovery",
                },
            )
        ]),
        ModelReply(tool_calls=[
            ToolCall(
                id="call-esc",
                name="escalate_incident",
                arguments={"reason": "Cannot close without verification; escalating", "evidence_ids": ["EV-0001"]},
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "escalated"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "close_incident" in trace_content


@pytest.mark.student
def test_controller_graceful_budget_exhaustion(tmp_path):
    """Failure Class: Hard budget exhaustion.
    Controller gracefully transitions to budget_exhausted when limits exceeded.
    """
    env = SimulationEnvironment("TEST-006", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    # Only 1 LLM call allowed; initial plan uses 1, next decide raises BudgetExceeded
    decisions = [
        ModelReply(content="Extra decision that should not execute")
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=1, max_tool_calls=5),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "budget_exhausted"


@pytest.mark.student
def test_verified_resolution_lifecycle(tmp_path):
    """Failure/Success Invariant: Verified resolution and optimistic concurrency recovery.
    Remediation followed by verification criteria_met=True and closure yields 'resolved'.
    If world state changes between verification and closure, re-verification restores criteria.
    """
    env = SimulationEnvironment("TEST-RES", "public-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    decisions = [
        # 1. Failover degraded database (Critical risk - requires approval)
        ModelReply(tool_calls=[
            ToolCall(
                id="call-failover",
                name="failover_database",
                arguments={
                    "service": "order-db",
                    "expected_world_version": 1,
                    "reason": "Database primary degraded; failover to healthy replica",
                },
            )
        ]),
        # 2. Verify objective criteria
        ModelReply(tool_calls=[
            ToolCall(
                id="call-verify-1",
                name="verify_recovery",
                arguments={},
            )
        ]),
        # 3. Attempt closure with first version (triggers stale_precondition due to scheduled event)
        ModelReply(tool_calls=[
            ToolCall(
                id="call-close-stale",
                name="close_incident",
                arguments={
                    "summary": "Database failover restored checkout and order services.",
                    "evidence_ids": ["EV-0003"],
                    "expected_world_version": 2,
                    "reason": "Recovery verified with criteria_met=True.",
                },
            )
        ]),
        # 4. Re-verify with updated world state
        ModelReply(tool_calls=[
            ToolCall(
                id="call-verify-2",
                name="verify_recovery",
                arguments={},
            )
        ]),
        # 5. Close incident with verified latest evidence ID and world_version 3
        ModelReply(tool_calls=[
            ToolCall(
                id="call-close-ok",
                name="close_incident",
                arguments={
                    "summary": "Database failover restored checkout and order services after event.",
                    "evidence_ids": ["EV-0005"],
                    "expected_world_version": 3,
                    "reason": "Recovery re-verified with criteria_met=True.",
                },
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict() for _ in range(5)],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=12, max_tool_calls=12),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "resolved"
    trace_content = trace_path.read_text(encoding="utf-8")
    assert "terminal_outcome" in trace_content
    assert "EV-0005" in outcome.evidence_ids


@pytest.mark.student
def test_impossible_incident_escalates_safely(tmp_path):
    """Failure Class: Impossible incident / external dependency.
    When autonomous remediation cannot fix external failures, escalate_incident is safe terminal action.
    """
    env = SimulationEnvironment("STUDENT-5", "hidden-a")
    registry = ToolRegistry(env)
    trace_path = tmp_path / "trace.jsonl"

    decisions = [
        # Check service health
        ModelReply(tool_calls=[
            ToolCall(
                id="call-health",
                name="get_service_health",
                arguments={"service": "payment-service"},
            )
        ]),
        # Recognize external failure and escalate
        ModelReply(tool_calls=[
            ToolCall(
                id="call-esc",
                name="escalate_incident",
                arguments={
                    "reason": "External payment gateway outage detected. Internal mitigation cannot resolve.",
                    "evidence_ids": ["EV-0001", "EV-0002"],
                },
            )
        ]),
    ]
    scripted = ScriptedModelClient(
        decisions=decisions,
        structured_outputs=[_dummy_plan_dict(), _dummy_plan_dict()],
    )
    controller = AgentController(
        model=scripted,
        tools=registry,
        approval=AlwaysApproveGateway(),
        budget=BudgetManager(max_llm_calls=10, max_tool_calls=10),
        trace=TraceRecorder(trace_path),
    )
    outcome = controller.run()
    assert outcome.status == "escalated"
    assert "escalated" in outcome.summary.lower()

