SET search_path = vytal;

-- Routine kinds. Every command the application or a job runs to change data is a PROCEDURE, run with CALL; any result it
-- hands back (a new id, a status) comes back in INOUT parameters as one row. FUNCTIONS are kept for what a procedure
-- cannot be: trigger functions, read-only calculations used inside queries (donor_eligibility, eligible_donors), and
-- small internal helpers used in expressions (require_role, require_session_user, require_actor, recompute_next_eligible,
-- refresh_alert). A few read-only SECURITY DEFINER functions hand the application the protected columns one screen needs
-- (login_lookup, my_profile, screening_answers, audit_entries; SE-07, at the end of this file).
-- separate_donation returns its new unit ids as an array, so it is a procedure like the other commands.
-- 06_security.sql makes every procedure SECURITY DEFINER (owner vytal_owner, search_path pinned): the application role
-- cannot write any table itself, so procedures are the only way it changes data (SE-04).

-- ---------------------------------------------------------------- FR-20 / UC-14 nightly expiry job
-- SE-02: runs for an active Admin session, or from the scheduler's own database login (vytal_scheduler, 06_security.sql),
-- which is how ops/nightly_expiry.sh calls it. The login comes from caller_role(), which a SECURITY DEFINER routine does
-- not change and the application role cannot fake. Anyone else, and a session with no acting user, is refused.
CREATE PROCEDURE run_nightly_expiry(INOUT expired int DEFAULT NULL) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vytal, pg_temp AS $$
DECLARE actor int;
BEGIN
    IF caller_role() = 'vytal_scheduler' THEN actor := NULL;          -- the job itself: no person to record
    ELSE actor := require_actor(ARRAY['Admin']);
    END IF;
    -- expired reservations are released first (Reserved -> Available), so every unit expires along Table 1
    UPDATE request_unit SET released_at = now() WHERE released_at IS NULL AND issued_at IS NULL
      AND unit_id IN (SELECT unit_id FROM blood_unit WHERE status = 'Reserved' AND expires_at <= now());
    UPDATE blood_unit SET status = 'Available' WHERE status = 'Reserved' AND expires_at <= now();
    UPDATE blood_unit SET status = 'Expired', status_reason = 'Wastage: past expiry'
    WHERE status IN ('Collected','Available') AND expires_at <= now();      -- UC-14
    GET DIAGNOSTICS expired = ROW_COUNT;
    INSERT INTO audit_log (user_id, action, table_name, record_id, new_value)
    VALUES (actor, 'JOB', 'blood_unit', 'run_nightly_expiry', expired::text);    -- the acting Admin, or NULL for the scheduler
    RAISE NOTICE 'expired % unit(s)', expired;
END $$;

-- ---------------------------------------------------------------- SE-02 the user id a routine is given must be the session's user
-- The backend stamps vytal.user_id per transaction (vytal/db.py); superusers (setup, sample data, maintenance) are exempt.
CREATE FUNCTION require_session_user(p_user int) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF p_user IS DISTINCT FROM nullif(current_setting('vytal.user_id', true), '')::int
       AND NOT caller_is_superuser() THEN
        RAISE EXCEPTION 'acting user does not match the session';
    END IF;
END $$;

-- ---------------------------------------------------------------- SE-02 the acting user must hold this role
CREATE FUNCTION require_role(p_user int, p_role text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_session_user(p_user);
    IF NOT EXISTS (SELECT 1 FROM user_account WHERE user_id = p_user AND role = p_role AND is_active) THEN
        RAISE EXCEPTION '% access only', p_role;
    END IF;
END $$;

-- ---------------------------------------------------------------- SE-02 procedures that take no user id act for the session's user
-- Returns the acting user (vytal.user_id), who must be active and hold one of the roles. A session with no acting user is
-- refused; only a superuser maintenance session (setup, sample data) passes without one.
CREATE FUNCTION require_actor(p_roles text[]) RETURNS int LANGUAGE plpgsql AS $$
DECLARE actor int := nullif(current_setting('vytal.user_id', true), '')::int;
BEGIN
    IF caller_is_superuser() THEN RETURN actor; END IF;
    IF actor IS NULL THEN RAISE EXCEPTION 'no acting user in this session'; END IF;
    IF NOT EXISTS (SELECT 1 FROM user_account WHERE user_id = actor AND is_active AND role = ANY (p_roles)) THEN
        RAISE EXCEPTION '% access only', array_to_string(p_roles, ' or ');
    END IF;
    RETURN actor;
END $$;

-- SE-02 a staff member works only on their own facility's records; Admins and superuser maintenance sessions pass
CREATE FUNCTION require_own_facility(p_actor int, p_facility int, p_what text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE fac int := (SELECT facility_id FROM staff WHERE user_id = p_actor);
BEGIN
    IF caller_is_superuser() OR fac IS NULL THEN RETURN; END IF;
    IF p_facility IS DISTINCT FROM fac THEN RAISE EXCEPTION '% belongs to another facility', p_what; END IF;
END $$;

-- ---------------------------------------------------------------- FR-01 donor self-registration (supertype + subtype in one transaction)
CREATE SEQUENCE donor_code_seq START 1001;           -- donor codes D-1001, D-1002, ... as in the Step 2 scenarios
CREATE PROCEDURE register_donor(p_email text, p_password_hash text, p_first text, p_last text, p_phone text,
                                p_dob date, p_sex char, p_weight numeric, p_address text, p_blood_group text,
                                INOUT new_user_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE min_age int := (SELECT min_age_years FROM eligibility_rule WHERE sex = p_sex);
BEGIN
    IF age(CURRENT_DATE, p_dob) < make_interval(years => min_age) THEN          -- UC-01 2b / BR-01
        RAISE EXCEPTION 'donor must be at least % years old to register', min_age; END IF;
    INSERT INTO user_account (email, password_hash, first_name, last_name, phone, role)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, p_phone, 'Donor') RETURNING user_id INTO new_user_id;
    -- the self-reported group stays unverified (verified_by NULL) until staff confirm it (FR-09)
    INSERT INTO donor (user_id, donor_code, date_of_birth, sex, weight_kg, address, blood_group)
    VALUES (new_user_id, 'D-' || nextval('donor_code_seq'), p_dob, p_sex, p_weight, p_address, p_blood_group);
END $$;

-- ---------------------------------------------------------------- FR-04 an Admin creates a staff account tied to one facility
CREATE PROCEDURE create_staff(p_admin int, p_email text, p_password_hash text, p_first text, p_last text,
                              p_facility int, p_job_title text, INOUT new_user_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF NOT EXISTS (SELECT 1 FROM facility WHERE facility_id = p_facility AND is_active) THEN
        RAISE EXCEPTION 'facility % does not exist or is inactive', p_facility; END IF;
    INSERT INTO user_account (email, password_hash, first_name, last_name, role, created_by)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, 'Staff', p_admin) RETURNING user_id INTO new_user_id;
    INSERT INTO staff (user_id, facility_id, job_title) VALUES (new_user_id, p_facility, p_job_title);
END $$;

-- ---------------------------------------------------------------- SE-05 lock for 15 minutes after 5 failed logins in a row
-- The password hash is checked by the application; this records the outcome atomically.
CREATE PROCEDURE login_attempt(p_user int, p_password_ok boolean, INOUT outcome text DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE a user_account%ROWTYPE;
BEGIN
    SELECT * INTO a FROM user_account WHERE user_id = p_user FOR UPDATE;
    IF NOT FOUND THEN outcome := 'failed'; RETURN; END IF;
    IF NOT a.is_active THEN outcome := CASE WHEN p_password_ok THEN 'inactive' ELSE 'failed' END; RETURN; END IF;
    IF a.locked_until > now() THEN outcome := 'locked'; RETURN; END IF;
    IF p_password_ok THEN
        UPDATE user_account SET failed_logins = 0, locked_until = NULL WHERE user_id = p_user;
        outcome := 'ok'; RETURN;
    END IF;
    IF a.failed_logins + 1 >= 5 THEN
        UPDATE user_account SET failed_logins = 0, locked_until = now() + interval '15 minutes' WHERE user_id = p_user;
        outcome := 'locked'; RETURN;
    END IF;
    UPDATE user_account SET failed_logins = a.failed_logins + 1 WHERE user_id = p_user;
    outcome := 'failed';
END $$;

-- ---------------------------------------------------------------- FR-06 / FR-08 screening; deferring answers create system deferrals
-- p_answers maps question_id -> answer, e.g. '{"1": true, "2": true}'. Every active question must be answered.
CREATE PROCEDURE submit_screening(p_donor int, p_answers jsonb, INOUT new_screening_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE sid int; missing int; actor int := require_actor('{Donor,Staff}');
BEGIN
    -- UC-04: the donor answers for themselves, or staff enter the answers at the clinic
    IF actor IS DISTINCT FROM p_donor AND NOT caller_is_superuser() AND NOT EXISTS (SELECT 1 FROM staff WHERE user_id = actor) THEN
        RAISE EXCEPTION 'screening is entered by the donor or by staff';
    END IF;
    IF (SELECT next_eligible_date FROM donor WHERE user_id = p_donor) = 'infinity' THEN     -- UC-04 precondition
        RAISE EXCEPTION 'donor is permanently deferred'; END IF;
    SELECT count(*) INTO missing FROM screening_question q WHERE q.is_active AND NOT p_answers ? q.question_id::text;
    IF missing > 0 THEN RAISE EXCEPTION '% screening question(s) unanswered', missing; END IF;
    INSERT INTO screening (donor_id) VALUES (p_donor) RETURNING screening_id INTO sid;
    INSERT INTO screening_answer (screening_id, question_id, answer)
    SELECT sid, key::int, value::boolean FROM jsonb_each_text(p_answers);
    INSERT INTO deferral (donor_id, reason_code, end_date, screening_id)
    SELECT p_donor, q.reason_code, CURRENT_DATE + r.default_days, sid
    FROM screening_answer a JOIN screening_question q USING (question_id) JOIN deferral_reason r USING (reason_code)
    WHERE a.screening_id = sid AND a.answer = q.defer_on_answer;
    new_screening_id := sid;
END $$;

-- ---------------------------------------------------------------- FR-11 / UC-07 record a donation; low hemoglobin defers instead (UC-07 2a)
-- Hands back the new donation_id, or NULL when the reading was below the donor's minimum and a LOWHB deferral was recorded.
-- Every other eligibility rule is still enforced by the check_donation_eligible trigger.
CREATE PROCEDURE record_donation(p_donor int, p_staff int, p_type text, p_volume int, p_hemoglobin numeric,
                                 INOUT new_donation_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_staff, 'Staff');          -- the session's own staff member records it at their facility
    new_donation_id := NULL;
    IF p_hemoglobin < (SELECT r.min_hemoglobin_gl FROM donor o JOIN eligibility_rule r USING (sex) WHERE o.user_id = p_donor) THEN
        INSERT INTO deferral (donor_id, reason_code, end_date, recorded_by)
        SELECT p_donor, 'LOWHB', CURRENT_DATE + default_days, p_staff FROM deferral_reason WHERE reason_code = 'LOWHB';
        RETURN;
    END IF;
    INSERT INTO donation (donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl)
    VALUES (p_donor, p_staff, p_type, now(), p_volume, p_hemoglobin) RETURNING donation_id INTO new_donation_id;
END $$;

-- ---------------------------------------------------------------- FR-12 split a donation into one unit per component
-- Hands back the new unit ids in the order the components were given.
CREATE PROCEDURE separate_donation(p_donation int, p_components text[], INOUT unit_ids varchar[] DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE pending int; actor int := require_actor('{Staff}');
BEGIN
    PERFORM require_own_facility(actor, donation_facility(p_donation), format('donation %s', p_donation));
    IF (SELECT status FROM donation WHERE donation_id = p_donation) = 'Discarded' THEN
        RAISE EXCEPTION 'donation % was discarded', p_donation; END IF;
    WITH made AS (
        INSERT INTO blood_unit (unit_id, donation_id, component_code)
        SELECT format('U%s-%s', p_donation, c), p_donation, c FROM unnest(p_components) AS c
        RETURNING unit_id, component_code)
    SELECT array_agg(unit_id ORDER BY array_position(p_components, component_code::text)) INTO unit_ids FROM made;
    CALL release_tested(p_donation, pending);    -- tests may already be in when the bag is split
END $$;

-- ---------------------------------------------------------------- FR-13 release a donation's units once every active test is negative
-- Hands back the number of active tests still missing (0 = released or nothing to release). Internal: only
-- separate_donation and record_tests call it, so the application role cannot (06_security.sql).
CREATE PROCEDURE release_tested(p_donation int, INOUT pending_tests int DEFAULT NULL) LANGUAGE plpgsql AS $$
BEGIN
    SELECT count(*) INTO pending_tests FROM test_type t
    WHERE t.is_active AND NOT EXISTS (SELECT 1 FROM test_result r WHERE r.donation_id = p_donation AND r.test_code = t.test_code);
    IF pending_tests = 0 AND (SELECT status FROM donation WHERE donation_id = p_donation) <> 'Discarded' THEN
        UPDATE blood_unit SET status = 'Available' WHERE donation_id = p_donation AND status = 'Collected';
    END IF;
END $$;

-- p_results maps test_code -> 'Negative' | 'Positive'. A positive result fires on_positive_test (discard + flag).
CREATE PROCEDURE record_tests(p_donation int, p_results jsonb, INOUT outcome text DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE pending int; actor int := require_actor('{Staff}');
BEGIN
    PERFORM require_own_facility(actor, donation_facility(p_donation), format('donation %s', p_donation));
    INSERT INTO test_result (donation_id, test_code, result)
    SELECT p_donation, key, value FROM jsonb_each_text(p_results);
    IF (SELECT status FROM donation WHERE donation_id = p_donation) = 'Discarded' THEN outcome := 'Discarded'; RETURN; END IF;
    CALL release_tested(p_donation, pending);
    outcome := CASE WHEN pending > 0 THEN format('Awaiting %s test(s)', pending) ELSE 'Released' END;
END $$;

-- ---------------------------------------------------------------- FR-09 staff verify a donor's group, as themselves
CREATE PROCEDURE verify_blood_group(p_staff int, p_donor int, p_group text) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    UPDATE donor SET blood_group = p_group, verified_by = p_staff, verified_at = now() WHERE user_id = p_donor;
    IF NOT FOUND THEN RAISE EXCEPTION 'donor % not found', p_donor; END IF;
END $$;

-- ---------------------------------------------------------------- FR-22 + FR-23 submit a request and reserve what stock covers
CREATE PROCEDURE submit_request(p_staff int, p_patient_ref text, p_group text, p_component text, p_units int, p_urgency text,
                                p_required_by timestamptz, INOUT new_request int DEFAULT NULL,
                                INOUT reserved_count int DEFAULT NULL, INOUT shortfall_count int DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE got int;
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    -- BR-10: Emergency is needed within 1 hour, Urgent within 24 hours, Routine is scheduled for any later time
    IF p_required_by <= now() THEN RAISE EXCEPTION 'required-by time must be in the future'; END IF;
    IF p_urgency = 'Emergency' AND p_required_by > now() + interval '1 hour' THEN
        RAISE EXCEPTION 'an Emergency request is needed within 1 hour (BR-10)'; END IF;
    IF p_urgency = 'Urgent' AND p_required_by > now() + interval '24 hours' THEN
        RAISE EXCEPTION 'an Urgent request is needed within 24 hours (BR-10)'; END IF;
    INSERT INTO blood_request (requested_by, patient_ref, recipient_group, component_code, units_requested, urgency, required_by)
    VALUES (p_staff, p_patient_ref, p_group, p_component, p_units, p_urgency, p_required_by) RETURNING request_id INTO new_request;
    CALL allocate(new_request, got);
    SELECT s.units_reserved, s.shortfall INTO reserved_count, shortfall_count FROM v_request_status s WHERE s.request_id = new_request;
END $$;

-- ---------------------------------------------------------------- UC-16 step 6 the lab confirms an Unknown group; reserve again
CREATE PROCEDURE confirm_request_group(p_staff int, p_request int, p_group text,
                                       INOUT reserved_count int DEFAULT NULL, INOUT shortfall_count int DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE got int;
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    IF (SELECT facility_id FROM v_request WHERE request_id = p_request)
       IS DISTINCT FROM (SELECT facility_id FROM staff WHERE user_id = p_staff) THEN
        RAISE EXCEPTION 'request % belongs to another facility', p_request; END IF;
    UPDATE blood_request SET recipient_group = p_group
    WHERE request_id = p_request AND recipient_group IS NULL AND status = 'Open';
    IF NOT FOUND THEN RAISE EXCEPTION 'request % is closed or already has a confirmed group', p_request; END IF;
    CALL allocate(p_request, got);
    SELECT s.units_reserved, s.shortfall INTO reserved_count, shortfall_count FROM v_request_status s WHERE s.request_id = p_request;
END $$;

-- ---------------------------------------------------------------- FR-26 donation call to eligible, compatible donors only
-- p_donor_codes NULL = call every match from eligible_donors(); otherwise only the listed codes that match.
-- SE-07: calls contain no health information, so staff cannot type the message; it is built from a fixed template that
-- names only the facility, its address, the component and an optional preferred date.
CREATE PROCEDURE send_donation_call(p_request int, p_staff int, p_donor_codes text[] DEFAULT NULL, p_preferred date DEFAULT NULL,
                                    INOUT new_call_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
DECLARE cid int; n int; msg text;
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    PERFORM require_own_facility(p_staff, (SELECT facility_id FROM v_request WHERE request_id = p_request),
                                 format('request %s', p_request));
    IF (SELECT status FROM blood_request WHERE request_id = p_request) IS DISTINCT FROM 'Open' THEN
        RAISE EXCEPTION 'request % is not open', p_request; END IF;
    msg := call_message(p_request, p_preferred);
    INSERT INTO donation_call (request_id, sent_by, message) VALUES (p_request, p_staff, msg) RETURNING call_id INTO cid;
    INSERT INTO call_recipient (call_id, donor_id)
    SELECT cid, o.user_id FROM eligible_donors(p_request) e JOIN donor o USING (donor_code)
    WHERE p_donor_codes IS NULL OR e.donor_code = ANY (p_donor_codes);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n = 0 THEN RAISE EXCEPTION 'no eligible, compatible donor to call for request %', p_request; END IF;
    INSERT INTO notification (user_id, message)
    SELECT donor_id, msg FROM call_recipient WHERE call_id = cid;
    new_call_id := cid;
END $$;

-- the facility that collected a donation (where its record was made)
CREATE FUNCTION donation_facility(p_donation int) RETURNS int LANGUAGE sql STABLE AS $$
    SELECT s.facility_id FROM vytal.donation d JOIN vytal.staff s ON s.user_id = d.recorded_by WHERE d.donation_id = p_donation
$$;

-- SE-07 the only text a donation call can carry
CREATE FUNCTION call_message(p_request int, p_preferred date DEFAULT NULL) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT format('%s is asking for donors to give %s. Please book a donation at %s%s. Accept or decline this call in Vytal.',
                  f.name, lower(ct.name), f.address,
                  CASE WHEN p_preferred IS NOT NULL THEN ' on or before ' || to_char(p_preferred, 'FMDD Mon YYYY') ELSE '' END)
    FROM vytal.blood_request r JOIN vytal.staff s ON s.user_id = r.requested_by JOIN vytal.facility f USING (facility_id)
    JOIN vytal.component_type ct ON ct.component_code = r.component_code
    WHERE r.request_id = p_request
$$;

-- ---------------------------------------------------------------- FR-27 / UC-20 a donor answers a call once
-- Hands back the facility's name, address and phone on accept, 'declined', or 'closed' when the request no longer needs donors (2a).
CREATE PROCEDURE respond_to_call(p_call int, p_donor int, p_accept boolean, INOUT reply text DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE st text; fname text; faddr text; fphone text;
BEGIN
    PERFORM require_session_user(p_donor);          -- UC-20: only the called donor answers
    SELECT br.status, fa.name, fa.address, fa.phone INTO st, fname, faddr, fphone
    FROM call_recipient cr JOIN donation_call c USING (call_id) JOIN blood_request br ON br.request_id = c.request_id
    JOIN staff s ON s.user_id = br.requested_by JOIN facility fa ON fa.facility_id = s.facility_id
    WHERE cr.call_id = p_call AND cr.donor_id = p_donor AND cr.response = 'Pending';
    IF NOT FOUND THEN RAISE EXCEPTION 'no pending call % for donor %', p_call, p_donor; END IF;
    IF st <> 'Open' THEN reply := 'closed'; RETURN; END IF;
    UPDATE call_recipient SET response = CASE WHEN p_accept THEN 'Accepted' ELSE 'Declined' END, responded_at = now()
    WHERE call_id = p_call AND donor_id = p_donor;
    reply := CASE WHEN p_accept THEN concat_ws(', ', fname, faddr, fphone) ELSE 'declined' END;
END $$;

-- ---------------------------------------------------------------- UC-06 / FR-08 staff record a deferral at the in-person check
-- A temporary reason without an end date gets the reason's default length; a permanent reason takes no end date (2a).
CREATE PROCEDURE record_deferral(p_staff int, p_donor int, p_reason text, p_end date DEFAULT NULL,
                                 INOUT new_deferral_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    INSERT INTO deferral (donor_id, reason_code, end_date, recorded_by)
    SELECT p_donor, reason_code, CASE WHEN is_permanent THEN p_end ELSE coalesce(p_end, CURRENT_DATE + default_days) END, p_staff
    FROM deferral_reason WHERE reason_code = p_reason RETURNING deferral_id INTO new_deferral_id;
    IF new_deferral_id IS NULL THEN RAISE EXCEPTION 'unknown deferral reason %', p_reason; END IF;
END $$;

-- ---------------------------------------------------------------- UC-15 / FR-16 discard a damaged or unsafe unit (Table 1)
-- Staff discard Available units at their own facility; an Admin discards a Quarantined unit once the lookback
-- confirms it (UC-22). A Reserved unit must be released from its request first (2a). Collected -> Discarded is UC-09's.
CREATE PROCEDURE discard_unit(p_user int, p_unit text, p_reason text) LANGUAGE plpgsql AS $$
DECLARE u blood_unit%ROWTYPE; r text; fac int;
BEGIN
    PERFORM require_session_user(p_user);
    SELECT a.role, s.facility_id INTO r, fac FROM user_account a LEFT JOIN staff s USING (user_id)
    WHERE a.user_id = p_user AND a.is_active;
    IF r IS NULL OR r = 'Donor' THEN RAISE EXCEPTION 'Staff or Admin access only'; END IF;
    SELECT * INTO u FROM blood_unit WHERE unit_id = p_unit FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unit % not found', p_unit; END IF;
    IF u.status = 'Reserved' THEN RAISE EXCEPTION 'release unit % from its request first', p_unit; END IF;
    IF r = 'Staff' AND u.facility_id <> fac THEN RAISE EXCEPTION 'unit % belongs to another facility', p_unit; END IF;
    IF r = 'Staff' AND u.status <> 'Available' THEN
        RAISE EXCEPTION 'staff can discard only Available units; unit % is %', p_unit, u.status; END IF;
    IF r = 'Admin' AND u.status <> 'Quarantined' THEN
        RAISE EXCEPTION 'an Admin discards Quarantined units only; unit % is %', p_unit, u.status; END IF;
    UPDATE blood_unit SET status = 'Discarded', status_reason = p_reason WHERE unit_id = p_unit;
END $$;

-- ---------------------------------------------------------------- UC-12 / FR-18 staff set their own facility's minimums; an Admin any
CREATE PROCEDURE set_threshold(p_user int, p_fac int, p_group text, p_comp text, p_min int) LANGUAGE plpgsql AS $$
DECLARE r text; fac int;
BEGIN
    PERFORM require_session_user(p_user);
    SELECT a.role, s.facility_id INTO r, fac FROM user_account a LEFT JOIN staff s USING (user_id)
    WHERE a.user_id = p_user AND a.is_active;
    IF r IS NULL OR r = 'Donor' THEN RAISE EXCEPTION 'Staff or Admin access only'; END IF;
    IF r = 'Staff' AND p_fac IS DISTINCT FROM fac THEN
        RAISE EXCEPTION 'staff can only set thresholds for their own facility'; END IF;
    INSERT INTO stock_threshold (facility_id, blood_group, component_code, min_units) VALUES (p_fac, p_group, p_comp, p_min)
    ON CONFLICT (facility_id, blood_group, component_code) DO UPDATE SET min_units = EXCLUDED.min_units;
END $$;

-- ---------------------------------------------------------------- UC-23 / FR-04, FR-05, FR-35 accounts and facilities
CREATE PROCEDURE create_admin(p_admin int, p_email text, p_password_hash text, p_first text, p_last text,
                              INOUT new_user_id int DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    INSERT INTO user_account (email, password_hash, first_name, last_name, role, created_by)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, 'Admin', p_admin) RETURNING user_id INTO new_user_id;
    INSERT INTO admin (user_id) VALUES (new_user_id);
END $$;

-- accounts are deactivated, never deleted (traceability); reactivating also clears a lockout
CREATE PROCEDURE set_account_active(p_admin int, p_user int, p_active boolean) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    UPDATE user_account SET is_active = p_active, failed_logins = 0, locked_until = NULL WHERE user_id = p_user;
    IF NOT FOUND THEN RAISE EXCEPTION 'user % not found', p_user; END IF;
END $$;

-- 2a: a facility with open requests cannot be deactivated until they are closed or moved
CREATE PROCEDURE set_facility_active(p_admin int, p_fac int, p_active boolean) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF NOT p_active AND EXISTS (SELECT 1 FROM v_request WHERE facility_id = p_fac AND status = 'Open') THEN
        RAISE EXCEPTION 'facility % has open requests: close or move them first', p_fac; END IF;
    UPDATE facility SET is_active = p_active WHERE facility_id = p_fac;
    IF NOT FOUND THEN RAISE EXCEPTION 'facility % not found', p_fac; END IF;
END $$;

-- p_id NULL creates a facility and comes back holding the new id; otherwise the facility p_id is edited.
CREATE PROCEDURE save_facility(p_admin int, INOUT p_id int, p_name text, p_type text, p_address text, p_phone text, p_email text)
LANGUAGE plpgsql AS $$
DECLARE found_id int;
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF p_id IS NULL THEN
        INSERT INTO facility (name, facility_type, address, phone, email) VALUES (p_name, p_type, p_address, p_phone, p_email)
        RETURNING facility_id INTO p_id;
    ELSE
        UPDATE facility SET name = p_name, facility_type = p_type, address = p_address, phone = p_phone, email = p_email
        WHERE facility_id = p_id RETURNING facility_id INTO found_id;
        IF found_id IS NULL THEN RAISE EXCEPTION 'facility % not found', p_id; END IF;
    END IF;
END $$;

-- ---------------------------------------------------------------- UC-03 / FR-03 a donor edits their contact details and weight
-- The blood group is not a parameter: once verified only staff change it (donor_group_guard).
CREATE PROCEDURE update_donor_profile(p_donor int, p_phone text, p_email text, p_address text, p_weight numeric)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_donor, 'Donor');
    UPDATE user_account SET phone = p_phone, email = lower(p_email) WHERE user_id = p_donor;
    UPDATE donor SET address = p_address, weight_kg = p_weight WHERE user_id = p_donor;
END $$;

-- ---------------------------------------------------------------- FR-32 / UC-24 an Admin maintains the clinical rules (MA-01)
-- New values apply to future calculations; every change is audited (SE-08) and compat_coverage refuses a stranding edit (2a).
CREATE PROCEDURE set_compatibility(p_admin int, p_component text, p_recipient text, p_donor text, p_allowed boolean)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF p_allowed THEN
        INSERT INTO compatibility (component_code, recipient_group, donor_group) VALUES (p_component, p_recipient, p_donor)
        ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM compatibility WHERE component_code = p_component AND recipient_group = p_recipient AND donor_group = p_donor;
    END IF;
END $$;

CREATE PROCEDURE set_shelf_life(p_admin int, p_component text, p_days int) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    UPDATE component_type SET shelf_life_days = p_days WHERE component_code = p_component;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown component %', p_component; END IF;
END $$;

CREATE PROCEDURE set_donation_interval(p_admin int, p_type text, p_sex char, p_days int) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    INSERT INTO donation_interval (donation_type_code, sex, interval_days) VALUES (p_type, p_sex, p_days)
    ON CONFLICT (donation_type_code, sex) DO UPDATE SET interval_days = EXCLUDED.interval_days;
END $$;

CREATE PROCEDURE save_deferral_reason(p_admin int, p_code text, p_description text, p_permanent boolean, p_default_days int)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    INSERT INTO deferral_reason (reason_code, description, is_permanent, default_days)
    VALUES (upper(p_code), p_description, p_permanent, p_default_days)
    ON CONFLICT (reason_code) DO UPDATE
        SET description = EXCLUDED.description, is_permanent = EXCLUDED.is_permanent, default_days = EXCLUDED.default_days;
END $$;

CREATE PROCEDURE set_eligibility_rule(p_admin int, p_sex char, p_min_age int, p_min_weight numeric, p_min_hemoglobin numeric)
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    UPDATE eligibility_rule SET min_age_years = p_min_age, min_weight_kg = p_min_weight, min_hemoglobin_gl = p_min_hemoglobin
    WHERE sex = p_sex;
    IF NOT FOUND THEN RAISE EXCEPTION 'no eligibility rule for sex %', p_sex; END IF;
END $$;

-- ---------------------------------------------------------------- UC-09 / UC-20 a user marks their own notification read, once
CREATE PROCEDURE mark_notification_read(p_user int, p_notification int) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_session_user(p_user);
    IF NOT EXISTS (SELECT 1 FROM notification WHERE notification_id = p_notification AND user_id = p_user) THEN
        RAISE EXCEPTION 'notification % not found', p_notification; END IF;
    UPDATE notification SET read_at = now() WHERE notification_id = p_notification AND read_at IS NULL;
END $$;

-- ---------------------------------------------------------------- UC-22 the lookback investigation is cleared (Table 1 Quarantined -> Available)
-- Refused while the donor has a permanent deferral or a positive test on record: then the confirmed path applies and an
-- Admin discards the units (discard_unit). Units already past their date go back through Available to Expired, the only
-- route Table 1 allows. Hands back how many units are back on the shelf; the flag is cleared so the donor can give again.
CREATE PROCEDURE clear_lookback(p_admin int, p_donor int, p_note text, INOUT released int DEFAULT NULL) LANGUAGE plpgsql AS $$
DECLARE why text := 'Lookback cleared: ' || coalesce(nullif(trim(p_note), ''), 'no risk found');
        expired int;
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF NOT EXISTS (SELECT 1 FROM donor WHERE user_id = p_donor) THEN RAISE EXCEPTION 'donor % not found', p_donor; END IF;
    IF EXISTS (SELECT 1 FROM deferral WHERE donor_id = p_donor AND end_date IS NULL) THEN
        RAISE EXCEPTION 'donor % has a permanent deferral: discard the quarantined units instead', p_donor; END IF;
    IF EXISTS (SELECT 1 FROM test_result t JOIN donation d USING (donation_id) WHERE d.donor_id = p_donor AND t.result = 'Positive') THEN
        RAISE EXCEPTION 'donor % has a positive test on record: discard the quarantined units instead', p_donor; END IF;
    UPDATE blood_unit SET status = 'Available', status_reason = why
    WHERE status = 'Quarantined' AND donation_id IN (SELECT donation_id FROM donation WHERE donor_id = p_donor);
    GET DIAGNOSTICS released = ROW_COUNT;
    UPDATE blood_unit SET status = 'Expired', status_reason = 'Wastage: expired during the lookback'
    WHERE status = 'Available' AND status_reason = why AND expires_at <= now()
      AND donation_id IN (SELECT donation_id FROM donation WHERE donor_id = p_donor);
    GET DIAGNOSTICS expired = ROW_COUNT;
    released := released - expired;
    UPDATE donor SET is_flagged = FALSE WHERE user_id = p_donor;
END $$;

-- ---------------------------------------------------------------- UC-01 2a a user changes their own password; an Admin resets one
-- Only bcrypt hashes are accepted (SE-01: never plain text). Either change ends the user's open sessions (SE-06); a reset
-- also clears a lockout (SE-05).
CREATE FUNCTION require_bcrypt(p_hash text) RETURNS void LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_hash IS NULL OR p_hash !~ '^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$' THEN
        RAISE EXCEPTION 'a password must be stored as a bcrypt hash'; END IF;
END $$;

CREATE PROCEDURE set_password(p_user int, p_hash text) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_session_user(p_user);
    PERFORM require_bcrypt(p_hash);
    UPDATE user_account SET password_hash = p_hash WHERE user_id = p_user AND is_active;
    IF NOT FOUND THEN RAISE EXCEPTION 'account % not found or inactive', p_user; END IF;
    UPDATE user_session SET ended_at = now() WHERE user_id = p_user AND ended_at IS NULL;
END $$;

CREATE PROCEDURE admin_reset_password(p_admin int, p_user int, p_hash text) LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    PERFORM require_bcrypt(p_hash);
    UPDATE user_account SET password_hash = p_hash, failed_logins = 0, locked_until = NULL WHERE user_id = p_user;
    IF NOT FOUND THEN RAISE EXCEPTION 'user % not found', p_user; END IF;
    UPDATE user_session SET ended_at = now() WHERE user_id = p_user AND ended_at IS NULL;
END $$;

-- ---------------------------------------------------------------- SE-07 minimum disclosure for the application role
-- vytal_app cannot SELECT password hashes, lockout counters, email, phone, address, screening answers or audit values
-- (06_security.sql). These read-only SECURITY DEFINER functions hand out exactly what one screen needs, and no more.

-- SE-01 / SE-05 login: the one place a password hash leaves the database, for the backend's bcrypt check. It answers for
-- one email at a time, so the hashes cannot be listed.
CREATE FUNCTION login_lookup(p_email text) RETURNS TABLE (user_id int, role varchar, password_hash varchar)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
    SELECT a.user_id, a.role, a.password_hash FROM user_account a WHERE a.email = lower(p_email)
$$;

-- UC-03 the acting user's own profile, contact details included; nobody else's
CREATE FUNCTION my_profile() RETURNS TABLE (user_id int, email varchar, phone varchar, first_name varchar, last_name varchar,
                                            role varchar, address varchar, date_of_birth date, weight_kg numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
    SELECT a.user_id, a.email, a.phone, a.first_name, a.last_name, a.role, o.address, o.date_of_birth, o.weight_kg
    FROM user_account a LEFT JOIN donor o ON o.user_id = a.user_id
    WHERE a.user_id = nullif(current_setting('vytal.user_id', true), '')::int
$$;

-- UC-04 screening answers: to the donor who gave them, or to active staff entering or reviewing them at the clinic
CREATE FUNCTION screening_answers(p_screening int) RETURNS TABLE (question_id int, question_text varchar, answer boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
#variable_conflict use_column
DECLARE actor int := nullif(current_setting('vytal.user_id', true), '')::int;
BEGIN
    IF NOT caller_is_superuser()
       AND NOT EXISTS (SELECT 1 FROM screening s WHERE s.screening_id = p_screening AND s.donor_id = actor)
       AND NOT EXISTS (SELECT 1 FROM staff st JOIN user_account a ON a.user_id = st.user_id WHERE st.user_id = actor AND a.is_active) THEN
        RAISE EXCEPTION 'screening answers are shown only to the donor and to clinic staff'; END IF;
    RETURN QUERY SELECT q.question_id, q.question_text, sa.answer
    FROM screening_answer sa JOIN screening_question q ON q.question_id = sa.question_id
    WHERE sa.screening_id = p_screening ORDER BY q.question_id;
END $$;

-- UC-25 the audit trail with its old and new values, for an Admin only (values can hold contact details)
CREATE FUNCTION audit_entries(p_user int DEFAULT NULL, p_table text DEFAULT NULL, p_record text DEFAULT NULL,
                              p_since timestamptz DEFAULT NULL, p_until timestamptz DEFAULT NULL, p_limit int DEFAULT 500)
RETURNS SETOF audit_log LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = vytal, pg_temp AS $$
BEGIN
    PERFORM require_actor('{Admin}');
    RETURN QUERY SELECT * FROM audit_log a
    WHERE (p_user IS NULL OR a.user_id = p_user) AND (p_table IS NULL OR a.table_name = p_table)
      AND (p_record IS NULL OR a.record_id = p_record) AND (p_since IS NULL OR a.changed_at >= p_since)
      AND (p_until IS NULL OR a.changed_at < p_until)
    ORDER BY a.changed_at, a.audit_id LIMIT p_limit;
END $$;
