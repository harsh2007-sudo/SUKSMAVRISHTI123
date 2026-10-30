-- ---------------------------------------------------------------------------
-- Suksmavrishti — Supabase (Postgres) hardening for the `farmers` table.
--
-- Run this ONCE in: Supabase Dashboard -> SQL Editor.
-- It is written to be safe to re-run.
--
-- Model: the Streamlit server holds the service-role key and is the ONLY
-- writer. The browser never talks to Supabase directly, so anonymous and
-- authenticated roles get no privileges at all (least privilege). RLS is on
-- with no policies, so even a leaked anon key cannot read or write rows.
-- ---------------------------------------------------------------------------

create table if not exists public.farmers (
    id bigserial primary key,
    phone text unique not null,
    name text not null default '',
    village text not null default '',
    language text not null default 'English',
    created_at timestamptz not null default now()
);

-- Server-side validation mirroring the app's validators.
alter table public.farmers drop constraint if exists farmers_phone_india_chk;
alter table public.farmers add constraint farmers_phone_india_chk
    check (phone ~ '^[6-9][0-9]{9}$') not valid;

alter table public.farmers drop constraint if exists farmers_name_len_chk;
alter table public.farmers add constraint farmers_name_len_chk
    check (char_length(name) <= 60) not valid;

alter table public.farmers drop constraint if exists farmers_village_len_chk;
alter table public.farmers add constraint farmers_village_len_chk
    check (char_length(village) <= 80) not valid;

-- Deny-by-default: no client role may touch this table.
alter table public.farmers enable row level security;
revoke all on table public.farmers from anon, authenticated;
revoke all on sequence public.farmers_id_seq from anon, authenticated;

-- ===========================================================================
-- OPERATOR CHECKS (run these and read the output)
-- ===========================================================================
-- 1) RLS is enabled and no policies exist:
--      select relname, relrowsecurity from pg_class where relname = 'farmers';
--      select * from pg_policies where tablename = 'farmers';   -- expect 0 rows
--
-- 2) The service-role key must exist ONLY in the server's .env. Never put it
--    in app code, a browser bundle, a public config file, or git.
--
-- 3) If the service-role key has ever been exposed (screenshot, commit,
--    paste), rotate it: Project Settings -> API -> "Reset" the service_role
--    secret. Deleting the key from a file does NOT invalidate a copy that was
--    already leaked.
--
-- 4) Backups: Supabase takes daily backups on paid plans. For the free tier,
--    export the table regularly and store the dump encrypted, access-limited:
--      pg_dump "$SUPABASE_DB_URL" -t public.farmers -f farmers_backup.sql
--    Restore with psql. Keep dumps out of the web root and out of git.
--
-- Data retention: the app stores only phone (as the account id), name, an
-- optional village and a language preference. Delete a farmer's row on
-- request; the audit log stores only masked phone numbers.
