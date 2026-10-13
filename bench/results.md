Dataset: donors 10,000, donations 54,107, units 117,332, requests 43,822. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 26.33 | 30.44 | no change |
| Q02 Red cell stock against minimum levels |  | 20.59 | 25.42 | no change |
| Q03 Donors to call for the worst shortfall |  | 616.74 | 33.39 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation |
| Q04 A donor's own donation history |  | 9.19 | 5.33 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 364.76 | 2.73 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Index Only Scan using donation_donor_time on donation, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 21.64 | 17.16 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation |
| Q07 Monthly wastage rate by component |  | 78.76 | 75.05 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit ->  |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 22.31 | 21.53 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 73.66 | 56.72 | Index Only Scan using blood_unit_donation_id_component_code_key on blood_unit -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 34.92 | 34.12 | no change |
| Q11 Lapsed regular donors to win back |  | 11.05 | 9.34 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation |
| Q12 Where O-negative red cells went |  | 21.86 | 26.74 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 0.59 | 0.67 | no change |
| PR-01 eligible_donors() | < 2 s | 6.98 | 7.04 |  |
| PR-02 allocate() 10 units | < 1 s | 105.59 | 42.12 |  |
| PR-03 inventory dashboard | < 3 s | 57.67 | 57.47 |  |
| PR-05 expire_units() 50k units | < 60 s | 4998.18 | 5664.65 |  |

PR-05 times the nightly job over 50,004 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
