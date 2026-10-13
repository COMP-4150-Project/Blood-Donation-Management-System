SET search_path = vytal;
-- PostgreSQL does not index foreign keys on its own. Each index below was kept because it measurably sped up
-- a query or procedure on the full-size bench dataset (bench/results.md); candidates with no gain were left out.

-- Q03 / Q04 / Q05 / Q10 and recompute_next_eligible() on every donation: Q03 822 -> 39 ms, Q05 456 -> 3.6 ms
CREATE INDEX donation_donor_time ON donation (donor_id, donated_at DESC);
-- allocate() and the low-stock count in refresh_alert(): only Available units are ever searched (PR-02 92 -> 50 ms)
CREATE INDEX unit_available ON blood_unit (facility_id, component_code, expires_at) WHERE status = 'Available';
-- expire_units() nightly predicate
CREATE INDEX unit_expiring ON blood_unit (expires_at) WHERE status IN ('Available', 'Reserved');
-- recompute_next_eligible() on every donation and deferral
CREATE INDEX deferral_donor ON deferral (donor_id);
-- lookback (Q05, v_lookback) and O-negative tracing (Q12) join every reservation of a unit, not only the active one
CREATE INDEX request_unit_unit ON request_unit (unit_id);
-- the screening check in check_donation_eligible() on every donation
CREATE INDEX screening_donor_time ON screening (donor_id, completed_at);
-- FR-33 audit trail for one record; audit_log is append-only and the fastest-growing table
CREATE INDEX audit_record ON audit_log (table_name, record_id);
