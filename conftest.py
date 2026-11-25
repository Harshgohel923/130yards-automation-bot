"""
Pytest setup shared by every test module.

Several modules build an API client at import time — caption.py constructs a
Gemini client in its module body, and dispatcher.py reads GH_TOKEN out of the
environment before defining anything. That means the environment has to be
populated *before* the first import, which is exactly what a root conftest is
for: pytest loads it ahead of the test modules.

The values below are placeholders and nothing in the suite reaches the network.
setdefault, not assignment, so a real environment is left alone.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# The bot's modules live at the repo root; the dispatcher sits under
# .github/scripts and is not importable by package path.
for path in (ROOT, ROOT / '.github' / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault('GEMINI_API_KEY', 'test-key-not-real')
os.environ.setdefault('GH_TOKEN', 'test-token-not-real')
os.environ.setdefault('GITHUB_REPOSITORY', 'test-owner/test-repo')


import pytest


@pytest.fixture(autouse=True)
def _no_photo_lookups_from_the_worker(monkeypatch):
    """Worker-loop tests reach the "is there a photo?" check late in each half
    (story_photo, from 35'/80'). It is a network call, so it answers "no photo"
    unless a test sets it — tests/test_story_photo.py does."""
    import main
    monkeypatch.setattr(main, 'match_photo_exists', lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _in_memory_leaderboard(monkeypatch):
    """Uploads are counted for the weekly recap (leaderboard.py), which keeps
    its numbers on Cloudinary. Tests get an in-memory store instead."""
    import leaderboard
    store = {'stats': {}}
    monkeypatch.setattr(leaderboard, 'load', lambda: json.loads(json.dumps(store['stats'])))
    monkeypatch.setattr(leaderboard, 'save', lambda s: store.__setitem__('stats', s))
    return store
