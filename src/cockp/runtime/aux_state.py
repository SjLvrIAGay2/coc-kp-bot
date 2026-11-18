"""Compatibility SQL exports; schema ownership is runtime.persistence.

Existing callers may import these names during the current 0.x release.
New code must use Store startup migrations, never service-time table creation.
"""
from cockp.runtime.persistence.schema import migration_sql

SQL = migration_sql(3)
IMPORT_INTERACTION_SQL = migration_sql(5)
# Retain the former subset exports without duplicating their SQL definitions.
_passive_start = SQL.index("CREATE TABLE IF NOT EXISTS passive_memories")
_admin_start = SQL.index("CREATE TABLE IF NOT EXISTS group_admin_requests")
PASSIVE_SQL = SQL[_passive_start:_admin_start]
GROUP_ADMIN_SQL = SQL[_admin_start:]
