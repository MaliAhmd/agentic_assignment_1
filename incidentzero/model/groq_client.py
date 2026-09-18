from __future__ import annotations

import json
import os
from typing import Any

# pyrefly: ignore [missing-import]
from groq import Groq

from incidentzero.domain.models import ModelReply, ToolCall
from .errors import PermanentModelError, TransientModelError


class GroqModelClient:
    def __init__(self, api_key: str | None = None, model: str = "openai/gpt-oss-20b") -> None:
        self.model = model
        self.client = Groq(api_key=api_key or os.getenv("GROQ_API_KEY"))

    def _translate_error(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if status in {408, 409, 429, 500, 502, 503, 504}:
            return TransientModelError(str(exc))
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error", {})
            if isinstance(err, dict):
                code = err.get("code")
                if code in {"tool_use_failed", "output_parse_failed", "rate_limit_exceeded", "json_validate_failed"}:
                    return TransientModelError(str(exc))
        err_str = str(exc).lower()
        if any(marker in err_str for marker in ("tool_use_failed", "output_parse_failed", "parsing failed", "failed to parse", "json_validate_failed", "does not match the expected schema", "attempted to call tool", "not in request.tools")):
            return TransientModelError(str(exc))
        return PermanentModelError(str(exc))

    def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        import time
        import uuid
        valid_tool_names = {t["function"]["name"] for t in tools}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    temperature=0.1 + (0.15 * attempt),
                    reasoning_effort="low",
                )
                msg = response.choices[0].message
                calls: list[ToolCall] = []
                for call in (msg.tool_calls or []):
                    clean_name = call.function.name.split("<|")[0].strip()
                    try:
                        args = json.loads(call.function.arguments)
                    except Exception:
                        args = {"__malformed_arguments__": call.function.arguments}
                    calls.append(ToolCall(id=call.id, name=clean_name, arguments=args))
                usage = {}
                if getattr(response, "usage", None):
                    usage = {
                        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
                    }
                return ModelReply(
                    content=msg.content,
                    tool_calls=calls,
                    usage=usage,
                    finish_reason=response.choices[0].finish_reason,
                )
            except Exception as exc:
                # Recover if Groq server rejected the tool call only due to channel delimiter suffix in tool name
                body = getattr(exc, "body", None)
                if isinstance(body, dict):
                    err = body.get("error", {})
                    failed_gen = err.get("failed_generation")
                    if failed_gen and isinstance(failed_gen, str):
                        try:
                            gen_data = json.loads(failed_gen)
                            raw_name = gen_data.get("name", "")
                            clean_name = raw_name.split("<|")[0].strip()
                            if clean_name in valid_tool_names:
                                args = gen_data.get("arguments", {})
                                if isinstance(args, str):
                                    args = json.loads(args) if args else {}
                                call_id = f"call_{uuid.uuid4().hex[:8]}"
                                return ModelReply(
                                    content=None,
                                    tool_calls=[ToolCall(id=call_id, name=clean_name, arguments=args)],
                                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    finish_reason="tool_calls",
                                )
                        except Exception:
                            pass

                translated = self._translate_error(exc)
                if isinstance(translated, PermanentModelError) or attempt == 2:
                    raise translated from exc
                last_exc = translated
                time.sleep(0.5 * (2 ** attempt))
        if last_exc:
            raise last_exc
        raise PermanentModelError("Failed to get model decision.")

    def structured(self, messages: list[dict[str, Any]], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                temperature=0.1,
                reasoning_effort="low",
            )
        except Exception as exc:
            raise self._translate_error(exc) from exc
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise TransientModelError(f"Model returned invalid structured JSON: {content[:160]}") from exc
