"""Test isolation shim for the modal span unit tests.

``kb_pipeline/__init__.py`` eagerly imports blockify/community/search, which pull
heavy external deps (markdown_it, bs4, lxml, networkx, community, psycopg). Those
are NOT needed to exercise ``modal.py`` (stdlib-only). In CI / dev shells where
those deps are absent, pytest's package collection of ``kb_pipeline/__init__.py``
would crash on import before our tests run.

We install minimal stand-in modules for the missing deps into ``sys.modules`` so
that importing the package succeeds. The unit under test (``modal.py``) is loaded
directly from its file path inside the test module and uses NONE of these stubs;
the stubs exist solely to let pytest collect cleanly. No live LLM / minio / OCR /
Java / db is touched.
"""

from __future__ import annotations

import sys
import types


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def _stub_if_missing() -> None:
    # markdown_it + markdown_it.token (blockify.py)
    try:
        import markdown_it  # noqa: F401
    except Exception:  # noqa: BLE001
        mi = _ensure_module("markdown_it")
        mi.MarkdownIt = type("MarkdownIt", (), {})
        tok = _ensure_module("markdown_it.token")
        tok.Token = type("Token", (), {})
        mi.token = tok

    # bs4 (blockify.py)
    try:
        import bs4  # noqa: F401
    except Exception:  # noqa: BLE001
        b = _ensure_module("bs4")
        b.BeautifulSoup = type("BeautifulSoup", (), {})

    # lxml (transitive parser backend)
    try:
        import lxml  # noqa: F401
    except Exception:  # noqa: BLE001
        _ensure_module("lxml")

    # networkx (community.py)
    try:
        import networkx  # noqa: F401
    except Exception:  # noqa: BLE001
        _ensure_module("networkx")

    # python-louvain exposes top-level module `community` (community.py)
    try:
        import community  # noqa: F401
    except Exception:  # noqa: BLE001
        c = _ensure_module("community")
        c.best_partition = lambda *a, **k: {}

    # psycopg (community.py / search.py)
    try:
        import psycopg  # noqa: F401
    except Exception:  # noqa: BLE001
        _ensure_module("psycopg")


_stub_if_missing()
