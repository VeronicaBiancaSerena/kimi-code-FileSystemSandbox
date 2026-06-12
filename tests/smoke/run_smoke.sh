#!/usr/bin/env bash
# Smoke tests for kimi-sandbox against a REAL bubblewrap.
#
# These map directly to design section 28 (acceptance criteria). They require
# bubblewrap and a kimi binary on PATH (or KIMI override). Run from anywhere:
#
#   tests/smoke/run_smoke.sh
#
# Exit code 0 means every check passed. Each check prints PASS/FAIL.

set -u

KS="${KS:-kimi-sandbox}"
PROJECT="$(mktemp -d /tmp/ks-smoke-XXXXXX)"
STATE_ROOT="$(mktemp -d /tmp/ks-state-XXXXXX)"
COMMON=(--state-root "$STATE_ROOT")
PASS=0
FAIL=0

note() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf 'FAIL: %s\n' "$1"; FAIL=$((FAIL + 1)); }

cleanup() { rm -rf "$PROJECT" "$STATE_ROOT" 2>/dev/null; }
trap cleanup EXIT

run() { "$KS" "$PROJECT" "${COMMON[@]}" "$@"; }

# 28.2 project writable, visible on host
note "28.2 project writable"
if run --exec "touch /workspace/.sandbox-write-test && test -f /workspace/.sandbox-write-test" >/dev/null 2>&1 \
   && test -f "$PROJECT/.sandbox-write-test"; then
  ok "project /workspace is writable and visible on host"
else
  bad "project /workspace write"
fi

# 28.3 system dirs read-only
note "28.3 /etc read-only"
if run --exec "touch /etc/kimi-sandbox-test" >/dev/null 2>&1; then
  bad "/etc is writable (should be read-only)"
else
  ok "/etc is read-only"
fi

# 28.4 real HOME not visible
note "28.4 real HOME hidden"
listing="$(run --exec "ls -1 /home" 2>/dev/null)"
# Must contain 'sandbox' and must NOT contain the real user's home name.
if printf '%s\n' "$listing" | grep -qx "sandbox" \
   && ! printf '%s\n' "$listing" | grep -qx "$(basename "$HOME")"; then
  ok "/home shows 'sandbox', real user home absent"
else
  bad "/home listing unexpected: [$listing]"
fi

# 28.5 /tmp isolation
note "28.5 /tmp isolation"
if run --exec "touch /tmp/kimi-sandbox-test" >/dev/null 2>&1 \
   && ! test -e /tmp/kimi-sandbox-test; then
  ok "/tmp writable inside, not leaked to host"
else
  bad "/tmp isolation"
fi

# 28.6 network NOT isolated
note "28.6 network enabled"
net_check() {
  run --exec "python3 -c \"import urllib.request; print(urllib.request.urlopen('https://example.com', timeout=10).status)\"" 2>/dev/null | grep -q 200
}
# This probes an EXTERNAL dependency (example.com); a single TLS/DNS blip is
# not a sandbox failure, so retry a few times before declaring unreachable.
net_ok=1
for _attempt in 1 2 3; do
  if net_check; then net_ok=0; break; fi
  sleep 2
done
if [ "$net_ok" -eq 0 ]; then
  ok "network reachable (v1 does not isolate network)"
else
  bad "network unreachable after retries (external network may be down)"
fi

# 28.7 KIMI_CODE_HOME isolation
note "28.7 KIMI_CODE_HOME"
if run --exec "test \"\$KIMI_CODE_HOME\" = /kimi-code-home && touch \"\$KIMI_CODE_HOME/test-file\"" >/dev/null 2>&1 \
   && test -f "$STATE_ROOT/profiles/default/kimi-code-home/test-file"; then
  ok "KIMI_CODE_HOME=/kimi-code-home, persisted to host profile"
else
  bad "KIMI_CODE_HOME isolation"
fi

# 28.8 dry-run does not launch
note "28.8 dry-run"
if run --dry-run 2>/dev/null | grep -q -- "--bind $PROJECT /workspace"; then
  ok "dry-run prints bwrap argv"
else
  bad "dry-run output"
fi

# 28.9 banner
note "28.9 start banner"
if run --exec "true" 2>&1 1>/dev/null | grep -q "Kimi Sandbox active"; then
  ok "start banner printed (to stderr)"
else
  bad "start banner missing"
fi

# 28.10 sandbox markers
note "28.10 sandbox env markers"
if run --exec "test \"\$KIMI_SANDBOX\" = 1 && test \"\$KIMI_SANDBOX_MODE\" = workspace-write" >/dev/null 2>&1; then
  ok "KIMI_SANDBOX=1 and mode=workspace-write"
else
  bad "sandbox env markers"
fi

# 28.11 /etc minimal mount (no whole-/etc bind)
note "28.11 minimal /etc"
plan="$(run --dry-run 2>/dev/null)"
if echo "$plan" | grep -q -- "--ro-bind /etc /etc"; then
  bad "whole /etc bind present"
elif echo "$plan" | grep -q -- "--ro-bind-try /etc/resolv.conf"; then
  ok "minimal /etc mount, no whole-/etc bind"
else
  bad "minimal /etc markers missing"
fi

# banner secret check
note "banner secret hygiene"
if run --exec "true" 2>&1 1>/dev/null | grep -Eqi "api[_-]?key|token|secret"; then
  bad "banner appears to leak a secret-looking token"
else
  ok "banner contains no secret-looking tokens"
fi

# =====================================================================
# v2 hardening / engineering features
# =====================================================================

# v2.1 TIOCSTI seccomp filter blocks the ioctl with EPERM
note "v2.1 TIOCSTI seccomp filter"
if .venv/bin/python -c "from kimi_sandbox import seccomp; raise SystemExit(0 if seccomp.is_supported_arch() else 1)" 2>/dev/null; then
  out="$(run --exec 'python3 -c "import fcntl,termios; fcntl.ioctl(0, termios.TIOCSTI, chr(97))" 2>&1; true')"
  if printf '%s' "$out" | grep -qi "operation not permitted"; then
    ok "TIOCSTI ioctl blocked with EPERM"
  else
    bad "TIOCSTI not blocked: [$out]"
  fi
  # And with --no-seccomp the error is NOT EPERM (filter absent).
  out2="$(run --no-seccomp --exec 'python3 -c "import fcntl,termios; fcntl.ioctl(0, termios.TIOCSTI, chr(97))" 2>&1; true')"
  if printf '%s' "$out2" | grep -qi "operation not permitted"; then
    bad "--no-seccomp still blocked with EPERM (filter not actually disabled)"
  else
    ok "--no-seccomp disables the filter (no EPERM)"
  fi
else
  printf 'SKIP: seccomp not supported on this architecture\n'
fi

# v2.2 --no-network isolates the network
note "v2.2 --no-network"
if run --no-network --exec "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=8)\"" >/dev/null 2>&1; then
  bad "--no-network still has network access"
else
  ok "--no-network blocks network"
fi

# v2.3 --read-only makes /workspace read-only
note "v2.3 --read-only"
if run --read-only --exec "touch /workspace/ro-test" >/dev/null 2>&1; then
  bad "/workspace writable under --read-only"
else
  if run --read-only --exec "test \"\$KIMI_SANDBOX_MODE\" = read-only" >/dev/null 2>&1; then
    ok "/workspace read-only and mode=read-only"
  else
    bad "read-only mode marker wrong"
  fi
fi

# v2.4 --persistent-cache persists /cache to host
note "v2.4 --persistent-cache"
if run --persistent-cache --exec "test \"\$XDG_CACHE_HOME\" = /cache && echo data > /cache/persist-marker" >/dev/null 2>&1 \
   && test -f "$STATE_ROOT/profiles/default/cache/persist-marker"; then
  ok "persistent cache at /cache, persisted to host profile"
else
  bad "persistent cache"
fi

# v2.5 --ro-mount exposes an extra path read-only
note "v2.5 --ro-mount"
EXTRA="$(mktemp -d /tmp/ks-extra-XXXXXX)"
echo "extra-data" > "$EXTRA/file.txt"
if run --ro-mount "$EXTRA:/opt/extra" --exec "cat /opt/extra/file.txt | grep -q extra-data" >/dev/null 2>&1 \
   && ! run --ro-mount "$EXTRA:/opt/extra" --exec "touch /opt/extra/w" >/dev/null 2>&1; then
  ok "--ro-mount readable, not writable"
else
  bad "--ro-mount"
fi
rm -rf "$EXTRA"

# v2.6 reserved-path collision rejected (launcher error 125)
note "v2.6 reserved-path rejection"
run --ro-mount "/tmp:/etc/evil" --dry-run >/dev/null 2>&1
if [ "$?" -eq 125 ]; then
  ok "reserved-path mount rejected with launcher error 125"
else
  bad "reserved-path mount not rejected as expected"
fi

# v2.7 resource limits via systemd-run (if available)
note "v2.7 resource limits"
if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope --quiet -- /bin/true >/dev/null 2>&1; then
  if run --memory-max 256M --pids-max 64 --exec "true" >/dev/null 2>&1; then
    ok "runs under systemd-run resource limits"
  else
    bad "resource-limited run failed"
  fi
else
  printf 'SKIP: systemd-run --user unavailable in this environment\n'
fi

# v2.8 config file applies (and CLI overrides it)
note "v2.8 config file"
CFG="$(mktemp /tmp/ks-cfg-XXXXXX.toml)"
printf 'no_network = true\nread_only = true\n' > "$CFG"
if run --config "$CFG" --exec "test \"\$KIMI_SANDBOX_MODE\" = read-only" >/dev/null 2>&1; then
  ok "config file applied (read_only)"
else
  bad "config file not applied"
fi

# v2.9 CLI negators override a config-set boolean back to false (R1)
note "v2.9 CLI negator overrides config"
if run --config "$CFG" --writable --exec "test \"\$KIMI_SANDBOX_MODE\" = workspace-write && touch /workspace/rw-override" >/dev/null 2>&1 \
   && test -f "$PROJECT/rw-override"; then
  ok "--writable overrides config read_only=true"
else
  bad "--writable did not override config read_only"
fi
rm -f "$PROJECT/rw-override"
rm -f "$CFG"

printf '\n========================\n'
printf 'smoke: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
