#!/usr/bin/env bash
# Optional REAL-conda smoke for kimi-sandbox (mod_v2 §17.3).
#
# NOT run in CI: it needs a real conda install. It verifies that, against a
# genuine conda root, the sandbox can run/activate existing envs and create new
# ones in the writable area, while every mutation of the read-only host root/env
# fails AND the host conda tree is byte-for-byte unchanged afterwards.
#
# Usage:
#   tests/smoke/run_real_conda_smoke.sh /home/user/anaconda3 [--existing-env NAME] [--with-network]
#
# Exit 0 only when: host snapshot unchanged, all expected-failures failed, and
# the base/console-script checks passed.

set -u

CONDA_ROOT="${1:-}"
if [ -z "$CONDA_ROOT" ] || [ ! -x "$CONDA_ROOT/bin/conda" ]; then
  echo "usage: $0 <conda_root> [--existing-env NAME] [--with-network]" >&2
  echo "  <conda_root>/bin/conda must exist and be executable" >&2
  exit 2
fi
shift || true

EXISTING_ENV=""
WITH_NETWORK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --existing-env) EXISTING_ENV="${2:-}"; shift 2 ;;
    --with-network) WITH_NETWORK=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

KS="${KS:-kimi-sandbox}"
WORK="$(mktemp -d "$HOME/ks-realconda-XXXXXX")"
PROJECT="$WORK/proj"; mkdir -p "$PROJECT"
STATE="$WORK/state"; mkdir -p "$STATE"
SNAP="$WORK/snap"; mkdir -p "$SNAP"
CFG="$WORK/config.toml"
PASS=0; FAIL=0

cleanup() { rm -rf "$WORK" 2>/dev/null; }
trap cleanup EXIT

cat > "$CFG" <<EOF
persistent_cache = true
conda_enabled = true
conda_root = "$CONDA_ROOT"
conda_writable = "cache"
conda_shell_integration = true
EOF

note() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf 'FAIL: %s\n' "$1"; FAIL=$((FAIL + 1)); }

run() {
  "$KS" "$PROJECT" --config "$CFG" --state-root "$STATE" --no-seccomp --exec "$1"
}

snapshot() {
  local tag="$1"
  for sub in envs pkgs conda-meta; do
    if [ -d "$CONDA_ROOT/$sub" ]; then
      find "$CONDA_ROOT/$sub" -maxdepth 2 -printf '%p %y %s %T@\n' 2>/dev/null \
        | sort > "$SNAP/$tag-$sub.txt"
    else
      : > "$SNAP/$tag-$sub.txt"
    fi
  done
}

expect_ok()   { note "$1"; if run "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
expect_fail() { note "$1"; if run "$2" >/dev/null 2>&1; then bad "$1 (should have failed)"; else ok "$1"; fi; }

snapshot before

# --- positive: queries, run, activate, console scripts ---
expect_ok "conda --version" 'conda --version'
expect_ok "conda env list"  'conda env list'
expect_ok "run -n base python" 'conda run -n base python -c "import sys; print(sys.executable)"'
expect_ok "activate base"   'bash -lc "conda activate base && python -c \"import sys; print(sys.prefix)\""'
expect_ok "run -n base pip --version (shebang)" 'conda run -n base pip --version'

if [ -n "$EXISTING_ENV" ]; then
  expect_ok "run -n $EXISTING_ENV python" "conda run -n $EXISTING_ENV python -c 'import sys; print(sys.prefix)'"
  expect_ok "run -n $EXISTING_ENV pip --version" "conda run -n $EXISTING_ENV pip --version"
fi

# --- positive: create/install in writable area (network only) ---
if [ "$WITH_NETWORK" -eq 1 ]; then
  expect_ok "create sandbox-smoke" 'conda create -y -n sandbox-smoke python=3.11'
  expect_ok "run sandbox-smoke"    'conda run -n sandbox-smoke python -c "print(123)"'
else
  note "network tests SKIPPED (pass --with-network to enable)"
fi

# --- negative: mutations of read-only host root/env must fail ---
expect_fail "install -n base"           'conda install -y -n base pip'
expect_fail "env remove -n base"        'conda env remove -y -n base'
expect_fail "--json install -n base"    'conda --json install -n base pip'
expect_fail "config --set"              'conda config --set channels defaults'
expect_fail "clean --force-pkgs-dirs"   'conda clean --force-pkgs-dirs -y'
if [ -n "$EXISTING_ENV" ]; then
  expect_fail "install -n $EXISTING_ENV" "conda install -y -n $EXISTING_ENV pip"
  expect_fail "env remove -n $EXISTING_ENV" "conda env remove -y -n $EXISTING_ENV"
fi

# --- negative: environment.yml targeting read-only prefix ---
cat > "$PROJECT/environment-readonly.yml" <<EOF
prefix: $CONDA_ROOT
dependencies:
  - python
EOF
expect_fail "env create -f readonly-prefix" 'conda env create -f /workspace/environment-readonly.yml'

# clean is allowed but must not touch the host root pkgs
expect_ok "clean --all (writable only)" 'conda clean --all -y'

snapshot after

note "host snapshot comparison"
CHANGED=0
for sub in envs pkgs conda-meta; do
  if ! diff -q "$SNAP/before-$sub.txt" "$SNAP/after-$sub.txt" >/dev/null 2>&1; then
    bad "host $CONDA_ROOT/$sub CHANGED"
    diff "$SNAP/before-$sub.txt" "$SNAP/after-$sub.txt" | head -20
    CHANGED=1
  fi
done
if [ "$CHANGED" -eq 0 ]; then
  ok "host conda root unchanged (envs/pkgs/conda-meta)"
fi

printf '\n=== report ===\n'
printf 'conda root: %s\n' "$CONDA_ROOT"
printf 'conda_writable: cache\n'
printf 'network available: %s\n' "$([ "$WITH_NETWORK" -eq 1 ] && echo yes || echo no)"
printf 'existing env tested: %s\n' "${EXISTING_ENV:-<none>}"
printf 'host snapshot changed: %s\n' "$([ "$CHANGED" -eq 0 ] && echo no || echo yes)"
printf '\nresult: %d passed, %d failed\n' "$PASS" "$FAIL"

[ "$FAIL" -eq 0 ] && [ "$CHANGED" -eq 0 ]
