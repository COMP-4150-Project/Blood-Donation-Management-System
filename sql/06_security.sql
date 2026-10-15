-- SE-04 least privilege; SE-08 / DI-04 history is never edited or deleted by the application
SET search_path = vytal;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vytal_owner') THEN CREATE ROLE vytal_owner; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vytal_app') THEN CREATE ROLE vytal_app; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vytal_report') THEN CREATE ROLE vytal_report; END IF;
END $$;
ALTER ROLE vytal_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;  -- roles are cluster-wide and may pre-exist
ALTER ROLE vytal_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE vytal_report LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
-- dev defaults; pass real ones with psql -v app_pw=... -v report_pw=... (setup.sh forwards VYTAL_APP_PW / VYTAL_REPORT_PW)
\if :{?app_pw} \else \set app_pw 'vytal_app_dev' \endif
\if :{?report_pw} \else \set report_pw 'vytal_report_dev' \endif
ALTER ROLE vytal_app PASSWORD :'app_pw';
ALTER ROLE vytal_report PASSWORD :'report_pw';

REVOKE ALL ON ALL TABLES IN SCHEMA vytal FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA vytal FROM PUBLIC;
REVOKE ALL ON ALL ROUTINES IN SCHEMA vytal FROM PUBLIC;

-- vytal_owner: a login-less, non-superuser role that owns every routine. Procedures run as it (SECURITY DEFINER), so it
-- holds exactly the table rights they need: read and write, an append-only audit log, and one DELETE (UC-24 removes a pair).
GRANT USAGE ON SCHEMA vytal TO vytal_owner;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA vytal TO vytal_owner;
REVOKE UPDATE ON audit_log FROM vytal_owner;
GRANT DELETE ON compatibility TO vytal_owner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vytal TO vytal_owner;
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig, p.prokind FROM pg_proc p WHERE p.pronamespace = 'vytal'::regnamespace LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO vytal_owner', r.sig);
        IF r.prokind = 'p' THEN
            EXECUTE format('ALTER PROCEDURE %s SECURITY DEFINER SET search_path = vytal, pg_temp', r.sig);
        END IF;
    END LOOP;
END $$;

-- application: reads tables and never writes them; every change goes through a procedure (CALL). Each procedure checks in
-- the database that the acting user (vytal.user_id, stamped by the backend after login) is active, holds the right role
-- and, for staff, works at the facility the record belongs to. Internal helpers and trigger functions are not callable.
GRANT USAGE ON SCHEMA vytal TO vytal_app;
GRANT SELECT ON ALL TABLES IN SCHEMA vytal TO vytal_app;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA vytal TO vytal_app;
REVOKE EXECUTE ON PROCEDURE release_tested(int, int) FROM vytal_app;      -- internal step of separate_donation / record_tests
GRANT EXECUTE ON FUNCTION donor_eligibility(int, date), eligible_donors(int), donors_for_group(text, text) TO vytal_app;
-- SE-07 minimum disclosure: no password hashes or lockout counters, contact details, screening answers, audit values or
-- token hashes by SELECT. The screens that need them read them through these scoped functions (03_procedures.sql).
REVOKE SELECT ON user_account FROM vytal_app;
GRANT SELECT (user_id, first_name, last_name, role, is_active, created_by, created_at) ON user_account TO vytal_app;
REVOKE SELECT ON donor FROM vytal_app;
GRANT SELECT (user_id, donor_code, date_of_birth, sex, weight_kg, blood_group, verified_by, verified_at, is_flagged,
              next_eligible_date) ON donor TO vytal_app;
REVOKE SELECT ON screening_answer FROM vytal_app;
REVOKE SELECT ON audit_log FROM vytal_app;
GRANT SELECT (audit_id, user_id, action, table_name, record_id, field_name, changed_at) ON audit_log TO vytal_app;
GRANT EXECUTE ON FUNCTION login_lookup(text), my_profile(), screening_answers(int),
                          audit_entries(int, text, text, timestamptz, timestamptz, int) TO vytal_app;
REVOKE SELECT ON user_session FROM vytal_app;
GRANT SELECT (session_id, user_id, created_at, last_seen_at, ended_at) ON user_session TO vytal_app;

-- reporting (FR-34 dashboards, analytics queries): read-only, and no login secrets
GRANT USAGE ON SCHEMA vytal TO vytal_report;
GRANT SELECT ON ALL TABLES IN SCHEMA vytal TO vytal_report;
REVOKE SELECT ON user_account FROM vytal_report;
GRANT SELECT (user_id, first_name, last_name, role, is_active, created_at) ON user_account TO vytal_report;
-- audit values can hold the same secrets (emails, phones), so reports see who changed what and when, not the values;
-- a column grant is simpler than a filtering view and cannot drift from the table
REVOKE SELECT ON audit_log FROM vytal_report;
GRANT SELECT (audit_id, user_id, action, table_name, record_id, field_name, changed_at) ON audit_log TO vytal_report;
-- the read-only calculations the views and key queries call (v_donor_eligibility, Q03); never a procedure
GRANT EXECUTE ON FUNCTION donor_eligibility(int, date), eligible_donors(int) TO vytal_report;
REVOKE SELECT ON user_session FROM vytal_report;
GRANT SELECT (session_id, user_id, created_at, last_seen_at, ended_at) ON user_session TO vytal_report;
