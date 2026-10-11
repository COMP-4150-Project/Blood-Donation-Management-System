-- SE-04 least privilege; SE-08 / DI-04 history is never edited or deleted by the application
SET search_path = vytal;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vytal_app') THEN CREATE ROLE vytal_app; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vytal_report') THEN CREATE ROLE vytal_report; END IF;
END $$;
ALTER ROLE vytal_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;     -- roles are cluster-wide and may pre-exist
ALTER ROLE vytal_report LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;

REVOKE ALL ON ALL TABLES IN SCHEMA vytal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vytal FROM PUBLIC;

-- application: read and write, never DELETE or TRUNCATE; audit rows are written only by SECURITY DEFINER functions
GRANT USAGE ON SCHEMA vytal TO vytal_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA vytal TO vytal_app;
REVOKE INSERT, UPDATE ON audit_log FROM vytal_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA vytal TO vytal_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA vytal TO vytal_app;

-- reporting (FR-34 dashboards, analytics queries): read-only, and no login secrets
GRANT USAGE ON SCHEMA vytal TO vytal_report;
GRANT SELECT ON ALL TABLES IN SCHEMA vytal TO vytal_report;
REVOKE SELECT ON user_account FROM vytal_report;
GRANT SELECT (user_id, first_name, last_name, role, is_active, created_at) ON user_account TO vytal_report;
