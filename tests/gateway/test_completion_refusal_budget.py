"""The async-delegation completion retry is bounded where the refusal is not a wait.

Live defect (thread 287584, ``deleg_34f266be``): one durable completion was refused
``admission_conflict`` every 60 s for 18 h. The drain-path bound (``_WATCH_REFUSAL_REPEAT_LIMIT``)
was unreachable from there because the completion path refunds every unadmitted delivery
(``defer_completion_delivery``), so the row never spent an attempt and never converged to
``dropped``. These tests pin the bound on the path that actually looped, the transient-code
exemption on that same path, and the attributed-code policy for both paths.
"""

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from tools import async_delegation as ad


class _RefusingAuthority:
    """Stands in for the session authority's admission verdict, nothing else.

    The real ``_inject_watch_notification`` refusal mapping (``RuntimeStoreError`` reason ->
    ``WakeNotAccepted.reason`` -> attributed code), the real pre-flight, the real durable ledger and
    the real retry bookkeeping all stay in the test: only the authority's verdict is substituted.
    """

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def logical_owner(session_id: str) -> str:
        """The route's logical root: this fixture has no compression lineage."""
        return session_id


def _runner(tmp_path, refusal_code: str, monkeypatch):
    """A bare runner whose completion targeting "refusal-budget" is refused with *refusal_code*."""
    from gateway import session_automation
    from hermes_state_runtime import RuntimeStoreError

    # Pin the home explicitly: the ledger and the runner's own reads must all resolve to tmp_path,
    # whatever HERMES_HOME the harness happens to leak into this process.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        entry = runner.session_store.get_or_create_session(SessionSource(
            platform=Platform.TELEGRAM, chat_id="refusal-budget", chat_type="dm",
        ))
        store = runner.session_store
        db = cast(Any, store._db)
        # A live owner: the refusal under test is the admission verdict, never a gone-session proof.
        assert db.get_session(entry.session_id) is not None

        async def accept(event):
            event._gateway_accepted = True

        runner.adapters[Platform.TELEGRAM] = cast(Any, SimpleNamespace(handle_message=accept))
        runner.session_authorities = None
        runner.session_authority = _RefusingAuthority(db)

        async def refuse_admission(*_args, **_kwargs):
            raise RuntimeStoreError(refusal_code)

        monkeypatch.setattr(session_automation, "admit_process_notice", refuse_admission)
        event = {
            "type": "async_delegation", "delegation_id": "refusal-budget",
            "session_key": entry.session_key, "parent_session_id": entry.session_id,
            "dispatched_at": 1.0, "status": "completed", "summary": "completed result",
        }
        ad._persist_dispatch(event)
        ad._persist_completion(event, {"status": "completed", "summary": event["summary"]})
        return runner, entry, event


def _row(delegation_id: str) -> dict:
    row = ad.get_durable_delegation(delegation_id)
    assert row is not None
    return row


def _drop_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "dropping the completion and its durable row" in r.getMessage()]


def _close(runner) -> None:
    runner.session_store.close_all_db_handles()
    runner.close_all_session_db_handles()


@pytest.mark.asyncio
async def test_admission_conflict_refused_completion_stops_and_drops_its_row(
    tmp_path, monkeypatch, private_db_probe_cleanup, caplog,
):
    """The 18 h loop, bounded: the budget ends it, one warning names the code, the row is dropped."""
    from gateway import run_notifications as rn

    with _profile_runtime_scope(tmp_path):
        runner, _entry, event = _runner(tmp_path, "admission_conflict", monkeypatch)
        limit = rn._WATCH_REFUSAL_REPEAT_LIMIT
        try:
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                results = [await runner._deliver_completion_notification("completed result", event)
                           for _ in range(limit)]
                dropped = _row(event["delegation_id"])
                # Beyond the budget the event is not offered again: nothing to requeue, no new warning.
                beyond = await runner._deliver_completion_notification("completed result", event)

            assert results == [False] * (limit - 1) + [None], 'the refusal never stopped'
            assert beyond is None, 'a dropped completion was re-offered'
            # The row is dropped, never left pending; the budget-crossing attempt is spent rather
            # than refunded (a drop is terminal, so the count no longer paces anything).
            assert (dropped["delivery_state"], dropped["delivery_attempts"]) == ("dropped", 1), dropped
            warnings = _drop_warnings(caplog)
            assert len(warnings) == 1, warnings
            assert "admission_conflict" in warnings[0]
            assert f"{limit} times" in warnings[0]
        finally:
            _close(runner)


@pytest.mark.asyncio
async def test_runtime_draining_refusal_retries_indefinitely_on_the_completion_path(
    tmp_path, monkeypatch, private_db_probe_cleanup, caplog,
):
    """``runtime_draining`` clears on its own (restart wait / parked profile): never dropped."""
    from gateway import run_notifications as rn

    with _profile_runtime_scope(tmp_path):
        runner, _entry, event = _runner(tmp_path, "runtime_draining", monkeypatch)
        try:
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                results = [await runner._deliver_completion_notification("completed result", event)
                           for _ in range(rn._WATCH_REFUSAL_REPEAT_LIMIT + 2)]
            pending = _row(event["delegation_id"])
            assert set(results) == {False}, 'a self-clearing refusal stopped retrying'
            assert pending["delivery_state"] == "pending"
            assert pending["delivery_attempts"] == 0, 'a retryable refusal spent the delivery budget'
            assert _drop_warnings(caplog) == []
        finally:
            _close(runner)


@pytest.mark.parametrize("code", ["queue_full", "storage_unavailable", "stale_generation"])
@pytest.mark.asyncio
async def test_self_clearing_attributed_codes_are_never_dropped_on_either_path(
    tmp_path, monkeypatch, private_db_probe_cleanup, caplog, code,
):
    """The completion retry keeps a recoverable refusal, and the drain keeps the same event."""
    from gateway import run_notifications as rn

    with _profile_runtime_scope(tmp_path):
        runner, _entry, event = _runner(tmp_path, code, monkeypatch)
        try:
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                results = [await runner._deliver_completion_notification("completed result", event)
                           for _ in range(rn._WATCH_REFUSAL_REPEAT_LIMIT + 1)]
            pending = _row(event["delegation_id"])
            assert set(results) == {False}, f'{code} stopped retrying on the completion path'
            assert pending["delivery_state"] == "pending"
            assert _drop_warnings(caplog) == []

            watch_evt = {"type": "watch_match", "platform": "telegram", "chat_id": "593572279",
                         "session_id": "proc_watch1", "_watch_refusal_code": code}
            q = __import__("queue").Queue()
            for _ in range(rn._WATCH_REFUSAL_REPEAT_LIMIT + 2):
                rn.GatewayNotificationsMixin._requeue_refused_watch_event(q, watch_evt)
                assert not q.empty(), f'the drain dropped a recoverable refusal ({code})'
                q.get_nowait()
        finally:
            _close(runner)
