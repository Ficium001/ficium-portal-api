-- =============================================================================
-- 071 — Size and file-type limits on the institution-docs bucket.
--
-- The bucket accepted any file of any size. It now accepts what the portal
-- actually uploads, at the API's own limit (deal_documents.MAX_BYTES, 20 MB):
-- PDF, JPEG, PNG, HEIC, WebP, Word (.docx) and Excel (.xlsx), the same list as
-- deal_documents.ALLOWED_MIME. Everything stored today is .docx or PDF under
-- 60 kB, so nothing existing is affected. Storage enforces this on upload, so
-- an upload straight from the browser (onboarding) is held to it too.
--
-- From the 2026-11-24 database audit.
-- =============================================================================

-- Guarded: a cell on plain Postgres (CI, or a bank cell without Supabase Storage) has no
-- storage schema, and there is nothing to limit.
do $$
begin
  if to_regclass('storage.buckets') is not null then
    update storage.buckets
       set file_size_limit = 20 * 1024 * 1024,
           allowed_mime_types = array[
             'application/pdf', 'image/jpeg', 'image/png', 'image/heic', 'image/webp',
             'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']
     where id = 'institution-docs';
  end if;
end $$;
