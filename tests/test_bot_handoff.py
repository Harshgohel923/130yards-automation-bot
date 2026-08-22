"""
Handing the Telegram token from one Actions job to the next.

A hosted-runner job cannot outlive six hours. A full Saturday of fixtures does
— 2026-08-22 was one unbroken 10.6 hours of worker activity — so the bot
holding the token is guaranteed to be cut mid-matchday. It used to be cut by
the platform, marking the run failed, and the chat then went quiet until a
later 15-minute tick noticed nobody was there.

Now the watchdog stops just under the cap and the dispatcher queues a successor
a little before that, which the workflow's concurrency group keeps pending
until the incumbent lets go.

What is pinned here is the ordering of the three numbers — they live in three
different files and mean nothing apart — and that the dispatcher relieves a bot
exactly once.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import bot_watchdog
import dispatcher


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'telegram_bot.yml'

DISPATCHER_TICK = 15 * 60      # the cron in dispatcher.yml


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
        def fake_get(url, params=None, headers=None, timeout=None):
            return FakeResponse(by_status.get((params or {}).get('status'), []))
        monkeypatch.setattr(dispatcher.requests, 'get', fake_get)
    return install


def run(minutes_old, key='run_started_at'):
    began = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    return {'id': 1, key: began.isoformat().replace('+00:00', 'Z')}


# ── The three numbers have to stay in this order ─────────────────────────────

class TestTheBudget:
    def job_timeout(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        return int(re.search(r'timeout-minutes:\s*(\d+)', text).group(1)) * 60

    def test_the_watchdog_stops_before_the_job_is_killed(self):
        """Otherwise the run is marked failed by the platform — the red tick."""
        assert bot_watchdog.MAX_RUNTIME < self.job_timeout()

    def test_the_successor_is_queued_before_the_watchdog_stops(self):
        assert dispatcher.BOT_HANDOFF_AFTER_SECS < bot_watchdog.MAX_RUNTIME

    def test_a_tick_cannot_step_over_the_handoff_window(self):
        """
        The dispatcher only looks every 15 minutes. If the window between
        queueing the successor and the incumbent stopping were narrower than
        that, a tick could land either side of it and no successor would ever
        be queued — silently back to the old behaviour.
        """
        window = bot_watchdog.MAX_RUNTIME - dispatcher.BOT_HANDOFF_AFTER_SECS
        assert window >= 2 * DISPATCHER_TICK

    def test_the_successor_has_time_to_reach_pending(self):
        """It is queued while the incumbent still has minutes left to run."""
        assert dispatcher.BOT_HANDOFF_AFTER_SECS < self.job_timeout() - DISPATCHER_TICK


# ── When a bot is due to be relieved ─────────────────────────────────────────

class TestNeedsHandoff:
    def test_a_fresh_bot_is_left_alone(self):
        assert dispatcher._bot_needs_handoff(run(10)) is False

    def test_a_bot_near_the_end_of_its_job_time_is_relieved(self):
        assert dispatcher._bot_needs_handoff(run(330)) is True

    def test_the_boundary_is_the_configured_one(self):
        cutoff = dispatcher.BOT_HANDOFF_AFTER_SECS / 60
        assert dispatcher._bot_needs_handoff(run(cutoff - 1)) is False
        assert dispatcher._bot_needs_handoff(run(cutoff + 1)) is True

    def test_it_falls_back_to_created_at(self):
        assert dispatcher._bot_needs_handoff(run(330, key='created_at')) is True

    @pytest.mark.parametrize('bad', [{}, {'run_started_at': None},
                                     {'run_started_at': 'not a date'}])
    def test_an_unreadable_start_leaves_the_bot_working(self, bad):
        """The job timeout still ends it; disturbing a working bot on a parsing
        failure would be the worse trade."""
        assert dispatcher._bot_needs_handoff(bad) is False


# ── What the dispatcher does about it ────────────────────────────────────────

class TestEnsureTelegramBot:
    @pytest.fixture
    def bot(self, monkeypatch, fake_runs):
        """Install a run listing and record any workflow_dispatch that follows."""
        fired = []

        class Posted:
            status_code = 204
            text = ''

        def install(in_progress=(), queued=()):
            fake_runs({'in_progress': list(in_progress), 'queued': list(queued)})
            monkeypatch.setattr(
                dispatcher.requests, 'post',
                lambda *a, **k: fired.append(k.get('json')) or Posted())
            return fired
        return install

    def test_nothing_running_starts_a_bot(self, bot):
        fired = bot()
        dispatcher.ensure_telegram_bot()
        assert len(fired) == 1

    def test_a_healthy_bot_is_not_disturbed(self, bot):
        fired = bot(in_progress=[run(20)])
        dispatcher.ensure_telegram_bot()
        assert fired == []

    def test_a_bot_out_of_job_time_gets_a_successor(self, bot):
        fired = bot(in_progress=[run(330)])
        dispatcher.ensure_telegram_bot()
        assert len(fired) == 1

    def test_the_successor_is_queued_only_once(self, bot):
        """Ticks keep coming while the incumbent finishes its last 15 minutes."""
        fired = bot(in_progress=[run(330)], queued=[run(0)])
        dispatcher.ensure_telegram_bot()
        assert fired == []

    def test_a_bot_already_queued_is_not_doubled(self, bot):
        fired = bot(queued=[run(0)])
        dispatcher.ensure_telegram_bot()
        assert fired == []

    def test_an_api_failure_does_not_raise(self, bot, monkeypatch):
        """Dispatching match workers must never be blocked by the bot check."""
        bot()
        monkeypatch.setattr(dispatcher.requests, 'get',
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('502')))
        dispatcher.ensure_telegram_bot()      # must not raise


# ── The watchdog's own deadline ──────────────────────────────────────────────

class TestWatchdogDeadline:
    def test_it_never_sleeps_past_the_deadline(self):
        """
        Overshooting hands the job back to the platform timeout, which is the
        red tick the deadline exists to avoid.
        """
        source = (ROOT / '.github' / 'scripts' / 'bot_watchdog.py').read_text()
        assert 'min(CHECK_EVERY, deadline - time.monotonic())' in source

    def test_stopping_at_the_deadline_is_not_an_error(self):
        """It returns rather than sys.exit(1) — the run should end green."""
        source = (ROOT / '.github' / 'scripts' / 'bot_watchdog.py').read_text()
        deadline_block = source.split('if time.monotonic() >= deadline:')[1]
        assert deadline_block.split('\n\n')[0].strip().endswith('return')
