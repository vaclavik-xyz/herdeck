from herdeck.notify import NoopNotifier, Notifier, escape_applescript


def test_escape_applescript_quotes_and_backslashes():
    assert escape_applescript('a"b\\c') == 'a\\"b\\\\c'


def test_noop_notifier_never_raises():
    NoopNotifier().notify("t", "b", sound=True)  # no exception, no side effect


def test_notifier_uses_injected_sink():
    calls = []
    n = Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    n.notify("Blocked", "api · main", sound=True)
    assert calls == [("Blocked", "api · main", True)]


def test_notifier_swallows_sink_errors():
    def boom(*a):
        raise RuntimeError("x")

    Notifier(sink=boom).notify("t", "b")  # must not raise


def test_telegram_sink_builds_url_and_payload():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append((url, fields)))
    sink("Blocked", "api · main", True)
    url, fields = sent[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert fields["chat_id"] == "42"
    assert fields["text"] == "Blocked\napi · main"
    assert fields["disable_notification"] == "false"  # sound=True -> not silent


def test_telegram_sink_silent_when_no_sound():
    from herdeck.notify import make_telegram_sink

    sent = []
    sink = make_telegram_sink("TOK", "42", post=lambda url, fields: sent.append(fields))
    sink("t", "b", False)
    assert sent[0]["disable_notification"] == "true"


def test_composite_sink_calls_all_even_if_one_raises():
    from herdeck.notify import composite_sink

    calls = []

    def boom(*a):
        raise RuntimeError("x")

    sink = composite_sink(
        [
            lambda t, b, s: calls.append(("a", t)),
            boom,
            lambda t, b, s: calls.append(("c", t)),
        ]
    )
    sink("title", "body", True)
    assert calls == [("a", "title"), ("c", "title")]
