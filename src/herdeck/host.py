"""``herdeck`` / ``herdeck-web``: the one runtime with a deck front attached.

The desktop sidecar and the launchd ``herdeck.runtime`` already run the one
runtime (``DeckApp`` + ``LiveSource`` + ``Orchestrator``). This entry point runs
the same runtime for the CLI consumers and attaches the requested front as a
render sink:

* ``web`` — the browser cockpit (``driver.web.WebDeck``), including the
  versioned ``/api/v1`` cockpit API and live ``/term`` streams;
* ``d200`` — a Ulanzi D200, arbitrated by ``d200.lock`` (one owner per machine);
* ``elgato`` — an Elgato Stream Deck over USB;
* ``fake`` — headless (tests, CI);
* unset — auto-detect D200, then Elgato, else the web cockpit.

``HERDECK_DECK=elgato-plugin`` hands off to the Elgato plugin's IPC backend
(``herdeck.elgato.runtime``), which is a separate front-end.

Interactive Telegram alerts and the cockpit API run in ``deckapp.services``
on top of the same notification engine and bridge connections.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .bootstrap import (
    _discover_config_path,
    _discover_local_config_path,
    _local_config_for_server,
    resolve_mode,
    resolve_runtime_config,
    resolve_socket_path,
)
from .config import Config, ConfigError, HardwareConfig
from .pins import PinStore

log = logging.getLogger("herdeck")


# --- web front helpers (also used by herdeck.web and herdeck.service) ---------


def validate_web_bind(host: str, *, getenv=os.environ.get) -> str:
    """Allow remote web control only on an explicit Tailscale interface."""
    from .bind import validate_bind

    return validate_bind(host, env_name="HERDECK_WEB_BIND", getenv=getenv)


def _iface_addr(probe_host: str) -> str | None:
    """The local source address the OS would route to ``probe_host`` (UDP
    connect — no packet is sent). Used to discover the Tailscale / LAN
    interface addresses for the simulator announcement."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe_host, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def simulator_urls(
    host: str,
    port: int,
    token: str,
    *,
    base_path: str = "",
    public_origin: str = "",
) -> list[str]:
    """URLs worth printing for the simulator. A wildcard bind is literally
    unroutable (http://0.0.0.0:…) and the README's primary workflow opens the
    page from a phone over Tailscale — so for wildcard binds the Tailscale
    (100.64/10) and default-route addresses are announced too."""
    suffix = f"{base_path}/?token={token}"
    if public_origin:
        return [f"{public_origin}{suffix}"]
    if host not in ("0.0.0.0", "::"):
        return [f"http://{host}:{port}{suffix}"]
    urls: list[str] = []
    tailscale = _iface_addr("100.100.100.100")  # MagicDNS resolver -> ts iface
    if tailscale and tailscale.startswith("100."):
        urls.append(f"http://{tailscale}:{port}{suffix}")
    lan = _iface_addr("1.1.1.1")
    if lan and f"http://{lan}:{port}{suffix}" not in urls:
        urls.append(f"http://{lan}:{port}{suffix}")
    urls.append(f"http://127.0.0.1:{port}{suffix}")
    return urls


def resolve_deck_kind(config: Config | None, *, getenv=os.environ.get):
    env_kind = getenv("HERDECK_DECK")
    if env_kind:
        return env_kind
    if getenv("HERDECK_FAKE_DECK"):
        return "fake"
    return config.hardware.deck if config and config.hardware.deck else None


def build_web_deck(slots: int, *, hardware: HardwareConfig, cols: int, language: str):
    """The browser cockpit front, configured from HERDECK_WEB_* (then config)."""
    from .driver.web import WebDeck

    host = os.environ.get("HERDECK_WEB_BIND") or hardware.web_bind or "127.0.0.1"
    host = validate_web_bind(host)
    env_port = os.environ.get("HERDECK_WEB_PORT")
    raw_port = env_port if env_port is not None else hardware.web_port
    port = int(raw_port if raw_port is not None else 8800)
    frame_ancestors = tuple(
        value.strip()
        for value in os.environ.get("HERDECK_WEB_FRAME_ANCESTORS", "").split(",")
        if value.strip()
    )
    deck = WebDeck(
        slots,
        host=host,
        port=port,
        icons_dir=hardware.icons_dir,
        cols=cols,
        language=language,
        base_path=os.environ.get("HERDECK_WEB_BASE_PATH", ""),
        public_origin=os.environ.get("HERDECK_WEB_PUBLIC_ORIGIN", ""),
        frame_ancestors=frame_ancestors,
    )
    if deck._allow_query_token and os.environ.get("HERDECK_SHOW_URL_TOKEN") == "1":
        for url in simulator_urls(
            deck.host,
            deck.port,
            deck.press_token,
            base_path=deck._base_path,
            public_origin=deck._public_origin,
        ):
            print(f"herdeck web simulator on {url}", flush=True)
    else:
        access_hint = (
            "run 'herdeck-web url --allow-query-token' to print the legacy capability URL"
            if deck._allow_query_token
            else "authenticated browser session required"
        )
        print(
            f"herdeck web simulator listening on "
            f"http://{deck.host}:{deck.port}{deck._base_path}/ ({access_hint})",
            flush=True,
        )
    return deck


# --- fronts ------------------------------------------------------------------


class _Front:
    """What a deck front needs from the host once the runtime exists."""

    def __init__(self, kind: str, *, deck=None, d200_driver=None, d200_lock=None):
        self.kind = kind
        self.deck = deck  # WebDeck / ElgatoDriver / FakeRenderer
        self.d200_driver = d200_driver  # an already-opened D200 (auto-detect)
        self.d200_lock = d200_lock  # held while that driver is open

    def close_unattached(self) -> None:
        """Release a front whose runtime never started."""
        for resource in (self.deck, self.d200_driver):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.d200_lock is not None:
            self.d200_lock.release()


def _d200_driver(hardware: HardwareConfig):
    from .driver.d200 import D200Driver

    return D200Driver(
        brightness=hardware.brightness,
        debounce=hardware.debounce,
        keep_alive_interval=hardware.keep_alive_interval,
        icons_dir=hardware.icons_dir,
        standard_writer=hardware.d200_standard_writer,
    )


def _elgato_driver(hardware: HardwareConfig):
    from .driver.elgato import ElgatoDriver

    return ElgatoDriver(brightness=hardware.brightness, icons_dir=hardware.icons_dir)


def open_front(
    kind,
    slots: int,
    *,
    hardware: HardwareConfig | None = None,
    cols: int = 5,
    language: str = "en",
    d200_factory=None,
    elgato_factory=None,
    web_factory=None,
    lock_factory=None,
) -> _Front:
    """Open the requested deck front. ``kind`` None => auto (d200, elgato, web).

    An explicit D200 is only *described* here: the runtime's reconnecting sink
    opens it under ``d200.lock`` (and keeps retrying while another runtime owns
    it). Auto-detect probes it once, under the lock, and keeps that driver."""
    from .deckapp.device_lock import DeviceLock, d200_lock_path
    from .driver.fake import FakeRenderer

    hardware = hardware or HardwareConfig()
    d200_factory = d200_factory or (lambda: _d200_driver(hardware))
    elgato_factory = elgato_factory or (lambda: _elgato_driver(hardware))
    web_factory = web_factory or (
        lambda: build_web_deck(slots, hardware=hardware, cols=cols, language=language)
    )
    lock_factory = lock_factory or (lambda: DeviceLock(d200_lock_path()))
    if kind == "fake":
        return _Front("fake", deck=FakeRenderer(slots))
    if kind == "web":
        return _Front("web", deck=web_factory())
    if kind == "d200":
        return _Front("d200")
    if kind == "elgato":
        return _Front("elgato", deck=elgato_factory())
    if kind is not None:
        raise ValueError(f"unsupported deck kind: {kind}")
    lock = lock_factory()
    if lock.acquire():
        try:
            return _Front("d200", d200_driver=d200_factory(), d200_lock=lock)
        except Exception as exc:
            lock.release()
            print(f"No Stream Deck opened ({exc}); close any vendor app holding the device.")
    else:
        owner = lock.owner_pid()
        print(
            "No Stream Deck opened (the D200 is owned by another herdeck runtime"
            f"{f', pid {owner}' if owner else ''})."
        )
    try:
        return _Front("elgato", deck=elgato_factory())
    except Exception as exc:
        print(f"No Stream Deck opened ({exc}); close any vendor app holding the device.")
    print("Falling back to the web simulator.")
    return _Front("web", deck=web_factory())


# --- the runtime -----------------------------------------------------------------


def _pin_store() -> PinStore:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return PinStore(base / "herdeck" / "pins.json")


def make_config_reloader(config_path: str, local_path: str | None):
    """Load + resolve the on-disk config; IO/parse errors become ConfigError."""
    import tomllib

    from .settings import load_settings, resolve_profile

    def reload_() -> Config:
        # An edit in progress can leave the file partially written: surface
        # it as ConfigError and keep the current config.
        try:
            refreshed = load_settings(config_path, local_path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"could not read config: {exc}") from exc
        return resolve_profile(refreshed).config

    return reload_


class Host:
    """One DeckApp + runtime services + the front sinks, built synchronously
    (``start``) and torn down by ``close``."""

    def __init__(
        self,
        config: Config | None,
        front: _Front,
        *,
        mode: str,
        config_path: str | None = None,
        local_path: str | None = None,
        source_factory=None,
        d200_driver_factory=None,
    ):
        self.config = config
        self.front = front
        self.mode = mode
        self._config_path = config_path
        self._local_path = local_path
        self._source_factory = source_factory
        self._d200_driver_factory = d200_driver_factory
        self.app = None
        self.services = None
        self._sinks: list = []
        self._watcher = None

    # sources
    def _build_source(self, config: Config):
        if self._source_factory is not None:
            return self._source_factory(config)
        from .deckapp.live import build_live_source

        # No deck shell attaches to this host: macOS banners go out directly.
        return build_live_source(config, shell_banners=False)

    def start(self) -> None:
        from .deckapp.config_service import ConfigService
        from .deckapp.mock import MockSource
        from .deckapp.server import DeckApp
        from .deckapp.services import RuntimeServices

        if self.mode == "mock":
            source = MockSource(HardwareConfig())
        else:
            source = self._build_source(self.config)
        holder: dict = {}
        self.services = RuntimeServices(
            source.config, current_source=lambda: holder["app"]._source
        )
        self.services.wire(source)
        config_service = None
        if self.mode != "mock" and self._config_path is not None:
            config_service = ConfigService(self._config_path, self._local_path)
        try:
            self.app = DeckApp(
                source,
                # The Elgato USB deck has a fixed key layout (key_count - 2 tiles
                # + 2 panel keys) independent of the configured grid; every other
                # front follows the grid.
                slots=self.front.deck.slot_count() if self.front.kind == "elgato" else None,
                serve=False,
                run_ticker=True,
                clock=time.monotonic,
                tick_interval=source.config.hardware.tick_interval,
                config_service=config_service,
                reloader=self._reload if config_service is not None else None,
                pin_store=_pin_store(),
            )
        except Exception:
            source.close()
            self.services.close()
            raise
        holder["app"] = self.app
        self._attach_front()
        if self.mode == "remote" and self._config_path is not None:
            from .deckapp.watcher import ConfigWatcher

            paths = [p for p in (self._config_path, self._local_path) if p]
            self._watcher = ConfigWatcher(paths, self.app._watcher_reload, adopt_before_fire=False)
            self.app._watcher = self._watcher
            self._watcher.start()

    def _attach_front(self) -> None:
        from .deckapp.sinks import DriverSink, ReconnectingD200Sink

        app = self.app
        front = self.front
        if front.kind == "web":
            from .deckapp.web_terminals import WebTerminals

            deck = front.deck
            terminals = WebTerminals(
                lambda: app._source,
                tile_is_current=deck.terminal_tile_is_current,
                language=lambda: app.config.view.language,
            )
            app.add_sink(deck)  # paint first: the routes below serve a rendered deck
            deck.on_press(app.press)
            deck.on_semantic(self.services.semantic_request)
            deck.on_terminal(terminals.open, terminals.close)
        elif front.kind == "d200":
            from .deckapp.device_lock import DeviceLock, d200_lock_path

            first = [front.d200_driver]
            factory = self._d200_driver_factory or _d200_driver

            def open_driver():
                if first[0] is not None:
                    driver, first[0] = first[0], None
                    return driver
                return factory(app.config.hardware)

            sink = ReconnectingD200Sink(
                open_driver,
                on_press=app.press,
                slots=app.slots,
                device_lock=front.d200_lock or DeviceLock(d200_lock_path()),
            )
            app.add_sink(sink)
        else:
            app.add_sink(DriverSink(front.deck, on_press=app.press, slots=app.slots))

    # config reload / profile switch (DeckApp.reload -> here)
    def _reload(self) -> None:
        if self._config_path is None:
            return
        try:
            new_config = make_config_reloader(self._config_path, self._local_path)()
        except ConfigError as exc:
            from .i18n import tr

            log.warning("config reload failed: %s", exc)
            self.app.hold_status_panel(
                tr(self.app.config.view.language, "status.reload_failed"), [str(exc)[:60]]
            )
            return
        if self.mode == "local" and self.config is not None and self.config.servers:
            # keep talking to the embedded bridge: only the file settings change
            new_config = _local_config_for_server(self.config.servers[0], new_config)
        source = self._build_source(new_config)
        self.services.wire(source)
        try:
            self.app.swap_source(source)
        except Exception:
            source.close()
            self.services.wire(self.app._source)
            raise
        self.config = new_config

    def close(self) -> None:
        if self.app is not None:
            self.app.close()  # closes the sinks, the watcher and the source
        else:
            self.front.close_unattached()
        if self.services is not None:
            self.services.close()


async def _serve_forever(host: Host) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
            pass
    await stop.wait()


async def run_host(mode, file_config, front: _Front, *, config_path=None, local_path=None):
    """Resolve the runtime config (starting the embedded bridge in local mode),
    run the one runtime with ``front`` until SIGTERM/SIGINT, then tear down."""
    aclose = None
    config = None
    if mode[0] != "mock":
        try:
            config, aclose = await resolve_runtime_config(mode, file_config)
        except BaseException:
            front.close_unattached()
            raise
    host = Host(
        config,
        front,
        mode=mode[0],
        config_path=config_path,
        local_path=local_path,
    )
    try:
        # DeckApp construction renders and connects synchronously; keep the
        # loop (which serves the embedded bridge in local mode) responsive.
        await asyncio.to_thread(host.start)
        await _serve_forever(host)
    finally:
        await asyncio.to_thread(host.close)
        if aclose is not None:
            await aclose()


async def _amain_elgato(mode, file_config, socket_path, token) -> None:
    from .elgato.runtime import serve_elgato

    config, aclose = await resolve_runtime_config(mode, file_config)
    try:
        await serve_elgato(config, socket_path=socket_path, token=token)
    finally:
        await aclose()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--version"]:
        from . import __version__

        print(f"herdeck {__version__}")
        return
    if argv[:1] == ["update"]:
        from .update import main as update_main

        raise SystemExit(update_main(argv[1:]))

    if os.environ.get("HERDECK_DEBUG"):
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
    mock = bool(os.environ.get("HERDECK_MOCK"))
    config_path = None if mock else _discover_config_path()
    local_path = None
    file_config = None
    if config_path:
        from .settings import load_settings, resolve_profile

        local_path = _discover_local_config_path(config_path)
        file_config = resolve_profile(load_settings(config_path, local_path)).config
    socket_path = resolve_socket_path(file_config)
    mode = resolve_mode(
        mock=mock,
        config_path=config_path,
        config_has_servers=bool(file_config and file_config.servers),
        socket_path=socket_path,
        socket_exists=os.path.exists(socket_path),
    )
    if mode[0] == "error":
        print(mode[1], file=sys.stderr)
        sys.exit(2)

    kind = resolve_deck_kind(file_config)
    if kind == "elgato-plugin":
        # The Elgato plugin is its own IPC front-end over the core; it does NOT use
        # the grid deck, so route it before opening a deck front.
        from .elgato.runtime import discover_ipc

        sock, token = discover_ipc()
        asyncio.run(_amain_elgato(mode, file_config, sock, token))
        return
    grid = file_config.grid if file_config else (5, 3)
    front = open_front(
        kind,
        grid[0] * grid[1] - 2,
        hardware=file_config.hardware if file_config else None,
        cols=grid[0],
        language=file_config.view.language if file_config else "en",
    )
    asyncio.run(run_host(mode, file_config, front, config_path=config_path, local_path=local_path))


if __name__ == "__main__":
    main()
