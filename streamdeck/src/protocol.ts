export const PROTOCOL_VERSION = 1;

export type Coord = { col: number; row: number };
export type SlotEntry = { instanceId: string; coord: Coord };
export type ActionKind = "approve" | "deny" | "stop" | "pager";
export type ActionKeyEntry = { instanceId: string; type: ActionKind; coord: Coord };
export type RenderKeys = Record<string, { image: string; title: string | null }>;

export function encodeLine(obj: unknown): string {
  return JSON.stringify(obj) + "\n";
}

export function decodeLine(line: string): any {
  return JSON.parse(line);
}

export function splitFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split("\n");
  const rest = parts.pop() ?? "";
  return { frames: parts.filter((p) => p.length > 0), rest };
}

export function helloMsg(token: string, device?: string, size?: object) {
  return { type: "hello", protocol_version: PROTOCOL_VERSION, token, device, size };
}

export function slotsMsg(slots: SlotEntry[]) {
  return { type: "slots", slots };
}

export function actionKeysMsg(keys: ActionKeyEntry[]) {
  return { type: "action_keys", action_keys: keys };
}

export function keyDownMsg(instanceId: string) {
  return { type: "keyDown", instanceId };
}

export function keyUpMsg(instanceId: string) {
  return { type: "keyUp", instanceId };
}

export function byeMsg() {
  return { type: "bye" };
}
