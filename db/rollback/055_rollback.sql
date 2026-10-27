ALTER TABLE institution.doc DROP COLUMN IF EXISTS reviewed_by_staff;
DROP TABLE IF EXISTS integration.admin_command_log;
DROP FUNCTION IF EXISTS integration._admin_command_log_worm();
