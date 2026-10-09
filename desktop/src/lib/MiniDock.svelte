<script lang="ts">
  // The mini dock: the floating window's default content ([desktop].floating
  // = "dock"). A narrow strip of the agents that are not idle, idle ones
  // folded into "+N", one card on hover (after an intent delay), keyboard
  // focus or click (click pins it until Esc / a click elsewhere). Data:
  // /agent/list long-polled via the Rust `agent_call` proxy (dockClient.ts);
  // actions: the agent card routes (agentCardClient.ts). While a card is open
  // the window grows beside the strip through Rust's `dock_layout`, which
  // keeps the strip where it is on screen. What the strip shows follows
  // `[desktop].dock_show` / `dock_group` (dockStrip.ts): a project's agents
  // can fold into one group item that expands inline on hover or focus.
  // Under a divider at the bottom, one usage ring per provider with data
  // (GET /usage, polled every minute and on show); hover/focus opens the
  // usage card through the same placement path as an agent card.
  // Keyboard answer flow (dockKeys.ts): the next-blocked hotkey focuses the
  // window and calls `openNextBlocked()`; an open card then takes 1–9 / Tab /
  // T / O / Esc, and the focus goes back to the previous app (`releaseFocus`)
  // after an answer, Esc or Open in herdr.
  // Edge snap + autohide (dockAutohide.ts): Rust snaps a dropped strip flush
  // to a near work-area edge and reports the edge in every placement; with
  // `dock_autohide` on and the strip snapped, it slides out after the pointer
  // left and Rust shrinks the window to a sliver showing the most urgent
  // status, until the pointer rests on it. Never while a card or group is
  // open, the dock has keyboard focus or any agent is blocked.
  import { tick, untrack, type Snippet } from "svelte";
  import DockItem from "./DockItem.svelte";
  import DockCard from "./DockCard.svelte";
  import DockGroup from "./DockGroup.svelte";
  import DockUsageRing from "./DockUsageRing.svelte";
  import DockUsageCard from "./DockUsageCard.svelte";
  import {
    USAGE_CARD_MAX_HEIGHT,
    USAGE_CARD_WIDTH,
    USAGE_POLL_MS,
    usageRings,
    type ProviderUsage,
  } from "./dockUsage";
  import { visibilityGatedLoop } from "./pollGate";
  import {
    DOCK_POLL_MS,
    DOCK_RETRY_MS,
    dockNextDelay,
    type DockAgent,
    type DockList,
    type DockTransport,
  } from "./dockClient";
  import type { AgentTransport } from "./agentCardClient";
  import {
    agentKey,
    CARD_GAP,
    CARD_MAX_HEIGHT,
    CARD_WIDTH,
    HOVER_CLOSE_MS,
    HOVER_OPEN_MS,
    liveSince,
    neutralReason,
    STRIP_WIDTH,
    type DockPlacement,
    type WindowLayoutFn,
  } from "./dockModel";
  import { DOCK_MESSAGES } from "./dockMessages";
  import {
    DEFAULT_DOCK_PREFS,
    GROUP_COLLAPSE_MS,
    stripLayoutDelay,
    stripModel,
    type DockPrefs,
    type MemberHandlers,
  } from "./dockStrip";
  import {
    AUTOHIDE_SLIDE_MS,
    SLIVER_WIDTH,
    createAutohide,
    sliverStatus,
    type AutohidePhase,
  } from "./dockAutohide";
  import { blockedOrder, cycleBlocked, dockKeyAction, isTypingTarget } from "./dockKeys";
  import { fmt, locale, setLang } from "./i18n.svelte";

  type Target =
    | { kind: "agent"; key: string }
    | { kind: "idle" }
    | { kind: "usage"; provider: string }
    | { kind: "toast" }; // "Nobody is waiting" after the next-blocked hotkey

  const TOAST_WIDTH = 180;
  const TOAST_HEIGHT = 44;

  function prefersReducedMotion(): boolean {
    try {
      return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  }

  let {
    transport,
    agentTransport,
    windowLayout,
    usageSource = null,
    scale = 1,
    prefs = DEFAULT_DOCK_PREFS,
    startDrag = () => {},
    onOpenSettings = () => {},
    reducedMotion = prefersReducedMotion(),
    hoverOpenMs = HOVER_OPEN_MS,
    hoverCloseMs = HOVER_CLOSE_MS,
    groupCollapseMs = GROUP_COLLAPSE_MS,
    releaseFocus = () => {},
    toastMs = 1800,
    herdrSettleMs = 700,
    children,
  }: {
    transport: DockTransport | null;
    agentTransport: AgentTransport | null;
    windowLayout: WindowLayoutFn;
    /** GET /usage (dockUsage.fetchUsage); null = no footer. A poll that
     *  answers null (failed) keeps the last data; only a successful answer
     *  without providers clears the footer. */
    usageSource?: (() => Promise<ProviderUsage[] | null>) | null;
    /** Zoom (the context menu's Zoom items). Everything inside lays out in
     *  unzoomed CSS px; only the window size and the placement Rust returns
     *  (window px) cross the zoom, so they are scaled at that boundary. */
    scale?: number;
    /** `[desktop]` dock keys (App re-reads them live after Apply). */
    prefs?: DockPrefs;
    /** An expanded group folds back this long after pointer and focus left. */
    groupCollapseMs?: number;
    startDrag?: (event: PointerEvent) => void;
    onOpenSettings?: () => void;
    reducedMotion?: boolean;
    hoverOpenMs?: number;
    hoverCloseMs?: number;
    /** Rust `dock_release_focus`: hand the focus back to the app that was
     *  frontmost before the next-blocked hotkey (a no-op when none is kept). */
    releaseFocus?: () => void;
    /** How long "Nobody is waiting" shows. */
    toastMs?: number;
    /** After Open in herdr, wait this long before releasing the focus, so a
     *  local herdr activating its terminal wins (Rust then sees we are no
     *  longer active and leaves it). */
    herdrSettleMs?: number;
    /** Overlay content positioned against the strip (App: the status dot). */
    children?: Snippet;
  } = $props();

  const m = $derived(DOCK_MESSAGES[locale.lang]);

  let list = $state<DockList | null>(null);
  let stale = $state(false);
  let receivedAt = $state(Date.now());
  let now = $state(Date.now());
  let open = $state<Target | null>(null);
  let pinned = $state(false);
  let revealed = $state(false);
  let anchorTop = $state(0);
  let placement = $state<DockPlacement>({ side: "right", stripX: 0, stripY: 0 });
  let stripHeight = $state(0);
  let sentKey = $state<string | null>(null);
  let stripEl = $state<HTMLElement | undefined>(undefined);
  let cardEl = $state<HTMLElement | undefined>(undefined);
  let cardRef = $state<ReturnType<typeof DockCard> | undefined>(undefined);
  // The open card came from the next-blocked hotkey: key legend + Tab cycling.
  let keyboard = $state(false);
  // The hotkey arrived before the first /agent/list: run it once one lands.
  let pendingNext = false;
  // The next-blocked hotkey moved the focus here and it has not been handed
  // back yet: whatever closes the card next (Esc, an answer, the agent
  // leaving, a blur) ends the session and releases the focus exactly once.
  let session = false;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  let settleTimer: ReturnType<typeof setTimeout> | undefined;
  // Read only inside timers, never rendered: plain flags.
  let hoverCard = false;
  let hoverItem = false;
  let hoverKey: string | null = null; // the agent item under the pointer, if any

  const strip = $derived(stripModel(list?.agents ?? [], prefs));
  const folded = $derived(strip.folded);
  const foldTitle = $derived(
    fmt(strip.foldedIdleOnly ? m.idle_title : m.more_title, { n: folded.length }),
  );
  const foldHeading = $derived(
    fmt(strip.foldedIdleOnly ? m.idle_heading : m.more_heading, { n: folded.length }),
  );
  // The expanded project group, if any (one at a time).
  let expandedGroup = $state<string | null>(null);
  const expanded = $derived(
    strip.items.find((it) => it.kind === "group" && it.key === expandedGroup) ?? null,
  );
  // Keys of the agent items a pointer can be on: standalone items plus the
  // members of the expanded group.
  const shownKeys = $derived.by(() => {
    const keys = new Set<string>();
    for (const it of strip.items) {
      if (it.kind === "agent") keys.add(it.key);
      else if (it.key === expandedGroup) for (const a of it.members) keys.add(agentKey(a));
    }
    return keys;
  });
  const neutral = $derived(neutralReason(list));
  const neutralTitle = $derived(
    neutral === "empty"
      ? m.neutral_empty
      : neutral === "disconnected"
        ? m.neutral_disconnected
        : m.neutral_loading,
  );
  const openAgent = $derived(findAgent(open, list));

  let usage = $state<ProviderUsage[]>([]);
  const rings = $derived(usageRings(usage));
  const openProvider = $derived(open?.kind === "usage" ? open.provider : null);
  const openUsage = $derived(
    openProvider === null ? null : (usage.find((p) => p.provider === openProvider) ?? null),
  );
  // Rust's placement is in window px; the zoomed content lays out in px / scale.
  const stripX = $derived(placement.stripX / scale);
  const stripY = $derived(placement.stripY / scale);
  const cardWidth = $derived(
    open?.kind === "usage" ? USAGE_CARD_WIDTH : open?.kind === "toast" ? TOAST_WIDTH : CARD_WIDTH,
  );
  const cardLeft = $derived(
    placement.side === "right"
      ? stripX + STRIP_WIDTH + CARD_GAP
      : stripX - CARD_GAP - cardWidth,
  );

  const openKey = $derived(open?.kind === "agent" ? open.key : null);

  // --- edge snap + autohide ---------------------------------------------------
  // The edge Rust reports the strip snapped to (every placement carries it).
  let edge = $state<"left" | "right" | null>(null);
  let ahPhase = $state<AutohidePhase>("shown");
  // The strip is translated out past its edge (sliding out, or hidden).
  let slidOut = $state(false);
  // The window was last laid out as the sliver.
  // (Rendered: the sliver bar shows only once the window IS the sliver, so a
  // right-edge bar never flashes inside the still full-width window.)
  let sliverApplied = $state(false);
  let focused = $state(typeof document !== "undefined" && document.hasFocus());
  const anyBlocked = $derived(list?.agents.some((a) => a.status === "blocked") ?? false);
  const sliverTone = $derived(sliverStatus(list?.agents ?? []) ?? "none");
  const autohide = createAutohide({
    slideMs: () => (reducedMotion ? 0 : AUTOHIDE_SLIDE_MS),
    onPhase: (p) => untrack(() => onAutohidePhase(p)),
  });

  $effect(() => {
    autohide.update({
      enabled: prefs.autohide && edge !== null,
      held: open !== null || expandedGroup !== null || focused || anyBlocked,
    });
  });

  function onAutohidePhase(p: AutohidePhase): void {
    ahPhase = p;
    if (p === "hiding") {
      slidOut = true; // CSS slides the strip past the edge…
    } else if (p === "hidden") {
      void applyLayout(open !== null); // …then the window shrinks to the sliver
    } else {
      void slideIn();
    }
  }

  /** Grow the window back first (the strip still translated out of it), then
   *  let CSS slide the strip in. */
  async function slideIn(): Promise<void> {
    if (sliverApplied) {
      await applyLayout(open !== null);
      await nextFrame();
    }
    if (ahPhase === "shown") slidOut = false;
  }

  function nextFrame(): Promise<void> {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
      else setTimeout(resolve, 16);
    });
  }

  /** A drag ended (Rust `dock-moved`): lay out again to learn the edge. */
  export function relayout(): void {
    void applyLayout(open !== null);
  }

  // A group's members act like standalone items.
  const memberHandlers: MemberHandlers = {
    enter: (a, top) => enter({ kind: "agent", key: agentKey(a) }, top),
    leave: () => leave(),
    focus: (a, top) => focusItem({ kind: "agent", key: agentKey(a) }, top),
    blur: () => blurItem(),
    click: (a, top) => clickItem({ kind: "agent", key: agentKey(a) }, top),
  };

  function holdsOpenCard(members: DockAgent[]): boolean {
    const key = openKey;
    return key !== null && members.some((a) => agentKey(a) === key);
  }

  function findAgent(target: Target | null, current: DockList | null): DockAgent | null {
    if (target === null || target.kind !== "agent" || current === null) return null;
    return current.agents.find((a) => agentKey(a) === target.key) ?? null;
  }

  function iconFor(a: DockAgent): string | null {
    return transport ? transport.markUrl(a, list?.iconFallback ?? "monogram") : null;
  }

  // --- data: /agent/list, long-polled while the window is visible ---------
  let version: number | null = null;
  let nextDelay = 0;

  async function step(): Promise<void> {
    const t = transport;
    if (!t) {
      nextDelay = DOCK_RETRY_MS;
      return;
    }
    const longPolled = version !== null;
    const started = Date.now();
    const got = await t.list(version, DOCK_POLL_MS);
    const elapsedMs = Date.now() - started;
    if (got === null) {
      // Keep the last list (dimmed); retry on the slow cadence.
      stale = true;
      nextDelay = dockNextDelay({ failed: true, longPolled, changed: false, elapsedMs });
      return;
    }
    const changed = got.version !== version;
    stale = false;
    if (changed || list === null) {
      list = got;
      receivedAt = Date.now();
      setLang(got.language);
    }
    version = got.version;
    nextDelay = dockNextDelay({ failed: false, longPolled, changed, elapsedMs });
  }

  $effect(() => {
    if (!transport) return;
    return untrack(() => {
      version = null;
      const loop = visibilityGatedLoop(step, () => nextDelay);
      return () => loop.stop();
    });
  });

  // --- usage: GET /usage every minute while visible, and on show ----------
  $effect(() => {
    const source = usageSource;
    if (!source) {
      usage = [];
      return;
    }
    return untrack(() => {
      let alive = true;
      const loop = visibilityGatedLoop(
        async () => {
          const next = await source();
          if (alive && next !== null) usage = next;
        },
        () => USAGE_POLL_MS,
      );
      return () => {
        alive = false;
        loop.stop();
      };
    });
  });

  // Elapsed times tick only while a card shows them.
  $effect(() => {
    if (open === null) return;
    now = Date.now();
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  // --- window size: Rust dock_layout keeps the strip still on screen --------
  let layoutSeq = 0;
  // The strip height the window was last fitted to (unzoomed CSS px).
  let fittedStripHeight = 0;
  let shrinkTimer: ReturnType<typeof setTimeout> | undefined;

  async function applyLayout(withCard: boolean): Promise<void> {
    const seq = ++layoutSeq;
    const kind = open?.kind;
    const width = kind === "usage" ? USAGE_CARD_WIDTH : kind === "toast" ? TOAST_WIDTH : CARD_WIDTH;
    const maxHeight =
      kind === "usage" ? USAGE_CARD_MAX_HEIGHT : kind === "toast" ? TOAST_HEIGHT : CARD_MAX_HEIGHT;
    const sliver = ahPhase === "hidden" && !withCard;
    let next: DockPlacement;
    try {
      next = await windowLayout({
        stripWidth: STRIP_WIDTH * scale,
        stripHeight: Math.max(fittedStripHeight, STRIP_WIDTH) * scale,
        cardWidth: withCard ? width * scale : 0,
        cardHeight: withCard ? (anchorTop + maxHeight) * scale : 0,
        sliverWidth: sliver ? SLIVER_WIDTH * scale : 0,
      });
    } catch {
      next = { side: "right", stripX: 0, stripY: 0 }; // plain browser preview
    }
    if (seq !== layoutSeq) return;
    // The autohide state moved on while Rust answered (e.g. a sliver request
    // in flight when a blocked agent, focus or the pref brought the strip
    // back): this reply describes a window we no longer want. Ignore it and
    // ask for the current one, or the window could stay a sliver while the
    // strip is meant to be out.
    if (sliver !== (ahPhase === "hidden" && open === null)) {
      void applyLayout(open !== null);
      return;
    }
    placement = next;
    edge = next.edge ?? null;
    // Rust only shrinks to the sliver at an edge (it reports none otherwise).
    sliverApplied = sliver && edge !== null;
  }

  /** The strip's height once every group box has finished opening or
   *  closing: its current height, with each animating box swapped for its
   *  end height. Constant through an animation, so the window is refitted
   *  once per change, not once per frame (animated window frames stutter). */
  function finalStripHeight(el: HTMLElement): number {
    let h = el.offsetHeight;
    for (const box of el.querySelectorAll<HTMLElement>("[data-group-members]")) {
      const end = box.dataset.open === "true" ? (box.firstElementChild as HTMLElement | null)?.scrollHeight ?? 0 : 0;
      h += end - box.offsetHeight;
    }
    return h;
  }

  $effect(() => {
    const el = stripEl;
    if (!el) return;
    stripHeight = finalStripHeight(el);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => (stripHeight = finalStripHeight(el)));
    ro.observe(el);
    return () => ro.disconnect();
  });

  // A group opened or closed (or the items changed): measure right after the
  // DOM update, before its box has moved, so the window grows first.
  $effect(() => {
    void expandedGroup;
    void strip;
    const el = stripEl;
    if (el) untrack(() => (stripHeight = finalStripHeight(el)));
  });

  // Grow the window at once (content must never be clipped), shrink it only
  // after a collapsing group has finished animating (stripLayoutDelay).
  $effect(() => {
    const h = stripHeight; // the strip grew or shrank: refit the window
    void scale; // …or the zoom changed
    untrack(() => {
      clearTimeout(shrinkTimer);
      const fit = () => {
        fittedStripHeight = h;
        void applyLayout(open !== null);
      };
      const delay = stripLayoutDelay(fittedStripHeight, h, reducedMotion);
      if (delay === 0) fit();
      else shrinkTimer = setTimeout(fit, delay);
    });
  });

  // --- open / close ----------------------------------------------------------
  let openTimer: ReturnType<typeof setTimeout> | undefined;
  let closeTimer: ReturnType<typeof setTimeout> | undefined;

  function clearTimers(): void {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    openTimer = undefined;
    closeTimer = undefined;
  }

  function sameTarget(a: Target | null, b: Target): boolean {
    if (a === null || a.kind !== b.kind) return false;
    if (a.kind === "idle" || a.kind === "toast") return true;
    if (a.kind === "agent") return b.kind === "agent" && a.key === b.key;
    return b.kind === "usage" && a.provider === b.provider;
  }

  async function show(target: Target, top: number, pin: boolean): Promise<void> {
    clearTimers();
    const wasOpen = open !== null;
    pinned = pin || (sameTarget(open, target) && pinned);
    anchorTop = top;
    open = target;
    if (!wasOpen) revealed = false;
    await applyLayout(true);
    if (open !== null) revealed = true;
  }

  async function hide(): Promise<void> {
    clearTimers();
    endSession();
    hoverCard = false;
    hoverItem = false;
    hoverKey = null;
    clearTimeout(toastTimer);
    if (open === null) return;
    open = null;
    pinned = false;
    keyboard = false;
    revealed = false;
    sentKey = null;
    scheduleGroupCollapse(); // a member's card no longer holds its group open
    await applyLayout(false);
  }

  // --- project groups: expand inline on hover/focus, fold back after -------
  let groupHover = false; // pointer on a group (head or members)
  let groupFocus = false; // keyboard focus inside a group
  let groupOpenTimer: ReturnType<typeof setTimeout> | undefined;
  let groupCloseTimer: ReturnType<typeof setTimeout> | undefined;

  function clearGroupTimers(): void {
    clearTimeout(groupOpenTimer);
    clearTimeout(groupCloseTimer);
    groupOpenTimer = undefined;
    groupCloseTimer = undefined;
  }

  /** The open card belongs to a member of the expanded group. */
  function memberCardOpen(): boolean {
    const g = expanded;
    return g !== null && g.kind === "group" && holdsOpenCard(g.members);
  }

  function expandGroup(key: string): void {
    clearGroupTimers();
    expandedGroup = key;
  }

  function collapseGroup(): void {
    clearGroupTimers();
    expandedGroup = null;
  }

  function scheduleGroupCollapse(): void {
    clearTimeout(groupCloseTimer);
    if (expandedGroup === null) return;
    groupCloseTimer = setTimeout(() => {
      if (groupHover || groupFocus || memberCardOpen()) return;
      expandedGroup = null;
    }, groupCollapseMs);
  }

  function groupPointerEnter(key: string): void {
    groupHover = true;
    clearTimeout(groupCloseTimer);
    if (expandedGroup === key) return;
    clearTimeout(groupOpenTimer);
    groupOpenTimer = setTimeout(() => expandGroup(key), hoverOpenMs);
  }

  function groupPointerLeave(): void {
    groupHover = false;
    clearTimeout(groupOpenTimer);
    scheduleGroupCollapse();
  }

  function groupFocusIn(): void {
    groupFocus = true;
    clearTimeout(groupCloseTimer);
  }

  function groupFocusOut(): void {
    groupFocus = false;
    scheduleGroupCollapse();
  }

  function scheduleClose(): void {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(() => {
      if (pinned || hoverCard || hoverItem) return;
      if (cardEl?.contains(document.activeElement)) return;
      void hide();
    }, hoverCloseMs);
  }

  function enter(target: Target, top: number): void {
    hoverItem = true;
    hoverKey = target.kind === "agent" ? target.key : null;
    clearTimeout(closeTimer);
    if (pinned) return; // a pinned card stays on its agent
    clearTimeout(openTimer);
    if (open !== null) {
      void show(target, top, false);
      return;
    }
    openTimer = setTimeout(() => void show(target, top, false), hoverOpenMs);
  }

  function leave(): void {
    hoverItem = false;
    hoverKey = null;
    clearTimeout(openTimer);
    if (!pinned) scheduleClose();
  }

  function focusItem(target: Target, top: number): void {
    if (!pinned) void show(target, top, false);
  }

  function blurItem(): void {
    if (!pinned) scheduleClose();
  }

  function clickItem(target: Target, top: number): void {
    keyboard = false; // the mouse took over: no key legend, Tab moves focus
    void show(target, top, true);
  }

  function cardEnter(): void {
    hoverCard = true;
    clearTimeout(closeTimer);
  }

  function cardLeave(): void {
    hoverCard = false;
    if (!pinned) scheduleClose();
  }

  // Typing a reply pins the card: hovering another strip item must not swap
  // it (and drop the text) under the caret.
  function replyFocus(focused: boolean): void {
    if (focused && open?.kind === "agent") pinned = true;
  }

  /** Hand the focus back to the app the hotkey took it from (Rust skips it
   *  when the user already moved elsewhere). Only once per hotkey session;
   *  a mouse-only dock never calls it. */
  function endSession(delayMs = 0): void {
    if (!session) return;
    session = false;
    clearTimeout(settleTimer);
    if (delayMs > 0) settleTimer = setTimeout(releaseFocus, delayMs);
    else releaseFocus();
  }

  // An answer (option or text reply to a blocked agent) was handed over.
  function markSent(): void {
    if (openAgent) sentKey = agentKey(openAgent);
    endSession();
  }

  /** Close the card by the user's hand (Esc, ×, Open in herdr). After Open in
   *  herdr a local herdr activates its terminal itself: release only after it
   *  had the chance, so Rust can tell (any other close releases in hide()). */
  function closeCard(reason?: "opened"): void {
    if (reason === "opened") endSession(herdrSettleMs);
    void hide();
  }

  /** The DOM top of an agent's strip item (its click/focus anchor). */
  function itemTop(a: DockAgent): number {
    const id = `${a.serverId}/${a.paneId}`;
    for (const el of stripEl?.querySelectorAll<HTMLElement>("button.item:not(.group)") ?? []) {
      if (el.dataset.agent === id) return el.offsetTop;
    }
    return 0;
  }

  /** Open an agent's card from the keyboard, unfolding its group first. */
  async function openByKeyboard(a: DockAgent): Promise<void> {
    const key = agentKey(a);
    const home = strip.items.find(
      (it) => it.kind === "group" && it.members.some((m) => agentKey(m) === key),
    );
    if (home && expandedGroup !== home.key) {
      expandGroup(home.key);
      await tick();
    }
    hoverKey = null;
    hoverItem = false;
    keyboard = true;
    await show({ kind: "agent", key }, itemTop(a), true);
  }

  /** The next-blocked hotkey (Rust focused the window and emitted
   *  `dock-next-blocked`): open the longest-blocked agent's card — again from
   *  a blocked card = the next one — or say briefly that nobody is waiting. */
  export function openNextBlocked(): void {
    session = true;
    if (list === null) {
      pendingNext = true;
      return;
    }
    pendingNext = false;
    const agents = list.agents;
    const current = keyboard && openAgent?.status === "blocked" ? agentKey(openAgent) : null;
    const target = current === null ? (blockedOrder(agents)[0] ?? null) : cycleBlocked(agents, current, 1);
    if (target !== null) {
      void openByKeyboard(target);
      return;
    }
    if (current !== null) {
      keyboard = true; // the only blocked agent: stay on its card
      return;
    }
    void show({ kind: "toast" }, 0, false);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      if (open?.kind === "toast") void hide(); // hide() releases the focus
    }, toastMs);
  }

  $effect(() => {
    if (list !== null && pendingNext) untrack(() => openNextBlocked());
  });

  function onKey(event: KeyboardEvent): void {
    if (isTypingTarget(event.target) || isTypingTarget(document.activeElement)) {
      // Every key belongs to the reply field; Esc only leaves it (DockReply
      // blurs it too — this covers any other field).
      if (event.key === "Escape") {
        event.preventDefault();
        (document.activeElement as HTMLElement | null)?.blur?.();
      }
      return;
    }
    if (event.defaultPrevented) return;
    if (event.key === "Escape" && open === null) {
      if (expandedGroup === null) return;
      event.preventDefault();
      collapseGroup(); // Esc folds an expanded group when no card is open
      return;
    }
    const action = dockKeyAction(event, {
      card: open === null ? null : open.kind === "agent" && openAgent !== null ? "agent" : "other",
      typing: false,
      keyboard,
    });
    if (action === null) return;
    switch (action.kind) {
      case "close":
        event.preventDefault();
        (document.activeElement as HTMLElement | null)?.blur?.();
        closeCard();
        return;
      case "cycle": {
        event.preventDefault();
        const next = cycleBlocked(list?.agents ?? [], openKey, action.dir);
        if (next !== null) void openByKeyboard(next);
        return;
      }
      case "answer":
        if (cardRef?.answerKey(action.key)) event.preventDefault();
        return;
      case "reply":
        // preventDefault keeps the T / slash out of the field it focuses.
        if (cardRef?.focusReply()) event.preventDefault();
        return;
      case "open":
        if (cardRef?.openInHerdr()) event.preventDefault();
        return;
    }
  }

  function onBackdrop(event: MouseEvent): void {
    // A click on the transparent part of the grown window = outside the card.
    if (event.target === event.currentTarget) void hide();
  }

  $effect(() => {
    const onVisibility = () => {
      if (!document.hidden) return;
      void hide();
      collapseGroup();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  });

  // Close when the card has nothing left to show.
  $effect(() => {
    const a = openAgent;
    const kind = open?.kind;
    if (kind === "agent" && list !== null && a === null) {
      untrack(() => void hide()); // the agent left the fleet
    } else if (a !== null && sentKey === agentKey(a) && a.status !== "blocked") {
      untrack(() => void hide()); // answered, and it moved on
    } else if (kind === "idle" && folded.length === 0) {
      untrack(() => void hide());
    } else if (kind === "usage" && !rings.some((r) => r.provider === openProvider)) {
      untrack(() => void hide()); // the provider's ring went away (gone, or no windows)
    }
  });

  // A hovered item that left the strip (folded into +N or a group, or gone)
  // never sends mouseleave; drop the hover flag so the card can still close.
  $effect(() => {
    const keys = shownKeys;
    untrack(() => {
      if (hoverKey !== null && !keys.has(hoverKey)) {
        hoverItem = false;
        hoverKey = null;
      }
    });
  });

  // The expanded group dissolved (members left, grouping turned off): forget it.
  $effect(() => {
    if (expandedGroup !== null && expanded === null) {
      untrack(() => {
        collapseGroup();
        groupHover = false;
        groupFocus = false;
      });
    }
  });

  // An open card's agent just folded into a group (a second agent of its
  // project appeared): open that group so the card keeps its item.
  $effect(() => {
    const target = open;
    if (target === null || target.kind !== "agent" || shownKeys.has(target.key)) return;
    const home = strip.items.find(
      (it) => it.kind === "group" && it.members.some((a) => agentKey(a) === target.key),
    );
    if (home) untrack(() => expandGroup(home.key));
  });

  $effect(() => () => {
    clearTimers();
    clearGroupTimers();
    clearTimeout(shrinkTimer);
    clearTimeout(toastTimer);
    clearTimeout(settleTimer);
    autohide.destroy();
  });
</script>

<svelte:window
  onkeydown={onKey}
  onfocus={() => (focused = true)}
  onblur={() => {
    focused = false;
    void hide();
  }}
/>

<div
  class="mini-dock"
  class:reduced={reducedMotion}
  class:stale
  role="presentation"
  style:zoom={scale === 1 ? undefined : String(scale)}
  onclick={onBackdrop}
  onpointerenter={() => autohide.pointer(true)}
  onpointerleave={() => autohide.pointer(false)}
>
  <nav
    class="strip"
    class:slid-out={slidOut}
    class:parked={ahPhase === "hidden"}
    class:out-left={edge === "left"}
    aria-label={m.strip_label}
    bind:this={stripEl}
    style:left="{stripX}px"
    style:top="{stripY}px"
  >
    <div
      class="grip"
      title={m.grip_title}
      aria-label={m.grip_title}
      role="presentation"
      data-tauri-drag-region
      onpointerdown={startDrag}
    >
      <span class="grip-bar" data-tauri-drag-region></span>
    </div>
    {@render children?.()}
    {#if neutral !== null}
      <button type="button" class="neutral" title={neutralTitle} aria-label={neutralTitle} onclick={onOpenSettings}>
        <span class="neutral-dot" aria-hidden="true"></span>
      </button>
    {:else}
      {#each strip.items as item (item.key)}
        {#if item.kind === "agent"}
          <DockItem
            agent={item.agent}
            iconSrc={iconFor(item.agent)}
            active={openKey === item.key}
            onenter={(top) => enter({ kind: "agent", key: item.key }, top)}
            onleave={leave}
            onfocus={(top) => focusItem({ kind: "agent", key: item.key }, top)}
            onblur={blurItem}
            onclick={(top) => clickItem({ kind: "agent", key: item.key }, top)}
          />
        {:else}
          <DockGroup
            group={item}
            {iconFor}
            expanded={expandedGroup === item.key}
            {openKey}
            onpointerenter={() => groupPointerEnter(item.key)}
            onpointerleave={groupPointerLeave}
            onfocusin={groupFocusIn}
            onfocusout={groupFocusOut}
            onexpand={() => expandGroup(item.key)}
            member={memberHandlers}
          />
        {/if}
      {/each}
      {#if folded.length > 0}
        <button
          type="button"
          class="idle-chip"
          class:active={open?.kind === "idle"}
          title={foldTitle}
          aria-label={foldTitle}
          onmouseenter={(e) => enter({ kind: "idle" }, e.currentTarget.offsetTop)}
          onmouseleave={leave}
          onfocus={(e) => focusItem({ kind: "idle" }, e.currentTarget.offsetTop)}
          onblur={blurItem}
          onclick={(e) => clickItem({ kind: "idle" }, e.currentTarget.offsetTop)}
        >+{folded.length}</button>
      {/if}
    {/if}
    {#if rings.length > 0}
      <div class="usage" role="group" aria-label={m.usage_label}>
        {#each rings as ring (ring.provider)}
          <DockUsageRing
            {ring}
            markSrc={transport?.providerMarkUrl(ring.provider) ?? null}
            active={openProvider === ring.provider}
            onenter={(top) => enter({ kind: "usage", provider: ring.provider }, top)}
            onleave={leave}
            onfocus={(top) => focusItem({ kind: "usage", provider: ring.provider }, top)}
            onblur={blurItem}
            onclick={(top) => clickItem({ kind: "usage", provider: ring.provider }, top)}
          />
        {/each}
      </div>
    {/if}
    {#if stale}
      <span class="offline-dot" role="img" title={m.stale_title} aria-label={m.stale_title}></span>
    {/if}
  </nav>
  {#if ahPhase === "hidden" && sliverApplied}
    <!-- The hidden dock: a sliver at its edge in the most urgent status's
         colour; resting the pointer on it brings the strip back. -->
    <div class="sliver st-{sliverTone}" class:at-left={edge === "left"} aria-hidden="true"></div>
  {/if}
  {#if open !== null && revealed}
    <div
      class="card-slot"
      bind:this={cardEl}
      role="presentation"
      style:left="{cardLeft}px"
      style:top="{stripY + anchorTop}px"
      style:width="{cardWidth}px"
      onmouseenter={cardEnter}
      onmouseleave={cardLeave}
    >
      {#if openAgent}
        {#key agentKey(openAgent)}
          <DockCard
            bind:this={cardRef}
            agent={openAgent}
            sinceOf={(a) => liveSince(a.sinceS, receivedAt, now)}
            iconUrl={iconFor}
            transport={agentTransport}
            onClose={closeCard}
            onSent={markSent}
            onReplyFocus={replyFocus}
            keyboardHints={keyboard}
          />
        {/key}
      {:else if openUsage}
        <DockUsageCard
          usage={openUsage}
          markSrc={transport?.providerMarkUrl(openUsage.provider) ?? null}
          {now}
          onClose={() => closeCard()}
        />
      {:else if open.kind === "idle"}
        <DockCard
          idle={folded}
          heading={foldHeading}
          sinceOf={(a) => liveSince(a.sinceS, receivedAt, now)}
          iconUrl={iconFor}
          transport={agentTransport}
          onClose={closeCard}
        />
      {:else if open.kind === "toast"}
        <p class="toast" role="status">{m.nobody_waiting}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .mini-dock {
    position: fixed;
    inset: 0;
    overflow: hidden;
    font: var(--t-body);
    color: var(--text);
  }
  .strip {
    position: absolute;
    width: 56px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s1);
    padding: 0 0 var(--s2);
    /* Opaque: a translucent, blurred strip smeared whatever sat behind it. */
    background: var(--panel);
    border: 1px solid var(--line-strong);
    border-radius: 28px;
    /* Slides in decelerating… */
    transition:
      opacity var(--dur) var(--ease),
      transform 180ms cubic-bezier(.2, .8, .2, 1);
  }
  /* …and out accelerating, past the edge it is snapped to (autohide). */
  .strip.slid-out {
    transform: translateX(100%);
    transition:
      opacity var(--dur) var(--ease),
      transform 180ms cubic-bezier(.5, 0, .9, .4);
  }
  .strip.slid-out.out-left { transform: translateX(-100%); }
  /* Hidden behind the sliver: not painted, so its rings stop animating. */
  .strip.parked { visibility: hidden; }
  /* The window is the sliver itself; the bar sits inset from the strip's
     rounded ends, flush with the screen edge and rounded on the inner side. */
  .sliver {
    position: absolute;
    top: 14px;
    bottom: 14px;
    left: 0;
    width: 6px;
    border-radius: 6px 0 0 6px;
    background: var(--sliver, var(--line-strong));
    animation: dock-sliver-in 160ms var(--ease);
  }
  .sliver.at-left { border-radius: 0 6px 6px 0; }
  /* A blocked agent holds the strip out, so this is only a safety net. */
  .sliver.st-blocked { --sliver: var(--st-blocked); animation: dock-sliver-pulse 1.8s var(--ease) infinite; }
  .sliver.st-waiting { --sliver: var(--st-waiting); }
  .sliver.st-done { --sliver: var(--st-done); }
  .sliver.st-working { --sliver: var(--st-working); }
  .sliver.st-unknown { --sliver: var(--st-unknown); }
  .sliver.st-idle { --sliver: var(--st-idle); }
  .stale .sliver { opacity: .55; }
  @keyframes dock-sliver-in {
    from { opacity: 0; }
  }
  /* Opacity only: composited, no repaint per frame. */
  @keyframes dock-sliver-pulse {
    50% { opacity: .45; }
  }
  .stale .strip { opacity: .55; }
  .grip {
    width: 100%;
    height: 18px;
    display: grid;
    place-items: center;
    cursor: grab;
  }
  .grip-bar {
    width: 20px;
    height: 4px;
    border-radius: 2px;
    background: var(--line-strong);
  }
  .neutral, .idle-chip {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 1px solid var(--line-strong);
    background: var(--key);
    color: var(--text-dim);
    font: var(--t-label);
    cursor: pointer;
    flex: none;
  }
  .idle-chip.active, .idle-chip:focus-visible { box-shadow: 0 0 0 2px var(--accent-ring); outline: none; }
  .neutral-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--st-unknown);
  }
  .offline-dot {
    position: absolute;
    top: 5px;
    right: 6px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--st-offline);
  }
  /* The usage footer: a thin divider, then one ring per provider. */
  .usage {
    width: 40px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s1);
    margin-top: var(--s1);
  }
  .usage::before {
    content: "";
    width: 28px;
    height: 1px;
    margin-bottom: var(--s1);
    background: var(--line-strong);
  }
  .toast {
    box-sizing: border-box;
    width: 100%;
    margin: 0;
    padding: var(--s2) var(--s3);
    background: var(--panel-raised);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-panel);
    color: var(--text-dim);
    font: var(--t-label);
    white-space: nowrap;
  }
  .card-slot {
    position: absolute;
    animation: dock-card-in 200ms cubic-bezier(.2, 1.3, .4, 1);
    transform-origin: left top;
  }
  @keyframes dock-card-in {
    from { opacity: 0; transform: scale(.96) translateY(-4px); }
    to { opacity: 1; transform: none; }
  }
  .reduced .card-slot { animation: none; }
  .reduced .strip { transition: none; }
  .reduced .sliver { animation: none; }
  @media (prefers-reduced-motion: reduce) {
    .card-slot { animation: none; }
    .strip { transition: none; }
    .sliver { animation: none; }
  }
</style>
