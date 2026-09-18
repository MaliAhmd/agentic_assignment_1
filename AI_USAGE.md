# AI Assistance Declaration

Name: Student  
Roll number: 25I-8003  

## Tools used
Antigravity Coding Assistant (powered by Google Gemini).

## What I used them for
- Initial architectural mapping and code walkthrough of the starter repository contracts.
- Assisting with the formulation of property-based test cases for optimistic concurrency, approval gating, and loop detection.
- Drafting structured markdown documentation and formatting evaluation telemetry tables for the engineering report.

## Two suggestions I rejected or changed
1. **LoopGuard Reset Strategy**: An initial implementation attempted to automatically reset loop guard counts upon receiving a `stale_precondition` result. I rejected this because an agent repeatedly firing an action against an outdated world version constitutes a concurrency loop; resetting the counter allowed infinite stale attempts. The counter reset was changed to trigger only upon intentional, material state changes.
2. **Generic Exception Swallowing in RetryPolicy**: An early proposal caught generic `Exception` inside `RetryPolicy.call_model()`. I rejected this because catching generic exceptions would retry `PermanentModelError`, schema validation mistakes, and unapproved tool actions, violating Requirement R5. I constrained the retry policy strictly to `TransientModelError`.

## One AI-generated or AI-assisted bug I personally diagnosed
- **Symptom**: During live Groq runs, the model failed with HTTP 400 `tool_use_failed` when calling `verify_recovery` because the model output `{"": ""}` in the arguments payload instead of an empty dictionary.
- **Cause**: The model assumed all tool calls required at least one parameter key-value pair, resulting in syntactically invalid JSON for parameterless tools.
- **Fix**: Added explicit prompt constraint Rule 11 in `SYSTEM_PROMPT` instructing the model to emit an empty JSON dictionary `{}` for parameterless tools (`verify_recovery`, `get_incident`), and updated `_translate_error` to classify `tool_use_failed` and `json_validate_failed` as transient retryable errors.

## Code ownership statement
I can explain every submitted component, its failure behavior, and the trade-offs I chose. I understand that the TA may ask me to modify the code during viva.

Signature / typed name: 25I-8003
