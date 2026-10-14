Dataset: requests 43,822, donors 10,000, donations 54,107, units 117,332. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 30.42 | 30.03 | no change |
| Q02 Red cell stock against minimum levels |  | 23.47 | 36.43 | no change |
| Q03 Donors to call for the worst shortfall |  | 872.32 | 68.32 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q04 A donor's own donation history |  | 11.17 | 8.24 |  -> Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 471.38 | 3.92 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Bitmap Heap Scan on donation, Bitmap Index Scan on donation_donor_time, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 44.91 | 50.02 | Seq Scan on screening -> Index Only Scan using screening_donor_time on screening |
| Q07 Monthly wastage rate by component |  | 100.33 | 92.1 | no change |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 28.06 | 24.22 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 91.9 | 69.85 |  -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 42.63 | 41.08 | no change |
| Q11 Lapsed regular donors to win back |  | 28.74 | 26.05 | Seq Scan on screening -> Index Only Scan using screening_donor_time on screening |
| Q12 Where O-negative red cells went |  | 23.55 | 31.55 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 0.77 | 0.82 | no change |
| PR-01 eligible_donors() | < 2 s | 28.87 | 31.93 |  |
| PR-02 allocate() 10 units | < 1 s | 97.47 | 50.76 |  |
| PR-03 inventory dashboard | < 3 s | 72.27 | 78.37 |  |
| PR-05 run_nightly_expiry() 50k units | < 60 s | 6274.86 | 6410.81 |  |

PR-05 times the nightly job over 50,000 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
