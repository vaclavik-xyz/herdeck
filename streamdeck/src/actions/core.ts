import type { Coord, ActionKind } from "../protocol.js";
import type { Adapter, Surface } from "../adapter.js";
import type { KeyRegistry } from "../registry.js";

export const ACTION_UUIDS = {
  slot: "xyz.vaclavik.herdeck.slot",
  approve: "xyz.vaclavik.herdeck.approve",
  deny: "xyz.vaclavik.herdeck.deny",
  stop: "xyz.vaclavik.herdeck.stop",
  pager: "xyz.vaclavik.herdeck.pager",
} as const;

export function coordToWire(coordinates: { column: number; row: number }): Coord {
  return { col: coordinates.column, row: coordinates.row };
}

export function onSlotAppear(reg: KeyRegistry, adapter: Adapter, id: string, coord: Coord, surface: Surface) {
  adapter.registerSurface(id, surface);
  reg.addSlot(id, coord);
}
export function onSlotDisappear(reg: KeyRegistry, adapter: Adapter, id: string) {
  reg.removeSlot(id);
  adapter.unregisterSurface(id);
}
export function onActionAppear(reg: KeyRegistry, adapter: Adapter, id: string, type: ActionKind, coord: Coord, surface: Surface) {
  adapter.registerSurface(id, surface);
  reg.addActionKey(id, type, coord);
}
export function onActionDisappear(reg: KeyRegistry, adapter: Adapter, id: string) {
  reg.removeActionKey(id);
  adapter.unregisterSurface(id);
}
