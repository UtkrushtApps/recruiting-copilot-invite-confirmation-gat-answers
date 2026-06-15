"""Tool implementations for the Candidate Outreach Copilot.

The `outreach_events` store is in-memory so the package needs no external
infrastructure. Each tool has a `side_effect` marker the orchestrator can
inspect. `search_candidates` and `get_candidate_profile` are read-only;
`send_interview_invite` writes a row and (notionally) emails the candidate.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List

# In-memory source of truth for what was actually sent.
outreach_events: List[Dict[str, Any]] = []
_event_counter = itertools.count(90100)

# Internal idempotency index. The orchestrator supplies a stable key for the
# confirmed invite side effect so retries/repeated turns return the same event
# instead of appending a duplicate outreach row.
_idempotency_index: Dict[str, Dict[str, Any]] = {}

# A tiny fixed candidate pool so tools return deterministic data.
_CANDIDATES = {
    "C-2042": {"name": "Priya N.", "top_skills": ["Python", "Django"], "match_score": 0.93,
                "years_experience": 7, "current_title": "Senior Backend Engineer"},
    "C-3310": {"name": "Alex M.", "top_skills": ["Go", "Postgres"], "match_score": 0.91,
                "years_experience": 8, "current_title": "Staff Engineer"},
    "C-3311": {"name": "Dana K.", "top_skills": ["Java", "Kafka"], "match_score": 0.88,
                "years_experience": 6, "current_title": "Backend Engineer"},
}


@dataclass
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] | None = None


def search_candidates(query: str, limit: int = 10) -> ToolResult:
    """READ-ONLY. Return candidate matches for a free-text query."""
    if not query:
        return ToolResult(ok=False, error={"code": "INVALID_QUERY", "message": "query is required"})
    candidates = [
        {"candidate_id": cid, "name": c["name"], "top_skills": c["top_skills"],
         "match_score": c["match_score"]}
        for cid, c in _CANDIDATES.items()
    ][:limit]
    return ToolResult(ok=True, data={"candidates": candidates})


def get_candidate_profile(candidate_id: str) -> ToolResult:
    """READ-ONLY. Return full detail for one candidate."""
    c = _CANDIDATES.get(candidate_id)
    if not c:
        return ToolResult(ok=False, error={"code": "NOT_FOUND", "message": candidate_id})
    return ToolResult(ok=True, data={
        "candidate_id": candidate_id, "name": c["name"],
        "years_experience": c["years_experience"], "skills": c["top_skills"],
        "current_title": c["current_title"],
    })


def get_outreach_event_by_idempotency_key(idempotency_key: str | None) -> Dict[str, Any] | None:
    """Return a previously sent event for an idempotency key, if present.

    A shallow copy is returned so callers cannot mutate the source-of-truth row
    in `outreach_events`.
    """
    if not idempotency_key:
        return None
    event = _idempotency_index.get(idempotency_key)
    return dict(event) if event is not None else None


def send_interview_invite(
    candidate_id: str,
    job_id: str,
    send_at: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResult:
    """WRITE / SIDE-EFFECTING. Insert a row into outreach_events and email the candidate.

    When `idempotency_key` is supplied, the operation is exactly-once for that
    key: a repeated call returns the original event and does not append another
    row or send another email. The orchestrator is responsible for supplying a
    stable key after the confirmation gate passes.
    """
    if not candidate_id or not job_id:
        return ToolResult(ok=False, error={"code": "INVALID_ARGS", "message": "candidate_id and job_id required"})

    if idempotency_key and idempotency_key in _idempotency_index:
        replay = dict(_idempotency_index[idempotency_key])
        replay["idempotent_replay"] = True
        return ToolResult(ok=True, data=replay)

    event = {
        "event_id": f"evt-{next(_event_counter)}",
        "status": "sent",
        "candidate_id": candidate_id,
        "job_id": job_id,
        "send_at": send_at,
        "idempotency_key": idempotency_key,
    }
    outreach_events.append(event)
    if idempotency_key:
        _idempotency_index[idempotency_key] = event
    return ToolResult(ok=True, data=dict(event))


# Tool registry. The orchestrator looks up tools here to execute them.
TOOLS = {
    "search_candidates": search_candidates,
    "get_candidate_profile": get_candidate_profile,
    "send_interview_invite": send_interview_invite,
}

# Side-effect profile per tool. The orchestrator uses this as the deterministic
# boundary between model-selected read actions and gated write actions.
SIDE_EFFECT_PROFILE = {
    "search_candidates": "read",
    "get_candidate_profile": "read",
    "send_interview_invite": "write",
}


def reset_store() -> None:
    """Test helper to clear the in-memory store between runs."""
    global _event_counter
    outreach_events.clear()
    _idempotency_index.clear()
    _event_counter = itertools.count(90100)
