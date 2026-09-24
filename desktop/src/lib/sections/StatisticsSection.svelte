<script lang="ts">
  // Statistics: how the agents spent their time and how long they waited for
  // you, from the bridges' status history (src/herdeck/history.py) merged by
  // the runtime's GET /stats (deckapp/stats.py). Not a config section: it
  // reads through the Rust `maintenance_call` proxy. Texts: en + cs below.
  import { onDestroy } from "svelte";
  import type { InvokeFn } from "../deckClient";
  import { defineMessages, fmt, locale } from "../i18n.svelte";
  import {
    CHART_SERIES, STATS_GROUPS, STATS_RANGES, activeMs, dayBars, fetchStats, formatDuration,
    type ChartSeries, type StatsGroup, type StatsRange, type StatsResult,
  } from "../statsClient";

  let { invoke = null }: {
    // Tauri `invoke`; null (browser preview / no runtime yet) shows a hint.
    invoke?: InvokeFn | null;
  } = $props();

  const MESSAGES = defineMessages({
    en: {
      range_label: "Range",
      range_title: "Calendar days in this computer's time zone: today, the last 7 or the last 30 days (history keeps 30).",
      range_1: "Today",
      range_7: "7 days",
      range_30: "30 days",
      group_label: "Group by",
      group_title: "Aggregate per agent (one terminal session), per repository, or per agent type (claude, codex…).",
      group_agent: "Agent",
      group_repo: "Repository",
      group_agent_type: "Agent type",
      refresh: "Refresh",
      refresh_title: "Ask the bridges for fresh statistics",
      loading: "Loading statistics…",
      no_runtime: "Statistics need the running Herdeck runtime.",
      error: "Statistics could not be loaded: {detail}",
      unsupported: "No connected bridge keeps status history yet. Update the bridges (Maintenance) to start recording.",
      disconnected: "No bridge is connected right now.",
      failed: "The bridges could not answer.",
      waited: "Agents waited for you",
      waited_title: "Total time agents spent blocked on a question or approval in this range (including blocks still open).",
      answers: "Answers given",
      answers_title: "Blocked episodes ended by an answer sent through Herdeck (deck, window, notification); typing in the terminal is not counted.",
      answers_of: "of {n} blocks",
      median: "Median time to answer",
      median_title: "Median length of a finished blocked episode (the pane moved on); p90 = 9 of 10 were answered faster.",
      p90: "p90 {value}",
      done: "Done tasks",
      done_title: "How many times an agent entered the done state in this range.",
      per_day: "Per day",
      per_group: "Breakdown",
      col_group: "Name",
      col_working: "Working",
      col_blocked: "Blocked",
      col_waiting: "Waiting",
      col_idle: "Idle",
      col_blocks: "Blocks",
      col_answered: "Answered",
      col_median: "Median",
      col_p90: "p90",
      col_done: "Done",
      working: "working",
      blocked: "blocked",
      waiting: "waiting",
      idle: "idle",
      empty: "Nothing recorded in this range yet.",
      truncated: "Partial result: the history was too large to aggregate in time.",
      missing: "Not included: {list}",
      reason_unsupported: "bridge without history",
      reason_disconnected: "disconnected",
      reason_timeout: "no answer",
      chart_label: "Agent time per day",
      day_title: "{day}: {detail}",
    },
    cs: {
      range_label: "Období",
      range_title: "Kalendářní dny v časovém pásmu tohoto počítače: dnes, posledních 7 nebo 30 dní (historie drží 30).",
      range_1: "Dnes",
      range_7: "7 dní",
      range_30: "30 dní",
      group_label: "Seskupit",
      group_title: "Souhrn po agentech (jedna terminálová relace), po repozitářích nebo po typu agenta (claude, codex…).",
      group_agent: "Agent",
      group_repo: "Repozitář",
      group_agent_type: "Typ agenta",
      refresh: "Obnovit",
      refresh_title: "Vyžádat od bridge čerstvé statistiky",
      loading: "Načítám statistiky…",
      no_runtime: "Statistiky potřebují běžící runtime Herdecku.",
      error: "Statistiky se nepodařilo načíst: {detail}",
      unsupported: "Žádný připojený bridge zatím neukládá historii stavů. Aktualizuj bridge (Údržba) a začne se zaznamenávat.",
      disconnected: "Právě není připojený žádný bridge.",
      failed: "Bridge nedokázaly odpovědět.",
      waited: "Agenti na tebe čekali",
      waited_title: "Celkový čas, kdy agenti stáli na otázce nebo schválení v tomto období (včetně ještě otevřených).",
      answers: "Odpovědi",
      answers_title: "Blokace ukončené odpovědí poslanou přes Herdeck (deck, okno, notifikace); psaní přímo do terminálu se nepočítá.",
      answers_of: "z {n} blokací",
      median: "Medián doby odpovědi",
      median_title: "Medián délky ukončené blokace (pane pokračoval dál); p90 = 9 z 10 odpovědí bylo rychlejších.",
      p90: "p90 {value}",
      done: "Hotové úkoly",
      done_title: "Kolikrát agent v tomto období přešel do stavu hotovo.",
      per_day: "Po dnech",
      per_group: "Rozpad",
      col_group: "Název",
      col_working: "Pracuje",
      col_blocked: "Blokováno",
      col_waiting: "Čeká",
      col_idle: "Nečinný",
      col_blocks: "Blokací",
      col_answered: "Odpovězeno",
      col_median: "Medián",
      col_p90: "p90",
      col_done: "Hotovo",
      working: "pracuje",
      blocked: "blokováno",
      waiting: "čeká",
      idle: "nečinný",
      empty: "V tomto období zatím nic zaznamenáno.",
      truncated: "Částečný výsledek: historie byla příliš velká na včasné zpracování.",
      missing: "Nezahrnuto: {list}",
      reason_unsupported: "bridge bez historie",
      reason_disconnected: "odpojeno",
      reason_timeout: "bez odpovědi",
      chart_label: "Čas agentů po dnech",
      day_title: "{day}: {detail}",
    },
  });
  const lm = $derived(MESSAGES[locale.lang]);

  const SERIES_LABEL: Record<ChartSeries, "working" | "blocked" | "waiting" | "idle"> = {
    workingMs: "working",
    blockedMs: "blocked",
    waitingMs: "waiting",
    idleMs: "idle",
  };
  const CHART_W = 600;
  const CHART_H = 140;

  let range = $state<StatsRange>(7);
  let group = $state<StatsGroup>("agent");
  let result = $state<StatsResult | null>(null);
  let loading = $state(false);
  let alive = true;
  let seq = 0;
  onDestroy(() => { alive = false; });

  async function load(): Promise<void> {
    const call = invoke;
    if (!call) return;
    const mine = ++seq;
    loading = true;
    const r = await fetchStats(call, range, group);
    if (!alive || mine !== seq) return;
    result = r;
    loading = false;
  }

  $effect(() => {
    // re-query whenever the selectors (or the transport) change
    void range; void group;
    if (invoke) void load();
  });

  const report = $derived(result?.kind === "ok" ? result.report : null);
  const bars = $derived(report ? dayBars(report.days, CHART_W, CHART_H) : []);
  const isEmpty = $derived(report != null && activeMs(report.total) === 0 && report.total.doneCount === 0);

  function dayLabel(day: string): string {
    const date = new Date(`${day}T00:00:00Z`);
    return date.toLocaleDateString(locale.lang === "cs" ? "cs-CZ" : "en-US", {
      timeZone: "UTC", day: "numeric", month: "numeric",
    });
  }

  function reasonText(reason: string): string {
    if (reason === "unsupported") return lm.reason_unsupported;
    if (reason === "disconnected") return lm.reason_disconnected;
    if (reason === "timeout") return lm.reason_timeout;
    return reason;
  }

  function barTitle(bar: (typeof bars)[number]): string {
    const day = report?.days.find((d) => d.day === bar.day);
    const detail = day
      ? CHART_SERIES.map((k) => `${lm[SERIES_LABEL[k]]} ${formatDuration(day[k])}`).join(", ")
      : "";
    return fmt(lm.day_title, { day: dayLabel(bar.day), detail });
  }

  const unavailableText = $derived(
    result?.kind === "unavailable"
      ? result.code === "unsupported" ? lm.unsupported : result.code === "disconnected" ? lm.disconnected : lm.failed
      : "",
  );
</script>

<div class="statistics">
  <div class="toolbar">
    <div class="range" role="group" aria-label={lm.range_label} title={lm.range_title}>
      {#each STATS_RANGES as r}
        <button type="button" data-range={r} aria-pressed={range === r} class:active={range === r} onclick={() => (range = r)}>
          {lm[`range_${r}`]}
        </button>
      {/each}
    </div>
    <label class="group-select" title={lm.group_title}>
      <span>{lm.group_label}</span>
      <select bind:value={group} data-group-select>
        {#each STATS_GROUPS as g}<option value={g}>{lm[`group_${g}`]}</option>{/each}
      </select>
    </label>
    <button type="button" class="refresh" data-action="refresh" onclick={() => void load()} disabled={!invoke || loading} title={lm.refresh_title}>
      {lm.refresh}
    </button>
  </div>

  {#if !invoke}
    <p class="hint">{lm.no_runtime}</p>
  {:else if result == null}
    <p class="hint">{lm.loading}</p>
  {:else if result.kind === "error"}
    <p class="hint bad" data-stats-error>{fmt(lm.error, { detail: result.message })}</p>
  {:else if result.kind === "unavailable"}
    <p class="hint" data-stats-unavailable>{unavailableText}</p>
  {:else if report}
    <div class="summary" data-summary>
      <div class="metric" title={lm.waited_title} data-metric="waited">
        <span>{lm.waited}</span>
        <strong>{formatDuration(report.total.blockedMs)}</strong>
      </div>
      <div class="metric" title={lm.answers_title} data-metric="answers">
        <span>{lm.answers}</span>
        <strong>{report.total.answeredCount}</strong>
        <small>{fmt(lm.answers_of, { n: report.total.blockedCount })}</small>
      </div>
      <div class="metric" title={lm.median_title} data-metric="median">
        <span>{lm.median}</span>
        <strong>{formatDuration(report.total.answerMedianMs)}</strong>
        <small>{fmt(lm.p90, { value: formatDuration(report.total.answerP90Ms) })}</small>
      </div>
      <div class="metric" title={lm.done_title} data-metric="done">
        <span>{lm.done}</span>
        <strong>{report.total.doneCount}</strong>
      </div>
    </div>

    {#if report.truncated}<p class="hint bad">{lm.truncated}</p>{/if}
    {#if report.missing.length}
      <p class="hint" data-missing>
        {fmt(lm.missing, { list: report.missing.map((m) => `${m.serverId} (${reasonText(m.reason)})`).join(", ") })}
      </p>
    {/if}

    {#if isEmpty}
      <p class="hint" data-empty>{lm.empty}</p>
    {:else}
      <section class="block">
        <h3>{lm.per_day}</h3>
        <svg class="chart" viewBox={`0 0 ${CHART_W} ${CHART_H + 18}`} role="img" aria-label={lm.chart_label} data-chart>
          {#each bars as bar}
            <g data-day={bar.day}>
              <title>{barTitle(bar)}</title>
              <rect class="slot" x={bar.x} y="0" width={bar.width} height={CHART_H} />
              {#each bar.segments as seg}
                <rect class="seg {SERIES_LABEL[seg.series]}" x={bar.x} y={seg.y} width={bar.width} height={seg.height} />
              {/each}
              {#if bars.length <= 10 || bars.indexOf(bar) % 5 === 0}
                <text x={bar.x + bar.width / 2} y={CHART_H + 13} text-anchor="middle">{dayLabel(bar.day)}</text>
              {/if}
            </g>
          {/each}
        </svg>
        <ul class="legend">
          {#each CHART_SERIES as k}<li><i class={SERIES_LABEL[k]}></i>{lm[SERIES_LABEL[k]]}</li>{/each}
        </ul>
      </section>

      <section class="block">
        <h3>{lm.per_group}</h3>
        <div class="table-wrap">
          <table data-groups>
            <thead>
              <tr>
                <th>{lm.col_group}</th><th>{lm.col_working}</th><th>{lm.col_blocked}</th>
                <th>{lm.col_waiting}</th><th>{lm.col_idle}</th><th>{lm.col_blocks}</th>
                <th>{lm.col_answered}</th><th>{lm.col_median}</th><th>{lm.col_p90}</th><th>{lm.col_done}</th>
              </tr>
            </thead>
            <tbody>
              {#each report.groups as g (g.key)}
                <tr data-group={g.key}>
                  <td class="name" title={g.label}>{g.label}</td>
                  <td>{formatDuration(g.workingMs)}</td>
                  <td>{formatDuration(g.blockedMs)}</td>
                  <td>{formatDuration(g.waitingMs)}</td>
                  <td>{formatDuration(g.idleMs)}</td>
                  <td>{g.blockedCount}</td>
                  <td>{g.answeredCount}</td>
                  <td>{formatDuration(g.answerMedianMs)}</td>
                  <td>{formatDuration(g.answerP90Ms)}</td>
                  <td>{g.doneCount}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}
  {/if}
</div>

<style>
  .statistics { display: flex; flex-direction: column; gap: var(--s4); }
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s3); }
  .range { display: inline-flex; border: 1px solid var(--line-strong); border-radius: var(--r-control); overflow: hidden; }
  .range button { border: 0; border-radius: 0; border-right: 1px solid var(--line); }
  .range button:last-child { border-right: 0; }
  .range button.active { background: var(--panel-raised); color: var(--text); font-weight: 630; }
  .group-select { display: inline-flex; align-items: center; gap: var(--s2); color: var(--text-dim); font: var(--t-help); }
  .group-select select { min-height: 30px; border: 1px solid var(--line-strong); border-radius: var(--r-control); background: var(--field); color: var(--text); padding: 0 var(--s2); }
  .refresh { margin-left: auto; }
  .hint { margin: 0; color: var(--text-dim); font: var(--t-help); }
  .hint.bad { color: var(--st-blocked); }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: var(--s3); }
  .metric { display: flex; flex-direction: column; gap: 2px; border: 1px solid var(--line); border-radius: var(--r-panel); background: var(--panel); padding: var(--s3) var(--s4); }
  .metric span { color: var(--text-dim); font: var(--t-help); }
  .metric strong { color: var(--text); font: 650 22px/1.2 var(--font-mono); }
  .metric small { color: var(--text-faint); font: var(--t-help); }
  .block { border: 1px solid var(--line); border-radius: var(--r-panel); background: var(--panel); padding: var(--s4) var(--s5); }
  h3 { margin: 0 0 var(--s3); color: var(--text); font: var(--t-h2); }
  .chart { display: block; width: 100%; height: auto; }
  .chart .slot { fill: var(--field); }
  .chart text { fill: var(--text-faint); font: 10px var(--font-mono); }
  .seg.working, .legend i.working { fill: var(--st-working); background: var(--st-working); }
  .seg.blocked, .legend i.blocked { fill: var(--st-blocked); background: var(--st-blocked); }
  .seg.waiting, .legend i.waiting { fill: var(--st-waiting); background: var(--st-waiting); }
  .seg.idle, .legend i.idle { fill: var(--st-idle); background: var(--st-idle); }
  .legend { display: flex; flex-wrap: wrap; gap: var(--s4); margin: var(--s2) 0 0; padding: 0; list-style: none; color: var(--text-dim); font: var(--t-help); }
  .legend li { display: inline-flex; align-items: center; gap: 6px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font: var(--t-mono); font-size: 11px; }
  th, td { padding: 6px 8px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
  th { color: var(--text-dim); font-weight: 600; }
  th:first-child, td.name { text-align: left; }
  td { color: var(--text); }
  td.name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
  button {
    min-height: 30px;
    padding: 0 var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-control);
    background: var(--field);
    color: var(--text-dim);
    cursor: pointer;
  }
  button:hover:not(:disabled) { color: var(--text); background: var(--panel-raised); }
  button:disabled { opacity: .55; cursor: default; }
</style>
