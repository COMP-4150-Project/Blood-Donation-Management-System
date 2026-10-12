SET search_path = vytal;

-- ---------------------------------------------------------------- subclass row must match role (EER disjoint)
CREATE FUNCTION check_subclass_role() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r text;
BEGIN
    SELECT role INTO r FROM user_account WHERE user_id = NEW.user_id;
    IF r IS DISTINCT FROM TG_ARGV[0] THEN
        RAISE EXCEPTION 'user % has role %, cannot be added to %', NEW.user_id, r, TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donor_role BEFORE INSERT OR UPDATE ON donor FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Donor');
CREATE TRIGGER staff_role BEFORE INSERT OR UPDATE ON staff FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Staff');
CREATE TRIGGER admin_role BEFORE INSERT OR UPDATE ON admin FOR EACH ROW EXECUTE FUNCTION check_subclass_role('Admin');

-- ---------------------------------------------------------------- FR-07 / FR-10 next eligible date
-- NULL = no restriction yet, 'infinity' = permanently deferred.
-- The interval is taken from the type of the donor's most recent donation.
CREATE FUNCTION recompute_next_eligible(p_donor int) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d_next date; def_next date; perm boolean;
BEGIN
    SELECT (dn.donated_at AT TIME ZONE 'UTC')::date + di.interval_days INTO d_next
    FROM donation dn JOIN donor o ON o.user_id = dn.donor_id
    JOIN donation_interval di ON di.donation_type_code = dn.donation_type_code AND di.sex = o.sex
    WHERE dn.donor_id = p_donor ORDER BY dn.donated_at DESC LIMIT 1;

    SELECT bool_or(end_date IS NULL), max(end_date) INTO perm, def_next
    FROM deferral WHERE donor_id = p_donor AND (end_date IS NULL OR end_date > CURRENT_DATE);

    UPDATE donor SET next_eligible_date =
        CASE WHEN perm THEN 'infinity'::date
             ELSE NULLIF(greatest(coalesce(d_next, '-infinity'), coalesce(def_next, '-infinity')), '-infinity') END
    WHERE user_id = p_donor;
END $$;
CREATE FUNCTION trg_recompute_eligible() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM recompute_next_eligible(NEW.donor_id); RETURN NULL; END $$;
CREATE TRIGGER donation_eligible AFTER INSERT ON donation FOR EACH ROW EXECUTE FUNCTION trg_recompute_eligible();
CREATE TRIGGER deferral_eligible AFTER INSERT OR UPDATE ON deferral FOR EACH ROW EXECUTE FUNCTION trg_recompute_eligible();

-- ---------------------------------------------------------------- FR-11 / DI-03 no donation for an ineligible donor
-- Age, weight and hemoglobin thresholds are the Canadian Blood Services values named in FR-07 / UC-07.
CREATE FUNCTION check_donation_eligible() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o donor%ROWTYPE; active boolean; er eligibility_rule%ROWTYPE;
BEGIN
    SELECT * INTO o FROM donor WHERE user_id = NEW.donor_id;
    SELECT is_active INTO active FROM user_account WHERE user_id = NEW.donor_id;
    IF NOT active THEN RAISE EXCEPTION 'donor account is deactivated'; END IF;
    IF o.is_flagged THEN RAISE EXCEPTION 'donor is flagged after a failed test'; END IF;
    SELECT * INTO er FROM eligibility_rule WHERE sex = o.sex;
    IF age((NEW.donated_at AT TIME ZONE 'UTC')::date, o.date_of_birth) < make_interval(years => er.min_age_years) THEN
        RAISE EXCEPTION 'donor is under %', er.min_age_years; END IF;
    IF o.weight_kg < er.min_weight_kg THEN RAISE EXCEPTION 'donor weighs under % kg', trim_scale(er.min_weight_kg); END IF;
    IF NEW.hemoglobin_gl < er.min_hemoglobin_gl THEN
        RAISE EXCEPTION 'hemoglobin % g/L is below the minimum for this donor', NEW.hemoglobin_gl; END IF;
    IF o.next_eligible_date IS NOT NULL AND o.next_eligible_date > (NEW.donated_at AT TIME ZONE 'UTC')::date THEN
        RAISE EXCEPTION 'donor not eligible until %', o.next_eligible_date; END IF;
    -- the group measured for this donation is kept on the donation, so later corrections to DONOR do not rewrite history
    IF NEW.blood_group IS NULL THEN
        IF o.verified_by IS NULL THEN RAISE EXCEPTION 'verify the donor''s blood group before recording the donation'; END IF;
        NEW.blood_group := o.blood_group;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM screening s WHERE s.donor_id = NEW.donor_id
                   AND s.completed_at BETWEEN NEW.donated_at - interval '24 hours' AND NEW.donated_at) THEN
        RAISE EXCEPTION 'donor has no screening in the 24 hours before this donation'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER donation_check BEFORE INSERT ON donation FOR EACH ROW EXECUTE FUNCTION check_donation_eligible();

-- ---------------------------------------------------------------- FR-12 expiry from shelf life, DI-02 expiry after collection
CREATE FUNCTION set_unit_expiry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE collected timestamptz; shelf int; dfac int;
BEGIN
    SELECT dn.donated_at, st.facility_id INTO collected, dfac
    FROM donation dn JOIN staff st ON st.user_id = dn.recorded_by WHERE dn.donation_id = NEW.donation_id;
    NEW.facility_id := coalesce(NEW.facility_id, dfac);   -- a new bag starts where it was collected
    SELECT shelf_life_days INTO shelf FROM component_type WHERE component_code = NEW.component_code;
    IF NEW.expires_at IS NULL THEN NEW.expires_at := collected + make_interval(days => shelf); END IF;
    IF NEW.expires_at <= collected THEN RAISE EXCEPTION 'expiry must be after collection'; END IF;
    RETURN NEW;
END $$;
-- NOT NULL on donation.blood_group and blood_unit.expires_at holds as designed: PostgreSQL checks it after BEFORE triggers run
CREATE TRIGGER unit_expiry BEFORE INSERT ON blood_unit FOR EACH ROW EXECUTE FUNCTION set_unit_expiry();

-- ---------------------------------------------------------------- FR-13 / FR-14 valid status changes
CREATE FUNCTION check_unit_status() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ok boolean; bad_or_missing int;
BEGIN
    IF NEW.status = OLD.status THEN RETURN NEW; END IF;
    ok := (OLD.status, NEW.status) IN (
        ('Collected','Available'), ('Collected','Discarded'), ('Collected','Quarantined'), ('Collected','Expired'),
        ('Available','Reserved'), ('Available','Discarded'), ('Available','Expired'), ('Available','Quarantined'),
        ('Reserved','Available'), ('Reserved','Issued'), ('Reserved','Expired'), ('Reserved','Quarantined'),
        ('Quarantined','Discarded'), ('Quarantined','Available'));
    -- Beyond the main lifecycle in Step 2 (FR-14): Collected -> Quarantined lets a lookback pull a bag still awaiting tests,
    -- and Reserved -> Expired lets a unit that expires while reserved be written off instead of issued.
    IF NOT ok THEN RAISE EXCEPTION 'invalid unit status change % -> %', OLD.status, NEW.status; END IF;
    NEW.status_changed_at := now();
    IF OLD.status = 'Collected' AND NEW.status = 'Available' THEN
        SELECT count(*) INTO bad_or_missing FROM test_type tt
        LEFT JOIN test_result tr ON tr.test_code = tt.test_code AND tr.donation_id = NEW.donation_id
        WHERE tt.is_active AND (tr.result IS NULL OR tr.result <> 'Negative');
        IF bad_or_missing > 0 THEN RAISE EXCEPTION 'unit % cannot be released: tests missing or positive', NEW.unit_id; END IF;
        IF (SELECT o.is_flagged FROM donation d JOIN donor o ON o.user_id = d.donor_id WHERE d.donation_id = NEW.donation_id) THEN
            RAISE EXCEPTION 'unit % cannot be released: donor is flagged', NEW.unit_id; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER unit_status BEFORE UPDATE OF status ON blood_unit FOR EACH ROW EXECUTE FUNCTION check_unit_status();

-- FR-13: a positive test discards every unit of that donation and flags the donor
CREATE FUNCTION on_positive_test() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.result = 'Positive' THEN
        UPDATE blood_unit SET status = 'Discarded', status_reason = 'Positive ' || NEW.test_code
        WHERE donation_id = NEW.donation_id AND status IN ('Collected','Available','Quarantined');
        UPDATE donor SET is_flagged = TRUE WHERE user_id = (SELECT donor_id FROM donation WHERE donation_id = NEW.donation_id);
        UPDATE donation SET status = 'Discarded' WHERE donation_id = NEW.donation_id;
        INSERT INTO notification (user_id, message)
        SELECT a.user_id, format('Donation %s failed test %s. Units discarded and donor flagged; run a lookback.',
                                 NEW.donation_id, NEW.test_code)
        FROM admin a JOIN user_account ua USING (user_id) WHERE ua.is_active;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER test_positive AFTER INSERT ON test_result FOR EACH ROW EXECUTE FUNCTION on_positive_test();

-- ---------------------------------------------------------------- UC-07/UC-08 donation status follows its units
CREATE FUNCTION mark_separated() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE donation SET status = 'Separated' WHERE donation_id = NEW.donation_id AND status = 'Awaiting separation';
    RETURN NULL;
END $$;
CREATE TRIGGER unit_separated AFTER INSERT ON blood_unit FOR EACH ROW EXECUTE FUNCTION mark_separated();

-- FR-16 / FR-20 every expired or discarded unit gets one wastage record
CREATE FUNCTION record_wastage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('Expired','Discarded') AND OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO wastage_record (unit_id, cause) VALUES (NEW.unit_id, NEW.status);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER unit_wastage AFTER UPDATE OF status ON blood_unit FOR EACH ROW EXECUTE FUNCTION record_wastage();

-- a deferral must agree with its reason: permanent reason = no end date, temporary reason = end date
CREATE FUNCTION check_deferral_reason() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE perm boolean;
BEGIN
    SELECT is_permanent INTO perm FROM deferral_reason WHERE reason_code = NEW.reason_code;
    IF perm AND NEW.end_date IS NOT NULL THEN RAISE EXCEPTION 'permanent deferral reason cannot have an end date'; END IF;
    IF NOT perm AND NEW.end_date IS NULL THEN RAISE EXCEPTION 'temporary deferral reason needs an end date'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER deferral_reason_match BEFORE INSERT OR UPDATE ON deferral FOR EACH ROW EXECUTE FUNCTION check_deferral_reason();

-- ---------------------------------------------------------------- views (derived values removed during normalization)
CREATE VIEW v_blood_unit AS
SELECT u.*, dn.blood_group, dn.donated_at AS collected_at, dn.donor_id
FROM blood_unit u JOIN donation dn USING (donation_id);

CREATE VIEW v_wastage AS                 -- FR-20 / FR-34 / Scenario 4 monthly wastage
SELECT u.facility_id, u.component_code, w.cause, date_trunc('month', w.wasted_at) AS month, count(*) AS units_wasted
FROM wastage_record w JOIN blood_unit u USING (unit_id) GROUP BY 1, 2, 3, 4;

CREATE VIEW v_request AS                 -- a request's facility is its requester's facility (FR-04)
SELECT r.*, s.facility_id FROM blood_request r JOIN staff s ON s.user_id = r.requested_by;

CREATE VIEW v_request_status AS          -- FR-24 reserved count and shortfall
SELECT r.request_id, r.units_requested,
       count(ru.unit_id) FILTER (WHERE ru.released_at IS NULL) AS units_reserved,
       greatest(r.units_requested - count(ru.unit_id) FILTER (WHERE ru.released_at IS NULL), 0) AS shortfall
FROM blood_request r LEFT JOIN request_unit ru USING (request_id) GROUP BY r.request_id;

CREATE VIEW v_inventory AS               -- FR-17
SELECT facility_id, blood_group, component_code,
       count(*) FILTER (WHERE status = 'Available') AS available,
       count(*) FILTER (WHERE status = 'Reserved') AS reserved,
       count(*) FILTER (WHERE status = 'Available' AND expires_at < now() + interval '48 hours') AS expiring_48h
FROM v_blood_unit GROUP BY 1, 2, 3;

-- ---------------------------------------------------------------- FR-19 low-stock alerts (one open alert per threshold)
CREATE FUNCTION refresh_alert(p_fac int, p_group text, p_comp text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE minu int; avail int;
BEGIN
    SELECT min_units INTO minu FROM stock_threshold
    WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp;
    IF minu IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO avail FROM v_blood_unit
    WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp AND status = 'Available';
    IF avail < minu THEN
        INSERT INTO stock_alert (facility_id, blood_group, component_code) VALUES (p_fac, p_group, p_comp)
        ON CONFLICT (facility_id, blood_group, component_code) WHERE cleared_at IS NULL DO NOTHING;
    ELSE
        UPDATE stock_alert SET cleared_at = now()
        WHERE facility_id = p_fac AND blood_group = p_group AND component_code = p_comp AND cleared_at IS NULL;
    END IF;
END $$;
CREATE FUNCTION trg_unit_alert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE g text;
BEGIN
    SELECT blood_group INTO g FROM v_blood_unit WHERE unit_id = NEW.unit_id;
    PERFORM refresh_alert(NEW.facility_id, g, NEW.component_code);
    RETURN NULL;
END $$;
CREATE TRIGGER unit_alert AFTER INSERT OR UPDATE OF status ON blood_unit FOR EACH ROW EXECUTE FUNCTION trg_unit_alert();

-- FR-18 / FR-19: changing a minimum re-checks stock straight away, not at the next unit movement
CREATE FUNCTION trg_threshold_alert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM refresh_alert(NEW.facility_id, NEW.blood_group, NEW.component_code); RETURN NULL; END $$;
CREATE TRIGGER threshold_alert AFTER INSERT OR UPDATE OF min_units ON stock_threshold
    FOR EACH ROW EXECUTE FUNCTION trg_threshold_alert();

-- ---------------------------------------------------------------- FR-23 FEFO allocation with O-negative conservation, DI-01
CREATE FUNCTION allocate(p_request int) RETURNS int LANGUAGE plpgsql AS $$
DECLARE r blood_request%ROWTYPE; need int; got int := 0; u record; fac int;
BEGIN
    SELECT * INTO r FROM blood_request WHERE request_id = p_request FOR UPDATE;
    IF r.status IS DISTINCT FROM 'Open' THEN RAISE EXCEPTION 'request % is not open', p_request; END IF;
    SELECT facility_id INTO fac FROM staff WHERE user_id = r.requested_by;
    SELECT shortfall INTO need FROM v_request_status WHERE request_id = p_request;
    FOR u IN
        SELECT b.unit_id, dn.blood_group FROM blood_unit b JOIN donation dn USING (donation_id)
        WHERE b.facility_id = fac AND b.component_code = r.component_code
          AND b.status = 'Available' AND b.expires_at > now()
          AND (   (r.recipient_group IS NOT NULL AND EXISTS (SELECT 1 FROM compatibility c
                     WHERE c.component_code = r.component_code AND c.recipient_group = r.recipient_group
                       AND c.donor_group = dn.blood_group))
               -- unknown group: only donor groups compatible with every recipient group (O- red cells, AB plasma)
               OR (r.recipient_group IS NULL AND (SELECT count(*) FROM compatibility c
                     WHERE c.component_code = r.component_code AND c.donor_group = dn.blood_group) = 8))
          -- O- red cells go to other groups in an Emergency, or only for what other compatible stock cannot cover
          AND (dn.blood_group <> 'O-' OR r.recipient_group = 'O-' OR r.recipient_group IS NULL
               OR r.urgency = 'Emergency' OR r.component_code <> 'RBC'
               OR (SELECT count(*) FROM v_blood_unit x JOIN compatibility c
                     ON c.component_code = r.component_code AND c.recipient_group = r.recipient_group AND c.donor_group = x.blood_group
                     WHERE x.facility_id = fac AND x.component_code = r.component_code
                       AND x.status = 'Available' AND x.expires_at > now() AND x.blood_group <> 'O-') < need)
        ORDER BY (dn.blood_group = r.recipient_group) DESC, (dn.blood_group = 'O-') ASC, b.expires_at ASC
        LIMIT need
        FOR UPDATE OF b SKIP LOCKED
    LOOP
        INSERT INTO request_unit (request_id, unit_id) VALUES (p_request, u.unit_id);
        UPDATE blood_unit SET status = 'Reserved' WHERE unit_id = u.unit_id;
        got := got + 1;
    END LOOP;
    RETURN got;
END $$;

-- FR-28 issue reserved units and close; unused units go back to Available
CREATE FUNCTION close_request(p_request int, p_cancel boolean DEFAULT FALSE) RETURNS text LANGUAGE plpgsql AS $$
DECLARE issued int; req int; st text;
BEGIN
    SELECT units_requested, status INTO req, st FROM blood_request WHERE request_id = p_request FOR UPDATE;
    IF st IS DISTINCT FROM 'Open' THEN RAISE EXCEPTION 'request % is not open', p_request; END IF;
    IF NOT p_cancel THEN
        -- a reservation that expired before issue is released and its unit expires (counted as wastage)
        UPDATE blood_unit SET status = 'Expired', status_reason = 'Wastage: expired while reserved'
        WHERE status = 'Reserved' AND expires_at <= now()
          AND unit_id IN (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL);
        UPDATE request_unit SET released_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL
          AND unit_id IN (SELECT unit_id FROM blood_unit WHERE status = 'Expired');
        UPDATE blood_unit SET status = 'Issued' WHERE unit_id IN
            (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL);
        UPDATE request_unit SET issued_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL;
    ELSE
        UPDATE blood_unit SET status = 'Available' WHERE unit_id IN
            (SELECT unit_id FROM request_unit WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL);
        UPDATE request_unit SET released_at = now() WHERE request_id = p_request AND released_at IS NULL AND issued_at IS NULL;
    END IF;
    SELECT count(*) FILTER (WHERE issued_at IS NOT NULL) INTO issued FROM request_unit WHERE request_id = p_request;
    st := CASE WHEN p_cancel THEN 'Cancelled' WHEN issued >= req THEN 'Fulfilled' ELSE 'Partially Fulfilled' END;
    UPDATE blood_request SET status = st, closed_at = now() WHERE request_id = p_request;
    RETURN st;
END $$;

-- FR-20 nightly expiry
CREATE FUNCTION expire_units() RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
DECLARE n int;
BEGIN
    UPDATE request_unit SET released_at = now() WHERE released_at IS NULL AND issued_at IS NULL
      AND unit_id IN (SELECT unit_id FROM blood_unit WHERE status = 'Reserved' AND expires_at <= now());
    UPDATE blood_unit SET status = 'Expired', status_reason = 'Wastage: past expiry'
    WHERE status IN ('Collected','Available','Reserved') AND expires_at <= now();      -- UC-14
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO audit_log (action, table_name, record_id, new_value) VALUES ('JOB', 'blood_unit', 'expire_units', n::text);
    RETURN n;
END $$;

-- FR-31 quarantine every Available / Reserved unit from a donor
CREATE FUNCTION quarantine_donor(p_donor int) RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
    UPDATE request_unit SET released_at = now() WHERE released_at IS NULL AND issued_at IS NULL
      AND unit_id IN (SELECT unit_id FROM v_blood_unit WHERE donor_id = p_donor AND status = 'Reserved');
    UPDATE blood_unit SET status = 'Quarantined', status_reason = 'Lookback'
    WHERE status IN ('Available','Reserved') AND donation_id IN (SELECT donation_id FROM donation WHERE donor_id = p_donor);
    GET DIAGNOSTICS n = ROW_COUNT;
    UPDATE donor SET is_flagged = TRUE WHERE user_id = p_donor;
    RETURN n;
END $$;

-- FR-07 / US-02 eligibility status shown to donors and staff
CREATE VIEW v_donor_eligibility AS
SELECT o.user_id, o.donor_code, o.blood_group, o.next_eligible_date, o.verified_by IS NOT NULL AS group_verified,
       CASE WHEN NOT a.is_active THEN 'Account inactive'
            WHEN o.is_flagged THEN 'Not eligible (flagged after a failed test)'
            WHEN o.next_eligible_date = 'infinity' THEN 'Permanently deferred'
            WHEN o.weight_kg < r.min_weight_kg THEN format('Not eligible (under %s kg)', trim_scale(r.min_weight_kg))
            WHEN greatest(o.next_eligible_date, (o.date_of_birth + make_interval(years => r.min_age_years))::date) > CURRENT_DATE
                THEN 'Not eligible until ' ||
                     greatest(o.next_eligible_date, (o.date_of_birth + make_interval(years => r.min_age_years))::date)
            ELSE 'Eligible' END AS status
FROM donor o JOIN user_account a USING (user_id) JOIN eligibility_rule r USING (sex);

-- FR-25 eligible, compatible, group-verified donors for a request (code, group, eligibility only; no contact details)
CREATE FUNCTION eligible_donors(p_request int)
RETURNS TABLE (donor_code varchar, blood_group varchar, next_eligible_date date) LANGUAGE sql STABLE AS $$
    SELECT e.donor_code, e.blood_group, e.next_eligible_date
    FROM blood_request r JOIN compatibility c ON c.component_code = r.component_code
         AND (c.recipient_group = r.recipient_group OR (r.recipient_group IS NULL AND c.recipient_group = 'AB+' AND
              (SELECT count(*) FROM compatibility c2 WHERE c2.component_code = r.component_code AND c2.donor_group = c.donor_group) = 8))
    JOIN v_donor_eligibility e ON e.blood_group = c.donor_group
    WHERE r.request_id = p_request AND e.status = 'Eligible' AND e.group_verified
$$;

-- FR-30 lookback
CREATE VIEW v_lookback AS
SELECT dn.donor_id, dn.donation_id, dn.donated_at, u.unit_id, u.component_code, u.status,
       ru.request_id, vr.facility_id AS request_facility, ru.issued_at
FROM donation dn JOIN blood_unit u USING (donation_id)
LEFT JOIN request_unit ru ON ru.unit_id = u.unit_id AND ru.released_at IS NULL LEFT JOIN v_request vr ON vr.request_id = ru.request_id;

-- ---------------------------------------------------------------- FR-33 / SE-08 audit log (one row per changed field)
CREATE FUNCTION audit_row() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
DECLARE o jsonb := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
        n jsonb := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
        k text; uid int := nullif(current_setting('vytal.user_id', true), '')::int;
        rid text := (SELECT string_agg(coalesce(n, o) ->> a, '/') FROM unnest(TG_ARGV) a);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        FOR k IN SELECT jsonb_object_keys(n) LOOP
            IF n -> k IS DISTINCT FROM o -> k THEN
                INSERT INTO audit_log (user_id, action, table_name, record_id, field_name, old_value, new_value)
                VALUES (uid, TG_OP, TG_TABLE_NAME, rid, k, o ->> k, n ->> k);
            END IF;
        END LOOP;
    ELSE
        INSERT INTO audit_log (user_id, action, table_name, record_id) VALUES (uid, TG_OP, TG_TABLE_NAME, rid);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER audit_donor AFTER INSERT OR UPDATE OR DELETE ON donor FOR EACH ROW EXECUTE FUNCTION audit_row('user_id');
CREATE TRIGGER audit_deferral AFTER INSERT OR UPDATE OR DELETE ON deferral FOR EACH ROW EXECUTE FUNCTION audit_row('deferral_id');
CREATE TRIGGER audit_unit AFTER INSERT OR UPDATE OR DELETE ON blood_unit FOR EACH ROW EXECUTE FUNCTION audit_row('unit_id');
CREATE TRIGGER audit_request AFTER INSERT OR UPDATE OR DELETE ON blood_request FOR EACH ROW EXECUTE FUNCTION audit_row('request_id');
CREATE TRIGGER audit_compat AFTER INSERT OR UPDATE OR DELETE ON compatibility FOR EACH ROW EXECUTE FUNCTION audit_row('component_code', 'recipient_group', 'donor_group');
-- SE-08: every clinical rule table an Admin can change (FR-32) is audited too
CREATE TRIGGER audit_component AFTER INSERT OR UPDATE OR DELETE ON component_type FOR EACH ROW EXECUTE FUNCTION audit_row('component_code');
CREATE TRIGGER audit_interval AFTER INSERT OR UPDATE OR DELETE ON donation_interval FOR EACH ROW EXECUTE FUNCTION audit_row('donation_type_code', 'sex');
CREATE TRIGGER audit_rule AFTER INSERT OR UPDATE OR DELETE ON eligibility_rule FOR EACH ROW EXECUTE FUNCTION audit_row('sex');
CREATE TRIGGER audit_reason AFTER INSERT OR UPDATE OR DELETE ON deferral_reason FOR EACH ROW EXECUTE FUNCTION audit_row('reason_code');
