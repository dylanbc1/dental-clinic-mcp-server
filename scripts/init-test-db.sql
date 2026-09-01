-- Runs once, on first initialisation of the postgres volume.
-- A dedicated database for the suite: the test fixtures drop and rebuild the
-- public schema, which must never happen to the development database.
CREATE DATABASE clinic_test OWNER clinic;
