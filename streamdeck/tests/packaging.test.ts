import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const spec = read("../herdeck-backend.spec");
const entry = read("../scripts/herdeck-backend-entry.py");
const pyproject = read("../../pyproject.toml");

/** Capture the contents of a `key = [ ... ]` list literal (flat list of strings). */
function listLiteral(src: string, key: string): string {
  const m = src.match(new RegExp(`${key}\\s*=\\s*\\[([^\\]]*)\\]`));
  if (!m) throw new Error(`no ${key}=[...] in spec`);
  return m[1];
}

describe("PyInstaller spec", () => {
  it("freezes onedir, arm64, named herdeck-backend", () => {
    expect(spec).toMatch(/name\s*=\s*['"]herdeck-backend['"]/);
    expect(spec).toMatch(/target_arch\s*=\s*['"]arm64['"]/);
    expect(spec).toContain("COLLECT("); // onedir (COLLECT), not a onefile EXE-only build
  });

  it("excludes the cairo chain + the lazy native HID driver stack, but NOT core deps", () => {
    const excludes = listLiteral(spec, "excludes");
    for (const mod of ["cairosvg", "cffi", "cairocffi", "StreamDeck", "hid"]) {
      expect(excludes).toContain(`"${mod}"`);
    }
    // websockets is a CORE dep (connector.py → Connector, used on the elgato path) — it must
    // never be excluded. serial/pyserial is not a repo dependency at all, so it is not listed.
    expect(excludes).not.toContain("websockets");
    expect(excludes).not.toContain("serial");
  });

  it("pins the elgato submodules + websockets as hidden imports", () => {
    const hidden = listLiteral(spec, "hiddenimports");
    for (const mod of [
      "herdeck.elgato.runtime",
      "herdeck.elgato.frozen",
      "herdeck.elgato.session",
      "herdeck.elgato.ipc",
      "websockets",
    ]) {
      expect(hidden).toContain(`"${mod}"`);
    }
  });

  it("bundles the assets dir as herdeck_assets data", () => {
    expect(spec).toContain("herdeck_assets");
    expect(spec).toMatch(/assets/);
  });
});

describe("freeze entry script", () => {
  it("invokes herdeck.app.main", () => {
    expect(entry).toContain("from herdeck.app import main");
    expect(entry).toMatch(/main\(\)/);
  });
});

describe("packaging build dependency", () => {
  it("declares pyinstaller in a packaging extra so a clean env can reproduce the build", () => {
    const m = pyproject.match(/packaging\s*=\s*\[([^\]]*)\]/);
    expect(m, "pyproject.toml must define a `packaging` optional-dependency").not.toBeNull();
    expect(m![1]).toMatch(/pyinstaller/);
  });
});
