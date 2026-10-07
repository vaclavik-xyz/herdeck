// Test-only reactive holder so a mounted DockCard can see its `agent` prop change.
import { dockAgent } from "./dockFixtures";
import type { DockAgent } from "./dockClient";

export function flushState(): { agent: DockAgent } {
  const s = $state({ agent: dockAgent() });
  return s;
}
