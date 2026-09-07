"""T3 0.0.38 lifecycle rules, independent of Herdeck's coarse status enum.

Mirrors client-runtime/state/threadSettled.ts; timestamps use UTC and snooze
expiration is evaluated on every poll, even when the server emitted no event.
"""
from datetime import UTC, datetime


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def queued_start(thread, now):
    session = thread.get('session') or {}
    if session.get('status') == 'error':
        return False
    sent = timestamp(thread.get('latestUserMessageAt'))
    if sent is None or abs(now - sent) > 120:
        return False
    turn = thread.get('latestTurn') or {}
    return all(timestamp(turn.get(k)) is None or timestamp(turn[k]) < sent
               for k in ('requestedAt', 'startedAt', 'completedAt'))


def lifecycle(thread, pending, now=None):
    now = datetime.now(UTC).timestamp() if now is None else now
    if thread.get('deletedAt'):
        return 'deleted'
    if thread.get('archivedAt'):
        return 'archived'
    if pending or thread.get('hasPendingApprovals') or thread.get('hasPendingUserInput'):
        return 'active'
    if thread.get('settledOverride') == 'settled':
        return 'settled'
    until = timestamp(thread.get('snoozedUntil'))
    if until is None or until <= now or queued_start(thread, now):
        return 'active'
    session, turn = thread.get('session') or {}, thread.get('latestTurn') or {}
    snoozed = timestamp(thread.get('snoozedAt'))
    error_at = timestamp(session.get('updatedAt'))
    completed = timestamp(turn.get('completedAt'))
    if session.get('status') == 'error' and (snoozed is None or error_at is not None and error_at > snoozed):
        return 'active'
    if turn.get('state') == 'completed' and snoozed is not None and completed is not None and completed > snoozed:
        return 'active'
    return 'snoozed'
