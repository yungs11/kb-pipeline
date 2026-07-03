"""도메인 플러그인 등록 (SoT §22)."""

from __future__ import annotations

from typing import List

from .base import PLUGIN_MATCH_THRESHOLD, ParserPlugin

__all__ = ["ParserPlugin", "PLUGIN_MATCH_THRESHOLD", "default_plugins"]


def default_plugins() -> List[ParserPlugin]:
    from .delegation_rule import DelegationRulePlugin

    return [DelegationRulePlugin()]
