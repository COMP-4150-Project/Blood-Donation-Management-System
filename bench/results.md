Dataset: requests 9,396, donors 6,000, donations 11,609, units 25,251. Median of 5 warm runs.

| Operation | Target | Before (ms) | After (ms) | Plan change |
|---|---|---|---|---|
| Q01 Open request queue |  | 6.13 | 6.66 | no change |
| Q02 Red cell stock against minimum levels |  | 7.66 | 11.43 | no change |
| Q03 Donors to call for the worst shortfall |  | 194.92 | 14.64 | Seq Scan on donation -> Index Only Scan using donation_donor_time on donation |
| Q04 A donor's own donation history |  | 4.01 | 3.32 |  -> Index Scan using donation_donor_time on donation |
| Q05 Lookback for a flagged donor |  | 30.38 | 2.22 | Index Scan using request_unit_unit_id_active on request_unit, Seq Scan on donation -> Bitmap Heap Scan on donation, Bitmap Index Scan on donation_donor_time, Index Scan using donation_donor_time on donation, Index Scan using request_unit_unit on request_unit |
| Q06 Admin overview |  | 10.81 | 9.82 | no change |
| Q07 Monthly wastage rate by component |  | 20.94 | 20.71 | no change |
| Q08 Request fulfilment by hospital and urgency (last 6 months) |  | 9.16 | 9.03 | no change |
| Q09 Red cell supply and demand by blood group (last 90 days) |  | 18.85 | 15.33 |  -> Bitmap Heap Scan on blood_unit, Bitmap Index Scan on unit_available, Bitmap Index Scan on unit_expiring |
| Q10 How soon donors come back |  | 8.44 | 8.22 | no change |
| Q11 Lapsed regular donors to win back |  | 5.84 | 5.86 | no change |
| Q12 Where O-negative red cells went |  | 8.05 | 7.22 | Seq Scan on request_unit -> Index Scan using request_unit_unit on request_unit |
| Q13 Why donors are deferred (last 12 months) |  | 0.61 | 0.41 | no change |
| PR-01 eligible_donors() | < 2 s | 5.58 | 6.7 |  |
| PR-02 allocate() 10 units | < 1 s | 56.88 | 13.88 |  |
| PR-03 inventory dashboard | < 3 s | 18.42 | 20.41 |  |
