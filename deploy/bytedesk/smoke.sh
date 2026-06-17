#!/usr/bin/env bash
# Phase-0 smoke for the ByteDesk Omnigent deployment (namespace: bytedesk).
#
# Deterministic part (always runs): verify the control-plane is up.
# Agent-run part: guided — see README "Smoke ladder". The most reliable first
# agent smoke is the LAPTOP one-shot `omnigent run`, which this script prints
# the exact command for rather than guessing the alpha's host/auth handshake.
set -euo pipefail
NS="${OMNIGENT_NS:-bytedesk}"
SVC="omnigent-server"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m! %s\033[0m\n' "$*"; }

say "1/4 omnigent pods in ns/$NS"
kubectl get pods -n "$NS" -l 'app.kubernetes.io/part-of=bytedesk-omnigent' -o wide || true

say "2/4 server rollout"
kubectl rollout status deploy/"$SVC" -n "$NS" --timeout=180s
ok "omnigent-server rolled out"

say "3/4 health check (port-forward $SVC :8000)"
kubectl port-forward -n "$NS" "svc/$SVC" 18000:80 >/tmp/omnigent-pf.log 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
sleep 3
if curl -fsS http://localhost:18000/health | grep -q '"status"'; then
  ok "GET /health → $(curl -fsS http://localhost:18000/health)"
else
  warn "health check failed — see kubectl logs deploy/$SVC -n $NS"
  exit 1
fi

say "4/4 next: run an agent (LAPTOP one-shot — most reliable first smoke)"
cat <<EOF
The control plane is UP. To prove an agent turn on the Codex path, run a runner
from your laptop against the port-forwarded server (keep this port-forward open
in another shell, or re-run: kubectl port-forward -n $NS svc/$SVC 18000:80):

  # 1. install the CLI (once):   uv tool install omnigent   (or: pipx install omnigent)
  # 2. load inference creds:     set -a; . deploy/bytedesk/env/runner.env; set +a
  # 3. single-agent smoke:
  omnigent run deploy/bytedesk/agents/bytedesk-engineer \\
      --server http://localhost:18000 \\
      -m "Smoke test: say hello, then run 'uname -a' and report the output."
  # 4. delegation smoke:
  omnigent run deploy/bytedesk/agents/bytedesk-orchestrator \\
      --server http://localhost:18000 \\
      -m "Investigate what this repo is and have the reviewer sanity-check it."

Or open the web UI:  http://localhost:18000   (auth is off in Phase 0).
EOF
ok "control-plane smoke passed"
