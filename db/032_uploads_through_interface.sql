-- =============================================================================
-- 032_uploads_through_interface.sql — contract 1.6.0.
-- Borrower files arrive on the interface API (PUT .../uploads/{id}/content), not
-- through a storage URL. Record what was received so /complete compares hashes
-- without downloading the file again.
-- =============================================================================
BEGIN;
ALTER TABLE integration.document_upload
    ADD COLUMN IF NOT EXISTS content_sha256      text CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN IF NOT EXISTS content_received_at timestamptz;
COMMIT;
