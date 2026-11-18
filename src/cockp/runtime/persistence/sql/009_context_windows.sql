-- Persist dialogue windows before any service is initialized.
CREATE TABLE IF NOT EXISTS context_source_exclusions (
        campaign_id TEXT NOT NULL, group_id TEXT NOT NULL, scope_key TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_request_id TEXT NOT NULL,
        request_id TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(campaign_id,group_id,scope_key,source_kind,source_id));
CREATE INDEX IF NOT EXISTS context_excluded_requests ON context_source_exclusions
        (campaign_id,group_id,scope_key,source_request_id);
CREATE TABLE IF NOT EXISTS context_segments (
        id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, group_id TEXT NOT NULL,
        scope_key TEXT NOT NULL, scene_id TEXT, visibility TEXT NOT NULL,
        content TEXT NOT NULL, sources TEXT NOT NULL, active INTEGER NOT NULL,
        created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS context_command_requests (
        group_id TEXT NOT NULL, request_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        campaign_id TEXT, command TEXT NOT NULL, fingerprint TEXT NOT NULL,
        result TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(group_id,request_id));
CREATE TABLE IF NOT EXISTS context_epochs (
        campaign_id TEXT NOT NULL, group_id TEXT NOT NULL, scope_key TEXT NOT NULL,
        generation INTEGER NOT NULL, PRIMARY KEY(campaign_id,group_id,scope_key));
CREATE TABLE IF NOT EXISTS context_request_sources (
        group_id TEXT NOT NULL, request_id TEXT NOT NULL, scope_key TEXT NOT NULL,
        campaign_id TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(group_id,request_id,scope_key));
