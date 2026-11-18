CREATE TABLE IF NOT EXISTS natural_action_runs (
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), request_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, input_digest TEXT NOT NULL, proposal TEXT NOT NULL,
 base_version INTEGER NOT NULL, result TEXT,
 PRIMARY KEY(campaign_id,request_id));
