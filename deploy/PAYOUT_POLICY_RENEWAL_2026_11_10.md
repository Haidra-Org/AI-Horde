# Worker payout sender restored after expired reward policy

## Posture

Production release `grid-core-0d2ee0b7` was unchanged. No code was deployed,
no service was restarted, and no timer state changed. One line of
`/etc/aipg/grid.env` was edited at `2026-11-10T03:52:41Z` with the owner's
approval.

## Incident

`aipg-payout.service` failed on every hourly run from `2026-09-14T02:00Z` to
`2026-11-10T03:01Z` with:

```text
ValueError: payout window lacks a reviewed demand reward policy
```

`WORKER_REWARD_DEMAND_POLICIES` held a single window,
`2026-09-13T02:00Z` to `2026-09-14T02:00Z` (2,000 micro-USD per AIPG, 8,500
bps). Windows last at most seven days and nothing was appended, so every later
hour failed closed, as designed. The timer stayed enabled and active. Nothing
monitors the unit, so the failure went unnoticed for eight weeks.

Before the window expired the sender was healthy but idle. From
`2026-09-10T18:00Z` each run logged `sent: 0`, because since the paid-only
cutoff (`2026-09-09T16:26:27Z`) only work bought with purchased credit earns.

## Change

The first window was preserved byte-for-byte. Fourteen contiguous windows were
appended (15 total):

| Windows | Valuation | Reason |
| --- | --- | --- |
| `2026-09-14T02:00Z` to `2026-11-10T05:00Z` (nine) | 2,000 micro-USD per AIPG | Held period keeps the valuation reviewed in September |
| `2026-11-10T05:00Z` to `2026-12-15T05:00Z` (five) | 1,200 micro-USD per AIPG | Spot on Base Uniswap AIPG/USDC was $0.001189 on 2026-11-09, with about $2,780 of pool liquidity |

Worker share stays 8,500 bps. The list was validated against the deployed
release's `WorkerRewardDemandPolicy` model and contiguity rule before writing.
The previous file is retained at
`/etc/aipg/grid.env.before-reward-policies-20261110T035241Z` (root, 0600). Only
line 166 differs.

## Verification

- Dry run under the unit's user, working directory and environment file, with
  no `--send`, for `hour-2026-11-10T02`: exit 0, zero accounts.
- First scheduled run at `2026-11-10T04:00:38Z` for `hour-2026-11-10T03`:
  `sent 0, pending 0, accrued 0, skipped 0, failed 0`, unit finished
  successfully. No unit is in a failed state.

## Not done

- **No catch-up.** Read-only weekly previews found purchased work in the held
  period: about 178 AIPG in total across at most four accounts between
  `2026-09-14T02:00Z` and `2026-11-10T03:00Z`. Weekly previews are not hourly
  plans, so the exact owed amounts differ. Paying them means running the
  sender for each held hour, which creates one plan row per hour. That needs a
  separate decision.
- The interval `2026-09-10T18:00Z` to `2026-09-13T02:00Z` precedes the first
  policy. A preview showed one account with funded work there; the hourly
  runs at the time sent nothing. Not investigated.
- No transfer was sent by this change.

## Follow-up

- The list ends at `2026-12-15T05:00Z`. Payouts will fail closed again after
  that unless a window is appended. Review the valuation when extending.
- Add monitoring for a failed `aipg-payout.service`.
- The cap pays at most 85% of purchased consumption. At current demand that is
  cents per week. Any subsidy for uptime or free work is a separate budget and
  is not introduced here.

## Rollback

Restore the retained file. The sender then fails closed again on the next
hour. Do not remove or edit the first window, and do not shorten windows that
already have plans.
