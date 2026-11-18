CREATE TABLE IF NOT EXISTS conversation_messages (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
 group_id TEXT NOT NULL, request_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 role TEXT NOT NULL, visibility TEXT NOT NULL, content TEXT NOT NULL,
 scene_id TEXT, retrievable INTEGER NOT NULL, source_event_id TEXT,
 created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS conversation_campaign ON conversation_messages(campaign_id,created_at,id);
CREATE TABLE IF NOT EXISTS conversation_summaries (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
 group_id TEXT NOT NULL, visibility TEXT NOT NULL, content TEXT NOT NULL,
 created_at REAL NOT NULL);
