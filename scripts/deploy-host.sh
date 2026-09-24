#!/usr/bin/env bash
# Deploy a committed ref of this checkout to a runtime or bridge host that runs
# from source, restart its herdeck-service unit, and prove it came back healthy.
#
# Usage:
#   scripts/deploy-host.sh --role runtime|bridge [options]
#
# Options:
#   --role ROLE         runtime (the deck host: herdeck.runtime, installs .[deck])
#                       or bridge (the agents host: herdeck.bridge, installs .)
#   --host SSH_HOST     deploy over ssh to this host; omit to deploy on this machine
#   --ref REF           git ref to deploy; its committed state, never the working
#                       tree (default: HEAD)
#   --root DIR          deployment root on the target, relative paths are under the
#                       target's $HOME (default: herdeck-deploy). Layout:
#                         DIR/releases/<sha>/   one immutable snapshot per commit
#                         DIR/current           symlink to the active snapshot
#                         DIR/previous          symlink to the one before (rollback)
#                         DIR/venv              the venv the service unit runs from
#   --venv DIR          venv the unit runs from (default: DIR/venv)
#   --python PY         interpreter that creates a missing venv (default: python3)
#   --health-timeout S  seconds to wait for the restarted service (default: 30)
#   --bridge-addr H:P   bridge address to probe (default: the unit's
#                       HERDECK_BIND/HERDECK_PORT)
#   --keep N            snapshots to keep, besides current and previous (default: 3)
#   --rollback          re-activate DIR/previous instead of deploying a ref
#   -h, --help          show this help
#
# The service unit is installed once, by hand, from the venv this script fills:
#   DIR/venv/bin/herdeck-service install runtime --config ~/.config/herdeck/config.toml
#   DIR/venv/bin/herdeck-service install bridge --bind <tailscale-ip> --server-id <id>
# The first deploy stops after installing and prints that command. Re-running
# the same deploy is safe: an existing snapshot is reused, pip is re-run, and the
# unit is restarted and health-checked again.
#
# Exit codes: 0 healthy; 1 failed (the message names the rollback command);
# 2 usage; 3 installed but no service unit to restart yet.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

die_usage() {
  echo "deploy-host: $*" >&2
  echo "run with --help for usage" >&2
  exit 2
}

# ---------------------------------------------------------------------------
# Everything below target_main runs ON THE TARGET (locally, or via `ssh bash -s`),
# so it must stay self-contained: no references to the local checkout.
# ---------------------------------------------------------------------------
target_main() {
  local role=$1 sha=$2 root=$3 venv=$4 python=$5 timeout=$6 bridge_addr=$7 keep=$8
  local rollback=$9 rollback_hint=${10}
  local label="dev.herdeck.$role" os uid
  os=$(uname -s)
  uid=$(id -u)

  say() { echo "deploy-host[$(hostname -s 2>/dev/null || hostname)]: $*"; }
  fail() {
    echo "deploy-host: FAILED: $*" >&2
    if [[ -n ${upload:-} ]]; then rm -f "$upload"; fi
    if [[ -n ${previous_release:-} ]]; then
      echo "deploy-host: roll back with: $rollback_hint" >&2
    fi
    exit 1
  }
  abspath() {
    case $1 in
      \~) echo "$HOME" ;;
      \~/*) echo "$HOME/${1#\~/}" ;;
      /*) echo "$1" ;;
      *) echo "$HOME/$1" ;;
    esac
  }

  root=$(abspath "$root")
  if [[ -z $venv ]]; then venv="$root/venv"; else venv=$(abspath "$venv"); fi
  local releases="$root/releases" upload="$HOME/.herdeck-deploy-$sha.tar"
  mkdir -p "$releases"

  # --- the unit this deploy restarts ------------------------------------------
  local unit_file="" unit_program="" unit_domain=""
  if [[ $os == Darwin ]]; then
    if [[ -f "$HOME/Library/LaunchAgents/$label.plist" ]]; then
      unit_file="$HOME/Library/LaunchAgents/$label.plist"
      if [[ $role == runtime ]]; then unit_domain="gui/$uid"; else unit_domain="user/$uid"; fi
    elif [[ -f "/Library/LaunchDaemons/$label.plist" ]]; then
      unit_file="/Library/LaunchDaemons/$label.plist"
      unit_domain="system"
    fi
    if [[ -n $unit_file ]]; then
      unit_program=$(plutil -extract ProgramArguments.0 raw -o - "$unit_file" 2>/dev/null || true)
    fi
  else
    unit_file="$HOME/.config/systemd/user/herdeck-$role.service"
    if [[ -f $unit_file ]]; then
      unit_program=$(systemctl --user show -p ExecStart --value "herdeck-$role.service" 2>/dev/null \
        | sed -n 's/^{ path=\([^ ;]*\).*/\1/p' | head -n 1)
    else
      unit_file=""
    fi
  fi
  # Refuse to restart a unit that does not run from this venv: an app-bundled
  # runtime (herdeck-service --from-app) or another checkout would restart
  # unchanged and "pass" the health check with code this deploy never touched.
  if [[ -n $unit_file && $(dirname "$unit_program") != "$venv/bin" ]]; then
    fail "$unit_file runs '${unit_program:-?}', not $venv/bin/python; pass --venv, or reinstall the unit from this venv"
  fi

  # --- pick the snapshot to activate --------------------------------------------
  local current_release="" previous_release="" target
  current_release=$(readlink "$root/current" 2>/dev/null || true)
  if [[ $rollback == 1 ]]; then
    rm -f "$upload"
    target=$(readlink "$root/previous" 2>/dev/null || true)
    [[ -n $target && -d $target ]] || fail "nothing to roll back to: $root/previous is missing"
    say "rolling back $role: $(basename "$current_release") -> $(basename "$target")"
  else
    target="$releases/$sha"
    if [[ -d $target ]]; then
      say "snapshot $sha already present, reusing it"
      rm -f "$upload"
    else
      local incoming="$releases/.incoming-$sha"
      rm -rf "$incoming"
      mkdir -p "$incoming"
      tar -x -f "$upload" -C "$incoming" || fail "could not unpack the snapshot"
      rm -f "$upload"
      mv "$incoming" "$target"
      say "unpacked snapshot $sha"
    fi
  fi
  if [[ -n $current_release && $current_release != "$target" ]]; then
    previous_release=$current_release
  fi

  # --- install into the venv ----------------------------------------------------
  if [[ ! -x "$venv/bin/python" ]]; then
    say "creating venv $venv with $python"
    "$python" -m venv "$venv" || fail "could not create $venv"
  fi
  local spec="$target"
  [[ $role == runtime ]] && spec="${target}[deck]"
  say "pip install -e $spec"
  "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$spec" \
    || fail "pip install into $venv failed (the running service still uses the old code until restarted)"

  # --- switch symlinks (previous first, so a failure never loses the rollback) ---
  if [[ -n $previous_release ]]; then
    ln -sfn "$previous_release" "$root/previous"
  fi
  ln -sfn "$target" "$root/current"

  if [[ -z $unit_file ]]; then
    say "installed $(basename "$target") into $venv, but no herdeck-service $role unit exists yet."
    if [[ $role == runtime ]]; then
      say "install it once: $venv/bin/herdeck-service install runtime --config ~/.config/herdeck/config.toml"
    else
      say "install it once: $venv/bin/herdeck-service install bridge --bind <tailscale-ip> --server-id <id>"
    fi
    say "then re-run this deploy to restart and health-check it"
    exit 3
  fi

  # --- restart --------------------------------------------------------------------
  local since
  since=$(date +%s)
  if [[ $os == Darwin ]]; then
    if [[ $unit_domain == system ]]; then
      say "sudo launchctl kickstart -k system/$label"
      sudo -n launchctl kickstart -k "system/$label" \
        || fail "could not restart system/$label (needs passwordless sudo for launchctl)"
    else
      say "launchctl kickstart -k $unit_domain/$label"
      launchctl kickstart -k "$unit_domain/$label" || fail "launchctl kickstart failed for $label"
    fi
  else
    say "systemctl --user restart herdeck-$role.service"
    systemctl --user restart "herdeck-$role.service" || fail "systemctl restart failed for herdeck-$role"
  fi

  # --- health ---------------------------------------------------------------------
  local log_hint="$HOME/Library/Logs/herdeck-$role.log"
  [[ $os != Darwin ]] && log_hint="journalctl --user -u herdeck-$role"
  if [[ $role == runtime ]]; then
    local runtime_health_py='
import json, os, sys, time, urllib.parse, urllib.request

since, deadline = float(sys.argv[1]), time.time() + float(sys.argv[2])
base = os.environ.get("HERDECK_RUNTIME_DIR") or os.path.expanduser("~/.cache/herdeck")
path = os.path.join(base, "runtime.json")
last = f"{path} was not rewritten by the restarted runtime"
while time.time() < deadline:
    try:
        if os.stat(path).st_mtime >= since:
            with open(path, encoding="utf-8") as handle:
                info = json.load(handle)
            url = info["url"]
            query = urllib.parse.urlencode({"token": info["token"]})
            with urllib.request.urlopen(f"{url}/health?{query}", timeout=3) as response:
                health = json.load(response)
            if health.get("ok"):
                state = "connected to its bridge" if health.get("connected") else (
                    "NOT connected to a bridge yet (check the bridge host)")
                print(f"deploy-host: runtime healthy at {url}, {state}")
                sys.exit(0)
            last = f"/health answered {health}"
    except (OSError, ValueError, KeyError) as error:
        last = str(error)
    time.sleep(0.5)
print(f"deploy-host: runtime health check failed: {last}", file=sys.stderr)
sys.exit(1)
'
    "$venv/bin/python" -c "$runtime_health_py" "$since" "$timeout" || fail "runtime is not healthy; see $log_hint"
  else
    if [[ -z $bridge_addr ]]; then
      local bind="" port=""
      if [[ $os == Darwin ]]; then
        bind=$(plutil -extract EnvironmentVariables.HERDECK_BIND raw -o - "$unit_file" 2>/dev/null || true)
        port=$(plutil -extract EnvironmentVariables.HERDECK_PORT raw -o - "$unit_file" 2>/dev/null || true)
      else
        local env
        env=$(systemctl --user show -p Environment --value "herdeck-$role.service" | tr ' ' '\n')
        bind=$(sed -n 's/^HERDECK_BIND=//p' <<<"$env")
        port=$(sed -n 's/^HERDECK_PORT=//p' <<<"$env")
      fi
      bridge_addr="${bind:-127.0.0.1}:${port:-8788}"
    fi
    local bridge_health_py='
import socket, sys, time

host, _, port = sys.argv[1].rpartition(":")
deadline, last = time.time() + float(sys.argv[2]), "no attempt"
time.sleep(1)  # let the killed process release the port before probing
while time.time() < deadline:
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            print(f"deploy-host: bridge accepting connections on {host}:{port}")
            sys.exit(0)
    except OSError as error:
        last = str(error)
    time.sleep(0.5)
print(f"deploy-host: bridge health check failed on {host}:{port}: {last}", file=sys.stderr)
sys.exit(1)
'
    "$venv/bin/python" -c "$bridge_health_py" "$bridge_addr" "$timeout" || fail "bridge is not reachable; see $log_hint"
  fi

  # --- prune (never current or previous) ----------------------------------------------
  local keep_current keep_previous old
  keep_current=$(readlink "$root/current" || true)
  keep_previous=$(readlink "$root/previous" 2>/dev/null || true)
  # newest first by mtime; skip the first $keep that are neither current nor previous
  local kept=0
  while IFS= read -r old; do
    [[ -z $old ]] && continue
    old="$releases/$old"
    [[ $old == "$keep_current" || $old == "$keep_previous" ]] && continue
    if (( kept < keep )); then
      kept=$((kept + 1))
      continue
    fi
    rm -rf "$old"
  done < <(ls -t "$releases" 2>/dev/null)

  say "deployed $(basename "$target") ($role); previous: $(basename "${previous_release:-none}")"
}

# ---------------------------------------------------------------------------
# Local side: parse flags, snapshot the ref, hand the snapshot to the target.
# ---------------------------------------------------------------------------
role="" host="" ref="HEAD" root="herdeck-deploy" venv="" python="python3"
timeout=30 bridge_addr="" keep=3 rollback=0
while (($#)); do
  case $1 in
    --role) role=${2:?}; shift 2 ;;
    --host) host=${2:?}; shift 2 ;;
    --ref) ref=${2:?}; shift 2 ;;
    --root) root=${2:?}; shift 2 ;;
    --venv) venv=${2:?}; shift 2 ;;
    --python) python=${2:?}; shift 2 ;;
    --health-timeout) timeout=${2:?}; shift 2 ;;
    --bridge-addr) bridge_addr=${2:?}; shift 2 ;;
    --keep) keep=${2:?}; shift 2 ;;
    --rollback) rollback=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done
[[ $role == runtime || $role == bridge ]] || die_usage "--role must be runtime or bridge"
[[ $timeout =~ ^[0-9]+$ ]] || die_usage "--health-timeout must be a number of seconds"
[[ $keep =~ ^[0-9]+$ ]] || die_usage "--keep must be a number"

sha=""
if [[ $rollback == 0 ]]; then
  repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  sha=$(git -C "$repo" rev-parse --verify "$ref^{commit}") || die_usage "unknown ref: $ref"
fi

# The exact command that undoes this deploy, printed by the target on failure.
rollback_hint="scripts/deploy-host.sh --role $role --root $root --rollback"
[[ -n $host ]] && rollback_hint+=" --host $host"
[[ -n $venv ]] && rollback_hint+=" --venv $venv"

args=("$role" "${sha:-rollback}" "$root" "$venv" "$python" "$timeout" "$bridge_addr" "$keep"
  "$rollback" "$rollback_hint")
upload_name=".herdeck-deploy-${sha:-rollback}.tar"

if [[ -n $host ]]; then
  if [[ $rollback == 0 ]]; then
    echo "deploy-host: sending $ref ($sha) to $host"
    # shellcheck disable=SC2029  # upload_name is meant to expand client-side
    git -C "$repo" archive --format=tar "$sha" | ssh "$host" "cat > $upload_name"
  fi
  # shellcheck disable=SC2029  # the quoted args are meant to expand client-side
  { echo 'set -euo pipefail'; declare -f target_main; echo 'target_main "$@"'; } \
    | ssh "$host" "bash -s -- $(printf '%q ' "${args[@]}")"
else
  if [[ $rollback == 0 ]]; then
    echo "deploy-host: snapshotting $ref ($sha)"
    git -C "$repo" archive --format=tar "$sha" >"$HOME/$upload_name"
  fi
  target_main "${args[@]}"
fi
