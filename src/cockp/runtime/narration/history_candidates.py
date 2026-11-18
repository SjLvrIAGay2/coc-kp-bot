"""Bound materialized transcript candidates after SQL audience/epoch filtering.

The original text remains searchable across the whole retained window. SQL
scans authorized text for lexical candidates; Python only expands a bounded
shortlist. This is not a full-text index or an exhaustive transcript export.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from cockp.documents.importers import search_terms


@dataclass(frozen=True)
class HistoryCandidates:
    rows: list[dict]
    limited: bool


class TranscriptQuery:
    def __init__(self, store, cid, group_id, request_id, *, private=False, actor_id=None):
        self.store = store
        self.params = [actor_id or "", cid, group_id, request_id, int(private), actor_id or ""]
        self.cte = """
            WITH visible AS (
                SELECT m.rowid AS sequence,m.*,
                    CASE WHEN json_extract(m.visibility,'$.scope')='public'
                         THEN 'public' ELSE 'private:' || ? END AS window_scope
                FROM conversation_messages m
                WHERE m.campaign_id=? AND m.group_id=? AND m.request_id<>?
                  AND (json_extract(m.visibility,'$.scope')='public' OR
                       (?=1 AND json_extract(m.visibility,'$.scope')='private'
                        AND EXISTS (SELECT 1 FROM json_each(m.visibility,'$.recipients') WHERE value=?)))
            ), eligible AS (
                SELECT m.* FROM visible m
                WHERE NOT EXISTS (
                    SELECT 1 FROM context_source_exclusions x
                    WHERE x.campaign_id=m.campaign_id AND x.group_id=m.group_id AND x.scope_key=m.window_scope
                      AND ((x.source_kind='dialogue' AND x.source_id=m.id) OR x.source_request_id=m.request_id)
                ) AND NOT EXISTS (
                    SELECT 1 FROM context_request_sources r
                    LEFT JOIN context_epochs e ON e.campaign_id=m.campaign_id
                        AND e.group_id=r.group_id AND e.scope_key=r.scope_key
                    WHERE r.group_id=m.group_id AND r.request_id=m.request_id AND r.scope_key=m.window_scope
                      AND (r.campaign_id<>m.campaign_id OR r.generation<COALESCE(e.generation,0))
                )
            )
        """

    def _rows(self, sql, params=()):
        with self.store._lock:
            rows = self.store.db.execute(self.cte + sql, [*self.params, *params]).fetchall()
        return [{**dict(row), "visibility": json.loads(row["visibility"])} for row in rows]

    def candidates(self, query: str, recent_limit: int, *, limit: int = 256) -> HistoryCandidates:
        # Keep candidate/recent selection within one in-process read boundary.
        with self.store._lock:
            return self._candidates(query, recent_limit, limit=limit)

    def _candidates(self, query: str, recent_limit: int, *, limit: int) -> HistoryCandidates:
        limit = max(limit, recent_limit * 4)
        # Small windows retain exact summary cadence and existing ordering.
        probe = self._rows("SELECT * FROM eligible WHERE retrievable=1 ORDER BY sequence DESC LIMIT ?", [limit + 1])
        if len(probe) <= limit:
            return HistoryCandidates(list(reversed(probe)), False)
        eligible_evidence = "retrievable=1 AND (role<>'assistant' OR source_event_id IS NOT NULL)"
        recent = self._rows(f"SELECT * FROM eligible WHERE {eligible_evidence} ORDER BY sequence DESC LIMIT ?",
                            [max(recent_limit, 8)])
        # Long queries cannot turn into an unbounded SQL expression or parameter
        # list. Longer terms carry more identifying information than short ones.
        terms = sorted(search_terms(query[:4000]), key=lambda term: (-len(term), term))[:128]
        matches = []
        if terms:
            matches = self._rows(f"""
                , terms AS MATERIALIZED (
                    SELECT q.value AS term, (SELECT count(*) FROM eligible e
                        WHERE e.retrievable=1 AND (e.role<>'assistant' OR e.source_event_id IS NOT NULL)
                          AND instr(lower(e.content),q.value)>0) AS frequency
                    FROM json_each(?) q
                )
                SELECT * FROM (
                    SELECT m.*, (SELECT sum(1.0/q.frequency) FROM terms q
                        WHERE q.frequency>0 AND instr(lower(m.content),q.term)>0) AS lexical_score
                    FROM eligible m WHERE {eligible_evidence}
                ) WHERE lexical_score>0 ORDER BY lexical_score DESC,sequence DESC LIMIT ?
            """, [json.dumps(terms, ensure_ascii=False), limit])
        selected = {row["id"]: row for row in [*matches, *recent]}
        return HistoryCandidates(sorted(selected.values(), key=lambda row: row["sequence"]), True)

    def formal(self, limit):
        return self._rows("""
            SELECT m.* FROM eligible m JOIN events e ON e.id=m.source_event_id AND e.campaign_id=m.campaign_id
            WHERE m.role='assistant' AND e.kind IN
                ('check','roll','dice_roll','skill_roll','move','routine','interact','npc_dialogue','ai_ruling','start')
            ORDER BY m.created_at DESC,m.sequence DESC LIMIT ?
        """, [limit])
