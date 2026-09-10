#!/usr/bin/env python3
"""Shared constants for skill-creator evaluation artifacts."""

from __future__ import annotations

import re


EVAL_VERSION = 1
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ASSERTION_KINDS = frozenset({"outcome", "process", "safety", "trigger"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
