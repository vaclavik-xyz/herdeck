// installed by herdeck (herdeck-service hooks install --agents opencode)
// HERDECK_INTEGRATION=subagents
// HERDECK_INTEGRATION_VERSION=1
// Managed by herdeck: installing again overwrites this file and uninstalling
// removes it. Keep your own plugins in separate files beside it.
//
// Reports OpenCode child sessions (subagents: sessions with a parentID) to
// herdeck-subagent-hook, which keeps the per-pane spool behind the deck's
// "⑂N" badge. Only runs inside a herdr pane (HERDR_PANE_ID); never awaits the
// hook, so OpenCode is never slowed down by it.

import { spawn } from "node:child_process";

// Replaced with the hook's absolute path (a JSON string) at install time.
const HOOK = "__HERDECK_SUBAGENT_HOOK__";
// A busy child re-reports at most this often (keeps its entry from going stale).
const HEARTBEAT_MS = 30_000;
const MAX_TRACKED = 512;
const MAX_DEPTH = 64;

const parents = new Map(); // child session id -> parent session id
const titles = new Map(); // child session id -> title
const lastBeat = new Map(); // child session id -> ms of the last heartbeat
const idle = new Set(); // children whose idle was reported (status + idle event)

function remember(map, key, value) {
  map.delete(key);
  map.set(key, value);
  while (map.size > MAX_TRACKED) {
    map.delete(map.keys().next().value);
  }
}

function lineage(id) {
  let root = id;
  let depth = 0;
  while (parents.has(root) && depth < MAX_DEPTH) {
    root = parents.get(root);
    depth += 1;
  }
  return { root, depth };
}

function send(payload) {
  try {
    const child = spawn(HOOK, ["--provider", "opencode"], {
      stdio: ["pipe", "ignore", "ignore"],
      env: process.env,
    });
    child.on("error", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
    child.unref();
  } catch {
    // A missing or broken hook must never disturb OpenCode.
  }
}

function report(eventName, sessionID, extra = {}) {
  const { root, depth } = lineage(sessionID);
  send({
    hook_event_name: eventName,
    session_id: sessionID,
    parent_id: parents.get(sessionID) ?? "",
    root_session_id: root === sessionID ? "" : root,
    depth,
    title: titles.get(sessionID) ?? "",
    ...extra,
  });
}

function reportIdle(sessionID) {
  lastBeat.delete(sessionID);
  if (idle.has(sessionID)) {
    return;
  }
  idle.add(sessionID);
  while (idle.size > MAX_TRACKED) {
    idle.delete(idle.values().next().value);
  }
  report("session.idle", sessionID);
}

function statusKind(status) {
  const kind = typeof status === "string" ? status : status?.type;
  return typeof kind === "string" ? kind.toLowerCase() : "";
}

export const HerdeckSubagentsPlugin = async () => {
  if (!process.env.HERDR_PANE_ID) {
    return {};
  }
  return {
    event: async ({ event }) => {
      const type = event?.type;
      const properties = event?.properties ?? {};
      const info = properties.info;

      if (type === "session.created" || type === "session.updated") {
        if (typeof info?.id !== "string" || typeof info.parentID !== "string" || !info.parentID) {
          return;
        }
        const known = parents.has(info.id);
        remember(parents, info.id, info.parentID);
        if (typeof info.title === "string") {
          remember(titles, info.id, info.title);
        }
        if (!known) {
          report("session.created", info.id);
        }
        return;
      }
      if (type === "session.deleted") {
        if (typeof info?.id === "string" && parents.has(info.id)) {
          report("session.deleted", info.id);
          parents.delete(info.id);
          titles.delete(info.id);
          lastBeat.delete(info.id);
          idle.delete(info.id);
        }
        return;
      }

      const sessionID = typeof properties.sessionID === "string" ? properties.sessionID : "";
      if (!sessionID || !parents.has(sessionID)) {
        return;
      }
      if (type === "session.status") {
        const kind = statusKind(properties.status);
        if (kind === "idle") {
          reportIdle(sessionID);
        } else if (kind) {
          idle.delete(sessionID);
          const now = Date.now();
          if (now - (lastBeat.get(sessionID) ?? 0) >= HEARTBEAT_MS) {
            remember(lastBeat, sessionID, now);
            report("session.status", sessionID, { status: kind });
          }
        }
      } else if (type === "session.idle") {
        reportIdle(sessionID);
      } else if (type === "session.error") {
        report("session.error", sessionID);
      }
    },
  };
};

// OpenCode V1 calls server(); V2's shared server has no pane environment, so
// there is nothing to report from setup().
export default {
  id: "herdeck.subagents",
  server: HerdeckSubagentsPlugin,
  setup() {},
};
