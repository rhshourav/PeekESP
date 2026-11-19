#!/bin/sh
# shellcheck shell=sh
# PEEKESP_SYNOLOGY_INSTALLER  <- the updater greps for this, so a downloaded 404 page is never installed
#
# install.sh - installer and day-to-day command line for the PeekESP Synology agent.
#
#   curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/main/synology/install.sh | sudo sh
#   sudo sh install.sh --code K7M2-P4QX-9R        (from a downloaded copy of this folder)
#
# One file, two jobs. Run as install.sh it installs: the agent goes to
# /usr/local/peekesp/, this very script goes beside it as `peekesp`, and a link in
# /usr/local/bin puts it on your PATH. Run as `peekesp` it is the control panel:
#
#   peekesp status | logs [-f] | test | doctor | show
#   peekesp pair CODE
#   peekesp push on|off          the data-sending part (relay)
#   peekesp serve on|off         the widget feed (LAN)
#   peekesp set NAME VALUE       interval, port, name, allow, bind, relay, ...
#   peekesp start | stop | restart | autostart on|off | update | uninstall [--purge]
#
# Commands that change anything need root; `peekesp` asks sudo for it itself, so you
# do not have to type sudo. (If you do, and DSM answers "command not found", that is
# DSM's sudo leaving /usr/local/bin off its PATH - just drop the `sudo`.)
#
# WHY A SHELL SUPERVISOR AND NOT A SERVICE
#
# DSM 6 used upstart and DSM 7 moved to a systemd-based init, but it is Synology's
# private one: it is rebuilt by every DSM update, and a unit dropped into it by
# hand is neither supported nor reliably still there after the next update. The
# supported way to run your own code on a DSM box is Task Scheduler (or packaging an
# SPK), and Task Scheduler only *starts* things: nothing restarts a process that
# dies. So `start` launches a tiny supervisor loop (the `supervise` command below)
# that runs the agent and restarts it with a growing delay if it ever exits. Task
# Scheduler then only has to start the supervisor at boot, and `start` is safe to
# run any number of times - a second one finds the first and leaves.
#
# WHY NOTHING HERE WRITES TO A VOLUME
#
# The agent and its settings live under /usr/local, on DSM's system partition. The
# status file and the log live in /var/run, which is RAM. Drive hibernation only
# works if nothing touches the volumes, and a monitor that logs to /volume1 every
# five seconds would be the thing that keeps your disks spinning.
#
# Plain POSIX sh, for BusyBox ash: no arrays, no [[ ]], no `local`, no `echo -e`.
#
# PeekESP by rhshourav  -  Discord: rhshourav
#   repo  https://github.com/rhshourav/PeekESP
#   site  https://rhshourav.github.io/PeekESP/

# Appended, not prepended: Task Scheduler runs us with a skeleton PATH and needs the
# standard directories added, but a PATH someone chose on purpose keeps its order.
PATH="$PATH:/usr/syno/sbin:/usr/syno/bin:/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin"
export PATH

VERSION="2.0.1"
AUTHOR="rhshourav"
DISCORD="rhshourav"
REPO="https://github.com/rhshourav/PeekESP"
SITE="https://rhshourav.github.io/PeekESP/"
RAW="${PEEK_RAW:-https://raw.githubusercontent.com/rhshourav/PeekESP/${PEEK_REF:-main}/synology}"

# Every location can be overridden, which is how the installer is tested on a box
# that is not a Synology. On a NAS you never set these.
PREFIX="${PEEK_PREFIX:-/usr/local/peekesp}"
ETC="${PEEK_ETC:-/usr/local/etc/peekesp}"
BINLINK="${PEEK_BIN:-/usr/local/bin/peekesp}"
RCD="${PEEK_RCD:-/usr/local/etc/rc.d}"
RUN="${PEEK_RUN:-/var/run}"

AGENT="$PREFIX/peek-agent-synology.py"
CTL="$PREFIX/peekesp"
PYFILE="$PREFIX/python"
HOOK="$RCD/S99peekesp.sh"
PIDFILE="$RUN/peekesp.pid"
LOG="$RUN/peekesp.log"
CRASH="$RUN/peekesp.crash"
STATUS="$RUN/peekesp.status.json"

# ---------------------------------------------------------------- output
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ]; then
  ESC=$(printf '\033')
  C_OK="${ESC}[32m"; C_WARN="${ESC}[33m"; C_ERR="${ESC}[31m"
  C_DIM="${ESC}[2m"; C_B="${ESC}[1m"; C_0="${ESC}[0m"
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_B=""; C_0=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %sok%s     %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '  %swarn%s   %s\n' "$C_WARN" "$C_0" "$*"; }
note() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "supervisor: $*" >> "$LOG" 2>/dev/null; }
die()  { printf '%serror%s: %s\n' "$C_ERR" "$C_0" "$*" >&2; exit 1; }

credits() {
  say ""
  say "${C_DIM}PeekESP by ${AUTHOR}  -  Discord: ${DISCORD}${C_0}"
  say "${C_DIM}repo  ${REPO}${C_0}"
  say "${C_DIM}site  ${SITE}${C_0}"
}

# ---------------------------------------------------------------- self
case "$0" in
  /*) SELF="$0" ;;
  *)  SELF="$(pwd)/$0" ;;
esac
[ -f "$SELF" ] || SELF=""
SELFDIR=""
[ -n "$SELF" ] && SELFDIR=$(dirname "$SELF")

# Re-run this command as root, through sudo, if we are not already root.
# Called as: as_root "$cmd" "$@"   before the command's own arguments are shifted.
as_root() {
  [ "$(id -u)" = 0 ] && return 0
  if command -v sudo >/dev/null 2>&1; then
    target="$CTL"
    [ -x "$target" ] || target="$SELF"
    [ -n "$target" ] || die "this needs root. Run it as root (sudo -i), then try again."
    say "(needs root - asking sudo)"
    exec sudo sh "$target" "$@"
  fi
  die "this needs root, and sudo is not available. Log in as root, or use an account with sudo."
}

# ---------------------------------------------------------------- python
py_ok() {
  "$1" -c 'import sys
if sys.version_info < (3, 7):
    sys.exit(1)
import ssl, ipaddress, hashlib, json, http.server, urllib.request' >/dev/null 2>&1
}

find_python() {
  # The fast paths first: the glob below walks the volumes, which is fine once at
  # install time and not something to do at every boot.
  for p in "${PEEK_PYTHON:-}" /usr/bin/python3 /bin/python3 /usr/local/bin/python3 \
           "$(command -v python3 2>/dev/null)" \
           /volume*/@appstore/Python3*/usr/bin/python3 \
           /volume*/@appstore/py3k/usr/local/bin/python3; do
    if [ -n "$p" ] && [ -x "$p" ] && py_ok "$p"; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}

no_python_help() {
  say ""
  say "No usable Python 3.7+ was found (it also needs the ssl module)."
  say "  - DSM 7 normally ships /usr/bin/python3. If yours is missing, install"
  say "    'Python 3' from Package Center and run this again."
  say "  - Or skip Python entirely and use the container instead: see synology/README.md"
  say "    (Container Manager path)."
}

need_python() {
  PY=""
  [ -r "$PYFILE" ] && PY=$(cat "$PYFILE" 2>/dev/null)
  if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    PY=$(find_python) || { no_python_help; die "Python 3 not found"; }
    [ -w "$PREFIX" ] && printf '%s\n' "$PY" > "$PYFILE"
  fi
}

agent() { PEEK_CONFIG_DIR="$ETC" PEEK_RUN_DIR="$RUN" "$PY" "$AGENT" "$@"; }

# ---------------------------------------------------------------- helpers
dsm_version() {
  [ -r /etc.defaults/VERSION ] || return 1
  v=$(sed -n 's/^productversion="\(.*\)"/\1/p' /etc.defaults/VERSION)
  b=$(sed -n 's/^buildnumber="\(.*\)"/\1/p' /etc.defaults/VERSION)
  [ -n "$v" ] || return 1
  printf '%s-%s\n' "$v" "$b"
}

lan_ip() {
  addr=""
  if command -v ip >/dev/null 2>&1; then
    addr=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
  fi
  [ -n "$addr" ] || addr=$(hostname -i 2>/dev/null | awk '{print $1}')
  printf '%s' "${addr:-<nas-ip>}"
}

fetch() {   # fetch URL DEST
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 10 --max-time 90 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 30 -O "$2" "$1"
  else
    return 127
  fi
}

validate_agent() {
  [ -s "$1" ] || return 1
  grep -q PEEKESP_SYNOLOGY_AGENT "$1" || return 1
  "$PY" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$1" 2>/dev/null
}

validate_ctl() {
  [ -s "$1" ] && grep -q PEEKESP_SYNOLOGY_INSTALLER "$1" && sh -n "$1" 2>/dev/null
}

install_file() {   # install_file SRC DEST MODE - atomic, so a running shell keeps its old copy
  cp "$1" "$2.new" && chmod "$3" "$2.new" && mv "$2.new" "$2"
}

# ---------------------------------------------------------------- autostart
hook_present() { [ -x "$HOOK" ]; }

write_hook() {
  mkdir -p "$RCD" || return 1
  cat > "$HOOK.new" <<EOF
#!/bin/sh
# PeekESP agent autostart, written by peekesp. DSM may or may not run rc.d scripts
# at boot (it is not a documented, supported hook on DSM 7); the Task Scheduler
# entry described in synology/README.md is the one that is. Both are harmless
# together, because \`start\` finds a running agent and leaves it alone.
case "\$1" in
  start|"") "$CTL" start --quiet ;;
  stop)     "$CTL" stop ;;
  restart)  "$CTL" restart ;;
esac
exit 0
EOF
  chmod 755 "$HOOK.new" && mv "$HOOK.new" "$HOOK"
}

# found | missing | unknown. `synoschedtask --get` lists tasks; its output format is
# not documented, so this only greps it for our name rather than parsing it.
sched_state() {
  command -v synoschedtask >/dev/null 2>&1 || { echo unknown; return; }
  out=$(synoschedtask --get 2>/dev/null)
  [ -n "$out" ] || { echo unknown; return; }
  if printf '%s' "$out" | grep -qi peekesp; then echo found; else echo missing; fi
}

autostart_report() {
  if hook_present; then h="rc.d hook installed"; else h="rc.d hook not installed"; fi
  s=$(sched_state)
  case "$s" in
    found)   t="Task Scheduler entry found" ;;
    missing) t="Task Scheduler entry NOT found" ;;
    *)       t="Task Scheduler entry: cannot tell from here" ;;
  esac
  say "  autostart     $h; $t"
  if [ "$s" != found ]; then
    say "                to survive a reboot, add the Task Scheduler entry (peekesp autostart)"
  fi
}

scheduler_howto() {
  say ""
  say "${C_B}Survive a reboot${C_0}  (one minute, once - DSM has no supported way to do this from a script)"
  say "  DSM > Control Panel > Task Scheduler > Create > Triggered Task > User-defined script"
  say "    General:        Task: PeekESP     User: root     Event: Boot-up     Enabled"
  say "    Task Settings:  User-defined script:   $CTL start --quiet"
  say "  Optional self-heal: a second, Scheduled Task every 10 minutes running the same"
  say "  command. It is idempotent, so it costs nothing and covers a killed supervisor."
}

# ---------------------------------------------------------------- the supervisor
sup_pid() {
  [ -r "$PIDFILE" ] || return 1
  p=$(cat "$PIDFILE" 2>/dev/null)
  case "$p" in ''|*[!0-9]*) return 1 ;; esac
  [ -r "/proc/$p/cmdline" ] || return 1
  # A pid in a pidfile can be reused by something else after a crash; only
  # believe it if the process really is one of ours.
  tr '\0' ' ' < "/proc/$p/cmdline" | grep -q 'supervise' || return 1
  printf '%s\n' "$p"
}

agent_pid() {   # the agent's own pid, from its status file
  [ -r "$STATUS" ] || return 1
  p=$(sed -n 's/.*"pid": \([0-9][0-9]*\).*/\1/p' "$STATUS" | head -n 1)
  [ -n "$p" ] && [ -r "/proc/$p/cmdline" ] || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" | grep -q 'peek-agent-synology' || return 1
  printf '%s\n' "$p"
}

# Of two supervisors started at the same instant (the boot hook and Task Scheduler
# both call `start`), exactly one must win. The pid is written to a private file
# first and then hard-linked into place: ln fails if the target exists, and the
# target appears already complete. (Creating the pidfile and then writing into it
# leaves a window in which a rival reads it empty, calls it stale, deletes it and
# takes the lock too - which is the first thing this replaced.)
# The agent takes its own kernel flock as a second layer, so even a lost race here
# cannot leave two agents running: the second one exits with status 3.
take_lock() {
  mine="$PIDFILE.$$"
  printf '%s\n' "$$" > "$mine" 2>/dev/null || return 1
  i=0
  while [ "$i" -lt 3 ]; do
    if ln "$mine" "$PIDFILE" 2>/dev/null; then
      rm -f "$mine"
      return 0
    fi
    if sup_pid >/dev/null; then
      rm -f "$mine"
      return 1
    fi
    rm -f "$PIDFILE"                       # held by a process that is gone
    i=$((i + 1))
  done
  rm -f "$mine"
  return 1
}

cmd_supervise() {
  mkdir -p "$RUN" 2>/dev/null
  take_lock || exit 0
  # Armed straight after the lock is ours, so no later failure leaves a stale pidfile.
  trap 'rm -f "$PIDFILE"' EXIT
  need_python
  : > "$CRASH" 2>/dev/null
  child=""
  stopping=0
  trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT
  trap '[ -n "$child" ] && kill -HUP "$child" 2>/dev/null' HUP

  NICE=""
  command -v nice >/dev/null 2>&1 && NICE="nice -n 10"       # DSM comes first
  delay=2
  note "started (python $PY)"
  while [ "$stopping" = 0 ]; do
    began=$(date +%s)
    # shellcheck disable=SC2086
    PEEK_CONFIG_DIR="$ETC" PEEK_RUN_DIR="$RUN" $NICE "$PY" -u "$AGENT" --log-file "$LOG" 2>>"$CRASH" &
    child=$!
    wait "$child"
    rc=$?
    # wait returns early when a trapped signal arrives; keep waiting for the real exit.
    while kill -0 "$child" 2>/dev/null; do
      wait "$child"
      rc=$?
    done
    child=""
    [ "$stopping" = 1 ] && break
    if [ "$rc" = 3 ]; then
      # The agent found another one already holding its lock. Restarting it in a
      # loop would be pointless; the other one is doing the job.
      note "another agent is already running; this supervisor is redundant and is exiting"
      break
    fi
    up=$(( $(date +%s) - began ))
    note "agent exited with status $rc after ${up}s; restarting in ${delay}s"
    # Interruptible, so `stop` is not held up by a minute of backoff.
    sleep "$delay" &
    child=$!
    wait "$child"
    child=""
    if [ "$up" -ge 60 ]; then
      delay=2
    else
      delay=$((delay * 2))
      [ "$delay" -gt 60 ] && delay=60
    fi
  done
  note "stopped"
  exit 0
}

# ---------------------------------------------------------------- start / stop
# An agent whose supervisor is gone (the supervisor was kill -9'd, or died with an
# OOM) is still alive, still holds the agent's lock, and would make a fresh start
# look like a failure. Retire it first so `start` always ends with both in place.
reap_orphan() {
  sp=$(agent_pid) || return 0
  say "found an agent running without its supervisor (pid $sp); replacing it"
  kill -TERM "$sp" 2>/dev/null
  i=0
  while [ "$i" -lt 8 ]; do
    kill -0 "$sp" 2>/dev/null || return 0
    sleep 1
    i=$((i + 1))
  done
  kill -KILL "$sp" 2>/dev/null
  sleep 1
}

cmd_start() {
  quiet=0
  [ "${1:-}" = "--quiet" ] && quiet=1
  [ -f "$AGENT" ] || die "the agent is not installed at $AGENT - run the installer first"
  need_python
  if p=$(sup_pid); then
    [ "$quiet" = 1 ] || say "already running (pid $p)"
    return 0
  fi
  mkdir -p "$RUN" 2>/dev/null
  reap_orphan
  # Detached from this terminal and this task: closing your SSH session, or the
  # Task Scheduler job that called us finishing, must not take the agent with it.
  # Deliberately NOT nohup. nohup sets SIGHUP to "ignored", and a shell cannot trap
  # a signal that was ignored when it started - so the supervisor's own HUP handler
  # (how `peekesp set` reaches the agent instantly) would silently never run.
  # Without nohup a hangup is trapped and forwarded, which only makes the agent
  # re-read its settings, and setsid (where DSM has it) removes the terminal anyway.
  if command -v setsid >/dev/null 2>&1; then
    setsid sh "$CTL" supervise >/dev/null 2>&1 </dev/null &
  else
    sh "$CTL" supervise >/dev/null 2>&1 </dev/null &
  fi
  i=0
  while [ "$i" -lt 10 ]; do
    sup_pid >/dev/null && break
    sleep 1
    i=$((i + 1))
  done
  sup_pid >/dev/null || die "the agent did not start. Look at: peekesp logs   (and $CRASH)"
  [ "$quiet" = 1 ] || say "started (pid $(sup_pid))"
}

cmd_stop() {
  if ! p=$(sup_pid); then
    rm -f "$PIDFILE"
    if sp=$(agent_pid); then kill -TERM "$sp" 2>/dev/null; fi     # an orphan, if the supervisor was kill -9'd
    [ "${1:-}" = "--quiet" ] || say "not running"
    return 0
  fi
  kill -TERM "$p" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ]; do
    kill -0 "$p" 2>/dev/null || break
    sleep 1
    i=$((i + 1))
  done
  kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null
  if sp=$(agent_pid); then kill -TERM "$sp" 2>/dev/null; fi
  rm -f "$PIDFILE"
  [ "${1:-}" = "--quiet" ] || say "stopped"
}

cmd_restart() { cmd_stop --quiet; cmd_start; }

apply_change() {   # tell a running agent to re-read its settings now
  if p=$(sup_pid); then
    kill -HUP "$p" 2>/dev/null
    say "applied to the running agent"
  else
    say "the agent is not running; start it with: peekesp start"
  fi
}

# Wait for the daemon (not a one-off test) to report a good push. This is the real
# end-to-end check: it goes through exactly the process, user and environment that
# will run after a reboot.
verify() {
  say "checking that the running agent is healthy..."
  agent --healthcheck --wait 90
  return $?
}

# ---------------------------------------------------------------- commands
cmd_status() {
  need_python
  agent --status
  if p=$(sup_pid); then
    say "  supervisor    running (pid $p) - restarts the agent if it ever exits"
  else
    say "  supervisor    not running"
  fi
  autostart_report
}

cmd_logs() {
  if [ -s "$CRASH" ]; then
    say "--- last crash output ($CRASH)"
    tail -n 20 "$CRASH"
    say "---"
  fi
  if [ ! -f "$LOG" ]; then
    say "no log yet ($LOG). It lives in memory, so a reboot clears it."
    return 0
  fi
  if [ "${1:-}" = "-f" ]; then
    exec tail -n 40 -f "$LOG"
  fi
  tail -n 80 "$LOG"
}

cmd_pair() {
  [ -n "${1:-}" ] || die "usage: peekesp pair CODE     (the code shown on the device's screen)"
  need_python
  agent --pair "$1" || exit 1
  if p=$(sup_pid); then kill -HUP "$p" 2>/dev/null; else cmd_start; fi
  verify
  rc=$?
  case "$rc" in
    0) say ""; say "Paired. The device switches from its code screen to the dashboard on its own." ;;
    2) say "Saved, but the agent does not see a pairing yet. Try: peekesp status" ;;
    *) say ""; say "Saved, but the relay has not accepted a push yet. See: peekesp status  /  peekesp logs" ;;
  esac
  return "$rc"
}

cmd_set() {
  case "${1:-}" in
    "") die "usage: peekesp set NAME VALUE      e.g.  peekesp set interval 10" ;;
    *=*) arg="$1" ;;
    *) [ $# -ge 2 ] || die "usage: peekesp set NAME VALUE"; arg="$1=$2" ;;
  esac
  need_python
  agent --set "$arg" || exit 1
  apply_change
}

cmd_toggle() {   # cmd_toggle push|serve on|off
  case "${2:-}" in
    on|off) ;;
    *) die "usage: peekesp $1 on|off" ;;
  esac
  need_python
  agent --set "$1=$2" || exit 1
  apply_change
}

cmd_autostart() {
  case "${1:-}" in
    on)  write_hook || die "could not write $HOOK"; ok "rc.d hook installed at $HOOK" ;;
    off) rm -f "$HOOK"; ok "rc.d hook removed" ;;
    ""|check) ;;
    *) die "usage: peekesp autostart [on|off]" ;;
  esac
  autostart_report
  [ "$(sched_state)" = found ] || scheduler_howto
}

cmd_update() {
  command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need curl or wget to update"
  need_python
  t=$(mktemp -d "${TMPDIR:-/tmp}/peekesp-update.XXXXXX") || die "cannot make a temp directory"
  fetch "$RAW/peek-agent-synology.py" "$t/peek-agent-synology.py" || { rm -rf "$t"; die "download failed: $RAW/peek-agent-synology.py"; }
  fetch "$RAW/install.sh" "$t/install.sh" || { rm -rf "$t"; die "download failed: $RAW/install.sh"; }
  validate_agent "$t/peek-agent-synology.py" || { rm -rf "$t"; die "the downloaded agent does not look right; nothing was changed"; }
  validate_ctl "$t/install.sh" || { rm -rf "$t"; die "the downloaded installer does not look right; nothing was changed"; }
  sh "$t/install.sh" --upgrade
  rc=$?
  rm -rf "$t"
  exit "$rc"
}

cmd_uninstall() {
  purge=0; yes=0
  for a in "$@"; do
    case "$a" in
      --purge) purge=1 ;;
      -y|--yes) yes=1 ;;
    esac
  done
  if [ "$yes" = 0 ]; then
    if ( : </dev/tty ) 2>/dev/null; then
      printf 'Remove the PeekESP agent from this NAS? [y/N] ' >/dev/tty
      read -r reply </dev/tty
      case "$reply" in y|Y|yes|YES) ;; *) say "cancelled"; return 0 ;; esac
    else
      die "not interactive: add -y to confirm"
    fi
  fi
  sched=$(sched_state)
  cmd_stop --quiet
  rm -f "$HOOK" "$BINLINK" "$PIDFILE" "$LOG" "$LOG.1" "$CRASH" "$STATUS"
  rm -rf "$PREFIX"
  if [ "$purge" = 1 ]; then
    rm -rf "$ETC"
    ok "removed the agent, its autostart hook and its settings"
  else
    ok "removed the agent and its autostart hook"
    say "     settings kept in $ETC (your pairing). Remove them too with: peekesp uninstall --purge"
  fi
  if [ "$sched" = found ]; then
    warn "your Task Scheduler entry 'PeekESP' is still there, and will now fail at boot."
    say "     Delete it: DSM > Control Panel > Task Scheduler."
  fi
  credits
}

usage() {
  cat <<EOF
PeekESP agent for Synology ${VERSION}

  peekesp status                 is it running, what it is sending, what it found
  peekesp logs [-f]              the recent log (-f to follow)
  peekesp test                   one reading and one real push (does not touch the running agent)
  peekesp doctor                 what this NAS can report, and from where (paste this in an issue)
  peekesp show                   the settings, with secrets hidden

  peekesp pair CODE              pair with the code on the device's screen
  peekesp push  on|off           the data-sending part: POST to the relay, reachable from anywhere
  peekesp serve on|off           the widget feed: http://<nas>:8080/telemetry on your LAN
  peekesp set NAME VALUE         interval  port  bind  allow  name  relay  volumes
                                 drive-temp  drive-interval  ups  messages  ttl-hours
                                 raid-system  tls-verify

  peekesp start | stop | restart
  peekesp autostart [on|off]     the rc.d hook, and what to add to Task Scheduler
  peekesp update                 fetch the latest agent and installer, keeping your settings
  peekesp uninstall [--purge] [-y]

Anything that changes something asks sudo for itself.
EOF
  credits
}

# ---------------------------------------------------------------- installer
do_install() {
  code="${PEEK_CODE:-}"; relay="${PEEK_RELAY:-}"; nostart=0; upgrade=0
  while [ $# -gt 0 ]; do
    case "$1" in
      install) ;;
      --code) shift; code="${1:-}" ;;
      --relay) shift; relay="${1:-}" ;;
      --no-start) nostart=1 ;;
      --upgrade) upgrade=1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done

  [ "$(id -u)" = 0 ] || die "the installer must run as root:   curl -fsSL $RAW/install.sh | sudo sh"

  say ""
  say "${C_B}PeekESP agent for Synology ${VERSION}${C_0}"
  say ""

  dsm=$(dsm_version)
  if [ -z "$dsm" ]; then
    [ "${PEEK_FORCE:-0}" = 1 ] || die "this does not look like a Synology DiskStation (no /etc.defaults/VERSION). Set PEEK_FORCE=1 to install anyway, for testing on another Linux."
    warn "not a Synology (PEEK_FORCE is set) - continuing"
  else
    ok "DSM $dsm"
    case "$dsm" in
      7.*) ;;
      *) warn "written for DSM 7.x; this is DSM ${dsm%%-*}. It will probably work, and nobody has checked." ;;
    esac
  fi

  PY=$(find_python) || { no_python_help; die "Python 3 not found"; }
  ok "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

  TMP=$(mktemp -d "${TMPDIR:-/tmp}/peekesp.XXXXXX") || die "cannot make a temp directory"
  trap 'rm -rf "$TMP"' EXIT

  # Prefer the files next to this script (a downloaded copy of the folder). Only
  # `curl | sh`, which has no neighbours, goes to the network for them.
  if [ -n "$SELFDIR" ] && [ -f "$SELFDIR/peek-agent-synology.py" ]; then
    cp "$SELFDIR/peek-agent-synology.py" "$TMP/agent" || die "cannot copy the agent"
  else
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need curl or wget to download the agent"
    fetch "$RAW/peek-agent-synology.py" "$TMP/agent" || die "download failed: $RAW/peek-agent-synology.py"
  fi
  if [ -n "$SELF" ] && grep -q PEEKESP_SYNOLOGY_INSTALLER "$SELF" 2>/dev/null; then
    cp "$SELF" "$TMP/ctl" || die "cannot copy the installer"
  else
    fetch "$RAW/install.sh" "$TMP/ctl" || die "download failed: $RAW/install.sh"
  fi
  validate_agent "$TMP/agent" || die "the agent file does not look right (a 404 page, or a truncated download?). Nothing was installed."
  validate_ctl "$TMP/ctl" || die "the installer file does not look right. Nothing was installed."

  if [ -n "${PEEK_SHA256:-}" ]; then
    command -v sha256sum >/dev/null 2>&1 || die "PEEK_SHA256 is set but sha256sum is not available"
    got=$(sha256sum "$TMP/agent" | awk '{print $1}')
    [ "$got" = "$PEEK_SHA256" ] || die "the agent's SHA-256 is $got, not the $PEEK_SHA256 you asked for. Nothing was installed."
    ok "agent SHA-256 matches"
  fi

  # Stop the old agent before replacing it; the files are swapped atomically, so a
  # supervisor still holding the old script is not corrupted.
  was_running=0
  sup_pid >/dev/null && was_running=1
  [ "$was_running" = 1 ] && cmd_stop --quiet

  mkdir -p "$PREFIX" "$(dirname "$BINLINK")" || die "cannot create $PREFIX"
  mkdir -p "$ETC" && chmod 700 "$ETC"
  install_file "$TMP/agent" "$AGENT" 755 || die "cannot install the agent"
  install_file "$TMP/ctl" "$CTL" 755 || die "cannot install the control script"
  printf '%s\n' "$PY" > "$PYFILE"
  ln -sf "$CTL" "$BINLINK" || warn "could not link $BINLINK; run it as $CTL"
  ok "installed to $PREFIX, and linked as peekesp"

  if write_hook; then
    ok "rc.d autostart hook installed ($HOOK)"
  else
    warn "could not write the rc.d hook; autostart will rely on Task Scheduler alone"
  fi

  # A relay is only written when asked for: an upgrade must not reset a custom one.
  if [ -n "$relay" ]; then
    if PEEK_CONFIG_DIR="$ETC" PEEK_RUN_DIR="$RUN" "$PY" "$AGENT" --set "relay=$relay" >/dev/null; then
      ok "relay set to $relay"
    else
      warn "could not set the relay to $relay"
    fi
  fi

  # Pairing: a code given on the command line wins; otherwise ask, but only where
  # there is a person to ask. An upgrade keeps the pairing it already has.
  if [ -z "$code" ] && [ "$upgrade" = 0 ] && ( : </dev/tty ) 2>/dev/null; then
    printf '\nPairing code shown on the device (Enter to skip): ' >/dev/tty
    read -r code </dev/tty
  fi
  if [ -n "$code" ]; then
    if PEEK_CONFIG_DIR="$ETC" PEEK_RUN_DIR="$RUN" "$PY" "$AGENT" --pair "$code" >/dev/null; then
      ok "paired with $code"
    else
      warn "that pairing code was not valid; pair later with: peekesp pair CODE"
    fi
  fi

  if [ "$nostart" = 1 ]; then
    say ""
    say "Installed, not started (--no-start). Start it with: peekesp start"
    credits
    return 0
  fi

  cmd_start --quiet
  ok "agent started (pid $(sup_pid))"
  say ""
  verify
  rc=$?

  say ""
  if [ "$rc" = 0 ]; then
    say "${C_OK}${C_B}PeekESP is running.${C_0}"
  elif [ "$rc" = 2 ]; then
    say "${C_B}PeekESP is installed and running, but not paired.${C_0}"
    say "  Type the code from the device's screen:   peekesp pair CODE"
  else
    say "${C_WARN}${C_B}PeekESP is installed, but not healthy yet.${C_0}  Run: peekesp status   and   peekesp logs"
  fi
  ip=$(lan_ip)
  port=$(agent --show 2>/dev/null | sed -n 's/.*"serve_port": \([0-9]*\).*/\1/p')
  port="${port:-8080}"
  say ""
  say "  relay push    peekesp push off      (the data-sending part; reachable from anywhere)"
  say "  widget feed   peekesp serve off     http://$ip:$port/telemetry   (private networks only)"
  say "  anything else peekesp help"
  if [ "$(sched_state)" != found ]; then
    scheduler_howto
  fi
  say ""
  say "  If DSM's firewall is on, allow TCP $port from your LAN for the widget feed:"
  say "  Control Panel > Security > Firewall. (Pushing to the relay needs only outbound.)"
  credits
  return 0
}

# ---------------------------------------------------------------- dispatch
main() {
  cmd="${1:-}"
  case "$(basename "$0")" in
    peekesp*) [ -n "$cmd" ] || cmd=help ;;
    *)        case "$cmd" in ""|install|-*) cmd=install ;; esac ;;
  esac
  case "$cmd" in
    install) do_install "$@"; exit $? ;;
    help|-h|--help) usage; exit 0 ;;
    version|--version) say "peekesp $VERSION  (by $AUTHOR - Discord: $DISCORD - $SITE)"; exit 0 ;;
  esac
  [ $# -gt 0 ] && shift

  case "$cmd" in
    status)     cmd_status ;;
    logs)       cmd_logs "$@" ;;
    supervise)  cmd_supervise ;;
    boot)       as_root "$cmd" "$@"; cmd_start --quiet ;;
    start)      as_root "$cmd" "$@"; cmd_start "$@" ;;
    stop)       as_root "$cmd" "$@"; cmd_stop ;;
    restart)    as_root "$cmd" "$@"; cmd_restart ;;
    pair)       as_root "$cmd" "$@"; cmd_pair "$@" ;;
    set)        as_root "$cmd" "$@"; cmd_set "$@" ;;
    push|serve) as_root "$cmd" "$@"; cmd_toggle "$cmd" "$@" ;;
    test)       as_root "$cmd" "$@"; need_python; agent --test ;;
    doctor)     as_root "$cmd" "$@"; need_python; agent --doctor; cmd_autostart check ;;
    show)       as_root "$cmd" "$@"; need_python; agent --show ;;
    autostart)  as_root "$cmd" "$@"; cmd_autostart "$@" ;;
    update)     as_root "$cmd" "$@"; cmd_update ;;
    uninstall)  as_root "$cmd" "$@"; cmd_uninstall "$@" ;;
    *)          say "peekesp: unknown command '$cmd'"; say ""; usage; exit 2 ;;
  esac
}

main "$@"
