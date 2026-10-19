"""A capacity-only hold must never be reported as a stuck dispatcher.

Measured live on 2026-10-18: ``gateway.log`` carried 45 ``kanban dispatcher stuck:``
warnings in one day, the latest three with no ``Last tick held back:`` clause at all.
The board held 10 ready cards with exactly one running worker per dev profile, both
heartbeating, and ``max_in_progress_per_profile: 1`` — normal steady state, zero
spawns by design. The warning still advised "Check profile health (venv, PATH,
credentials)", which sent the first investigation of the day after a fault that did
not exist.

Cause: :func:`describe_suppression` did not count
``DispatchResult.skipped_per_profile_capped``, so a capacity-only tick rendered the
empty string (and with it no reason clause), and both health counters treated
"nothing spawned" as "stuck".

Pinned here, one test per site:
1. ``describe_suppression`` names the capped bucket per assignee.
2. ``capacity_only_hold`` / ``record_stuck_tick`` — the shared decision — separate a
   busy profile from a genuine hold.
3. the gateway's embedded watcher loop does not warn across a capacity-only window
   but still warns (naming reason + deferred profiles) on a genuine hold.
4. the standalone ``--force`` daemon's twin counter behaves identically.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_dispatch as kbd


def _capped(task_id: str, assignee: str, current: int = 1) -> tuple[str, str, int]:
    return (task_id, assignee, current)


def _capacity_only_result() -> kbd.DispatchResult:
    """What one live tick looked like: every ready card waiting on a busy profile."""
    res = kbd.DispatchResult()
    res.skipped_per_profile_capped.extend(
        [_capped("t_cody_1", "cody"), _capped("t_devy_1", "devy"), _capped("t_devy_2", "devy")]
    )
    return res


def _genuine_result() -> kbd.DispatchResult:
    """A real hold that happens to sit next to a busy profile."""
    res = kbd.DispatchResult()
    res.skipped_per_profile_capped.append(_capped("t_devy_1", "devy"))
    res.respawn_guarded.append(("t_cody_1", "blocker_auth"))
    return res


# ── Site 1: describe_suppression ─────────────────────────────────────────────


def test_describe_suppression_counts_the_capped_bucket_per_assignee():
    assert kbd.describe_suppression([_capacity_only_result()]) == "capped=cody:1,devy:2"


def test_describe_suppression_keeps_capped_alongside_every_other_reason():
    res = kbd.DispatchResult()
    res.skipped_per_profile_capped.extend([_capped("t1", "devy")])
    res.respawn_guarded.append(("t2", "active_pr"))
    res.spawn_failed.append("t3")
    res.skipped_locked = True
    res.memory_pressure = "critical"

    assert kbd.describe_suppression([res]) == (
        "active_pr=1, skipped_locked=1, spawn_failed=1, capped=devy:1, memory_pressure=critical"
    )


def test_describe_suppression_of_an_idle_tick_stays_empty():
    """No holds at all still renders nothing: the clause is only appended when held."""
    assert kbd.describe_suppression([kbd.DispatchResult()]) == ""


# ── Site 2: the shared counter decision ─────────────────────────────────────


def test_capacity_only_hold_is_true_only_when_capacity_is_the_whole_story():
    assert kbd.capacity_only_hold([_capacity_only_result()]) is True
    # Nothing held at all is not a capacity hold — there is no capacity deferral.
    assert kbd.capacity_only_hold([kbd.DispatchResult()]) is False
    # One genuine hold in the same tick disables the suppression entirely.
    assert kbd.capacity_only_hold([_genuine_result()]) is False
    # A per-board sweep: one board busy, another genuinely held.
    assert kbd.capacity_only_hold([_capacity_only_result(), None, _genuine_result()]) is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.respawn_guarded.append(("t9", "recent_success")), id="respawn_guarded"),
        pytest.param(lambda r: r.rate_limited.append("t9"), id="rate_limited"),
        pytest.param(lambda r: setattr(r, "skipped_locked", True), id="skipped_locked"),
        pytest.param(lambda r: setattr(r, "memory_pressure", "critical"), id="memory_pressure"),
        pytest.param(lambda r: r.auto_blocked.append("t9"), id="auto_blocked"),
        pytest.param(lambda r: r.spawn_failed.append("t9"), id="spawn_failed"),
        pytest.param(lambda r: r.skipped_unassigned.append("t9"), id="skipped_unassigned"),
    ],
)
def test_every_genuine_hold_keeps_the_stuck_streak_alive(mutate):
    res = _capacity_only_result()
    mutate(res)

    assert kbd.capacity_only_hold([res]) is False
    assert kbd.record_stuck_tick(2, [res], ready_pending=True) == 3


def test_record_stuck_tick_resets_on_capacity_progress_and_idle():
    capacity = _capacity_only_result()
    # The live bug: six consecutive capacity-only ticks must stay at 0, not reach the window.
    streak = 0
    for _ in range(8):
        streak = kbd.record_stuck_tick(streak, [capacity], ready_pending=True)
    assert streak == 0
    # Progress on the board and an idle board both reset a live streak.
    assert kbd.record_stuck_tick(4, [capacity], ready_pending=True, any_spawned=True) == 0
    assert kbd.record_stuck_tick(4, [capacity], ready_pending=False) == 0


# ── Site 3: the gateway's embedded watcher loop ─────────────────────────────


class _FakeDispatcher:
    """One board, one synthesized tick result per call."""

    def __init__(self, results: list[kbd.DispatchResult]) -> None:
        self._results = results
        self.ticks = 0

    def auto_decompose_tick(self, per_tick: int) -> int:
        return 0

    def tick_once(self):
        res = self._results[min(self.ticks, len(self._results) - 1)]
        self.ticks += 1
        return [("default", res)]

    def ready_nonempty(self) -> bool:
        return True


def _drive_watcher(monkeypatch, dispatcher, ticks, on_sleep=None):
    """Run the real watcher loop for *ticks* iterations against *dispatcher*."""
    from gateway import kanban_watchers as kw
    from gateway import kanban_watchers_dispatcher as kwd

    class _Runner(kw.GatewayKanbanWatchersMixin):
        def __init__(self) -> None:
            self._running = True
            self._kanban_dispatcher_lock_handle = None
            self.slept = 0

        def _kanban_dispatcher_boot(self):
            from hermes_cli import kanban_db as kb
            return (lambda: {}, kb, {"dispatch_in_gateway": True})

        def _release_kanban_dispatcher_lock(self) -> None:
            return None

        async def _sleep_between_ticks(self, interval: float) -> None:
            self.slept += 1
            if on_sleep is not None:
                on_sleep(self.slept)
            if self.slept >= ticks:
                self._running = False

    async def _direct(func, *args):
        return func(*args)

    async def _no_delay(_seconds):
        return None

    monkeypatch.setattr(
        kw, "_resolve_dispatcher_settings",
        lambda cfg, kb: kwd._DispatcherSettings(1.0, None, None, 2, 0, True, None, 1),
    )
    monkeypatch.setattr(kw, "_KanbanDispatcher", lambda kb, settings: dispatcher)
    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load_config: (False, 1))
    # Module-local asyncio stub: only the 5s boot delay needs neutralizing.
    monkeypatch.setattr(
        kw, "asyncio", SimpleNamespace(sleep=_no_delay, CancelledError=asyncio.CancelledError)
    )
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda *a, **k: [])

    runner = _Runner()
    asyncio.run(runner._kanban_dispatcher_watcher())
    return dispatcher, runner


def _drive_gateway_watcher(monkeypatch, results, ticks):
    """The loop against synthesized tick results (counter/wording wiring only)."""
    return _drive_watcher(monkeypatch, _FakeDispatcher(results), ticks)



def _stuck_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "kanban dispatcher stuck" in r.getMessage()]


def test_gateway_watcher_is_silent_across_a_capacity_only_window(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.run")

    dispatcher, runner = _drive_gateway_watcher(monkeypatch, [_capacity_only_result()], ticks=8)

    assert dispatcher.ticks == 8, "the window must actually be exercised"
    assert runner.slept == 8
    assert _stuck_warnings(caplog) == []


def test_gateway_watcher_still_warns_on_a_genuine_hold_and_names_it(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.run")

    dispatcher, _runner = _drive_gateway_watcher(monkeypatch, [_genuine_result()], ticks=6)

    assert dispatcher.ticks == 6
    warnings = _stuck_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "6 consecutive ticks" in warnings[0]
    assert "Last tick held back: blocker_auth=1, capped=devy:1." in warnings[0]


def test_gateway_watcher_warns_on_critical_memory_pressure(monkeypatch, caplog):
    """Memory pressure is not capacity: the reason must still reach the operator."""
    caplog.set_level(logging.WARNING, logger="gateway.run")
    res = kbd.DispatchResult()
    res.memory_pressure = "critical"

    _drive_gateway_watcher(monkeypatch, [res], ticks=6)

    warnings = _stuck_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "Last tick held back: memory_pressure=critical." in warnings[0]


# ── End to end: the real dispatcher tick, real board, real cap ──────────────


class _RealTickDispatcher:
    """The real ``dispatch_once`` on a real board, with a spawn hook instead of workers.

    Proves the tick itself produces the capacity bucket the counter now reads —
    a synthesized ``DispatchResult`` would only prove the counter, not that the
    live tick ever fills that bucket.
    """

    def __init__(self, spawn_fn) -> None:
        self._spawn_fn = spawn_fn
        self.results: list[kbd.DispatchResult] = []

    def auto_decompose_tick(self, per_tick: int) -> int:
        return 0

    def tick_once(self):
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            res = kbd.dispatch_once(
                conn, spawn_fn=self._spawn_fn, max_in_progress_per_profile=1,
            )
        self.results.append(res)
        return [("default", res)]

    def ready_nonempty(self) -> bool:
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            return kbd.has_spawnable_ready(conn)


def _live_board(monkeypatch, tmp_path, per_profile: int = 3):
    """A temp home with two busy profiles' worth of cards, the cap from the live config."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import profiles

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _scrub_kanban_env(monkeypatch)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()
    with kbc.connect_closing() as conn:
        for name in ("alice", "bob"):
            for i in range(per_profile):
                kb.create_task(conn, title=f"{name} card {i}", assignee=name)
    return kb


def test_live_dispatcher_tick_defers_at_the_per_profile_cap(monkeypatch, tmp_path):
    """The live tick really does fill ``skipped_per_profile_capped`` — the root cause."""
    from hermes_cli import kanban_db_connect as kbc

    _live_board(monkeypatch, tmp_path)

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_in_progress_per_profile=1)

    assert len(res.spawned) == 2, res.spawned
    assert {who for _tid, who, _ws in res.spawned} == {"alice", "bob"}
    assert sorted(who for _tid, who, _cur in res.skipped_per_profile_capped) == [
        "alice", "alice", "bob", "bob"
    ]
    assert kbd.describe_suppression([res]) == "capped=alice:2,bob:2"
    # The tick spawned both workers AND deferred four cards: capacity was the only
    # hold recorded, so the counter resets on the spawn, not on the deferrals.
    assert kbd.capacity_only_hold([res]) is True
    assert kbd.record_stuck_tick(3, [res], ready_pending=True, any_spawned=True) == 0


def test_live_capacity_only_ticks_never_warn(monkeypatch, caplog, tmp_path):
    """Both profiles at cap=1 with live workers: 9 real ticks, no warning."""
    _live_board(monkeypatch, tmp_path)
    dispatcher = _RealTickDispatcher(lambda *a, **k: os.getpid())
    caplog.set_level(logging.WARNING, logger="gateway.run")

    _drive_watcher(monkeypatch, dispatcher, ticks=10)

    # Tick 1 spawned both profiles; ticks 2-10 were held back by the cap alone.
    assert dispatcher.results[0].spawned, "the first tick must have spawned"
    capped_ticks = [r for r in dispatcher.results[1:] if r.skipped_per_profile_capped]
    assert len(capped_ticks) == 9, [len(r.spawned) for r in dispatcher.results]
    assert all(r.spawned == [] for r in capped_ticks)
    assert all(kbd.capacity_only_hold([r]) for r in capped_ticks)
    assert _stuck_warnings(caplog) == []


def test_live_mixed_tick_still_warns_and_names_reason_and_profiles(monkeypatch, caplog, tmp_path):
    """A real non-capacity hold beside a real capacity hold: still warns, naming both."""
    _live_board(monkeypatch, tmp_path)
    dispatcher = _RealTickDispatcher(lambda *a, **k: os.getpid())
    pressure = {"level": "unknown"}
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: pressure["level"])
    caplog.set_level(logging.WARNING, logger="gateway.run")

    # After the first tick both profiles hold their one worker; from then on the
    # host reports elevated pressure, which restricts the tick for the *host's*
    # sake — that is not the same thing as a busy profile.
    _drive_watcher(
        monkeypatch, dispatcher, ticks=7,
        on_sleep=lambda n: pressure.__setitem__("level", "elevated") if n >= 1 else None,
    )

    assert dispatcher.results[0].spawned, "the first tick must have spawned"
    held = [r for r in dispatcher.results[1:] if r.memory_pressure]
    assert len(held) == 6, [r.memory_pressure for r in dispatcher.results]
    warnings = _stuck_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "6 consecutive ticks" in warnings[0], warnings[0]
    assert "Last tick held back: capped=alice:2,bob:2, memory_pressure=elevated." in warnings[0]


# ── Site 4: the standalone `--force` daemon's twin counter ──────────────────


def _scrub_kanban_env(monkeypatch) -> None:
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)


def _run_daemon_ticks(monkeypatch, tmp_path, results):
    """Drive `hermes kanban daemon --force` for len(results) ticks; return its stderr."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_ops as ops

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _scrub_kanban_env(monkeypatch)
    kb.init_db()
    # The daemon's health counter only fires when a spawnable ready card exists.
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="waiting on a busy profile", assignee="default")
        row = conn.execute("SELECT status, assignee FROM tasks").fetchone()
        assert row["status"] == "ready" and row["assignee"] == "default"

    ticks = []

    def _fake_run_daemon(*, interval, max_spawn, failure_limit, stop_event=None, on_tick=None):
        for res in results:
            ticks.append(res)
            on_tick(res)

    monkeypatch.setattr(ops.kbd, "run_daemon", _fake_run_daemon)

    args = SimpleNamespace(force=True, interval=5, max=None, failure_limit=2,
                           pidfile=None, verbose=False)
    assert ops._cmd_daemon(args) == 0
    return ticks


def test_standalone_daemon_is_silent_across_a_capacity_only_window(monkeypatch, capsys, tmp_path):
    ticks = _run_daemon_ticks(
        monkeypatch, tmp_path, [_capacity_only_result() for _ in range(8)]
    )

    assert len(ticks) == 8
    err = capsys.readouterr().err
    assert "dispatcher stuck" not in err


def test_standalone_daemon_still_warns_on_a_genuine_hold_and_names_it(monkeypatch, capsys, tmp_path):
    _run_daemon_ticks(monkeypatch, tmp_path, [_genuine_result() for _ in range(6)])

    err = capsys.readouterr().err
    assert "dispatcher stuck" in err
    assert "6 consecutive ticks" in err
    assert "Last tick held back: blocker_auth=1, capped=devy:1." in err
