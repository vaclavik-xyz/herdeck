// The agent card's live terminal reuses the xterm.js bundle vendored for the
// browser dashboard (src/herdeck/assets/web, see VENDORED.md) — one pinned,
// licensed copy instead of a second npm dependency. vite.config.ts maps the
// `@herdeck-web` alias there. The bundles are UMD: depending on how the
// bundler wraps them the constructors arrive as exports, as a CommonJS
// default, or as globals, so all three are accepted. Loaded lazily: ~290 kB
// that only a user who opens a terminal pays for.

/** The small surface the card needs from a terminal (injectable for tests). */
export interface TerminalHandle {
  readonly cols: number;
  readonly rows: number;
  write(bytes: Uint8Array): void;
  reset(): void;
  resize(cols: number, rows: number): void;
  fit(): void;
  dispose(): void;
}

export type TerminalFactory = (host: HTMLElement) => Promise<TerminalHandle>;

type Ctor<T> = new (...args: never[]) => T;

interface XtermInstance {
  cols: number;
  rows: number;
  open(el: HTMLElement): void;
  write(data: Uint8Array): void;
  reset(): void;
  resize(cols: number, rows: number): void;
  loadAddon(addon: unknown): void;
  dispose(): void;
}

function pick<T>(mod: unknown, name: string): T | undefined {
  const m = (mod ?? {}) as Record<string, unknown>;
  const d = (m.default ?? {}) as Record<string, unknown>;
  const g = globalThis as unknown as Record<string, unknown>;
  const global = g[name];
  const fromGlobal =
    typeof global === "function"
      ? global
      : (global as Record<string, unknown> | undefined)?.[name];
  return (m[name] ?? d[name] ?? fromGlobal) as T | undefined;
}

/** xterm's canvas cannot read CSS variables: resolve the theme tokens once. */
function themeFromTokens(): Record<string, string> {
  const css = getComputedStyle(document.documentElement);
  const token = (name: string) => css.getPropertyValue(name).trim();
  return {
    background: token("--field"),
    foreground: token("--text"),
    cursor: token("--text-dim"),
    selectionBackground: token("--accent-soft"),
  };
}

/** The vendored constructors, whichever way the UMD bundles exposed them. */
export async function loadXtermModules(): Promise<{
  Terminal: Ctor<XtermInstance>;
  FitAddon: Ctor<{ fit(): void }>;
}> {
  const [xterm, fit] = await Promise.all([
    import("@herdeck-web/xterm.js"),
    import("@herdeck-web/addon-fit.js"),
    import("@herdeck-web/xterm.css"),
  ]);
  const Terminal = pick<Ctor<XtermInstance>>(xterm, "Terminal");
  const FitAddon = pick<Ctor<{ fit(): void }>>(fit, "FitAddon");
  if (typeof Terminal !== "function" || typeof FitAddon !== "function") {
    throw new Error("xterm.js did not load");
  }
  return { Terminal, FitAddon };
}

export const createXterm: TerminalFactory = async (host) => {
  const { Terminal, FitAddon } = await loadXtermModules();
  const TerminalCtor = Terminal as unknown as new (options: object) => XtermInstance;
  const term = new TerminalCtor({
    disableStdin: true, // a preview: typing goes through the card's reply box
    cursorBlink: false,
    scrollback: 1000,
    fontSize: 11,
    lineHeight: 1.15,
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim(),
    theme: themeFromTokens(),
  });
  const fitter = new (FitAddon as unknown as new () => { fit(): void })();
  term.loadAddon(fitter);
  term.open(host);
  return {
    get cols() {
      return term.cols;
    },
    get rows() {
      return term.rows;
    },
    write: (bytes) => term.write(bytes),
    reset: () => term.reset(),
    resize: (cols, rows) => term.resize(cols, rows),
    fit: () => fitter.fit(),
    dispose: () => term.dispose(),
  };
};

/** base64 frame payload -> bytes for `write`. */
export function frameBytes(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
