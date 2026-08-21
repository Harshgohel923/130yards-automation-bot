"""
Dispatcher dedup and registry pruning.

The dedup half matters most: it is the only thing standing between a fixture
and two workers posting its cards twice, because nothing downstream survives
the end of a run to catch a duplicate.
"""

from datetime import datetime, timedelta, timezone

import pytest

import dispatcher


class FakeResponse:
    def __init__(self, runs):
        self._runs = runs

    def raise_for_status(self):
        pass

    def json(self):
        return {'workflow_runs': self._runs}


@pytest.fixture
def fake_runs(monkeypatch):
    """Serve a canned run list per Actions run status."""
    def install(by_status):
        seen = []

        def fake_get(url, params=None, headers=None, timeout=None):
            status = (params or {}).get('status')
            seen.append(status)
            return FakeResponse(by_status.get(status, []))

        monkeypatch.setattr(dispatcher.requests, 'get', fake_get)
        return seen
    return install


def run(match_id, label='Home vs Away'):
    return {'name': f'match-{match_id} · {label}'}


class TestActiveMatchIds:
    def test_finds_in_progress_workers(self, fake_runs):
        fake_runs({'in_progress': [run('111')]})
        assert dispatcher.active_match_ids() == {'111'}

    def test_finds_queued_workers(self, fake_runs):
        # Regression: a run this dispatcher just triggered sits queued until a
        # runner picks it up. Counting only in_progress left the next tick
        # blind to it and it dispatched a second worker for the same match.
        fake_runs({'queued': [run('222')]})
        assert dispatcher.active_match_ids() == {'222'}

    def test_queries_both_statuses(self, fake_runs):
        seen = fake_runs({})
        dispatcher.active_match_ids()
        assert set(seen) == {'in_progress', 'queued'}

    def test_merges_across_statuses_without_duplicates(self, fake_runs):
        fake_runs({'in_progress': [run('111')],
                   'queued': [run('222'), run('111')]})
        assert dispatcher.active_match_ids() == {'111', '222'}

    def test_ignores_runs_that_are_not_match_workers(self, fake_runs):
        fake_runs({'in_progress': [{'name': 'Match Dispatcher'}, run('333')]})
        assert dispatcher.active_match_ids() == {'333'}

    def test_only_the_first_token_of_the_run_name_carries_meaning(self, fake_runs):
        fake_runs({'in_progress': [run('444', 'Rayo Vallecano vs Deportivo')]})
        assert dispatcher.active_match_ids() == {'444'}

    def test_a_run_with_no_name_is_skipped(self, fake_runs):
        fake_runs({'in_progress': [{}, run('555')]})
        assert dispatcher.active_match_ids() == {'555'}


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def fixture(match_id='900', hours_ago=10, group=None):
    kickoff = NOW - timedelta(hours=hours_ago)
    entry = {'match_id': match_id,
             'kickoff_utc': kickoff.isoformat().replace('+00:00', 'Z'),
             'home_team': 'Home', 'away_team': 'Away'}
    if group:
        entry['carousel_group'] = group
    return entry


class TestPruneReason:
    def test_long_finished_fixture_is_prunable(self):
        assert _reason(fixture(hours_ago=10)) is not None

    def test_fixture_inside_the_safety_window_is_kept(self):
        # PRUNE_AFTER_HOURS is the point past every deadline the bot has.
        assert _reason(fixture(hours_ago=2)) is None

    def test_running_worker_protects_a_fixture(self):
        assert _reason(fixture('900', hours_ago=10), running={'900'}) is None

    def test_unposted_carousel_protects_its_members(self):
        # publish_group reads membership from matches.json, so pruning a member
        # early would shrink the post.
        entry = fixture(hours_ago=10, group='saturday')
        assert _reason(entry, pending={'saturday'}) is None
        assert _reason(entry, pending=set()) is not None

    def test_unreadable_kickoff_is_left_for_a_human(self):
        assert _reason({'match_id': '1', 'kickoff_utc': 'not a date'}) is None
        assert _reason({'match_id': '1'}) is None


def _reason(entry, running=frozenset(), pending=frozenset()):
    return dispatcher._prune_reason(entry, NOW, set(running), set(pending))


class TestPruneWindow:
    def test_open_during_the_daily_housekeeping_hour(self):
        # 10:00 Europe/Berlin is 08:00 UTC in August (CEST).
        berlin_ten = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
        assert dispatcher._prune_window_open([], berlin_ten) is True

    def test_closed_outside_it_when_nothing_is_overdue(self):
        assert dispatcher._prune_window_open([(fixture(hours_ago=10), 'x')], NOW) is False

    def test_a_missed_window_triggers_a_catch_up(self):
        # PRUNE_CATCHUP_HOURS past the deadline: the daily pass was clearly
        # missed, so don't let the registry grow until tomorrow.
        stale = fixture(hours_ago=dispatcher.PRUNE_AFTER_HOURS
                        + dispatcher.PRUNE_CATCHUP_HOURS + 1)
        assert dispatcher._prune_window_open([(stale, 'x')], NOW) is True


class TestTelegramWake:
    """Starting the bot for a message that arrived while it was down.

    The bot only exists while an Actions job holds it, so a message sent to a
    stopped bot goes nowhere until something starts one — and the bot cannot
    start itself, because it isn't running to hear the request. This is what
    makes /card usable when no match is playing.
    """

    @pytest.fixture
    def telegram(self, monkeypatch):
        """Capture the getUpdates call and serve a canned result."""
        calls = []

        def install(result):
            class Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {'ok': True, 'result': result}

            def fake_get(url, params=None, timeout=None, **kw):
                calls.append((url, params))
                return Resp()

            monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
            monkeypatch.setattr(dispatcher.requests, 'get', fake_get)
            return calls
        return install

    def test_a_waiting_message_is_seen(self, telegram):
        telegram([{'update_id': 1, 'message': {'text': '/card'}}])
        assert dispatcher.telegram_message_waiting() is True

    def test_an_empty_queue_is_not(self, telegram):
        telegram([])
        assert dispatcher.telegram_message_waiting() is False

    def test_the_peek_sends_no_offset(self, telegram):
        """Telegram only discards updates when getUpdates is called with an
        offset above their id. Sending one here — a negative one especially,
        which forgets everything before the update it returns — would take the
        message out of the bot's mouth before it ever started."""
        calls = telegram([])
        dispatcher.telegram_message_waiting()
        [(url, params)] = calls
        assert url.endswith('/getUpdates')
        assert 'offset' not in params

    def test_a_telegram_outage_does_not_wake_anything(self, monkeypatch):
        """A failed check must read as 'no message'. The alternative is
        starting a runner every fifteen minutes for as long as Telegram is
        unreachable."""
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')

        def boom(*a, **kw):
            raise dispatcher.requests.RequestException('down')

        monkeypatch.setattr(dispatcher.requests, 'get', boom)
        assert dispatcher.telegram_message_waiting() is False

    def test_no_token_means_no_check(self, monkeypatch):
        monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
        monkeypatch.setattr(dispatcher.requests, 'get', lambda *a, **kw: pytest.fail(
            'should not have called Telegram without a token'))
        assert dispatcher.telegram_message_waiting() is False

    def test_the_queue_is_not_touched_while_the_bot_is_running(self, monkeypatch):
        """Two pollers on one token fight over the same queue, and the live bot
        would start losing updates to this one."""
        monkeypatch.setattr(dispatcher, 'telegram_bot_running', lambda: True)
        monkeypatch.setattr(dispatcher, 'telegram_message_waiting', lambda: pytest.fail(
            'peeked at the queue while the bot was polling it'))
        monkeypatch.setattr(dispatcher, 'ensure_telegram_bot', lambda *a: pytest.fail(
            'started a second bot'))
        dispatcher.wake_telegram_bot_if_messaged()

    def test_the_bot_is_started_for_a_waiting_message(self, monkeypatch):
        started = []
        monkeypatch.setattr(dispatcher, 'telegram_bot_running', lambda: False)
        monkeypatch.setattr(dispatcher, 'telegram_message_waiting', lambda: True)
        monkeypatch.setattr(dispatcher, 'ensure_telegram_bot',
                            lambda reason='': started.append(reason))
        dispatcher.wake_telegram_bot_if_messaged()
        assert started

    def test_nothing_waiting_leaves_the_bot_down(self, monkeypatch):
        monkeypatch.setattr(dispatcher, 'telegram_bot_running', lambda: False)
        monkeypatch.setattr(dispatcher, 'telegram_message_waiting', lambda: False)
        monkeypatch.setattr(dispatcher, 'ensure_telegram_bot', lambda *a: pytest.fail(
            'started the bot with nothing to do'))
        dispatcher.wake_telegram_bot_if_messaged()
