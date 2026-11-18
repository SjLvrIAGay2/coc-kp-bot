
CREATE TABLE IF NOT EXISTS official_wire_events (
 fingerprint TEXT PRIMARY KEY, source_id TEXT, event_type TEXT NOT NULL,
 received_at REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS official_event_inbox (
 source_id TEXT PRIMARY KEY, message TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', received_at REAL NOT NULL,
 completed_at REAL, error_code TEXT);
CREATE TABLE IF NOT EXISTS official_reply_sources (
 source_id TEXT PRIMARY KEY, group_openid TEXT, user_openid TEXT NOT NULL,
 msg_id TEXT NOT NULL, received_at REAL NOT NULL, private INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS official_outbox_sources (
 outbox_id TEXT PRIMARY KEY REFERENCES outbox(id),
 source_id TEXT NOT NULL REFERENCES official_reply_sources(source_id));
CREATE TABLE IF NOT EXISTS official_reply_sequences (
 source_id TEXT NOT NULL, destination TEXT NOT NULL, next_seq INTEGER NOT NULL,
 PRIMARY KEY(source_id,destination));
CREATE TABLE IF NOT EXISTS official_delivery_parts (
 outbox_id TEXT NOT NULL, part_index INTEGER NOT NULL, status TEXT NOT NULL,
 receipt TEXT, msg_seq INTEGER, PRIMARY KEY(outbox_id,part_index));
CREATE TABLE IF NOT EXISTS official_group_bindings (
 internal_group TEXT PRIMARY KEY, group_openid TEXT UNIQUE NOT NULL,
 owner_openid TEXT NOT NULL, evidence_source TEXT NOT NULL, bound_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS official_member_bindings (
 group_openid TEXT NOT NULL, member_openid TEXT NOT NULL,
 actor_id TEXT NOT NULL, evidence_source TEXT NOT NULL, bound_at REAL NOT NULL,
 PRIMARY KEY(group_openid,member_openid));
CREATE TABLE IF NOT EXISTS official_c2c_bindings (
 user_openid TEXT PRIMARY KEY, actor_id TEXT NOT NULL UNIQUE,
 group_openid TEXT NOT NULL, evidence_source TEXT NOT NULL, bound_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS official_member_roles (
 group_openid TEXT NOT NULL, member_openid TEXT NOT NULL,
 role TEXT NOT NULL, source_id TEXT NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(group_openid,member_openid));
CREATE TABLE IF NOT EXISTS official_job_sources (
 job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 campaign_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS official_message_index (
 group_openid TEXT NOT NULL, message_index TEXT NOT NULL,
 member_openid TEXT NOT NULL, source_id TEXT NOT NULL,
 PRIMARY KEY(group_openid,message_index));
CREATE TABLE IF NOT EXISTS official_private_links (
 token_hash TEXT PRIMARY KEY, user_openid TEXT NOT NULL,
 group_openid TEXT, member_openid TEXT, actor_id TEXT,
 expires_at REAL NOT NULL, confirmed_at REAL);

CREATE TABLE IF NOT EXISTS official_admin_actions (
 request_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, app_id TEXT NOT NULL,
 group_openid TEXT NOT NULL, actor_openid TEXT NOT NULL, campaign_id TEXT,
 action TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, created_at REAL NOT NULL, dispatched_at REAL, completed_at REAL);
