"""Request handler / tool-call loop for the Candidate Outreach Copilot.

This is the orchestration layer behind POST /api/agent/candidate-outreach.
It builds context, asks the policy (LLM stand-in) for a tool call, executes
the selected tool, and returns an assistant response.

The orchestrator is the deterministic safety boundary for side effects. It
allows read-only tools to execute normally, but write tools must pass an
explicit confirmation gate bound to the selected candidate and requisition.
Confirmed invite sends use a stable idempotency key so retries or duplicate
turns do not double-write `outreach_events`.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from . import tools
from .agent_policy import plan_tool_call

_REQ_RE = re.compile(r"REQ-\d+", re.IGNORECASE)
_CAND_RE = re.compile(r"C-\d+", re.IGNORECASE)


class OutreachOrchestrator:
    def __init__(self) -> None:
        # Per-conversation context (selected candidate, requisition, pending
        # confirmation, etc.). This is intentionally small and structured so
        # the model transcript is not the source of truth for write safety.
        self._context: Dict[str, Dict[str, Any]] = {}

    def _ctx(self, conversation_id: str) -> Dict[str, Any]:
        return self._context.setdefault(conversation_id, {})

    def handle_message(self, conversation_id: str, message: str,
                       max_retries: int = 1) -> Dict[str, Any]:
        """Process one recruiter message and return an assistant response.

        Returns a dict: {"assistant": str, "tool_call": dict|None, "tool_result": dict|None}.
        """
        ctx = self._ctx(conversation_id)

        # Handle confirmations before asking the model/policy. This supports
        # confirmations such as "yes, confirm C-3310 to REQ-211" even if the
        # policy would not select a tool for that phrasing.
        pending = ctx.get("pending_invite")
        if pending and self._is_cancel_message(message):
            ctx.pop("pending_invite", None)
            return {"assistant": "Okay — I will not send that interview invite.",
                    "tool_call": None, "tool_result": None}

        if pending and self._is_confirmation_message(message):
            msg_candidate = self._candidate_id(message)
            msg_job = self._job_id(message)
            if msg_candidate == pending.get("candidate_id") and msg_job == pending.get("job_id"):
                return self._execute_confirmed_invite(
                    conversation_id=conversation_id,
                    invite=pending,
                    max_retries=max_retries,
                )
            return {"assistant": self._confirmation_prompt(pending, mismatch=True),
                    "tool_call": None,
                    "tool_result": {"error": {"code": "CONFIRMATION_MISMATCH"}}}

        call = plan_tool_call(message, ctx)

        if call is None:
            return {"assistant": "How can I help with this requisition?",
                    "tool_call": None, "tool_result": None}

        name = call["name"]
        args = call.get("arguments", {})
        fn = tools.TOOLS.get(name)
        if fn is None:
            return {"assistant": f"Unknown tool {name}.",
                    "tool_call": call, "tool_result": None}

        side_effect_profile = tools.SIDE_EFFECT_PROFILE.get(name, "write")
        if side_effect_profile == "write":
            return self._handle_write_tool_call(
                conversation_id=conversation_id,
                message=message,
                call=call,
                max_retries=max_retries,
            )

        result = self._execute_with_retry(
            fn,
            args,
            max_retries=max_retries,
            side_effect_profile=side_effect_profile,
        )

        # Remember a top candidate from a search so later turns can target it,
        # but do not treat it as approval to send outreach.
        if name == "search_candidates" and result.ok:
            cands = result.data.get("candidates", [])
            if cands:
                ctx["top_candidate_id"] = cands[0]["candidate_id"]
            if args.get("query"):
                m = _REQ_RE.search(args["query"])
                if m:
                    ctx["job_id"] = m.group(0).upper()

        assistant = self._render_reply(name, args, result)
        return {"assistant": assistant, "tool_call": call,
                "tool_result": (result.data if result.ok else {"error": result.error})}

    def _handle_write_tool_call(self, conversation_id: str, message: str,
                                call: Dict[str, Any], max_retries: int) -> Dict[str, Any]:
        """Gate all model-selected write tools before execution.

        Today `send_interview_invite` is the only write tool. Keeping this gate
        generic around the side-effect profile ensures future writes are not
        accidentally executed just because the model selected them.
        """
        name = call["name"]
        if name != "send_interview_invite":
            return {"assistant": f"I need explicit confirmation before running {name}.",
                    "tool_call": call,
                    "tool_result": {"error": {"code": "WRITE_REQUIRES_CONFIRMATION"}}}

        ctx = self._ctx(conversation_id)
        raw_args = call.get("arguments", {})
        candidate_id = self._normalize_candidate_id(raw_args.get("candidate_id"))
        job_id = self._normalize_job_id(raw_args.get("job_id"))
        if not candidate_id or not job_id:
            return {"assistant": "I need both a candidate id and requisition id before preparing an invite.",
                    "tool_call": call,
                    "tool_result": {"error": {"code": "INVALID_ARGS"}}}

        invite = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "send_at": raw_args.get("send_at"),
        }
        idempotency_key = self._idempotency_key(conversation_id, candidate_id, job_id)

        # If this exact confirmed invite was already sent, replay the existing
        # result instead of requiring another pending state or appending a row.
        existing = tools.get_outreach_event_by_idempotency_key(idempotency_key)
        if existing is not None:
            replay = dict(existing)
            replay["idempotent_replay"] = True
            result = tools.ToolResult(ok=True, data=replay)
            ctx.setdefault("sent_invites", set()).add(self._invite_key(candidate_id, job_id))
            return {"assistant": self._render_reply(name, {**invite, "idempotency_key": idempotency_key}, result),
                    "tool_call": {"name": name, "arguments": {**invite, "idempotency_key": idempotency_key}},
                    "tool_result": result.data}

        pending = ctx.get("pending_invite")
        msg_candidate = self._candidate_id(message)
        msg_job = self._job_id(message)

        if (pending
                and self._is_confirmation_message(message)
                and msg_candidate == pending.get("candidate_id")
                and msg_job == pending.get("job_id")):
            return self._execute_confirmed_invite(
                conversation_id=conversation_id,
                invite=pending,
                max_retries=max_retries,
            )

        # A recruiter asking to invite/reach out starts a pending action, but
        # does not send. The required second step must explicitly confirm the
        # same candidate+job pair.
        if (self._has_invite_request(message)
                and msg_candidate == candidate_id
                and msg_job == job_id
                and not self._is_confirmation_message(message)):
            ctx["pending_invite"] = invite
            return {"assistant": self._confirmation_prompt(invite),
                    "tool_call": call,
                    "tool_result": {"error": {"code": "INVITE_REQUIRES_CONFIRMATION"}}}

        # This is the trace-backed failure mode: the policy selected a write
        # during a browse/compare turn using candidate/job context. Do not set a
        # pending invite, because the recruiter did not ask for outreach.
        return {"assistant": ("I will not send an interview invite without an explicit "
                              "candidate-and-requisition confirmation. I can keep browsing "
                              "or comparing candidates if you'd like."),
                "tool_call": call,
                "tool_result": {"error": {"code": "WRITE_BLOCKED_NO_CONFIRMATION"}}}

    def _execute_confirmed_invite(self, conversation_id: str, invite: Dict[str, Any],
                                  max_retries: int) -> Dict[str, Any]:
        candidate_id = self._normalize_candidate_id(invite.get("candidate_id"))
        job_id = self._normalize_job_id(invite.get("job_id"))
        args = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "send_at": invite.get("send_at"),
            "idempotency_key": self._idempotency_key(conversation_id, candidate_id, job_id),
        }
        call = {"name": "send_interview_invite", "arguments": args}
        result = self._execute_with_retry(
            tools.send_interview_invite,
            args,
            max_retries=max_retries,
            side_effect_profile="write",
        )

        ctx = self._ctx(conversation_id)
        if result.ok:
            ctx.pop("pending_invite", None)
            ctx.setdefault("sent_invites", set()).add(self._invite_key(candidate_id, job_id))

        return {"assistant": self._render_reply("send_interview_invite", args, result),
                "tool_call": call,
                "tool_result": (result.data if result.ok else {"error": result.error})}

    def _execute_with_retry(self, fn, args: Dict[str, Any], max_retries: int,
                            side_effect_profile: str = "read"):
        # Read-only tools can be retried freely. Write tools are not blindly
        # retried by the orchestrator; exactly-once semantics are handled with
        # idempotency at the tool boundary.
        if side_effect_profile == "write":
            return fn(**args)

        attempt = 0
        last = None
        while attempt <= max_retries:
            last = fn(**args)
            if last.ok:
                return last
            attempt += 1
        return last

    def _render_reply(self, name: str, args: Dict[str, Any], result) -> str:
        if name == "send_interview_invite" and result.ok:
            if result.data.get("idempotent_replay"):
                return (f"Already sent — the interview invite to {args.get('candidate_id')} "
                        f"for {args.get('job_id')} was not sent again.")
            return (f"Done — sent an interview invite to {args.get('candidate_id')} "
                    f"for {args.get('job_id')}.")
        if name == "search_candidates" and result.ok:
            names = ", ".join(c["name"] for c in result.data.get("candidates", []))
            return f"Top matches: {names}."
        if not result.ok:
            return f"That action failed: {result.error}."
        return "Done."

    def _confirmation_prompt(self, invite: Dict[str, Any], mismatch: bool = False) -> str:
        prefix = "That did not match the pending invite. " if mismatch else ""
        return (f"{prefix}Please confirm you want me to send an interview invite to "
                f"{invite.get('candidate_id')} for {invite.get('job_id')}. Reply with "
                f"'confirm invite {invite.get('candidate_id')} to {invite.get('job_id')}' to send it.")

    def _idempotency_key(self, conversation_id: str, candidate_id: str | None, job_id: str | None) -> str:
        return f"invite:{conversation_id}:{candidate_id}:{job_id}"

    def _invite_key(self, candidate_id: str | None, job_id: str | None) -> str:
        return f"{candidate_id}:{job_id}"

    def _candidate_id(self, text: str) -> str | None:
        m = _CAND_RE.search(text or "")
        return m.group(0).upper() if m else None

    def _job_id(self, text: str) -> str | None:
        m = _REQ_RE.search(text or "")
        return m.group(0).upper() if m else None

    def _normalize_candidate_id(self, value: Any) -> str | None:
        if not value:
            return None
        text = str(value).strip().upper()
        return text if _CAND_RE.fullmatch(text) else None

    def _normalize_job_id(self, value: Any) -> str | None:
        if not value:
            return None
        text = str(value).strip().upper()
        return text if _REQ_RE.fullmatch(text) else None

    def _is_confirmation_message(self, message: str) -> bool:
        text = (message or "").lower()
        return any(token in text for token in ["confirm", "approved", "approve"])

    def _is_cancel_message(self, message: str) -> bool:
        text = (message or "").lower().strip()
        return text in {"no", "nope", "cancel", "don't send", "do not send"} or "cancel" in text

    def _has_invite_request(self, message: str) -> bool:
        text = (message or "").lower()
        return any(phrase in text for phrase in [
            "invite",
            "interview invite",
            "reach out",
            "send an invite",
            "send invite",
            "let's get",
            "lets get",
            "get "
        ])
