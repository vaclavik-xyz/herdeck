<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import Banner from "./lib/Banner.svelte";
  import DeckView from "./lib/DeckView.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport } from "./lib/deckClient";
  import { fitDecision } from "./lib/windowFit";
  import {
    setupTransport,
    shouldOnboard,
    type SetupStatus,
  } from "./lib/onboardingClient";
  import { locale } from "./lib/i18n.svelte";
  import { visibilityGatedLoop } from "./lib/pollGate";
  import { updateTransport, type UpdateInfo } from "./lib/updateClient";

  // Window mode is injected on <html data-window-mode> by Rust BEFORE first paint
  // (initialization_script), so the borderless CSS applies with no FOUC. Falls
  // back to "normal" in a plain browser (no Tauri / no attribute).
  const windowMode =
    (typeof document !== "undefined"
      ? document.documentElement.dataset.windowMode
      : undefined) ?? "normal";
  const borderless = windowMode !== "normal";

  // Borderless width matches the Rust builder inner_size width; the window is
  // non-resizable, so width is constant and only the height is fit to content.
  const BORDERLESS_WIDTH = 360;

  let shell = $state<HTMLElement | undefined>(undefined);

  let discovery = $state<Discovery | null>(null);
  let status = $state<SetupStatus | null>(null);
  // Manual "change connection" override: open the welcome card even when the
  // status would show the deck (so a demo/local-pinned user can re-onboard).
  let reonboard = $state(false);
  let availableUpdate = $state<UpdateInfo | null>(null);
  let updateError = $state("");
  let installingUpdate = $state(false);

  const updater = updateTransport((cmd, args) => invoke(cmd, args));

  const transport = $derived(
    discovery ? commandTransport((cmd, args) => invoke(cmd, args)) : null,
  );
  const setup = $derived(
    discovery ? setupTransport((cmd, args) => invoke(cmd, args)) : null,
  );

  const view = $derived(shouldOnboard(status, reonboard));

  async function pullDiscovery(): Promise<void> {
    try {
      const d = asDiscovery(await invoke("get_discovery"));
      if (d) discovery = d;
    } catch {
      // Not in a Tauri WebView (plain browser): leave null, DeckView goes offline.
    }
  }

  async function checkForUpdate(): Promise<void> {
    try {
      availableUpdate = await updater.check();
    } catch {
      // Automatic checks are best-effort: offline startup and an empty release
      // channel must never interfere with the deck.
    }
  }

  async function installUpdate(): Promise<void> {
    if (installingUpdate) return;
    installingUpdate = true;
    updateError = "";
    try {
      const installed = await updater.install();
      if (!installed) availableUpdate = null;
    } catch (error) {
      updateError = error instanceof Error ? error.message : String(error);
    } finally {
      installingUpdate = false;
    }
  }

  // Content-fit: size the borderless window to the intrinsic content height. Skips
  // redundant calls via fitDecision's anti-feedback guard. No-op (try/catch) when
  // not in a Tauri WebView.
  let lastRequestedHeight: number | null = null;
  async function fitWindow(scrollHeight: number): Promise<void> {
    const d = fitDecision(scrollHeight, lastRequestedHeight, BORDERLESS_WIDTH);
    if (!d.apply) return;
    lastRequestedHeight = d.height;
    try {
      const { getCurrentWindow, LogicalSize } = await import("@tauri-apps/api/window");
      await getCurrentWindow().setSize(new LogicalSize(d.width, d.height));
    } catch {
      /* not in a Tauri WebView */
    }
  }

  onMount(() => {
    let alive = true;

    void listen<Discovery>("discovery", (event) => {
      const d = asDiscovery(event.payload);
      if (d) discovery = d;
    });
    void listen("reonboard", () => {
      reonboard = true;
    });

    void (async () => {
      while (alive && !discovery) {
        await pullDiscovery();
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
    })();
    void checkForUpdate();

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
          if (shell) void fitWindow(shell.scrollHeight);
        });
      });
      ro.observe(shell);
    }

    return () => {
      alive = false;
      setupPoll.stop();
      ro?.disconnect();
    };
  });

  const changeConnectionTitle = $derived(
    locale.lang === "cs" ? "Změnit připojení" : "Change connection",
  );
  const updateMessage = $derived(
    availableUpdate
      ? locale.lang === "cs"
        ? `Je dostupný Herdeck ${availableUpdate.version}.`
        : `Herdeck ${availableUpdate.version} is available.`
      : "",
  );
  const updateAction = $derived(
    installingUpdate
      ? locale.lang === "cs"
        ? "Instaluji…"
        : "Installing…"
      : locale.lang === "cs"
        ? "Nainstalovat a restartovat"
        : "Install and restart",
  );

  // The tray menu is native (Rust) — retitle its items whenever the language
  // the deck reports changes (DeckView feeds `locale` from /state).
  $effect(() => {
    void invoke("tray_set_language", { lang: locale.lang }).catch(() => {});
  });

  function onConnected(): void {
    reonboard = false;
    void (async () => {
      if (setup) status = await setup.status();
    })();
  }
</script>

<main class:borderless>
  <div class="shell" bind:this={shell}>
    {#if borderless}
      <div class="drag" data-tauri-drag-region>
        <span class="grabber" data-tauri-drag-region></span>
      </div>
    {/if}
    {#if updateError}
      <Banner kind="error" message={updateError} />
    {:else if availableUpdate}
      <Banner
        kind="warning"
        message={updateMessage}
        actionLabel={updateAction}
        onAction={installUpdate}
      />
    {/if}
    {#if view === "deck"}
      <DeckView {transport} />
      <!-- Re-onboarding affordance, in document flow so content-fit measures it
           and overflow:hidden never clips it. -->
      <div class="tools">
        <button
          class="reonboard"
          title={changeConnectionTitle}
          aria-label={changeConnectionTitle}
          onclick={() => (reonboard = true)}>⚙</button
        >
      </div>
    {:else}
      <Onboarding
        {view}
        {status}
        transport={setup}
        {onConnected}
        onDismiss={reonboard ? () => (reonboard = false) : undefined}
      />
    {/if}
  </div>
</main>

<style>
  /* Opaque by default (normal + plain browser); borderless makes the window
     transparent so the rounded .shell is the only painted surface. */
  :global(html, body) {
    margin: 0;
    background: #0b0b0d;
    color-scheme: dark; /* dark native widgets + scrollbars (WebKit) */
  }
  :global(html[data-window-mode="floating"]),
  :global(html[data-window-mode="floating"] body),
  :global(html[data-window-mode="always_on_top"]),
  :global(html[data-window-mode="always_on_top"] body) {
    background: transparent;
  }

  main {
    position: relative;
    width: 100vw;
    box-sizing: border-box;
  }
  .shell {
    background: #0b0b0d;
  }
  /* Rounded opaque card flush to the (transparent) window edge so the drop shadow
     traces the card silhouette. */
  main.borderless .shell {
    border-radius: 12px;
    overflow: hidden;
  }
  /* The drag strip is the ONLY way to move the borderless window — give it a
     visible grabber pill instead of an 18px invisible blind target. */
  .drag {
    height: 18px;
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: grab;
  }
  .drag:active {
    cursor: grabbing;
  }
  .grabber {
    width: 36px;
    height: 4px;
    border-radius: 2px;
    background: #2a2a2e;
    transition: background 0.15s;
  }
  .drag:hover .grabber {
    background: #4a4a52;
  }
  .tools {
    display: flex;
    justify-content: flex-end;
    padding: 2px 6px 6px;
  }
  .reonboard {
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: #17171b;
    color: #8b97a4;
    font-size: 12px;
    line-height: 22px;
    cursor: pointer;
    opacity: 0.55;
  }
  .reonboard:hover {
    opacity: 1;
  }
</style>
