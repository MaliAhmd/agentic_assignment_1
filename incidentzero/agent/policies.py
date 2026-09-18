from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from incidentzero.domain.models import RiskLevel


class RiskPolicy:
    def __init__(self, config_path: str | Path = "configs/risk_policy.json") -> None:
        self.mapping = json.loads(Path(config_path).read_text(encoding="utf-8"))

    def risk(self, tool_name: str) -> RiskLevel:
        return RiskLevel(self.mapping.get(tool_name, "critical"))

    def requires_human_approval(self, tool_name: str) -> bool:
        return self.risk(tool_name) in {RiskLevel.HIGH, RiskLevel.CRITICAL}


class ReplanPolicy:
    def should_replan(self, tool_result: dict[str, Any]) -> bool:
        """Evaluate if a tool outcome warrants revising the active incident plan.

        Differentiates among stale world state, human denial, loop detection,
        failed verification, and non-retryable action errors.
        """
        status = tool_result.get("status")
        tool = tool_result.get("tool")
        retryable = tool_result.get("retryable", False)

        # Stale precondition: world state changed after observation
        if status == "stale_precondition":
            return True

        # Human operator denied high/critical action
        if status == "approval_denied":
            return True

        # Action blocked due to loop detection
        if status == "loop_detected":
            return True

        # Failed recovery check when verifying criteria
        if tool == "verify_recovery":
            data = tool_result.get("data")
            if isinstance(data, dict) and data.get("criteria_met") is False:
                return True

        # Close incident attempted but rejected
        if tool == "close_incident" and status != "ok":
            return True

        # Non-retryable error during action execution or validation
        if status in {"error", "validation_error"} and not retryable:
            return True

        # Normal successful observations or transient retryable errors do not force a replan
        return False


class LoopGuard:
    def __init__(self, max_same_action_repeats: int = 2) -> None:
        self.max_same_action_repeats = max_same_action_repeats
        self._counts: dict[str, int] = {}

    def _fingerprint(self, action_name: str, arguments: dict[str, Any]) -> str:
        serialized = json.dumps(arguments, sort_keys=True, default=str)
        return f"{action_name}::{serialized}"

    def record(self, action_name: str, arguments: dict[str, Any]) -> bool:
        """Return True when the exact same action has repeated too often."""
        fp = self._fingerprint(action_name, arguments)
        self._counts[fp] = self._counts.get(fp, 0) + 1
        return self._counts[fp] > self.max_same_action_repeats

    def reset(self) -> None:
        """Reset action counts when environment state changes materially."""
        self._counts.clear()
