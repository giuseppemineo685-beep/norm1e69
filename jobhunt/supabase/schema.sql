-- Run in the Supabase SQL editor. One table for postings, one for run logs.
create table if not exists jobs (
  id          text primary key,
  source      text not null,
  company     text not null,
  title       text not null,
  url         text not null,
  location    text default '',
  external_id text,
  posted_at   text,
  jd          text default '',
  raw         jsonb default '{}'::jsonb,
  status      text not null default 'new',
  score       integer,
  reasons     jsonb default '{}'::jsonb,
  first_seen  timestamptz not null default now(),
  last_seen   timestamptz not null default now(),
  decided_at  timestamptz,
  drive_url   text,
  error       text
);
create index if not exists jobs_status_idx on jobs (status);
create index if not exists jobs_first_seen_idx on jobs (first_seen desc);

create table if not exists runs (
  id      bigserial primary key,
  at      timestamptz not null default now(),
  kind    text not null,        -- scrape | process
  source  text not null,
  found   integer default 0,
  new     integer default 0,
  error   text default ''
);

-- Only the service role key (server side) touches these tables.
alter table jobs enable row level security;
alter table runs enable row level security;
