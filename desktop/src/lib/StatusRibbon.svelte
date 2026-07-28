<script lang="ts">
  // Overview's single authoritative state line: runtime health plus the agent
  // counts, each in the SAME colour the physical key uses for that status.
  import type { DeckSummary } from "./deckClient";
  import { DEFAULT_STATUS_COLORS, PALETTE } from "./statusColors";

  let { ready, summary, labels, colors = {} }: {
    ready: boolean;
    summary: DeckSummary;
    // status -> palette NAME from [theme].colors, so a remapped status keeps
    // the window and the physical key in agreement. Falls back to the backend
    // defaults for anything the config does not override.
    colors?: Record<string, string>;
    labels: {
      runtime: string; ready: string; connecting: string; agents: string;
      working: string; blocked: string; waiting: string; done: string;
    };
  } = $props();

  // `idle` stays out: it is the resting state, not news. The four shown here
  // are what a glance at the deck is actually looking for.
  // A hand-edited TOML can carry a name outside the palette. The backend
  // resolves those to the dim empty-tile grey (COLORS.get(name, dim)); the
  // window must degrade the same way instead of rendering nothing.
  const paletteName = (status: string): string => {
    const name = colors[status] || DEFAULT_STATUS_COLORS[status];
    return name && Object.hasOwn(PALETTE, name) ? name : "dim";
  };

  const cells = $derived([
    { status: "working", count: summary.working, label: labels.working },
    { status: "blocked", count: summary.blocked, label: labels.blocked },
    { status: "waiting", count: summary.waiting, label: labels.waiting },
    { status: "done", count: summary.done, label: labels.done },
  ].map((cell) => ({ ...cell, tone: paletteName(cell.status) })));

  const runtimeTone = $derived(paletteName("working"));
</script>

<div class="ribbon">
  <div class="runtime" class:ready style={`--cell:var(--st-${runtimeTone})`}>
    <span class="dot" aria-hidden="true"></span>
    <span class="eyebrow">{labels.runtime}</span>
    <strong>{ready ? labels.ready : labels.connecting}</strong>
  </div>
  <div class="agents">
    <span class="eyebrow">{labels.agents}</span>
    <strong>{summary.agents}</strong>
  </div>
  {#each cells as cell (cell.status)}
    <div class="cell" class:zero={cell.count === 0} data-status={cell.status} style={`--cell:var(--st-${cell.tone})`}>
      <span class="dot" aria-hidden="true"></span>
      <strong>{cell.count}</strong>
      <span class="eyebrow">{cell.label}</span>
    </div>
  {/each}
</div>

<style>
  .ribbon {
    display: grid;
    grid-template-columns: minmax(180px, 1.4fr) repeat(5, minmax(92px, 1fr));
    border: 1px solid var(--line);
    border-radius: var(--r-panel);
    background: var(--panel);
    overflow: hidden;
  }
  .runtime, .agents, .cell {
    display: grid;
    align-content: center;
    gap: var(--s1);
    min-height: 82px;
    padding: var(--s4) var(--s5);
    border-right: 1px solid var(--line);
  }
  .cell:last-child { border-right: 0; }
  .runtime {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    column-gap: var(--s3);
  }
  .runtime .eyebrow, .runtime strong { grid-column: 2; }
  .cell { grid-template-columns: auto minmax(0, 1fr); align-items: baseline; column-gap: var(--s2); }
  .cell .eyebrow { grid-column: 2; }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--cell, var(--st-blocked));
  }
  /* Not-ready must never borrow the ready tone: --cell is set on the row for
     the ready case, so the waiting state overrides it explicitly. */
  .runtime .dot { grid-row: 1 / span 2; width: 10px; height: 10px; background: var(--st-blocked); }
  .runtime.ready .dot { background: var(--cell); }
  /* A status with nobody in it is not news. The cells keep their places (the
     row must not reflow as counts change) but step back, so the one status
     that DOES have agents in it is what the eye lands on. */
  .cell.zero .dot { background: var(--st-dim); }
  .cell.zero strong { color: var(--text-faint); }
  .cell.zero .eyebrow { color: var(--text-faint); }
  strong {
    font: 650 22px/1 var(--font-mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: -.03em;
  }
  .runtime strong { font: var(--t-h2); }
  .eyebrow {
    color: var(--text-dim);
    font: var(--t-eyebrow);
    text-transform: uppercase;
    letter-spacing: .06em;
  }
  /* Narrow: the ribbon must stay a ribbon, not a tower — the live deck below it
     is the reason anyone opens this page. Four status cells keep one row. */
  @media (max-width: 900px) {
    .ribbon { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .runtime, .agents { grid-column: span 2; border-bottom: 1px solid var(--line); }
    .runtime, .agents, .cell { min-height: 68px; padding: var(--s3) var(--s4); }
    .agents { border-right: 0; }
    .cell:nth-child(6) { border-right: 0; }
  }
  @media (max-width: 560px) {
    .ribbon { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .cell:nth-child(even) { border-right: 0; }
    .cell:nth-child(3), .cell:nth-child(4) { border-bottom: 1px solid var(--line); }
  }
</style>
