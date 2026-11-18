
CREATE TABLE IF NOT EXISTS group_observations (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
 request_id TEXT NOT NULL, actor_id TEXT NOT NULL, private INTEGER NOT NULL,
 content TEXT NOT NULL, summary TEXT NOT NULL, decision TEXT NOT NULL,
 consumed_by TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_scope ON group_observations(campaign_id,private,created_at);
CREATE TABLE IF NOT EXISTS import_jobs (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, private INTEGER NOT NULL,
 kind TEXT NOT NULL, descriptor TEXT NOT NULL, status TEXT NOT NULL,
 result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS imports_status ON import_jobs(status,created_at);

CREATE TABLE IF NOT EXISTS passive_memories (
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
 private INTEGER NOT NULL, actor_id TEXT NOT NULL, source_ids TEXT NOT NULL,
 source_message_ids TEXT NOT NULL, summary TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS passive_memory_sources (
 observation_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES passive_memories(id)
);
CREATE INDEX IF NOT EXISTS passive_memories_scope ON passive_memories(campaign_id,group_id,private,actor_id);

CREATE TABLE IF NOT EXISTS group_admin_requests(
 group_id TEXT NOT NULL,request_id TEXT NOT NULL,campaign_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,action TEXT NOT NULL,fingerprint TEXT NOT NULL,
 parameters TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created_at REAL NOT NULL,
 PRIMARY KEY(group_id,request_id));
CREATE TABLE IF NOT EXISTS group_admin_messages(
 self_id TEXT NOT NULL,group_id TEXT NOT NULL,message_id TEXT NOT NULL,user_id TEXT NOT NULL,
 observed_at REAL NOT NULL,PRIMARY KEY(self_id,group_id,message_id));
CREATE TABLE IF NOT EXISTS group_nickname_syncs(
 self_id TEXT NOT NULL,campaign_id TEXT NOT NULL,request_id TEXT NOT NULL,
 group_id TEXT NOT NULL,source_state_version INTEGER NOT NULL,plan TEXT NOT NULL,
 created_at REAL NOT NULL,PRIMARY KEY(self_id,campaign_id,request_id));
