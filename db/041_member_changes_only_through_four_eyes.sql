-- 041_member_changes_only_through_four_eyes.sql
-- After 040, the database applies user.update / user.deactivate /
-- user.reactivate on approval, and the API endpoints only queue them. Two
-- things remained: the primary-admin protection lived only in the API, and
-- bank users could still UPDATE/DELETE institution.member directly through
-- the Supabase API (policy institution_members_update_admin), i.e. change a
-- colleague's role or deactivate them with no second person.
-- Registration's first-admin INSERT (038) is untouched.

begin;

-- 1. Primary admin cannot be edited or deactivated, enforced where it executes.
create or replace function governance.guard_member_action()
  returns trigger language plpgsql security definer set search_path = '' as
$$
begin
  if new.category in ('user.update', 'user.deactivate')
     and exists (select 1 from institution.member m
                  where m.id = (new.payload ->> 'member_id')::uuid
                    and m.is_primary_admin) then
    raise exception 'The primary admin cannot be edited or deactivated';
  end if;
  return new;
end
$$;
drop trigger if exists trg_guard_member_action on governance.action;
create trigger trg_guard_member_action
  before insert on governance.action
  for each row when (new.category in ('user.update', 'user.deactivate'))
  execute function governance.guard_member_action();

-- 2. No direct member changes by bank users; four-eyes is the only path.
drop policy if exists institution_members_update_admin on institution.member;
revoke update, delete, truncate on institution.member from authenticated, anon;

-- 3. Verify.
do $verify$
declare
  maker   uuid := 'bbb283cf-548d-476c-a190-9957526e2afa';  -- mcbadmin
  primary_member uuid := (select id from institution.member where auth_user_id = maker);
  other   uuid := '8617bae3-a263-4c38-8db1-45968f58a4dc';
  bad text := '';
begin
  if primary_member is null then raise notice '041: test member absent, skipped'; return; end if;
  begin
    perform set_config('request.jwt.claims', json_build_object('sub', maker, 'role', 'authenticated')::text, true);
    set local role authenticated;
    begin
      update institution.member set member_role = 'viewer' where id = other;
      bad := bad || 'direct member update; ';
    exception when insufficient_privilege then null; end;
    begin
      delete from institution.member where id = other;
      bad := bad || 'direct member delete; ';
    exception when insufficient_privilege then null; end;
    if (select is_primary_admin from institution.member where id = primary_member) then
      begin
        perform governance.submit('user.deactivate', 'member', primary_member,
          jsonb_build_object('member_id', primary_member));
        bad := bad || 'primary admin deactivation queued; ';
      exception when others then null; end;
    end if;
    -- an ordinary change still queues fine
    perform governance.submit('user.update', 'member', other,
      jsonb_build_object('member_id', other, 'field', 'full_name', 'value', 'x'));
    raise exception using errcode = 'P0002', message = 'rollback';
  exception
    when sqlstate 'P0002' then null;
    when others then bad := bad || 'ERROR: ' || sqlerrm || '; ';
  end;
  reset role;
  if bad <> '' then raise exception '041 verification failed: %', bad; end if;
end
$verify$;

commit;
