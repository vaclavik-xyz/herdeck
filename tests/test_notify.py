from herdeck.notify import NoopNotifier, Notifier, escape_applescript


def test_escape_applescript_quotes_and_backslashes():
    assert escape_applescript('a"b\\c') == 'a\\"b\\\\c'


def test_noop_notifier_never_raises():
    NoopNotifier().notify("t", "b", sound=True)   # no exception, no side effect


def test_notifier_uses_injected_sink():
    calls = []
    n = Notifier(sink=lambda title, body, sound: calls.append((title, body, sound)))
    n.notify("Blocked", "api · main", sound=True)
    assert calls == [("Blocked", "api · main", True)]


def test_notifier_swallows_sink_errors():
    def boom(*a):
        raise RuntimeError("x")
    Notifier(sink=boom).notify("t", "b")   # must not raise
