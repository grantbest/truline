# factory-dispatcher

Turns a `dev.task` bead into a pull request.

The dispatcher is the producer the factory board waits on. It claims a pending task, runs an
agent on it inside an isolated clone, verifies the work stayed where it was told to, and opens a
pull request for review. Every step writes a `dev.note` back to the task, so a twenty-minute run
is legible on the board while it happens.

It is one app of the Truline platform. The beads it reads and writes live in the substrate
([`apps/substrate/`](../substrate/)); the verdict it waits on is written by the release gate
([`scripts/gate-prepass.py`](../../scripts/gate-prepass.py),
[`scripts/gate-verify.py`](../../scripts/gate-verify.py)); the merge it never performs itself
runs through [`scripts/merge-pr.sh`](../../scripts/merge-pr.sh). The process these pieces
implement is described in
[`docs/architecture/agentic-operating-model.md`](../../docs/architecture/agentic-operating-model.md).

## Use

```bash
# The `dev` bead NAMESPACE and the `dev` ENVIRONMENT are different things. A dev.task
# describing production work is a production record: point SUBSTRATE_URL at the
# production substrate, which is the instance the factory board reads.
export SUBSTRATE_URL=http://127.0.0.1:18001
export SUBSTRATE_API_KEY=...

python file_task.py my-task.json              # file work
python dispatch.py --once --dry-run           # see the prompt, claim nothing
python dispatch.py --once                     # claim the oldest runnable task and run it
python dispatch.py --task <bead-id>           # run one specific task
python dispatch.py --report-stuck             # list stale doing tasks, write nothing
python dispatch.py --reconcile-review --dry-run
                                            # list review tasks ready for done
python dispatch.py --reconcile-review         # mark landed review tasks done
python schedule_status.py                     # report paused flag plus in-flight workflows
```

A task spec needs only the fields that take judgement; see the docstring in
[`file_task.py`](file_task.py). An operational finding is filed with
[`file_finding.py`](file_finding.py) and dispositioned before the task it produces; the task
names the finding in `derived_from_finding_ids` and the filer writes the `derived_from` edge.

**The substrate is one database, never two.** `bead_link` has foreign keys, so a graph split
across two instances cannot express `arch.change --affects--> arch.application` or
`arch.incident --caused_by--> dev.task` at all. Do not create `dev.task` beads in a second
instance; the factory board reads one.

## How a task becomes a pull request

| Step | What it does |
|---|---|
| claim | `pending → doing`, so the board shows the work is live |
| isolate | clone into a temp dir whose `.git` has no path back to this repo |
| run | the selected registry worker, bounded by the task's `max_agent_minutes` |
| contain | verify the real working tree is byte-identical to before the run |
| scope | verify the diff stayed inside `scope.paths` and clear of `forbidden_paths` |
| smoke | byte-compile changed Python before spending time on declared checks |
| verify | bootstrap a clone-local `venv/` and run the task's declared commands |
| propose | branch, push, PR; `doing → review`, `pr_url` stamped on the task |

Each step is a Temporal activity in
[`activities/dispatch_steps.py`](activities/dispatch_steps.py), driven by
[`workflows/dispatch_task.py`](workflows/dispatch_task.py). A failed run is recorded as a
`Run failed:` status note. Temporal owns the retry bound; once it is spent, the dispatcher
records the task as `failed`.

### Intake

Work enters as a `dev.task` bead. The outer loop files one by hand with `file_task.py`, or the
lane scanner ([`scanner.py`](scanner.py)) proposes them mechanically from drift and failing
checks. The scanner's scan path has no flag that files anything: filing is a separate command
(`python scanner.py file`), because a dry run you can switch off is not a dry run. Every
candidate is assessed through the real filing path (`file_task.build_content`), so a candidate
reported as fileable is one filing would actually accept. Candidates whose every path is one
the factory may not touch are reported as suppressed, and
[`scanner-suppressions.yaml`](scanner-suppressions.yaml) names the ones suppressed by hand.

### Dispatch

`dispatch.py --once` claims the oldest runnable task with a compare-and-set transition, so
concurrent dispatchers cannot both claim the same task. Before claim or clone the dispatcher
resolves the worker (see *Worker registry* below), refuses unknown, quarantined and
lane-forbidden workers, and refuses a task whose declared scope has nowhere permitted to work.
A per-host run lock keeps two dispatch runs from sharing one working tree.

### Isolated worker clone

Isolation is a clone, not a worktree. A worktree's `.git` is a *file* pointing at the parent
repository, and a tool with its own project-resolution logic follows it home. A clone has its
own object store, and `origin` is rewritten to the remote URL, so nothing inside references the
real tree. `verify_clone_isolated()` in [`dispatch.py`](dispatch.py) asserts this rather than
assuming it: the run fails if `.git/config` still mentions the repo root or if an alternates
file appears.

The worker is additionally wrapped in an OS-level deny-write profile
([`containment.py`](containment.py)) scoped to the clone, so a write outside the allowed
subpaths fails at the OS layer regardless of what the worker believes or reports.

### Containment

The before/after fingerprint of the real working tree is defence in depth, and it is checked
**before the diff is even read**. An uncontained pass is not a pass.

The containment guard needs a quiescent tree. It compares the whole working tree before and
after, and it cannot attribute a change, so an operator editing the repository while a run is in
flight trips it exactly as a worker escaping would. Fail-closed is the right behaviour, and it is
the argument for running the worker from a checkout nobody types in. Do not edit the worker's
checkout mid-run, and read the paths in a `CONTAINMENT BREACH` message before blaming the worker.

### Scope

Scope is verified, not enforced. The sandbox confines writes to the workspace but not to
`scope.paths` within it, so the dispatcher grades the diff afterwards and discards work that
strayed, however good it is. The allow-matcher fails closed (a pattern it does not understand
matches nothing) while the forbid-matcher deliberately over-matches. Both lean toward refusing.
The matchers live in [`guards.py`](guards.py).

### Verification

Nothing here trusts the worker's own report. If a task declares commands, the dispatcher creates
`venv/` inside the isolated clone, installs the repository's Python dependency declarations plus
`PyYAML` for the checker scripts, and runs the commands with that venv first on `PATH`. A
command that exits non-zero fails the run. A command that cannot start also fails the run, and
the note names what ran, what failed, and what could not start. Tasks with no declared
verification are not failed solely for omitting commands.

Budgets use `subprocess` timeouts, not `timeout(1)`: macOS ships no `timeout` binary.

### Release gate and merge on verdict

The dispatcher never merges a pull request it opened. Every run ends at a PR regardless of a
task's `autonomy` value. A fresh-context gate agent writes the verdict; the mechanical pre-pass
([`scripts/gate-prepass.py`](../../scripts/gate-prepass.py)) checks the facts a machine can
check, and a PASS there is not a merge verdict.

The merge path is [`scripts/merge-pr.sh`](../../scripts/merge-pr.sh), which refuses to merge a
PR carrying no recognisable `Release-gate:` record or a recorded `DO-NOT-MERGE`, using the
single recognizer in [`scripts/gate_markers.py`](../../scripts/gate_markers.py). The
[`workflows/merge_on_verdict.py`](workflows/merge_on_verdict.py) workflow lets the gateway
request a merge-by-verdict without holding a GitHub credential: the activity on the worker that
holds `gh auth` runs `merge-pr.sh`, and the disposition is queryable afterwards. That activity is
never auto-retried, because a retry after a partially successful merge must not run a second
`gh pr merge` unsupervised.

## Stuck task report

`python dispatch.py --report-stuck` reports `dev.task` beads still in `doing` whose
`updated_at` is older than the stuck threshold. The default threshold is 45 minutes,
deliberately above the default 30-minute worker budget. Override it with
`--stuck-threshold-minutes <minutes>`.

The report is read-only: it lists `doing` tasks through the substrate API and filters them by
`updated_at`. It does not transition, patch, add notes, record leases, or write timeout metadata
to bead content. Reclaim is deliberately absent; recovery belongs to Temporal heartbeat
timeouts.

## Review reconciliation

`python dispatch.py --reconcile-review` scans `dev.task` beads in `review` that carry a
`pr_url`. A task moves to `done` only when GitHub reports the PR as merged **and** the PR's
merge commit is an ancestor of the fetched `main` tip. The second check is required because a
PR can be "merged" into a branch that never reaches trunk.

Use `--dry-run` to print every intended `review -> done` transition without writing. Review
tasks without `pr_url` are reported and left alone. Merged PRs whose merge commit is not on
`main` are reported as orphaned merges and left in `review`.

## Worker registry and persona materialisation

Worker routing is explicit. A task's `worker_hint` resolves through `WORKER_REGISTRY` in
[`dispatch.py`](dispatch.py); when it is absent, the dispatcher uses the declared default
worker, `claude`. Unknown workers, quarantined workers, retired workers and workers not
permitted for the task's lane are refused before claim or clone. Adding or changing a worker is
a registry edit, not a call-site edit. Each entry declares:

- `argv`: the exact command shape. For the default worker the skip-permissions flag belongs
  inside the wrapper and nowhere else, session persistence is off so the worker writes nothing
  under its home directory, settings sources are emptied and MCP config is strict so the worker
  inherits nothing from user scope, the model is pinned, and output is JSON because this CLI's
  exit codes are not a health signal.
- `quarantined` / `retired` with a reason. A quarantined worker is refused outright. A retired
  worker named by `worker_hint` falls back to the default worker with a note; a retired default
  is refused.
- `allowed_lanes`, `containment_allow` (extra paths the deny-write profile permits),
  `extra_env`, and `provenance_model` (stamped on the beads the run writes).
- `uses_personas`: when set, the dispatcher materialises the tracked persona definitions from
  [`docs/agents/`](../../docs/agents/) into the clone's `.claude/agents/` with
  [`scripts/materialize_agents.py`](../../scripts/materialize_agents.py) and assembles the
  persona prompt. Personas are tracked, diffable and reviewed under `docs/agents/`; the
  materialised copy is ignored by git. Adding a persona means adding it under `docs/agents/`
  and in the materialiser's list.

The registry is local to the dispatcher. It is not a substrate-backed capability registry.

## Configuration

The worker reads a shell-compatible env file (`KEY=value` or `export KEY=value`; the parser
accepts both). The file lives outside any repository checkout, mode 600 in a directory of
mode 700:

```bash
mkdir -p ~/.factory-dispatcher
chmod 700 ~/.factory-dispatcher
$EDITOR ~/.factory-dispatcher/env
chmod 600 ~/.factory-dispatcher/env
```

Required:

| Variable | Meaning |
|---|---|
| `SUBSTRATE_URL` | the substrate the beads live in |
| `SUBSTRATE_API_KEY` | its write key; never in a plist, never in the repository |
| `TEMPORAL_URL` | `host:port` of the Temporal frontend; it is configuration, not a constant |
| `TEMPORAL_NAMESPACE` | Temporal namespace (default `dev`) |
| `FACTORY_REPO` | `owner/name` of the repository the factory works on |
| `FACTORY_REMOTE` | the remote URL the isolated clone's `origin` is rewritten to |
| `CLAUDE_CODE_OAUTH_TOKEN` | the default worker's credential (see below) |

Optional:

| Variable | Meaning |
|---|---|
| `FACTORY_BASE_REF` | the base branch PRs target (default `main`) |
| `FACTORY_DISPATCH_SCHEDULE_ID` | dispatch schedule id (default `factory-dispatcher-dev`) |
| `FACTORY_DISPATCH_INTERVAL_SECONDS` | dispatch interval (default `900`) |
| `FACTORY_PYTHON` | interpreter for clone-local verification (default: the worker's own) |
| `FACTORY_PATCH_DIR` | where failed runs' patches land (default `~/.factory-dispatcher/failed`) |
| `FACTORY_OPERATOR` | identity stamped on hand-filed beads and notes |
| `FACTORY_PROVENANCE_MODEL` | overrides the registry's provenance model |
| `FACTORY_DAILY_USD_CAP` | skip the drain once the day's measured worker spend reaches this |
| `FACTORY_DISK_FLOOR_GB` | skip the drain when free disk is below this (a default applies) |
| `FACTORY_DISPATCHER_WORKER_CHECKOUT` | the factory-owned checkout the worker runs from |
| `FACTORY_DISPATCHER_ENV_FILE` | default for the installer's `--env-file` |
| `FACTORY_<NAME>_SCHEDULE_ID` | overrides any non-dispatch schedule id listed below |
| `DISCORD_WEBHOOK_URL` | where health and tunnel alerts post; alerts are skipped without it |

`CLAUDE_CODE_OAUTH_TOKEN` is a long-lived token from `claude setup-token` (interactive,
browser auth; the token prints in the **terminal**, not the browser). It outranks the
interactive `/login` session, so the worker survives that session expiring. **It expires
silently at the one-year boundary; record the date it was generated and regenerate it before
then.** Two traps: an editor that hard-wraps long lines splits the token, and the installer
refuses with a parse error naming the line (`nano -w` avoids it); and `ANTHROPIC_API_KEY`
anywhere in the environment silently outranks this token, so keep it out of the env file.

Whether a subscription credential can be carried into a container, technically and within the
provider's terms, is an open question. Until it is answered, the worker runs as a supervised
process on a host where that credential has been proven unattended, not as a cluster
Deployment.

## Temporal schedules

`worker.py` registers the dispatch schedule before it starts polling its task queue. The
schedule starts one `DispatchTaskWorkflow` on a fixed interval with
`ScheduleOverlapPolicy.SKIP`, so a still-running dispatch causes the next trigger to be skipped
instead of overlapped. The default interval is `900` seconds, deliberately minutes rather than
seconds, because the queue is measured in single-digit tasks per day.

The same worker registers the non-dispatch schedules declared in
[`schedule_runtime.py`](schedule_runtime.py). Each has an id override of the form
`FACTORY_<NAME>_SCHEDULE_ID`:

| Schedule id | Cadence | Workflow |
|---|---|---|
| `factory-ea-apply-15m` | 15 min | apply the tracked EA model to the substrate |
| `factory-release-apply-15m` | 15 min | apply release charters to the substrate |
| `factory-requirements-apply-15m` | 15 min | apply the requirements registry |
| `factory-change-apply-15m` | 15 min | apply change records |
| `factory-worker-revision-drift-15m` | 15 min | report the worker checkout drifting from `main` |
| `factory-capacity-resume-probe-15m` | 15 min | resume dispatch once a capacity pause clears |
| `factory-cluster-health-15m` | 15 min | cluster health and CI workflow-run health |
| `factory-verdict-staleness-nightly` | 24 h | report conformance verdicts going stale |
| `factory-ea-observation-nightly` | 24 h | observe the live model against the tracked one |
| `factory-release-status-nightly` | 24 h | release balance report |
| `factory-doctrine-staleness-nightly` | 24 h | doctrine staleness and deployed-revision drift |

Every schedule the runtime declares is named in
[`docs/architecture/README.md`](../../docs/architecture/README.md);
[`scripts/readme_conformance.py`](../../scripts/readme_conformance.py) checks that the two
lists agree.

### Pausing and stopping

Pause the unattended drain without redeploying:

```bash
temporal schedule pause \
  --address "$TEMPORAL_URL" \
  --namespace "$TEMPORAL_NAMESPACE" \
  --schedule-id factory-dispatcher-dev \
  --reason "pause factory dispatcher"
```

Pause governs future schedule firings only. It does **not** stop a workflow that already fired.
A paused schedule can still have a `RunningWorkflows` entry, and that execution wakes up and
claims work as soon as a worker is started.

Safe stop order: pause, then check.

```bash
python schedule_status.py
```

`schedule_status.py` is the operator check for whether the factory is genuinely quiet. It
reports the schedule's paused flag and its in-flight workflow executions in one output, plus
the tunnel keeper heartbeat and cluster health. It exits `0` only when the schedule is paused
and no in-flight workflows are reported; otherwise it exits `1`.

When in-flight executions exist, drain them by leaving the worker running until
`schedule_status.py` reports `in_flight_workflows: 0`, or terminate one:

```bash
temporal workflow terminate \
  --address "$TEMPORAL_URL" \
  --namespace "$TEMPORAL_NAMESPACE" \
  --workflow-id "<workflow-id from schedule_status.py>" \
  --run-id "<run-id from schedule_status.py>" \
  --reason "stop factory dispatcher in-flight execution"
```

Resume with `temporal schedule unpause` and the same arguments. The worker logs the schedule
id, interval, overlap policy and paused state at startup. Restarts update the schedule in place
and preserve an externally paused state.

## Running locally

```bash
pip install -r requirements.txt
export SUBSTRATE_URL=... SUBSTRATE_API_KEY=... TEMPORAL_URL=127.0.0.1:7233
export FACTORY_REPO=owner/name FACTORY_REMOTE=https://github.com/owner/name.git
python dispatch.py --once --dry-run      # one attended run, no claim
python worker.py                         # the Temporal worker, foreground
```

`dispatch.py --once` runs the whole loop in the foreground against whatever substrate and
remote the environment names. `worker.py` registers the schedules and polls until stopped.

## Worker checkout

**The worker never runs inside the operator's own working tree.** It runs from a checkout the
factory controls, advanced deliberately with one command:

```bash
python3 apps/factory-dispatcher/worker_checkout.py advance
```

With no revision given, this advances `~/.factory-dispatcher/worker-checkout` (override with
`FACTORY_DISPATCHER_WORKER_CHECKOUT`) to the local tip of `main`. Pass an explicit revision to
pin to something older; the command refuses, before writing anything, a revision that is not an
ancestor of `main`.

The rule exists because a supervisor restart while a PR branch is checked out in a shared tree
makes that branch the factory's code, invisibly. The worker backstops it: at startup it refuses
to run, naming the branch, unless the checkout it loaded is an ancestor of `main`
(`worker_revision.ensure_checkout_is_on_main_ancestor`). Restart the worker after advancing
the checkout for the new revision to take effect; the `factory-worker-revision-drift-15m`
schedule reports when the checkout falls behind.

## Running under launchd

On a macOS dispatcher host the Temporal worker runs as a launchd agent, installed by
[`launchd_agent.py`](launchd_agent.py) from the templates under [`launchd/`](launchd/):

- `com.gastown.factory-dispatcher-worker.plist.template`: the worker itself.
- `com.gastown.factory-dispatcher-tunnel.plist.template`: a port-forward tunnel agent.
- `com.gastown.factory-dispatcher-tunnel-keeper-watchdog.plist.template`: the keeper watchdog.

The worker plist sets `RunAtLoad` and `KeepAlive`, and redirects stdout/stderr under
`~/.factory-dispatcher/logs/`. The installer creates that log directory. **The plist never
contains a secret**; it sources the env file named by `--env-file` at install time, so that file
is the single place the worker's environment comes from. Never keep the env file inside a
repository checkout: one `git add -A` from history is too close. `.gitignore` guards
`worker.env` as a backstop, but the rule is that the file lives under `~/.factory-dispatcher/`.

Install or reinstall idempotently. `--python` must point at an interpreter carrying the worker
requirements; the installer refuses a bare system python and names the missing dependency:

```bash
python3 apps/factory-dispatcher/launchd_agent.py install \
  --env-file ~/.factory-dispatcher/env \
  --python /path/to/venv/bin/python
```

Remove it:

```bash
python3 apps/factory-dispatcher/launchd_agent.py uninstall \
  --env-file ~/.factory-dispatcher/env
```

The Temporal address must already be reachable when `install` runs: the installer refuses
when it is not, so launchd is not loaded into a `Client.connect` crash loop. If the address
becomes unreachable after startup, `worker.py` logs `Temporal tunnel lost` and logs again when
it is reachable. The host sleeping is expected; the worker resumes polling when it wakes.

`install-worker-identity` and `probe-worker-identity` set up and check a dedicated worker
account ([`worker_identity.py`](worker_identity.py)) so untrusted work can run as its own
user. Split mode is opt-in through `FACTORY_WORKER_USER` and is not yet wired into the run
path; with it unset the worker behaves exactly as described above.

### Reaching Temporal and the substrate

`TEMPORAL_URL` and `SUBSTRATE_URL` are configuration. When they are served from a cluster the
dispatcher host reaches through a port-forward, [`tunnel_keeper.py`](tunnel_keeper.py)
supervises the link instead of a bare `kubectl port-forward`:

```bash
nohup python3 apps/factory-dispatcher/tunnel_keeper.py run --link <name> \
  > ~/.factory-dispatcher/logs/tunnel-keeper.nohup.log 2>&1 &
```

It restarts a dead or unreachable link with backoff, posts an alert naming the link once it has
been down past `FACTORY_TUNNEL_KEEPER_ALERT_THRESHOLD_SECONDS` (default 60 s), and posts a
recovery alert when it comes back. A link with no on-file default is supervised as
`--link temporal:<namespace>:<resource>:7233:7233`. The keeper writes a heartbeat to
`~/.factory-dispatcher/tunnel-keeper.json` after every check; `schedule_status.py` reads it and
renders `tunnel_keeper_stale`, per-link reachability, and a named
`TUNNEL KEEPER STALE OR NOT RUNNING` fault when the heartbeat itself goes stale or absent.

**macOS Local Network privacy denies LAN access to a process launchd starts in the
background, in every launchd domain this code can select.** The OS charges a Local-Network
connection to the responsible process by walking the parent chain, and a launchd-started job
has launchd as that ancestor, not an already-permitted terminal. This is a platform boundary,
not a bug to work around, so the tunnel stays an attended, `nohup`-started process. The
`install-tunnel` / `verify-tunnel` subcommands remain for hosts where the rule does not apply.

What *can* survive a reboot unattended is the alert, not the tunnel.
`tunnel_keeper.py check-heartbeat` reads the heartbeat file and, if it looks dead, posts the
"an operator is required" alert naming the exact recovery command above, using only a local
file read and an outbound HTTPS POST. Install it as a `RunAtLoad` + `StartInterval` agent so it
fires at every login and periodically during a long session:

```bash
python3 apps/factory-dispatcher/launchd_agent.py install-keeper-watchdog \
  --python /path/to/venv/bin/python
```

## Rules this code enforces

- The worker never runs inside the operator's working tree; it runs from a factory-owned
  checkout that must be an ancestor of `main`.
- Isolation is a clone with a rewritten `origin`, never a worktree, and the clone is verified.
- Containment is checked before the diff is read. An uncontained pass is not a pass.
- Scope is graded after the run; the allow-matcher fails closed and the forbid-matcher
  over-matches.
- Declared verification runs in a clone-local venv; the worker's own report is not evidence.
- The dispatcher never merges. A merge happens only through `merge-pr.sh` on a recorded verdict.
- No secret in a plist, no env file in a checkout, no `ANTHROPIC_API_KEY` beside the OAuth token.
- An OAuth token expires silently at the one-year boundary; regenerate it before then.
- The `dev` namespace and the `dev` environment are different things; production work is a
  production record.
- Do not edit the worker's checkout while a run is in flight.

## Tests

```bash
python -m pytest tests/ -q
```

Tests cover the safety logic in [`guards.py`](guards.py) and the acceptance path in
[`dispatch.py`](dispatch.py): scope matching, the question gate, prompt construction, worker
routing, stuck-task reporting, review reconciliation, declared verification outcomes, and the
launchd plist and installer. The scope tests are written against the *unsafe* direction on
purpose: a check that fails open is worse than no check, because it looks like a gate.
`test_sibling_directory_is_not_allowed` pins the case where a scope of `apps/mcp-hub` would
authorise `apps/mcp-hub-evil/` under a naive `startswith`.
