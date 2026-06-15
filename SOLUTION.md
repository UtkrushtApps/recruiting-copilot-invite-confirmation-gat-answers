# Solution Steps

1. Reproduce the failure with the trace-backed policy: browse/compare turns can populate `top_candidate_id` and `job_id`, after which the policy may emit `send_interview_invite` even though the recruiter only asked to browse or compare.

2. Identify the root cause as an orchestration/tool-boundary issue, not just a prompt issue: the orchestrator executed every model-selected tool the same way and the invite tool had no confirmation or idempotency contract.

3. Mark the side-effect boundary in code by using `tools.SIDE_EFFECT_PROFILE` inside `OutreachOrchestrator.handle_message`; let read-only tools execute normally, but route write tools through a dedicated gate.

4. Add minimal structured conversation state in the orchestrator: keep search context separately from a `pending_invite`, and only create `pending_invite` when the recruiter explicitly asks to invite an explicit candidate to an explicit requisition.

5. Require a second recruiter turn that explicitly confirms and repeats the same candidate id and requisition id before executing `send_interview_invite`. Reject mismatched or unbound confirmations and block browse/compare-triggered write calls without creating outreach events.

6. Add a stable idempotency key for confirmed invites using conversation id + candidate id + requisition id, and pass it to `send_interview_invite`.

7. Update `send_interview_invite` to store and consult an in-memory idempotency index; repeated calls with the same key return the original event instead of appending a new row.

8. Stop blindly retrying write tools in `_execute_with_retry`; keep retries only for read-only tools. Combined with the idempotency key, this prevents double sends under retries or duplicate turns.

9. Update the tool catalog and prompt documentation to describe the safer intended model behavior, while documenting that the actual guarantee lives in orchestrator/tool code.

10. Verify with `pytest -q`: browse and comparison replay turns leave `outreach_events` empty, unconfirmed invite requests ask for confirmation, and a bound confirmation creates exactly one invite row even with retries enabled.

