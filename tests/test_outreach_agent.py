"""Behavioral tests for the Candidate Outreach Copilot.

Several of these tests FAIL against the original orchestrator because it
executes side-effecting invites without confirmation and double-writes on
retry. They encode the behavior expected after remediation:

  1. A browse/compare turn must NOT create an outreach_events row.
  2. An invite must require an explicit confirmation bound to candidate+job.
  3. A confirmed invite executes exactly once, even with retries enabled.

Run with:  pytest -q
"""

import pytest

from src import tools
from src.orchestrator import OutreachOrchestrator


@pytest.fixture(autouse=True)
def _clean_store():
    tools.reset_store()
    yield
    tools.reset_store()


def test_browse_request_does_not_send_invite():
    orch = OutreachOrchestrator()
    orch.handle_message("conv-7781", "show me strong Python candidates for REQ-184")
    # Browsing must never write an outreach event.
    assert tools.outreach_events == [], (
        "A browse-intent turn created an outreach_events row"
    )


def test_comparison_question_does_not_send_invite():
    orch = OutreachOrchestrator()
    orch.handle_message("conv-8042", "compare top backend candidates for REQ-211")
    orch.handle_message("conv-8042", "who has more Kafka experience between them?")
    assert tools.outreach_events == [], (
        "A comparison question created an outreach_events row"
    )


def test_invite_without_confirmation_is_blocked_and_asks():
    orch = OutreachOrchestrator()
    resp = orch.handle_message("conv-9001", "go ahead and invite C-3310 to REQ-211")
    # No write should happen until the recruiter confirms the exact candidate+job.
    assert tools.outreach_events == [], "Invite was sent without confirmation"
    # The assistant should ask for confirmation.
    assert "confirm" in resp["assistant"].lower(), (
        "Agent did not ask the recruiter to confirm the invite"
    )


def test_confirmed_invite_sends_exactly_once():
    orch = OutreachOrchestrator()
    orch.handle_message("conv-9001", "go ahead and invite C-3310 to REQ-211")
    # Recruiter confirms; remediation should accept a bound confirmation.
    orch.handle_message("conv-9001", "yes, confirm invite C-3310 to REQ-211")
    rows = [e for e in tools.outreach_events
            if e["candidate_id"] == "C-3310" and e["job_id"] == "REQ-211"]
    assert len(rows) == 1, f"Expected exactly one invite row, got {len(rows)}"


def test_confirmed_invite_is_idempotent_under_retry():
    orch = OutreachOrchestrator()
    orch.handle_message("conv-9001", "go ahead and invite C-3310 to REQ-211")
    # max_retries=2 simulates the orchestrator retrying after a transient timeout.
    orch.handle_message("conv-9001", "yes, confirm invite C-3310 to REQ-211",
                        max_retries=2)
    rows = [e for e in tools.outreach_events
            if e["candidate_id"] == "C-3310" and e["job_id"] == "REQ-211"]
    assert len(rows) == 1, (
        f"Retry double-sent the invite: expected 1 row, got {len(rows)}"
    )
