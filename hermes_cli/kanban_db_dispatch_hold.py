"""Per-tick hold accounting for the kanban dispatcher: what held ready work back.

``hermes_cli.kanban_db_dispatch`` is the facade (``DispatchResult`` plus the
dispatch/spawn/reap phases). The one question the "dispatcher stuck" warnings
ask of a tick is *why* ready work was held back, and whether any of it is
something an operator has to act on — that reading lives here, next to the
shared streak decision both health counters use (the gateway's embedded watcher
and the standalone ``hermes kanban dispatch`` daemon), so the two lanes cannot
drift into disagreeing about what "stuck" means.

Siblings never import the facade at module scope (that would be a cycle), so
``DispatchResult`` is referenced for typing only; the functions read the buckets
off whatever result objects they are handed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult


def describe_suppression(results: Iterable[Optional[DispatchResult]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    spawn_failed=1, capped=cody:1,devy:1, memory_pressure=critical`` — the
    respawn-guard reasons counted per task, plus the tick-level holds, plus the
    per-profile capacity deferrals broken out by assignee. Feeds the
    "dispatcher stuck" warnings of the CLI daemon and the embedded gateway
    dispatcher, which otherwise report a bare zero-spawn count while
    ``hermes kanban tail`` is the only place the guard reason is written
    (#111910).

    ``capped`` is what tells a BUSY profile apart from a broken one: a tick
    whose every ready card was only waiting for an assignee's
    ``kanban.max_in_progress_per_profile`` slot used to render as an empty
    string, so the warning printed "0 workers spawned. Check profile health
    (venv, PATH, credentials)" naming no reason at all — and sent the reader
    after a fault that did not exist.
    """
    counts: dict[str, int] = {}
    capped: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.spawn_failed:
            counts["spawn_failed"] = counts.get("spawn_failed", 0) + len(res.spawn_failed)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        for _task_id, assignee, _current in res.skipped_per_profile_capped:
            key = assignee or "?"
            capped[key] = capped.get(key, 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if capped:
        parts.append("capped=" + ",".join(f"{name}:{n}" for name, n in sorted(capped.items())))
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


# DispatchResult buckets that mean "ready work was held back for a reason other
# than capacity" — the reasons an operator may have to act on. ``auto_blocked``
# and ``spawn_failed`` are the spawn fault, ``skipped_unassigned`` is a card
# waiting for routing, and the rest are the guards/pressure. Anything here is
# evidence the tick was stuck even if some cards were also merely capped.
_GENUINE_HOLD_FIELDS = (
    "respawn_guarded",
    "rate_limited",
    "skipped_locked",
    "memory_pressure",
    "auto_blocked",
    "spawn_failed",
    "skipped_unassigned",
)


def capacity_only_hold(results: Iterable[Optional[DispatchResult]]) -> bool:
    """True when every hold this tick recorded is a per-profile capacity deferral.

    A ready card deferred because its assignee already holds
    ``kanban.max_in_progress_per_profile`` workers is normal steady state: the
    card is picked up on a later tick, and the operator has nothing to fix. A
    tick whose holds are all that one is therefore NOT "stuck" — warning about
    it fires the "Check profile health (venv, PATH, credentials)" advice at a
    healthy fleet that is simply at capacity. (A tick that also spawned a worker
    answers True here; :func:`record_stuck_tick` resets on the spawn itself, so
    the streak never depends on that reading.)

    Deliberately conservative: any non-capacity hold in the same tick (see
    :data:`_GENUINE_HOLD_FIELDS`) or no capacity deferral at all returns False,
    so the genuinely-stuck warning is never suppressed by a busy neighbour. The
    host-level budget early-return is also False: the tick stops before it
    examines a row, so nothing proves the profiles had a free slot.
    """
    capped = 0
    for res in results:
        if res is None:
            continue
        if any(getattr(res, field) for field in _GENUINE_HOLD_FIELDS):
            return False
        capped += len(res.skipped_per_profile_capped)
    return capped > 0


def record_stuck_tick(
    bad_ticks: int,
    results: Iterable[Optional[DispatchResult]],
    *,
    ready_pending: bool,
    any_spawned: bool = False,
) -> int:
    """Advance one tick of the "ready queue non-empty but nothing spawned" streak.

    Shared by BOTH dispatchers — the gateway's embedded watcher
    (``gateway/kanban_watchers.py``) and the standalone ``hermes kanban dispatch
    --force`` daemon (``hermes_cli/kanban_ops.py``) — so the two lanes cannot
    drift into disagreeing about what "stuck" means. Progress, an idle board, or
    a capacity-only hold all reset the streak to 0; only a genuine hold extends
    it. The caller warns once the streak reaches its window.
    """
    if not ready_pending or any_spawned:
        return 0
    if capacity_only_hold(results):
        return 0
    return int(bad_ticks) + 1
