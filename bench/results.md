Dataset: requests 43,822, donors 10,000, donations 54,107, units 117,332. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 26.05 | 30.05 | no change |
| Q02 Red cell stock against minimum levels |  | 23.89 | 30.96 | no change |
| Q03 Donors to call for the worst shortfall |  | 1009.25 | 67.81 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q04 A donor's own donation history |  | 11.01 | 7.99 |  -> Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 562.57 | 3.82 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Bitmap Heap Scan on donation, Bitmap Index Scan on donation_donor_time, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 68.74 | 48.81 | Seq Scan on screening -> Index Only Scan using screening_donor_time on screening |
| Q07 Monthly wastage rate by component |  | 97.77 | 92.58 | no change |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 24.05 | 23.92 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 98.77 | 76.1 |  -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 45.58 | 41.1 | no change |
| Q11 Lapsed regular donors to win back |  | 34.34 | 24.56 | Seq Scan on screening -> Index Only Scan using screening_donor_time on screening |
| Q12 Where O-negative red cells went |  | 27.27 | 31.67 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 0.84 | 1.47 | no change |
| PR-01 eligible_donors() | < 2 s | 29.73 | 32.99 |  |
| PR-02 allocate() 10 units | < 1 s | 92.5 | 51.34 |  |
| PR-03 inventory dashboard | < 3 s | 75.35 | 73.69 |  |
| PR-05 run_nightly_expiry() 50k units | < 60 s | 6351.32 | 6964.72 |  |

PR-05 times the nightly job over 50,000 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
