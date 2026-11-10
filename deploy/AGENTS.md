# deploy - production runtime wiring

## Purpose

Fresh-host and existing-host operations for the Grid-native core. Production
executes an immutable release selected through `/home/aipg/current`.

## Ownership

- `media_codec_runtime.py`, `media-codecs.lock.json`,
  `media-codecs-jammy.sources`, `test_media_codec_runtime.py` and
  `MEDIA_CODEC_RUNTIME.md` own the separate signed-source, static codec
  candidate. Its workflow emits main-only provenance, not host installation
  or admission. Verify the exact artifact and current security state before
  selecting a versioned binary; preserve system libraries and rollback.
  Core8e74ef37/Gallery950979d3 now select qualified service-private codecs
  with actual restricted-process proof and purchased source acceptance.
  BF16 audio/video sources are public through337frames; larger Gallery
  original uploads do not raise that bound. NVFP4 sources stay closed.

- `PAYOUT_POLICY_RENEWAL_2026_11_10.md` - the eight-week payout sender
  failure after the single reward-policy window expired, the appended
  windows and valuations through 2026-12-15, verification, and the catch-up
  and monitoring work still open. Extend the window list before it ends.

- `VIDEO_PAID_RETEST_2026_10_14.md` - subsequent funded-account exporter
  deployment/retest and closed cohort, exact paid completion/clock evidence,
  preserved unsuccessful interruption control, scoped key401 cleanup and
  still-open real refund/consumer gates. The original dark-deploy document
  remains its historical initial snapshot, not the current journal count.

- `VIDEO_NVFP4_DARK_2026_10_14.md` - exact4e9864c8/0045 dark cutover,
  main-tree identity, fresh PG16 and production restore proof, private-source
  wiring and preserved charging/models/timers. Async remains off; no funded
  canary, new worker enrollment, consumer rollout or video x402 activation.

- `VIDEO_CREDIT_DARK_2026_10_12.md` - owner-approved Distilled tariff and
  credit-only PR217 merge, exact tested/main tree identity, actual production
  backup/restore and drained `b053b2ce` / `0045` cutover. All six workers return;
  charging/timers/env are unchanged. Async remains off/pilot with an empty
  journal, no LTX-2.5 workers, no funded canary and no x402 activation.

- `CUSTOMER_PRICING_ROLLOUT_2026_09_17.md` - immutable `3384c223` Qwen pricing
  fix, real Chat billing canary, preserved cancelled/partial observation and
  replacement start blocked by stale-candidate health; no compensation changes.
- `VALIDATOR_SHADOW_START_2026_09_16.md` - actual observation-only activation
  on unchanged `d1aafcf4`, fresh start-gate/restore proof, guarded aborted
  attempts, fixed September23 end and unchanged client-backend/economic state.
- `VALIDATOR_PERIOD_ROLLOUT_2026_09_15.md` - immutable `64d38951`, then
  `d1aafcf4` / `0042` backup/restore and drained shadow-evidence/collector-error
  deployments, unchanged live
  controls, remaining observer/cohort gates and single-replica capacity limit.
  The broader tool-choice candidate PR #195 remains draft and undeployed.
- `VALIDATOR_BUDGET_DEPLOYMENT_2026_09_15.md` - immutable `f5211254` / `0042`
  deployment of owner-approved compensation ceilings, backup/restore and
  drained cutover proof, followed by the approved three-member earning campaign
  September 15-22 at 14:30 UTC. Payout-wallet consent does not gate earning.
- `VALIDATOR_TOKEN_LIMIT_ROLLOUT_2026_09_13.md` - immutable `d606e4d8` / `0042`
  backup/restore and drained compatibility deployment. Preserves billing and
  worker-payout controls; subsequent exact .19/.20 admissions preserve all
  prior reviewed versions. Preview.20 combines the qualified token-limit and
  committed-empty delivery corrections. Fresh owned .19 evidence is
  instruction-only; neither it nor retained-response verification proves
  a live v2 or committed-empty delivery loop.
- `WORKER_PAYOUT_FREEZE_2026_09_09.md` - prospective hourly allocation candidate,
  PostgreSQL proof, migration and rollback gates. Not payout activation evidence.
- `bootstrap.sh` - fresh-host bootstrap pinned to an operator-supplied full
  commit SHA. Installs only the Grid API, PostgreSQL, Redis, and Nginx.
- `env.template` - `/etc/aipg/grid.env` source of production env names.
- `README.md` - deploy/cutover/runbook notes.
- `DEMAND_BILLING_RUNBOOK.md` - dark deploy, allowlisted canary, alert,
  rollback, and staged demand-charging procedure.
- `DEMAND_BILLING_LAUNCH_2026_09_08.md` - active launch evidence and outstanding
  gates; includes deployed Music journal recovery and successive Core
  payout-input/admission guard releases. Local tests and implementation are not
  production activation proof.
- `VALIDATOR_COHORT_RUNBOOK.md` - privacy-safe intake, opaque common-control
  review, public per-node status verification, 72-hour qualification,
  verification, expiry, and incident handling for independent preview
  operators.
- `VALIDATOR_RECOVERY_2026_09_07.md` - immutable `508ca14f` / Alembic `0035`
  production backup/restore, cutover and preserved-identity evidence. Recent
  heartbeat collection is live; completed recovery and pilot evidence remain
  separate gates.
- `VALIDATOR_UPDATER_ROLLOUT_2026_09_07.md` - immutable `3714a927`
  compatibility deployment, published preview.17 provenance and exact
  four-release admission with preserved qualification. Owned-node canary,
  public download promotion and the pilot remain separate gates.
- `VALIDATOR_COMPENSATION_DARK_2026_09_07.md` - immutable `874f7407` / `0039`
  backup/restore and dark cutover proof, preserved identities/config/timers,
  empty compensation tables and the shared-nonce rollback boundary. Native
  release, live consent, operator reviews and budget activation remain separate.
- `VALIDATOR_RELEASE_OVERLAP_2026_09_07.md` - immutable `84fe0fd6` / `0039`
  deployment of bounded release-overlap capacity with unchanged admission,
  identity, environment and timer state. It does not admit preview.18 or enable
  a validator compensation campaign.
- `nginx/aipg-api.conf` - Grid routes, exact OAuth metadata routes, optional
  reviewed exact-route overlays, restricted metrics, public docs/health, and
  static `410 Gone` responses for retired API paths.
- `systemd/aipg-gridapi.service` - uvicorn Grid API unit.
- `systemd/aipg-payout.{service,timer}` - custodial payout one-shot and hourly
  scheduler. The service invokes the wrapper from the selected release.
- `systemd/aipg-postgres-backup.{service,timer}` - hardened root-only daily
  backup scheduler; existing hosts enable it only after a supervised restore
  proof.

## Local Contracts

- The owner-approved x402-only USD 0.001 minimum is a candidate, not production
  activation. Apply `0046` before starting its release, retain old receipts with
  zero minimum, and preserve minimum-aware completion/recovery on rollback.
  CI plus a fresh production restore/migration proof precede deployment; a
  separately capped real-worker payment/replay test precedes public admission.
  The prior `607d1c03` / `0045` deployment record remains historical evidence.

- Public NVFP4 additionally requires `MEDIA_ASYNC_NVFP4_ENABLED=1` after
  reviewed native/exporter/worker-credential and purchased path qualification.
  Default0 retains BF16-only public admission. This switch neither widens337
  frames nor opens source modes, bypasses a hold or changes tariffs. Close the
  switch for rollback while retaining accepted NVFP4 receipts/replay/GET.

- The owner approved the credit-first LTX-2.5 rollout on 2026-10-12.
  PR217 stops at 0045; do not pull the later x402/refund migration stack into
  this release. Distilled alone is priced at USD 0.020/output-second, with a
  USD 1.00 total funded owner canary ceiling and an expiring account pilot.
  Preserve global charging, payouts, other model services and the protected
  client GPT backend. No broad opening follows from the approval or tariff
  commit; current-source CI, restore proof and actual canary must pass first.
  FFmpeg/ffprobe and the separately verified private-source bucket are now
  configured at4e9864c8/0045. The funded canary and corrected BF16 clock
  retest have now run; the owner cohort is closed and seven video records are
  settled. The warm-render interruption control completed successfully, so
  actual failure/refund is not proven. See `VIDEO_PAID_RETEST_2026_10_14.md`;
  do not replace that control, erase history or silently enlarge the budget.
  Verify storage prerequisites before source-video admission, rather than
  bypassing input validation or using the public output bucket.

- Async video remains a default-off candidate, not production admission.
  Source conditioning additionally requires explicit `audio-to-video` /
  `video-to-video` allowlist entries, which are excluded from omitted defaults.
  Ingredients also requires the default-dark `appearance-to-video` path and
  matching appearance worker feature. Its distinct fixed profile remains
  non-serving/unpriced; adding schema support is not activation authority.
  Core needs FFmpeg/ffprobe before MP4 source admission; package pinning,
  decoder isolation, input privacy/retention and joint qualification remain
  gates. Do not change production allowlists merely because schemas exist.
  Linux MP4 verification requires Bubblewrap and prlimit as well as the codecs.
  On restricted Ubuntu, review and load the distribution's
  `bwrap-userns-restrict` policy from `apparmor-profiles` before an actual
  service-user isolation test. Retain the host restriction and policy's
  capability-denying child stack; do not use an unconfined custom profile,
  privileged decoder or global namespace/AppArmor bypass. Tool installation
  and CI policy success do not prove the production service can decode safely.
  `MEDIA_PRIVATE_SOURCE_BUCKET` defaults empty and must differ from output
  storage. Keep its r2.dev/custom-domain access disabled, verify anonymous
  denial and expiry, and review its one-day source lifecycle before admitting
  any governed LTX input. Legacy image sources are not migrated by this gate.
  Apply migrations through `0045` before starting this release, even with
  `MEDIA_ASYNC_ENABLED=0`: the lifecycle outbox reads `grid_media_jobs`.
  `MEDIA_ASYNC_ADMISSION_MODE=pilot` is the default. A reviewed enabled pilot
  requires one to ten distinct private account UUIDs in
  `MEDIA_ASYNC_CANARY_ACCOUNTS` and an aware `MEDIA_ASYNC_CANARY_UNTIL` no more
  than 24 hours ahead at startup. Normal expiry closes new jobs, not Core
  startup: recovery continues even if the enable flag remains set. Use a future
  expiry for a new pilot. Public `open` requires both
  pilot fields cleared and separate approval; it cannot silently ignore a cohort.
  Contract-backed waiting/chat video is closed in pilot mode. These admission
  controls do not change global charging, tariffs or model-use clearance.
  Defaults are `MEDIA_ASYNC_CONCURRENCY=4`, `MEDIA_JOB_SWEEP_SECONDS=10`,
  `MEDIA_JOB_DISPATCH_LEASE_SECONDS=60`, and `MEDIA_JOB_BATCH_SIZE=10`.
  Turning admission off preserves authenticated replay/status, drains existing
  holds and continues accepted-job private-input cleanup. A separate minute loop deletes only
  exact recorded private keys after committed financial closure; failures do
  not block the queue or billing. Preserve the old private bucket and scoped
  credentials until pending records drain when rotating buckets. Changing the
  R2 account or rebinding a recorded source bucket as public output fails
  cleanup closed. The provider one-day lifecycle still covers pre-admission
  orphan inputs; ComfyUI and output/attempt retention remain separate gates.
  Never drop journal history or restore a worker
  handler without the execution fence. Enablement additionally requires
  qualified `async-video-resume-v1` workers, durable prompt/result recovery,
  restart/crash proofs and supervised billing/storage canaries. The current
  media-worker candidate does not advertise that capability; do not add it by
  hand merely to bypass the gate. See `docs/architecture/LTX_2_5_VIDEO.md`.
- Core `ca1f2a12` / Alembic `0042` was selected September28 at 19:00:18 UTC.
  PR/main CI, production backup/restore, drained cutover and authenticated
  reduced-account response checks passed; all six workers returned. Billing,
  generation admission, payout controls and worker runtimes were unchanged.
  See `docs/MEDIA_ONBOARDING_FOLLOWUP.md`. The pre-existing missing reward-policy
  payout failure is not fixed by this release. The later September28 22:36 UTC
  configuration-only cutover admitted image editing/batches after real staged
  billing qualification. Six paid production canaries passed; image-worker
  `50476b9` and Gallery PR #39 are live. Other controls are unchanged and
  timeline/3D remain closed. See the same follow-up for exact receipts/rollback.
- Core `3384c223` / Alembic `0042` is live as of September17 01:55 UTC.
  Exact-main CI, production restore, drained cutover and a real paid Qwen Chat
  canary passed. Charging, daily free spending, compensation contracts and
  worker-payout controls are unchanged. The owner approved closing the previous
  observation as partial for this customer fix: it was cancelled at 01:54:42 UTC.
  Preserve its records/HMAC. The replacement did not start: one stale candidate
  blocks the fresh cohort gate. Do not backdate, bypass gates, extend earning
  dates or claim a running observation. See the September17 rollout record.
- On September16 at 13:49:46 UTC, `shadow_20260916_observation_v4` started on
  unchanged `d1aafcf4` / `0042`, with preview.20 baseline and no upgrade overlap.
  The observer flag is on; routing and economic authority remain absent.
  Preserve the protected run HMAC and exact implementation for its evidence.
  The September17 closure above supersedes its active-run posture. Its original scheduled end was
  September23 13:49:46 UTC, separate from compensation's September22 end/grace.
  Client GPT backend changes/restarts/canaries are expressly out of scope.
- Core `d1aafcf4` / Alembic `0042` was selected September 15 at 22:27:13 UTC.
  It supersedes `64d38951`, recording rejected observer events durably before
  acknowledgement. Exact-main CI, production restore and drained cutover
  passed; all nine workers returned. Environment, payout controls, campaigns
  and observer-off state were unchanged at that deployment. The separate
  September16 activation above supersedes only the observer/cohort posture.
- Core `64d38951` / Alembic `0042` was selected September 15 at 21:38:17 UTC.
  This supersedes `bc49519c` while preserving environment, charging, worker
  payout controls, qualification and compensation contracts. Shadow observation
  stays disabled; its code fix is not experiment activation or routing authority.
- Core `f5211254` / Alembic `0042` was selected September 15 at 13:53:06 UTC.
  This supersedes `d606e4d8` as the running release but preserves its runtime
  controls. No validator campaign or transfer was created by deployment.
  Following explicit owner control confirmation, three reviews and campaign
  `validator-pilot-20260915` committed at 14:11 UTC. Frozen earning dates are
  September 15-22 at 14:30 UTC; earliest finalization is September 22 at 15:30
  UTC. Three members and a 25,000 AIPG/operator cap imply at most 75,000 AIPG
  allocated from the approved 100,000 ceiling. No transfers occurred. Do not
  reset qualification, append members or substitute node keys for consent.
- Core `d606e4d8` / Alembic `0042` was selected on September 13 at 02:44:58 UTC
  for versioned token-limit scoring compatibility. The environment and payout
  timer controls were unchanged from the live `93a21eec` funded-lineage pilot.
  Validator compensation, media and shadow gates remain off. Published
  preview.18 does not advertise the new scorer. Preview.19 was published and
  admitted at 03:16:03 UTC; its first owned-node report is instruction-only.
  Combined preview.20 was published at 03:53:15 UTC and admitted at 03:57:06
  UTC, after four-platform native qualification and local payload/provenance
  checks. Fresh v2 and committed-empty live delivery remain gates before
  public promotion. Preserve v1 historical semantics.
- Demand-bounded rewards are candidate code, not an activated production rule.
  `WORKER_REWARD_DEMAND_POLICIES=[]` retains the existing sender. Activation
  needs a reviewed valuation and future whole-hour boundary, preserved policy
  history, funded canary/replay and old-plan equivalence. After a v2 plan exists,
  retain the cap-aware sender; rollback must stop payouts, not restore an older
  sender that lacks the downgrade guard. Policy exhaustion holds new payout
  hours while keeping inference and prior frozen retries available. See
  `docs/architecture/POST_LAUNCH_HARDENING.md` for the full remaining scope.
- Global demand charging is active on immutable `793fe904` / Alembic `0041`.
  The mode changed at `2026-09-10T00:14:57Z`; public generation reopened at
  `00:15:53Z` after eighteen no-dispatch rejection checks. Six processes use
  `GRID_CHARGING_MODE=on`; only that environment field changed. Preserve the
  seven admitted capabilities, service caps, exact promotion campaign and
  reward cutoff. On September 10, six browser-submitted Chat/Art/Music/Director
  first-frame jobs reconciled exactly; plain Gallery video passed. Gallery
  `24a0fbf0` subsequently fixed and live-verified the disabled Director timeline
  rejection state without a charge or automatic retry; timelines stay disabled.
  The payout timer resumed at `14:19:51Z` only after a supervised frozen-hour
  send, independent Transfer proofs and a zero-transfer replay. Its first
  scheduled cycle passed too. Historical accrual/disputes remain excluded.
  See the launch record for evidence, limits and rollback.
- The preceding production release selected immutable `3ab6d933` / Alembic `0041` at
  `2026-09-09T22:44:38Z`, including the frozen payout and screening fixes.
  Restored-production migration, drained restart, six-process configuration
  and existing canary/global balance reconciliation passed. The plan table is
  empty at that point and historical payout fingerprints were unchanged. At that deployment
  charging was allowlisted; the later global cutover above supersedes that
  posture. The subsequent supervised payout proof above supersedes the stopped
  sender posture, not the exact admission/cutoff/settings. The code deployment
  alone was not a supervised payout proof.
- The frozen worker-payout code requires Alembic `0041` before any sender.
  It adds empty hourly plans, never adopts historical obligations, and does not
  authorize a timer restart. If this code is rolled back, keep payouts paused:
  an older sender can recompute amounts or retry excluded historical rows.
  API rollback may retain the additive table. Never downgrade nonempty plans.

- The previous release selected immutable `94be0cc1` / Alembic `0040` for atomic queue
  retry recovery at `2026-09-09T20:03:49Z`. Fresh restore, drained restart,
  six-process configuration, funded text and six-route empty-service canaries
  passed without changing the cohort or payout timers. Preserve the existing
  admission list and reward cutoff; see the launch evidence for exact limits
  and rollback. This does not authorize global billing or payout resumption.
  The launch record separately documents 27 local Uvicorn/PG/Redis crash tests
  and alert transport delivery; these do not count as elapsed observation or
  authorize a new runtime deployment.
  The subsequent consumer review verifies Chat's canonical image provider and
  current-load payout query timings. Those older observations do not replace
  the frozen-period deployment evidence above or authorize a payout restart.

- Media-result recovery requires Alembic `0040` before the candidate starts.
  Verify backup/restore and schema parity with the exact release. Ordinary
  holds and terminals reference the new columns even with charging off.
  Keep additive columns on rollback; never drop retained recovery evidence.
  Core recovery alone does not close Gallery/Director's restart/retry gate.

- Env names in `env.template`, systemd, code, and docs must match exactly.
- `TYPED_VIDEO_WAITING_ENABLED` defaults off and keeps contract-backed legacy
  waiting/chat video separate from public async rollout. Enabling public
  async jobs alone must not expose those distinct lifecycles. The waiting
  opt-in additionally requires enabled open async admission; existing legacy
  non-governed models, old receipt recovery and terminal settlement remain.
- `MEDIA_ASYNC_SOURCE_ADMISSION_MODE` independently controls uploaded sources
  under public async admission. Default off admits no new public source work.
  A source `pilot` requires one to ten canonical accounts in
  `MEDIA_ASYNC_SOURCE_CANARY_ACCOUNTS` and a fixed aware
  `MEDIA_ASYNC_SOURCE_CANARY_UNTIL` within24hours; expiry preserves startup and
  receipt recovery. Source `open` requires both pilot fields cleared. Keep the
  generated-video gate and charging unchanged while qualifying this cohort.
  Audio/video path flags remain separately required. Linux source verification
  requires FFmpeg/ffprobe, bubblewrap and prlimit under actual service isolation;
  never grant broad privileges or bypass the sandbox to make a canary pass.
- A billing incident must close new public generation, not merely disable
  charging. Gate exact generation routes, select explicit empty admission,
  and preserve settlement/recovery and stopped payouts. `GRID_CHARGING_MODE=off`
  alone leaves free generation open. Follow the runbook's closed-generation
  rollback and separate global-activation sequence.
- The billing runbook's operator-response table assigns action to the sole
  maintainer. Discord delivery is best-effort, not a durable incident ledger or
  proof of acknowledgment; pair it with direct read-only reconciliation.
- Old `GRID_PROBE_*` coordinator sampling variables are retired. Disable the
  old enable flag when deploying so rollback cannot restart the sampler; the
  new release ignores it and cannot dispatch ordinary unreserved canary work. Keep current
  validator and manager setup flags unchanged, and preserve historical ledger
  records for separate reconciliation.
- `GRID_REWARD_MONITOR_ENABLED` defaults off. It checks the last completed UTC
  hour on the billing-monitor cadence, using the current reward boundary and
  purchased fraction. It warns about unrestricted-pool exposure, not transfers;
  walletless account accrual is included. It does not certify historical
  backpay, policy economics, or a stopped/running payout timer. Query failures
  report unknown status, and monitoring never changes allocations or resumes
  payouts. A legacy or unset boundary can deliberately produce warnings while
  payouts are paused for reconciliation.
- `GRID_TREASURY_MONITOR_ENABLED` is a separate default-off balance warning.
  Activation requires the actual payout wallet and AIPG token, `BASE_RPC_URL`,
  and positive ETH-wei/AIPG-raw thresholds. It runs on the billing-monitor
  cadence and never invokes a signer or sender. Verify the public addresses
  against the deployed payout configuration; do not copy private keys into it.
  RPC failure means balances are unknown. Threshold alerts do not authorize a
  refill, payout, or economic-policy change.
- Preserve the exact `WORKER_REWARDS_PAID_ONLY_SINCE` boundary once activated.
  Production fixed it at `2026-09-09T16:26:27+00:00`; the launch evidence records
  the immutable release, historical-equivalence proof and private backup state.
  Clearing or moving it would re-admit unbilled work or reprice history. Keep
  payout timers stopped through demand/reward reconciliation; never treat
  switching charging off as permission to resume unbounded free emissions.
- Production explicitly narrowed `GENERATION_ENABLED_PATHS` at
  `2026-09-09T17:06:56Z` to chat, Responses, Anthropic, single image, video,
  image-to-video and audio. Batch images, img2img, video timeline and 3D are
  disabled pending their remaining qualification. Preserve this restriction
  during rollback rather than restoring implicit all-path admission. Consult
  the current table in `DEMAND_BILLING_LAUNCH_2026_09_08.md`; older snapshots
  in that file are historical, not the active configuration. Charging remains
  allowlisted and the path restriction does not authorize payout resumption.
- `VALIDATOR_COMPENSATION_OPERATOR_ENABLED` defaults off. Apply `0039` and
  ship/test matching node-app and Console consent screens before enabling it.
  Current account association and fresh human proof remain mandatory; this
  API only collects signatures for private review and cannot send payments.
  Rollback retains pending proof and disables the flag. See
  `docs/architecture/VALIDATOR_PAYOUT_CONSENT.md` for the complete route contract.
- `VALIDATOR_COMPENSATION_SEND_ENABLED` defaults off and has no timer hook.
  Apply `0038` before any updated worker payout process runs, because its nonce
  lookup reads validator payment history even while disabled. Upgrade all
  treasury-sharing senders before enabling validator transfers. After any
  validator nonce is bound, retain the table and nonce-aware worker allocator
  on rollback; disable the validator flag instead. Budget approval, recipient
  consent and a supervised transfer are separate from a dark code deployment.
- `GRID_CHARGING_ALL_MODEL_SERVICES` is a JSON array of exact capped direct
  service IDs, empty by default. Use this for sponsored auto-model demos without
  expanding the user/model charging cohort. `GRID_CHARGING_MODE=off` still wins.
  Revalidate every current direct key's active service, canonical account, caps
  and spendable balance before a cohort expansion. Zero balance must reject;
  do not synthesize purchased credit. After restart, wait for each tested
  modality's workers, not merely healthy Redis, before a live rejection canary.
  For text format canaries, verify both advertised API format and a positive
  price for the exact model before expanding the account/model cohort; general
  network availability alone proves neither. Preserve unsupported/unpriced
  rejection evidence separately from successful paid-format tests.
  Verify the authenticated credit summary says all models are charged before
  attempting an unfunded generation, and revoke any temporary test key.
- Promotional spending requires both the global emergency gate and a non-empty
  exact `GRID_PROMO_SPENDABLE_CAMPAIGNS` allowlist. Never use or emulate a
  wildcard; enable reviewed builder cohorts independently of welcome grants.
- Public route split is intentional:
  - `/v1/*`, `/`, `/health`, `/docs`, and `/openapi.json` -> Grid API.
  - The two exact OAuth `/.well-known/*` metadata routes -> Grid API; all other
    well-known paths remain under the static fallback.
  - `/etc/nginx/aipg-api.d/*.conf` may add reviewed exact locations such as
    `/v1/mcp`; no overlay may add a broad prefix proxy.
  - `/v1/oauth/introspect` returns an exact public `404`; the co-located MCP
    process reaches it only through loopback Uvicorn transport.
  - `/api/v2/*` and `/v2/*` -> static `410 Gone`; no legacy process.
  - `/metrics` should remain restricted by nginx.
- Existing-host deployments install the versioned Nginx site from the selected
  release, preserve reviewed files in `/etc/nginx/aipg-api.d`, run `nginx -t`,
  and reload Nginx. Do not let the base live site drift from
  `nginx/aipg-api.conf`.
- `/health` and `/v1/status/network` must report the reviewed immutable commit;
  `GRID_BUILD_COMMIT`, when set, must be the full 40-character release SHA.
- Secrets belong in `/etc/aipg/grid.env` with restrictive permissions, never in
  git, command argv, or logs.
- Host and image builds install only binary wheels from
  `requirements-grid.lock` with `--require-hashes`; production must never
  resolve floating source requirements, build an sdist, or upgrade pip during
  a release.
- Deployment scripts may be destructive on fresh VMs. Do not run them locally
  from an agent without explicit user approval.

## Work Guidance

- During a reviewed validator upgrade, set `VALIDATOR_COHORT_UPGRADE_VERSION`
  to one exact release tag while preserving `VALIDATOR_COHORT_BASELINE_VERSION`.
  For multiple reviewed upgrades, clear the singular setting and use the JSON-array
  `VALIDATOR_COHORT_UPGRADE_VERSIONS` (at most seven distinct exact release tags).
  Never set both. Verify all listed versions remain eligible and preserve stored qualification history.
  Shadow observation must stay disabled during this overlap. After migration,
  promote the new baseline and clear both upgrade settings; restarting a node must
  never reset its signing identity or qualification timestamps.

- When adding services, document ports, health checks, restart behavior, and
  firewall/nginx impact.
- `GRID_SALT` stays server-side. The developer console has no local DB/salt path
  and must not receive it.
- `VALIDATOR_PAIRING_ENABLED` is a separate default-off account-visibility gate.
  Apply Alembic `0030` before enablement and ship the matching Console and local
  node-app consent flows first. The audience and approval URL must be explicit
  HTTPS values. Rollback disables the flag and preserves node identities and
  association tables. This gate never activates validator economics.
  A supervised pilot may keep that global flag off and set
  `VALIDATOR_PAIRING_CANARY_ACCOUNTS` (JSON array, maximum ten canonical UUIDs)
  plus `VALIDATOR_PAIRING_CANARY_UNTIL` (timezone-aware ISO deadline, at most
  24 hours ahead at startup). Include both the test node and test human accounts.
  Public capability advertisement remains false. Remove test links before
  expiry, then clear the allowlist; an expired pilot retains links but cannot
  read or remove them until deliberately reauthorized. Keep IDs private. Full
  rollback clears the pilot as well as disabling the global flag.
- `GRID_SIWE_ALLOWED_DOMAINS` is the exact frontend authority allowlist for
  wallet-login challenges. Keep `GRID_LEGACY_SIWE_VERIFY_ENABLED=0`; it is an
  emergency client-migration switch, not a permanent compatibility mode.
- Validator probe leases must exceed the worker-response timeout and keep a
  small bounded retry budget. Do not make targeted validation an unlimited
  free-inference path.
- `VALIDATOR_TEXT_GROUP_MIN_INTERVAL_SECONDS` limits creation of real text
  workloads per worker/model (default one hour; Core enforces a five-minute
  floor). `VALIDATOR_HISTORY_RETENTION_DAYS` and
  `VALIDATOR_HISTORY_SWEEP_SECONDS` bound finalized assignment/group machinery;
  signed attestations are preserved. Keep `env.template`, `config.py`, and the
  validator runbook aligned when changing these controls.
- `VALIDATOR_COHORT_MONITOR_ENABLED` starts an aggregate-only read path inside
  Core. It may emit redacted transition alerts for stale candidates, assignment
  completion/evidence regressions, probe errors, disagreement, version drift,
  fresh baseline registrations waiting for operator review, and duplicate
  reviewed control groups. A waiting-for-review count is an intake prompt, not
  an independence claim or qualification transition. The monitor never changes
  qualification or any economic/routing state. Keep its interval, window, and
  frozen baseline in `env.template`, `config.py`, and the cohort runbook aligned.
- `VALIDATOR_SHADOW_OBSERVER_ENABLED` is a separate, default-off seven-day
  advisory-observation gate. Migrations `0032`, `0033`, and `0034` must all be applied;
  `0032` creates the observer records, `0033` enforces one running experiment,
  and `0034` adds exact privacy-safe ledger correlation
  at the database boundary. Applying either migration or deploying report
  tooling does not permit a run to start. Enable it only after three current,
  participating, independently reviewed operator groups have produced real
  finalized quorum and the frozen PostgreSQL migration/concurrency, replay, and
  no-side-effect verification reference is recorded. Keep
  `VALIDATOR_SHADOW_SAMPLE_SECONDS=300`; no router or economic unit may consume
  shadow state. `VALIDATOR_SHADOW_RETENTION_DAYS` is reserved configuration
  until the future collector ships a tested pruner, so do not claim that dark
  records are currently deleted. Rollback is flag-off.
- `OAUTH_AUTHORIZATION_RETENTION_SECONDS`,
  `OAUTH_UNUSED_CLIENT_RETENTION_SECONDS`, and `OAUTH_STATE_SWEEP_SECONDS`
  bound unauthenticated OAuth operational storage. Keep at least one hour of
  retention and do not disable cleanup during an OAuth rollback.
- Keep `VALIDATOR_SEALED_ASSIGNMENTS_ENABLED=0` through the compatible-node
  rollout. Merge and upgrade the validator fleet first, deploy the compatible
  Core second, verify old unsealed operation, then enable the flag in a
  supervised evidence-only canary. Roll back the flag, not the database, if a
  node cannot verify terminal disclosure. This flag never enables routing,
  rewards, strikes, or slashing.
- Apply Alembic `0024` before code that issues media assignments; it adds the
  group execution lease and shared frozen-witness columns read by that path.
- Apply Alembic `0027` before starting the bond-sync loop or selecting bonded
  references. It adds the finalized block/facet proofs and durable sync cursor;
  deploying the code first will fail and must not be attempted.
- Apply Alembic `0028` before reviewing worker common-control groups or selecting
  media candidates/references under the independent-control policy. It adds the
  private identity-bound review table and intentionally backfills no trust;
  verify the table remains empty after a dark deployment.
- Apply Alembic `0029` before starting any release with compensated-audit
  terminal integration, even while scheduling is off: every ordinary terminal
  checks the private audit table. Deploying code first would break ordinary
  settlement. A dark migration must leave both audit tables empty and create no
  counters, jobs, ledger rows, or worker acknowledgements. Schema presence is
  not permission to enable scheduling. No scheduler or runtime flag can create
  an audit hold yet; explicit default-off configuration and scheduler review
  remain separate gates.
- `VALIDATOR_MEDIA_PROBE_ENABLED` is not a standalone launch switch. Keep it off
  until the reviewed bond contract/verifier/minimum, finalized reference sync,
  governed deterministic recipe/model digest, independent operators, immutable
  R2 witness retention, and supervised preview gates are all proven.
- `VALIDATOR_MEDIA_BOND_SYNC_ENABLED` is independently dark. Its Diamond
  address, reviewed verifier version, primary Base RPC, independently operated
  confirmation RPC, bounds, and interval must be set together. The exact facet
  runtime is compiled into the Core release and must never be supplied through
  operator configuration. The
  cache refreshes only when both RPCs return the same complete finalized
  snapshot and prior finalized anchor. A fault immediately invalidates that
  authority's cached eligibility. Enabling the loop does not enable media assignments or
  create/activate a reference row.
- Do not enable the backup timer merely because its unit was installed. Run one
  backup, restore it into the generated scratch database, migrate with the exact
  candidate release, and inspect the proof first.
- The backup one-shot stays UID 0 for its protected state directory and restore
  tooling, but its primary group must be `aipg`. Its empty capability set
  removes DAC bypass, while immutable releases live below `0750 aipg:aipg`
  directories; changing the group back to `root` makes the unit unable to
  execute its own versioned script.
- Verify group read/traverse on the selected release root and `scripts/`, and
  group read/execute on `scripts/backup_postgres.sh` (normally `0750 aipg:aipg`).
  UID 0 with empty capabilities cannot bypass a `0700` release owned by `aipg`.
  Check exact paths; never broaden secret-file permissions recursively.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- `systemctl start aipg-postgres-backup.service` followed by
  `scripts/prove_postgres_restore.sh` on the target host before enabling its
  timer.
- `grep -qx 'Group=aipg' deploy/systemd/aipg-postgres-backup.service`.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.
