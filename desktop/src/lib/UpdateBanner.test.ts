// UpdateBanner renders the one thing App.svelte's two windows and its manual
// and automatic check paths all funnel into: a check's outcome. It takes
// plain props (no Tauri invoke/listen), so unlike ConfigApp.test.ts this
// mounts the real component directly, no bridge mock needed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "./i18n.svelte";
import UpdateBanner from "./UpdateBanner.svelte";
import type { UpdateCheckState } from "./updateClient";

let target: HTMLElement;

beforeEach(() => {
  setLang("en");
  target = document.createElement("div");
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

function render(props: {
  state?: UpdateCheckState | null;
  installError?: string;
  installing?: boolean;
  onInstall?: () => void;
}): { target: HTMLElement; cleanup: () => void } {
  const instance = mount(UpdateBanner, {
    target,
    props: { onInstall: () => {}, ...props },
  });
  return { target, cleanup: () => unmount(instance) };
}

describe("UpdateBanner", () => {
  it("renders no visible banner box when there is no state and no install error", () => {
    const { target, cleanup } = render({ state: null });
    try {
      expect(target.querySelector(".banner")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("still renders a persistent [role=status] live region even with nothing to say", () => {
    // Mounted unconditionally in App.svelte specifically so this region
    // exists BEFORE the first check settles — a live region only reliably
    // announces a content change on an element that already existed.
    const { target, cleanup } = render({ state: null });
    try {
      expect(target.querySelector('[role="status"]')).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("shows a checking message while a check is in flight", () => {
    const { target, cleanup } = render({ state: { kind: "checking" } });
    try {
      expect(target.textContent).toContain("Checking for updates…");
      expect(target.querySelector("button")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("offers to install when an update is available, and calls onInstall", () => {
    const onInstall = vi.fn();
    const { target, cleanup } = render({
      state: { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
      onInstall,
    });
    try {
      expect(target.textContent).toContain("Herdeck 0.2.0 is available.");
      const button = target.querySelector("button");
      expect(button, "no install action rendered").not.toBeNull();
      expect(button!.textContent).toBe("Install and restart");
      button!.click();
      expect(onInstall).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
    }
  });

  it("shows an installing label while installing is true", () => {
    const { target, cleanup } = render({
      state: { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
      installing: true,
    });
    try {
      expect(target.querySelector("button")!.textContent).toBe("Installing…");
    } finally {
      cleanup();
    }
  });

  it("says explicitly that Herdeck is up to date — a state that used to render as nothing", () => {
    const { target, cleanup } = render({ state: { kind: "up-to-date" } });
    try {
      expect(target.textContent).toContain("Herdeck is up to date.");
      expect(target.querySelector(".success")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("surfaces a failed check with the reason, not a swallowed error", () => {
    const { target, cleanup } = render({
      state: { kind: "failed", reason: "network unreachable" },
    });
    try {
      expect(target.textContent).toContain("Update check failed: network unreachable");
      expect(target.querySelector(".error")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("lets an install failure outrank the check's own state", () => {
    const { target, cleanup } = render({
      state: { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
      installError: "disk full",
    });
    try {
      expect(target.textContent).toContain("disk full");
      expect(target.textContent).not.toContain("is available");
    } finally {
      cleanup();
    }
  });

  it("offers a retry action on an install failure, not an unretryable dead end", () => {
    const onInstall = vi.fn();
    const { target, cleanup } = render({
      state: { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
      installError: "disk full",
      onInstall,
    });
    try {
      const button = target.querySelector("button");
      expect(button, "no retry action rendered on the install error").not.toBeNull();
      expect(button!.textContent).toBe("Install and restart");
      button!.click();
      expect(onInstall).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
    }
  });

  it("renders every state in Czech too", () => {
    setLang("cs");
    const cases: Array<[UpdateCheckState, string]> = [
      [{ kind: "checking" }, "Kontroluji aktualizace…"],
      [
        { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
        "Je dostupný Herdeck 0.2.0.",
      ],
      [{ kind: "up-to-date" }, "Herdeck je aktuální."],
      [{ kind: "failed", reason: "boom" }, "Kontrola aktualizací selhala: boom"],
    ];
    for (const [state, expected] of cases) {
      const { target, cleanup } = render({ state });
      try {
        flushSync();
        expect(target.textContent).toContain(expected);
      } finally {
        cleanup();
      }
    }
    const { target, cleanup } = render({
      state: { kind: "available", info: { version: "0.2.0", current_version: "0.1.0" } },
    });
    try {
      expect(target.querySelector("button")!.textContent).toBe("Nainstalovat a restartovat");
    } finally {
      cleanup();
    }
  });
});
