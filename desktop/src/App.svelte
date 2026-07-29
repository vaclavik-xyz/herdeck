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
  import { asUpdateCheckState, reasonOf, runUpdateCheck, updateTransport } from "./lib/updateClient";
  import {
    applyAvailableBroadcast,
    applyCheckResult,
    applyResolvedAway,
    beginCheck,
    initialUpdateState,
    isDismissableNotice,
    type UpdateState,
  } from "./lib/updateState";

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
  // Sticky `availableUpdate` + transient `notice`, reconciled by pure
  // functions in updateState.ts — see that file for the full reasoning. A
  // stale in-flight check can never erase a live update because the two are
  // different fields; `updateState.checkSeq` (a monotonic epoch) is the only
  // protection needed against overlapping checks or a check racing an
  // install resolution.
  let updateState = $state<UpdateState>(initialUpdateState());
  // Install failures are kept separate from the check's own state: they can
  // only happen after installUpdate() is already running, on top of whatever
  // updateState says, and must outrank it in UpdateBanner regardless of which
  // check produced it.
  let updateError = $state("");
  let installingUpdate = $state(false);
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
  // tray's manual one. `manual` is the only thing that differs between them
  // — see updateState.ts's beginCheck/applyCheckResult for the reasoning
  // (a manual check always shows "checking" and reports every outcome; an
  // automatic one stays silent unless it finds an update).
  async function checkForUpdate(manual: boolean): Promise<void> {
    const { state, seq } = beginCheck(updateState, manual);
    updateState = state;
    const result = await runUpdateCheck(() => updater.check());
    // Dropped outright if a LATER check or an install resolution has already
    // moved the epoch on since this one started — no comparison needed, and
    // no way for a stale result to un-clear or resurrect anything.
    if (seq !== updateState.checkSeq) return;
    updateState = applyCheckResult(updateState, seq, manual, result);
    if (result.kind === "available") {
      void emit(UPDATE_RESULT_EVENT, result).catch(() => {
        // Not in a Tauri WebView (plain browser preview): nothing to tell.
      });
    }
  }

  async function installUpdate(): Promise<void> {
    if (installingUpdate) return;
    installingUpdate = true;
    updateError = "";
    try {
      const installed = await updater.install();
      if (!installed) {
        // A definitive resolution — the updater's own re-check found the
        // release gone or already applied (a SUCCESSFUL install never
        // reaches here: it calls app.request_restart() Rust-side and the
        // process ends, so no race with a successful install can exist).
        // Clears the banner in BOTH windows via the broadcast below, the one
        // retraction the "available"-only broadcast otherwise has no way to
        // make: without this, a window that never installs anything (the
        // deck) would keep an "Install and restart" button for an update
        // the process has already decided is gone. Runs the same whether or
        // not there was a live target (installError's retry action offers
        // this regardless of state) — applyResolvedAway answers either way.
        updateState = applyResolvedAway(updateState);
        void emit(UPDATE_RESULT_EVENT, null).catch(() => {
          // Not in a Tauri WebView (plain browser preview): nothing to tell.
        });
      }
    } catch (error) {
      updateError = reasonOf(error);
    } finally {
      installingUpdate = false;
    }
  }

  // "Up to date" and "failed" notices are dead ends — nothing else ever
  // clears them (unlike the sticky `availableUpdate`, which only
  // installUpdate resolves), so left alone they would pin the banner to the
  // top of the window (or permanently steal height from the content-fit
  // deck) for the rest of the process's life. "checking" is NOT swept by
  // this timer: it always resolves on its own once the check it stands for
  // actually settles (or is overwritten by applyResolvedAway if a
  // resolution lands first — see updateState.ts). Re-runs on every new
  // notice, so a fresh one cancels the previous timer via the effect's own
  // cleanup and starts a new one.
  $effect(() => {
    if (!isDismissableNotice(updateState.notice)) return;
    const dismissed = updateState.notice;
    const timer = setTimeout(() => {
      if (updateState.notice === dismissed) updateState = { ...updateState, notice: null };
    }, 8000);
    return () => clearTimeout(timer);
  });

  // installError outranks both `notice` and `availableUpdate` in UpdateBanner
  // and is otherwise cleared only by installUpdate's own entry (a retry
  // click) — ignored, it would pin a red bar for the rest of the process's
  // life, masking any later state and permanently stealing content-fit
  // height on the deck. Same 8s window as the effect above, tracked
  // separately since installError changes independently of updateState.
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

    // The other half of `checkForUpdate`'s broadcast (a found "available"
    // update) and `installUpdate`'s (its resolution, a `null` payload):
    // whichever window ran the check (only the app window ever does — see
    // below) or clicked Install (either window can — both render the
    // button), BOTH windows' `updateState` follow it. Registering this in
    // the SAME window that emitted is harmless: `applyAvailableBroadcast`
    // and `applyResolvedAway` are both safe to apply twice for one event.
    // `listen<unknown>`'s type parameter is a compile-time label only —
    // `asUpdateCheckState` is what actually guards against a malformed
    // payload reaching `state.info` and crashing the render, the same way
    // `asDiscovery` guards `discoveryListener`. `null` is distinguished from
    // a malformed payload: it is the explicit "resolved" signal, not
    // something to just silently ignore.
    const updateResultListener = listen<unknown>(UPDATE_RESULT_EVENT, (event) => {
      if (event.payload === null) {
        updateState = applyResolvedAway(updateState);
        return;
      }
      const result = asUpdateCheckState(event.payload);
      if (result?.kind === "available") {
        updateState = applyAvailableBroadcast(updateState, result.info);
      }
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
        availableUpdate={updateState.availableUpdate}
        notice={updateState.notice}
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
      availableUpdate={updateState.availableUpdate}
      notice={updateState.notice}
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
