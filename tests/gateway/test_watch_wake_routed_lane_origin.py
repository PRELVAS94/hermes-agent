"""A wake rebuilt from a durable origin is admitted on a lane whose route postdates that origin.

``snapshot_automation`` compares the source rebuilt from ``SessionEntry.origin`` (written once at
route creation, never refreshed) with the route's last committed envelope (a source INGRESS stamped,
e.g. ``profile='default'`` for a lane whose ``gateway.profile_routes`` entry was added later). The two
disagree on ``profile`` alone whenever the route config postdates the origin, so every
process/watch/completion notice on that lane is refused ``admission_conflict`` forever. The
comparison resolves the profile the route names NOW, on both sides, and never overrides a stamp the
durable data already carries — so chat/thread/user/scope drift (and a lane re-homed to another
profile) is still refused byte-for-byte.
"""
import logging

import pytest

from gateway.session_automation import _route_identity
from gateway.session_identity import replace_source


def _routed(runner, source, adapter_profile=None):
    from gateway.session_automation import _routed_route_identity
    return _routed_route_identity(runner, source, adapter_profile)


_SOURCE = dict(chat_id='42', chat_type='dm', user_id='42', user_name='human')
ENVELOPE_PROFILE = 'default'  # what ingress stamps for a lane routed to the primary profile


class _Source:
    """Minimal stand-in for ``SessionSource``: the only surface ``_route_identity`` touches."""

    def __init__(self, **fields):
        self._fields = fields

    def to_dict(self):
        return dict(self._fields)


class _Router:
    def __init__(self, *, profile=None, raises=None, only_for_bot=None):
        self.profile, self.raises, self.only_for_bot = profile, raises, only_for_bot
        self.calls = 0

    def _profile_name_for_source(self, source, adapter_profile=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.only_for_bot is not None and adapter_profile != self.only_for_bot:
            return None  # a route scoped to one bot does not match another's traffic
        return self.profile


def test_missing_profile_is_filled_from_the_route_on_both_sides():
    """The live shape: the origin predates its lane route, the last envelope carries the stamp."""
    wake = _Source(platform='telegram', chat_id='593572279', chat_type='dm', thread_id='287584',
                   user_id='593572279', user_name='Pedro', chat_name='Pedro', chat_topic='Shopping')
    envelope = _Source(**{**wake.to_dict(), 'profile': ENVELOPE_PROFILE, 'message_id': '1476'})
    router = _Router(profile=ENVELOPE_PROFILE)

    assert _route_identity(wake) != _route_identity(envelope)  # the defect, unchanged comparison
    assert _routed(router, wake) == _routed(router, envelope)


def test_a_present_profile_is_never_overridden():
    """A stamp the durable data already carries wins over route resolution (no widening)."""
    router = _Router(profile='default')
    pinned = _Source(platform='telegram', chat_id='42', chat_type='dm', profile='stocky')

    assert _routed(router, pinned)['profile'] == 'stocky'
    assert router.calls == 0


def test_genuine_profile_drift_still_differs():
    """Two different stamps on the same lane are a real conflict, not a stale-origin artefact."""
    router = _Router(profile='default')
    a = _Source(platform='telegram', chat_id='42', chat_type='dm', profile='stocky')
    b = _Source(platform='telegram', chat_id='42', chat_type='dm', profile='default')

    assert _routed(router, a) != _routed(router, b)


def test_a_route_scoped_to_one_bot_resolves_with_that_bots_profile():
    """A shared gateway's ``bot_profile`` route must resolve here the way it did at ingress."""
    source = _Source(platform='telegram', chat_id='42', chat_type='dm')
    router = _Router(profile='satellite', only_for_bot='beta')

    # Ingress passes the receiving bot's profile; without it the route does not match at all.
    assert _routed(router, source) == _route_identity(source)
    assert _routed(router, source, 'beta')['profile'] == 'satellite'


def test_an_unresolvable_route_leaves_the_identity_untouched():
    """No route / a rejected route must not paper over a real conflict."""
    from gateway.profile_routing import ProfileRouteRejected

    source = _Source(platform='telegram', chat_id='42', chat_type='dm')

    assert _routed(_Router(profile=None), source) == _route_identity(source)
    assert _routed(_Router(raises=ProfileRouteRejected('lane')), source) == _route_identity(source)
    assert _routed(object(), source) == _route_identity(source)


def test_chat_thread_and_user_drift_still_fails():
    """The fields the comparison exists for are compared byte-for-byte."""
    router = _Router(profile='default')
    base = _Source(platform='telegram', chat_id='42', chat_type='dm', thread_id='7', user_id='42')
    for field, value in (('thread_id', '8'), ('chat_id', '43'), ('user_id', '7'), ('chat_type', 'group')):
        drifted = _Source(**{**base.to_dict(), field: value})
        assert _routed(router, drifted) != _routed(router, base), field


# --- the same defect, end to end through the live admission path -------------------------------

async def _authority(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setattr('gateway.session_bot._watch_reply', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    authority = await initialize_session_authority(
        runner, profile_id='default', instance_id='wake-route-fixture', db=db)
    return runner, authority


async def _lane(tmp_path, monkeypatch):
    """A served lane whose durable origin predates the profile stamp its route now carries."""
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.profile_routing import parse_profile_routes
    from gateway.session_identity import replace_source
    from gateway.session_ingress_context import native_callback, register_transport_home
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner, authority = await _authority(tmp_path, monkeypatch)
    runner._primary_profile_name = 'default'
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner.config.multiplex_profiles = True
    runner.config.profile_routes = parse_profile_routes(
        [{'name': 'lane', 'platform': 'discord', 'profile': ENVELOPE_PROFILE, 'chat_id': '42'}])
    runner.config._runtime_profile_homes = [(ENVELOPE_PROFILE, tmp_path)]
    source = replace_source(adapter.build_source(**_SOURCE), profile=ENVELOPE_PROFILE)
    human = MessageEvent(text='human', source=source, message_type=MessageType.TEXT, message_id='human-1')
    with native_callback(runner, human, tmp_path):
        await authority.admit_native(human)
    route = runner.session_store._generate_session_key(source)
    entry = runner.session_store._entries[route]
    entry.transport_profile = None  # written before the transport-profile column existed
    entry.origin = replace_source(source, profile=None, message_id=None)  # never refreshed
    return runner, authority, adapter, route


def _watch_evt(runner, route, *, event_id='e' * 32):
    return {'type': 'watch_match', 'session_id': 'proc_watch1', 'session_key': route,
            'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'event_id': event_id,
            'started_at': 1.0, 'parent_session_id': runner.session_store.peek_session_id(route),
            'command': 'tail -f x', 'output': 'match'}


@pytest.mark.asyncio
async def test_watch_wake_is_admitted_when_its_lane_route_postdates_the_origin(tmp_path, monkeypatch):
    """The live defect end to end: the wake commits instead of being refused ``admission_conflict``."""
    from hermes_state_runtime import list_session_admissions

    runner, authority, _adapter, route = await _lane(tmp_path, monkeypatch)
    try:
        assert await runner._inject_watch_notification('[watch] match', _watch_evt(runner, route)) is True
        sid = runner.session_store.peek_session_id(route)
        rows = [r for r in list_session_admissions(authority.db, session_id=sid, pending_only=False)
                if (r['payload'].get('native_text_v1') or {}).get('automation')]
        assert len(rows) == 1, rows
        assert rows[0]['principal_id'] == 'automation:' + route
        assert rows[0]['payload']['native_text_v1']['automation']['identity'] == (
            '["watch_match","proc_watch1","' + 'e' * 32 + '"]')
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_wake_rebuilt_for_another_user_is_still_refused(tmp_path, monkeypatch, caplog):
    """The check this comparison exists for survives the fix: real source drift is not admitted."""
    runner, authority, _adapter, route = await _lane(tmp_path, monkeypatch)
    try:
        entry = runner.session_store._entries[route]
        entry.origin = replace_source(entry.origin, user_name='renamed', user_id='7')
        with caplog.at_level(logging.WARNING, logger='gateway.run'):
            refused = await runner._inject_watch_notification('[watch] match', _watch_evt(runner, route))
        assert refused is False
        assert [r.getMessage() for r in caplog.records if 'not admitted' in r.getMessage()] == [
            'Watch notification not admitted for discord chat=42: admission_conflict']
    finally:
        authority.db.close()
