# Tool Catalog — Candidate Outreach Copilot

All tools are invoked one-per-turn by the orchestrator based on the model's selected tool call. The orchestrator applies workflow-level validation before executing any write/side-effecting tool.

---

## search_candidates
**Description (for the model):** Search the candidate pool for matches to a free-text query. Use this when the recruiter wants to find, browse, filter, or compare candidates. Read-only.

**Side-effect profile:** READ-ONLY. Safe to call freely.

**Auth:** Recruiter scope `candidates:read`.

**Input contract:**
```json
{
  "query": { "type": "string", "required": true, "description": "Free-text search, may include job ref like REQ-184" },
  "limit": { "type": "integer", "required": false, "default": 10, "max": 50 }
}
```

**Output contract:**
```json
{
  "candidates": [
    { "candidate_id": "string", "name": "string", "top_skills": ["string"], "match_score": "number" }
  ]
}
```

**Common errors:** `RATE_LIMITED`, `INVALID_QUERY`.

---

## get_candidate_profile
**Description (for the model):** Fetch full profile detail for a single candidate by id. Use to review experience before deciding on outreach. Read-only.

**Side-effect profile:** READ-ONLY.

**Auth:** Recruiter scope `candidates:read`.

**Input contract:**
```json
{
  "candidate_id": { "type": "string", "required": true }
}
```

**Output contract:**
```json
{
  "candidate_id": "string",
  "name": "string",
  "years_experience": "number",
  "skills": ["string"],
  "current_title": "string"
}
```

**Common errors:** `NOT_FOUND`.

---

## send_interview_invite
**Description (for the model):** Send an interview invitation to a candidate for a specific requisition only after the recruiter has explicitly confirmed the exact candidate and requisition. The model may request this tool, but the orchestrator is the enforcement point and will block it unless the confirmation gate passes.

**Side-effect profile:** WRITE / SIDE-EFFECTING. Inserts a row into `outreach_events` and dispatches an email to the candidate.

**Auth:** Recruiter scope `outreach:write`.

**Input contract:**
```json
{
  "candidate_id": { "type": "string", "required": true },
  "job_id": { "type": "string", "required": true, "description": "Requisition id, e.g. REQ-184" },
  "send_at": { "type": "string", "required": false, "description": "ISO-8601; omit to send now" },
  "idempotency_key": { "type": "string", "required": false, "description": "Internal orchestrator-supplied key for exactly-once invite sends" }
}
```

**Output contract:**
```json
{
  "event_id": "string",
  "status": "string",
  "candidate_id": "string",
  "job_id": "string",
  "send_at": "string|null",
  "idempotency_key": "string|null"
}
```

**Common errors:** `RATE_LIMITED`, `CANDIDATE_OPTED_OUT`, `INVALID_ARGS`.

**Notes:** The orchestrator supplies a stable idempotency key derived from conversation id + candidate id + requisition id. Reusing that key returns the original event instead of appending a duplicate row. Calls without an idempotency key are supported for compatibility but should not be used by the agent orchestration path.
