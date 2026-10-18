# Index evidence (leave-one-out)

Dataset: 4,751 deferrals, 58,620 screenings, 71,369 request_unit, 54,107 donations, 117,332 units, 343,380 audit_log. Each workload is timed with every candidate index present and again with only that index removed, in 3 rounds that alternate which side runs first. Within a round each figure is the median of warm runs (routines and triggers exclude their setup); the table shows the median across rounds and the lowest and highest speedup seen in any round. 'Used' means the index appears in the actual plan (EXPLAIN ANALYZE, or auto_explain for statements run inside routines and triggers).

Decision rule (computed by the script): an index is kept if, on at least one of its workloads, it is used in the plan and in every round it saves at least 1 ms or is at least 1.5x faster.

## Verdicts

- **donation_donor_time: kept.** Deciding workload: Q03 donors to call, 722.613 -> 52.034 ms (speedup 13.41-14.83x across rounds).
- **unit_available: kept.** Deciding workload: PR-02 allocate() 10 units, 62.237 -> 45.557 ms (speedup 1.34-1.41x across rounds).
- **unit_expiring: kept.** Deciding workload: run_nightly_expiry(), real night, 197.805 -> 82.406 ms (speedup 1.25-4.51x across rounds).
- **deferral_donor: dropped.** Best workload: donation insert (triggers), 0.719 -> 0.564 ms (speedup 1.26-1.55x across rounds); the gain was below the rule in at least one round.
- **request_unit_unit: kept.** Deciding workload: Q12 O-neg tracing, 27.829 -> 19.56 ms (speedup 1.06-1.47x across rounds).
- **screening_donor_time: kept.** Deciding workload: PR-01 eligible_donors(), 23.928 -> 20.091 ms (speedup 1.16-1.19x across rounds).
- **audit_record: kept.** Deciding workload: audit trail of one unit, 65.732 -> 0.031 ms (speedup 1640.16-2592.1x across rounds).

## Measurements

| Index | Kept | Workload | With (ms) | Without (ms) | Speedup (median) | Speedup range | Used in plan | Passes rule |
|---|---|---|---|---|---|---|---|---|
| donation_donor_time | yes | Q03 donors to call | 52.034 | 722.613 | 13.89x | 13.41-14.83x | yes | yes |
| donation_donor_time | yes | Q04 donor history | 4.722 | 8.512 | 1.8x | 1.72-1.93x | yes | yes |
| donation_donor_time | yes | Q05 lookback | 2.418 | 392.497 | 162.32x | 157.12-163.95x | yes | yes |
| donation_donor_time | yes | Q10 return interval | 36.92 | 39.737 | 1.08x | 1.02-1.08x | no | no |
| donation_donor_time | yes | donation insert (triggers) | 0.555 | 2.59 | 4.67x | 4.5-6.99x | yes | yes |
| donation_donor_time | yes | recompute_next_eligible() | 0.653 | 2.783 | 4.26x | 4.2-4.57x | yes | yes |
| unit_available | yes | PR-02 allocate() 10 units | 45.557 | 62.237 | 1.37x | 1.34-1.41x | yes | yes |
| unit_available | yes | refresh_alert() | 4.495 | 6.323 | 1.41x | 1.37-1.41x | yes | yes |
| unit_available | yes | Q02 stock vs minimum | 37.011 | 38.272 | 1.03x | 0.92-1.03x | no | no |
| unit_expiring | yes | run_nightly_expiry(), real night | 82.406 | 197.805 | 2.4x | 1.25-4.51x | yes | yes |
| unit_expiring | yes | PR-05 run_nightly_expiry() 50k units | 6157.633 | 6096.166 | 0.99x | 0.96-1.07x | yes | no |
| deferral_donor | no | recompute_next_eligible() | 0.674 | 0.816 | 1.21x | 1.16-1.79x | yes | no |
| deferral_donor | no | donation insert (triggers) | 0.564 | 0.719 | 1.27x | 1.26-1.55x | yes | no |
| request_unit_unit | yes | Q05 lookback | 2.385 | 1.425 | 0.6x | 0.44-2.67x | yes | no |
| request_unit_unit | yes | Q12 O-neg tracing | 19.56 | 27.829 | 1.42x | 1.06-1.47x | yes | yes |
| request_unit_unit | yes | v_lookback for one donor | 0.334 | 0.297 | 0.89x | 0.67-2.04x | yes | no |
| screening_donor_time | yes | donation insert (triggers) | 0.567 | 2.62 | 4.62x | 4.49-4.89x | yes | yes |
| screening_donor_time | yes | PR-01 eligible_donors() | 20.091 | 23.928 | 1.19x | 1.16-1.19x | yes | yes |
| screening_donor_time | yes | Q03 donors to call | 50.485 | 52.54 | 1.04x | 1.04-1.05x | yes | yes |
| audit_record | yes | audit trail of one unit | 0.031 | 65.732 | 2120.39x | 1640.16-2592.1x | yes | yes |
