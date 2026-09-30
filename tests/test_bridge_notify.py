"""BridgeNotifier: Telegram alerts decided and sent by the bridge itself.

Fake Telegram transport, fake wall clock and fake timers throughout: no real
token, no real API, no real sleeps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.error

import pytest

from herdeck.app_control import ActionResult
from herdeck.bridge_notify import BridgeNotifier
from herdeck.bridge_settings import BridgeSettingsStore
from herdeck.bridge_telegram import BridgeTelegramStore
from herdeck.events import EventHub
from herdeck.i18n import STRINGS, tr
from herdeck.model import AgentKey
from herdeck.presence_hub import PresenceHub
from herdeck.telegram import TelegramApiError, TelegramBotClient

TOKEN = "123456789:" + "A" * 35
CHAT = "-100123"
KEY = AgentKey("srv", "w1:p1")
USER = 42


# --- fakes ------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Handle:
    def __init__(self, at: float, fn):
        self.at, self.fn, self.cancelled = at, fn, False

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    """``loop_timer(delay, fn)`` on the fake clock; ``advance`` fires due ones."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.handles: list[Handle] = []

    def __call__(self, delay: float, fn) -> Handle:
        handle = Handle(self.clock.now + delay, fn)
        self.handles.append(handle)
        return handle

    def pending(self) -> list[Handle]:
        return [h for h in self.handles if not h.cancelled]

    def advance(self, seconds: float) -> None:
        target = self.clock.now + seconds
        while True:
            due = sorted(
                (h for h in self.pending() if h.at <= target), key=lambda h: h.at
            )
            if not due:
                break
            handle = due[0]
            handle.cancelled = True  # fired
            self.clock.now = max(self.clock.now, handle.at)
            handle.fn()
        self.clock.now = target


class Transport:
    """Fake Bot API transport: records calls, returns scripted results."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list = []  # each item: list of updates or an exception
        self.send_errors: list[Exception] = []
        self.message_id = 100
        self.block_updates: threading.Event | None = None
        self.polling = threading.Event()

    def __call__(self, method: str, fields: dict):
        self.calls.append((method, dict(fields)))
        if method == "sendMessage":
            if self.send_errors:
                raise self.send_errors.pop(0)
            self.message_id += 1
            return {"message_id": self.message_id}
        if method == "getUpdates":
            self.polling.set()
            if self.block_updates is not None:
                self.block_updates.wait(5)
            if self.updates:
                item = self.updates.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return []
        return True

    def sent(self) -> list[dict]:
        return [f for m, f in self.calls if m == "sendMessage"]

    def polls(self) -> list[dict]:
        return [f for m, f in self.calls if m == "getUpdates"]


class FakeControl:
    def __init__(self, notifier_agents=None):
        self.calls: list[tuple] = []
        self.agents = notifier_agents or (lambda: {})

    def current_agent(self, key):
        return self.agents().get(key)

    def reset_confirmation(self, key=None):
        pass

    async def read_prompt(self, key, *, timeout=3.0):
        return "Allow edit?\n1. Yes\n2. No"

    async def approve(self, key, *, timeout=3.0, **kw):
        self.calls.append(("approve", key))
        return ActionResult(True)

    async def deny(self, key, *, timeout=3.0, **kw):
        self.calls.append(("deny", key))
        return ActionResult(True)

    async def stop(self, key, *, timeout=3.0, **kw):
        self.calls.append(("stop", key))
        return ActionResult(True)

    async def send_text(self, key, text, *, timeout=3.0):
        self.calls.append(("send_text", key, text))
        return ActionResult(True)


# --- helpers ----------------------------------------------------------------


def pane(status="working", since=9_000_000, pane_id="w1:p1", terminal="t1", **extra):
    p = {
        "pane_id": pane_id,
        "status": status,
        "agent_type": "claude",
        "label": "api",
        "workspace": "api",
        "status_since_ms": since,
        "terminal_id": terminal,
        **extra,
    }
    EventHub.stamp([p])
    return p


def frame(kind, p, *, episode=None, at_ms=None, **extra):
    return {
        "type": "event",
        "server_id": "srv",
        "epoch": "e1",
        "seq": 1,
        "kind": kind,
        "episode_id": episode or p["episode_id"],
        "pane_id": p["pane_id"],
        "terminal_id": p["terminal_id"],
        "at_ms": p["status_since_ms"] if at_ms is None else at_ms,
        **extra,
    }


TG_ON = {"enabled": True, "chat_id": CHAT}


async def make(
    tmp_path,
    *,
    tg=TG_ON,
    shared=None,
    presence=None,
    token=True,
    transport=None,
    control=None,
    clock=None,
    env=None,
):
    clock = clock or Clock()
    timers = Timers(clock)
    transport = transport or Transport()
    store = BridgeTelegramStore(tmp_path / "tg.toml", tmp_path / "tok", env=env or {})
    if token and not env:
        assert store.set_token(TOKEN) is None
    if tg is not None:
        assert store.put(0, tg, "t").ok
    settings = BridgeSettingsStore(tmp_path / "s.toml")
    if shared is not None:
        assert settings.put(0, shared, "t").ok
    holder = {}
    control = control or FakeControl(lambda: holder["n"].agents())
    n = BridgeNotifier(
        server_id="srv",
        telegram=store,
        settings=settings,
        presence=presence,
        control=control,
        client_factory=lambda tok: TelegramBotClient(tok, request=transport),
        clock=clock,
        loop_timer=timers,
        poll_timeout=0,
    )
    holder["n"] = n
    n.t, n.clock, n.timers, n.control_ = transport, clock, timers, control
    n.tg_store, n.settings_store = store, settings
    return n


async def start(n, *panes):
    """Baseline observation (what was open before the notifier ran is not news)."""
    n.observe_panes([pane("working")] if not panes else list(panes))
    await n.flush()


async def block(n, since=9_100_000, prompt="Allow edit?\n1. Yes\n2. No"):
    p = pane("blocked", since=since)
    n.observe_panes([p])
    extra = {"prompt": prompt, "prompt_revision": "r1"} if prompt is not None else {}
    n.on_event(frame("blocked", p, **extra))
    await n.flush()
    return p


async def finish(n, *, work_since=9_000_000, done_since=9_600_000):
    """working (seen) -> done; returns the done pane."""
    n.observe_panes([pane("working", since=work_since)])
    p = pane("done", since=done_since)
    n.observe_panes([p])
    n.on_event(frame("done", p))
    await n.flush()
    return p


# --- blocked ----------------------------------------------------------------


async def test_blocked_event_sends_one_message(tmp_path):
    n = await make(tmp_path)
    await start(n)
    await block(n)
    sent = n.t.sent()
    assert len(sent) == 1
    msg = sent[0]
    assert msg["chat_id"] == CHAT
    assert msg["text"].startswith(tr("en", "notify.title_blocked", agent="claude"))
    assert "api" in msg["text"] and "Allow edit?" in msg["text"]
    assert msg["disable_notification"] == "false"
    assert "reply_markup" not in msg
    assert n.status()["last_sent_at_ms"] == int(n.clock.now * 1000)


async def test_blocked_goes_to_thread_and_silent_when_sound_off(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "message_thread_id": 7, "sound": False})
    await start(n)
    await block(n)
    (msg,) = n.t.sent()
    assert msg["message_thread_id"] == "7"
    assert msg["disable_notification"] == "true"


async def test_blocked_not_in_shared_on_is_not_sent(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"on": ["done"]}})
    await start(n)
    await block(n)
    assert n.t.sent() == []


async def test_blocked_episode_open_at_start_is_baseline_not_alerted(tmp_path):
    n = await make(tmp_path)
    p = pane("blocked", since=9_100_000)
    n.observe_panes([p])  # first observation: already blocked
    n.on_event(frame("blocked", p, prompt="x", prompt_revision="r1"))
    await n.flush()
    assert n.t.sent() == []


async def test_blocked_stale_episode_not_sent(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = pane("blocked", since=9_100_000)
    n.observe_panes([pane("working", since=9_200_000)])  # already moved on
    n.on_event(frame("blocked", p, prompt="x", prompt_revision="r1"))
    await n.flush()
    assert n.t.sent() == []


async def test_blocked_cooldown_5s(tmp_path):
    n = await make(tmp_path)
    await start(n)
    await block(n, since=9_100_000)
    n.observe_panes([pane("working", since=9_101_000)])
    n.clock.now += 2
    await block(n, since=9_102_000)
    assert len(n.t.sent()) == 1
    n.observe_panes([pane("working", since=9_103_000)])
    n.clock.now += 10
    await block(n, since=9_104_000)
    assert len(n.t.sent()) == 2


async def test_interactive_blocked_has_keyboard_and_reply_routes_to_send_text(tmp_path):
    n = await make(
        tmp_path, tg={**TG_ON, "interactive": True, "allowed_user_ids": [USER]}
    )
    await start(n)
    await block(n)
    (msg,) = n.t.sent()
    markup = json.loads(msg["reply_markup"])
    data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(d.endswith(":approve") for d in data)
    assert any(d.endswith(":deny") for d in data)
    assert any(d.endswith(":stop") for d in data)
    mid = n.t.message_id
    n.t.updates.append(
        [
            {
                "update_id": 500,
                "message": {
                    "message_id": 900,
                    "from": {"id": USER},
                    "chat": {"id": int(CHAT), "type": "supergroup", "title": "Ops"},
                    "text": "go on",
                    "reply_to_message": {"message_id": mid},
                },
            }
        ]
    )
    await n.poll_step()
    await n.flush()
    assert ("send_text", KEY, "go on") in n.control_.calls
    # the offset advanced: the same update is never processed twice
    await n.poll_step()
    assert n.t.polls()[-1]["offset"] == "501"
    assert n.status()["inbound"] == "ok"


async def test_interactive_approve_button_calls_control(tmp_path):
    n = await make(
        tmp_path, tg={**TG_ON, "interactive": True, "allowed_user_ids": [USER]}
    )
    await start(n)
    await block(n)
    (msg,) = n.t.sent()
    markup = json.loads(msg["reply_markup"])
    approve = markup["inline_keyboard"][0][0]["callback_data"]
    n.t.updates.append(
        [
            {
                "update_id": 7,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": USER},
                    "data": approve,
                    "message": {"message_id": n.t.message_id, "chat": {"id": int(CHAT)}},
                },
            }
        ]
    )
    await n.poll_step()
    assert ("approve", KEY) in n.control_.calls


# --- done / quiet done --------------------------------------------------------


async def test_done_after_long_run_sent(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"done_min_work": 5}})
    await start(n)
    await finish(n, work_since=9_000_000, done_since=9_000_000 + 6 * 60_000)
    (msg,) = n.t.sent()
    assert msg["text"].startswith(tr("en", "notify.title_done", agent="claude"))


async def test_done_short_run_deferred_then_sent_when_still_done(tmp_path):
    n = await make(
        tmp_path, shared={"notifications": {"done_min_work": 5, "done_short_delay": 10}}
    )
    await start(n)
    n.clock.now = 9_060.0  # wall seconds == done_since
    await finish(n, work_since=9_000_000, done_since=9_060_000)
    assert n.t.sent() == []
    (timer,) = n.timers.pending()
    assert timer.at == pytest.approx(9_060.0 + 600)
    n.timers.advance(600)
    await n.flush()
    assert len(n.t.sent()) == 1


async def test_done_short_run_left_done_not_sent(tmp_path):
    n = await make(
        tmp_path, shared={"notifications": {"done_min_work": 5, "done_short_delay": 10}}
    )
    await start(n)
    n.clock.now = 9_060.0
    await finish(n, work_since=9_000_000, done_since=9_060_000)
    n.observe_panes([pane("working", since=9_100_000)])
    n.timers.advance(600)
    await n.flush()
    assert n.t.sent() == []


async def test_done_short_run_delay_zero_never_sent(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"done_min_work": 5}})
    await start(n)
    await finish(n, work_since=9_000_000, done_since=9_060_000)
    assert n.timers.pending() == []
    n.timers.advance(3600)
    await n.flush()
    assert n.t.sent() == []


async def test_done_unknown_run_sent(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"done_min_work": 5}})
    await start(n, pane("idle", since=8_000_000))
    p = pane("done", since=9_060_000)  # the working stretch was never seen
    n.observe_panes([p])
    n.on_event(frame("done", p))
    await n.flush()
    assert len(n.t.sent()) == 1


async def test_done_cooldown_60s_per_agent(tmp_path):
    n = await make(tmp_path)
    await start(n)
    await finish(n, work_since=9_000_000, done_since=9_100_000)
    n.clock.now += 30
    await finish(n, work_since=9_200_000, done_since=9_300_000)
    assert len(n.t.sent()) == 1
    n.clock.now += 31
    await finish(n, work_since=9_400_000, done_since=9_500_000)
    assert len(n.t.sent()) == 2


async def test_answered_then_done_within_10s_suppressed(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = await block(n)
    assert len(n.t.sent()) == 1
    n.on_event(frame("answered", p, by="deck"))
    n.clock.now += 5
    await finish(n, work_since=9_200_000, done_since=9_300_000)
    assert len(n.t.sent()) == 1  # the done right after the answer is not news


async def test_done_message_in_czech(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "language": "cs"})
    await start(n)
    await finish(n)
    (msg,) = n.t.sent()
    assert msg["text"].startswith(tr("cs", "notify.title_done", agent="claude"))
    assert tr("cs", "notify.title_done", agent="x") != tr("en", "notify.title_done", agent="x")


# --- reminders ------------------------------------------------------------------


async def test_reminders_x1_x2_x3_then_stop(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"remind_after": 1}})
    await start(n)
    n.clock.now = 9_100.0
    await block(n, since=9_100_000)
    assert len(n.t.sent()) == 1
    for i in range(3):
        n.timers.advance(60)
        await n.flush()
        assert len(n.t.sent()) == 2 + i
        assert n.t.sent()[-1]["text"].startswith(
            tr("en", "notify.title_reminder", agent="claude", minutes=i + 1)
        )
    n.timers.advance(3600)
    await n.flush()
    assert len(n.t.sent()) == 4
    assert n.timers.pending() == []


@pytest.mark.parametrize("kind", ["answered", "unblocked"])
async def test_reminders_cancelled_by_answer_or_unblock(tmp_path, kind):
    n = await make(tmp_path, shared={"notifications": {"remind_after": 1}})
    await start(n)
    n.clock.now = 9_100.0
    p = await block(n, since=9_100_000)
    n.on_event(frame(kind, p))
    await n.flush()
    n.timers.advance(600)
    await n.flush()
    assert len(n.t.sent()) == 1


# --- subagents ----------------------------------------------------------------


async def test_subagents_done_burst_one_message(tmp_path):
    n = await make(tmp_path, shared={"notifications": {"subagents_done": True, "on": []}})
    await start(n)
    subs = [
        {"id": "a", "status": "running", "started_ms": 9_000_100},
        {"id": "b", "status": "running", "started_ms": 9_000_200},
    ]
    n.observe_panes([pane("working", subagents=subs)])
    done = [{**s, "status": "done"} for s in subs]
    n.observe_panes([pane("working", subagents=done)])  # parent still at work
    await n.flush()
    assert n.t.sent() == []
    n.observe_panes([pane("idle", since=9_500_000, subagents=done)])
    n.observe_panes([pane("idle", since=9_500_000, subagents=done)])
    await n.flush()
    (msg,) = n.t.sent()
    assert msg["text"].startswith(
        tr("en", "notify.title_subagents_done", agent="claude", count=2)
    )


# --- presence (only_when_away) ------------------------------------------------------


def presence_hub(idle_s=None, *, reporters=True):
    clock = Clock(1_000.0)
    hub = PresenceHub(clock=clock)
    if reporters:
        hub.report(object(), idle_s)
    return hub


async def test_only_when_away_user_present_not_sent(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "only_when_away": 5}, presence=presence_hub(60))
    await start(n)
    await block(n)
    assert n.t.sent() == []


async def test_only_when_away_user_idle_long_sent(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "only_when_away": 5}, presence=presence_hub(400))
    await start(n)
    await block(n)
    assert len(n.t.sent()) == 1


async def test_only_when_away_no_reporters_means_away_and_sends(tmp_path):
    # Review focus 5: every Mac asleep -> no runtime reports presence -> away.
    n = await make(
        tmp_path, tg={**TG_ON, "only_when_away": 5}, presence=presence_hub(reporters=False)
    )
    await start(n)
    await block(n)
    assert len(n.t.sent()) == 1


async def test_no_presence_hub_at_all_means_away(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "only_when_away": 5}, presence=None)
    await start(n)
    await block(n)
    assert len(n.t.sent()) == 1


# --- inactive -------------------------------------------------------------------


@pytest.mark.parametrize(
    "tg,token",
    [
        ({"enabled": False, "chat_id": CHAT}, True),
        (TG_ON, False),
        ({"enabled": True, "chat_id": ""}, True),
        (None, True),
    ],
)
async def test_inactive_sends_nothing(tmp_path, tg, token):
    n = await make(tmp_path, tg=tg, token=token)
    await start(n)
    await block(n)
    await finish(n)
    assert n.t.sent() == []
    assert n.status()["active"] is False


async def test_status_shape(tmp_path):
    n = await make(tmp_path)
    st = n.status()
    assert st == {
        "token": "file",
        "active": True,
        "inbound": "ok",
        "last_error": "",
        "last_sent_at_ms": 0,
        "recent_chats": [],
    }
    n2 = await make(tmp_path / "x", token=False)
    assert n2.status()["token"] is None and n2.status()["inbound"] == "off"


async def test_env_token_status(tmp_path):
    n = await make(tmp_path, env={"HERDECK_BRIDGE_TELEGRAM_TOKEN": TOKEN})
    assert n.status()["token"] == "env" and n.status()["active"] is True


# --- failures (review focus 1 + 4) -----------------------------------------------------


def url_error():
    return urllib.error.URLError(f"https://api.telegram.org/bot{TOKEN}/sendMessage refused")


@pytest.mark.parametrize(
    "exc",
    [
        TelegramApiError(500, f"Internal error at https://api.telegram.org/bot{TOKEN}/sendMessage"),
        TimeoutError(f"timed out: bot{TOKEN}"),
        TelegramApiError(429, "Too Many Requests: retry after 5"),
        url_error(),
    ],
)
async def test_send_failure_logged_not_raised_and_token_scrubbed(tmp_path, caplog, exc):
    caplog.set_level(logging.DEBUG)
    n = await make(tmp_path)
    await start(n)
    n.t.send_errors.append(exc)
    await block(n)  # must not raise
    st = n.status()
    assert st["last_error"]
    assert TOKEN not in st["last_error"]
    assert TOKEN.split(":")[1] not in st["last_error"]
    assert TOKEN not in caplog.text
    # the next alert still goes out
    n.observe_panes([pane("working", since=9_200_000)])
    n.clock.now += 10
    await block(n, since=9_300_000)
    assert len(n.t.sent()) == 2
    assert n.status()["last_error"] == ""


async def test_interactive_send_failure_token_scrubbed(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    n = await make(
        tmp_path, tg={**TG_ON, "interactive": True, "allowed_user_ids": [USER]}
    )
    await start(n)
    n.t.send_errors.append(
        TelegramApiError(400, f"Bad Request https://api.telegram.org/bot{TOKEN}/sendMessage")
    )
    await block(n)
    assert n.status()["last_error"] and TOKEN not in n.status()["last_error"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "description",
    [
        "Conflict: can't use getUpdates method while webhook is active",
        "Conflict: terminated by other getUpdates request; make sure that only one bot instance",
    ],
)
@pytest.mark.parametrize("interactive", [True, False])
async def test_409_disables_inbound_and_retries_after_60s(tmp_path, description, interactive):
    tg = {**TG_ON, "interactive": True, "allowed_user_ids": [USER]} if interactive else TG_ON
    n = await make(tmp_path, tg=tg)
    changes = []
    n.on_status_change = lambda: changes.append(n.status()["inbound"])
    n.t.updates.append(TelegramApiError(409, description))
    delay = await n.poll_step()
    assert delay == pytest.approx(60)
    assert n.status()["inbound"] == "disabled"
    # The Bot API outcome reaches the loop via call_soon_threadsafe from the
    # client's worker thread, so it may land just after poll_step returns.
    for _ in range(200):
        if n.status()["last_error"]:
            break
        await asyncio.sleep(0.005)
    assert "Conflict" in n.status()["last_error"]
    assert "disabled" in changes
    polls = len(n.t.polls())
    n.clock.now += 30
    assert await n.poll_step() == pytest.approx(30)
    assert len(n.t.polls()) == polls  # no poll while disabled
    n.clock.now += 30
    await n.poll_step()
    assert len(n.t.polls()) == polls + 1
    assert n.status()["inbound"] == "ok"


@pytest.mark.parametrize(
    "updates",
    [
        [None, 5, "x", {"update_id": "nope"}, {"update_id": 3, "message": "garbage"}],
        [{"update_id": 4, "callback_query": "garbage"}],
        [{"update_id": 5, "message": {"chat": "bad", "from": None}}],
        {"not": "a list"},
    ],
)
@pytest.mark.parametrize("interactive", [True, False])
async def test_malformed_updates_never_crash(tmp_path, updates, interactive):
    tg = {**TG_ON, "interactive": True, "allowed_user_ids": [USER]} if interactive else TG_ON
    n = await make(tmp_path, tg=tg)
    n.t.updates.append(updates)
    await n.poll_step()  # must not raise
    await n.poll_step()


async def test_run_survives_errors_and_close_is_prompt(tmp_path):
    n = await make(tmp_path)
    n.t.updates.extend(
        [
            RuntimeError("boom"),
            TelegramApiError(429, "Too Many Requests"),
            TimeoutError("slow"),
            ValueError("bad json"),
        ]
    )
    n.t.send_errors.append(TelegramApiError(500, "down"))
    naps = []

    async def fast_sleep(seconds):
        naps.append(seconds)
        await asyncio.sleep(0)

    n._sleep = fast_sleep  # noqa: SLF001 - test seam: no real waits
    await start(n)
    task = asyncio.create_task(n.run())
    for _ in range(200):
        await asyncio.sleep(0.001)
        if len(n.t.polls()) >= 6:
            break
    assert not task.done()
    await block(n)
    assert not task.done()
    n.t.block_updates = threading.Event()
    n.t.polling.clear()
    await asyncio.to_thread(n.t.polling.wait, 2)
    # a long poll is in flight (a thread that cannot be cancelled): close
    # must not wait for it
    await asyncio.wait_for(n.close(), 1)
    # asyncio.wait never cancels the task itself (wait_for would, and so
    # hide a close() that left the poller running)
    await asyncio.wait({task}, timeout=1)
    assert task.done()
    n.t.block_updates.set()


async def test_close_cancels_timers(tmp_path):
    n = await make(
        tmp_path,
        shared={"notifications": {"done_min_work": 5, "done_short_delay": 10, "remind_after": 1}},
    )
    await start(n)
    n.clock.now = 9_100.0
    await block(n, since=9_100_000)
    await finish(n, work_since=9_000_000, done_since=9_100_000)
    assert n.timers.pending()
    await n.close()
    assert n.timers.pending() == []


# --- discovery -----------------------------------------------------------------


def chat_update(uid, chat_id, title, *, thread=None, topic=None, kind="supergroup"):
    message = {
        "message_id": uid,
        "from": {"id": 1},
        "chat": {"id": chat_id, "type": kind, "title": title},
        "text": "/status",
    }
    if thread is not None:
        message["message_thread_id"] = thread
        message["is_topic_message"] = True
        if topic is not None:
            message["reply_to_message"] = {
                "message_id": thread,
                "forum_topic_created": {"name": topic},
            }
    return {"update_id": uid, "message": message}


async def test_discovery_records_recent_chats_and_sends_nothing(tmp_path):
    n = await make(tmp_path, tg={"enabled": True, "chat_id": ""})
    changes = []
    n.on_status_change = lambda: changes.append(1)
    batch = [chat_update(i, -1000 - i, f"Group {i}") for i in range(1, 12)]
    batch.append(chat_update(12, -1001, "Group 1", thread=77, topic="herdeck"))
    n.t.updates.append(batch)
    await n.poll_step()
    await n.flush()
    chats = n.status()["recent_chats"]
    assert len(chats) == 10
    assert chats[0] == {
        "chat_id": "-1001",
        "title": "Group 1",
        "type": "supergroup",
        "message_thread_id": 77,
        "topic_name": "herdeck",
    }
    assert chats[1]["chat_id"] == "-1011" and chats[1]["message_thread_id"] is None
    assert n.t.sent() == []  # /status ignored in discovery
    assert changes
    await n.poll_step()
    assert n.t.polls()[-1]["offset"] == "13"


async def test_discovery_when_interactive_off(tmp_path):
    n = await make(tmp_path)  # chat set, interactive off
    n.t.updates.append([chat_update(1, -5, "Ops")])
    await n.poll_step()
    await n.flush()
    assert n.status()["recent_chats"][0]["chat_id"] == "-5"
    assert n.t.sent() == []


async def test_switch_to_interactive_mid_poll_leaves_updates_to_interactor(tmp_path):
    n = await make(tmp_path)  # discovery: interactive off
    status_cmd = {
        "update_id": 30,
        "message": {
            "message_id": 31,
            "from": {"id": USER},
            "chat": {"id": int(CHAT), "type": "supergroup", "title": "Ops"},
            "text": "/status",
        },
    }
    n.t.block_updates = threading.Event()
    n.t.updates.append([status_cmd])
    step = asyncio.create_task(n.poll_step())
    await asyncio.to_thread(n.t.polling.wait, 2)
    # interactive goes on while that discovery poll is in flight
    tg = {**TG_ON, "interactive": True, "allowed_user_ids": [USER]}
    assert n.tg_store.put(1, tg, "t").ok
    n.refresh()
    n.t.block_updates.set()
    await step
    assert n.t.sent() == []
    # the stale discovery round did not consume the update: the interactor
    # gets it again (Telegram re-delivers unconfirmed updates) and acts on it
    n.t.block_updates = None
    n.t.updates.append([status_cmd])
    await n.poll_step()
    assert "offset" not in n.t.polls()[-1]
    assert len(n.t.sent()) == 1 and "no tracked blocked alerts" in n.t.sent()[0]["text"]


def test_remind_max_matches_runtime():
    from herdeck import bridge_notify
    from herdeck.deckapp.live import REMIND_MAX

    assert bridge_notify.REMIND_MAX == REMIND_MAX == 3


async def test_no_token_no_polling(tmp_path):
    n = await make(tmp_path, token=False)
    await n.poll_step()
    assert n.t.calls == []
    assert n.status()["inbound"] == "off"


# --- send_test -----------------------------------------------------------------------


async def test_send_test_ok(tmp_path):
    n = await make(tmp_path, tg={"enabled": False, "chat_id": CHAT, "message_thread_id": 9})
    ok, message = await n.send_test()
    assert ok is True
    (msg,) = n.t.sent()
    assert msg["text"] == tr("en", "telegram.test") == "herdeck ✓ test"
    assert msg["chat_id"] == CHAT and msg["message_thread_id"] == "9"


async def test_send_test_czech(tmp_path):
    n = await make(tmp_path, tg={**TG_ON, "language": "cs"})
    assert (await n.send_test())[0] is True
    assert n.t.sent()[0]["text"] == tr("cs", "telegram.test")


async def test_send_test_error_scrubbed(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    n = await make(tmp_path)
    n.t.send_errors.append(
        TelegramApiError(401, f"Unauthorized https://api.telegram.org/bot{TOKEN}/sendMessage")
    )
    ok, message = await n.send_test()
    assert ok is False and message
    assert TOKEN not in message and TOKEN not in caplog.text
    assert TOKEN not in n.status()["last_error"]


async def test_send_test_without_token_or_chat(tmp_path):
    n = await make(tmp_path, token=False)
    ok, message = await n.send_test()
    assert ok is False and message
    n2 = await make(tmp_path / "b", tg={"enabled": True, "chat_id": ""})
    assert (await n2.send_test())[0] is False
    assert n2.t.sent() == []


# --- wiring ---------------------------------------------------------------------------


async def test_event_hub_listener_feeds_notifier(tmp_path):
    from herdeck.bridge import StubHerdr, _wire_panes
    from herdeck.status_since import StatusSinceTracker

    n = await make(tmp_path)
    raw = {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "cwd": "/tmp/api",
        "foreground_cwd": "/tmp/api",
        "agent_status": "working",
        "agent": "claude",
        "terminal_id": "t1",
    }
    herdr = StubHerdr(panes=[raw])
    herdr.detection["w1:p1"] = "Allow edit?\n1. Yes\n2. No"
    since = StatusSinceTracker(None)
    hub = EventHub(herdr, "srv", clock=lambda: 1000.0, epoch="e1")
    hub.add_listener(n.on_event)

    def observe():
        panes = _wire_panes(herdr.panes)
        since.stamp(panes)
        EventHub.stamp(panes)
        hub.observe(panes)
        n.observe_panes(panes)

    observe()
    herdr.panes[0]["agent_status"] = "blocked"
    observe()
    for _ in range(20):
        await asyncio.sleep(0)
    await n.flush()
    assert len(n.t.sent()) == 1
    await hub.close()


async def test_event_hub_listener_exceptions_are_logged_not_raised(caplog):
    hub = EventHub(None, "srv", clock=lambda: 1000.0, epoch="e1")
    seen = []

    def bad(frame):
        raise RuntimeError("listener broke")

    hub.add_listener(bad)
    hub.add_listener(seen.append)
    p = pane("done", since=900_000)
    hub.observe([p])  # emits "done" synchronously
    assert [f["kind"] for f in seen] == ["done"]
    assert "listener" in caplog.text


def test_i18n_test_message_keys():
    assert STRINGS["en"]["telegram.test"] == "herdeck ✓ test"
    assert "telegram.test" in STRINGS["cs"]


async def test_token_change_mid_interactive_poll_drops_old_cursor(tmp_path):
    """The getUpdates cursor belongs to one bot: an interactive poll that was in
    flight for the old token must not carry its offset over to the new bot."""
    tg = {**TG_ON, "interactive": True, "allowed_user_ids": [USER]}
    n = await make(tmp_path, tg=tg)
    n.t.updates.append([{"update_id": 50}])
    await n.poll_step()
    assert n._offset == 51
    n.t.block_updates = threading.Event()
    n.t.updates.append([{"update_id": 60}])
    n.t.polling.clear()
    step = asyncio.create_task(n.poll_step())
    await asyncio.to_thread(n.t.polling.wait, 2)
    assert n.t.polls()[-1]["offset"] == "51"
    assert n.tg_store.set_token("987654321:" + "B" * 35) is None
    n.refresh()
    assert n._offset is None
    n.t.block_updates.set()
    await step
    assert n._offset is None  # not the old bot's cursor
    n.t.block_updates = None
    await n.poll_step()
    assert "offset" not in n.t.polls()[-1]
    await n.close()


# --- the same blocked episode asks again ------------------------------------------


def _reask(n, p, revision, prompt="Run tests?\n1. Yes\n2. No"):
    n.observe_panes([p])
    n.on_event(frame("blocked", p, prompt=prompt, prompt_revision=revision))


async def test_blocked_new_revision_without_answer_is_not_realerted(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = await block(n)
    assert len(n.t.sent()) == 1
    n.clock.now += 60  # well past the blocked cooldown
    _reask(n, p, "r2")
    await n.flush()
    assert len(n.t.sent()) == 1  # still the same unanswered question for the user


async def test_blocked_new_revision_after_answer_realerts(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = await block(n)
    n.on_event(frame("answered", p, by="deck"))
    n.clock.now += 60
    _reask(n, p, "r2")
    await n.flush()
    sent = n.t.sent()
    assert len(sent) == 2
    assert "Run tests?" in sent[1]["text"]
    # the re-announced question is open again: the same revision re-sent
    # (snapshot churn) is not news
    _reask(n, p, "r2")
    await n.flush()
    assert len(n.t.sent()) == 2


async def test_blocked_same_revision_after_answer_is_not_realerted(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = await block(n)
    n.on_event(frame("answered", p, by="deck"))
    n.clock.now += 60
    _reask(n, p, "r1", prompt="Allow edit?\n1. Yes\n2. No")
    await n.flush()
    assert len(n.t.sent()) == 1


async def test_blocked_reopen_after_answer_respects_cooldown(tmp_path):
    n = await make(tmp_path)
    await start(n)
    p = await block(n)
    n.on_event(frame("answered", p, by="deck"))
    n.clock.now += 2  # inside the 5 s blocked cooldown
    _reask(n, p, "r2")
    await n.flush()
    assert len(n.t.sent()) == 1
    n.on_event(frame("answered", p, by="deck"))
    n.clock.now += 10
    _reask(n, p, "r3")
    await n.flush()
    assert len(n.t.sent()) == 2


async def test_discovery_sees_a_command_addressed_to_the_bot_in_a_topic(tmp_path):
    """A group bot in privacy mode only receives commands addressed to it (and
    replies to it): ``/start@<bot>`` sent inside a forum topic is how the setup
    finds that topic. Discovery records it and never answers."""
    n = await make(tmp_path, tg={"enabled": False, "chat_id": ""})
    update = chat_update(5, -100777, "Team", thread=33, topic="deck alerts")
    update["message"]["text"] = "/start@herdeck_bot"
    update["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 18}]
    n.t.updates.append([update])
    await n.poll_step()
    await n.flush()
    assert n.status()["recent_chats"][0] == {
        "chat_id": "-100777",
        "title": "Team",
        "type": "supergroup",
        "message_thread_id": 33,
        "topic_name": "deck alerts",
    }
    assert [m for m, _ in n.t.calls if m != "getUpdates"] == []  # no reply of any kind
