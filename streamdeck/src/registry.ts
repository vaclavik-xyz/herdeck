import type { Coord, SlotEntry, ActionKeyEntry, ActionKind } from "./protocol.js";

export class KeyRegistry {
  private slots = new Map<string, Coord>();
  private actions = new Map<string, { type: ActionKind; coord: Coord }>();
  private changeCbs: Array<() => void> = [];

  onChange(cb: () => void) { this.changeCbs.push(cb); }
  private changed() { this.changeCbs.forEach((cb) => cb()); }

  addSlot(instanceId: string, coord: Coord) { this.slots.set(instanceId, coord); this.changed(); }
  removeSlot(instanceId: string) { this.slots.delete(instanceId); this.changed(); }
  addActionKey(instanceId: string, type: ActionKind, coord: Coord) { this.actions.set(instanceId, { type, coord }); this.changed(); }
  removeActionKey(instanceId: string) { this.actions.delete(instanceId); this.changed(); }

  slotsSnapshot(): SlotEntry[] {
    return [...this.slots.entries()].map(([instanceId, coord]) => ({ instanceId, coord }));
  }

  actionKeysSnapshot(): ActionKeyEntry[] {
    return [...this.actions.entries()].map(([instanceId, { type, coord }]) => ({ instanceId, type, coord }));
  }
}
