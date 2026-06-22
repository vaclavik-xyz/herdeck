import { describe, it, expect } from "vitest";
import {
  PROTOCOL_VERSION,
  encodeLine,
  decodeLine,
  splitFrames,
  helloMsg,
  slotsMsg,
  actionKeysMsg,
  keyDownMsg,
  keyUpMsg,
  byeMsg,
} from "../src/protocol.js";

describe("protocol wire format matches the Python backend", () => {
  it("protocol version is 1", () => {
    expect(PROTOCOL_VERSION).toBe(1);
  });

  it("encodeLine is compact JSON + newline (matches json.dumps separators)", () => {
    expect(encodeLine({ type: "keyUp", instanceId: "s0" })).toBe('{"type":"keyUp","instanceId":"s0"}\n');
  });

  it("hello carries snake_case protocol_version + token", () => {
    expect(helloMsg("secret", "MK.2", { columns: 5, rows: 3 })).toEqual({
      type: "hello",
      protocol_version: 1,
      token: "secret",
      device: "MK.2",
      size: { columns: 5, rows: 3 },
    });
  });

  it("slots/action_keys use instanceId + coord{col,row} + snake_case action_keys", () => {
    expect(slotsMsg([{ instanceId: "s0", coord: { col: 1, row: 0 } }])).toEqual({
      type: "slots",
      slots: [{ instanceId: "s0", coord: { col: 1, row: 0 } }],
    });
    expect(actionKeysMsg([{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }])).toEqual({
      type: "action_keys",
      action_keys: [{ instanceId: "a", type: "approve", coord: { col: 0, row: 2 } }],
    });
  });

  it("keyDown/keyUp/bye shapes", () => {
    expect(keyDownMsg("s0")).toEqual({ type: "keyDown", instanceId: "s0" });
    expect(keyUpMsg("s0")).toEqual({ type: "keyUp", instanceId: "s0" });
    expect(byeMsg()).toEqual({ type: "bye" });
  });

  it("splitFrames yields complete frames and keeps the trailing partial", () => {
    const { frames, rest } = splitFrames('{"type":"ready"}\n{"type":"render","ke');
    expect(frames).toEqual(['{"type":"ready"}']);
    expect(rest).toBe('{"type":"render","ke');
    expect(decodeLine(frames[0])).toEqual({ type: "ready" });
  });
});
