import { action, SingletonAction } from "@elgato/streamdeck";
import type { WillAppearEvent, WillDisappearEvent, KeyDownEvent, KeyUpEvent } from "@elgato/streamdeck";
import type { ActionKind } from "../protocol.js";
import type { Adapter, Surface } from "../adapter.js";
import type { KeyRegistry } from "../registry.js";
import { coordToWire, onSlotAppear, onSlotDisappear, onActionAppear, onActionDisappear } from "./core.js";

function surfaceOf(ev: WillAppearEvent): Surface {
  return {
    setImage: (image: string) => void ev.action.setImage(image),
    setTitle: (title: string) => void ev.action.setTitle(title),
  };
}

export function makeSlotAction(reg: KeyRegistry, adapter: Adapter) {
  @action({ UUID: "xyz.vaclavik.herdeck.slot" })
  class AgentSlotAction extends SingletonAction {
    override onWillAppear(ev: WillAppearEvent) {
      // Coordinates are undefined when the key lives in a Multi-Action (not on the grid).
      // Skip registration instead of throwing in coordToWire — the brain's slot/action
      // model is coordinate-derived, so an off-grid instance simply isn't tracked.
      if (!ev.action.coordinates) return;
      onSlotAppear(reg, adapter, ev.action.id, coordToWire(ev.action.coordinates), surfaceOf(ev));
    }
    override onWillDisappear(ev: WillDisappearEvent) { onSlotDisappear(reg, adapter, ev.action.id); }
    override onKeyDown(ev: KeyDownEvent) { adapter.handleKeyDown(ev.action.id); }
    override onKeyUp(ev: KeyUpEvent) { adapter.handleKeyUp(ev.action.id); }
  }
  return new AgentSlotAction();
}

export function makeActionKey(reg: KeyRegistry, adapter: Adapter, uuid: string, type: ActionKind) {
  @action({ UUID: uuid })
  class HerdrActionKey extends SingletonAction {
    override onWillAppear(ev: WillAppearEvent) {
      // See AgentSlotAction: a Multi-Action instance has no coordinates; skip it.
      if (!ev.action.coordinates) return;
      onActionAppear(reg, adapter, ev.action.id, type, coordToWire(ev.action.coordinates), surfaceOf(ev));
    }
    override onWillDisappear(ev: WillDisappearEvent) { onActionDisappear(reg, adapter, ev.action.id); }
    override onKeyDown(ev: KeyDownEvent) { adapter.handleKeyDown(ev.action.id); }
    override onKeyUp(ev: KeyUpEvent) { adapter.handleKeyUp(ev.action.id); }
  }
  return new HerdrActionKey();
}
