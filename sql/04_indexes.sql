SET search_path = vytal;
-- PostgreSQL does not index foreign keys on its own. Every index below passed the rule bench/index_ablation.py applies on the
-- full-size bench dataset (leave-one-out, 3 rounds, results in bench/index_ablation.md): on at least one of its workloads it is
-- used in the plan and, in every round, saves at least 1 ms or is at least 1.5x faster. deferral_donor (ON deferral (donor_id))
-- was measured the same way and failed, so it is not created. Numbers are median ms without -> with the index.

-- latest donation per donor: Q03 756 -> 50 ms, Q05 411 -> 2.5 ms, Q04, and recompute_next_eligible() on every donation (2.7 -> 0.6 ms)
CREATE INDEX donation_donor_time ON donation (donor_id, donated_at DESC);
-- allocate() and the low-stock count in refresh_alert(): only Available units are searched (PR-02 65 -> 46 ms)
CREATE INDEX unit_available ON blood_unit (facility_id, component_code, expires_at) WHERE status = 'Available';
-- run_nightly_expiry(): finds the night's expiring units without scanning every unit (a real night 139 -> 74 ms). At PR-05 scale
-- (50,000 units, 43% of the table) a scan is as fast and the trigger work dominates, so that workload shows no gain.
CREATE INDEX unit_expiring ON blood_unit (expires_at) WHERE status IN ('Collected', 'Available', 'Reserved');
-- every reservation of a unit, not only the active one: O-negative tracing Q12 33 -> 26 ms, and the lookback view
CREATE INDEX request_unit_unit ON request_unit (unit_id);
-- the screening check in check_donation_eligible() on every donation insert (2.7 -> 0.6 ms)
CREATE INDEX screening_donor_time ON screening (donor_id, completed_at);
-- FR-33 audit trail for one record; audit_log is append-only and the fastest-growing table (71 -> 0.03 ms)
CREATE INDEX audit_record ON audit_log (table_name, record_id);
