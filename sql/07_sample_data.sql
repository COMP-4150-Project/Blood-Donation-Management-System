-- Vytal sample data: hand-designed, realistic activity over the last ten months at three hospitals and a blood centre.
-- Every row goes through the real constraints and triggers. Dates are relative to now() so the data never goes stale.
-- Load with psql (uses one psql variable): every account's password is Vytal-demo-2026, stored as a salted bcrypt hash (cost 12) (SE-01).
SET search_path = vytal;
\set pw '$2b$12$.d5mROZxm0o.9vc/teECyuBjT.teAXf9eJtHFrvULF42.iCz5EP0m'

-- History is loaded like a data migration: the audit log (FR-33) starts with today's live actions further down.
ALTER TABLE user_account DISABLE TRIGGER audit_user;
ALTER TABLE facility DISABLE TRIGGER audit_facility;
ALTER TABLE donor DISABLE TRIGGER audit_donor;
ALTER TABLE donation DISABLE TRIGGER audit_donation;
ALTER TABLE deferral DISABLE TRIGGER audit_deferral;
ALTER TABLE blood_unit DISABLE TRIGGER audit_unit;
ALTER TABLE blood_request DISABLE TRIGGER audit_request;

-- ---------------------------------------------------------------- facilities (FR-35)
INSERT INTO facility (facility_id, name, facility_type, address, phone, email) VALUES
    (1, 'Riverside General Hospital', 'Hospital', '1030 Riverside Dr E, Windsor, ON', '519-555-0101', 'bloodbank@riverside.example.ca'),
    (2, 'Essex County Hospital', 'Hospital', '250 Manning Rd, Tecumseh, ON', '519-555-0202', 'lab@essex.example.ca'),
    (3, 'Lakeshore Blood Centre', 'Blood Bank', '3200 Lakeshore Blvd, Windsor, ON', '519-555-0303', 'clinic@lakeshore.example.ca'),
    (4, 'St. Clair Community Hospital', 'Hospital', '725 Notre Dame St, Belle River, ON', '519-555-0404', 'lab@stclair.example.ca');

-- ---------------------------------------------------------------- admins and staff (FR-04): each staff member belongs to one facility
INSERT INTO user_account (user_id, email, password_hash, first_name, last_name, phone, role, created_by, created_at) VALUES
    (1, 'admin@vytal.example.ca', :'pw', 'Sara', 'Haddad', '519-555-0001', 'Admin', NULL, now() - interval '400 days');
INSERT INTO admin VALUES (1);
INSERT INTO user_account (user_id, email, password_hash, first_name, last_name, phone, role, created_by, created_at) VALUES
    (2, 'ops@vytal.example.ca', :'pw', 'Daniel', 'Okoro', '519-555-0002', 'Admin', 1, now() - interval '390 days');
INSERT INTO admin VALUES (2);
INSERT INTO user_account (user_id, email, password_hash, first_name, last_name, phone, role, created_by, created_at) VALUES
    (11, 'audit@vytal.example.ca', :'pw', 'Kevin', 'Tran', '519-555-0011', 'Admin', 1, now() - interval '200 days');
INSERT INTO admin VALUES (11);
INSERT INTO user_account (user_id, email, password_hash, first_name, last_name, phone, role, created_by, created_at) VALUES
    (3, 'm.ross@riverside.example.ca', :'pw', 'Maya', 'Ross', '519-555-0103', 'Staff', 1, now() - interval '380 days'),
    (4, 'j.campbell@riverside.example.ca', :'pw', 'James', 'Campbell', '519-555-0104', 'Staff', 1, now() - interval '380 days'),
    (5, 'a.kowalski@essex.example.ca', :'pw', 'Anna', 'Kowalski', '519-555-0205', 'Staff', 1, now() - interval '375 days'),
    (6, 't.osei@essex.example.ca', :'pw', 'Thomas', 'Osei', '519-555-0206', 'Staff', 2, now() - interval '375 days'),
    (7, 'r.desai@lakeshore.example.ca', :'pw', 'Rina', 'Desai', '519-555-0307', 'Staff', 2, now() - interval '370 days'),
    (8, 'p.murphy@lakeshore.example.ca', :'pw', 'Patrick', 'Murphy', '519-555-0308', 'Staff', 2, now() - interval '370 days'),
    (9, 'h.liu@stclair.example.ca', :'pw', 'Helen', 'Liu', '519-555-0409', 'Staff', 2, now() - interval '160 days'),
    (10, 'e.petrova@stclair.example.ca', :'pw', 'Elena', 'Petrova', '519-555-0410', 'Staff', 11, now() - interval '160 days');
INSERT INTO staff (user_id, facility_id, job_title) VALUES
    (3, 1, 'Emergency Department Nurse'), (4, 1, 'Medical Lab Technologist'),
    (5, 2, 'Transfusion Nurse'), (6, 2, 'Medical Lab Technologist'),
    (7, 3, 'Phlebotomist'), (8, 3, 'Clinic Supervisor'),
    (9, 4, 'Emergency Department Nurse'), (10, 4, 'Medical Lab Technologist');

-- ---------------------------------------------------------------- donors (FR-01, FR-09): group verified by staff at the first donation
-- Donor codes continue the Step 2 scenarios' numbering: D-1042 (donor 127) is the Scenario 5 lookback donor.
INSERT INTO user_account (user_id, email, password_hash, first_name, last_name, phone, role, created_at) VALUES
    (101, 'liam.tremblay@mail.ca', :'pw', 'Liam', 'Tremblay', '519-555-1101', 'Donor', now() - interval '310 days'),
    (102, 'emma.gagnon@mail.ca', :'pw', 'Emma', 'Gagnon', '519-555-1102', 'Donor', now() - interval '300 days'),
    (103, 'noah.roy@mail.ca', :'pw', 'Noah', 'Roy', '519-555-1103', 'Donor', now() - interval '320 days'),
    (104, 'olivia.cote@mail.ca', :'pw', 'Olivia', 'Côté', '519-555-1104', 'Donor', now() - interval '270 days'),
    (105, 'ethan.bouchard@mail.ca', :'pw', 'Ethan', 'Bouchard', '519-555-1105', 'Donor', now() - interval '350 days'),
    (106, 'ava.singh@mail.ca', :'pw', 'Ava', 'Singh', '519-555-1106', 'Donor', now() - interval '220 days'),
    (107, 'lucas.nguyen@mail.ca', :'pw', 'Lucas', 'Nguyen', '519-555-1107', 'Donor', now() - interval '260 days'),
    (108, 'mia.patel@mail.ca', :'pw', 'Mia', 'Patel', '519-555-1108', 'Donor', now() - interval '80 days'),
    (109, 'sofia.martin@mail.ca', :'pw', 'Sofia', 'Martin', '519-555-1109', 'Donor', now() - interval '170 days'),
    (110, 'owen.chen@mail.ca', :'pw', 'Owen', 'Chen', '519-555-1110', 'Donor', now() - interval '40 days'),
    (111, 'chloe.wilson@mail.ca', :'pw', 'Chloe', 'Wilson', '519-555-1111', 'Donor', now() - interval '330 days'),
    (112, 'jacob.khan@mail.ca', :'pw', 'Jacob', 'Khan', '519-555-1112', 'Donor', now() - interval '210 days'),
    (113, 'daniel.brown@mail.ca', :'pw', 'Daniel', 'Brown', '519-555-1113', 'Donor', now() - interval '315 days'),
    (114, 'zara.ali@mail.ca', :'pw', 'Zara', 'Ali', '519-555-1114', 'Donor', now() - interval '280 days'),
    (115, 'arjun.mehta@mail.ca', :'pw', 'Arjun', 'Mehta', '519-555-1115', 'Donor', now() - interval '300 days'),
    (116, 'grace.taylor@mail.ca', :'pw', 'Grace', 'Taylor', '519-555-1116', 'Donor', now() - interval '250 days'),
    (117, 'omar.ahmed@mail.ca', :'pw', 'Omar', 'Ahmed', '519-555-1117', 'Donor', now() - interval '195 days'),
    (118, 'mateo.lee@mail.ca', :'pw', 'Mateo', 'Lee', '519-555-1118', 'Donor', now() - interval '340 days'),
    (119, 'hana.park@mail.ca', :'pw', 'Hana', 'Park', '519-555-1119', 'Donor', now() - interval '230 days'),
    (120, 'ryan.morin@mail.ca', :'pw', 'Ryan', 'Morin', '519-555-1120', 'Donor', now() - interval '165 days'),
    (121, 'leah.shah@mail.ca', :'pw', 'Leah', 'Shah', '519-555-1121', 'Donor', now() - interval '280 days'),
    (122, 'kai.fortin@mail.ca', :'pw', 'Kai', 'Fortin', '519-555-1122', 'Donor', now() - interval '320 days'),
    (123, 'ines.lavoie@mail.ca', :'pw', 'Ines', 'Lavoie', '519-555-1123', 'Donor', now() - interval '110 days'),
    (124, 'ravi.joshi@mail.ca', :'pw', 'Ravi', 'Joshi', '519-555-1124', 'Donor', now() - interval '205 days'),
    (125, 'jonah.pelletier@mail.ca', :'pw', 'Jonah', 'Pelletier', '519-555-1125', 'Donor', now() - interval '290 days'),
    (126, 'amara.okafor@mail.ca', :'pw', 'Amara', 'Okafor', '519-555-1126', 'Donor', now() - interval '220 days'),
    (127, 'victor.belanger@mail.ca', :'pw', 'Victor', 'Belanger', '519-555-1127', 'Donor', now() - interval '310 days'),
    (128, 'elif.demir@mail.ca', :'pw', 'Elif', 'Demir', '519-555-1128', 'Donor', now() - interval '140 days'),
    (129, 'darius.reid@mail.ca', :'pw', 'Darius', 'Reid', '519-555-1129', 'Donor', now() - interval '220 days'),
    (130, 'meera.iyer@mail.ca', :'pw', 'Meera', 'Iyer', '519-555-1130', 'Donor', now() - interval '170 days'),
    (131, 'samuel.gauthier@mail.ca', :'pw', 'Samuel', 'Gauthier', '519-555-1131', 'Donor', now() - interval '100 days'),
    (132, 'nora.hughes@mail.ca', :'pw', 'Nora', 'Hughes', '519-555-1132', 'Donor', now() - interval '190 days'),
    (133, 'lily.morgan@mail.ca', :'pw', 'Lily', 'Morgan', '519-555-1133', 'Donor', now() - interval '45 days'),
    (134, 'theo.dubois@mail.ca', :'pw', 'Theo', 'Dubois', '519-555-1134', 'Donor', now() - interval '55 days'),
    (135, 'aisha.rahman@mail.ca', :'pw', 'Aisha', 'Rahman', '519-555-1135', 'Donor', now() - interval '15 days'),
    (136, 'felix.wong@mail.ca', :'pw', 'Felix', 'Wong', '519-555-1136', 'Donor', now() - interval '25 days'),
    (137, 'hannah.bergeron@mail.ca', :'pw', 'Hannah', 'Bergeron', '519-555-1137', 'Donor', now() - interval '330 days'),
    (138, 'william.fraser@mail.ca', :'pw', 'William', 'Fraser', '519-555-1138', 'Donor', now() - interval '330 days'),
    (139, 'julie.lefebvre@mail.ca', :'pw', 'Julie', 'Lefebvre', '519-555-1139', 'Donor', now() - interval '320 days'),
    (140, 'mohammed.siddiqui@mail.ca', :'pw', 'Mohammed', 'Siddiqui', '519-555-1140', 'Donor', now() - interval '310 days'),
    (141, 'kenji.sato@mail.ca', :'pw', 'Kenji', 'Sato', '519-555-1141', 'Donor', now() - interval '325 days'),
    (142, 'isabelle.roy@mail.ca', :'pw', 'Isabelle', 'Roy', '519-555-1142', 'Donor', now() - interval '310 days'),
    (143, 'gabriel.ouellet@mail.ca', :'pw', 'Gabriel', 'Ouellet', '519-555-1143', 'Donor', now() - interval '290 days'),
    (144, 'tyler.mackenzie@mail.ca', :'pw', 'Tyler', 'MacKenzie', '519-555-1144', 'Donor', now() - interval '130 days'),
    (145, 'chantal.leblanc@mail.ca', :'pw', 'Chantal', 'Leblanc', '519-555-1145', 'Donor', now() - interval '210 days'),
    (146, 'aiden.clarke@mail.ca', :'pw', 'Aiden', 'Clarke', '519-555-1146', 'Donor', now() - interval '200 days'),
    (147, 'maryam.hassan@mail.ca', :'pw', 'Maryam', 'Hassan', '519-555-1147', 'Donor', now() - interval '250 days'),
    (148, 'benjamin.girard@mail.ca', :'pw', 'Benjamin', 'Girard', '519-555-1148', 'Donor', now() - interval '170 days'),
    (149, 'sophie.nadeau@mail.ca', :'pw', 'Sophie', 'Nadeau', '519-555-1149', 'Donor', now() - interval '230 days'),
    (150, 'ali.rezaei@mail.ca', :'pw', 'Ali', 'Rezaei', '519-555-1150', 'Donor', now() - interval '140 days'),
    (151, 'charlotte.kim@mail.ca', :'pw', 'Charlotte', 'Kim', '519-555-1151', 'Donor', now() - interval '215 days'),
    (152, 'jordan.mitchell@mail.ca', :'pw', 'Jordan', 'Mitchell', '519-555-1152', 'Donor', now() - interval '260 days'),
    (153, 'fatima.noor@mail.ca', :'pw', 'Fatima', 'Noor', '519-555-1153', 'Donor', now() - interval '185 days'),
    (154, 'nathan.poirier@mail.ca', :'pw', 'Nathan', 'Poirier', '519-555-1154', 'Donor', now() - interval '155 days'),
    (155, 'rosa.alvarez@mail.ca', :'pw', 'Rosa', 'Alvarez', '519-555-1155', 'Donor', now() - interval '270 days'),
    (156, 'connor.doyle@mail.ca', :'pw', 'Connor', 'Doyle', '519-555-1156', 'Donor', now() - interval '125 days'),
    -- Scenario 2 (Priya) and Scenario 3 (Marcus)
    (157, 'priya.nair@mail.ca', :'pw', 'Priya', 'Nair', '519-555-1157', 'Donor', now() - interval '33 days'),
    (158, 'marcus.bell@mail.ca', :'pw', 'Marcus', 'Bell', '519-555-1158', 'Donor', now() - interval '300 days'),
    -- a first-time O-negative donor at Riverside, three self-registered donors who chose "I don't know" for their group,
    -- and donors who gave in the last four weeks at each site
    (159, 'ethan.lapointe@mail.ca', :'pw', 'Ethan', 'Lapointe', '519-555-1159', 'Donor', now() - interval '25 days'),
    (160, 'lucia.romano@mail.ca', :'pw', 'Lucia', 'Romano', '519-555-1160', 'Donor', now() - interval '20 days'),
    (161, 'marco.rossi@mail.ca', :'pw', 'Marco', 'Rossi', '519-555-1161', 'Donor', now() - interval '12 days'),
    (162, 'kofi.mensah@mail.ca', :'pw', 'Kofi', 'Mensah', '519-555-1162', 'Donor', now() - interval '3 days'),
    (163, 'adam.pereira@mail.ca', :'pw', 'Adam', 'Pereira', '519-555-1163', 'Donor', now() - interval '30 days'),
    (164, 'nina.volkova@mail.ca', :'pw', 'Nina', 'Volkova', '519-555-1164', 'Donor', now() - interval '60 days'),
    (165, 'jamal.wright@mail.ca', :'pw', 'Jamal', 'Wright', '519-555-1165', 'Donor', now() - interval '40 days'),
    (166, 'hiro.tanaka@mail.ca', :'pw', 'Hiro', 'Tanaka', '519-555-1166', 'Donor', now() - interval '45 days'),
    (167, 'amelie.caron@mail.ca', :'pw', 'Amélie', 'Caron', '519-555-1167', 'Donor', now() - interval '20 days'),
    (168, 'sean.byrne@mail.ca', :'pw', 'Sean', 'Byrne', '519-555-1168', 'Donor', now() - interval '50 days'),
    (169, 'leila.karimi@mail.ca', :'pw', 'Leila', 'Karimi', '519-555-1169', 'Donor', now() - interval '70 days');

INSERT INTO donor (user_id, donor_code, date_of_birth, sex, weight_kg, address, blood_group, verified_by, verified_at) VALUES
    (101, 'D-1016', '1985-03-14', 'M', 91, '201 Ouellette Ave, Windsor, ON', 'O+', 4, now() - interval '290 days' - interval '30 minutes'),
    (102, 'D-1017', '1992-07-22', 'F', 64, '202 Ouellette Ave, Windsor, ON', 'A+', 3, now() - interval '280 days' - interval '30 minutes'),
    (103, 'D-1018', '1979-11-02', 'M', 77, '203 Ouellette Ave, Windsor, ON', 'O-', 4, now() - interval '300 days' - interval '30 minutes'),
    (104, 'D-1019', '1996-01-30', 'F', 90, '204 Ouellette Ave, Windsor, ON', 'O+', 3, now() - interval '250 days' - interval '30 minutes'),
    (105, 'D-1020', '1970-06-18', 'M', 63, '205 Ouellette Ave, Windsor, ON', 'A+', 4, now() - interval '330 days' - interval '30 minutes'),
    (106, 'D-1021', '1999-09-05', 'F', 76, '206 Ouellette Ave, Windsor, ON', 'B+', 3, now() - interval '200 days' - interval '30 minutes'),
    (107, 'D-1022', '1988-12-11', 'M', 89, '207 Ouellette Ave, Windsor, ON', 'A-', 4, now() - interval '240 days' - interval '30 minutes'),
    (108, 'D-1023', '1983-04-27', 'M', 62, '208 Ouellette Ave, Windsor, ON', 'O+', 3, now() - interval '60 days' - interval '30 minutes'),
    (109, 'D-1024', '1994-08-16', 'F', 75, '209 Ouellette Ave, Windsor, ON', 'AB+', 4, now() - interval '150 days' - interval '30 minutes'),
    (110, 'D-1025', '1975-02-09', 'M', 88, '210 Ouellette Ave, Windsor, ON', 'O+', 3, now() - interval '20 days' - interval '30 minutes'),
    (111, 'D-1026', '1990-10-03', 'F', 61, '211 Ouellette Ave, Windsor, ON', 'A+', 4, now() - interval '310 days' - interval '30 minutes'),
    (112, 'D-1027', '1981-05-21', 'M', 74, '212 Ouellette Ave, Windsor, ON', 'O-', 3, now() - interval '190 days' - interval '30 minutes'),
    (113, 'D-1028', '1987-07-07', 'M', 87, '213 Ouellette Ave, Windsor, ON', 'O+', 5, now() - interval '295 days' - interval '30 minutes'),
    (114, 'D-1029', '1995-03-19', 'F', 60, '214 Ouellette Ave, Windsor, ON', 'A+', 5, now() - interval '260 days' - interval '30 minutes'),
    (115, 'D-1030', '1968-11-25', 'M', 73, '215 Ouellette Ave, Windsor, ON', 'B+', 6, now() - interval '280 days' - interval '30 minutes'),
    (116, 'D-1031', '1993-06-12', 'F', 86, '216 Ouellette Ave, Windsor, ON', 'O+', 5, now() - interval '230 days' - interval '30 minutes'),
    (117, 'D-1032', '1986-09-29', 'M', 59, '217 Ouellette Ave, Windsor, ON', 'A+', 5, now() - interval '175 days' - interval '30 minutes'),
    (118, 'D-1033', '1977-01-15', 'M', 72, '218 Ouellette Ave, Windsor, ON', 'O-', 5, now() - interval '320 days' - interval '30 minutes'),
    (119, 'D-1034', '1998-04-04', 'F', 85, '219 Ouellette Ave, Windsor, ON', 'A-', 6, now() - interval '210 days' - interval '30 minutes'),
    (120, 'D-1035', '1991-12-20', 'M', 58, '220 Ouellette Ave, Windsor, ON', 'O+', 6, now() - interval '145 days' - interval '30 minutes'),
    (121, 'D-1036', '1984-08-08', 'F', 71, '221 Ouellette Ave, Windsor, ON', 'AB-', 6, now() - interval '260 days' - interval '30 minutes'),
    (122, 'D-1037', '1980-02-28', 'M', 84, '222 Ouellette Ave, Windsor, ON', 'A+', 5, now() - interval '300 days' - interval '30 minutes'),
    (123, 'D-1038', '2000-05-17', 'F', 97, '223 Ouellette Ave, Windsor, ON', 'O+', 6, now() - interval '90 days' - interval '30 minutes'),
    (124, 'D-1039', '1989-10-10', 'M', 70, '224 Ouellette Ave, Windsor, ON', 'B-', 6, now() - interval '185 days' - interval '30 minutes'),
    (125, 'D-1040', '1973-07-31', 'M', 83, '225 Ouellette Ave, Windsor, ON', 'O+', 8, now() - interval '270 days' - interval '30 minutes'),
    (126, 'D-1041', '1997-11-14', 'F', 96, '226 Ouellette Ave, Windsor, ON', 'A+', 7, now() - interval '200 days' - interval '30 minutes'),
    (127, 'D-1042', '1982-03-03', 'M', 69, '227 Ouellette Ave, Windsor, ON', 'B+', 4, now() - interval '300 days' - interval '30 minutes'),
    (128, 'D-1043', '1990-06-06', 'F', 82, '228 Ouellette Ave, Windsor, ON', 'O+', 7, now() - interval '120 days' - interval '30 minutes'),
    (129, 'D-1044', '1994-12-01', 'M', 95, '229 Ouellette Ave, Windsor, ON', 'A+', 8, now() - interval '200 days' - interval '30 minutes'),
    (130, 'D-1045', '1986-01-23', 'F', 68, '230 Ouellette Ave, Windsor, ON', 'O+', 7, now() - interval '150 days' - interval '30 minutes'),
    (131, 'D-1046', '1978-09-09', 'M', 81, '231 Ouellette Ave, Windsor, ON', 'AB+', 8, now() - interval '80 days' - interval '30 minutes'),
    (132, 'D-1047', '1992-02-14', 'F', 94, '232 Ouellette Ave, Windsor, ON', 'O-', 7, now() - interval '170 days' - interval '30 minutes'),
    -- lapsed regulars (Q11): gave three or four times, then stopped coming about six months ago
    (137, 'D-1052', '1984-05-09', 'F', 66, '237 Ouellette Ave, Windsor, ON', 'O-', 7, now() - interval '320 days' - interval '30 minutes'),
    (138, 'D-1053', '1976-10-21', 'M', 92, '238 Ouellette Ave, Windsor, ON', 'O+', 4, now() - interval '320 days' - interval '30 minutes'),
    (139, 'D-1054', '1991-03-02', 'F', 63, '239 Ouellette Ave, Windsor, ON', 'B-', 8, now() - interval '310 days' - interval '30 minutes'),
    (140, 'D-1055', '1982-08-17', 'M', 80, '240 Ouellette Ave, Windsor, ON', 'AB+', 7, now() - interval '300 days' - interval '30 minutes'),
    (141, 'D-1056', '1972-12-05', 'M', 78, '241 Ouellette Ave, Windsor, ON', 'O-', 5, now() - interval '315 days' - interval '30 minutes'),
    (142, 'D-1057', '1989-06-28', 'F', 70, '242 Ouellette Ave, Windsor, ON', 'A-', 3, now() - interval '300 days' - interval '30 minutes'),
    -- platelet apheresis donor at Riverside's donor clinic
    (143, 'D-1058', '1987-01-19', 'M', 86, '243 Ouellette Ave, Windsor, ON', 'A+', 4, now() - interval '280 days' - interval '30 minutes'),
    -- HIV-positive donation (permanent deferral) and a pregnancy deferral
    (144, 'D-1059', '1993-09-14', 'M', 79, '244 Ouellette Ave, Windsor, ON', 'A+', 6, now() - interval '120 days' - interval '30 minutes'),
    (145, 'D-1060', '1995-11-08', 'F', 67, '245 Ouellette Ave, Windsor, ON', 'O+', 3, now() - interval '200 days' - interval '30 minutes'),
    -- registered but never donated: a new 18-year-old, under 50 kg, group unknown, brand new (an under-17 sign-up is
    -- refused by register_donor, UC-01 2b, so it has no row here)
    (133, 'D-1048', (CURRENT_DATE - interval '18 years 2 months')::date, 'F', 55, '233 Ouellette Ave, Windsor, ON', 'A+', NULL, NULL),
    (134, 'D-1049', '2001-04-12', 'M', 47.5, '234 Ouellette Ave, Windsor, ON', 'O+', NULL, NULL),
    (135, 'D-1050', '1999-08-30', 'F', 61, '235 Ouellette Ave, Windsor, ON', NULL, NULL, NULL),
    (136, 'D-1051', '2003-02-02', 'M', 74, '236 Ouellette Ave, Windsor, ON', 'B+', NULL, NULL);
-- O-negative regulars who last gave four to eight months ago: with the three above, the 14 O- donors Maya can call in
-- Scenario 1 (FR-25). Verified by the staff member who took their donation (FR-09).
INSERT INTO donor (user_id, donor_code, date_of_birth, sex, weight_kg, address, blood_group, verified_by, verified_at)
SELECT uid, 'D-' || (1061 + uid - 146), dob::date, sex, kg, (201 + uid - 100) || ' Ouellette Ave, Windsor, ON', 'O-', staff,
       now() - make_interval(days => days_ago) - interval '30 minutes'
FROM (VALUES (146, '1986-02-11', 'M', 82, 4, 180), (147, '1990-07-04', 'F', 63, 7, 230), (148, '1979-10-19', 'M', 88, 5, 150),
             (149, '1994-03-27', 'F', 58, 8, 210), (150, '1988-12-02', 'M', 76, 3, 120), (151, '1996-05-15', 'F', 66, 6, 195),
             (152, '1983-09-08', 'M', 93, 7, 240), (153, '1991-01-30', 'F', 70, 4, 165), (154, '1975-06-21', 'M', 85, 5, 135),
             (155, '1987-11-12', 'F', 61, 8, 250), (156, '1999-04-03', 'M', 79, 3, 105)) AS v(uid, dob, sex, kg, staff, days_ago);
-- Scenario 2: Priya is 19 and self-reported B-positive (unverified until her first donation). Scenario 3: Marcus is a regular
-- O-positive donor at Lakeshore. Unknown groups stay NULL and unverified, so those donors are never called (FR-25).
INSERT INTO donor (user_id, donor_code, date_of_birth, sex, weight_kg, address, blood_group, verified_by, verified_at) VALUES
    (157, 'D-1072', (CURRENT_DATE - interval '19 years 3 months')::date, 'F', 58, '18 Sunset Ave, Windsor, ON', 'B+', NULL, NULL),
    (158, 'D-1073', '1988-04-22', 'M', 84, '57 Lakeshore Blvd, Windsor, ON', 'O+', 7, now() - interval '290 days' - interval '30 minutes'),
    (159, 'D-1074', '1993-08-09', 'M', 80, '920 Erie St E, Windsor, ON', 'O-', 4, now() - interval '18 days' - interval '30 minutes'),
    (160, 'D-1075', '1998-02-17', 'F', 62, '41 Tecumseh Rd W, Windsor, ON', NULL, NULL, NULL),
    (161, 'D-1076', '1985-11-30', 'M', 88, '1500 Howard Ave, Windsor, ON', NULL, NULL, NULL),
    (162, 'D-1077', '2004-06-05', 'M', 71, '303 Pelissier St, Windsor, ON', NULL, NULL, NULL),
    (163, 'D-1078', '1990-01-12', 'M', 83, '77 Giles Blvd E, Windsor, ON', 'A+', 4, now() - interval '12 days' - interval '30 minutes'),
    (164, 'D-1079', '1994-05-03', 'F', 66, '12 Main St, Tecumseh, ON', 'O+', 6, now() - interval '9 days' - interval '30 minutes'),
    (165, 'D-1080', '1987-09-14', 'M', 90, '88 Rourke Line, Belle River, ON', 'O+', 10, now() - interval '16 days' - interval '30 minutes'),
    (166, 'D-1081', '1982-03-27', 'M', 76, '9 Notre Dame St, Belle River, ON', 'A+', 10, now() - interval '21 days' - interval '30 minutes'),
    (167, 'D-1082', '1997-12-08', 'F', 61, '640 Lesperance Rd, Tecumseh, ON', 'O+', 5, now() - interval '6 days' - interval '30 minutes'),
    (168, 'D-1083', '1991-07-19', 'M', 85, '2050 Wyandotte St W, Windsor, ON', 'B+', 4, now() - interval '11 days' - interval '30 minutes'),
    (169, 'D-1084', '1989-10-25', 'F', 64, '415 Manning Rd, Tecumseh, ON', 'A+', 6, now() - interval '27 days' - interval '30 minutes');

-- ---------------------------------------------------------------- screening before every clinic visit (FR-06)
INSERT INTO screening (screening_id, donor_id, completed_at) VALUES
    (1, 105, now() - interval '330 days' - interval '20 minutes'),
    (2, 118, now() - interval '320 days' - interval '20 minutes'),
    (3, 111, now() - interval '310 days' - interval '20 minutes'),
    (4, 103, now() - interval '300 days' - interval '20 minutes'),
    (5, 122, now() - interval '300 days' - interval '20 minutes'),
    (6, 113, now() - interval '295 days' - interval '20 minutes'),
    (7, 101, now() - interval '290 days' - interval '20 minutes'),
    (8, 102, now() - interval '280 days' - interval '20 minutes'),
    (9, 115, now() - interval '280 days' - interval '20 minutes'),
    (10, 125, now() - interval '270 days' - interval '20 minutes'),
    (11, 105, now() - interval '260 days' - interval '20 minutes'),
    (12, 114, now() - interval '260 days' - interval '20 minutes'),
    (13, 121, now() - interval '260 days' - interval '20 minutes'),
    (14, 104, now() - interval '250 days' - interval '20 minutes'),
    (15, 118, now() - interval '250 days' - interval '20 minutes'),
    (16, 107, now() - interval '240 days' - interval '20 minutes'),
    (17, 127, now() - interval '240 days' - interval '20 minutes'),
    (18, 103, now() - interval '230 days' - interval '20 minutes'),
    (19, 116, now() - interval '230 days' - interval '20 minutes'),
    (20, 122, now() - interval '225 days' - interval '20 minutes'),
    (21, 113, now() - interval '220 days' - interval '20 minutes'),
    (22, 111, now() - interval '215 days' - interval '20 minutes'),
    (23, 101, now() - interval '210 days' - interval '20 minutes'),
    (24, 119, now() - interval '210 days' - interval '20 minutes'),
    (25, 106, now() - interval '200 days' - interval '20 minutes'),
    (26, 115, now() - interval '200 days' - interval '20 minutes'),
    (27, 126, now() - interval '200 days' - interval '20 minutes'),
    (28, 129, now() - interval '200 days' - interval '20 minutes'),
    (29, 112, now() - interval '190 days' - interval '20 minutes'),
    (30, 124, now() - interval '185 days' - interval '20 minutes'),
    (31, 102, now() - interval '180 days' - interval '20 minutes'),
    (32, 123, now() - interval '180 days' - interval '20 minutes'),
    (33, 125, now() - interval '180 days' - interval '20 minutes'),
    (34, 117, now() - interval '175 days' - interval '20 minutes'),
    (35, 132, now() - interval '170 days' - interval '20 minutes'),
    (36, 103, now() - interval '160 days' - interval '20 minutes'),
    (37, 114, now() - interval '160 days' - interval '20 minutes'),
    (38, 127, now() - interval '180 days' - interval '20 minutes'),
    (39, 109, now() - interval '150 days' - interval '20 minutes'),
    (40, 113, now() - interval '150 days' - interval '20 minutes'),
    (41, 130, now() - interval '150 days' - interval '20 minutes'),
    (42, 120, now() - interval '145 days' - interval '20 minutes'),
    (43, 101, now() - interval '140 days' - interval '20 minutes'),
    (44, 107, now() - interval '120 days' - interval '20 minutes'),
    (45, 116, now() - interval '120 days' - interval '20 minutes'),
    (46, 128, now() - interval '120 days' - interval '20 minutes'),
    (47, 122, now() - interval '110 days' - interval '20 minutes'),
    (48, 126, now() - interval '105 days' - interval '20 minutes'),
    (49, 104, now() - interval '100 days' - interval '20 minutes'),
    (50, 112, now() - interval '100 days' - interval '20 minutes'),
    (51, 125, now() - interval '100 days' - interval '20 minutes'),
    (52, 117, now() - interval '95 days' - interval '20 minutes'),
    (53, 102, now() - interval '90 days' - interval '20 minutes'),
    (54, 123, now() - interval '90 days' - interval '20 minutes'),
    (55, 131, now() - interval '80 days' - interval '20 minutes'),
    (56, 120, now() - interval '75 days' - interval '20 minutes'),
    (57, 101, now() - interval '70 days' - interval '20 minutes'),
    (58, 108, now() - interval '60 days' - interval '20 minutes'),
    (59, 113, now() - interval '60 days' - interval '20 minutes'),
    (60, 132, now() - interval '60 days' - interval '20 minutes'),
    (61, 114, now() - interval '50 days' - interval '20 minutes'),
    (62, 108, now() - interval '45 days' - interval '20 minutes'),
    (63, 119, now() - interval '40 days' - interval '20 minutes'),
    (64, 122, now() - interval '35 days' - interval '20 minutes'),
    (65, 107, now() - interval '30 days' - interval '20 minutes'),
    (66, 108, now() - interval '30 days' - interval '20 minutes'),
    (67, 129, now() - interval '30 days' - interval '20 minutes'),
    (68, 116, now() - interval '28 days' - interval '20 minutes'),
    (69, 103, now() - interval '25 days' - interval '20 minutes'),
    (70, 125, now() - interval '22 days' - interval '20 minutes'),
    (71, 104, now() - interval '20 days' - interval '20 minutes'),
    (72, 110, now() - interval '20 days' - interval '20 minutes'),
    (73, 124, now() - interval '18 days' - interval '20 minutes'),
    (74, 106, now() - interval '15 days' - interval '20 minutes'),
    (75, 128, now() - interval '14 days' - interval '20 minutes'),
    (76, 120, now() - interval '12 days' - interval '20 minutes'),
    (77, 117, now() - interval '10 days' - interval '20 minutes'),
    (78, 112, now() - interval '8 days' - interval '20 minutes'),
    (79, 127, now() - interval '6 days' - interval '20 minutes'),
    (80, 110, now() - interval '4 days' - interval '20 minutes');
-- visits by the lapsed regulars, the platelet donor and the two deferred donors (donor, days ago)
INSERT INTO screening (screening_id, donor_id, completed_at)
SELECT 80 + row_number() OVER (ORDER BY ord), donor, now() - make_interval(days => days_ago) - interval '20 minutes'
FROM (VALUES (1, 137, 320), (2, 137, 290), (3, 137, 255), (4, 137, 215),
             (5, 138, 320), (6, 138, 255), (7, 138, 195),
             (8, 139, 310), (9, 139, 275), (10, 139, 240),
             (11, 140, 300), (12, 140, 265), (13, 140, 230), (14, 140, 190),
             (15, 141, 315), (16, 141, 250), (17, 141, 190),
             (18, 142, 300), (19, 142, 210),
             (20, 143, 280), (21, 143, 220), (22, 143, 160), (23, 143, 100), (24, 143, 40),
             (25, 144, 120), (26, 145, 200), (27, 145, 45)) AS v(ord, donor, days_ago);
INSERT INTO screening (screening_id, donor_id, completed_at)
SELECT 107 + uid - 145, uid, now() - make_interval(days => days_ago) - interval '20 minutes'
FROM (VALUES (146, 180), (147, 230), (148, 150), (149, 210), (150, 120), (151, 195), (152, 240), (153, 165), (154, 135),
             (155, 250), (156, 105)) AS v(uid, days_ago);
-- D-1042's other visits, Marcus's regular visits, the donors added above, and Priya's first questionnaire (Scenario 2)
INSERT INTO screening (screening_id, donor_id, completed_at)
SELECT 118 + row_number() OVER (ORDER BY ord), donor, now() - make_interval(days => days_ago) - interval '20 minutes'
FROM (VALUES (1, 127, 300), (2, 127, 120), (3, 127, 63),
             (4, 158, 290), (5, 158, 225), (6, 158, 160), (7, 158, 95), (8, 158, 1),
             (9, 159, 18), (10, 105, 13), (11, 111, 13), (12, 126, 12), (13, 163, 12),
             (14, 164, 9), (15, 165, 16), (16, 166, 21), (17, 167, 6), (18, 168, 11), (19, 169, 27),
             (20, 157, 31)) AS v(ord, donor, days_ago);

-- every visit answers all six questions; the answers below are the 'clean' ones (no deferral)
INSERT INTO screening_answer (screening_id, question_id, answer)
SELECT s.screening_id, q.question_id, coalesce(q.reason_code = 'UNWELL', FALSE)   -- 'feeling well today?' = yes, the rest = no
FROM screening s CROSS JOIN screening_question q;

-- ---------------------------------------------------------------- donations (FR-11), oldest first
INSERT INTO donation (donation_id, donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl) VALUES
    (1, 105, 4, 'WB', now() - interval '330 days', 450, 145),
    (2, 118, 5, 'WB', now() - interval '320 days', 450, 151),
    (3, 111, 4, 'WB', now() - interval '310 days', 450, 139),
    (4, 103, 4, 'WB', now() - interval '300 days', 450, 146),
    (5, 122, 5, 'WB', now() - interval '300 days', 450, 159),
    (115, 127, 4, 'WB', now() - interval '300 days', 450, 150),
    (6, 113, 5, 'WB', now() - interval '295 days', 450, 151),
    (7, 101, 4, 'WB', now() - interval '290 days', 450, 152),
    (8, 102, 3, 'WB', now() - interval '280 days', 450, 136),
    (9, 115, 6, 'WB', now() - interval '280 days', 450, 150),
    (10, 125, 8, 'WB', now() - interval '270 days', 450, 150),
    (11, 105, 4, 'WB', now() - interval '260 days', 450, 150),
    (12, 114, 5, 'WB', now() - interval '260 days', 450, 140),
    (13, 121, 6, 'WB', now() - interval '260 days', 450, 144),
    (14, 104, 3, 'WB', now() - interval '250 days', 450, 135),
    (15, 118, 5, 'WB', now() - interval '250 days', 450, 156),
    (16, 107, 4, 'WB', now() - interval '240 days', 450, 159),
    (17, 127, 4, 'WB', now() - interval '240 days', 450, 149),
    (18, 103, 4, 'WB', now() - interval '230 days', 450, 151),
    (19, 116, 5, 'WB', now() - interval '230 days', 450, 139),
    (20, 122, 6, 'WB', now() - interval '225 days', 450, 159),
    (21, 113, 6, 'WB', now() - interval '220 days', 450, 151),
    (22, 111, 3, 'WB', now() - interval '215 days', 450, 134),
    (23, 101, 4, 'WB', now() - interval '210 days', 450, 147),
    (24, 119, 6, 'WB', now() - interval '210 days', 450, 140),
    (25, 106, 3, 'WB', now() - interval '200 days', 450, 144),
    (26, 115, 6, 'WB', now() - interval '200 days', 450, 145),
    (27, 126, 7, 'WB', now() - interval '200 days', 450, 134),
    (28, 129, 8, 'WB', now() - interval '200 days', 450, 153),
    (29, 112, 3, 'WB', now() - interval '190 days', 450, 159),
    (30, 124, 6, 'WB', now() - interval '185 days', 450, 148),
    (31, 102, 3, 'WB', now() - interval '180 days', 450, 141),
    (37, 127, 4, 'WB', now() - interval '180 days', 450, 159),
    (32, 125, 8, 'WB', now() - interval '180 days', 450, 150),
    (33, 117, 5, 'WB', now() - interval '175 days', 450, 149),
    (34, 132, 7, 'WB', now() - interval '170 days', 450, 146),
    (35, 103, 4, 'WB', now() - interval '160 days', 450, 156),
    (36, 114, 5, 'WB', now() - interval '160 days', 450, 145),
    (38, 109, 4, 'WB', now() - interval '150 days', 450, 145),
    (39, 113, 6, 'WB', now() - interval '150 days', 450, 156),
    (40, 130, 7, 'WB', now() - interval '150 days', 450, 142),
    (41, 120, 6, 'WB', now() - interval '145 days', 450, 155),
    (42, 101, 4, 'WB', now() - interval '140 days', 450, 152),
    (43, 107, 4, 'WB', now() - interval '120 days', 450, 159),
    (116, 127, 4, 'WB', now() - interval '120 days', 450, 151),
    (44, 116, 5, 'WB', now() - interval '120 days', 450, 134),
    (45, 128, 7, 'WB', now() - interval '120 days', 450, 143),
    (46, 122, 5, 'WB', now() - interval '110 days', 450, 149),
    (47, 126, 8, 'WB', now() - interval '105 days', 450, 144),
    (48, 112, 3, 'WB', now() - interval '100 days', 450, 159),
    (49, 125, 8, 'WB', now() - interval '100 days', 450, 145),
    (50, 117, 5, 'WB', now() - interval '95 days', 450, 159),
    (51, 102, 3, 'WB', now() - interval '90 days', 450, 141),
    (52, 123, 6, 'WB', now() - interval '90 days', 450, 138),
    (53, 131, 8, 'WB', now() - interval '80 days', 450, 152),
    (54, 120, 6, 'WB', now() - interval '75 days', 450, 145),
    (55, 101, 4, 'WB', now() - interval '70 days', 450, 157),
    (117, 127, 4, 'WB', now() - interval '63 days', 450, 153),
    (56, 108, 3, 'PLS', now() - interval '60 days', 600, 151),
    (57, 113, 6, 'WB', now() - interval '60 days', 450, 156),
    (58, 132, 7, 'WB', now() - interval '60 days', 450, 141),
    (59, 114, 5, 'WB', now() - interval '50 days', 450, 140),
    (60, 108, 4, 'PLS', now() - interval '45 days', 600, 151),
    (61, 119, 6, 'WB', now() - interval '40 days', 450, 135),
    (62, 122, 6, 'WB', now() - interval '35 days', 450, 149),
    (63, 107, 4, 'WB', now() - interval '30 days', 450, 159),
    (64, 108, 3, 'PLS', now() - interval '30 days', 600, 151),
    (65, 116, 5, 'WB', now() - interval '28 days', 450, 132),
    (66, 103, 3, 'WB', now() - interval '25 days', 450, 156),
    (67, 125, 8, 'WB', now() - interval '22 days', 450, 157),
    (68, 104, 3, 'WB', now() - interval '20 days', 450, 145),
    (69, 110, 3, 'PLT', now() - interval '20 days', 600, 155),
    (70, 124, 5, 'WB', now() - interval '18 days', 450, 146),
    (71, 106, 4, 'WB', now() - interval '15 days', 450, 139),
    (72, 128, 7, 'WB', now() - interval '14 days', 450, 142),
    (73, 120, 5, 'WB', now() - interval '12 days', 450, 157),
    (74, 117, 6, 'WB', now() - interval '10 days', 450, 149),
    (75, 112, 3, 'WB', now() - interval '8 days', 450, 157),
    (76, 127, 4, 'WB', now() - interval '6 days', 450, 155),
    (77, 110, 3, 'PLT', now() - interval '4 days', 600, 154);
-- (donation, donor, staff, type, days ago, ml, Hb); plasma donors give at Lakeshore, platelets are collected at Riverside
INSERT INTO donation (donation_id, donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl)
SELECT id, donor, staff, typ, now() - make_interval(days => days_ago), ml, hb
FROM (VALUES (78, 137, 7, 'PLS', 320, 600, 133), (79, 137, 7, 'PLS', 290, 600, 131), (80, 137, 8, 'PLS', 255, 600, 134),
             (81, 137, 7, 'PLS', 215, 600, 132),
             (82, 138, 4, 'WB', 320, 450, 152), (83, 138, 4, 'WB', 255, 450, 149), (84, 138, 3, 'WB', 195, 450, 151),
             (85, 139, 8, 'PLS', 310, 600, 129), (86, 139, 8, 'PLS', 275, 600, 131), (87, 139, 7, 'PLS', 240, 600, 128),
             (88, 140, 7, 'PLS', 300, 600, 148), (89, 140, 7, 'PLS', 265, 600, 150), (90, 140, 8, 'PLS', 230, 600, 147),
             (91, 140, 7, 'PLS', 190, 600, 149),
             (92, 141, 5, 'WB', 315, 450, 144), (93, 141, 6, 'WB', 250, 450, 146), (94, 141, 5, 'WB', 190, 450, 143),
             (95, 142, 3, 'WB', 300, 450, 136), (96, 142, 3, 'WB', 210, 450, 134),
             (97, 143, 4, 'PLT', 280, 600, 153), (98, 143, 4, 'PLT', 220, 600, 151), (99, 143, 4, 'PLT', 160, 600, 154),
             (100, 143, 4, 'PLT', 100, 600, 152), (101, 143, 4, 'PLT', 40, 600, 150),
             (102, 144, 6, 'WB', 120, 450, 147),
             (103, 145, 3, 'WB', 200, 450, 131)) AS v(id, donor, staff, typ, days_ago, ml, hb);
INSERT INTO donation (donation_id, donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl)
SELECT 104 + uid - 146, uid, staff, 'WB', now() - make_interval(days => days_ago), 450, hb
FROM (VALUES (146, 4, 180, 151), (147, 7, 230, 133), (148, 5, 150, 148), (149, 8, 210, 129), (150, 3, 120, 155),
             (151, 6, 195, 136), (152, 7, 240, 157), (153, 4, 165, 131), (154, 5, 135, 146), (155, 8, 250, 134),
             (156, 3, 105, 150)) AS v(uid, staff, days_ago, hb);
-- (donation, donor, staff, days ago, Hb): Marcus's regular whole-blood visits at Lakeshore, the last one yesterday at
-- 142 g/L (Scenario 3); Scenario 4's four A-positive donations at Riverside; and recent donations at every site
INSERT INTO donation (donation_id, donor_id, recorded_by, donation_type_code, donated_at, volume_ml, hemoglobin_gl)
SELECT id, donor, staff, 'WB', now() - make_interval(days => days_ago), 450, hb
FROM (VALUES (118, 158, 7, 290, 148), (119, 158, 8, 225, 150), (120, 158, 7, 160, 147), (121, 158, 8, 95, 149),
             (122, 158, 7, 1, 142),
             (123, 159, 4, 18, 152),
             (124, 105, 4, 13, 148), (125, 111, 4, 13, 138), (126, 126, 4, 12, 136), (127, 163, 4, 12, 150),
             (128, 164, 6, 9, 137), (129, 165, 10, 16, 151), (130, 166, 10, 21, 147), (131, 167, 5, 6, 133),
             (132, 168, 4, 11, 155), (133, 169, 6, 27, 131)) AS v(id, donor, staff, days_ago, hb);

-- four visits ended in a deferral from the questionnaire (TRAVEL, UNWELL, TATTOO, PREG); recorded after the donation batch
-- because a still-active deferral would otherwise block that donor's older donations in the same INSERT
UPDATE screening_answer SET answer = TRUE WHERE screening_id = 32 AND question_id = 2;
UPDATE screening_answer SET answer = FALSE WHERE screening_id = 49 AND question_id = 3;
UPDATE screening_answer SET answer = TRUE WHERE screening_id = 67 AND question_id = 1;
UPDATE screening_answer SET answer = TRUE WHERE screening_id = 107 AND question_id = 6;
UPDATE screening_answer SET answer = TRUE WHERE question_id = 1 AND screening_id = (SELECT screening_id FROM screening WHERE donor_id = 157);
INSERT INTO deferral (donor_id, reason_code, start_date, end_date, screening_id) VALUES
    (123, 'TRAVEL', (now() - interval '180 days')::date, (now() - interval '180 days')::date + 90, 32),
    (104, 'UNWELL', (now() - interval '100 days')::date, (now() - interval '100 days')::date + 1, 49),
    (129, 'TATTOO', (now() - interval '30 days')::date, (now() - interval '30 days')::date + 90, 67),
    (145, 'PREG', (now() - interval '45 days')::date, (now() - interval '45 days')::date + 180, 107);
-- Scenario 2: Priya's tattoo was six weeks old when she answered, so she is deferred until three months after it
INSERT INTO deferral (donor_id, reason_code, start_date, end_date, screening_id)
SELECT 157, 'TATTOO', completed_at::date, completed_at::date + 90, screening_id FROM screening WHERE donor_id = 157;

-- ---------------------------------------------------------------- component units (FR-12); expiry and location are set by trigger
INSERT INTO blood_unit (unit_id, donation_id, component_code) VALUES
    ('U1-RBC', 1, 'RBC'), ('U1-PLASMA', 1, 'PLASMA'), ('U2-RBC', 2, 'RBC'),
    ('U2-PLASMA', 2, 'PLASMA'), ('U3-RBC', 3, 'RBC'), ('U3-PLASMA', 3, 'PLASMA'),
    ('U4-RBC', 4, 'RBC'), ('U4-PLASMA', 4, 'PLASMA'), ('U5-RBC', 5, 'RBC'),
    ('U5-PLASMA', 5, 'PLASMA'), ('U6-RBC', 6, 'RBC'), ('U6-PLASMA', 6, 'PLASMA'),
    ('U7-RBC', 7, 'RBC'), ('U7-PLASMA', 7, 'PLASMA'), ('U8-RBC', 8, 'RBC'),
    ('U8-PLASMA', 8, 'PLASMA'), ('U9-RBC', 9, 'RBC'), ('U9-PLASMA', 9, 'PLASMA'),
    ('U10-RBC', 10, 'RBC'), ('U10-PLASMA', 10, 'PLASMA'), ('U11-RBC', 11, 'RBC'),
    ('U11-PLASMA', 11, 'PLASMA'), ('U12-RBC', 12, 'RBC'), ('U12-PLASMA', 12, 'PLASMA'),
    ('U13-RBC', 13, 'RBC'), ('U13-PLASMA', 13, 'PLASMA'), ('U14-RBC', 14, 'RBC'),
    ('U14-PLASMA', 14, 'PLASMA'), ('U15-RBC', 15, 'RBC'), ('U15-PLASMA', 15, 'PLASMA'),
    ('U16-RBC', 16, 'RBC'), ('U16-PLASMA', 16, 'PLASMA'), ('U17-RBC', 17, 'RBC'),
    ('U17-PLASMA', 17, 'PLASMA'), ('U18-RBC', 18, 'RBC'), ('U18-PLASMA', 18, 'PLASMA'),
    ('U19-RBC', 19, 'RBC'), ('U19-PLASMA', 19, 'PLASMA'), ('U20-RBC', 20, 'RBC'),
    ('U20-PLASMA', 20, 'PLASMA'), ('U21-RBC', 21, 'RBC'), ('U21-PLASMA', 21, 'PLASMA'),
    ('U22-RBC', 22, 'RBC'), ('U22-PLASMA', 22, 'PLASMA'), ('U23-RBC', 23, 'RBC'),
    ('U23-PLASMA', 23, 'PLASMA'), ('U24-RBC', 24, 'RBC'), ('U24-PLASMA', 24, 'PLASMA'),
    ('U25-RBC', 25, 'RBC'), ('U25-PLASMA', 25, 'PLASMA'), ('U26-RBC', 26, 'RBC'),
    ('U26-PLASMA', 26, 'PLASMA'), ('U27-RBC', 27, 'RBC'), ('U27-PLASMA', 27, 'PLASMA'),
    ('U28-RBC', 28, 'RBC'), ('U28-PLASMA', 28, 'PLASMA'), ('U29-RBC', 29, 'RBC'),
    ('U29-PLASMA', 29, 'PLASMA'), ('U30-RBC', 30, 'RBC'), ('U30-PLASMA', 30, 'PLASMA'),
    ('U31-RBC', 31, 'RBC'), ('U31-PLASMA', 31, 'PLASMA'), ('U32-RBC', 32, 'RBC'),
    ('U32-PLASMA', 32, 'PLASMA'), ('U33-RBC', 33, 'RBC'), ('U33-PLASMA', 33, 'PLASMA'),
    ('U34-RBC', 34, 'RBC'), ('U34-PLASMA', 34, 'PLASMA'), ('U35-RBC', 35, 'RBC'),
    ('U35-PLASMA', 35, 'PLASMA'), ('U36-RBC', 36, 'RBC'), ('U36-PLASMA', 36, 'PLASMA'),
    ('U37-RBC', 37, 'RBC'), ('U37-PLASMA', 37, 'PLASMA'), ('U38-RBC', 38, 'RBC'),
    ('U38-PLASMA', 38, 'PLASMA'), ('U39-RBC', 39, 'RBC'), ('U39-PLASMA', 39, 'PLASMA'),
    ('U40-RBC', 40, 'RBC'), ('U40-PLASMA', 40, 'PLASMA'), ('U41-RBC', 41, 'RBC'),
    ('U41-PLASMA', 41, 'PLASMA'), ('U42-RBC', 42, 'RBC'), ('U42-PLASMA', 42, 'PLASMA'),
    ('U42-PLT', 42, 'PLT'), ('U43-RBC', 43, 'RBC'), ('U43-PLASMA', 43, 'PLASMA'),
    ('U44-RBC', 44, 'RBC'), ('U44-PLASMA', 44, 'PLASMA'), ('U45-RBC', 45, 'RBC'),
    ('U45-PLASMA', 45, 'PLASMA'), ('U46-RBC', 46, 'RBC'), ('U46-PLASMA', 46, 'PLASMA'),
    ('U47-RBC', 47, 'RBC'), ('U47-PLASMA', 47, 'PLASMA'), ('U48-RBC', 48, 'RBC'),
    ('U48-PLASMA', 48, 'PLASMA'), ('U49-RBC', 49, 'RBC'), ('U49-PLASMA', 49, 'PLASMA'),
    ('U50-RBC', 50, 'RBC'), ('U50-PLASMA', 50, 'PLASMA'), ('U51-RBC', 51, 'RBC'),
    ('U51-PLASMA', 51, 'PLASMA'), ('U52-RBC', 52, 'RBC'), ('U52-PLASMA', 52, 'PLASMA'),
    ('U53-RBC', 53, 'RBC'), ('U53-PLASMA', 53, 'PLASMA'), ('U54-RBC', 54, 'RBC'),
    ('U54-PLASMA', 54, 'PLASMA'), ('U55-RBC', 55, 'RBC'), ('U55-PLASMA', 55, 'PLASMA'),
    ('U56-PLASMA', 56, 'PLASMA'), ('U57-RBC', 57, 'RBC'), ('U57-PLASMA', 57, 'PLASMA'),
    ('U57-PLT', 57, 'PLT'), ('U58-RBC', 58, 'RBC'), ('U58-PLASMA', 58, 'PLASMA'),
    ('U59-RBC', 59, 'RBC'), ('U59-PLASMA', 59, 'PLASMA'), ('U60-PLASMA', 60, 'PLASMA'),
    ('U61-RBC', 61, 'RBC'), ('U61-PLASMA', 61, 'PLASMA'), ('U62-RBC', 62, 'RBC'),
    ('U62-PLASMA', 62, 'PLASMA'), ('U63-RBC', 63, 'RBC'), ('U63-PLASMA', 63, 'PLASMA'),
    ('U64-PLASMA', 64, 'PLASMA'), ('U65-RBC', 65, 'RBC'), ('U65-PLASMA', 65, 'PLASMA'),
    ('U66-RBC', 66, 'RBC'), ('U66-PLASMA', 66, 'PLASMA'), ('U67-RBC', 67, 'RBC'),
    ('U67-PLASMA', 67, 'PLASMA'), ('U68-RBC', 68, 'RBC'), ('U68-PLASMA', 68, 'PLASMA'),
    ('U68-PLT', 68, 'PLT'), ('U69-PLT', 69, 'PLT'), ('U70-RBC', 70, 'RBC'),
    ('U70-PLASMA', 70, 'PLASMA'), ('U71-RBC', 71, 'RBC'), ('U71-PLASMA', 71, 'PLASMA'),
    ('U72-RBC', 72, 'RBC'), ('U72-PLASMA', 72, 'PLASMA'), ('U73-RBC', 73, 'RBC'),
    ('U73-PLASMA', 73, 'PLASMA'), ('U74-RBC', 74, 'RBC'), ('U74-PLASMA', 74, 'PLASMA'),
    ('U74-PLT', 74, 'PLT'), ('U75-RBC', 75, 'RBC'), ('U75-PLASMA', 75, 'PLASMA'),
    ('U76-RBC', 76, 'RBC'), ('U76-PLASMA', 76, 'PLASMA'), ('U77-PLT', 77, 'PLT');
INSERT INTO blood_unit (unit_id, donation_id, component_code)
SELECT format('U%s-%s', d.donation_id, c.code), d.donation_id, c.code
FROM donation d JOIN (VALUES ('WB', 'RBC'), ('WB', 'PLASMA'), ('PLS', 'PLASMA'), ('PLT', 'PLT')) AS c(typ, code)
     ON c.typ = d.donation_type_code
WHERE d.donation_id BETWEEN 78 AND 114;
INSERT INTO blood_unit (unit_id, donation_id, component_code)
SELECT format('U%s-%s', id, c), id, c
FROM (VALUES (115, ARRAY['RBC','PLASMA']), (116, ARRAY['RBC','PLASMA','PLT']), (117, ARRAY['RBC','PLASMA']),
             (118, ARRAY['RBC','PLASMA']), (119, ARRAY['RBC','PLASMA']), (120, ARRAY['RBC','PLASMA']), (121, ARRAY['RBC','PLASMA']),
             (122, ARRAY['RBC','PLASMA','PLT']), (123, ARRAY['RBC','PLASMA']),
             (124, ARRAY['RBC','PLASMA','PLT']), (125, ARRAY['RBC','PLASMA','PLT']), (126, ARRAY['RBC','PLASMA','PLT']),
             (127, ARRAY['RBC','PLASMA','PLT']), (128, ARRAY['RBC','PLASMA']), (129, ARRAY['RBC','PLASMA']),
             (130, ARRAY['RBC','PLASMA']), (131, ARRAY['RBC','PLASMA']), (132, ARRAY['RBC','PLASMA']),
             (133, ARRAY['RBC','PLASMA'])) AS v(id, comps), unnest(comps) AS c;

-- ---------------------------------------------------------------- test results, 12 hours after each donation (FR-13)
INSERT INTO test_result (donation_id, test_code, result, tested_at)
SELECT d.donation_id, t.test_code, 'Negative', d.donated_at + interval '12 hours'
FROM donation d CROSS JOIN test_type t WHERE d.donation_id NOT IN (76, 102, 122);
-- every unit whose tests all passed is released to stock (the status trigger checks the tests); this happens
-- before donor 127's positive result below, as it did in time, so their earlier units were already on the shelf
UPDATE blood_unit SET status = 'Available' WHERE status = 'Collected' AND donation_id NOT IN (76, 102, 122);

-- donor D-1059's (user 144) only donation tests HIV positive: units discarded, donor flagged and permanently deferred (FR-13)
INSERT INTO test_result (donation_id, test_code, result, tested_at)
SELECT 102, test_code, CASE test_code WHEN 'HIV' THEN 'Positive' ELSE 'Negative' END, now() - interval '120 days' + interval '12 hours'
FROM test_type;
INSERT INTO deferral (donor_id, reason_code, start_date, recorded_by) VALUES (144, 'HIV', (now() - interval '119 days')::date, 6);

-- donor D-1042's (user 127) latest donation tests positive for hepatitis B: the trigger discards its units, flags the donor and notifies admins
INSERT INTO test_result (donation_id, test_code, result, tested_at)
SELECT 76, test_code, CASE test_code WHEN 'HBV' THEN 'Positive' ELSE 'Negative' END, now() - interval '5 days 12:00' FROM test_type;
INSERT INTO deferral (donor_id, reason_code, start_date, recorded_by) VALUES (127, 'HBV', (now() - interval '5 days')::date, 3);

-- a staff-recorded deferral: hemoglobin too low at the clinic 10 days ago
INSERT INTO deferral (donor_id, reason_code, start_date, end_date, recorded_by) VALUES (109, 'LOWHB', (now() - interval '10 days')::date, (now() - interval '10 days')::date + 56, 4);


-- ---------------------------------------------------------------- past requests (FR-22..28): created, reserved, issued, closed
INSERT INTO blood_request (request_id, requested_by, patient_ref, recipient_group, component_code, units_requested, urgency,
                           required_by, status, created_at, closed_at) VALUES
    (1, 6, 'P-24037', 'O+', 'RBC', 2, 'Routine', now() - interval '285 days' + interval '48 hours', 'Fulfilled', now() - interval '285 days', now() - interval '285 days' + interval '20 hours'),
    (2, 3, 'P-24074', 'O+', 'RBC', 2, 'Routine', now() - interval '275 days' + interval '48 hours', 'Fulfilled', now() - interval '275 days', now() - interval '275 days' + interval '20 hours'),
    (3, 4, 'P-24111', 'A+', 'RBC', 1, 'Urgent', now() - interval '262 days' + interval '6 hours', 'Fulfilled', now() - interval '262 days', now() - interval '262 days' + interval '4 hours'),
    (4, 5, 'P-24148', 'A+', 'RBC', 1, 'Routine', now() - interval '240 days' + interval '48 hours', 'Fulfilled', now() - interval '240 days', now() - interval '240 days' + interval '20 hours'),
    (5, 4, 'P-24185', 'O-', 'RBC', 1, 'Emergency', now() - interval '225 days' + interval '1 hours', 'Fulfilled', now() - interval '225 days', now() - interval '225 days' + interval '1 hours'),
    (6, 5, 'P-24222', 'O+', 'RBC', 1, 'Urgent', now() - interval '212 days' + interval '6 hours', 'Fulfilled', now() - interval '212 days', now() - interval '212 days' + interval '4 hours'),
    (7, 4, 'P-24259', 'A+', 'RBC', 2, 'Routine', now() - interval '205 days' + interval '48 hours', 'Fulfilled', now() - interval '205 days', now() - interval '205 days' + interval '20 hours'),
    (8, 5, 'P-24296', 'A-', 'RBC', 1, 'Routine', now() - interval '190 days' + interval '48 hours', 'Fulfilled', now() - interval '190 days', now() - interval '190 days' + interval '20 hours'),
    (9, 4, 'P-24333', NULL, 'RBC', 2, 'Emergency', now() - interval '170 days' + interval '1 hours', 'Partially Fulfilled', now() - interval '170 days', now() - interval '170 days' + interval '1 hours'),
    (10, 3, 'P-24370', 'B+', 'PLASMA', 1, 'Routine', now() - interval '150 days' + interval '48 hours', 'Fulfilled', now() - interval '150 days', now() - interval '150 days' + interval '20 hours'),
    (11, 6, 'P-24407', 'O+', 'RBC', 2, 'Routine', now() - interval '140 days' + interval '48 hours', 'Fulfilled', now() - interval '140 days', now() - interval '140 days' + interval '20 hours'),
    (12, 3, 'P-24444', 'O+', 'RBC', 2, 'Urgent', now() - interval '130 days' + interval '6 hours', 'Fulfilled', now() - interval '130 days', now() - interval '130 days' + interval '4 hours'),
    (13, 4, 'P-24481', 'A-', 'RBC', 1, 'Routine', now() - interval '110 days' + interval '48 hours', 'Fulfilled', now() - interval '110 days', now() - interval '110 days' + interval '20 hours'),
    (14, 5, 'P-24518', 'A+', 'RBC', 2, 'Urgent', now() - interval '100 days' + interval '6 hours', 'Fulfilled', now() - interval '100 days', now() - interval '100 days' + interval '4 hours'),
    (15, 4, 'P-24555', 'AB+', 'PLASMA', 2, 'Routine', now() - interval '85 days' + interval '48 hours', 'Partially Fulfilled', now() - interval '85 days', now() - interval '85 days' + interval '20 hours'),
    (16, 5, 'P-24592', 'B+', 'PLASMA', 1, 'Routine', now() - interval '70 days' + interval '48 hours', 'Fulfilled', now() - interval '70 days', now() - interval '70 days' + interval '20 hours'),
    (17, 4, 'P-24629', 'O+', 'RBC', 1, 'Routine', now() - interval '62 days' + interval '48 hours', 'Fulfilled', now() - interval '62 days', now() - interval '62 days' + interval '20 hours'),
    (18, 3, 'P-24666', 'A+', 'RBC', 1, 'Urgent', now() - interval '50 days' + interval '6 hours', 'Fulfilled', now() - interval '50 days', now() - interval '50 days' + interval '4 hours'),
    (19, 6, 'P-24703', 'O+', 'RBC', 1, 'Emergency', now() - interval '45 days' + interval '1 hours', 'Fulfilled', now() - interval '45 days', now() - interval '45 days' + interval '1 hours'),
    (20, 5, 'P-24740', 'A+', 'RBC', 1, 'Routine', now() - interval '30 days' + interval '48 hours', 'Fulfilled', now() - interval '30 days', now() - interval '30 days' + interval '20 hours'),
    (21, 4, 'P-24777', 'O+', 'PLT', 1, 'Urgent', now() - interval '18 days' + interval '6 hours', 'Fulfilled', now() - interval '18 days', now() - interval '18 days' + interval '4 hours'),
    (22, 5, 'P-24814', 'O-', 'RBC', 1, 'Emergency', now() - interval '14 days' + interval '1 hours', 'Partially Fulfilled', now() - interval '14 days', now() - interval '14 days' + interval '1 hours'),
    (27, 3, 'P-24851', 'A+', 'PLT', 1, 'Urgent', now() - interval '278 days' + interval '6 hours', 'Fulfilled', now() - interval '278 days', now() - interval '278 days' + interval '4 hours'),
    (28, 4, 'P-24888', 'O+', 'RBC', 1, 'Routine', now() - interval '254 days' + interval '48 hours', 'Fulfilled', now() - interval '254 days', now() - interval '254 days' + interval '20 hours'),
    (29, 5, 'P-24925', 'O-', 'RBC', 1, 'Emergency', now() - interval '189 days' + interval '1 hours', 'Fulfilled', now() - interval '189 days', now() - interval '189 days' + interval '1 hours'),
    (30, 4, 'P-24962', 'O+', 'PLT', 1, 'Urgent', now() - interval '158 days' + interval '6 hours', 'Fulfilled', now() - interval '158 days', now() - interval '158 days' + interval '4 hours'),
    -- Scenario 4: oncology's Routine request for two A-positive platelet units on "Monday", nine days ago
    (32, 4, 'P-24990', 'A+', 'PLT', 2, 'Routine', now() - interval '9 days' + interval '48 hours', 'Fulfilled', now() - interval '9 days', now() - interval '9 days' + interval '20 hours'),
    -- Scenario 5: six of D-1042's earlier units went to Riverside patients before his positive test
    (33, 3, 'P-24300', 'B+', 'RBC', 1, 'Routine', now() - interval '295 days' + interval '48 hours', 'Fulfilled', now() - interval '295 days', now() - interval '295 days' + interval '20 hours'),
    (34, 4, 'P-24412', 'B+', 'RBC', 1, 'Urgent', now() - interval '235 days' + interval '6 hours', 'Fulfilled', now() - interval '235 days', now() - interval '235 days' + interval '4 hours'),
    (35, 3, 'P-24561', 'B+', 'RBC', 1, 'Routine', now() - interval '176 days' + interval '48 hours', 'Fulfilled', now() - interval '176 days', now() - interval '176 days' + interval '20 hours'),
    (36, 4, 'P-24708', 'B+', 'RBC', 1, 'Routine', now() - interval '115 days' + interval '48 hours', 'Fulfilled', now() - interval '115 days', now() - interval '115 days' + interval '20 hours'),
    (37, 3, 'P-24321', 'B+', 'PLASMA', 1, 'Routine', now() - interval '290 days' + interval '48 hours', 'Fulfilled', now() - interval '290 days', now() - interval '290 days' + interval '20 hours');
INSERT INTO request_unit (request_id, unit_id, reserved_at, issued_at) VALUES
    (1, 'U6-RBC', now() - interval '285 days' + interval '5 minutes', now() - interval '285 days' + interval '20 hours'),
    (1, 'U2-RBC', now() - interval '285 days' + interval '5 minutes', now() - interval '285 days' + interval '20 hours'),
    (2, 'U7-RBC', now() - interval '275 days' + interval '5 minutes', now() - interval '275 days' + interval '20 hours'),
    (2, 'U4-RBC', now() - interval '275 days' + interval '5 minutes', now() - interval '275 days' + interval '20 hours'),
    (3, 'U8-RBC', now() - interval '262 days' + interval '5 minutes', now() - interval '262 days' + interval '4 hours'),
    (4, 'U12-RBC', now() - interval '240 days' + interval '5 minutes', now() - interval '240 days' + interval '20 hours'),
    (5, 'U18-RBC', now() - interval '225 days' + interval '5 minutes', now() - interval '225 days' + interval '1 hours'),
    (6, 'U19-RBC', now() - interval '212 days' + interval '5 minutes', now() - interval '212 days' + interval '4 hours'),
    (7, 'U22-RBC', now() - interval '205 days' + interval '5 minutes', now() - interval '205 days' + interval '20 hours'),
    (7, 'U16-RBC', now() - interval '205 days' + interval '5 minutes', now() - interval '205 days' + interval '20 hours'),
    (8, 'U24-RBC', now() - interval '190 days' + interval '5 minutes', now() - interval '190 days' + interval '20 hours'),
    (9, 'U29-RBC', now() - interval '170 days' + interval '5 minutes', now() - interval '170 days' + interval '1 hours'),
    (10, 'U17-PLASMA', now() - interval '150 days' + interval '5 minutes', now() - interval '150 days' + interval '20 hours'),
    (11, 'U39-RBC', now() - interval '140 days' + interval '5 minutes', now() - interval '140 days' + interval '20 hours'),
    (11, 'U41-RBC', now() - interval '140 days' + interval '5 minutes', now() - interval '140 days' + interval '20 hours'),
    (12, 'U42-RBC', now() - interval '130 days' + interval '5 minutes', now() - interval '130 days' + interval '4 hours'),
    (12, 'U35-RBC', now() - interval '130 days' + interval '5 minutes', now() - interval '130 days' + interval '4 hours'),
    (13, 'U43-RBC', now() - interval '110 days' + interval '5 minutes', now() - interval '110 days' + interval '20 hours'),
    (14, 'U46-RBC', now() - interval '100 days' + interval '5 minutes', now() - interval '100 days' + interval '4 hours'),
    (14, 'U44-RBC', now() - interval '100 days' + interval '5 minutes', now() - interval '100 days' + interval '4 hours'),
    (15, 'U38-PLASMA', now() - interval '85 days' + interval '5 minutes', now() - interval '85 days' + interval '20 hours'),
    (16, 'U9-PLASMA', now() - interval '70 days' + interval '5 minutes', now() - interval '70 days' + interval '20 hours'),
    (17, 'U55-RBC', now() - interval '62 days' + interval '5 minutes', now() - interval '62 days' + interval '20 hours'),
    (18, 'U51-RBC', now() - interval '50 days' + interval '5 minutes', now() - interval '50 days' + interval '4 hours'),
    (19, 'U54-RBC', now() - interval '45 days' + interval '5 minutes', now() - interval '45 days' + interval '1 hours'),
    (20, 'U59-RBC', now() - interval '30 days' + interval '5 minutes', now() - interval '30 days' + interval '20 hours'),
    (21, 'U68-PLT', now() - interval '18 days' + interval '5 minutes', now() - interval '18 days' + interval '4 hours'),
    (27, 'U97-PLT', now() - interval '278 days' + interval '5 minutes', now() - interval '278 days' + interval '4 hours'),
    (28, 'U83-RBC', now() - interval '254 days' + interval '5 minutes', now() - interval '254 days' + interval '20 hours'),
    (29, 'U94-RBC', now() - interval '189 days' + interval '5 minutes', now() - interval '189 days' + interval '1 hours'),
    (30, 'U99-PLT', now() - interval '158 days' + interval '5 minutes', now() - interval '158 days' + interval '4 hours'),
    -- FEFO: the two platelet units collected 13 days ago expire first, so they go; the two from 12 days ago are left over
    (32, 'U124-PLT', now() - interval '9 days' + interval '5 minutes', now() - interval '9 days' + interval '20 hours'),
    (32, 'U125-PLT', now() - interval '9 days' + interval '5 minutes', now() - interval '9 days' + interval '20 hours'),
    (33, 'U115-RBC', now() - interval '295 days' + interval '5 minutes', now() - interval '295 days' + interval '20 hours'),
    (34, 'U17-RBC', now() - interval '235 days' + interval '5 minutes', now() - interval '235 days' + interval '4 hours'),
    (35, 'U37-RBC', now() - interval '176 days' + interval '5 minutes', now() - interval '176 days' + interval '20 hours'),
    (36, 'U116-RBC', now() - interval '115 days' + interval '5 minutes', now() - interval '115 days' + interval '20 hours'),
    (37, 'U115-PLASMA', now() - interval '290 days' + interval '5 minutes', now() - interval '290 days' + interval '20 hours');
-- routine red cell demand: every facility issues nearly all its red cells well before the 42-day expiry. Lakeshore fills
-- standing orders for community hospitals that hold no stock of their own. Within each facility and group units go out
-- in collection order (FEFO: same shelf life, so oldest = soonest to expire); one unit in 12 is left to expire as wastage
CREATE TEMP TABLE past_rbc AS
SELECT row_number() OVER (ORDER BY u.collected_at, u.unit_id) AS n, u.unit_id, u.facility_id, u.blood_group,
       u.collected_at + make_interval(days => 3 + position(left(u.blood_group, 1) IN 'OAB') * 2) AS created_at
FROM v_blood_unit u
WHERE u.component_code = 'RBC' AND u.status = 'Available' AND u.expires_at <= now()
  AND NOT EXISTS (SELECT 1 FROM request_unit ru WHERE ru.unit_id = u.unit_id)
  AND u.unit_id <> 'U117-RBC';             -- Scenario 5: D-1042's last red cells expired on the shelf
DELETE FROM past_rbc WHERE n % 12 = 0;
CREATE TEMP TABLE past_req AS
SELECT 100 + row_number() OVER (ORDER BY n) AS request_id, unit_id, facility_id, blood_group, created_at,
       CASE WHEN n % 9 = 0 THEN 'Emergency' WHEN n % 4 = 0 THEN 'Urgent' ELSE 'Routine' END AS urgency
FROM past_rbc;
INSERT INTO blood_request (request_id, requested_by, patient_ref, recipient_group, component_code, units_requested, urgency,
                           required_by, status, created_at, closed_at)
SELECT p.request_id, (SELECT min(user_id) FROM staff s WHERE s.facility_id = p.facility_id), 'P-' || (25000 + p.request_id),
       p.blood_group, 'RBC', 1, p.urgency,
       p.created_at + CASE p.urgency WHEN 'Emergency' THEN interval '1 hour' WHEN 'Urgent' THEN interval '6 hours' ELSE interval '48 hours' END,
       'Fulfilled', p.created_at,
       p.created_at + CASE p.urgency WHEN 'Emergency' THEN interval '1 hour' WHEN 'Urgent' THEN interval '4 hours' ELSE interval '20 hours' END
FROM past_req p;
INSERT INTO request_unit (request_id, unit_id, reserved_at, issued_at)
SELECT p.request_id, p.unit_id, r.created_at + interval '5 minutes', r.closed_at
FROM past_req p JOIN blood_request r USING (request_id);
DROP TABLE past_rbc, past_req;

UPDATE blood_unit SET status = 'Reserved' WHERE unit_id IN (SELECT unit_id FROM request_unit);
UPDATE blood_unit SET status = 'Issued' WHERE unit_id IN (SELECT unit_id FROM request_unit);

-- two earlier donation calls (FR-26, FR-27) for Emergency requests that stock could not cover. Each recipient was notified
-- (a donor reads the notification when they answer); some never answered.
INSERT INTO donation_call (call_id, request_id, sent_by, sent_at, message) VALUES
    (1, 9, 4, now() - interval '170 days' + interval '15 minutes', 'O-negative donors are needed at Riverside General Hospital today. Please book a donation if you can.'),
    (2, 22, 5, now() - interval '14 days' + interval '20 minutes', 'O-negative donors are needed at Essex County Hospital. Please book a donation this week.');
INSERT INTO call_recipient (call_id, donor_id, response, responded_at)
SELECT call_id, donor, response, CASE WHEN response <> 'Pending' THEN c.sent_at + make_interval(hours => hrs) END
FROM (VALUES (1, 103, 'Accepted', 2), (1, 118, 'Accepted', 5), (1, 112, 'Declined', 3), (1, 137, 'Declined', 20),
             (1, 141, 'Pending', 0), (1, 147, 'Pending', 0),
             (2, 148, 'Accepted', 4), (2, 154, 'Declined', 6), (2, 150, 'Declined', 30), (2, 146, 'Pending', 0),
             (2, 141, 'Pending', 0)) AS v(call_id, donor, response, hrs)
JOIN donation_call c USING (call_id);
INSERT INTO notification (user_id, message, created_at, read_at)
SELECT r.donor_id, c.message, c.sent_at, r.responded_at FROM call_recipient r JOIN donation_call c USING (call_id);
SELECT setval(pg_get_serial_sequence('vytal.donation_call', 'call_id'), 2);

ALTER TABLE user_account ENABLE TRIGGER audit_user;
ALTER TABLE facility ENABLE TRIGGER audit_facility;
ALTER TABLE donor ENABLE TRIGGER audit_donor;
ALTER TABLE donation ENABLE TRIGGER audit_donation;
ALTER TABLE deferral ENABLE TRIGGER audit_deferral;
ALTER TABLE blood_unit ENABLE TRIGGER audit_unit;
ALTER TABLE blood_request ENABLE TRIGGER audit_request;

-- ================================================================ today, through the stored procedures
SELECT set_config('vytal.user_id', '1', false);

-- nightly job (FR-20): units past expiry become Expired and are recorded as wastage; wastage is dated at the expiry time
CALL run_nightly_expiry();
UPDATE wastage_record w SET wasted_at = u.expires_at FROM blood_unit u WHERE u.unit_id = w.unit_id AND w.cause = 'Expired';
UPDATE wastage_record w SET wasted_at = t.tested_at
FROM blood_unit u JOIN test_result t ON t.donation_id = u.donation_id AND t.result = 'Positive'
WHERE u.unit_id = w.unit_id AND w.cause = 'Discarded';

-- minimum stock levels for red cells, plasma and platelets at every site (FR-18); the threshold trigger opens alerts at once
-- (FR-19). Riverside keeps 6 O-negative red cell units for emergencies (Scenario 1) and 2 A-positive platelet units (Scenario 4).
INSERT INTO stock_threshold (facility_id, blood_group, component_code, min_units) VALUES
    (1, 'O+', 'RBC', 4), (1, 'A+', 'RBC', 3), (1, 'O-', 'RBC', 6), (1, 'B+', 'RBC', 1), (1, 'A-', 'RBC', 1),
    (1, 'O+', 'PLT', 1), (1, 'A+', 'PLT', 2), (1, 'O+', 'PLASMA', 4), (1, 'A+', 'PLASMA', 3), (1, 'AB+', 'PLASMA', 1),
    (2, 'O+', 'RBC', 4), (2, 'A+', 'RBC', 3), (2, 'O-', 'RBC', 2), (2, 'B+', 'RBC', 1), (2, 'A-', 'RBC', 1),
    (2, 'O+', 'PLT', 1), (2, 'O+', 'PLASMA', 4), (2, 'A+', 'PLASMA', 3),
    (3, 'O+', 'RBC', 2), (3, 'O+', 'PLASMA', 4), (3, 'A+', 'PLASMA', 3), (3, 'B+', 'PLASMA', 1),
    (4, 'O+', 'RBC', 2), (4, 'A+', 'RBC', 1), (4, 'O+', 'PLASMA', 1);

-- open requests, reserved by allocate() (FR-22, FR-23). Scenario 1: Maya's trauma patient TR-2291 has an unknown group,
-- so only O- red cells can be used; the AB- request at Essex County is also short
INSERT INTO blood_request (request_id, requested_by, patient_ref, recipient_group, component_code, units_requested, urgency, required_by) VALUES
    (23, 3, 'TR-2291', NULL, 'RBC', 4, 'Emergency', now() + interval '1 hour'),
    (24, 4, 'P-31057', 'A+', 'RBC', 2, 'Urgent', now() + interval '6 hours'),
    (25, 5, 'P-31063', 'O+', 'PLASMA', 2, 'Routine', now() + interval '2 days'),
    (26, 6, 'P-31078', 'AB-', 'RBC', 3, 'Routine', now() + interval '3 days'),
    (31, 4, 'P-31085', 'B+', 'PLASMA', 2, 'Routine', now() + interval '2 days');
DO $$ DECLARE r int; got int; BEGIN
    FOR r IN SELECT request_id FROM blood_request WHERE status = 'Open' ORDER BY request_id LOOP CALL allocate(r, got); END LOOP;
END $$;

-- TR-2291 cannot be covered from stock, so Maya calls every eligible O- donor (FR-25, FR-26). As in Scenario 1, two accept
-- for the next morning; two decline and the rest have not answered yet (FR-27)
DO $$ DECLARE cid int; r text; d int; ok boolean; BEGIN
    PERFORM set_config('vytal.user_id', '3', true);
    CALL send_donation_call(23, 3, NULL, CURRENT_DATE + 1, cid);            -- SE-07 template message, for the next morning
    FOR d, ok IN VALUES (118, TRUE), (146, TRUE), (141, FALSE), (147, FALSE) LOOP
        PERFORM set_config('vytal.user_id', d::text, true);               -- UC-20: each donor answers for themselves
        CALL respond_to_call(cid, d, ok, r);
    END LOOP;
END $$;

-- UC-21: the A-positive surgical patient needed only one of the two units reserved for request 24, so James issues one
-- and the other goes back on the shelf (Partially Fulfilled)
DO $$ DECLARE st text; first_unit text[]; BEGIN
    PERFORM set_config('vytal.user_id', '4', true);
    first_unit := ARRAY(SELECT ru.unit_id::text FROM request_unit ru JOIN blood_unit b USING (unit_id)
                        WHERE ru.request_id = 24 AND ru.released_at IS NULL ORDER BY b.expires_at, b.unit_id LIMIT 1);
    CALL close_request(24, FALSE, first_unit, st);
END $$;

-- Essex County's AB- patient is one unit short; Thomas calls only the non-O- donors who match, keeping O- donors for
-- emergencies (BR-09). One accepts, one declines, one has not answered.
SELECT set_config('vytal.user_id', '6', false);
CALL send_donation_call(26, 6, ARRAY['D-1036', 'D-1054', 'D-1057']);
DO $$ DECLARE r text; c int := (SELECT max(call_id) FROM donation_call); d int; ok boolean; BEGIN
    FOR d, ok IN VALUES (121, TRUE), (139, FALSE) LOOP
        PERFORM set_config('vytal.user_id', d::text, true);
        CALL respond_to_call(c, d, ok, r);
    END LOOP;
END $$;

-- Scenario 5 lookback (FR-30, FR-31): donor D-1042 (user 127) tested positive, so every unit of theirs still on a shelf is quarantined
SELECT set_config('vytal.user_id', '1', false);
CALL quarantine_donor(127);

-- keep the sequences ahead of the explicit ids used above
SELECT setval(pg_get_serial_sequence('vytal.user_account', 'user_id'), (SELECT max(user_id) FROM user_account));
SELECT setval(pg_get_serial_sequence('vytal.facility', 'facility_id'), (SELECT max(facility_id) FROM facility));
SELECT setval(pg_get_serial_sequence('vytal.screening', 'screening_id'), (SELECT max(screening_id) FROM screening));
SELECT setval(pg_get_serial_sequence('vytal.donation', 'donation_id'), (SELECT max(donation_id) FROM donation));
SELECT setval(pg_get_serial_sequence('vytal.blood_request', 'request_id'), (SELECT max(request_id) FROM blood_request));
SELECT setval('donor_code_seq', (SELECT max(substr(donor_code, 3)::int) FROM donor));

-- St. Clair's emergency department needs three O-positive red cell units for a surgical patient (UC-16, BR-10: Urgent is due
-- within 24 hours). Its one O-positive unit is reserved and Helen calls the eligible O-positive donors only, keeping
-- O-negative donors for emergencies (BR-09); one accepts and one declines (FR-26, FR-27)
DO $$ DECLARE rid int; got int; short int; cid int; r text; d int; ok boolean; codes text[]; BEGIN
    PERFORM set_config('vytal.user_id', '9', true);
    CALL submit_request(9, 'P-31120', 'O+', 'RBC', 3, 'Urgent', now() + interval '12 hours', rid, got, short);
    codes := ARRAY(SELECT donor_code::text FROM eligible_donors(rid) WHERE blood_group = 'O+' ORDER BY donor_code LIMIT 6);
    CALL send_donation_call(rid, 9, codes, CURRENT_DATE + 7, cid);
    FOR d, ok IN SELECT donor_id, row_number() OVER (ORDER BY donor_id) = 1 FROM call_recipient WHERE call_id = cid
                 ORDER BY donor_id LIMIT 2 LOOP
        PERFORM set_config('vytal.user_id', d::text, true);
        CALL respond_to_call(cid, d, ok, r);
    END LOOP;
END $$;

-- Essex County has no platelets on its shelf (they are collected at Riverside), so an Urgent platelet request for a
-- chemotherapy patient goes straight to a donation call (FR-22, FR-26); one donor accepts, two decline
DO $$ DECLARE rid int; got int; cid int; r text; d int; ok boolean; BEGIN
    PERFORM set_config('vytal.user_id', '5', true);
    INSERT INTO blood_request (requested_by, patient_ref, recipient_group, component_code, units_requested, urgency, required_by)
    VALUES (5, 'P-31092', 'B+', 'PLT', 1, 'Urgent', now() + interval '6 hours') RETURNING request_id INTO rid;
    CALL allocate(rid, got);
    CALL send_donation_call(rid, 5, NULL, NULL, cid);
    FOR d, ok IN SELECT donor_id, row_number() OVER (ORDER BY donor_id) = 1 FROM call_recipient WHERE call_id = cid
                 ORDER BY donor_id LIMIT 3 LOOP
        PERFORM set_config('vytal.user_id', d::text, true);
        CALL respond_to_call(cid, d, ok, r);
    END LOOP;
END $$;

-- second lookback (FR-30, FR-31): donor D-1043 (user 128) phoned the clinic to report malaria diagnosed after a trip,
-- so the Admin quarantines every unit of theirs still on a shelf and staff record a travel deferral
SELECT set_config('vytal.user_id', '2', false);
CALL quarantine_donor(128);
SELECT set_config('vytal.user_id', '7', false);
CALL record_deferral(7, 128, 'TRAVEL');

-- Scenario 3: quarantining D-1043's O-positive red cells left Lakeshore one unit below its minimum. Marcus's bag from
-- yesterday passes every test this morning, his three units go on the shelf, and the open alert clears by itself (FR-19)
CALL record_tests(122, '{"HIV": "Negative", "HBV": "Negative", "HCV": "Negative", "SYPH": "Negative"}');

-- live actions this morning, written to the audit log with the acting user (FR-33):
-- donor D-1046 (user 131) updates his phone number and address himself (UC-03)
SELECT set_config('vytal.user_id', '131', false);
CALL update_donor_profile(131, '519-555-2131', 'samuel.gauthier@mail.ca', '88 Wyandotte St E, Windsor, ON', 81);
-- James screens and bleeds donor D-1016 (user 101) at Riverside; the bag is split and waits for its test results (UC-07, UC-08)
SELECT set_config('vytal.user_id', '4', false);
DO $$ DECLARE sid int; dn int; units varchar[]; BEGIN
    CALL submit_screening(101, '{"1": false, "2": false, "3": true, "4": false, "5": false, "6": false}', sid);
    CALL record_donation(101, 4, 'WB', 450, 151, dn);
    CALL separate_donation(dn, ARRAY['RBC', 'PLASMA'], units);
END $$;
-- a new donor signs up online (FR-01); self-registration has no acting user yet
SELECT set_config('vytal.user_id', '', false);
CALL register_donor('zoe.marchand@mail.ca', :'pw', 'Zoe', 'Marchand', '519-555-1157', '2002-09-17', 'F', 59,
                    '410 University Ave W, Windsor, ON', 'A-');
SELECT set_config('vytal.user_id', '1', false);

-- UC-04: donor D-1017 (user 102) completes her own screening this morning and reports taking antibiotics, so the
-- questionnaire defers her for 30 days (MEDS); the audit log records her as the actor
SELECT set_config('vytal.user_id', '102', false);
CALL submit_screening(102, '{"1": false, "2": false, "3": true, "4": false, "5": true, "6": false}');
SELECT set_config('vytal.user_id', '1', false);

-- UC-24 / FR-32: Kevin (Admin) brings the tattoo deferral wording in line with Canadian Blood Services, which also defers
-- donors after acupuncture; the rule change is audited (SE-08)
SELECT set_config('vytal.user_id', '11', false);
CALL save_deferral_reason(11, 'TATTOO', 'Tattoo, piercing or acupuncture in the last 3 months', FALSE, 90);
-- Scenario 4, month end: Riverside wasted two of its four A-positive platelet units, so James lowers the minimum (UC-12)
SELECT set_config('vytal.user_id', '4', false);
CALL set_threshold(4, 1, 'A+', 'PLT', 1);
-- notifications are read as they are opened (UC-09, UC-20): Sara reads the positive-test alerts, and the donors who
-- answered today's O-negative call read theirs
DO $$ DECLARE n record; BEGIN
    FOR n IN SELECT nt.notification_id, nt.user_id FROM notification nt
             WHERE nt.read_at IS NULL AND (nt.user_id = 1 OR nt.user_id IN (
                 SELECT r.donor_id FROM call_recipient r JOIN donation_call c USING (call_id)
                 WHERE c.request_id = 23 AND r.response <> 'Pending'))
             ORDER BY nt.notification_id LOOP
        PERFORM set_config('vytal.user_id', n.user_id::text, true);
        CALL mark_notification_read(n.user_id, n.notification_id);
    END LOOP;
END $$;
SELECT set_config('vytal.user_id', '1', false);

-- SE-06: earlier sign-ins today. Every one has ended (logout or idle timeout), so none of these hashes opens a session.
INSERT INTO user_session (user_id, token_hash, created_at, last_seen_at, ended_at) VALUES
    (3, encode(sha256('sample-session-maya'::bytea), 'hex'), now() - interval '9 hours', now() - interval '8 hours 40 minutes', now() - interval '8 hours 25 minutes'),
    (101, encode(sha256('sample-session-liam'::bytea), 'hex'), now() - interval '5 hours', now() - interval '4 hours 50 minutes', now() - interval '4 hours 20 minutes'),
    (1, encode(sha256('sample-session-sara'::bytea), 'hex'), now() - interval '3 hours', now() - interval '2 hours 30 minutes', now() - interval '2 hours 30 minutes');
