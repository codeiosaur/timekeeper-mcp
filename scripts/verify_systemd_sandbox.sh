#!/usr/bin/env bash
# verify_systemd_sandbox.sh — check the sandboxing in
# deploy/timekeeper-mcp.service.example by behavior, not by reading it.
#
# DESTRUCTIVE TO THE MACHINE IT RUNS ON. It creates the users `timekeeper` and
# `probeuser`, installs a release under /opt/timekeeper, writes synthetic
# fixtures under /root, /home, /etc, /usr/local and /tmp, loosens /root to
# 0711 while it runs, and installs, starts and removes the systemd units
# timekeeper-mcp.service and timekeeper-probe.service. Run it only on a
# disposable Linux VM (for example a fresh CI runner) that you will throw
# away afterwards. Never on a workstation or a server that runs timekeeper.
#
# It refuses to run unless ALL of these hold, checked before any change:
#   - Linux, with systemd as PID 1, running as root;
#   - TIMEKEEPER_SANDBOX_OPT_IN=this-vm-is-disposable in the environment;
#   - /etc/timekeeper-sandbox-disposable exists, is owned by root, and
#     contains exactly this machine's /etc/machine-id (so a marker copied
#     from another machine does not count);
#   - /root is a real directory, not a symlink;
#   - none of the users, paths, units, unit drop-ins or the port it would use
#     exist yet (an existing timekeeper install, including /opt/timekeeper-mcp,
#     stops it), and no administrator drop-in applies to every service;
#   - its prerequisites are present (systemd tools, Python >= 3.11 with venv,
#     at least one block device in /dev, a non-loopback IPv4 address, and
#     network access to PyPI).
#
# Usage, as root on the disposable VM, from a checkout of this repository:
#   cat /etc/machine-id > /etc/timekeeper-sandbox-disposable
#   TIMEKEEPER_SANDBOX_OPT_IN=this-vm-is-disposable \
#   TIMEKEEPER_PYTHON=/usr/bin/python3.11 \
#     scripts/verify_systemd_sandbox.sh 2>&1 | tee sandbox-verification.log
#
# Exit status 0 means every probe and service check passed. Every systemd,
# journal, user-management, network and package-install command runs with a
# deadline; plain local file commands (cp, stat, cat, chown and the like) do
# not. On exit (success, failure or interrupt) it stops and
# removes the units, users, files and listeners it created and restores
# /root's mode. The marker file and any pre-existing drop-ins are left alone.
# `systemd-analyze security` output is printed for information only.
#
# Not checked: environment isolation. systemd hands its manager environment
# (DefaultEnvironment=, systemd.setenv=, `systemctl set-environment`) to every
# unit it starts, so a variable placed there would reach this service too; the
# unit does nothing to prevent that. A caller's shell environment is never
# passed to a unit, so testing for it would prove nothing either way.
#
# The unit denies creating Unix-domain sockets. Whether the service still
# starts and serves that way (for example if a glibc NSS lookup tried to reach
# nscd or systemd-userdb over a Unix socket) is established only by the real
# MCP session this script runs against the service; it is the acceptance
# check for that restriction.

set -euo pipefail
umask 022

readonly OPT_IN_VALUE="this-vm-is-disposable"
readonly MARKER_FILE=/etc/timekeeper-sandbox-disposable

# Every name and path this script may create or remove.
readonly SERVICE=timekeeper-mcp.service
readonly PROBE_SERVICE=timekeeper-probe.service
readonly UNIT_DIR=/etc/systemd/system
readonly SERVICE_USER=timekeeper
readonly PROBE_USER=probeuser
readonly INSTALL_ROOT=/opt/timekeeper
readonly RELEASE=/opt/timekeeper/releases/sandbox
readonly ROOT_SECRET=/root/SYNTHETIC_SECRET
readonly HOME_SECRET=/home/probeuser/SYNTHETIC_SECRET
readonly ETC_WRITABLE=/etc/timekeeper-sandbox-writable
readonly USR_WRITABLE=/usr/local/timekeeper-sandbox-writable
readonly OPT_WRITABLE=/opt/timekeeper/releases/sandbox/writable
readonly SERVICE_PORT=8765
readonly SANDBOX_DOMAIN=timekeeper.sandbox.test
readonly IPV6_TEST_TARGET=2001:db8::1   # documentation prefix; never a real host

# Unit directories searched for drop-ins, and the drop-in directory names
# that would apply to our two units ("timekeeper-.service.d" is a prefix
# drop-in that applies to both).
readonly UNIT_SEARCH_DIRS=(/etc/systemd/system /run/systemd/system
                           /usr/local/lib/systemd/system /usr/lib/systemd/system
                           /lib/systemd/system)
readonly OUR_DROPIN_DIRS=(timekeeper-mcp.service.d timekeeper-probe.service.d
                          timekeeper-.service.d)

# Deadlines (seconds).
readonly COMMAND_DEADLINE=30          # any single systemctl/journalctl/user command
readonly INSTALL_DEADLINE=900
readonly UNIT_START_DEADLINE=120
readonly CLIENT_DEADLINE=120
readonly ANALYZE_DEADLINE=60
readonly LISTENER_READY_DEADLINE=10
readonly LISTENER_STOP_DEADLINE=5
readonly STOP_DEADLINE=30
readonly TIMEOUT_STOP_SECONDS=10   # must match TimeoutStopSec= in the unit

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
readonly SCRIPT_DIR REPO_ROOT
readonly UNIT_TEMPLATE=$REPO_ROOT/deploy/timekeeper-mcp.service.example
readonly PROBE_SOURCE=$REPO_ROOT/scripts/sandbox_probe.py
PYTHON=${TIMEKEEPER_PYTHON:-python3}

log() { printf 'verify_systemd_sandbox: %s\n' "$*"; }
die() { printf 'verify_systemd_sandbox: ERROR: %s\n' "$*" >&2; exit 1; }

# Run an external command with a deadline (SIGKILL 5 s after SIGTERM).
bounded() {  # seconds command...
    local seconds=$1
    shift
    timeout --kill-after=5 "$seconds" "$@"
}

# --- guards: nothing on the host is changed before these all pass ------------

require_disposable_host() {
    [[ $(uname -s) == Linux ]] || die "not Linux; refusing to run"
    [[ -r /proc/1/comm && $(cat /proc/1/comm) == systemd && -d /run/systemd/system ]] \
        || die "PID 1 is not systemd; refusing to run"
    [[ $EUID -eq 0 ]] || die "must run as root on a disposable VM"
    [[ ${TIMEKEEPER_SANDBOX_OPT_IN:-} == "$OPT_IN_VALUE" ]] \
        || die "set TIMEKEEPER_SANDBOX_OPT_IN=$OPT_IN_VALUE to confirm this VM is disposable"

    [[ -f $MARKER_FILE && ! -L $MARKER_FILE ]] \
        || die "$MARKER_FILE missing; create it only on a disposable VM"
    [[ $(stat -c %u "$MARKER_FILE") -eq 0 ]] || die "$MARKER_FILE is not owned by root"
    local machine_id
    machine_id=$(cat /etc/machine-id 2>/dev/null || true)
    [[ -n $machine_id && $(cat "$MARKER_FILE") == "$machine_id" ]] \
        || die "$MARKER_FILE does not contain this machine's /etc/machine-id"

    # The script loosens and later restores /root's mode; chmod follows
    # symlinks, so refuse rather than touch whatever /root points to.
    [[ -d /root && ! -L /root ]] || die "/root is not a plain directory (symlink?); refusing to run"
}

require_prerequisites() {
    # Cleanup expands possibly empty arrays under `set -u`, which Bash < 4.4
    # treats as an error; that would abort cleanup and leave state behind.
    ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4))) \
        || die "needs Bash >= 4.4 (this is $BASH_VERSION)"
    local tool
    for tool in systemctl journalctl systemd-analyze useradd userdel runuser \
                ip timeout install sed grep diff stat id readlink awk cut head \
                od tr mktemp basename hostname cp ln chmod chown date find sort; do
        command -v "$tool" >/dev/null || die "missing prerequisite: $tool"
    done
    [[ -x /usr/sbin/nologin ]] || die "missing /usr/sbin/nologin"
    [[ -f $UNIT_TEMPLATE && -f $PROBE_SOURCE ]] || die "run from a checkout of the repository"

    command -v "$PYTHON" >/dev/null || die "Python not found: $PYTHON (set TIMEKEEPER_PYTHON)"
    bounded "$COMMAND_DEADLINE" "$PYTHON" -c \
        'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 11))' \
        || die "$PYTHON must be Python >= 3.11 with the venv module"
    local python_real
    python_real=$(readlink -f "$(command -v "$PYTHON")")
    case $python_real in
        /root/*|/home/*) die "$python_real is under a home directory, which the sandbox hides" ;;
    esac

    # Positive control for PrivateDevices: without a block device on the
    # host, "no block devices in the sandbox" would pass vacuously.
    [[ -n $(bounded "$COMMAND_DEADLINE" find /dev -type b -print -quit 2>/dev/null) ]] \
        || die "no block device in the host /dev; the PrivateDevices check would prove nothing"
}

require_clean_slate() {
    local user path dir name
    for user in "$SERVICE_USER" "$PROBE_USER"; do
        ! id -u "$user" >/dev/null 2>&1 || die "user $user already exists; not a fresh VM"
    done
    for path in "$INSTALL_ROOT" /opt/timekeeper-mcp "$UNIT_DIR/$SERVICE" \
                "$UNIT_DIR/$PROBE_SERVICE" "$ROOT_SECRET" /home/"$PROBE_USER" \
                "$ETC_WRITABLE" "$USR_WRITABLE" /etc/nginx/sites-enabled/timekeeper \
                /var/mail/"$PROBE_USER" /var/spool/mail/"$PROBE_USER"; do
        [[ ! -e $path && ! -L $path ]] || die "$path already exists; not a fresh VM"
    done
    # Drop-ins would silently change the configuration under test. Refuse
    # (never remove them): any drop-in for our unit names, and any
    # administrator drop-in that applies to every service. Vendor-shipped
    # global drop-ins under /usr/lib or /lib are allowed and reported later.
    for dir in "${UNIT_SEARCH_DIRS[@]}"; do
        for name in "${OUR_DROPIN_DIRS[@]}"; do
            [[ ! -e $dir/$name ]] || die "drop-in directory $dir/$name exists; not a fresh VM"
        done
    done
    for dir in /etc/systemd/system/service.d /run/systemd/system/service.d \
               /usr/local/lib/systemd/system/service.d; do
        [[ ! -e $dir ]] || die "global service drop-in directory $dir exists; refusing"
    done
    local unit listed
    for unit in "$SERVICE" "$PROBE_SERVICE"; do
        listed=$(bounded "$COMMAND_DEADLINE" systemctl list-unit-files --no-legend "$unit" \
                 2>/dev/null || true)
        [[ -z $listed ]] || die "unit $unit is already installed; not a fresh VM"
    done
    bounded "$COMMAND_DEADLINE" "$PYTHON" -c \
        "import socket; socket.socket().bind(('127.0.0.1', $SERVICE_PORT))" 2>/dev/null \
        || die "port $SERVICE_PORT is in use"
}

host_ipv4() {
    bounded "$COMMAND_DEADLINE" ip -4 -o addr show scope global \
        | awk '{print $4}' | cut -d/ -f1 | head -n1
}

# "yes" if the host has an IPv6 route towards the documentation prefix.
# `ip route get` only consults the routing table; it sends nothing. Without a
# route, connect() fails with ENETUNREACH before any firewall sees the
# packet, so the IPv6 deny check would prove nothing and is skipped.
host_ipv6_route() {
    if [[ -e /proc/net/if_inet6 ]] \
        && bounded "$COMMAND_DEADLINE" ip -6 route get "$IPV6_TEST_TARGET" >/dev/null 2>&1; then
        echo yes
    else
        echo no
    fi
}

# --- cleanup: only what this run created, only allowlisted paths -------------

CREATED_SERVICE_USER=0
CREATED_PROBE_USER=0
CREATED_INSTALL_ROOT=0
CREATED_ROOT_SECRET=0
CREATED_ETC_WRITABLE=0
CREATED_USR_WRITABLE=0
INSTALLED_UNITS=()
LISTENER_PIDS=()
ROOT_MODE_ORIGINAL=""
WORK_DIR=""
HOST_MARKER=""

stop_listener() {  # pid
    kill "$1" 2>/dev/null || return 0
    local deadline=$((SECONDS + LISTENER_STOP_DEADLINE))
    while kill -0 "$1" 2>/dev/null && ((SECONDS < deadline)); do
        sleep 0.1
    done
    kill -9 "$1" 2>/dev/null
    wait "$1" 2>/dev/null
}

cleanup() {
    local status=$?
    set +e
    log "cleaning up"
    local unit
    for unit in "${INSTALLED_UNITS[@]}"; do
        bounded "$STOP_DEADLINE" systemctl stop "$unit" 2>/dev/null
        bounded "$COMMAND_DEADLINE" systemctl reset-failed "$unit" 2>/dev/null
        case $unit in
            "$SERVICE"|"$PROBE_SERVICE") rm -f "$UNIT_DIR/$unit" ;;
        esac
    done
    ((${#INSTALLED_UNITS[@]})) && bounded "$COMMAND_DEADLINE" systemctl daemon-reload
    local pid
    for pid in "${LISTENER_PIDS[@]}"; do
        stop_listener "$pid"
    done
    ((CREATED_ROOT_SECRET)) && rm -f "$ROOT_SECRET"
    # /root was checked to be a plain directory before any change.
    [[ -n $ROOT_MODE_ORIGINAL && -d /root && ! -L /root ]] && chmod "$ROOT_MODE_ORIGINAL" /root
    ((CREATED_ETC_WRITABLE)) && rm -rf "$ETC_WRITABLE"
    ((CREATED_USR_WRITABLE)) && rm -rf "$USR_WRITABLE"
    [[ $HOST_MARKER == /tmp/timekeeper-sandbox-marker-* ]] && rm -f "$HOST_MARKER"
    [[ $WORK_DIR == /tmp/timekeeper-sandbox.* ]] && rm -rf "$WORK_DIR"
    ((CREATED_PROBE_USER)) && bounded "$COMMAND_DEADLINE" userdel --remove "$PROBE_USER" 2>/dev/null
    ((CREATED_SERVICE_USER)) && bounded "$COMMAND_DEADLINE" userdel "$SERVICE_USER"
    ((CREATED_INSTALL_ROOT)) && [[ $INSTALL_ROOT == /opt/timekeeper ]] && rm -rf "$INSTALL_ROOT"
    log "cleanup done"
    exit "$status"
}

# --- helpers -----------------------------------------------------------------

wait_for_file() {  # path deadline_seconds
    local deadline=$((SECONDS + $2))
    until [[ -s $1 ]]; do
        ((SECONDS < deadline)) || die "timed out waiting for $1"
        sleep 0.1
    done
}

start_listener() {  # bind_address name; the port lands in $WORK_DIR/<name>.port
    # and the number of accepted connections in $WORK_DIR/<name>.count
    local port_file="$WORK_DIR/$2.port"
    "$PYTHON" -I "$PROBE_SOURCE" listen --bind "$1" --port-file "$port_file" \
        --count-file "$WORK_DIR/$2.count" --lifetime 900 >"$WORK_DIR/$2.log" 2>&1 </dev/null &
    LISTENER_PIDS+=("$!")
    wait_for_file "$port_file" "$LISTENER_READY_DEADLINE"
}

accepted() { cat "$WORK_DIR/$1.count"; }  # name -> accepted connections so far

wait_for_accepted() {  # name count deadline_seconds: true once the listener has accepted count
    local deadline=$((SECONDS + $3))
    until [[ $(accepted "$1") -ge $2 ]]; do
        ((SECONDS < deadline)) || return 1
        sleep 0.1
    done
}

as_service_user() { bounded "$COMMAND_DEADLINE" runuser -u "$SERVICE_USER" -- "$@"; }

replace_line() {  # file pattern replacement: the pattern must match exactly one line
    local count
    count=$(grep -c -x -- "$2" "$1" || true)
    [[ $count -eq 1 ]] || die "expected exactly one line '$2' in $1, found $count"
    local escaped
    escaped=$(printf '%s' "$3" | sed 's/[&|\\]/\\&/g')
    sed -i "s|^$2\$|$escaped|" "$1"
}

unit_property() { bounded "$COMMAND_DEADLINE" systemctl show -p "$2" --value "$1"; }

unit_journal_since() {  # unit epoch_seconds
    bounded "$COMMAND_DEADLINE" journalctl --sync 2>/dev/null || true
    bounded "$COMMAND_DEADLINE" journalctl --no-pager -o cat -u "$1" --since "@$2" \
        2>/dev/null || true
}

# When the system manager cannot compile or attach the IPAddressDeny/Allow
# filter it runs the unit without it ("best effort") and logs it in its own
# words: "bpf-firewall: ..." errors, or "<unit>: unit configures an IP
# firewall, but ..." followed by "(This warning is only shown for the first
# unit using IP firewalling.)". That notice is shown once per manager per
# boot, against whichever unit used IP filtering first, so the system
# manager's (PID 1's) messages are searched for the whole boot, plus
# everything our two units logged since the probe started (argument: epoch).
# Other processes' firewall messages (cloud agents, ufw, user managers for
# rootless containers, which run their own units) say nothing about our
# units and are not consulted. A match fails the run; no match is consistent
# with an active filter but does not prove it, and some systems log the
# notice only at debug level. The behavioral evidence is the host-interface
# listener check; these messages are supporting evidence, never proof.
readonly BPF_FIREWALL_PATTERN='bpf-firewall|IP firewall|firewalling'
bpf_firewall_messages() {  # since_epoch
    bounded "$COMMAND_DEADLINE" journalctl --sync 2>/dev/null || true
    {
        bounded "$COMMAND_DEADLINE" journalctl -b _PID=1 --no-pager -o cat 2>/dev/null || true
        bounded "$COMMAND_DEADLINE" journalctl -u "$PROBE_SERVICE" -u "$SERVICE" --since "@$1" \
            --no-pager -o cat 2>/dev/null || true
    } | grep -i -E "$BPF_FIREWALL_PATTERN" | sort -u || true
}

FAILURES=0
check() {  # description actual expected passed(ok|anything else)
    if [[ $4 == ok ]]; then
        log "PASS $1: $2"
    else
        log "FAIL $1: $2 (expected $3)"
        FAILURES=$((FAILURES + 1))
    fi
}

# --- main --------------------------------------------------------------------

require_disposable_host
require_prerequisites
require_clean_slate
HOST_IP=$(host_ipv4)
[[ -n $HOST_IP ]] || die "no non-loopback IPv4 address on this host"
IPV6_ROUTE=$(host_ipv6_route)
log "all guards passed on $(hostname) (machine-id $(cat /etc/machine-id))"

trap cleanup EXIT
trap 'exit 130' INT TERM

WORK_DIR=$(mktemp -d /tmp/timekeeper-sandbox.XXXXXXXX)
RANDOM_TAG=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
LOG_MARKER="SYNTHETIC_LOG_MARKER_$RANDOM_TAG"

log "creating users $SERVICE_USER and $PROBE_USER"
bounded "$COMMAND_DEADLINE" useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
CREATED_SERVICE_USER=1
bounded "$COMMAND_DEADLINE" useradd --create-home --shell /usr/sbin/nologin "$PROBE_USER"
CREATED_PROBE_USER=1
chmod 0755 /home/"$PROBE_USER"

log "installing a root-owned release at $RELEASE"
install -d -o root -g root -m 0755 "$INSTALL_ROOT"
CREATED_INSTALL_ROOT=1
install -d -o root -g root -m 0755 "$INSTALL_ROOT/releases" "$RELEASE" "$RELEASE/src" "$RELEASE/probe"
cp -r "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/README.md" "$REPO_ROOT/LICENSE" \
      "$REPO_ROOT/timekeeper" "$RELEASE/src/"
bounded "$INSTALL_DEADLINE" "$PYTHON" -m venv "$RELEASE/venv"
bounded "$INSTALL_DEADLINE" "$RELEASE/venv/bin/pip" install --quiet --no-cache-dir \
    --disable-pip-version-check "$RELEASE/src"
# Dependencies resolve fresh from PyPI (not from uv.lock): record exactly
# which versions this run tested.
log "resolved package versions in the release venv:"
bounded "$COMMAND_DEADLINE" "$RELEASE/venv/bin/python" -I -m pip freeze --all \
    --disable-pip-version-check | sed 's/^/    /'
install -o root -g root -m 0644 "$PROBE_SOURCE" "$RELEASE/probe/sandbox_probe.py"
# A root-owned setuid copy of `id`: outside the sandbox it reports euid 0.
install -o root -g root -m 4755 "$(command -v id)" "$RELEASE/probe/setuid-id"
ln -sfn "$RELEASE" "$INSTALL_ROOT/current"
chown -R root:root "$INSTALL_ROOT"
chmod -R go-w "$INSTALL_ROOT"
chmod 4755 "$RELEASE/probe/setuid-id"   # chown cleared the setuid bit
[[ -x $INSTALL_ROOT/current/venv/bin/timekeeper-mcp ]] || die "entry point was not installed"

log "creating synthetic fixtures"
# Directories the service user owns, so only the sandbox can stop writes.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$OPT_WRITABLE"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$ETC_WRITABLE"
CREATED_ETC_WRITABLE=1
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$USR_WRITABLE"
CREATED_USR_WRITABLE=1
# World-readable fake secrets, with /root traversable, so only the sandbox
# can make them unreadable.
ROOT_MODE_ORIGINAL=$(stat -c %a /root)
chmod 0711 /root
printf 'synthetic secret %s, not a real credential\n' "$RANDOM_TAG" > "$ROOT_SECRET"
CREATED_ROOT_SECRET=1
chmod 0644 "$ROOT_SECRET"
printf 'synthetic secret %s, not a real credential\n' "$RANDOM_TAG" > "$HOME_SECRET"
chmod 0644 "$HOME_SECRET"
HOST_MARKER=/tmp/timekeeper-sandbox-marker-$RANDOM_TAG
: > "$HOST_MARKER"
chmod 0644 "$HOST_MARKER"
start_listener 127.0.0.1 loopback
start_listener "$HOST_IP" host
LOOPBACK_PORT=$(cat "$WORK_DIR/loopback.port")
HOST_PORT=$(cat "$WORK_DIR/host.port")
log "listeners on 127.0.0.1:$LOOPBACK_PORT and $HOST_IP:$HOST_PORT; IPv6 route: $IPV6_ROUTE"

log "positive controls outside the sandbox (as $SERVICE_USER)"
as_service_user cat "$ROOT_SECRET" >/dev/null || die "control: $ROOT_SECRET not readable outside the sandbox"
as_service_user cat "$HOME_SECRET" >/dev/null || die "control: $HOME_SECRET not readable outside the sandbox"
[[ $(as_service_user "$RELEASE/probe/setuid-id" -u) == 0 ]] \
    || die "control: setuid helper does not elevate outside the sandbox"
for dir in "$OPT_WRITABLE" "$ETC_WRITABLE" "$USR_WRITABLE"; do
    as_service_user touch "$dir/control" && rm -f "$dir/control" \
        || die "control: $dir not writable outside the sandbox"
done
as_service_user "$PYTHON" -I -c \
    "import socket; socket.create_connection(('$HOST_IP', $HOST_PORT), timeout=5).close()" \
    || die "control: $HOST_IP:$HOST_PORT not reachable outside the sandbox"
wait_for_accepted host 1 "$LISTENER_READY_DEADLINE" \
    || die "control: the $HOST_IP listener did not count the unsandboxed connection"
[[ $(accepted host) -eq 1 && $(accepted loopback) -eq 0 ]] \
    || die "control: unexpected listener counts (host $(accepted host), loopback $(accepted loopback))"
as_service_user "$PYTHON" -I -c "import socket; socket.socket(socket.AF_UNIX).close()" \
    || die "control: $SERVICE_USER cannot create a Unix-domain socket outside the sandbox"
[[ -e $HOST_MARKER ]] || die "control: host marker missing"

log "rendering units"
RENDERED=$WORK_DIR/$SERVICE
cp "$UNIT_TEMPLATE" "$RENDERED"
replace_line "$RENDERED" "Environment=MCP_ALLOWED_HOSTS=YOUR_DOMAIN" \
    "Environment=MCP_ALLOWED_HOSTS=$SANDBOX_DOMAIN"
grep -q -x "ExecStart=$INSTALL_ROOT/current/venv/bin/timekeeper-mcp" "$RENDERED" \
    || die "unit ExecStart is not the expected entry point"
grep -q -x "TimeoutStopSec=$TIMEOUT_STOP_SECONDS" "$RENDERED" \
    || die "unit TimeoutStopSec does not match this script"

PROBE_UNIT=$WORK_DIR/$PROBE_SERVICE
cp "$RENDERED" "$PROBE_UNIT"
PROBE_COMMAND="$INSTALL_ROOT/current/venv/bin/python -I $INSTALL_ROOT/current/probe/sandbox_probe.py probe"
PROBE_COMMAND+=" --user $SERVICE_USER --setuid-helper $RELEASE/probe/setuid-id"
PROBE_COMMAND+=" --deny-write $OPT_WRITABLE --deny-write $ETC_WRITABLE --deny-write $USR_WRITABLE"
PROBE_COMMAND+=" --host-marker $(basename "$HOST_MARKER")"
PROBE_COMMAND+=" --secret $ROOT_SECRET --secret $HOME_SECRET"
PROBE_COMMAND+=" --loopback-port $LOOPBACK_PORT --host-ip $HOST_IP --host-port $HOST_PORT"
PROBE_COMMAND+=" --ipv6-target $IPV6_TEST_TARGET --ipv6-route $IPV6_ROUTE"
replace_line "$PROBE_UNIT" "ExecStart=.*" "ExecStart=$PROBE_COMMAND"
replace_line "$PROBE_UNIT" "Type=simple" "Type=oneshot"
CHANGED=$(diff "$RENDERED" "$PROBE_UNIT" | grep -c '^[<>]' || true)
[[ $CHANGED -eq 4 ]] || die "probe unit must differ from the service unit in exactly 2 lines"

install -o root -g root -m 0644 "$RENDERED" "$UNIT_DIR/$SERVICE"
INSTALLED_UNITS+=("$SERVICE")
install -o root -g root -m 0644 "$PROBE_UNIT" "$UNIT_DIR/$PROBE_SERVICE"
INSTALLED_UNITS+=("$PROBE_SERVICE")
bounded "$COMMAND_DEADLINE" systemctl daemon-reload

# The effective units must be exactly the files just installed, plus at most
# vendor-shipped global drop-ins (identical for both, so the two units still
# differ only in ExecStart and Type).
for unit in "$SERVICE" "$PROBE_SERVICE"; do
    fragment=$(unit_property "$unit" FragmentPath)
    [[ $fragment == "$UNIT_DIR/$unit" ]] || die "$unit loads from $fragment, not $UNIT_DIR/$unit"
    dropins=$(unit_property "$unit" DropInPaths)
    for path in $dropins; do
        case $path in
            /usr/lib/systemd/system/service.d/*|/lib/systemd/system/service.d/*)
                log "note: vendor drop-in applies to $unit: $path" ;;
            *) die "unexpected drop-in applies to $unit: $path" ;;
        esac
    done
done
[[ $(unit_property "$SERVICE" DropInPaths) == "$(unit_property "$PROBE_SERVICE" DropInPaths)" ]] \
    || die "the two units have different drop-ins"

log "running the probe inside the sandbox ($PROBE_SERVICE)"
PROBE_EPOCH=$(date +%s)
set +e
bounded "$UNIT_START_DEADLINE" systemctl start "$PROBE_SERVICE"
PROBE_START=$?
set -e
bounded "$STOP_DEADLINE" systemctl stop "$PROBE_SERVICE" || true  # cancel any pending restart
PROBE_OUTPUT=$(unit_journal_since "$PROBE_SERVICE" "$PROBE_EPOCH")
printf '%s\n' "$PROBE_OUTPUT"
check "probe unit exit" "systemctl start exited $PROBE_START" 0 \
    "$( ((PROBE_START == 0)) && echo ok)"
check "probe result" "$(grep -E '^PROBE RESULT: ' <<<"$PROBE_OUTPUT" || echo 'no result line')" \
    "N passed, 0 failed" \
    "$(grep -q -E '^PROBE RESULT: [0-9]+ passed, 0 failed' <<<"$PROBE_OUTPUT" && echo ok)"
# Listener-side evidence. Inside the sandbox the probe connected to the
# loopback listener (allowed) and tried the host-interface listener, which
# the same user reached from outside the sandbox above. Only the loopback
# listener may have counted a connection from the probe.
wait_for_accepted loopback 1 "$LISTENER_READY_DEADLINE" || true
check "loopback listener reached from the sandbox" "accepted $(accepted loopback)" 1 \
    "$([[ $(accepted loopback) -eq 1 ]] && echo ok)"
check "host-interface listener not reached from the sandbox" \
    "accepted $(accepted host) (1 = the unsandboxed control)" 1 \
    "$([[ $(accepted host) -eq 1 ]] && echo ok)"

log "starting the real service ($SERVICE)"
SERVICE_EPOCH=$(date +%s)
if ! bounded "$UNIT_START_DEADLINE" systemctl start "$SERVICE"; then
    unit_journal_since "$SERVICE" "$SERVICE_EPOCH" | tail -n 40
    die "$SERVICE did not start"
fi
set +e
bounded "$CLIENT_DEADLINE" "$RELEASE/venv/bin/python" -I "$RELEASE/probe/sandbox_probe.py" \
    client --url "http://127.0.0.1:$SERVICE_PORT/mcp" --marker "$LOG_MARKER"
CLIENT_STATUS=$?
set -e
check "initialized MCP session over loopback" "client exited $CLIENT_STATUS" 0 \
    "$( ((CLIENT_STATUS == 0)) && echo ok)"

value=$(unit_property "$SERVICE" ActiveState)
check "ActiveState before stop" "$value" active "$([[ $value == active ]] && echo ok)"
value=$(unit_property "$SERVICE" User)
check User "$value" "$SERVICE_USER" "$([[ $value == "$SERVICE_USER" ]] && echo ok)"
value=$(unit_property "$SERVICE" MemoryMax)
check MemoryMax "$value" 268435456 "$([[ $value == 268435456 ]] && echo ok)"
value=$(unit_property "$SERVICE" TasksMax)
check TasksMax "$value" 64 "$([[ $value == 64 ]] && echo ok)"
# The configured value only. Behavioral evidence that the filter is active
# comes from the host-interface listener check above; the absence of
# BPF-firewall messages below is only supporting evidence, never proof.
value=$(unit_property "$SERVICE" IPAddressDeny)
check "IPAddressDeny (configured)" "$value" "0.0.0.0/0 and ::/0" \
    "$([[ $value == *0.0.0.0/0* && $value == *::/0* ]] && echo ok)"

SERVICE_LOG=$(unit_journal_since "$SERVICE" "$SERVICE_EPOCH")
# Positive control: the journal does capture this service's request log.
check "journal has request log lines" "$(grep -c 'POST /mcp' <<<"$SERVICE_LOG" || true) lines" \
    "at least 1" "$(grep -q 'POST /mcp' <<<"$SERVICE_LOG" && echo ok)"
check "journal free of the tool-argument marker" \
    "$(grep -c -F "$LOG_MARKER" <<<"$SERVICE_LOG" || true) matches" 0 \
    "$(grep -q -F "$LOG_MARKER" <<<"$SERVICE_LOG" || echo ok)"
WARNINGS=$(bpf_firewall_messages "$PROBE_EPOCH")
[[ -z $WARNINGS ]] || printf '%s\n' "$WARNINGS"
check "no BPF-firewall message from the system manager or our units (not proof of an active filter)" \
    "$(grep -c . <<<"$WARNINGS" || true) lines" 0 "$([[ -z $WARNINGS ]] && echo ok)"

log "informational only: systemd-analyze security $SERVICE"
bounded "$ANALYZE_DEADLINE" systemd-analyze security --no-pager "$SERVICE" || true

log "stopping $SERVICE"
STOP_STARTED=$SECONDS
set +e
bounded "$STOP_DEADLINE" systemctl stop "$SERVICE"
STOP_STATUS=$?
set -e
STOP_ELAPSED=$((SECONDS - STOP_STARTED))
check "systemctl stop" "exited $STOP_STATUS after ${STOP_ELAPSED}s" \
    "exit 0 within TimeoutStopSec=${TIMEOUT_STOP_SECONDS}s" \
    "$( ((STOP_STATUS == 0 && STOP_ELAPSED < TIMEOUT_STOP_SECONDS)) && echo ok)"
value=$(unit_property "$SERVICE" Result)
check "Result after stop" "$value" success "$([[ $value == success ]] && echo ok)"
value=$(unit_property "$SERVICE" ActiveState)
check "ActiveState after stop" "$value" inactive "$([[ $value == inactive ]] && echo ok)"

if ((FAILURES)); then
    log "RESULT: $FAILURES check(s) failed"
    exit 1
fi
log "RESULT: all checks passed"
