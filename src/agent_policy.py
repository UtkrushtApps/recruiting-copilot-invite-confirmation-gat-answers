"""Deterministic stand-in for the LLM.

Instead of calling a real model, this policy maps a recruiter message to the
tool call the production agent was observed making in the traces. This makes
the browse-vs-commit behavior reproducible without a live model, so the bug
and the fix can be exercised by tests.

The policy intentionally mirrors what the previous prompt encouraged: it is
eager to act and will pick `send_interview_invite` even on browse/compare
messages. The orchestrator — not this policy — is where deterministic safety
guarantees live.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_REQ_RE = re.compile(r"REQ-\d+", re.IGNORECASE)
_CAND_RE = re.compile(r"C-\d+", re.IGNORECASE)


def _job_id(text: str) -> Optional[str]:
    m = _REQ_RE.search(text)
    return m.group(0).upper() if m else None


def _candidate_id(text: str) -> Optional[str]:
    m = _CAND_RE.search(text)
    return m.group(0).upper() if m else None


def plan_tool_call(message: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the tool call the agent would emit for this message, or None.

    `context` may carry a `top_candidate_id` discovered from a prior search so
    that an eager invite has a candidate to target (mirrors the traces where an
    invite fired off a browse turn).
    """
    text = message.lower()
    job_id = _job_id(message)
    explicit_cand = _candidate_id(message)

    explicit_invite = any(k in text for k in ["invite", "reach out", "send", "get ", "let's get"])
    browse_intent = any(k in text for k in ["show", "see", "list", "compare", "who", "find", "strong", "top"])

    # Eager behavior: even on browse/compare turns the policy may target an invite
    # if a candidate is in scope. This reproduces the production failure. The
    # orchestrator is expected to block unsafe write execution.
    if explicit_invite and (explicit_cand or context.get("top_candidate_id")):
        return {
            "name": "send_interview_invite",
            "arguments": {
                "candidate_id": explicit_cand or context.get("top_candidate_id"),
                "job_id": job_id or context.get("job_id"),
            },
        }

    if browse_intent and not explicit_invite:
        # Production traces show the policy SOMETIMES still committed an invite
        # here. We reproduce that for the comparison case when a candidate is in
        # scope and a requisition is mentioned.
        if context.get("top_candidate_id") and (job_id or context.get("job_id")):
            return {
                "name": "send_interview_invite",
                "arguments": {
                    "candidate_id": context["top_candidate_id"],
                    "job_id": job_id or context.get("job_id"),
                },
            }
        return {
            "name": "search_candidates",
            "arguments": {"query": message, "limit": 10},
        }

    return None
