Dataset: requests 43,822, donors 10,000, donations 54,107, units 117,332. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 33.16 | 28.68 | no change |
| Q02 Red cell stock against minimum levels |  | 25.44 | 33.77 | no change |
| Q03 Donors to call for the worst shortfall |  | 821.88 | 39.32 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation |
| Q04 A donor's own donation history |  | 10.59 | 7.69 |  -> Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 456.1 | 3.63 | Index Scan using donation_pkey on donation, Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Bitmap Heap Scan on donation, Bitmap Index Scan on donation_donor_time, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 25.23 | 25.68 | no change |
| Q07 Monthly wastage rate by component |  | 80.92 | 85.7 | no change |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 23.73 | 23.48 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 92.8 | 65.84 |  -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring, Seq Scan on donation |
| Q10 How soon donors come back |  | 41.69 | 40.62 | no change |
| Q11 Lapsed regular donors to win back |  | 20.27 | 17.09 | no change |
| Q12 Where O-negative red cells went |  | 27.85 | 29.21 | Seq Scan on blood_unit, Seq Scan on donation, Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit, Parallel Seq Scan on blood_unit, Parallel Seq Scan on donation |
| Q13 Why donors are deferred (last 12 months) |  | 1.01 | 0.86 | no change |
| PR-01 eligible_donors() | < 2 s | 8.38 | 8.39 |  |
| PR-02 allocate() 10 units | < 1 s | 92.44 | 49.67 |  |
| PR-03 inventory dashboard | < 3 s | 70.9 | 72.91 |  |
| PR-05 expire_units() 50k units | < 60 s | 5680.3 | 6378.43 |  |

PR-05 times the nightly job over 50,000 expired units with every trigger on: median of 3 runs with indexes, 1 run without.
