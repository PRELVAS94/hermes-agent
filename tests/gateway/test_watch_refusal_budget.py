"""The drain's retry budget: a refusal that cannot clear is dropped, a transient one is not.

Upstream #140899 bounded the *cadence* of a refused watch event (2 s tick -> jittered backoff -> a
60 s cap) and dropped only a refusal proven permanent, so a route/config-static code — the repeated
``admission_conflict`` of a lane whose route identity can never match — requeued once a minute
forever: 1 528 log pairs in one boot on a live gateway, with the notice never delivered. A code
nothing inside the drain can clear now drops at ``_WATCH_REFUSAL_REPEAT_LIMIT`` with one warning
naming it; the permanent cap (8) and every self-clearing code are unchanged.
"""
import logging
import queue

from gateway import run_notifications as rn


def _evt(**fields):
    return {'type': 'watch_match', 'platform': 'telegram', 'chat_id': '593572279',
            'session_id': 'proc_watch1', **fields}


def test_drain_drops_a_repeating_attributed_refusal(caplog):
    """A refusal that keeps repeating the same code is a config defect: drop it, name it once."""
    evt = _evt(_watch_refusal_code='admission_conflict')
    q = queue.Queue()
    with caplog.at_level(logging.WARNING, logger='gateway.run'):
        for _ in range(rn._WATCH_REFUSAL_REPEAT_LIMIT + 3):
            rn.GatewayNotificationsMixin._requeue_refused_watch_event(q, evt)
            if q.empty():
                break
            assert q.get_nowait() is evt  # the next drain tick takes it back out

    assert q.empty(), 'the refused event kept requeueing'
    dropped = [r.getMessage() for r in caplog.records if 'dropping the queued event' in r.getMessage()]
    assert len(dropped) == 1, dropped
    assert 'admission_conflict' in dropped[0]
    assert str(rn._WATCH_REFUSAL_REPEAT_LIMIT) in dropped[0]


def test_drain_never_drops_a_transient_refusal():
    """``runtime_draining`` clears on its own (a restart's after-turn wait): that event survives."""
    evt = _evt(_watch_refusal_code='runtime_draining')
    q = queue.Queue()
    for _ in range(rn._WATCH_REFUSAL_REPEAT_LIMIT + 5):
        rn.GatewayNotificationsMixin._requeue_refused_watch_event(q, evt)
        assert not q.empty(), 'a draining refusal dropped the event'
        q.get_nowait()

    assert evt['_watch_next_retry_at'] > 0


def test_drain_keeps_the_upstream_cap_for_a_permanent_refusal():
    """A refusal proven permanent still drops at the upstream attempt cap (8), not later."""
    evt = _evt()
    q = queue.Queue()
    for _ in range(rn._MAX_COMPLETION_INJECTION_ATTEMPTS - 1):
        evt['_refusal_permanent'] = True
        rn.GatewayNotificationsMixin._requeue_refused_watch_event(q, evt)
        assert not q.empty()
        q.get_nowait()

    evt['_refusal_permanent'] = True
    rn.GatewayNotificationsMixin._requeue_refused_watch_event(q, evt)
    assert q.empty(), 'a permanent refusal outlived the upstream cap'
