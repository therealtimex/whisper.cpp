# RealTimeX whisper addon packaging

This fork builds `whisper_addon.node` for Electron from a pinned
[`ggml-org/whisper.cpp`](https://github.com/ggml-org/whisper.cpp) ref and
publishes it as GitHub release assets. RealTimeX release builds download them
(`realtimex-ai-app` `frontend/native/whisper-addon-pin.json` names the
release tag and Electron version).

| Contract | Example |
|----------|---------|
| Release tag | `realtimex-b4938`, `realtimex-b4938-rtx1` |
| Asset name | `whisper-addon-mac-arm64-electron-41.0.3.zip` |
| Zip layout | `whisper_addon.node` + `_realtimexWhisperAddonMetadata.json` |
| Manifest | `whisper-addon-manifest.json` (assets, sha256, upstream and packaging SHAs, `complete`) |
| Platforms | mac, linux, win × x64, arm64 (all six are required) |

The harness lives in `electron-addon/`; the release scripts in `scripts/`.

## Rules

- **Published releases are immutable.** The app pins a release by tag alone,
  so changing its assets changes what every build ships. A rebuilt addon (new
  harness, new flags, new Electron runtime) gets a new tag such as
  `realtimex-b4938-rtx2`. A publish run refuses a tag that is already
  published, before building and again before publishing (dry runs may build
  any tag; they publish nothing). It also refuses a leftover git tag that
  points at another commit.
- **Assembled as a draft, published complete.** Assets go to a draft release;
  the manifest is uploaded last; the release is published only when all six
  zips and the manifest are there. A failed run leaves at most a draft, which
  the next attempt reuses.
- **Manual runs are dry runs** unless `publish=true`.
- **Not marked Latest** unless `mark_latest=true`. Nothing should depend on
  "Latest"; pin a tag.
- **Every zip passes gates** before it is uploaded (`scripts/validate_addon.py`):
  - the zip layout and metadata match the asset, including the sha256;
  - the binary format and CPU match the asset (Mach-O, ELF or PE header);
  - macOS: the minimum macOS version is at most the app's floor, `13.0`
    (`REALTIMEX_MACOS_DEPLOYMENT_TARGET`). The build sets
    `CMAKE_OSX_DEPLOYMENT_TARGET` to it; without that the addon inherits the
    runner's macOS (`realtimex-b4938` says 15.0);
  - macOS: no exported weak node-addon-api (`__ZN4Napi*`) symbols. dyld
    coalesces weak definitions across addons, so an exported copy without
    C++ exception handling turns another addon's `Napi::Error` into an app
    abort (realtimex-ai-app#3229). Linux reports them as a warning.
  - and the addon loads in Node and exports functions
    (`scripts/smoke_load_addon.py`; N-API is the same ABI in Electron).

## Workflows

### Build and publish: `realtimex-whisper-addon.yml`

```bash
# Dry run: build, gate and keep the zips as workflow artifacts (7 days)
gh workflow run realtimex-whisper-addon.yml --repo therealtimex/whisper.cpp \
  -f upstream_ref=b4938 -f release_tag=realtimex-b4938-rtx2

# Publish to a new tag
gh workflow run realtimex-whisper-addon.yml --repo therealtimex/whisper.cpp \
  -f upstream_ref=b4938 -f release_tag=realtimex-b4938-rtx2 -f publish=true
```

Run it from a branch with `--ref <branch>` to test harness changes; the
release tag then points at that branch's commit.

**When one platform fails, re-run the failed jobs of the same run** (Actions UI,
or `gh run rerun <run-id> --failed`). The other platforms' artifacts are reused,
and the publish job runs once the matrix is green. Do not dispatch a fresh run
for that: it rebuilds all six platforms.

### Fallback runner for mac-arm64

Hosted Apple Silicon runners are capacity-limited: since 11/2026, `macos-15`
jobs here (and `macos-26` in the llama.cpp fork) were often cancelled with "The
job was not acquired by Runner of type hosted even after multiple attempts".
GitHub gives up on a hosted job after about 15 minutes; that is the wait before
the fallback (it can't be shortened per job).

1. `build-mac-arm64` asks for a hosted runner (`macos-15`, or the
   `mac_arm64_runner` input / `REALTIMEX_MAC_ARM64_RUNNER` variable).
2. If GitHub cancels it for lack of a runner, `build-mac-arm64-fallback` runs on
   the self-hosted label in `REALTIMEX_MAC_ARM64_FALLBACK_RUNNER`
   (`rtx-whisper-mac-arm64`). A real build failure does not fall back. Empty
   variable = no fallback.
3. Nothing listens on that label by default: this repository is public, and
   its owner is a user account, so a self-hosted runner can't be limited to
   this workflow. Instead the RealTimeX heartbeat on the iMac
   (`agent-heartbeat/scripts/whisper-release-watch.sh`) books the Mac mini
   when such a job is queued:
   - `scripts/book_runner.py` sees the queued job and no runner online, deletes
     stale single-use registrations, and mints a single-use (JIT) runner
     configuration that only carries the fallback label;
   - the heartbeat sends `self-hosted/start-jit-runner.sh` with that
     configuration over SSH to the mini's `rtxqa` account (SSH alias
     `macmini-qa`), a non-admin account that can't change the Homebrew tools
     the GitLab release builds use;
   - the script refuses while `~rtxqa/QA-LOCK` is held (it reclaims only a
     lock its own supervisor left behind when that supervisor is gone), its
     supervisor takes QA-LOCK atomically with its PID for the build, runs the
     runner from `~rtxqa/actions-runner` at lower CPU priority, stops it if no
     job arrives within 20 minutes (75 minutes overall), and releases only its
     own lock. The runner takes one job and unregisters.
4. Publish takes mac-arm64 from whichever job succeeded.

The heartbeat ticks every 15 minutes, so the fallback build starts up to about
15 minutes after GitHub's cancellation. If the mini stays busy (QA-LOCK) the job
waits; a queued self-hosted job is cancelled by GitHub after 24 hours.

Token: booking needs `~/.config/rtx-whisper-runner/token` (or
`.../token/token`) on the iMac: a fine-grained token for this repository only,
with Administration read/write and Actions read. Without it the heartbeat does
not book the mini (it never falls back to a broader token for this). Only the
single-use runner configuration reaches the mini.

Mini setup (once, as rtxqa): the GitHub runner is unpacked in
`~/actions-runner` (actions/runner 2.338.2, `osx-arm64`, sha256 checked). It is
not configured as a service and holds no registration.

### Watch upstream: `realtimex-watch-upstream.yml` and the iMac heartbeat

`scripts/watch_upstream.py` compares the latest upstream release with
`realtimex-<ref>` and the past runs of the release workflow, then says what to
do (`none`, `dispatch`, or `rerun-failed`). Two callers act on it:

| Caller | When | Notes |
|--------|------|-------|
| RealTimeX heartbeat on the iMac (agent-heartbeat workspace, task `whisper-cpp-release-watch`, `scripts/whisper-release-watch.sh`) | every 15 minutes | GitHub throttles scheduled workflows on forks; it also books the fallback runner |
| `realtimex-watch-upstream.yml` | daily cron backstop, or manual | manual runs are dry runs unless `publish=true` |

The watcher:

- does nothing while a run for the same target is queued or running;
- after a failure, waits `--backoff-hours` (6), then re-runs only the failed
  jobs of that run while its artifacts last (6 days), else dispatches anew;
- stops after `--max-attempts` (4) failed attempts on the current packaging
  commit and reports `attempts-exhausted`. A new commit on `master` (for
  example a harness fix) starts a fresh count;
- never touches a published release. A published release built for another
  Electron runtime is left as is, and the new runtime gets its own tag (next
  bullet); any other published but incomplete, invalid or mismatched release
  is reported as `needs-human:<state>`;
- builds a new Electron runtime (`REALTIMEX_ELECTRON_RUNTIME_VERSION`) under
  `realtimex-<ref>-electron-<version>`, since `realtimex-<ref>` is immutable.

**Off switch:** disable `realtimex-watch-upstream.yml` to pause every automatic
release, the heartbeat included. Disabling `realtimex-whisper-addon.yml` also
pauses it. The watcher never re-enables a workflow.

```bash
# What would the watcher do now? (read-only)
GH_TOKEN="$(gh auth token)" python3 realtimex/scripts/watch_upstream.py --json

# Check a specific upstream ref
GH_TOKEN="$(gh auth token)" python3 realtimex/scripts/watch_upstream.py --force-ref b4938
```

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/addon_release.py` | Asset names, run titles, release states, GitHub API client |
| `scripts/watch_upstream.py` | Decide `none` / `dispatch` / `rerun-failed` |
| `scripts/release_state.py` | Report a release's state; refuse published tags |
| `scripts/package_addon.py` | Zip a built addon with its metadata |
| `scripts/validate_addon.py` | Gate a zip (layout, metadata, CPU, macOS floor, weak Napi exports) |
| `scripts/smoke_load_addon.py` | Load the zipped addon and list its exports |
| `scripts/build_manifest.py` | Write `whisper-addon-manifest.json`; require all platforms |
| `scripts/book_runner.py` | Book a single-use runner for a queued fallback job (`--mint`, `--cleanup`) |
| `self-hosted/start-jit-runner.sh` | On the mini: run one single-use runner under QA-LOCK |
| `../.github/actions/whisper-addon` | The build, gate and upload steps every build job runs |

```bash
python3 -m unittest discover -s realtimex/tests -v

# Gate a downloaded release asset
python3 realtimex/scripts/validate_addon.py \
  --zip whisper-addon-mac-arm64-electron-41.0.3.zip \
  --platform mac --arch arm64 --electron-version 41.0.3
```

## Repository variables

| Variable | Default | Use |
|----------|---------|-----|
| `REALTIMEX_ELECTRON_RUNTIME_VERSION` | `41.0.3` | Electron headers the addon is built against |
| `REALTIMEX_MACOS_DEPLOYMENT_TARGET` | `13.0` | macOS floor for the build and the gate |
| `REALTIMEX_MAC_ARM64_RUNNER` | `macos-15` | Runner label for the first mac-arm64 attempt |
| `REALTIMEX_MAC_ARM64_FALLBACK_RUNNER` | (empty: no fallback) | Self-hosted label for the mac-arm64 fallback, `rtx-whisper-mac-arm64` |

The same shape is used by the llama.cpp fork (`therealtimex/llama.cpp`,
`realtimex/README.md`): a promote workflow, a watcher, a heartbeat, scripts with
tests, immutable releases.
