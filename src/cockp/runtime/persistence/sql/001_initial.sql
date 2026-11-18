
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_campaign ON campaigns(group_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), action_id TEXT NOT NULL,
 character_id TEXT, actor_id TEXT NOT NULL, kind TEXT NOT NULL,
 ruleset_version TEXT NOT NULL, state_version INTEGER NOT NULL,
 visibility TEXT NOT NULL, payload TEXT NOT NULL, snapshot TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(campaign_id,request_id));
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, event_id TEXT NOT NULL,
 destination TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inbox (
 group_id TEXT NOT NULL, message_id TEXT NOT NULL, status TEXT NOT NULL,
 received_at REAL NOT NULL, source_time REAL NOT NULL, result TEXT,
 PRIMARY KEY(group_id,message_id));
CREATE TABLE IF NOT EXISTS rolls (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, action_id TEXT NOT NULL,
 character_id TEXT NOT NULL, result TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(campaign_id,action_id));
CREATE TABLE IF NOT EXISTS memories (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
 source_event_id TEXT NOT NULL, entity_id TEXT, visibility TEXT NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, confidence REAL NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS character_versions (
 campaign_id TEXT NOT NULL, character_id TEXT NOT NULL, version INTEGER NOT NULL,
 actor_id TEXT NOT NULL, sheet TEXT NOT NULL, review TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(campaign_id,character_id,version));
CREATE TABLE IF NOT EXISTS provider_usage (
 id TEXT PRIMARY KEY, campaign_id TEXT, action_id TEXT, provider TEXT,
 usage TEXT NOT NULL, created_at REAL NOT NULL);
