"""Deck-rendering language catalog: en/cs parity + safe fallbacks."""

from herdeck.i18n import LANGUAGES, STRINGS, tr


def test_every_language_has_the_full_key_set():
    # A key added for one language but forgotten for another would silently
    # fall back to English — fail loudly at test time instead.
    reference = set(STRINGS["en"])
    for lang in LANGUAGES:
        assert set(STRINGS[lang]) == reference, f"catalog keys diverge for '{lang}'"


def test_tr_translates_and_formats():
    assert tr("en", "agents_total", n=4) == "4 agents"
    assert tr("cs", "agents_total", n=4) == "agentů: 4"
    assert tr("cs", "sent", label="repo") == "posláno › repo"


def test_tr_falls_back_to_english_for_unknown_language():
    assert tr("de", "stop") == "Stop"
    assert tr("", "needs_you_one") == "▲ needs you"


def test_tr_never_raises_for_unknown_key():
    # A render must not crash on a stale/missing key — the key itself comes back.
    assert tr("en", "no_such_key") == "no_such_key"


def test_status_words_exist_for_all_statuses():
    from herdeck.model import Status

    for lang in LANGUAGES:
        for status in Status:
            text = tr(lang, f"status.{status.value}")
            assert text and not text.startswith("status."), (lang, status)
