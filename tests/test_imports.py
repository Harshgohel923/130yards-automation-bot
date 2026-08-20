"""
Every module imports cleanly.

Production has no staging step: code reaches a live match by being pushed to
master, and a worker only runs when a fixture kicks off. Without this, a typo
or a bad import sits on master until a match fails to post — which is the one
moment nobody wants to be debugging.

Deliberately shallow. It proves the module parses and its imports resolve,
nothing more.
"""

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Modules that do real work in their body rather than behind a __main__ guard:
# importing them would run it. match_worker_runner is the live example — it
# reads argv and starts a match. CI compile-checks these instead.
SKIP = {'conftest', 'setup_token', 'find_chat_id', 'pick_coords',
        'match_worker_runner'}

MODULES = sorted(
    p.stem for p in ROOT.glob('*.py')
    if p.stem not in SKIP and not p.stem.startswith('test_')
)


def test_the_module_list_is_not_accidentally_empty():
    # A bad glob would make every test below vacuously pass.
    assert len(MODULES) > 10


@pytest.mark.parametrize('name', MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_dispatcher_imports():
    # Lives under .github/scripts; conftest puts it on the path.
    importlib.import_module('dispatcher')
