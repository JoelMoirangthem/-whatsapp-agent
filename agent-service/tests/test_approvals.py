"""Approval workflow tests: create → decide → execute/expire transitions."""

import time

import pytest

from app.config import load_settings_for_test
from app.db import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "agent.db"))


class TestPendingActions:
    def test_create_and_get(self, store):
        action = store.create_action(
            user_id="tester",
            tool="send_message",
            args={"recipient": "15550001", "message": "hi"},
            reason="user asked",
            warnings=[],
            ttl_seconds=300,
        )
        fetched = store.get_action(action.id)
        assert fetched.status == "pending"
        assert fetched.args["recipient"] == "15550001"
        assert fetched.expires_at > time.time()

    def test_approve_then_complete(self, store):
        action = store.create_action("u", "send_message", {}, "", [], 300)
        decided = store.decide_action(action.id, approved=True)
        assert decided.status == "approved"
        done = store.complete_action(action.id, success=True, result={"sent": True})
        assert done.status == "executed"
        assert done.result == {"sent": True}

    def test_reject(self, store):
        action = store.create_action("u", "delete_message", {}, "", [], 300)
        decided = store.decide_action(action.id, approved=False)
        assert decided.status == "rejected"
        # cannot decide twice
        assert store.decide_action(action.id, approved=True) is None

    def test_double_decision_conflict(self, store):
        action = store.create_action("u", "send_message", {}, "", [], 300)
        store.decide_action(action.id, approved=True)
        assert store.decide_action(action.id, approved=False) is None
        assert store.get_action(action.id).status == "approved"

    def test_expiry_prevents_approval(self, store):
        action = store.create_action("u", "send_message", {}, "", [], ttl_seconds=0.05)
        time.sleep(0.1)
        decided = store.decide_action(action.id, approved=True)
        assert decided is None
        assert store.get_action(action.id).status == "expired"

    def test_expire_stale_counts(self, store):
        store.create_action("u", "send_message", {}, "", [], ttl_seconds=0.01)
        time.sleep(0.05)
        assert store.expire_stale() == 1
        assert store.expire_stale() == 0

    def test_list_by_status(self, store):
        a1 = store.create_action("u", "send_message", {}, "", [], 300)
        a2 = store.create_action("u", "delete_message", {}, "", [], 300)
        store.decide_action(a1.id, approved=False)
        pending = store.list_actions(status="pending")
        rejected = store.list_actions(status="rejected")
        assert [a.id for a in pending] == [a2.id]
        assert [a.id for a in rejected] == [a1.id]

    def test_audit_trail_records(self, store):
        store.audit("action_created", "u", tool="send_message")
        events = store.get_audit()
        assert events[0]["event"] == "action_created"
        assert events[0]["detail"]["tool"] == "send_message"

    def test_settings_ttl_used(self):
        settings = load_settings_for_test(pending_action_ttl_seconds=123)
        assert settings.pending_action_ttl_seconds == 123
