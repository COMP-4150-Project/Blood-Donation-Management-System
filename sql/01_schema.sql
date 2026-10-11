DROP SCHEMA IF EXISTS vytal CASCADE;
CREATE SCHEMA vytal;
SET search_path = vytal;

CREATE TABLE user_account (
    user_id SERIAL NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    phone VARCHAR(20),
    role VARCHAR(10) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_logins SMALLINT NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    created_by INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id),
    CHECK (role IN ('Donor','Staff','Admin')),
    CHECK (failed_logins >= 0)
);
CREATE TABLE donor (
    user_id INT NOT NULL,
    donor_code VARCHAR(10) NOT NULL UNIQUE,
    date_of_birth DATE NOT NULL,
    sex CHAR(1) NOT NULL,
    weight_kg NUMERIC(5,1) NOT NULL,
    address VARCHAR(255),
    blood_group VARCHAR(3),
    verified_by INT,
    verified_at TIMESTAMPTZ,
    is_flagged BOOLEAN NOT NULL DEFAULT FALSE,
    next_eligible_date DATE,
    PRIMARY KEY (user_id),
    CHECK (weight_kg > 0),
    CHECK ((verified_by IS NULL) = (verified_at IS NULL)),
    CHECK (verified_by IS NULL OR blood_group IS NOT NULL)
);
CREATE TABLE staff (
    user_id INT NOT NULL,
    facility_id INT NOT NULL,
    job_title VARCHAR(60),
    PRIMARY KEY (user_id)
);
CREATE TABLE admin (
    user_id INT NOT NULL,
    PRIMARY KEY (user_id)
);
CREATE TABLE facility (
    facility_id SERIAL NOT NULL,
    name VARCHAR(100) NOT NULL UNIQUE,
    facility_type VARCHAR(12) NOT NULL,
    address VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    email VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (facility_id),
    CHECK (facility_type IN ('Hospital','Blood Bank'))
);
CREATE TABLE blood_group (
    blood_group VARCHAR(3) NOT NULL,
    abo VARCHAR(2) NOT NULL,
    rh CHAR(1) NOT NULL,
    PRIMARY KEY (blood_group),
    UNIQUE (abo, rh),
    CHECK (abo IN ('O','A','B','AB')),
    CHECK (rh IN ('+','-'))
);
CREATE TABLE component_type (
    component_code VARCHAR(10) NOT NULL,
    name VARCHAR(40) NOT NULL UNIQUE,
    shelf_life_days INT NOT NULL,
    PRIMARY KEY (component_code),
    CHECK (shelf_life_days > 0)
);
CREATE TABLE compatibility (
    component_code VARCHAR(10) NOT NULL,
    recipient_group VARCHAR(3) NOT NULL,
    donor_group VARCHAR(3) NOT NULL,
    PRIMARY KEY (component_code, recipient_group, donor_group)
);
CREATE TABLE donation_type (
    donation_type_code VARCHAR(5) NOT NULL,
    name VARCHAR(40) NOT NULL UNIQUE,
    PRIMARY KEY (donation_type_code)
);
CREATE TABLE donation_interval (
    donation_type_code VARCHAR(5) NOT NULL,
    sex CHAR(1) NOT NULL,
    interval_days INT NOT NULL,
    PRIMARY KEY (donation_type_code, sex),
    CHECK (sex IN ('M','F')),
    CHECK (interval_days > 0)
);
CREATE TABLE eligibility_rule (
    sex CHAR(1) NOT NULL,
    min_age_years INT NOT NULL,
    min_weight_kg NUMERIC(5,1) NOT NULL,
    min_hemoglobin_gl NUMERIC(4,1) NOT NULL,
    PRIMARY KEY (sex),
    CHECK (sex IN ('M','F')),
    CHECK (min_age_years > 0),
    CHECK (min_weight_kg > 0),
    CHECK (min_hemoglobin_gl > 0)
);
CREATE TABLE deferral_reason (
    reason_code VARCHAR(10) NOT NULL,
    description VARCHAR(200) NOT NULL,
    is_permanent BOOLEAN NOT NULL,
    default_days INT,
    PRIMARY KEY (reason_code),
    CHECK ((is_permanent AND default_days IS NULL) OR (NOT is_permanent AND default_days > 0))
);
CREATE TABLE test_type (
    test_code VARCHAR(10) NOT NULL,
    name VARCHAR(60) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (test_code)
);
CREATE TABLE screening_question (
    question_id SERIAL NOT NULL,
    question_text VARCHAR(300) NOT NULL,
    reason_code VARCHAR(10),
    defer_on_answer BOOLEAN,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (question_id),
    CHECK ((reason_code IS NULL) = (defer_on_answer IS NULL))
);
CREATE TABLE screening (
    screening_id SERIAL NOT NULL,
    donor_id INT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (screening_id)
);
CREATE TABLE screening_answer (
    screening_id INT NOT NULL,
    question_id INT NOT NULL,
    answer BOOLEAN NOT NULL,
    PRIMARY KEY (screening_id, question_id)
);
CREATE TABLE deferral (
    deferral_id SERIAL NOT NULL,
    donor_id INT NOT NULL,
    reason_code VARCHAR(10) NOT NULL,
    start_date DATE NOT NULL DEFAULT CURRENT_DATE,
    end_date DATE,
    screening_id INT,
    recorded_by INT,
    PRIMARY KEY (deferral_id),
    CHECK (end_date IS NULL OR end_date > start_date),
    CHECK (recorded_by IS NOT NULL OR screening_id IS NOT NULL)
);
CREATE TABLE donation (
    donation_id SERIAL NOT NULL,
    donor_id INT NOT NULL,
    recorded_by INT NOT NULL,
    donation_type_code VARCHAR(5) NOT NULL,
    donated_at TIMESTAMPTZ NOT NULL,
    blood_group VARCHAR(3) NOT NULL,
    volume_ml INT NOT NULL,
    hemoglobin_gl NUMERIC(4,1) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Awaiting separation',
    PRIMARY KEY (donation_id),
    CHECK (volume_ml > 0),
    CHECK (hemoglobin_gl > 0),
    CHECK (status IN ('Awaiting separation','Separated','Discarded'))
);
CREATE TABLE test_result (
    donation_id INT NOT NULL,
    test_code VARCHAR(10) NOT NULL,
    result VARCHAR(8) NOT NULL,
    tested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (donation_id, test_code),
    CHECK (result IN ('Negative','Positive'))
);
CREATE TABLE blood_unit (
    unit_id VARCHAR(20) NOT NULL,
    donation_id INT NOT NULL,
    component_code VARCHAR(10) NOT NULL,
    facility_id INT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'Collected',
    status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status_reason VARCHAR(200),
    PRIMARY KEY (unit_id),
    UNIQUE (donation_id, component_code),
    CHECK (status IN ('Collected','Available','Reserved','Issued','Discarded','Expired','Quarantined'))
);
CREATE TABLE wastage_record (
    wastage_id SERIAL NOT NULL,
    unit_id VARCHAR(20) NOT NULL UNIQUE,
    cause VARCHAR(12) NOT NULL,
    wasted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (wastage_id),
    CHECK (cause IN ('Expired','Discarded'))
);
CREATE TABLE stock_threshold (
    facility_id INT NOT NULL,
    blood_group VARCHAR(3) NOT NULL,
    component_code VARCHAR(10) NOT NULL,
    min_units INT NOT NULL,
    PRIMARY KEY (facility_id, blood_group, component_code),
    CHECK (min_units >= 0)
);
CREATE TABLE stock_alert (
    alert_id SERIAL NOT NULL,
    facility_id INT NOT NULL,
    blood_group VARCHAR(3) NOT NULL,
    component_code VARCHAR(10) NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at TIMESTAMPTZ,
    PRIMARY KEY (alert_id),
    CHECK (cleared_at IS NULL OR cleared_at >= opened_at)
);
CREATE UNIQUE INDEX stock_alert_facility_id_blood_group_component_code_active ON stock_alert (facility_id, blood_group, component_code) WHERE cleared_at IS NULL;
CREATE TABLE blood_request (
    request_id SERIAL NOT NULL,
    requested_by INT NOT NULL,
    patient_ref VARCHAR(20) NOT NULL,
    recipient_group VARCHAR(3),
    component_code VARCHAR(10) NOT NULL,
    units_requested INT NOT NULL,
    urgency VARCHAR(10) NOT NULL,
    required_by TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    PRIMARY KEY (request_id),
    CHECK (units_requested > 0),
    CHECK (urgency IN ('Emergency','Urgent','Routine')),
    CHECK (status IN ('Open','Fulfilled','Partially Fulfilled','Cancelled'))
);
CREATE TABLE request_unit (
    request_id INT NOT NULL,
    unit_id VARCHAR(20) NOT NULL,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    PRIMARY KEY (request_id, unit_id),
    CHECK (NOT (issued_at IS NOT NULL AND released_at IS NOT NULL))
);
CREATE UNIQUE INDEX request_unit_unit_id_active ON request_unit (unit_id) WHERE released_at IS NULL;
CREATE TABLE donation_call (
    call_id SERIAL NOT NULL,
    request_id INT NOT NULL,
    sent_by INT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    message VARCHAR(500) NOT NULL,
    PRIMARY KEY (call_id)
);
CREATE TABLE call_recipient (
    call_id INT NOT NULL,
    donor_id INT NOT NULL,
    response VARCHAR(10) NOT NULL DEFAULT 'Pending',
    responded_at TIMESTAMPTZ,
    PRIMARY KEY (call_id, donor_id),
    CHECK (response IN ('Pending','Accepted','Declined'))
);
CREATE TABLE audit_log (
    audit_id BIGSERIAL NOT NULL,
    user_id INT,
    action VARCHAR(6) NOT NULL,
    table_name VARCHAR(40) NOT NULL,
    record_id VARCHAR(40) NOT NULL,
    field_name VARCHAR(40),
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (audit_id),
    CHECK (action IN ('INSERT','UPDATE','DELETE','JOB'))
);
CREATE TABLE notification (
    notification_id SERIAL NOT NULL,
    user_id INT NOT NULL,
    message VARCHAR(500) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    PRIMARY KEY (notification_id),
    CHECK (read_at IS NULL OR read_at >= created_at)
);

ALTER TABLE user_account ADD CONSTRAINT fk_user_account_0 FOREIGN KEY (created_by) REFERENCES admin (user_id) ON DELETE RESTRICT;
ALTER TABLE donor ADD CONSTRAINT fk_donor_0 FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT;
ALTER TABLE donor ADD CONSTRAINT fk_donor_1 FOREIGN KEY (sex) REFERENCES eligibility_rule (sex) ON DELETE RESTRICT;
ALTER TABLE donor ADD CONSTRAINT fk_donor_2 FOREIGN KEY (blood_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE donor ADD CONSTRAINT fk_donor_3 FOREIGN KEY (verified_by) REFERENCES staff (user_id) ON DELETE RESTRICT;
ALTER TABLE staff ADD CONSTRAINT fk_staff_0 FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT;
ALTER TABLE staff ADD CONSTRAINT fk_staff_1 FOREIGN KEY (facility_id) REFERENCES facility (facility_id) ON DELETE RESTRICT;
ALTER TABLE admin ADD CONSTRAINT fk_admin_0 FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT;
ALTER TABLE compatibility ADD CONSTRAINT fk_compatibility_0 FOREIGN KEY (component_code) REFERENCES component_type (component_code) ON DELETE RESTRICT;
ALTER TABLE compatibility ADD CONSTRAINT fk_compatibility_1 FOREIGN KEY (recipient_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE compatibility ADD CONSTRAINT fk_compatibility_2 FOREIGN KEY (donor_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE donation_interval ADD CONSTRAINT fk_donation_interval_0 FOREIGN KEY (donation_type_code) REFERENCES donation_type (donation_type_code) ON DELETE RESTRICT;
ALTER TABLE screening_question ADD CONSTRAINT fk_screening_question_0 FOREIGN KEY (reason_code) REFERENCES deferral_reason (reason_code) ON DELETE RESTRICT;
ALTER TABLE screening ADD CONSTRAINT fk_screening_0 FOREIGN KEY (donor_id) REFERENCES donor (user_id) ON DELETE RESTRICT;
ALTER TABLE screening_answer ADD CONSTRAINT fk_screening_answer_0 FOREIGN KEY (screening_id) REFERENCES screening (screening_id) ON DELETE RESTRICT;
ALTER TABLE screening_answer ADD CONSTRAINT fk_screening_answer_1 FOREIGN KEY (question_id) REFERENCES screening_question (question_id) ON DELETE RESTRICT;
ALTER TABLE deferral ADD CONSTRAINT fk_deferral_0 FOREIGN KEY (donor_id) REFERENCES donor (user_id) ON DELETE RESTRICT;
ALTER TABLE deferral ADD CONSTRAINT fk_deferral_1 FOREIGN KEY (reason_code) REFERENCES deferral_reason (reason_code) ON DELETE RESTRICT;
ALTER TABLE deferral ADD CONSTRAINT fk_deferral_2 FOREIGN KEY (screening_id) REFERENCES screening (screening_id) ON DELETE RESTRICT;
ALTER TABLE deferral ADD CONSTRAINT fk_deferral_3 FOREIGN KEY (recorded_by) REFERENCES staff (user_id) ON DELETE RESTRICT;
ALTER TABLE donation ADD CONSTRAINT fk_donation_0 FOREIGN KEY (donor_id) REFERENCES donor (user_id) ON DELETE RESTRICT;
ALTER TABLE donation ADD CONSTRAINT fk_donation_1 FOREIGN KEY (recorded_by) REFERENCES staff (user_id) ON DELETE RESTRICT;
ALTER TABLE donation ADD CONSTRAINT fk_donation_2 FOREIGN KEY (donation_type_code) REFERENCES donation_type (donation_type_code) ON DELETE RESTRICT;
ALTER TABLE donation ADD CONSTRAINT fk_donation_3 FOREIGN KEY (blood_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE test_result ADD CONSTRAINT fk_test_result_0 FOREIGN KEY (donation_id) REFERENCES donation (donation_id) ON DELETE RESTRICT;
ALTER TABLE test_result ADD CONSTRAINT fk_test_result_1 FOREIGN KEY (test_code) REFERENCES test_type (test_code) ON DELETE RESTRICT;
ALTER TABLE blood_unit ADD CONSTRAINT fk_blood_unit_0 FOREIGN KEY (donation_id) REFERENCES donation (donation_id) ON DELETE RESTRICT;
ALTER TABLE blood_unit ADD CONSTRAINT fk_blood_unit_1 FOREIGN KEY (component_code) REFERENCES component_type (component_code) ON DELETE RESTRICT;
ALTER TABLE blood_unit ADD CONSTRAINT fk_blood_unit_2 FOREIGN KEY (facility_id) REFERENCES facility (facility_id) ON DELETE RESTRICT;
ALTER TABLE wastage_record ADD CONSTRAINT fk_wastage_record_0 FOREIGN KEY (unit_id) REFERENCES blood_unit (unit_id) ON DELETE RESTRICT;
ALTER TABLE stock_threshold ADD CONSTRAINT fk_stock_threshold_0 FOREIGN KEY (facility_id) REFERENCES facility (facility_id) ON DELETE RESTRICT;
ALTER TABLE stock_threshold ADD CONSTRAINT fk_stock_threshold_1 FOREIGN KEY (blood_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE stock_threshold ADD CONSTRAINT fk_stock_threshold_2 FOREIGN KEY (component_code) REFERENCES component_type (component_code) ON DELETE RESTRICT;
ALTER TABLE stock_alert ADD CONSTRAINT fk_stock_alert_0 FOREIGN KEY (facility_id, blood_group, component_code) REFERENCES stock_threshold (facility_id, blood_group, component_code) ON DELETE RESTRICT;
ALTER TABLE blood_request ADD CONSTRAINT fk_blood_request_0 FOREIGN KEY (requested_by) REFERENCES staff (user_id) ON DELETE RESTRICT;
ALTER TABLE blood_request ADD CONSTRAINT fk_blood_request_1 FOREIGN KEY (recipient_group) REFERENCES blood_group (blood_group) ON DELETE RESTRICT;
ALTER TABLE blood_request ADD CONSTRAINT fk_blood_request_2 FOREIGN KEY (component_code) REFERENCES component_type (component_code) ON DELETE RESTRICT;
ALTER TABLE request_unit ADD CONSTRAINT fk_request_unit_0 FOREIGN KEY (request_id) REFERENCES blood_request (request_id) ON DELETE RESTRICT;
ALTER TABLE request_unit ADD CONSTRAINT fk_request_unit_1 FOREIGN KEY (unit_id) REFERENCES blood_unit (unit_id) ON DELETE RESTRICT;
ALTER TABLE donation_call ADD CONSTRAINT fk_donation_call_0 FOREIGN KEY (request_id) REFERENCES blood_request (request_id) ON DELETE RESTRICT;
ALTER TABLE donation_call ADD CONSTRAINT fk_donation_call_1 FOREIGN KEY (sent_by) REFERENCES staff (user_id) ON DELETE RESTRICT;
ALTER TABLE call_recipient ADD CONSTRAINT fk_call_recipient_0 FOREIGN KEY (call_id) REFERENCES donation_call (call_id) ON DELETE RESTRICT;
ALTER TABLE call_recipient ADD CONSTRAINT fk_call_recipient_1 FOREIGN KEY (donor_id) REFERENCES donor (user_id) ON DELETE RESTRICT;
ALTER TABLE audit_log ADD CONSTRAINT fk_audit_log_0 FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT;
ALTER TABLE notification ADD CONSTRAINT fk_notification_0 FOREIGN KEY (user_id) REFERENCES user_account (user_id) ON DELETE RESTRICT;
