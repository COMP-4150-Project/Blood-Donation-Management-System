SET search_path = vytal;

-- Jobs are procedures (CALL); operations that hand a value back to the caller are functions (SELECT).

-- ---------------------------------------------------------------- FR-20 / UC-14 nightly expiry job
CREATE PROCEDURE run_nightly_expiry(INOUT expired int DEFAULT NULL) LANGUAGE plpgsql SET search_path = vytal, pg_temp AS $$
BEGIN
    expired := expire_units();
    RAISE NOTICE 'expired % unit(s)', expired;
END $$;

-- ---------------------------------------------------------------- SE-02 the user id a routine is given must be the session's user
-- The backend stamps vytal.user_id per transaction (vytal/db.py); superusers (setup, sample data, maintenance) are exempt.
CREATE FUNCTION require_session_user(p_user int) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF p_user IS DISTINCT FROM nullif(current_setting('vytal.user_id', true), '')::int
       AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
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

-- ---------------------------------------------------------------- FR-01 donor self-registration (supertype + subtype in one transaction)
CREATE SEQUENCE donor_code_seq START 1001;           -- donor codes D-1001, D-1002, ... as in the Step 2 scenarios
CREATE FUNCTION register_donor(p_email text, p_password_hash text, p_first text, p_last text, p_phone text,
                               p_dob date, p_sex char, p_weight numeric, p_address text, p_blood_group text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE uid int;
BEGIN
    INSERT INTO user_account (email, password_hash, first_name, last_name, phone, role)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, p_phone, 'Donor') RETURNING user_id INTO uid;
    -- the self-reported group stays unverified (verified_by NULL) until staff confirm it (FR-09)
    INSERT INTO donor (user_id, donor_code, date_of_birth, sex, weight_kg, address, blood_group)
    VALUES (uid, 'D-' || nextval('donor_code_seq'), p_dob, p_sex, p_weight, p_address, p_blood_group);
    RETURN uid;
END $$;

-- ---------------------------------------------------------------- FR-04 an Admin creates a staff account tied to one facility
CREATE FUNCTION create_staff(p_admin int, p_email text, p_password_hash text, p_first text, p_last text,
                             p_facility int, p_job_title text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE uid int;
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF NOT EXISTS (SELECT 1 FROM facility WHERE facility_id = p_facility AND is_active) THEN
        RAISE EXCEPTION 'facility % does not exist or is inactive', p_facility; END IF;
    INSERT INTO user_account (email, password_hash, first_name, last_name, role, created_by)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, 'Staff', p_admin) RETURNING user_id INTO uid;
    INSERT INTO staff (user_id, facility_id, job_title) VALUES (uid, p_facility, p_job_title);
    RETURN uid;
END $$;

-- ---------------------------------------------------------------- SE-05 lock for 15 minutes after 5 failed logins in a row
-- The password hash is checked by the application; this records the outcome atomically.
CREATE FUNCTION login_attempt(p_user int, p_password_ok boolean) RETURNS text LANGUAGE plpgsql AS $$
DECLARE a user_account%ROWTYPE;
BEGIN
    SELECT * INTO a FROM user_account WHERE user_id = p_user FOR UPDATE;
    IF NOT FOUND THEN RETURN 'failed'; END IF;
    IF NOT a.is_active THEN RETURN CASE WHEN p_password_ok THEN 'inactive' ELSE 'failed' END; END IF;
    IF a.locked_until > now() THEN RETURN 'locked'; END IF;
    IF p_password_ok THEN
        UPDATE user_account SET failed_logins = 0, locked_until = NULL WHERE user_id = p_user;
        RETURN 'ok';
    END IF;
    IF a.failed_logins + 1 >= 5 THEN
        UPDATE user_account SET failed_logins = 0, locked_until = now() + interval '15 minutes' WHERE user_id = p_user;
        RETURN 'locked';
    END IF;
    UPDATE user_account SET failed_logins = a.failed_logins + 1 WHERE user_id = p_user;
    RETURN 'failed';
END $$;

-- ---------------------------------------------------------------- FR-06 / FR-08 screening; deferring answers create system deferrals
-- p_answers maps question_id -> answer, e.g. '{"1": true, "2": true}'. Every active question must be answered.
CREATE FUNCTION submit_screening(p_donor int, p_answers jsonb) RETURNS int LANGUAGE plpgsql AS $$
DECLARE sid int; missing int; actor int := nullif(current_setting('vytal.user_id', true), '')::int;
BEGIN
    -- UC-04: the donor answers for themselves, or staff enter the answers at the clinic
    IF actor IS DISTINCT FROM p_donor AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
       AND NOT EXISTS (SELECT 1 FROM staff s JOIN user_account a USING (user_id) WHERE s.user_id = actor AND a.is_active) THEN
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
    RETURN sid;
END $$;

-- ---------------------------------------------------------------- FR-11 / UC-07 record a donation; low hemoglobin defers instead (UC-07 2a)
-- Returns the new donation_id, or NULL when the reading was below the donor's minimum and a LOWHB deferral was recorded.
-- Every other eligibility rule is still enforced by the check_donation_eligible trigger.
CREATE FUNCTION record_donation(p_donor int, p_staff int, p_type text, p_volume int, p_hemoglobin numeric)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE dn int;
BEGIN
    IF p_hemoglobin < (SELECT r.min_hemoglobin_gl FROM donor o JOIN eligibility_rule r USING (sex) WHERE o.user_id = p_donor) THEN
        INSERT INTO deferral (donor_id, reason_code, end_date, recorded_by)
        SELECT p_donor, 'LOWHB', CURRENT_DATE + default_days, p_staff FROM deferral_reason WHERE reason_code = 'LOWHB';
        RETURN NULL;
    END IF;
    INSERT INTO donation (donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl)
    VALUES (p_donor, p_staff, p_type, now(), p_volume, p_hemoglobin) RETURNING donation_id INTO dn;
    RETURN dn;
END $$;

-- ---------------------------------------------------------------- FR-12 split a donation into one unit per component
CREATE FUNCTION separate_donation(p_donation int, p_components text[]) RETURNS SETOF varchar LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT status FROM donation WHERE donation_id = p_donation) = 'Discarded' THEN
        RAISE EXCEPTION 'donation % was discarded', p_donation; END IF;
    RETURN QUERY
    INSERT INTO blood_unit (unit_id, donation_id, component_code)
    SELECT format('U%s-%s', p_donation, c), p_donation, c FROM unnest(p_components) AS c
    RETURNING unit_id;
    PERFORM release_tested(p_donation);          -- tests may already be in when the bag is split
END $$;

-- ---------------------------------------------------------------- FR-13 release a donation's units once every active test is negative
-- Returns the number of units still waiting for tests (0 = released or nothing to release).
CREATE FUNCTION release_tested(p_donation int) RETURNS int LANGUAGE plpgsql AS $$
DECLARE pending int;
BEGIN
    SELECT count(*) INTO pending FROM test_type t
    WHERE t.is_active AND NOT EXISTS (SELECT 1 FROM test_result r WHERE r.donation_id = p_donation AND r.test_code = t.test_code);
    IF pending = 0 AND (SELECT status FROM donation WHERE donation_id = p_donation) <> 'Discarded' THEN
        UPDATE blood_unit SET status = 'Available' WHERE donation_id = p_donation AND status = 'Collected';
    END IF;
    RETURN pending;
END $$;

-- p_results maps test_code -> 'Negative' | 'Positive'. A positive result fires on_positive_test (discard + flag).
CREATE FUNCTION record_tests(p_donation int, p_results jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE pending int;
BEGIN
    INSERT INTO test_result (donation_id, test_code, result)
    SELECT p_donation, key, value FROM jsonb_each_text(p_results);
    IF (SELECT status FROM donation WHERE donation_id = p_donation) = 'Discarded' THEN RETURN 'Discarded'; END IF;
    pending := release_tested(p_donation);
    RETURN CASE WHEN pending > 0 THEN format('Awaiting %s test(s)', pending) ELSE 'Released' END;
END $$;

-- ---------------------------------------------------------------- FR-26 donation call to eligible, compatible donors only
-- p_donor_codes NULL = call every match from eligible_donors(); otherwise only the listed codes that match.
CREATE FUNCTION send_donation_call(p_request int, p_staff int, p_message text, p_donor_codes text[] DEFAULT NULL)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE cid int; n int;
BEGIN
    IF (SELECT status FROM blood_request WHERE request_id = p_request) IS DISTINCT FROM 'Open' THEN
        RAISE EXCEPTION 'request % is not open', p_request; END IF;
    INSERT INTO donation_call (request_id, sent_by, message) VALUES (p_request, p_staff, p_message) RETURNING call_id INTO cid;
    INSERT INTO call_recipient (call_id, donor_id)
    SELECT cid, o.user_id FROM eligible_donors(p_request) e JOIN donor o USING (donor_code)
    WHERE p_donor_codes IS NULL OR e.donor_code = ANY (p_donor_codes);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n = 0 THEN RAISE EXCEPTION 'no eligible, compatible donor to call for request %', p_request; END IF;
    INSERT INTO notification (user_id, message)
    SELECT donor_id, p_message FROM call_recipient WHERE call_id = cid;
    RETURN cid;
END $$;

-- ---------------------------------------------------------------- FR-27 / UC-20 a donor answers a call once
-- Returns the facility's name, address and phone on accept, 'declined', or 'closed' when the request no longer needs donors (2a).
CREATE FUNCTION respond_to_call(p_call int, p_donor int, p_accept boolean) RETURNS text LANGUAGE plpgsql AS $$
DECLARE st text; fname text; faddr text; fphone text;
BEGIN
    PERFORM require_session_user(p_donor);          -- UC-20: only the called donor answers
    SELECT br.status, fa.name, fa.address, fa.phone INTO st, fname, faddr, fphone
    FROM call_recipient cr JOIN donation_call c USING (call_id) JOIN blood_request br ON br.request_id = c.request_id
    JOIN staff s ON s.user_id = br.requested_by JOIN facility fa ON fa.facility_id = s.facility_id
    WHERE cr.call_id = p_call AND cr.donor_id = p_donor AND cr.response = 'Pending';
    IF NOT FOUND THEN RAISE EXCEPTION 'no pending call % for donor %', p_call, p_donor; END IF;
    IF st <> 'Open' THEN RETURN 'closed'; END IF;
    UPDATE call_recipient SET response = CASE WHEN p_accept THEN 'Accepted' ELSE 'Declined' END, responded_at = now()
    WHERE call_id = p_call AND donor_id = p_donor;
    RETURN CASE WHEN p_accept THEN concat_ws(', ', fname, faddr, fphone) ELSE 'declined' END;
END $$;

-- ---------------------------------------------------------------- UC-06 / FR-08 staff record a deferral at the in-person check
-- A temporary reason without an end date gets the reason's default length; a permanent reason takes no end date (2a).
CREATE FUNCTION record_deferral(p_staff int, p_donor int, p_reason text, p_end date DEFAULT NULL) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE id int;
BEGIN
    PERFORM require_role(p_staff, 'Staff');
    INSERT INTO deferral (donor_id, reason_code, end_date, recorded_by)
    SELECT p_donor, reason_code, CASE WHEN is_permanent THEN p_end ELSE coalesce(p_end, CURRENT_DATE + default_days) END, p_staff
    FROM deferral_reason WHERE reason_code = p_reason RETURNING deferral_id INTO id;
    IF id IS NULL THEN RAISE EXCEPTION 'unknown deferral reason %', p_reason; END IF;
    RETURN id;
END $$;

-- ---------------------------------------------------------------- UC-15 / FR-16 discard a damaged or unsafe unit (Table 1)
-- Staff discard Available units at their own facility; an Admin discards a Quarantined unit once the lookback
-- confirms it (UC-22). A Reserved unit must be released from its request first (2a). Collected -> Discarded is UC-09's.
CREATE FUNCTION discard_unit(p_user int, p_unit text, p_reason text) RETURNS void LANGUAGE plpgsql AS $$
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
CREATE FUNCTION set_threshold(p_user int, p_fac int, p_group text, p_comp text, p_min int) RETURNS void LANGUAGE plpgsql AS $$
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
CREATE FUNCTION create_admin(p_admin int, p_email text, p_password_hash text, p_first text, p_last text) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE uid int;
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    INSERT INTO user_account (email, password_hash, first_name, last_name, role, created_by)
    VALUES (lower(p_email), p_password_hash, p_first, p_last, 'Admin', p_admin) RETURNING user_id INTO uid;
    INSERT INTO admin (user_id) VALUES (uid);
    RETURN uid;
END $$;

-- accounts are deactivated, never deleted (traceability); reactivating also clears a lockout
CREATE FUNCTION set_account_active(p_admin int, p_user int, p_active boolean) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    UPDATE user_account SET is_active = p_active, failed_logins = 0, locked_until = NULL WHERE user_id = p_user;
    IF NOT FOUND THEN RAISE EXCEPTION 'user % not found', p_user; END IF;
END $$;

-- 2a: a facility with open requests cannot be deactivated until they are closed or moved
CREATE FUNCTION set_facility_active(p_admin int, p_fac int, p_active boolean) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF NOT p_active AND EXISTS (SELECT 1 FROM v_request WHERE facility_id = p_fac AND status = 'Open') THEN
        RAISE EXCEPTION 'facility % has open requests: close or move them first', p_fac; END IF;
    UPDATE facility SET is_active = p_active WHERE facility_id = p_fac;
    IF NOT FOUND THEN RAISE EXCEPTION 'facility % not found', p_fac; END IF;
END $$;

-- p_id NULL creates a facility; otherwise edits it. Returns the facility_id.
CREATE FUNCTION save_facility(p_admin int, p_id int, p_name text, p_type text, p_address text, p_phone text, p_email text)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE id int;
BEGIN
    PERFORM require_role(p_admin, 'Admin');
    IF p_id IS NULL THEN
        INSERT INTO facility (name, facility_type, address, phone, email) VALUES (p_name, p_type, p_address, p_phone, p_email)
        RETURNING facility_id INTO id;
    ELSE
        UPDATE facility SET name = p_name, facility_type = p_type, address = p_address, phone = p_phone, email = p_email
        WHERE facility_id = p_id RETURNING facility_id INTO id;
        IF id IS NULL THEN RAISE EXCEPTION 'facility % not found', p_id; END IF;
    END IF;
    RETURN id;
END $$;

-- ---------------------------------------------------------------- UC-03 / FR-03 a donor edits their contact details and weight
-- The blood group is not a parameter: once verified only staff change it (donor_group_guard).
CREATE FUNCTION update_donor_profile(p_donor int, p_phone text, p_email text, p_address text, p_weight numeric)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM require_role(p_donor, 'Donor');
    UPDATE user_account SET phone = p_phone, email = lower(p_email) WHERE user_id = p_donor;
    UPDATE donor SET address = p_address, weight_kg = p_weight WHERE user_id = p_donor;
END $$;
