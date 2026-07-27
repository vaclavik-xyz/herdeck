import defaults from "./configDefaults.json";

export const DEFAULT_STATUS_COLORS: Record<string, string> = { ...defaults.theme.colors };
export const DEFAULT_SERVER_ACCENTS: string[] = [...defaults.theme.server_accents];

// The USER-ASSIGNABLE subset of the backend named tile palette
// (src/herdeck/driver/base.py COLORS) — keep in sync. The backend's 13th entry,
// "empty" (32,32,36), is deliberately absent: it paints vacant slots, not agent
// state, and offering it in the picker would let a status render as an empty
// key. A config that names it still resolves on the backend; the window
// degrades it to dim like any other name outside this subset. Status colours resolve STRICTLY through this palette on the
// backend (COLORS.get(name, dim)): a typo silently renders as the empty-tile
// grey, so the editor offers these as a picker with swatches instead of free
// text.
export const PALETTE: Record<string, string> = {
  green: "rgb(40,180,70)",
  blue: "rgb(50,120,220)",
  amber: "rgb(230,170,20)",
  cyan: "rgb(45,200,215)",
  dim: "rgb(70,70,70)",
  red: "rgb(210,50,50)",
  grey: "rgb(120,120,120)",
  teal: "rgb(24,150,145)",
  violet: "rgb(135,100,235)",
  orange: "rgb(220,115,35)",
  pink: "rgb(215,80,135)",
  lime: "rgb(125,175,45)",
};

export const PALETTE_NAMES: string[] = Object.keys(PALETTE);

/** CSS colour for a palette name or #rrggbb literal (server accents allow hex). */
export function swatchColor(value: string): string {
  if (/^#[0-9a-fA-F]{6}$/.test(value)) return value;
  // hasOwn, not `?? `: PALETTE["constructor"] would otherwise return a function.
  return Object.hasOwn(PALETTE, value) ? PALETTE[value] : "transparent";
}
