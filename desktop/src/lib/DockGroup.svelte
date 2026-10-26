<script lang="ts">
  // A project group on the mini dock strip ([desktop].dock_group): the
  // project's mark in a ring of its most urgent member's status, with a count
  // badge. Hovering or focusing it expands it inline — the members slide out
  // under it, one after another — and MiniDock folds it back a moment after
  // the pointer and focus have left it (and no member's card is open).
  //
  // The members stay in the DOM while folded (inert, zero height) so both
  // directions are plain CSS transitions: the members' box opens with a
  // 0fr → 1fr grid row (the strip grows smoothly under it, no jump) and each
  // member fades/slides in with a stagger. MiniDock sizes the window for the
  // final height before the box opens, and shrinks it only after it closed.
  import DockItem from "./DockItem.svelte";
  import type { DockAgent } from "./dockClient";
  import { agentKey } from "./dockModel";
  import { DOCK_MESSAGES, statusLabel } from "./dockMessages";
  import type { MemberHandlers, StripItem } from "./dockStrip";
  import { fmt, locale } from "./i18n.svelte";

  type Group = Extract<StripItem, { kind: "group" }>;

  let {
    group,
    iconFor,
    expanded,
    openKey = null,
    onpointerenter,
    onpointerleave,
    onfocusin,
    onfocusout,
    onexpand,
    member,
  }: {
    group: Group;
    iconFor: (agent: DockAgent) => string | null;
    expanded: boolean;
    /** The agent whose card is open, if any (agentKey). */
    openKey?: string | null;
    onpointerenter: () => void;
    onpointerleave: () => void;
    onfocusin: () => void;
    /** Focus left the group entirely (not just moved between its items). */
    onfocusout: () => void;
    /** Expand now (click / Enter on the head). */
    onexpand: () => void;
    member: MemberHandlers;
  } = $props();

  const m = $derived(DOCK_MESSAGES[locale.lang]);
  // While folded, the head carries the active ring of a member's open card.
  const active = $derived(openKey !== null && group.members.some((a) => agentKey(a) === openKey));
  // The head draws like the lead member, in the group's status and name.
  const head = $derived<DockAgent>({
    ...group.lead,
    status: group.status,
    offline: group.offline,
    title: group.label,
  });
  const label = $derived(
    fmt(m.group_label, {
      name: group.label,
      n: group.members.length,
      status: statusLabel(locale.lang, group.status, group.offline),
    }),
  );

  function focusOut(event: FocusEvent): void {
    const next = event.relatedTarget as Node | null;
    if (next && (event.currentTarget as HTMLElement).contains(next)) return;
    onfocusout();
  }
</script>

<div
  class="group"
  class:expanded
  role="group"
  aria-label={group.label}
  onmouseenter={onpointerenter}
  onmouseleave={onpointerleave}
  onfocusin={onfocusin}
  onfocusout={focusOut}
>
  <DockItem
    agent={head}
    iconSrc={iconFor(group.lead)}
    {label}
    count={group.members.length}
    {expanded}
    active={active && !expanded}
    onenter={() => {}}
    onleave={() => {}}
    onfocus={onexpand}
    onblur={() => {}}
    onclick={onexpand}
  />
  <div class="members" data-group-members data-open={expanded ? "true" : "false"} inert={!expanded}>
    <div class="members-clip">
      <div class="members-list">
        {#each group.members as agent, i (agentKey(agent))}
          <div class="member" style:--i={i}>
            <DockItem
              {agent}
              iconSrc={iconFor(agent)}
              active={openKey === agentKey(agent)}
              onenter={(top) => member.enter(agent, top)}
              onleave={member.leave}
              onfocus={(top) => member.focus(agent, top)}
              onblur={member.blur}
              onclick={(top) => member.click(agent, top)}
            />
          </div>
        {/each}
      </div>
    </div>
  </div>
</div>

<style>
  /* The group and its open members share one soft capsule, so the members
     read as belonging to the head above them. */
  .group {
    width: 52px;
    display: flex;
    flex-direction: column;
    align-items: center;
    border-radius: 26px;
    background: transparent;
    transition: background-color 160ms var(--ease);
    flex: none;
  }
  .group.expanded { background: color-mix(in srgb, var(--key) 75%, transparent); }
  .members {
    display: grid;
    grid-template-rows: 0fr;
    width: 100%;
    transition: grid-template-rows 160ms var(--ease);
  }
  .members[data-open="true"] {
    grid-template-rows: 1fr;
    /* A beat for Rust to grow the window before the strip does. */
    transition-delay: 30ms;
  }
  .members-clip { min-height: 0; overflow: hidden; }
  /* Padding inside the clip: room for the focus/active rings at the ends. */
  .members-list {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s1);
    padding: var(--s1) 0 2px;
  }
  .member {
    opacity: 0;
    transform: translateY(-10px) scale(.86);
    transition:
      opacity 120ms var(--ease),
      transform 160ms var(--ease);
  }
  .members[data-open="true"] .member {
    opacity: 1;
    transform: none;
    transition:
      opacity 160ms var(--ease),
      transform 160ms cubic-bezier(.2, 1.25, .4, 1);
    /* Staggered 40 ms (GROUP_STAGGER_MS), after the 30 ms window beat. */
    transition-delay: calc(30ms + var(--i) * 40ms);
  }
  :global(.mini-dock.reduced) .group,
  :global(.mini-dock.reduced) .members,
  :global(.mini-dock.reduced) .member { transition: none !important; }
  @media (prefers-reduced-motion: reduce) {
    .group, .members, .member { transition: none !important; }
  }
</style>
