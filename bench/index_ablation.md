# Index evidence (leave-one-out)

Dataset: 4,751 deferrals, 58,620 screenings, 71,369 request_unit, 54,107 donations, 117,332 units, 55,128 audit_log. Each workload is timed with every candidate index present and again with only that index removed, in 3 rounds that alternate which side runs first. Within a round each figure is the median of warm runs (routines and triggers exclude their setup); the table shows the median across rounds and the lowest and highest speedup seen in any round. 'Used' means the index appears in the actual plan (EXPLAIN ANALYZE, or auto_explain for statements run inside routines and triggers).

Decision rule (computed by the script): an index is kept if, on at least one of its workloads, it is used in the plan and in every round it saves at least 1 ms or is at least 1.5x faster.

## Verdicts

- **donation_donor_time: kept.** Deciding workload: Q03 donors to call, 756.442 -> 49.601 ms (speedup 15.06-15.82x across rounds).
- **unit_available: kept.** Deciding workload: PR-02 allocate() 10 units, 64.717 -> 45.618 ms (speedup 1.35-1.52x across rounds).
- **unit_expiring: kept.** Deciding workload: run_nightly_expiry(), real night, 138.976 -> 74.192 ms (speedup 1.02-2.68x across rounds).
- **deferral_donor: dropped.** Best workload: recompute_next_eligible(), 0.9 -> 0.689 ms (speedup 1.17-1.73x across rounds); the gain was below the rule in at least one round.
- **request_unit_unit: kept.** Deciding workload: Q12 O-neg tracing, 33.353 -> 26.116 ms (speedup 1.18-1.82x across rounds).
- **screening_donor_time: kept.** Deciding workload: donation insert (triggers), 2.659 -> 0.565 ms (speedup 4.32-5.29x across rounds).
- **audit_record: kept.** Deciding workload: audit trail of one unit, 70.811 -> 0.033 ms (speedup 1116.7-2844.25x across rounds).

## Measurements

| Index | Kept | Workload | With (ms) | Without (ms) | Speedup (median) | Speedup range | Used in plan | Passes rule |
|---|---|---|---|---|---|---|---|---|
| donation_donor_time | yes | Q03 donors to call | 49.601 | 756.442 | 15.25x | 15.06-15.82x | yes | yes |
| donation_donor_time | yes | Q04 donor history | 4.753 | 8.791 | 1.85x | 1.74-2.01x | yes | yes |
| donation_donor_time | yes | Q05 lookback | 2.543 | 410.937 | 161.6x | 156.02-161.6x | yes | yes |
| donation_donor_time | yes | Q10 return interval | 36.881 | 38.299 | 1.04x | 0.91-1.04x | no | no |
| donation_donor_time | yes | donation insert (triggers) | 0.578 | 2.716 | 4.7x | 4.38-9.69x | yes | yes |
| donation_donor_time | yes | recompute_next_eligible() | 0.631 | 2.736 | 4.34x | 4.07-6.52x | yes | yes |
| unit_available | yes | PR-02 allocate() 10 units | 45.618 | 64.717 | 1.42x | 1.35-1.52x | yes | yes |
| unit_available | yes | refresh_alert() | 4.655 | 6.455 | 1.39x | 1.35-1.48x | yes | yes |
| unit_available | yes | Q02 stock vs minimum | 37.513 | 38.079 | 1.02x | 1.02-1.05x | no | no |
| unit_expiring | yes | run_nightly_expiry(), real night | 74.192 | 138.976 | 1.87x | 1.02-2.68x | yes | yes |
| unit_expiring | yes | PR-05 run_nightly_expiry() 50k units | 6246.831 | 6136.735 | 0.98x | 0.94-1.01x | yes | no |
| deferral_donor | no | recompute_next_eligible() | 0.689 | 0.9 | 1.31x | 1.17-1.73x | yes | no |
| deferral_donor | no | donation insert (triggers) | 0.655 | 0.693 | 1.06x | 0.73-1.29x | yes | no |
| request_unit_unit | yes | Q05 lookback | 2.375 | 2.727 | 1.15x | 1.15-1.56x | yes | no |
| request_unit_unit | yes | Q12 O-neg tracing | 26.116 | 33.353 | 1.28x | 1.18-1.82x | yes | yes |
| request_unit_unit | yes | v_lookback for one donor | 0.32 | 0.674 | 2.11x | 0.89-2.11x | yes | no |
| screening_donor_time | yes | donation insert (triggers) | 0.565 | 2.659 | 4.71x | 4.32-5.29x | yes | yes |
| screening_donor_time | yes | PR-01 eligible_donors() | 19.898 | 18.845 | 0.95x | 0.93-0.95x | yes | no |
| screening_donor_time | yes | Q03 donors to call | 50.925 | 49.766 | 0.98x | 0.93-0.99x | yes | no |
| audit_record | yes | audit trail of one unit | 0.033 | 70.811 | 2145.79x | 1116.7-2844.25x | yes | yes |
