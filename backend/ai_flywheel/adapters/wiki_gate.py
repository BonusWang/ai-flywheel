"""The Wiki gate, re-exported.

The implementation moved into the `ai_flywheel.adapters.wiki` package (schema, validator,
router, T5 adapter, postflight, gate). This module stays as the historical import
surface: `from ai_flywheel.adapters.wiki_gate import WikiGate, split_frontmatter` keeps
working exactly as before.
"""
from __future__ import annotations

from ai_flywheel.adapters.wiki import *  # noqa: F401,F403
from ai_flywheel.adapters.wiki import __all__  # noqa: F401
