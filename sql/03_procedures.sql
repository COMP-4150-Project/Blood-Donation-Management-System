SET search_path = vytal;

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
DECLARE sid int; missing int;
BEGIN
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

-- ---------------------------------------------------------------- FR-27 a donor answers a call once
CREATE FUNCTION respond_to_call(p_call int, p_donor int, p_accept boolean) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE call_recipient SET response = CASE WHEN p_accept THEN 'Accepted' ELSE 'Declined' END, responded_at = now()
    WHERE call_id = p_call AND donor_id = p_donor AND response = 'Pending';
    IF NOT FOUND THEN RAISE EXCEPTION 'no pending call % for donor %', p_call, p_donor; END IF;
END $$;
