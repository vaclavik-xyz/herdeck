<script lang="ts">
  import { onMount, tick } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { emit, listen } from "@tauri-apps/api/event";
  import PlugsConnected from "phosphor-svelte/lib/PlugsConnected";
  import ConfigApp from "./ConfigApp.svelte";
  import DeckView from "./lib/DeckView.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import { appSurface, desktopSetupVisible, windowRole } from "./lib/appSurface";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport } from "./lib/deckClient";
  import {
    DECK_ZOOM_EVENT,
    floatingScaleCommandFromEvent,
    shouldBypassDeckContextMenu,
  } from "./lib/deckContextMenu";
  import { fitDecision } from "./lib/windowFit";
  import {
    anchoredFloatingPosition,
    changeFloatingScale,
    FLOATING_BASE_WIDTH,
    floatingFrameSize,
    floatingScaleCommandForKey,
    floatingViewport,
    readFloatingScale,
    writeFloatingScale,
    type FloatingScaleCommand,
  } from "./lib/floatingScale";
  import {
    setupTransport,
    shouldOnboard,
    type SetupStatus,
  } from "./lib/onboardingClient";
  import { locale } from "./lib/i18n.svelte";
  import { visibilityGatedLoop } from "./lib/pollGate";
  import UpdateBanner from "./lib/UpdateBanner.svelte";
  import {
    asUpdateCheckState,
    reasonOf,
    runUpdateCheck,
    updateTransport,
    type UpdateCheckState,
  } from "./lib/updateClient";

  // Injected on <html data-window-role> by Rust BEFORE first paint
  // (initialization_script), so the borderless CSS applies with no flash of
  // opaque chrome. Falls back to "app" in a plain browser (no Tauri / no
  // attribute) — the settings surface is the one worth designing against.
  const role =
    (typeof document !== "undefined" ? windowRole(document) : undefined) ?? "app";
  const borderless = role === "deck";
  const surface = appSurface(role);

  let shell = $state<HTMLElement | undefined>(undefined);
  let desktopSetupOverlay = $state<HTMLElement | undefined>(undefined);

  let discovery = $state<Discovery | null>(null);
  let status = $state<SetupStatus | null>(null);
  // Manual "change connection" override: open the welcome card even when the
  // status would show the deck (so a demo/local-pinned user can re-onboard).
  let reonboard = $state(false);
  let desktopSetupHidden = $state(false);
  // null = no check has landed yet (nothing to render). Once a check
  // completes it is always one of the four UpdateCheckState kinds — see
  // UpdateBanner, the one place that renders this.
  let updateState = $state<UpdateCheckState | null>(null);
  // Install failures are kept separate from the check's own state: they can
  // only happen after installUpdate() is already running, on top of whatever
  // updateState says, and must outrank it in UpdateBanner regardless of which
  // check produced it.
  let updateError = $state("");
  let installingUpdate = $state(false);
  // Bumped only when an install (in EITHER window — both render the install
  // button, see the `resolvedAway` branch of the listener below) definitively
  // resolves an update away. Lets checkForUpdate tell "nothing else has
  // happened since I started" from "install already gave a real answer
  // while I was still in flight" — a check that started BEFORE that
  // resolution reports stale information once it finally settles.
  let updateGeneration = 0;
  // Every version a resolution has retracted, so a check straddling one of
  // them can tell "the same stale news install just retracted" (drop it, or
  // the user could click Install again and just get `false` again) from "a
  // genuinely different, newer update" (apply it — it's real, current
  // information no earlier resolution knows anything about). An
  // ACCUMULATING set, not a single slot: two resolutions can happen in
  // sequence (an install, then a fresh check finding something newer, then
  // ANOTHER install), and a slot would let the second overwrite — and so
  // un-protect — the first's record.
  let resolvedAwayVersions = new Set<string>();

  /** Record that `version` — the update installUpdate actually targeted,
   *  captured at ITS start, not re-derived here — has been resolved away.
   *  Shared by installUpdate's own resolution and the broadcast listener's
   *  "resolved" case (an install can happen in either window), so the two
   *  can never disagree about what was retracted.
   *
   *  `version` is `null` when installUpdate ran with no live "available"
   *  state to target (e.g. retried from the installError banner, which
   *  offers the same action regardless of `state`) — that resolution
   *  retracted NOTHING in particular, so it must not bump the generation
   *  either: a check already in flight (left showing "checking" — see the
   *  no-target fallback below) captured its `startGeneration` before this
   *  ran, and an unearned bump would mark its own eventual, perfectly good
   *  result as "superseded" with nothing to be superseded BY, stranding the
   *  placeholder forever (the 8s effect only sweeps up-to-date/failed).
   *
   *  Only CLEARS the live state when it currently shows that exact version:
   *  `updater.install()` is awaited, so a genuinely newer update can land
   *  (from a check settling, or the other window) while it was in flight —
   *  that update was never what got resolved and must survive. A real Tauri
   *  `emit` reaches its own sender, so installUpdate's direct call and this
   *  SAME window's own updateResultListener both fire for one clear — this
   *  is naturally idempotent to that since both add the SAME `version`. */
  function recordResolvedAway(version: string | null): void {
    if (version !== null) {
      updateGeneration += 1;
      resolvedAwayVersions.add(version);
    }
    if (updateState?.kind === "available" && updateState.info.version === version) {
      updateState = null;
    }
  }
  let floatingScrollable = $state(false);
  let floatingContentHeight = $state(300);
  let floatingScale = $state((() => {
    if (!borderless || typeof localStorage === "undefined") return 1;
    try {
      return readFloatingScale(localStorage);
    } catch {
      return 1;
    }
  })());
  const floatingFrame = $derived(
    floatingFrameSize(floatingContentHeight, floatingScale),
  );

  const updater = updateTransport((cmd, args) => invoke(cmd, args));

  const transport = $derived(
    discovery ? commandTransport((cmd, args) => invoke(cmd, args)) : null,
  );
  const setup = $derived(
    discovery ? setupTransport((cmd, args) => invoke(cmd, args)) : null,
  );

  const view = $derived(shouldOnboard(status, reonboard));
  const showDesktopSetup = $derived(desktopSetupVisible(surface, view, desktopSetupHidden));

  // Opening Settings during first-run intentionally hides the setup layer. Once
  // a connection succeeds, clear that one-shot override so a later disconnect
  // can surface setup again.
  $effect(() => {
    if (status != null && view === "deck" && desktopSetupHidden) desktopSetupHidden = false;
  });

  // Keep keyboard focus inside the modal surface. ConfigApp stays mounted so
  // unsaved settings survive a reconnect, but must not remain interactive.
  $effect(() => {
    if (!showDesktopSetup) return;
    const frame = requestAnimationFrame(() => {
      const overlay = desktopSetupOverlay;
      if (overlay && !overlay.contains(document.activeElement)) overlay.focus();
    });
    return () => cancelAnimationFrame(frame);
  });

  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // Not in a Tauri WebView (plain browser): leave null, DeckView goes offline.
    }
  }

  // Only the app window ever runs a check (see the mount gate below), but
  // BOTH windows need to be able to show a FOUND update — the deck's own
  // automatic check is gone, so without this it could never learn of one at
  // all. Broadcast (not emit_to) reaches every window, including the one
  // that sent it; a single shared name keeps the emit and the listen below
  // from drifting apart. Only "available" ever crosses this: "checking" /
  // "up-to-date" / "failed" are informational answers to a check the user
  // asked for in ONE window, and popping them onto the deck too would resize
  // its content-fit frame for a message it never asked about.
  const UPDATE_RESULT_EVENT = "update-check-result";

  // One check path for both callers below: the silent mount check and the
  // tray's manual one. `manual` is the only thing that differs between them —
  // it decides whether a non-available outcome (checking/up-to-date/failed)
  // is worth showing at all.
  async function checkForUpdate(manual: boolean): Promise<void> {
    // Only feeds the "checking" placeholder decision just below — NOT the
    // anti-downgrade guard further down, which must read the LIVE state at
    // resolution time instead. This snapshot can go stale while the check is
    // in flight (an install resolving, another check landing), and using it
    // there let a retracted or superseded update come back from the dead.
    const before = updateState;
    const startGeneration = updateGeneration;
    // Don't hide an already-actionable "available" banner behind a transient
    // "checking" placeholder: `update_check` is a plain invoke with no
    // timeout, so a hung re-check would otherwise strand the install button
    // gone for the rest of the process's life, which is the same silence
    // this whole feature exists to close, reached by a different path.
    if (manual && before?.kind !== "available") updateState = { kind: "checking" };
    const result = await runUpdateCheck(() => updater.check());
    // Automatic checks are best-effort: offline startup and an empty release
    // channel must never interfere with the deck, so only a found update is
    // worth surfacing. A user-requested check reports EVERY outcome,
    // including failure — the fact that the automatic one never could is
    // exactly the defect a manual check exists to fix.
    if (manual || result.kind === "available") {
      // An install (in EITHER window) resolved definitively WHILE this check
      // was in flight: a lesser (non-"available") outcome from a check that
      // started before that resolution is stale news and must not un-clear
      // it. An "available" result for that SAME version is equally stale —
      // it is exactly what was just resolved away, and re-showing it would
      // put the install button back for a click that only resolves `false`
      // again. Only a genuinely different (newer) version is real news.
      const supersededByInstall = updateGeneration !== startGeneration;
      const staleResolvedVersion =
        result.kind === "available" && resolvedAwayVersions.has(result.info.version);
      const isFreshOutcome =
        !supersededByInstall || (result.kind === "available" && !staleResolvedVersion);
      if (isFreshOutcome) {
        // A transient/failed outcome must never displace an update that is
        // STILL there and installable: a re-check finding nothing new (or
        // failing outright) does not mean the earlier one stopped being
        // real. Compare against whatever is LIVE right now (checks can
        // overlap — a mount check racing a manual one, or a double-clicked
        // tray item), not a snapshot from when this call started: "checking"
        // is never "available", so this still lets the very next resolved
        // check apply its own result normally.
        const prior = updateState;
        updateState = result.kind === "available" || prior?.kind !== "available" ? result : prior;
        // Gated on the SAME `isFreshOutcome` as the local assignment above —
        // not just `result.kind === "available"` — or a stale same-version
        // result rejected here would still reach the OTHER window (and this
        // window's own listener, since a broadcast reaches its sender too)
        // completely unfiltered, resurrecting exactly what was just rejected.
        if (result.kind === "available") {
          void emit(UPDATE_RESULT_EVENT, result).catch(() => {
            // Not in a Tauri WebView (plain browser preview): nothing to tell.
          });
        }
      }
    }
  }

  async function installUpdate(): Promise<void> {
    if (installingUpdate) return;
    installingUpdate = true;
    updateError = "";
    // Captured BEFORE `updater.install()` — an awaited download-and-verify
    // that can take a while — rather than re-read from `updateState` once it
    // resolves, which could have moved on to a genuinely different update by
    // then (see recordResolvedAway's doc comment).
    const targetVersion = updateState?.kind === "available" ? updateState.info.version : null;
    try {
      const installed = await updater.install();
      if (!installed) {
        // A definitive resolution (the updater itself found nothing to
        // install, most likely someone/something else already did), unlike
        // the transient outcomes above — it clears the banner in BOTH
        // windows, the one retraction the "available"-only broadcast above
        // otherwise has no way to make: without this, a window that never
        // installs anything (the deck) would keep an "Install and restart"
        // button for an update the process has already decided is gone.
        recordResolvedAway(targetVersion);
        // No live target (installUpdate ran from the installError retry
        // with no "available" state showing): the user asked for a retry
        // and deserves an answer, not the banner just silently vanishing —
        // show "up to date" (the 8s effect below sweeps it away same as any
        // other check's). Nothing to tell the OTHER window in this case: it
        // retracted nothing in particular for it either.
        if (targetVersion === null) {
          // A live "available" banner already IS the answer to "is there
          // something to install" — it must survive a resolution that never
          // concerned it (e.g. a genuinely newer update landed from a check
          // or the other window while this retry's install() was in
          // flight). "checking" is itself an answer-in-progress: a check the
          // user explicitly started must not be told "up to date" out from
          // under it before it even finishes — and since THIS resolution
          // retracted nothing, it doesn't bump the generation either (see
          // recordResolvedAway), so it alone can't mark that check's own
          // outcome superseded. A DIFFERENT resolution (e.g. a real
          // retraction broadcast from the other window) still can — in
          // which case that check's settle is dropped unless it finds a
          // genuinely different "available" version; for up-to-date/failed,
          // only ANOTHER manual check (which re-enters with a fresh
          // generation) clears the placeholder. Nothing sweeps "checking" on
          // a timer the way up-to-date/failed are. Only answer here when
          // NEITHER "available" nor "checking" is live.
          if (updateState?.kind !== "available" && updateState?.kind !== "checking") {
            updateState = { kind: "up-to-date" };
          }
        } else {
          void emit(UPDATE_RESULT_EVENT, { resolvedAway: targetVersion }).catch(() => {
            // Not in a Tauri WebView (plain browser preview): nothing to tell.
          });
        }
      }
    } catch (error) {
      updateError = reasonOf(error);
    } finally {
      installingUpdate = false;
    }
  }

  // "Up to date" and "failed" are informational dead ends — nothing else
  // ever clears them (unlike "available", which installUpdate can resolve),
  // so left alone they would pin the banner to the top of the window (or
  // permanently steal height from the content-fit deck) for the rest of the
  // process's life. Re-runs on every new `updateState`, so a fresh check
  // result cancels the previous timer via the effect's own cleanup and starts
  // a new one — it never clears a kind newer than the one it was scheduled for.
  $effect(() => {
    const kind = updateState?.kind;
    if (kind !== "up-to-date" && kind !== "failed") return;
    const timer = setTimeout(() => {
      if (updateState?.kind === kind) updateState = null;
    }, 8000);
    return () => clearTimeout(timer);
  });

  // installError outranks every updateState kind in UpdateBanner and is
  // otherwise cleared only by installUpdate's own entry (a retry click) —
  // ignored, it would pin a red bar for the rest of the process's life,
  // masking any later state and permanently stealing content-fit height on
  // the deck. Same 8s window as the effect above, tracked separately since
  // installError and updateState change independently of each other.
  $effect(() => {
    if (!updateError) return;
    const timer = setTimeout(() => {
      updateError = "";
    }, 8000);
    return () => clearTimeout(timer);
  });

  // Content-fit: size the borderless window to the intrinsic content height. Skips
  // redundant calls via fitDecision's anti-feedback guard. No-op (try/catch) when
  // not in a Tauri WebView.
  let lastRequestedHeight: number | null = null;
  let fitQueue = Promise.resolve();
  async function fitWindow(scrollHeight: number): Promise<void> {
    try {
      const {
        currentMonitor,
        getCurrentWindow,
        LogicalSize,
        PhysicalPosition,
      } = await import("@tauri-apps/api/window");
      const window = getCurrentWindow();
      const [position, previousSize, monitor] = await Promise.all([
        window.outerPosition(),
        window.outerSize(),
        currentMonitor(),
      ]);
      const width = Math.round(FLOATING_BASE_WIDTH * floatingScale);
      const monitorHeight = monitor
        ? Math.max(160, monitor.workArea.size.height / monitor.scaleFactor - 32)
        : Number.POSITIVE_INFINITY;
      const viewport = floatingViewport(scrollHeight, floatingScale, monitorHeight);
      floatingScrollable = viewport.scrollable;
      const d = fitDecision(
        viewport.height,
        lastRequestedHeight,
        width,
      );
      if (!d.apply) return;
      lastRequestedHeight = d.height;
      await window.setSize(new LogicalSize(d.width, d.height));
      if (monitor) {
        const nextSize = await window.outerSize();
        const nextPosition = anchoredFloatingPosition(
          position,
          previousSize,
          nextSize,
          { ...monitor.workArea.position, ...monitor.workArea.size },
        );
        if (nextPosition.x !== position.x || nextPosition.y !== position.y) {
          await window.setPosition(new PhysicalPosition(nextPosition.x, nextPosition.y));
        }
      }
    } catch {
      /* not in a Tauri WebView */
    }
  }

  function scheduleFitWindow(scrollHeight: number): Promise<void> {
    // A measurement of 0 is a WebView that has not laid out on screen, not
    // content that shrank to nothing: the deck window is created hidden and its
    // ResizeObserver attaches on mount regardless. Acting on it would record a
    // zero-height frame here and ask for a zero-height window below (fitDecision
    // refuses the second half; this is what keeps the frame geometry honest).
    if (!(scrollHeight > 0)) return Promise.resolve();
    floatingContentHeight = scrollHeight;
    const scheduled = fitQueue.then(() => fitWindow(scrollHeight));
    fitQueue = scheduled.catch(() => {});
    return scheduled;
  }

  async function applyFloatingScale(command: FloatingScaleCommand): Promise<void> {
    const next = changeFloatingScale(floatingScale, command);
    if (next === floatingScale) return;
    floatingScale = next;
    try {
      writeFloatingScale(localStorage, next);
    } catch {
      // Resizing still works when WebView storage is unavailable.
    }
    lastRequestedHeight = null;
    await tick();
    if (shell) await scheduleFitWindow(shell.scrollHeight);
  }

  async function startWindowDrag(event: PointerEvent): Promise<void> {
    if (event.button !== 0) return;
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      await getCurrentWindow().startDragging();
    } catch {
      // Plain browser previews do not expose native window dragging.
    }
  }

  onMount(() => {
    let alive = true;

    const discoveryListener = listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) discovery = d;
    });
    // Rust emits both of these to APP_WINDOW only (`app.emit_to("config", ...)`
    // in lib.rs's tray handlers), and Tauri delivers an emit_to'd event solely
    // to the targeted webview's IPC channel — so this listener is registered
    // in both windows but only ever fires in the config window, where
    // `reonboard`/`desktopSetupHidden` actually affect the rendered surface
    // (both are read only by the surface === "desktop" branch below).
    const reonboardListener = listen("reonboard", () => {
      reonboard = true;
      desktopSetupHidden = false;
    });
    const settingsListener = listen("open-settings", () => {
      reonboard = false;
      desktopSetupHidden = true;
    });
    const onFloatingScaleKey = (event: KeyboardEvent): void => {
      if (!borderless) return;
      const target = event.target;
      if (
        target instanceof HTMLElement
        && target.matches("input, textarea, select, [contenteditable='true']")
      ) return;
      const command = floatingScaleCommandForKey(event);
      if (!command) return;
      event.preventDefault();
      void applyFloatingScale(command);
    };
    window.addEventListener("keydown", onFloatingScaleKey);

    // Borderless-only: claim the deck's right-click for the native menu Rust
    // pops via `show_deck_context_menu` (see lib.rs's `build_deck_context_menu`).
    // Shift+right-click deliberately falls through to the WebView's own
    // context menu instead, so "Inspect Element" stays reachable in dev
    // builds once the plain right-click is claimed by the custom menu.
    const onDeckContextMenu = (event: MouseEvent): void => {
      if (!borderless || shouldBypassDeckContextMenu(event)) return;
      event.preventDefault();
      void invoke("show_deck_context_menu").catch(() => {
        // Not in a Tauri WebView (plain browser preview): nothing to pop.
      });
    };
    window.addEventListener("contextmenu", onDeckContextMenu);

    // Rust emits this to the DECK window only (`app.emit_to(DECK_WINDOW, ...)`
    // from the context menu's zoom items in lib.rs) — same emit_to pattern as
    // `reonboardListener`/`settingsListener` above: registered in both
    // windows, but Tauri only ever delivers it to the deck's own IPC channel.
    // The `borderless` guard is a second line of defense, not the only one —
    // every other floating-scale entry point (`onFloatingScaleKey` above,
    // the `ResizeObserver` below) carries the same guard so none of them
    // depends solely on Rust never widening the `emit_to` target.
    const zoomListener = listen(DECK_ZOOM_EVENT, (event) => {
      if (!borderless) return;
      const command = floatingScaleCommandFromEvent(event.payload);
      if (command) void applyFloatingScale(command);
    });

    // Rust emits this to the APP window only (tray's "Check for updates" item
    // — see MENU_ID_CHECK_UPDATE in lib.rs), via the same emit_to pattern as
    // `reonboardListener`/`settingsListener` above: registered in both
    // windows, but Tauri only ever delivers it to the app window's own IPC
    // channel. `show_role_window` already brought that window forward before
    // this fires, so the result is never rendered into a hidden window.
    const checkUpdateListener = listen("check-for-updates", () => {
      void checkForUpdate(true);
    });

    // The other half of `checkForUpdate`'s broadcast (an available update)
    // and `installUpdate`'s (its resolution, `null`): whichever window ran
    // the check (only the app window ever does — see below) or clicked
    // Install (either window can — both render the button), BOTH windows'
    // `updateState` follow it. Registering this in the SAME window that
    // emitted is harmless: the assignment just repeats the value that
    // function already set locally. `listen<UpdateCheckState>`'s type
    // parameter is a compile-time label only — `asUpdateCheckState` is what
    // actually guards against a malformed payload reaching `state.info` and
    // crashing the render, the same way `asDiscovery` guards `discoveryListener`.
    // A `{resolvedAway}` payload is distinguished from a malformed one: it is
    // the explicit "install resolution" signal (carrying WHICH version, so
    // this window doesn't have to guess from its own possibly-moved-on live
    // state), not something to just silently ignore — and it goes through
    // the SAME `recordResolvedAway` an install in THIS window would, so a
    // check in flight here is protected against a resolution that happened
    // in the OTHER window exactly as it would be against one of its own.
    const updateResultListener = listen<unknown>(UPDATE_RESULT_EVENT, (event) => {
      const payload = event.payload;
      if (payload !== null && typeof payload === "object" && "resolvedAway" in payload) {
        const version = (payload as { resolvedAway: unknown }).resolvedAway;
        recordResolvedAway(typeof version === "string" ? version : null);
        return;
      }
      const result = asUpdateCheckState(payload);
      if (result) updateState = result;
    });

    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
    })();
    // Both windows mount this component, but only one process should ever
    // check on startup: the app window. The deck window's own banner can
    // still DISPLAY a found update — it just never runs a second check of
    // its own, learning the result from `updateResultListener` instead.
    if (surface === "desktop") void checkForUpdate(false);

    // Visibility-gated: the setup poll parks while the window is hidden (the
    // deck lives in the tray) and refreshes immediately on show.
    const setupPoll = visibilityGatedLoop(
      async () => {
        if (setup) status = await setup.status();
      },
      () => (status ? 2500 : 600),
    );

    // Borderless content-fit: observe the shell's intrinsic height and resize the
    // window to match. rAF-batched so a burst of mutations triggers one setSize.
    let ro: ResizeObserver | undefined;
    if (borderless && shell && typeof ResizeObserver !== "undefined") {
      let scheduled = false;
      ro = new ResizeObserver(() => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
          scheduled = false;
          if (shell) void scheduleFitWindow(shell.scrollHeight);
        });
      });
      ro.observe(shell);
    }

    return () => {
      alive = false;
      void discoveryListener.then((unlisten) => unlisten());
      void reonboardListener.then((unlisten) => unlisten());
      void settingsListener.then((unlisten) => unlisten());
      void zoomListener.then((unlisten) => unlisten());
      void checkUpdateListener.then((unlisten) => unlisten());
      void updateResultListener.then((unlisten) => unlisten());
      window.removeEventListener("keydown", onFloatingScaleKey);
      window.removeEventListener("contextmenu", onDeckContextMenu);
      setupPoll.stop();
      ro?.disconnect();
    };
  });

  const connectionSetupLabel = $derived(
    locale.lang === "cs" ? "Nastavení připojení" : "Connection setup",
  );
  const connectionIntroTitle = $derived(
    locale.lang === "cs" ? "Vyber, kde agenti běží" : "Choose where agents run",
  );
  const connectionIntroBody = $derived(
    locale.lang === "cs"
      ? "Připoj lokální herdr sessions, uložený vzdálený bridge, nebo obojí. Později to můžeš změnit v Připojeních."
      : "Connect local herdr sessions, a saved remote bridge, or both. You can change this later in Connections.",
  );

  // The tray menu is native (Rust) — retitle its items whenever the language
  // the deck reports changes (DeckView feeds `locale` from /state).
  $effect(() => {
    void invoke("tray_set_language", { lang: locale.lang }).catch(() => {});
  });

  function onConnected(): void {
    reonboard = false;
    desktopSetupHidden = false;
    void (async () => {
      if (setup) status = await setup.status();
    })();
  }

  function openDesktopSettings(): void {
    reonboard = false;
    desktopSetupHidden = true;
  }
</script>

{#if surface === "desktop"}
  <div class="desktop-app">
    <div class="desktop-banner">
      <UpdateBanner
        state={updateState}
        installError={updateError}
        installing={installingUpdate}
        onInstall={installUpdate}
      />
    </div>
    <div class="desktop-control-room" inert={showDesktopSetup} aria-hidden={showDesktopSetup}>
      <ConfigApp interactive={!showDesktopSetup} />
    </div>
    {#if showDesktopSetup}
      <div
        class="desktop-setup-overlay"
        bind:this={desktopSetupOverlay}
        tabindex="-1"
      >
        <header class="desktop-topbar">
          <div class="desktop-brand" aria-label="Herdeck">
            <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
            <strong>Herdeck</strong>
          </div>
          <span>{connectionSetupLabel}</span>
        </header>
        <div class="desktop-setup-stage">
          <aside class="setup-intro">
            <span class="setup-icon" aria-hidden="true"><PlugsConnected size={28} weight="regular" /></span>
            <h2>{connectionIntroTitle}</h2>
            <p>{connectionIntroBody}</p>
          </aside>
          <Onboarding
            variant="desktop"
            {view}
            {status}
            transport={setup}
            {onConnected}
            onOpenSettings={openDesktopSettings}
            manual={reonboard}
          />
        </div>
      </div>
    {/if}
  </div>
{:else}
  <main
    class:borderless
    class:desktop={surface === "desktop"}
    class:scrollable={floatingScrollable}
    style:--floating-scale={String(floatingScale)}
    style:--floating-frame-width={`${floatingFrame.width}px`}
    style:--floating-frame-height={`${floatingFrame.height}px`}
  >
    <div class="scale-frame">
      <div class="shell" bind:this={shell}>
    {#if borderless}
      <div
        class="drag"
        role="presentation"
        data-tauri-drag-region
        onpointerdown={startWindowDrag}
      >
        <span class="grabber" data-tauri-drag-region></span>
      </div>
    {/if}
    <UpdateBanner
      state={updateState}
      installError={updateError}
      installing={installingUpdate}
      onInstall={installUpdate}
    />
    {#if view === "deck"}
      <DeckView {transport} compact />
    {:else}
      <Onboarding
        variant="compact"
        {view}
        {status}
        transport={setup}
        {onConnected}
        onDismiss={reonboard ? () => (reonboard = false) : undefined}
        manual={reonboard}
      />
    {/if}
      </div>
    </div>
  </main>
{/if}

<style>
  /* Opaque by default (the app window + a plain browser); the deck role makes
     the window transparent so the rounded .shell is the only painted surface. */
  :global(html, body) {
    margin: 0;
    background: var(--canvas);
    color-scheme: dark; /* dark native widgets + scrollbars (WebKit) */
  }
  :global(html[data-window-role="deck"]),
  :global(html[data-window-role="deck"] body) {
    background: transparent;
  }

  main {
    position: relative;
    width: 100vw;
    box-sizing: border-box;
  }
  .desktop-app {
    position: relative;
    min-height: 100vh;
    background: var(--canvas);
  }
  .desktop-setup-overlay {
    position: fixed;
    inset: 0;
    z-index: 10;
    overflow: auto;
    background: var(--canvas);
  }
  .desktop-setup-overlay:focus {
    outline: none;
  }
  .desktop-banner {
    position: fixed;
    top: 12px;
    left: 50%;
    z-index: 20;
    width: min(560px, calc(100vw - 32px));
    transform: translateX(-50%);
  }
  .shell {
    background: var(--canvas);
  }
  main.desktop {
    min-height: 100vh;
    background: var(--canvas);
  }
  main.desktop .shell {
    min-height: 100vh;
  }
  .desktop-topbar {
    height: 54px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 22px;
    border-bottom: 1px solid var(--line);
    color: var(--text-dim);
    font: 11px/1.2 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  }
  .desktop-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text);
    font-size: 13px;
  }
  .brand-mark {
    display: grid;
    grid-template-columns: repeat(2, 4px);
    gap: 2px;
  }
  .brand-mark i {
    width: 4px;
    height: 4px;
    border-radius: 1px;
    background: var(--accent);
  }
  .desktop-setup-stage {
    display: grid;
    grid-template-columns: minmax(230px, 0.72fr) minmax(440px, 1.28fr);
    width: min(920px, calc(100vw - 64px));
    min-height: 470px;
    margin: 56px auto;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--sidebar);
    box-shadow: 0 24px 70px color-mix(in srgb, var(--canvas) 70%, transparent);
  }
  .setup-intro {
    padding: 38px 32px;
    border-right: 1px solid var(--line);
    background: var(--panel);
    color: var(--text);
    font: 13px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  }
  .setup-icon {
    display: grid;
    width: 48px;
    height: 48px;
    margin-bottom: 62px;
    place-items: center;
    border: 1px solid var(--line-strong);
    border-radius: 12px;
    background: var(--panel-raised);
    color: var(--accent-strong);
  }
  .setup-intro h2 {
    margin: 0 0 10px;
    max-width: 12ch;
    font-size: 25px;
    line-height: 1.08;
    letter-spacing: -.035em;
  }
  .setup-intro p {
    margin: 0;
    max-width: 31ch;
    color: var(--text-dim);
  }
  /* Rounded opaque card flush to the (transparent) window edge, so macOS derives
     the WINDOW shadow from this silhouette. Deliberately no CSS drop shadow: the
     card fills the window exactly, so an outer box-shadow has nowhere to go but
     the four rounded-corner notches, where it renders as a grey square corner.
     PLATFORM NOTE: Tauri's window `shadow()` is macOS/Windows only, so the Linux
     packages get no drop shadow here — only the 1px inset ring. Giving the card
     a shadow on every platform means insetting it from the window edge (window =
     card + blur padding) and teaching the content-fit sizing about that padding. */
  main.borderless .shell {
    width: 328px;
    position: absolute;
    inset: 0 auto auto 0;
    border-radius: 14px;
    background: var(--canvas);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--text-faint) 60%, transparent);
    transform: scale(var(--floating-scale));
    transform-origin: top left;
    overflow: hidden;
  }
  main.borderless .scale-frame {
    position: relative;
    width: var(--floating-frame-width);
    height: var(--floating-frame-height);
  }
  main.borderless {
    height: 100vh;
    overflow: hidden;
  }
  main.borderless.scrollable {
    overflow: auto;
    scrollbar-width: none;
  }
  main.borderless.scrollable::-webkit-scrollbar {
    display: none;
  }
  /* The drag strip is the primary way to move the borderless window: keep the
     target generous and the grabber visible without adding desktop chrome. */
  .drag {
    height: 24px;
    width: 100%;
    padding: 0;
    border: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--canvas);
    cursor: grab;
    user-select: none;
    -webkit-user-select: none;
  }
  .drag:active {
    cursor: grabbing;
  }
  .grabber {
    width: 32px;
    height: 3px;
    border-radius: 2px;
    background: var(--line-strong);
    transition: background 0.15s;
  }
  .drag:hover .grabber {
    background: var(--text-dim);
  }
  @media (max-width: 760px) {
    .desktop-setup-stage {
      grid-template-columns: 1fr;
      width: min(560px, calc(100vw - 28px));
      margin: 24px auto;
    }
    .setup-intro {
      padding: 22px 24px;
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
    .setup-icon {
      margin-bottom: 24px;
    }
  }
</style>
