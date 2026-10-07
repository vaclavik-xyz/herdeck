"""Deck themes: how a key and the status window are drawn.

The public, versioned contract for theme authors is :mod:`herdeck.themes.api`
(see docs/themes.md). This package must not import anything at module level:
``herdeck.icons`` imports it lazily, and ``themes.api`` / ``themes.classic``
import ``herdeck.icons``.
"""
