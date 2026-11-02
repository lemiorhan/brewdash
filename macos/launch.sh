#!/bin/bash
# brewdash.app's side of the server.
#   launch.sh open <app pid>   print the dashboard URL, starting a server first if this app has none
#   launch.sh stop             stop the server the app started
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
state="${XDG_STATE_HOME:-$HOME/.local/state}/brewdash"
current="$state/app-server"  # "<server pid> <app pid> <url>"
log="$state/brewdash.log"

# The file outlives a server that crashed or was stopped with its app, and its pid may belong to another
# process by now; only this app's own server counts.
running_pid() {
  local pid
  pid="$(cut -d' ' -f1 "$current" 2>/dev/null)" || return 1
  if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -qF "$here/brewdash.py"; then
    echo "$pid"
  else
    return 1
  fi
}

find_python() {
  local p
  for p in python3 /usr/bin/python3; do
    if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
      command -v "$p"
      return
    fi
  done
  echo "brewdash needs Python 3.9 or newer. Install the Command Line Tools: xcode-select --install" >&2
  return 1
}

start() {
  # Finder and the Dock start apps with PATH=/usr/bin:/bin:/usr/sbin:/sbin, where brew is not found and
  # brew doctor reports problems the user's own shell does not have. Whatever .zprofile prints comes
  # before the last line.
  local login_path
  login_path="$("${SHELL:-/bin/zsh}" -l -c 'printf "\n%s" "$PATH"' </dev/null 2>/dev/null | tail -n 1 || true)"
  [ -n "$login_path" ] && export PATH="$login_path"
  local python pid url
  python="$(find_python)"
  mkdir -p "$state"
  chmod 700 "$state"
  # The log and the state file carry the dashboard token. The umask stays in these subshells: the server
  # and the brew commands it runs keep the user's own.
  rm -f "$log" "$current"
  (umask 077 && : >"$log")
  nohup "$python" "$here/brewdash.py" --no-browser --parent "$1" </dev/null >"$log" 2>&1 &
  pid=$!
  for _ in $(seq 100); do
    url="$(grep -Eo 'http://127\.0\.0\.1:[0-9]+/\?t=[A-Za-z0-9_-]+' "$log" | head -1 || true)"
    if [ -n "$url" ]; then
      (umask 077 && echo "$pid $1 $url" >"$current")
      echo "$url"
      return
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  echo "Its log, $log, ends with:" >&2
  tail -n 15 "$log" >&2
  return 1
}

case "${1:-}" in
  open)
    app="${2:?app pid}"
    # The server of an app that was force-quit stops itself within moments, so it is not handed out again.
    if pid="$(running_pid)"; then
      if [ "$(cut -d' ' -f2 "$current")" = "$app" ]; then
        cut -d' ' -f3 "$current"
        exit
      fi
      kill "$pid"
    fi
    start "$app" ;;
  stop)
    pid="$(running_pid)" || pid=""
    rm -f "$current"
    [ -z "$pid" ] || kill "$pid" ;;
  *)
    echo "usage: launch.sh open <app pid> | stop" >&2
    exit 2 ;;
esac
