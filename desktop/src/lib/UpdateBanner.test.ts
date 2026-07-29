// UpdateBanner renders the two orthogonal pieces of state App.svelte tracks
// (see updateState.ts): a sticky `availableUpdate` and a transient `notice`.
// It takes plain props (no Tauri invoke/listen), so unlike ConfigApp.test.ts
// this mounts the real component directly, no bridge mock needed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { setLang } from "./i18n.svelte";
import UpdateBanner from "./UpdateBanner.svelte";
import type { Notice } from "./updateState";
import type { UpdateInfo } from "./updateClient";

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
  availableUpdate?: UpdateInfo | null;
  notice?: Notice | null;
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

const update: UpdateInfo = { version: "0.2.0", current_version: "0.1.0" };

describe("UpdateBanner", () => {
  it("renders no visible banner box when there is nothing to say", () => {
    const { target, cleanup } = render({});
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
    const { target, cleanup } = render({});
    try {
      expect(target.querySelector('[role="status"]')).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("shows a checking message while a check is in flight", () => {
    const { target, cleanup } = render({ notice: { kind: "checking" } });
    try {
      expect(target.textContent).toContain("Checking for updates…");
      expect(target.querySelector("button")).toBeNull();
    } finally {
      cleanup();
    }
  });

  it("offers to install when an update is available, and calls onInstall", () => {
    const onInstall = vi.fn();
    const { target, cleanup } = render({ availableUpdate: update, onInstall });
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
    const { target, cleanup } = render({ availableUpdate: update, installing: true });
    try {
      expect(target.querySelector("button")!.textContent).toBe("Installing…");
    } finally {
      cleanup();
    }
  });

  it("says explicitly that Herdeck is up to date — a state that used to render as nothing", () => {
    const { target, cleanup } = render({ notice: { kind: "up-to-date" } });
    try {
      expect(target.textContent).toContain("Herdeck is up to date.");
      expect(target.querySelector(".success")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  it("surfaces a failed check with the reason, not a swallowed error", () => {
    const { target, cleanup } = render({ notice: { kind: "failed", reason: "network unreachable" } });
    try {
      expect(target.textContent).toContain("Update check failed: network unreachable");
      expect(target.querySelector(".error")).not.toBeNull();
    } finally {
      cleanup();
    }
  });

  // The fix for "a manual check reports nothing when an update is already
  // showing": notice covers availableUpdate rather than being suppressed by
  // it. The update is still there underneath (App.svelte never touches
  // availableUpdate for a "failed" result) — this pins that the BANNER
  // shows the notice, not that the update was lost.
  it("lets a transient notice cover a live available update rather than being suppressed by it", () => {
    const { target, cleanup } = render({
      availableUpdate: update,
      notice: { kind: "failed", reason: "offline" },
    });
    try {
      expect(target.textContent).toContain("Update check failed: offline");
      expect(target.textContent).not.toContain("is available");
    } finally {
      cleanup();
    }
  });

  it("lets an install failure outrank both the notice and the available update", () => {
    const { target, cleanup } = render({
      availableUpdate: update,
      notice: { kind: "checking" },
      installError: "disk full",
    });
    try {
      expect(target.textContent).toContain("disk full");
      expect(target.textContent).not.toContain("is available");
      expect(target.textContent).not.toContain("Checking for updates");
    } finally {
      cleanup();
    }
  });

  it("offers a retry action on an install failure, not an unretryable dead end", () => {
    const onInstall = vi.fn();
    const { target, cleanup } = render({
      availableUpdate: update,
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
    const cases: Array<[{ availableUpdate?: UpdateInfo | null; notice?: Notice | null }, string]> = [
      [{ notice: { kind: "checking" } }, "Kontroluji aktualizace…"],
      [{ availableUpdate: update }, "Je dostupný Herdeck 0.2.0."],
      [{ notice: { kind: "up-to-date" } }, "Herdeck je aktuální."],
      [{ notice: { kind: "failed", reason: "boom" } }, "Kontrola aktualizací selhala: boom"],
    ];
    for (const [props, expected] of cases) {
      const { target, cleanup } = render(props);
      try {
        flushSync();
        expect(target.textContent).toContain(expected);
      } finally {
        cleanup();
      }
    }
    const { target, cleanup } = render({ availableUpdate: update });
    try {
      expect(target.querySelector("button")!.textContent).toBe("Nainstalovat a restartovat");
    } finally {
      cleanup();
    }
  });
});
