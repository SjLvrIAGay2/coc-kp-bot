
CREATE TABLE IF NOT EXISTS file_ingress_assets (
 canonical_key TEXT PRIMARY KEY, self_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
 group_id TEXT NOT NULL, actor_id TEXT NOT NULL, private INTEGER NOT NULL,
 sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, cache_name TEXT NOT NULL,
 descriptor TEXT NOT NULL, job_id TEXT, claim_token TEXT, claim_until REAL NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_ingress_sources (
 self_id TEXT NOT NULL, event_id TEXT NOT NULL, file_index INTEGER NOT NULL,
 campaign_id TEXT NOT NULL, group_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 private INTEGER NOT NULL, canonical_key TEXT NOT NULL,
 source_kind TEXT NOT NULL, alias_sha256 TEXT NOT NULL,
 metadata_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(self_id,event_id,file_index)
);
CREATE TABLE IF NOT EXISTS file_ingress_jobs (
 canonical_key TEXT NOT NULL, job_id TEXT NOT NULL, duplicate_of TEXT,
 created_at REAL NOT NULL, PRIMARY KEY(canonical_key,job_id)
);
