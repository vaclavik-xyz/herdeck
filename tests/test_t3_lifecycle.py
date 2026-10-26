"""Lifecycle rules (port of threadSettled.ts) against V2 shell data."""
import pytest

from herdeck.t3_state import lifecycle, queued_start, raised_hand, timestamp

NOW = timestamp("2026-10-14T12:00:00.000Z")


def iso(delta):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(NOW + delta, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def shell(**kw):
    return {"id": "t1", "status": "idle", "updatedAt": iso(-7200), **kw}


@pytest.mark.parametrize("kw,archived,expected", [
    ({"deletedAt": iso(-5)}, False, "deleted"),
    ({"deletedAt": iso(-5), "archivedAt": iso(-9)}, False, "deleted"),
    ({"archivedAt": iso(-5)}, False, "archived"),
    ({}, True, "archived"),
    ({"settledOverride": "settled"}, False, "settled"),
    ({"settledOverride": "settled", "pendingRuntimeRequest": {"id": "r"}}, False, "active"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600)}, False, "snoozed"),
    ({"snoozedUntil": iso(-60), "snoozedAt": iso(-3600)}, False, "active"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "pendingRuntimeRequest": {"id": "r"}}, False, "active"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "status": "failed",
      "updatedAt": iso(-60)}, False, "active"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "status": "completed",
      "latestRunCompletedAt": iso(-60)}, False, "active"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "status": "completed",
      "latestRunCompletedAt": iso(-7200)}, False, "snoozed"),
    ({"snoozedUntil": iso(3600), "snoozedAt": iso(-3600), "status": "queued"}, False, "active"),
])
def test_lifecycle(kw, archived, expected):
    assert lifecycle(shell(**kw), NOW, archived) == expected


def test_failed_snoozed_after_the_failure_stays_snoozed():
    s = shell(status="failed", snoozedUntil=iso(3600), snoozedAt=iso(-60), updatedAt=iso(-3600))
    assert not raised_hand(s)
    assert lifecycle(s, NOW) == "snoozed"


def test_queued_start_window_and_failed_exclusion():
    base = {"latestUserMessageAt": iso(-30), "latestRunCompletedAt": iso(-600)}
    assert queued_start(shell(status="completed", **base), NOW)
    assert not queued_start(shell(status="failed", **base), NOW)
    assert not queued_start(shell(status="completed", latestUserMessageAt=iso(-600)), NOW)
    assert queued_start(shell(status="preparing"), NOW)


def test_timestamp_rejects_garbage_and_naive():
    assert timestamp(None) is None
    assert timestamp("nope") is None
    assert timestamp("2026-10-14T12:00:00") is None
