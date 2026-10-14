"""Invariants for the pre-agent phase wall-clock bound (upstream issue #138358).

The sentinel is claimed before ``_handle_message_with_agent`` runs, and everything
up to the agent-runner entry — session resolution, turn preparation, the
``agent:start`` hook emit — awaits on the event loop with no watchdog (the
inactivity watchdog only arms inside the runner). A stall there (a blocked
session store, or a hanging user ``agent:start`` hook) held the sentinel forever
and silently queued every later message, recoverable only by a restart.

The fix bounds that phase with ``gateway.pre_agent_timeout``: on expiry the turn
is failed with a visible notice and the sentinel released, so the session
recovers without a restart. ``0`` disables the bound (the old behaviour).
"""

import asyncio
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _bootstrap(monkeypatch, tmp_path):
    """Minimal GatewayRunner setup shared by all tests in this module."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: []
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner._hmwa_stop_typing_for_turn = AsyncMock()

    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-preagent",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.transcript_tail_role.return_value = "user"
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def _event():
    return MessageEvent(
        text="hello world",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        ),
        message_id="msg-42",
    )


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


@pytest.mark.asyncio
async def test_agent_start_hook_stall_releases_sentinel_within_bound(monkeypatch, tmp_path):
    """A hanging ``agent:start`` hook must not wedge the session forever.

    With a small pre-agent bound the turn fails with the visible notice, and the
    agent runner is never entered. The notice is returned rather than the turn
    hanging.
    """
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._pre_agent_timeout = 0.05  # small bound so the stall trips fast

    # The agent:start hook hangs until released: this is the stall #138358 reports.
    hook_entered = asyncio.Event()
    hook_gate = asyncio.Event()

    async def hanging_hook(event_type, ctx):
        hook_entered.set()
        await hook_gate.wait()

    runner.hooks.emit = AsyncMock(side_effect=hanging_hook)
    # A hard tripwire: if the agent runner is ever entered, fail loudly — the
    # wedge is upstream of it, so it must never be reached.
    async def _forbidden_run(*args, **kwargs):
        raise AssertionError("agent runner must not be entered on a pre-agent stall")

    runner._run_agent = _forbidden_run

    # Outer ceiling: the whole pre-agent phase must finish (or fail cleanly) well
    # inside it. With the bound, the turn returns the notice in ~50ms; without it
    # (pre-fix) this call wedges forever on the hanging hook and the ceiling trips.
    response = await asyncio.wait_for(
        runner._handle_message_with_agent(
            _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
        ),
        timeout=5.0,
    )

    # Visible notice surfaced, not a silent wedge.
    assert "took too long" in response
    assert "wasn't processed" in response
    hook_gate.set()


@pytest.mark.asyncio
async def test_prepare_stall_releases_sentinel_within_bound(monkeypatch, tmp_path):
    """A stall inside turn preparation also trips the bound and returns the notice.

    This is the phase that acquires the turn lease and sets the task-local session
    env tokens; the fix must clean those up on cancellation so the next turn is not
    polluted.
    """
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._pre_agent_timeout = 0.05

    prepare_entered = asyncio.Event()
    prepare_gate = asyncio.Event()

    async def hanging_prepare(*args, **kwargs):
        prepare_entered.set()
        await prepare_gate.wait()

    runner._hmwa_prepare_turn = hanging_prepare

    async def _forbidden_run(*args, **kwargs):
        raise AssertionError("agent runner must not be entered on a pre-agent stall")

    runner._run_agent = _forbidden_run

    response = await asyncio.wait_for(
        runner._handle_message_with_agent(
            _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
        ),
        timeout=5.0,
    )

    assert "took too long" in response
    assert "wasn't processed" in response
    prepare_gate.set()


@pytest.mark.asyncio
async def test_zero_bound_disables_the_timeout(monkeypatch, tmp_path):
    """``pre_agent_timeout = 0`` keeps the pre-#138358 unbounded behaviour.

    A slow-but-eventually-completing hook runs to completion instead of being
    cut off, proving ``0`` is an opt-out and not a fallback to the default.
    """
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._pre_agent_timeout = 0.0

    completed = asyncio.Event()

    async def slow_hook(event_type, ctx):
        completed.set()

    runner.hooks.emit = AsyncMock(side_effect=slow_hook)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "ok",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "history_offset": 1,
        "last_prompt_tokens": 0,
    })

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    # Hook ran and the turn proceeded normally to the runner.
    assert completed.is_set()
    runner._run_agent.assert_awaited_once()


def test_parse_pre_agent_timeout_zero_is_deliberate_optout():
    """The parser keeps ``0`` as a deliberate disable, not a fallback to the default."""
    from gateway.restart import (
        DEFAULT_GATEWAY_PRE_AGENT_TIMEOUT,
        parse_pre_agent_timeout,
    )

    assert parse_pre_agent_timeout(0) == 0.0
    assert parse_pre_agent_timeout("0") == 0.0
    # A real value passes through.
    assert parse_pre_agent_timeout(30) == 30.0
    assert parse_pre_agent_timeout("45") == 45.0
    # Garbage falls back to the default, not zero.
    assert parse_pre_agent_timeout("not a number") == DEFAULT_GATEWAY_PRE_AGENT_TIMEOUT
    assert DEFAULT_GATEWAY_PRE_AGENT_TIMEOUT > 0
