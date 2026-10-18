Dataset: donors 10,000, requests 43,822, donations 54,107, units 117,332. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 23.53 | 25.2 | no change |
| Q02 Red cell stock against minimum levels |  | 17.83 | 24.27 | no change |
| Q03 Donors to call for the worst shortfall |  | 624.35 | 50.63 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q04 A donor's own donation history |  | 8.8 | 5.31 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 352.51 | 2.14 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 33.87 | 32.4 | Seq Scan on donation, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q07 Monthly wastage rate by component |  | 70.45 | 69.19 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit ->  |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 21.21 | 20.18 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 63.28 | 58.36 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 32.75 | 34.59 | no change |
| Q11 Lapsed regular donors to win back |  | 21.63 | 13.73 | Seq Scan on donation, Seq Scan on donor, Seq Scan on screening -> Index Only Scan using donation_donor_time on donation, Index Only Scan using screening_donor_time on screening |
| Q12 Where O-negative red cells went |  | 17.46 | 25.51 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 0.62 | 0.62 | no change |
| PR-01 eligible_donors() | < 2 s | 25.89 | 22.31 |  |
| PR-02 allocate() 10 units | < 1 s | 74.99 | 41.88 |  |
| PR-03 inventory dashboard | < 3 s | 53.04 | 58.03 |  |
| PR-05 run_nightly_expiry() 50k units | < 60 s | 5229.61 | 5648.78 |  |

PR-05 times the nightly job over 50,000 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
