
CREATE TABLE IF NOT EXISTS import_selections (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, private INTEGER NOT NULL, generation INTEGER NOT NULL,
 intent TEXT NOT NULL, source_request_id TEXT NOT NULL, job_ids TEXT NOT NULL,
 supplemental_text TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
 consumed_request_id TEXT
);
CREATE INDEX IF NOT EXISTS import_selection_scope ON import_selections
 (campaign_id,group_id,actor_id,private,created_at);
