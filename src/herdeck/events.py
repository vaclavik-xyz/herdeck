"""Bridge-side agent lifecycle events: episodes, the blocked prompt, answers.

Every deck runtime used to detect "agent X just blocked / finished" on its
own, from snapshot diffs. Several runtimes (a desk Mac, a laptop, the web
cockpit, Telegram) therefore disagreed, a runtime that slept missed whatever
happened meanwhile, and an answer given on one of them left the others'
banners up. The bridge sees every change, so it is the single source of truth:

* **Episodes.** Each BLOCKED and DONE stretch of a pane is an episode with a
  stable id: a hash of pane id + terminal identity + kind + ``status_since_ms``
  (status_since.py persists that clock, so a bridge restart keeps the id).
* **Event frames** (capability ``events``) announce transitions::

    {"type": "event", "server_id", "epoch", "seq", "kind", "episode_id",
     "pane_id", "terminal_id", "at_ms",
     "prompt"?, "prompt_revision"?, "prompt_truncated"?,   # kind "blocked"
     "by"?, "via"?,                                        # kind "answered"
     "replay"?: true}                                      # sent from the ring

  ``kind`` is ``blocked`` / ``done`` (an episode opened; ``at_ms`` is when the
  pane entered the status), ``unblocked`` / ``cleared`` (a blocked / done
  episode ended) or ``answered`` (a client's answer to that blocked episode
  went through). ``blocked`` is sent once the bridge has pre-read the prompt
  (or gave up after ``PROMPT_WAIT_S``) and again whenever the prompt's
  revision changes within the same episode.
* **Replay.** Events live in an in-memory ring (``RING_MAX_EVENTS`` /
  ``RING_MAX_AGE_MS``). A client subscribes with ``{"type": "list",
  "events": {"after": <seq>|null, "epoch": <epoch>|null, "client": <label>}}``
  and gets the events after ``after`` (same ``epoch``), or — for a first
  subscription, a bridge restart (other epoch) or an evicted ``after`` — the
  events it can still give, then ``{"type": "event_sync", "server_id",
  "epoch", "seq", "gap"}``. ``gap`` means some events may be missing: the
  client dedupes by ``episode_id``. With ``after: null`` only the latest
  events of the currently open episodes are sent (a fresh client's baseline).
* **Answers.** An answer (guarded ``act``, ``send_text`` or
  ``choose_if_blocked``) to a pane with an open blocked episode marks the
  episode answered and broadcasts ``answered``. An answer that names an
  ``episode_id`` is refused with ``{"skipped": true, "message": "stale"}`` when
  that episode is no longer the pane's open one, is being answered by another
  request right now, or was already answered — unless the prompt changed
  since (the request's ``prompt_revision`` is the current one, and not the one
  that was answered).

Event-loop only (not thread-safe), like the bridge's other per-pane state.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .model import Status
from .protocol import effective_status, encode

log = logging.getLogger(__name__)

CAPABILITY = "events"
WIRE_FIELD = "episode_id"
# Ring bounds: enough to cover a laptop lid closed over a meeting.
RING_MAX_EVENTS = 500
RING_MAX_AGE_MS = 30 * 60 * 1000
# A blocked event waits this long for the prompt pre-read, then goes without.
PROMPT_WAIT_S = 2.0
# Open blocked episodes re-read their prompt this often (a dialog may change
# in place: the next question of a multi-step prompt, with no status change).
PROMPT_POLL_S = 5.0
# The prompt rides in event frames: the question sits at the bottom of the
# capture, so an overlong one keeps its tail.
PROMPT_MAX_CHARS = 8000
CLIENT_LABEL_MAX = 64
ANSWER_MESSAGES = frozenset({"act", "send_text", "choose_if_blocked"})
STALE = "stale"

_OPEN_KINDS = {Status.BLOCKED: "blocked", Status.DONE: "done"}
_CLOSE_KIND = {"blocked": "unblocked", "done": "cleared"}
# CSI / OSC / other two-byte escape sequences, then leftover controls (C0 but
# tab/newline, DEL, C1) and bidi overrides: a capture is shown verbatim.
_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_LABEL_RE = re.compile(r"[^\w .:@/+-]")


def episode_id(pane_id: str, terminal_id: str, kind: str, since_ms: int) -> str:
    """The stable id of one blocked/done stretch of a pane."""
    raw = json.dumps([pane_id, terminal_id, kind, since_ms], separators=(",", ":"))
    return hashlib.blake2s(raw.encode(), digest_size=8).hexdigest()


def episode_kind(pane: dict) -> str | None:
    """"blocked" / "done" for a wire pane in such an episode, else None.
    Uses the status the runtime shows (a ``waiting_on`` done pane is WAITING)."""
    return _OPEN_KINDS.get(effective_status(pane.get("status", "unknown"), pane.get("waiting_on")))


def pane_episode_id(pane: dict) -> str:
    """The episode id a wire pane is in ("" when none or not derivable)."""
    kind = episode_kind(pane)
    since = pane.get("status_since_ms")
    pane_id = pane.get("pane_id")
    if kind is None or type(since) is not int or not isinstance(pane_id, str):
        return ""
    return episode_id(pane_id, pane.get("terminal_id") or "", kind, since)


def _clean(text: str) -> str:
    clean = _CONTROL_RE.sub("", _ESCAPE_RE.sub("", text).replace("\r\n", "\n"))
    # A lone surrogate (json.loads accepts one) cannot be re-encoded.
    return clean.encode("utf-8", "replace").decode("utf-8")


def sanitize_prompt(text: object) -> tuple[str, bool]:
    """A pane capture -> (plain text, truncated). Escape sequences and control
    characters go (CRLF -> LF); an overlong capture keeps its last
    ``PROMPT_MAX_CHARS`` characters."""
    if not isinstance(text, str):
        return "", False
    clean = _clean(text)
    if len(clean) > PROMPT_MAX_CHARS:
        return clean[-PROMPT_MAX_CHARS:], True
    return clean, False


def prompt_revision(prompt: str) -> str:
    return hashlib.blake2s(prompt.encode("utf-8"), digest_size=8).hexdigest()


def prompt_forms(raw: str) -> list[str]:
    """Every text a client may have computed a decision revision over: the raw
    ``read`` capture (older runtimes) and the event prompt (sanitized, capped)."""
    forms = [raw]
    for form in (_clean(raw), sanitize_prompt(raw)[0]):
        if form not in forms:
            forms.append(form)
    return forms


def client_label(value: object) -> str:
    """A client's self-chosen name for ``answered.by`` (bounded, printable)."""
    if not isinstance(value, str):
        return "client"
    label = _LABEL_RE.sub("", value).strip()[:CLIENT_LABEL_MAX]
    return label or "client"


@dataclass
class Episode:
    id: str
    kind: str
    pane_id: str
    terminal_id: str
    since_ms: int
    prompt: str | None = None
    truncated: bool = False
    revision: str | None = None
    announced: bool = False
    # None = not answered; "" = answered while the prompt was unknown.
    answered_revision: str | None = None
    answering: bool = False
    read_at: float = 0.0
    reading: asyncio.Task | None = None
    # Latest frame per kind of this episode (a fresh subscriber's baseline).
    frames: dict[str, dict] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.answered_revision is not None


@dataclass
class AnswerTicket:
    episode: Episode
    by: str
    via: str


@dataclass
class _Sub:
    lock: asyncio.Lock
    active: bool = False
    sent_upto: int = 0


class EventHub:
    """Episodes of one bridge, the event ring, and the subscribed clients."""

    def __init__(
        self,
        herdr,
        server_id: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        prompt_wait_s: float = PROMPT_WAIT_S,
        prompt_poll_s: float = PROMPT_POLL_S,
        epoch: str | None = None,
    ):
        self._herdr = herdr
        self._server_id = server_id
        self._clock = clock
        self._monotonic = monotonic
        self._prompt_wait_s = prompt_wait_s
        self._prompt_poll_s = prompt_poll_s
        self.epoch = epoch or secrets.token_hex(8)
        self.seq = 0
        self._ring: collections.deque[tuple[int, dict]] = collections.deque()
        self._episodes: dict[str, Episode] = {}  # pane_id -> open episode
        self._subs: dict[object, _Sub] = {}
        self._queue: asyncio.Queue[dict] | None = None
        self._pump: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # --- wire panes ---------------------------------------------------------
    @staticmethod
    def stamp(panes: list[dict]) -> list[dict]:
        """Set ``episode_id`` on every wire pane ("" outside an episode)."""
        for pane in panes:
            pane[WIRE_FIELD] = pane_episode_id(pane)
        return panes

    def open_episode(self, pane_id: str) -> Episode | None:
        return self._episodes.get(pane_id)

    # --- transitions --------------------------------------------------------
    def observe(self, panes: list[dict]) -> None:
        """Digest one FULL fleet snapshot (after it went out to the clients)."""
        seen: dict[str, str] = {}
        for pane in panes:
            pane_id = pane.get("pane_id")
            kind = episode_kind(pane)
            since = pane.get("status_since_ms")
            if not isinstance(pane_id, str) or kind is None or type(since) is not int:
                continue
            terminal_id = pane.get("terminal_id") or ""
            ep_id = episode_id(pane_id, terminal_id, kind, since)
            seen[pane_id] = ep_id
            current = self._episodes.get(pane_id)
            if current is not None and current.id == ep_id:
                continue
            if current is not None:
                self._close(current)
            ep = Episode(ep_id, kind, pane_id, terminal_id, since)
            self._episodes[pane_id] = ep
            if kind == "done":
                self._announce(ep)
            else:
                ep.reading = self._spawn(self._first_read(ep))
        for pane_id, ep in list(self._episodes.items()):
            if pane_id not in seen:
                self._close(ep)

    def _close(self, ep: Episode) -> None:
        if self._episodes.get(ep.pane_id) is ep:
            del self._episodes[ep.pane_id]
        if ep.reading is not None and not ep.reading.done():
            ep.reading.cancel()
        if ep.announced:
            self._emit(ep, _CLOSE_KIND[ep.kind], at_ms=self._now_ms())

    def _announce(self, ep: Episode) -> None:
        ep.announced = True
        extra: dict = {}
        if ep.kind == "blocked" and ep.prompt is not None:
            extra = {"prompt": ep.prompt, "prompt_revision": ep.revision}
            if ep.truncated:
                extra["prompt_truncated"] = True
        self._emit(ep, ep.kind, at_ms=ep.since_ms, **extra)

    # --- the blocked prompt -------------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _read(self, ep: Episode) -> bool:
        """Read ``ep``'s prompt; True when the revision changed."""
        ep.read_at = self._monotonic()
        text = await self._herdr.read_pane(ep.pane_id, "detection")
        if self._episodes.get(ep.pane_id) is not ep:
            return False
        prompt, truncated = sanitize_prompt(text)
        revision = prompt_revision(prompt)
        if revision == ep.revision:
            return False
        ep.prompt, ep.truncated, ep.revision = prompt, truncated, revision
        return True

    async def _first_read(self, ep: Episode) -> None:
        try:
            await asyncio.wait_for(self._read(ep), timeout=self._prompt_wait_s)
        except asyncio.CancelledError:
            if self._episodes.get(ep.pane_id) is not ep:
                return  # the episode ended before it was announced
            raise
        except Exception as exc:
            log.info("blocked prompt pre-read failed pane=%s: %s", ep.pane_id, exc)
        finally:
            ep.reading = None
        if self._episodes.get(ep.pane_id) is ep and not ep.announced:
            self._announce(ep)

    async def _reread(self, ep: Episode) -> None:
        try:
            changed = await asyncio.wait_for(self._read(ep), timeout=self._prompt_wait_s * 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            ep.reading = None
        if changed and self._episodes.get(ep.pane_id) is ep and ep.announced:
            self._announce(ep)  # same episode, new prompt revision

    def poll_prompts(self) -> int:
        """Start a re-read for each announced blocked episode that is due."""
        now = self._monotonic()
        started = 0
        for ep in list(self._episodes.values()):
            if (
                ep.kind == "blocked"
                and ep.announced
                and ep.reading is None
                and now - ep.read_at >= self._prompt_poll_s
            ):
                ep.reading = self._spawn(self._reread(ep))
                started += 1
        return started

    async def run(self) -> None:
        """The prompt poll loop (the fan-out pump starts on the first event)."""
        while True:
            await asyncio.sleep(self._prompt_poll_s)
            try:
                self.poll_prompts()
            except Exception:
                log.warning("prompt poll failed", exc_info=True)

    # --- answers ------------------------------------------------------------
    def begin_answer(self, msg: dict, label: str) -> AnswerTicket | str | None:
        """Gate an answer message: a ticket to settle after the send, "stale"
        to refuse it, or None when it answers no episode (a forced act, a
        pane with no open blocked episode and no claimed one)."""
        kind = msg.get("type")
        if kind not in ANSWER_MESSAGES or (kind == "act" and msg.get("guard", True) is False):
            return None
        pane_id = msg.get("pane_id")
        ep = self._episodes.get(pane_id) if isinstance(pane_id, str) else None
        if ep is not None and ep.kind != "blocked":
            ep = None
        terminal_id = msg.get("terminal_id")
        if ep is not None and isinstance(terminal_id, str) and terminal_id:
            if ep.terminal_id and ep.terminal_id != terminal_id:
                ep = None
        claimed = msg.get("episode_id")
        if isinstance(claimed, str) and claimed:
            if ep is None or ep.id != claimed or ep.answering:
                return STALE
            if ep.answered:
                revision = msg.get("prompt_revision")
                if not (
                    isinstance(revision, str)
                    and revision
                    and revision == ep.revision
                    and revision != ep.answered_revision
                ):
                    return STALE
        elif ep is None or ep.answering:
            return None  # an older client: never refused, only reported
        ep.answering = True
        via = msg.get("via")
        return AnswerTicket(ep, label, client_label(via) if isinstance(via, str) else "")

    def end_answer(self, ticket: AnswerTicket, sent: bool) -> None:
        ep = ticket.episode
        ep.answering = False
        if not sent:
            return
        ep.answered_revision = ep.revision or ""
        extra = {"by": ticket.by}
        if ticket.via:
            extra["via"] = ticket.via
        self._emit(ep, "answered", at_ms=self._now_ms(), **extra)

    # --- the ring and the fan-out -------------------------------------------
    def _emit(self, ep: Episode, kind: str, *, at_ms: int, **extra) -> dict:
        self.seq += 1
        frame = {
            "type": "event",
            "server_id": self._server_id,
            "epoch": self.epoch,
            "seq": self.seq,
            "kind": kind,
            "episode_id": ep.id,
            "pane_id": ep.pane_id,
            "terminal_id": ep.terminal_id,
            "at_ms": at_ms,
            **extra,
        }
        ep.frames[kind] = frame
        # Age is judged by emission time: at_ms of a blocked event is when the
        # pane blocked, possibly long before a bridge restart re-announced it.
        self._ring.append((self._now_ms(), frame))
        self._prune()
        if self._subs:
            self._ensure_pump()
            assert self._queue is not None
            self._queue.put_nowait(frame)
        return frame

    def _prune(self) -> None:
        horizon = self._now_ms() - RING_MAX_AGE_MS
        while self._ring and (len(self._ring) > RING_MAX_EVENTS or self._ring[0][0] < horizon):
            self._ring.popleft()

    def events(self) -> list[dict]:
        """The ring, oldest first."""
        return [frame for _t, frame in self._ring]

    def _ensure_pump(self) -> None:
        if self._pump is None or self._pump.done():
            if self._queue is None:
                self._queue = asyncio.Queue()
            self._pump = asyncio.get_running_loop().create_task(self._pump_loop())

    async def _pump_loop(self) -> None:
        from .bridge import _send_to_client

        assert self._queue is not None
        while True:
            frame = await self._queue.get()
            targets = [
                (ws, sub)
                for ws, sub in list(self._subs.items())
                if sub.active and frame["seq"] > sub.sent_upto
            ]
            for _ws, sub in targets:
                sub.sent_upto = frame["seq"]
            if targets:
                raw = encode(frame)
                await asyncio.gather(*(_send_to_client(ws, raw, sub.lock) for ws, sub in targets))

    def _replay_frames(self, after: int | None, epoch: object) -> tuple[list[dict], bool]:
        if after is None:
            frames = [f for ep in self._episodes.values() for f in ep.frames.values()]
            return sorted(frames, key=lambda f: f["seq"]), False
        ring = self.events()
        exact = (
            epoch == self.epoch
            and after <= self.seq
            and (ring[0]["seq"] <= after + 1 if ring else after == self.seq)
        )
        if exact:
            return [f for f in ring if f["seq"] > after], False
        # Bridge restarted, or the ring no longer reaches back that far: all
        # that is left, plus the open episodes' events the ring already lost.
        frames = {f["seq"]: f for f in ring}
        for ep in self._episodes.values():
            for f in ep.frames.values():
                frames.setdefault(f["seq"], f)
        return [frames[s] for s in sorted(frames)], True

    async def subscribe(self, ws, lock: asyncio.Lock, request: dict) -> bool:
        """Replay to a client that asked for events, then keep it posted.
        False when the client went away meanwhile."""
        from .bridge import _send_to_client

        after = request.get("after")
        if type(after) is not int or after < 0:
            after = None
        frames, gap = self._replay_frames(after, request.get("epoch"))
        upto = self.seq  # everything up to here is replayed or deliberately left out
        sub = self._subs[ws] = _Sub(lock, sent_upto=upto)
        sync = {
            "type": "event_sync",
            "server_id": self._server_id,
            "epoch": self.epoch,
            "seq": upto,
            "gap": gap,
        }
        for raw in [*(encode({**f, "replay": True}) for f in frames), encode(sync)]:
            if not await _send_to_client(ws, raw, lock):
                self._subs.pop(ws, None)
                return False
        # Catch up on what was emitted while the replay was on the wire; the
        # pump takes over once nothing is left (no await between that check
        # and the switch, so no event can fall in between).
        while self._subs.get(ws) is sub:
            pending = [f for f in self.events() if f["seq"] > sub.sent_upto]
            if not pending:
                sub.active = True
                return True
            for frame in pending:
                sub.sent_upto = frame["seq"]
                if not await _send_to_client(ws, encode(frame), lock):
                    self._subs.pop(ws, None)
                    return False
        return False

    def unsubscribe(self, ws) -> None:
        self._subs.pop(ws, None)

    async def close(self) -> None:
        tasks = [t for t in (self._pump, *self._tasks) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
