#!/usr/bin/env bash
# Start one single-use GitHub Actions runner on this Mac from a JIT
# configuration, for the whisper addon's mac-arm64 fallback job.
#
# The iMac heartbeat sends this script over SSH as stdin, prefixed with
# `JIT_CONFIG=...` (from realtimex/scripts/book_runner.py --mint):
#
#   { printf 'JIT_CONFIG=%q\n' "$jit"; cat start-jit-runner.sh; } | ssh macmini-qa 'bash -s'
#
# It runs as the Mac mini's non-admin QA account (rtxqa), from the runner
# installed in ~/actions-runner (no service, no stored registration). It:
#   - refuses while ~/QA-LOCK is held (a QA round is using this account) or a
#     runner is already running; a lock left by this script's own supervisor
#     that is no longer running (killed, or the Mac restarted) is reclaimed,
#     any other lock is left alone;
#   - has the supervisor take ~/QA-LOCK atomically, with its PID, for the
#     whole build, so QA waits for it;
#   - runs the runner detached, at lower CPU priority (the realtimex account
#     runs timing-sensitive GitLab jobs on this Mac);
#   - stops it if no job arrived within IDLE_LIMIT, or after MAX_TIME;
#   - removes ~/QA-LOCK only if it still holds this supervisor's mark and PID.
#
# The JIT configuration is single-use and only registers a runner with the
# fallback label; it is visible in the runner's arguments to this Mac's own
# accounts while the runner runs.
set -euo pipefail

: "${JIT_CONFIG:?JIT_CONFIG must be set}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner}"
LOCK="$HOME/QA-LOCK"
MARK="github-runner whisper.cpp mac-arm64 fallback"
IDLE_LIMIT="${IDLE_LIMIT:-1200}"
MAX_TIME="${MAX_TIME:-4500}"

if [[ ! -x "${RUNNER_DIR}/run.sh" ]]; then
  echo "refused: no GitHub runner in ${RUNNER_DIR}"
  exit 2
fi
if pgrep -U "$(id -u)" -f "${RUNNER_DIR}/bin/Runner.Listener" >/dev/null; then
  echo "refused: a runner is already running in ${RUNNER_DIR}"
  exit 3
fi
if [[ -e "${LOCK}" ]]; then
  first="$(head -1 "${LOCK}" 2>/dev/null || true)"
  owner="$(sed -n 's/.* pid \([0-9][0-9]*\).*/\1/p' <<<"${first}")"
  if [[ "${first}" == "${MARK}"* && -n "${owner}" ]] && ! kill -0 "${owner}" 2>/dev/null; then
    echo "reclaimed: stale QA-LOCK from supervisor pid ${owner}, which is no longer running"
    rm -f "${LOCK}"
  else
    echo "refused: QA-LOCK held: $(head -c 160 "${LOCK}")"
    exit 3
  fi
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log="${RUNNER_DIR}/rtx-jit-${stamp}.log"
supervisor="${RUNNER_DIR}/rtx-jit-supervise.sh"
cat > "${supervisor}" <<'SUPERVISOR'
#!/usr/bin/env bash
# Written by start-jit-runner.sh; runs one JIT runner and cleans up after it.
set -u
RUNNER_DIR="$1" LOCK="$2" MARK="$3" IDLE_LIMIT="$4" MAX_TIME="$5" LOG="$6"
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
# The mini's IPv6 route to some of GitHub's Azure endpoints is broken (IPv6
# connects to pipelines.actions.githubusercontent.com time out; IPv4 works),
# and an artifact upload failed with ETIMEDOUT. Node-based actions run with
# this, so they resolve IPv4 first.
export NODE_OPTIONS="--dns-result-order=ipv4first"
# Take QA-LOCK atomically (noclobber): if QA took it meanwhile, back off.
if ! ( set -C; printf '%s since %s pid %s (log %s)\n' "${MARK}" "$(date -u +%FT%TZ)" "$$" "${LOG}" > "${LOCK}" ) 2>/dev/null; then
  echo "$(date -u +%FT%TZ) supervisor: QA-LOCK taken meanwhile; not starting" >>"${LOG}"
  exit 3
fi
release() {
  if [[ -f "${LOCK}" ]] && head -1 "${LOCK}" | grep -qF "${MARK}" && head -1 "${LOCK}" | grep -qF " pid $$ "; then
    rm -f "${LOCK}"
  fi
}
trap release EXIT
cd "${RUNNER_DIR}" || exit 1
nice -n 10 ./run.sh --jitconfig "${JIT_CONFIG}" >>"${LOG}" 2>&1 &
pid=$!
unset JIT_CONFIG
start=$(date +%s)
job=0
while kill -0 "${pid}" 2>/dev/null; do
  sleep 10
  now=$(date +%s)
  if (( job == 0 )) && grep -q 'Running job' "${LOG}"; then job=1; fi
  if { (( job == 0 )) && (( now - start > IDLE_LIMIT )); } || (( now - start > MAX_TIME )); then
    echo "$(date -u +%FT%TZ) supervisor: stopping the runner (job started: ${job})" >>"${LOG}"
    pkill -TERM -U "$(id -u)" -f "${RUNNER_DIR}/bin/Runner.(Listener|Worker)" || true
    sleep 15
    pkill -KILL -U "$(id -u)" -f "${RUNNER_DIR}/bin/Runner.(Listener|Worker)" || true
    break
  fi
done
wait "${pid}" 2>/dev/null
echo "$(date -u +%FT%TZ) supervisor: runner exited" >>"${LOG}"
# Keep the last ten runner logs.
ls -1t "${RUNNER_DIR}"/rtx-jit-*.log 2>/dev/null | tail -n +11 | while IFS= read -r old; do rm -f "${old}"; done
SUPERVISOR
chmod 700 "${supervisor}"

JIT_CONFIG="${JIT_CONFIG}" nohup "${supervisor}" "${RUNNER_DIR}" "${LOCK}" "${MARK}" \
  "${IDLE_LIMIT}" "${MAX_TIME}" "${log}" </dev/null >/dev/null 2>&1 &
sup=$!
# Confirm the supervisor took the lock before reporting success.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if head -1 "${LOCK}" 2>/dev/null | grep -qF " pid ${sup} "; then
    echo "started: single-use runner (supervisor pid ${sup}), log ${log}"
    exit 0
  fi
  if ! kill -0 "${sup}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
echo "failed: supervisor pid ${sup} did not take QA-LOCK; see ${log}"
exit 4
