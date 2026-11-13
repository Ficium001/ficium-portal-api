-- 060_institution_access_request.sql
-- KJ (2026-11-13): the portal is restrictive, SaaS-like. An institution no
-- longer registers itself (POST /auth/register is off by default); it APPLIES.
-- An application is a record only: no institution, no login, no member.
--
--   unverified  -> the applicant has not clicked the emailed link yet (24 h)
--   submitted   -> confirmed; visible to Ficium staff
--   in_review / info_requested / approved / rejected   (one reviewer, admin site)
--   provisioned -> after the contract, two admins (four-eyes) created the
--                  institution and its first admin login (institution.provision)
--   expired     -> never confirmed
--
-- Only portal-api's service session touches this table: the public endpoints
-- (/access-requests) and the signed admin channel (/internal/admin/v1).
-- No API role (anon / authenticated) has any privilege on it.

begin;

create table if not exists institution.access_request (
  id                 uuid primary key default gen_random_uuid(),
  reference          text not null unique
                     default upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 10)),
  institution_name   text not null check (length(btrim(institution_name)) between 2 and 200),
  institution_type   text not null check (institution_type in
                       ('commercial_bank','fintech','micro_credit','leasing','insurance','cooperative','other')),
  country            text not null check (country ~ '^[A-Z]{2,3}$'),
  reg_number         text check (length(reg_number) <= 100),
  regulator          text check (length(regulator) <= 100),
  website            text check (length(website) <= 300),
  contact_name       text not null check (length(btrim(contact_name)) between 2 and 200),
  contact_email      text not null check (length(contact_email) between 3 and 254),
  contact_phone      text check (length(contact_phone) <= 40),
  contact_role       text check (length(contact_role) <= 100),
  message            text check (length(message) <= 2000),
  status             text not null default 'unverified' check (status in
                       ('unverified','submitted','in_review','info_requested','approved',
                        'rejected','provisioned','expired')),
  verify_token_hash  text,
  verify_expires_at  timestamptz,
  verified_at        timestamptz,
  reviewed_by        uuid,
  reviewed_at        timestamptz,
  review_note        text check (length(review_note) <= 500),
  provisioned_by     uuid,
  provisioned_at     timestamptz,
  contract_reference text check (length(contract_reference) <= 100),
  institution_id     uuid references institution.institution(id) on delete set null,
  submit_ip          inet,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  check (status <> 'provisioned' or (institution_id is not null and provisioned_at is not null)),
  check (status in ('unverified','expired') or verified_at is not null)
);

-- One open application per contact email; a closed one never blocks a new one.
create unique index if not exists access_request_one_open_per_email
  on institution.access_request (lower(contact_email))
  where status in ('unverified','submitted','in_review','info_requested','approved');
create unique index if not exists access_request_verify_token
  on institution.access_request (verify_token_hash) where verify_token_hash is not null;
create index if not exists access_request_status_idx
  on institution.access_request (status, created_at desc);

alter table institution.access_request enable row level security;
revoke all on institution.access_request from public, anon, authenticated;

comment on table institution.access_request is
  'Institution applications (060). Record only: an institution and its first login are created by the four-eyes institution.provision admin command after the contract is signed.';

commit;
