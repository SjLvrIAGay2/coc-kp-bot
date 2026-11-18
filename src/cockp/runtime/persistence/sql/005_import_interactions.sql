
CREATE TABLE IF NOT EXISTS import_reply_origins (
 job_id TEXT PRIMARY KEY, descriptor_hash TEXT NOT NULL, private INTEGER NOT NULL,
 actor_id TEXT NOT NULL, request_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_file_fingerprints (
 campaign_id TEXT NOT NULL, actor_id TEXT NOT NULL, file_id TEXT NOT NULL,
 sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 PRIMARY KEY(campaign_id,actor_id,file_id)
);
CREATE TABLE IF NOT EXISTS import_file_references (
 campaign_id TEXT NOT NULL, group_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 native_message_id TEXT NOT NULL, job_id TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(campaign_id,group_id,actor_id,native_message_id)
);
