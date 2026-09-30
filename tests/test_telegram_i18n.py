"""Interactive Telegram alerts and every reply of the interactor follow the
configured language (en default, cs), like the one-way messages."""

from __future__ import annotations

import re

import pytest

from herdeck.app_control import ActionResult
from herdeck.model import AgentKey, AgentState, Status
from herdeck.telegram import TelegramAlertFormatter, TelegramInteractor

KEY = AgentKey("local", "w1H:p4")
AGENT = AgentState(KEY, "claude", "api", Status.BLOCKED, terminal_id="t1")
PROMPT = "Allow edit?\n1. Yes\n2. No"
# English words the cs formatter output must not contain (the prompt excerpt
# and the agent name are the agent's own text, not ours).
ENGLISH = re.compile(r"\b(Waiting|Reply|Approve|Deny|Read again|blocked|needs input|"
                     r"unavailable|message|agent\b)", re.I)


def fmt(lang, prompt=PROMPT, body="api"):
    return TelegramAlertFormatter(language=lang).blocked_alert(
        AGENT, metadata_body=body, prompt=prompt, token="tok"
    )


def buttons(markup):
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


def callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def test_english_alert():
    text, markup = fmt("en")
    assert text.startswith("claude · needs input\napi\n")
    assert "Waiting for:\n" + PROMPT in text
    assert "Reply to this message to send text to the agent." in text
    assert buttons(markup) == ["Approve", "Deny", "Stop", "Read again"]


def test_czech_alert_has_no_english():
    text, markup = fmt("cs")
    assert text.startswith("claude · čeká na tebe\napi\n")
    assert "Ptá se:\n" + PROMPT in text
    assert "Odpovědí na tuto zprávu pošleš text agentovi." in text
    assert buttons(markup) == ["Schválit", "Zamítnout", "Stop", "Načíst znovu"]
    ours = text.replace(PROMPT, "").replace("claude", "")
    assert not ENGLISH.search(ours), ours


@pytest.mark.parametrize("lang,expect", [("en", "Prompt unavailable; use Read again."),
                                         ("cs", "Otázka není k dispozici, použij Načíst znovu.")])
def test_prompt_unavailable(lang, expect):
    text, markup = fmt(lang, prompt="")
    assert expect in text
    assert len(buttons(markup)) == 2
    if lang == "cs":
        assert not ENGLISH.search(text.replace("claude", ""))


@pytest.mark.parametrize("lang", ["en", "cs"])
def test_no_internal_pane_id_and_unchanged_callback_data(lang):
    text, markup = fmt(lang)
    assert "w1H:p4" not in text and "local:" not in text
    assert callbacks(markup) == ["h:tok:approve", "h:tok:deny", "h:tok:stop", "h:tok:read"]


# --- the interactor's replies ------------------------------------------------------


class Bot:
    def __init__(self):
        self.sent, self.answers, self.edits = [], [], []

    def send_message(self, **fields):
        self.sent.append(fields)
        return {"message_id": 100 + len(self.sent)}

    def answer_callback_query(self, cb_id, *, text=""):
        self.answers.append(text)

    def edit_message_text(self, **fields):
        self.edits.append(fields)
        return {}


class Control:
    def __init__(self, result=None):
        self.result = result or ActionResult(True)
        self.agent = AGENT

    def current_agent(self, key):
        return self.agent

    async def read_prompt(self, key, *, timeout=3.0):
        return PROMPT

    async def approve(self, key, *, timeout=3.0, **kw):
        return self.result

    async def send_text(self, key, text, *, timeout=3.0, **kw):
        return self.result


def interactor(lang, control=None):
    bot = Bot()
    tg = TelegramInteractor(bot, control or Control(), chat_id="-100", message_thread_id=None,
                            allowed_user_ids=[7], language=lang)
    return tg, bot


def tap(token, action, user=7):
    return {"callback_query": {"id": "cb", "from": {"id": user}, "data": f"h:{token}:{action}",
                               "message": {"message_id": 101, "chat": {"id": -100}}}}


def msg(text, reply_to=101):
    m = {"message_id": 500, "from": {"id": 7}, "chat": {"id": -100}, "text": text}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return m


async def alerted(lang, control=None):
    tg, bot = interactor(lang, control)
    await tg.notify_blocked(AGENT, body="api", sound=False, multi_server=False)
    token = bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    return tg, bot, token


@pytest.mark.parametrize("lang,stale,denied", [("en", "stale", "not authorized"),
                                               ("cs", "neaktuální", "nemáš oprávnění")])
async def test_stale_and_unauthorized_replies(lang, stale, denied):
    tg, bot, token = await alerted(lang)
    await tg.process_update(tap(token, "approve", user=8))
    assert bot.answers[-1] == denied
    await tg.process_update(tap("gone", "approve"))
    assert bot.answers[-1] == stale
    stale_result = Control(ActionResult(False, skipped=True, message="stale"))
    tg, bot, token = await alerted(lang, stale_result)
    await tg.process_update(tap(token, "approve"))
    assert bot.answers[-1] == stale
    await tg.process_update({"message": msg("hi", reply_to=999)})
    assert bot.sent[-1]["text"] == {"en": "alert is stale", "cs": "upozornění je neaktuální"}[lang]


@pytest.mark.parametrize("lang,sent,confirm,gone,failed", [
    ("en", "sent", "tap again to confirm", "agent is no longer available", "failed"),
    ("cs", "odesláno", "potvrď dalším klepnutím", "agent už není k dispozici", "nepovedlo se"),
])
async def test_action_replies(lang, sent, confirm, gone, failed):
    for result, expect in [
        (ActionResult(True), sent),
        (ActionResult(False, message="confirmation required"), confirm),
        (ActionResult(False, message="agent is no longer available"), gone),
        (ActionResult(False, message="some bridge error text"), failed),
    ]:
        tg, bot, token = await alerted(lang, Control(result))
        await tg.process_update(tap(token, "approve"))
        assert bot.answers[-1] == expect


@pytest.mark.parametrize("lang,sent,none,tracked", [
    ("en", "sent to the agent", "no tracked blocked alerts", "tracked blocked alerts:"),
    ("cs", "odesláno agentovi", "žádná sledovaná upozornění", "sledovaná upozornění:"),
])
async def test_reply_and_status_replies(lang, sent, none, tracked):
    tg, bot, token = await alerted(lang)
    await tg.process_update({"message": msg("/status", reply_to=None)})
    assert bot.sent[-1]["text"].splitlines()[0] == tracked
    assert "w1H:p4" not in bot.sent[-1]["text"]
    await tg.process_update({"message": msg("do it")})
    assert bot.sent[-1]["text"] == sent
    tg, bot = interactor(lang)
    await tg.process_update({"message": msg("/status", reply_to=None)})
    assert bot.sent[-1]["text"] == none


@pytest.mark.parametrize("lang", ["en", "cs"])
async def test_read_again_edits_in_the_language_without_pane_ids(lang):
    tg, bot, token = await alerted(lang)
    await tg.process_update(tap(token, "read"))
    assert bot.answers[-1] == {"en": "refreshed", "cs": "načteno znovu"}[lang]
    edited = bot.edits[-1]["text"]
    assert edited == bot.sent[0]["text"]  # the same message, re-rendered
