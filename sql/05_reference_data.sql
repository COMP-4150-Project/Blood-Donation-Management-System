-- Clinical reference data (FR-32 / MA-01: an Admin can change any of these rows without a code change)
SET search_path = vytal;

INSERT INTO blood_group VALUES
    ('O-','O','-'), ('O+','O','+'), ('A-','A','-'), ('A+','A','+'),
    ('B-','B','-'), ('B+','B','+'), ('AB-','AB','-'), ('AB+','AB','+');

INSERT INTO component_type VALUES ('RBC','Red Blood Cells',42), ('PLASMA','Plasma',365), ('PLT','Platelets',5);

-- Red cells and platelets: the donor's ABO antigens must be a subset of the recipient's, and Rh- recipients need Rh- units (27 pairs each).
INSERT INTO compatibility
SELECT c.code, r.blood_group, d.blood_group
FROM blood_group r CROSS JOIN blood_group d CROSS JOIN (VALUES ('RBC'), ('PLT')) AS c(code)
WHERE (d.abo = 'O' OR position(d.abo IN r.abo) > 0) AND (d.rh = '-' OR r.rh = '+');

-- Plasma is the reverse (ABO only): the donor's plasma must carry no antibody against the recipient's antigens (36 pairs).
INSERT INTO compatibility
SELECT 'PLASMA', r.blood_group, d.blood_group
FROM blood_group r CROSS JOIN blood_group d
WHERE r.abo = 'O' OR position(r.abo IN d.abo) > 0;

INSERT INTO donation_type VALUES ('WB','Whole Blood'), ('PLS','Plasma'), ('PLT','Platelets');
INSERT INTO donation_interval VALUES
    ('WB','M',56), ('WB','F',84), ('PLS','M',7), ('PLS','F',7), ('PLT','M',14), ('PLT','F',14);
INSERT INTO eligibility_rule VALUES ('M',17,50,130), ('F',17,50,125);

INSERT INTO deferral_reason VALUES
    ('TATTOO','Tattoo or piercing in the last 3 months',FALSE,90),
    ('TRAVEL','Travel to a malaria-risk area in the last 3 months',FALSE,90),
    ('UNWELL','Feeling unwell on the day of donation',FALSE,1),
    ('LOWHB','Hemoglobin below the minimum',FALSE,56),
    ('HBV','Hepatitis B positive',TRUE,NULL),
    ('HIV','HIV positive',TRUE,NULL);

INSERT INTO test_type (test_code, name) VALUES
    ('HIV','HIV-1/2'), ('HBV','Hepatitis B'), ('HCV','Hepatitis C'), ('SYPH','Syphilis');

INSERT INTO screening_question (question_text, reason_code, defer_on_answer) VALUES
    ('Have you had a tattoo or piercing in the last 3 months?','TATTOO',TRUE),
    ('Have you travelled to a malaria-risk area in the last 3 months?','TRAVEL',TRUE),
    ('Are you feeling well today?','UNWELL',FALSE),
    ('Do you have any questions for staff before donating?',NULL,NULL);
