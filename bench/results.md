Dataset: donors 10,000, requests 43,822, donations 54,107, units 117,332. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 25.36 | 28.15 | no change |
| Q02 Red cell stock against minimum levels |  | 25.19 | 26.73 | no change |
| Q03 Donors to call for the worst shortfall |  | 689.22 | 54.47 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q04 A donor's own donation history |  | 8.95 | 6.57 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 369.82 | 2.83 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 32.99 | 30.0 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q07 Monthly wastage rate by component |  | 86.48 | 84.61 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit ->  |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 25.7 | 24.13 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 78.53 | 73.59 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 42.81 | 40.61 | no change |
| Q11 Lapsed regular donors to win back |  | 20.23 | 13.9 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q12 Where O-negative red cells went |  | 26.08 | 29.5 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 0.73 | 0.87 | no change |
| PR-01 eligible_donors() | < 2 s | 19.04 | 20.46 |  |
| PR-02 allocate() 10 units | < 1 s | 129.26 | 45.9 |  |
| PR-03 inventory dashboard | < 3 s | 71.42 | 71.3 |  |
| PR-05 run_nightly_expiry() 50k units | < 60 s | 5548.76 | 5751.26 |  |

PR-05 times the nightly job over 50,000 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
