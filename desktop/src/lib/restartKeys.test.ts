import { describe, expect, it } from "vitest";

import { parseConfig } from "./configClient";
import { restartRequiredChanges } from "./restartKeys";

function withLocal(local: Record<string, unknown>) {
  return parseConfig({ base: {}, profiles: {}, local: { local }, secrets: {}, active_profile: "default" })!;
}

describe("restartRequiredChanges", () => {
  it("lists the startup-only keys whose value changed", () => {
    const before = withLocal({ deck: "d200", web_port: 8800 });
    const after = withLocal({ deck: "web", web_port: 8801, web_bind: "0.0.0.0" });
    expect(restartRequiredChanges(before, after)).toEqual(["deck", "web_bind", "web_port"]);
  });

  it("is empty when nothing restart-only changed", () => {
    const before = withLocal({ deck: "d200" });
    expect(restartRequiredChanges(before, withLocal({ deck: "d200" }))).toEqual([]);
    // empty string and absent both mean "default"
    expect(restartRequiredChanges(withLocal({}), withLocal({ herdr_socket: "" }))).toEqual([]);
  });

  it("treats a missing baseline as no known change", () => {
    expect(restartRequiredChanges(null, null)).toEqual([]);
  });
});
