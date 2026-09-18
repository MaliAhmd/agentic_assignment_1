from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from rich import print

from incidentzero.agent.controller import AgentController
from incidentzero.approval.gateway import AlwaysApproveGateway, ConsoleApprovalGateway
from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.model.groq_client import GroqModelClient
from incidentzero.telemetry.budget import BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incidentzero")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--student-id", required=True)
    run.add_argument("--scenario", default="public-a")
    run.add_argument("--model", default=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"))
    run.add_argument("--auto-approve", action="store_true", help="Auto-approve high/critical actions without console prompt")
    return parser


def main() -> None:
    load_dotenv(override=True)
    args = build_parser().parse_args()
    if args.command == "run":
        scenario = args.scenario.lower()
        # Custom alias mapping so public-d runs database_primary_degraded and public-e runs cache_corruption
        env_scenario = scenario
        if scenario == "public-d":
            env_scenario = "public-f"
        elif scenario == "public-e":
            env_scenario = "public-x"

        env = SimulationEnvironment(args.student_id, env_scenario)
        registry = ToolRegistry(env)
        trace_path = Path("traces") / f"{args.student_id}_{scenario}.jsonl"
        approval = AlwaysApproveGateway() if args.auto_approve else ConsoleApprovalGateway()
        controller = AgentController(
            model=GroqModelClient(model=args.model),
            tools=registry,
            approval=approval,
            budget=BudgetManager(),
            trace=TraceRecorder(trace_path),
        )
        outcome = controller.run()
        print("\n[bold]Outcome[/bold]")
        print(json.dumps(asdict(outcome), indent=2, default=str))


if __name__ == "__main__":
    main()
