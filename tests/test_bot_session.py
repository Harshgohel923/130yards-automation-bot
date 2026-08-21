"""
The bot stays up while someone is talking to it.

The watchdog's original rule — live while a match worker is — is right for the
photo flow, whose whole purpose is a live fixture. It is wrong for /card, which
builds a match the scraper never saw and is therefore used precisely when
nothing is running. Without the session file, the watchdog would shut the bot
down in the middle of someone typing in a scoreline.

Two processes on one runner, no import between them, agreeing by filename —
which is exactly the kind of contract that breaks silently, so it is asserted
here rather than assumed.
"""

import os
import time

import pytest

import bot_watchdog
import telegram_bot


def test_both_sides_use_the_same_file():
    assert telegram_bot.SESSION_FILE == bot_watchdog.SESSION_FILE


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """Run in a scratch directory — the session path is relative to cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_file_means_nobody_is_talking(in_tmp):
    assert bot_watchdog.session_active() is False


def test_the_bot_writes_a_file_the_watchdog_reads(in_tmp):
    telegram_bot._touch_session()
    assert bot_watchdog.session_active() is True


def test_a_stale_session_has_expired(in_tmp):
    telegram_bot._touch_session()
    stale = time.time() - bot_watchdog.SESSION_IDLE - 1
    os.utime(bot_watchdog.SESSION_FILE, (stale, stale))
    assert bot_watchdog.session_active() is False


def test_a_session_just_inside_the_window_is_still_live(in_tmp):
    telegram_bot._touch_session()
    recent = time.time() - bot_watchdog.SESSION_IDLE + 60
    os.utime(bot_watchdog.SESSION_FILE, (recent, recent))
    assert bot_watchdog.session_active() is True


def test_touching_is_best_effort(in_tmp, monkeypatch):
    """A bot that cannot write a file in its own directory must still take
    photos. The only cost of a failed write is the usual shutdown time."""
    def boom(*a, **kw):
        raise OSError('read-only file system')

    monkeypatch.setattr('builtins.open', boom)
    telegram_bot._touch_session()   # must not raise


def test_the_window_is_long_enough_to_type_a_match_in():
    """Eight fields and a photo to find. A short window would end the
    conversation between two of them."""
    assert bot_watchdog.SESSION_IDLE >= 15 * 60
