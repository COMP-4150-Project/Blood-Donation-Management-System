# Index evidence (leave-one-out)

Dataset: 4,751 deferrals, 58,620 screenings, 71,369 request_unit, 54,107 donations, 117,332 units, 55,128 audit_log. Each workload is timed with every candidate index present and again with only that index removed, in 3 rounds that alternate which side runs first. Within a round each figure is the median of warm runs (routines and triggers exclude their setup); the table shows the median across rounds and the lowest and highest speedup seen in any round. 'Used' means the index appears in the actual plan (EXPLAIN ANALYZE, or auto_explain for statements run inside routines and triggers).

Decision rule (computed by the script): an index is kept if, on at least one of its workloads, it is used in the plan and in every round it saves at least 1 ms or is at least 1.5x faster.

## Verdicts

- **donation_donor_time: kept.** Deciding workload: Q03 donors to call, 894.404 -> 65.148 ms (speedup 13.67-14.12x across rounds).
- **unit_available: kept.** Deciding workload: PR-02 allocate() 10 units, 70.156 -> 49.148 ms (speedup 1.39-1.49x across rounds).
- **unit_expiring: kept.** Deciding workload: run_nightly_expiry(), real night, 179.194 -> 79.62 ms (speedup 1.33-3.16x across rounds).
- **deferral_donor: dropped.** Best workload: donation insert (triggers), 0.769 -> 0.597 ms (speedup 1.27-1.53x across rounds); the gain was below the rule in at least one round.
- **request_unit_unit: kept.** Deciding workload: Q12 O-neg tracing, 31.239 -> 26.158 ms (speedup 1.08-1.21x across rounds).
- **screening_donor_time: kept.** Deciding workload: donation insert (triggers), 2.975 -> 0.618 ms (speedup 4.51-5.46x across rounds).
- **audit_record: kept.** Deciding workload: audit trail of one unit, 88.536 -> 0.048 ms (speedup 1824.77-2506.31x across rounds).

## Measurements

| Index | Kept | Workload | With (ms) | Without (ms) | Speedup (median) | Speedup range | Used in plan | Passes rule |
|---|---|---|---|---|---|---|---|---|
| donation_donor_time | yes | Q03 donors to call | 65.148 | 894.404 | 13.73x | 13.67-14.12x | yes | yes |
| donation_donor_time | yes | Q04 donor history | 6.709 | 10.049 | 1.5x | 1.39-1.54x | yes | yes |
| donation_donor_time | yes | Q05 lookback | 3.468 | 465.515 | 134.23x | 128.13-138.9x | yes | yes |
| donation_donor_time | yes | Q10 return interval | 39.25 | 39.889 | 1.02x | 0.94-1.04x | no | no |
| donation_donor_time | yes | donation insert (triggers) | 0.556 | 2.986 | 5.37x | 5.3-8.11x | yes | yes |
| donation_donor_time | yes | recompute_next_eligible() | 0.608 | 2.956 | 4.86x | 4.85-5.5x | yes | yes |
| unit_available | yes | PR-02 allocate() 10 units | 49.148 | 70.156 | 1.43x | 1.39-1.49x | yes | yes |
| unit_available | yes | refresh_alert() | 5.165 | 7.078 | 1.37x | 1.08-1.42x | yes | no |
| unit_available | yes | Q02 stock vs minimum | 46.855 | 45.199 | 0.96x | 0.8-1.05x | no | no |
| unit_expiring | yes | run_nightly_expiry(), real night | 79.62 | 179.194 | 2.25x | 1.33-3.16x | yes | yes |
| unit_expiring | yes | PR-05 run_nightly_expiry() 50k units | 6776.302 | 6710.342 | 0.99x | 0.94-1.04x | yes | no |
| deferral_donor | no | recompute_next_eligible() | 0.703 | 0.868 | 1.23x | 1.19-1.54x | yes | no |
| deferral_donor | no | donation insert (triggers) | 0.597 | 0.769 | 1.29x | 1.27-1.53x | yes | no |
| request_unit_unit | yes | Q05 lookback | 3.605 | 2.478 | 0.69x | 0.54-1.49x | yes | no |
| request_unit_unit | yes | Q12 O-neg tracing | 26.158 | 31.239 | 1.19x | 1.08-1.21x | yes | yes |
| request_unit_unit | yes | v_lookback for one donor | 0.491 | 0.334 | 0.68x | 0.52-1.24x | yes | no |
| screening_donor_time | yes | donation insert (triggers) | 0.618 | 2.975 | 4.81x | 4.51-5.46x | yes | yes |
| screening_donor_time | yes | PR-01 eligible_donors() | 31.026 | 28.525 | 0.92x | 0.85-0.92x | yes | no |
| screening_donor_time | yes | Q03 donors to call | 64.864 | 61.849 | 0.95x | 0.92-0.96x | yes | no |
| audit_record | yes | audit trail of one unit | 0.048 | 88.536 | 1844.5x | 1824.77-2506.31x | yes | yes |
