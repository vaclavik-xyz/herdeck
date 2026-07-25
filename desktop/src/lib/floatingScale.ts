export const FLOATING_SCALE_STORAGE_KEY = "herdeck.floatingScale";
export const MIN_FLOATING_SCALE = 0.8;
export const MAX_FLOATING_SCALE = 1.4;

export type FloatingScaleCommand = "in" | "out" | "reset";

interface ScaleReader {
  getItem(key: string): string | null;
}

interface ScaleWriter {
  setItem(key: string, value: string): unknown;
}

interface ScaleShortcut {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
}

interface Point {
  x: number;
  y: number;
}

interface Size {
  width: number;
  height: number;
}

interface Rect extends Point, Size {}

export function readFloatingScale(storage: ScaleReader): number {
  const value = Number(storage.getItem(FLOATING_SCALE_STORAGE_KEY));
  return Number.isFinite(value) && value > 0 ? boundedScale(value) : 1;
}

function boundedScale(value: number): number {
  return Math.min(MAX_FLOATING_SCALE, Math.max(MIN_FLOATING_SCALE, value));
}

export function writeFloatingScale(storage: ScaleWriter, scale: number): number {
  const value = Number.isFinite(scale) ? boundedScale(scale) : 1;
  storage.setItem(FLOATING_SCALE_STORAGE_KEY, String(value));
  return value;
}

export function changeFloatingScale(
  current: number,
  command: FloatingScaleCommand,
): number {
  if (command === "reset") return 1;
  const delta = command === "in" ? 0.1 : -0.1;
  const stepped = Math.round((current + delta) * 10) / 10;
  return boundedScale(stepped);
}

export function floatingScaleCommandForKey(
  event: ScaleShortcut,
): FloatingScaleCommand | null {
  if ((!event.metaKey && !event.ctrlKey) || event.altKey) return null;
  if (event.key === "+" || event.key === "=") return "in";
  if (event.key === "-") return "out";
  if (event.key === "0") return "reset";
  return null;
}

export function anchoredFloatingPosition(
  position: Point,
  previousSize: Size,
  nextSize: Size,
  monitor: Rect,
  edgeThreshold = 32,
): Point {
  const right = monitor.x + monitor.width;
  const bottom = monitor.y + monitor.height;
  const rightGap = right - (position.x + previousSize.width);
  const bottomGap = bottom - (position.y + previousSize.height);
  const anchoredX = Math.abs(rightGap) <= edgeThreshold
    ? right - nextSize.width - rightGap
    : position.x;
  const anchoredY = Math.abs(bottomGap) <= edgeThreshold
    ? bottom - nextSize.height - bottomGap
    : position.y;
  const maxX = Math.max(monitor.x, right - nextSize.width);
  const maxY = Math.max(monitor.y, bottom - nextSize.height);

  return {
    x: Math.round(Math.min(maxX, Math.max(monitor.x, anchoredX))),
    y: Math.round(Math.min(maxY, Math.max(monitor.y, anchoredY))),
  };
}
