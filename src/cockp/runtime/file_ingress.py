"""Byte-verified inbound attachment deduplication, before routing or workers.

Identities are scoped by authenticated self/group/campaign/uploader/private and
SHA256. Opaque gateway IDs, labels and timestamps never prove equal contents.
Only the configured AttachmentFiles downloader may introduce a cache asset.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from cockp.core.contracts import DomainError


from cockp.runtime.persistence.schema import migration_sql

# Compatibility export; Store applies this version before ingress is created.
FILE_INGRESS_SQL = migration_sql(6)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class FileIngressError(DomainError):
    def __init__(self, code):
        self.code = code
        text = {
            "metadata": "附件元数据不合法，请重新发送原文件。",
            "acquisition": "未能取得附件，请重新发送原文件或有效HTTPS链接。",
            "size": "附件大小校验不一致，未创建资料任务。",
            "cache": "已下载附件缓存校验失败，请重新上传文件。",
            "source": "上传事件身份发生冲突，未创建或替换资料任务。",
            "scope": "战役或附件接收范围已变化，请在原团重新发送。",
            "job": "资料任务与本次附件来源不符，未修改原任务。",
            "claim": "附件接收归属已变化，请查看原任务进度。",
        }.get(code, "附件接收暂未完成，请重新发送。")
        super().__init__(text)


@dataclass(frozen=True)
class PreparedFile:
    canonical_key: str
    descriptor: dict = field(repr=False)
    existing_job_id: str | None
    is_owner: bool
    claim_token: str | None = field(repr=False)
    source_event_id: str
    source_index: int
    scope: tuple = field(repr=False)
    metadata_sha256: str = field(repr=False)

    @property
    def duplicate(self):
        return not self.is_owner

    @property
    def job_id_hint(self):
        return self.existing_job_id or self.canonical_key


class FileIngress:
    def __init__(self, store, attachments, adapter=None, *, claim_seconds=300):
        self.store, self.attachments, self.adapter = store, attachments, adapter
        self.root = Path(attachments.root).resolve()
        self.max_bytes = min(int(getattr(attachments, "max_bytes", 20 * 1024 * 1024)), 20 * 1024 * 1024)
        if type(claim_seconds) is not int or not 1 <= claim_seconds <= 3600:
            raise ValueError("附件接收租约不合法")
        self.claim_seconds = claim_seconds
        for statement in FILE_INGRESS_SQL.split(";"):
            if statement.strip():
                store.db.execute(statement)

    @staticmethod
    def _scope(self_id, campaign_id, group_id, actor_id, private):
        if type(private) is not bool or any(not isinstance(value, str) or not value or len(value) > 512
                for value in (self_id, campaign_id, group_id, actor_id)):
            raise FileIngressError("scope")
        return (self_id, campaign_id, group_id, actor_id, int(private))

    @staticmethod
    def _metadata(descriptor):
        if not isinstance(descriptor, dict) or set(descriptor) - {"id", "name", "size", "url", "busid", "mime_type"}:
            raise FileIngressError("metadata")
        data = {key: value for key, value in descriptor.items() if key != "mime_type"}
        name, ident, url, size = data.get("name", ""), data.get("id"), data.get("url"), data.get("size", 0)
        if not isinstance(name, str) or len(name) > 512 or type(size) is not int or size < 0:
            raise FileIngressError("metadata")
        if ident is not None and (not isinstance(ident, str) or not 1 <= len(ident) <= 512 or any(ord(c) < 32 for c in ident)):
            raise FileIngressError("metadata")
        if url is not None and (not isinstance(url, str) or not url.startswith("https://") or len(url) > 4096):
            raise FileIngressError("metadata")
        busid = data.get("busid")
        if busid is not None and (type(busid) is not int or not 0 <= busid <= 2147483647):
            raise FileIngressError("metadata")
        if not ident and not url:
            raise FileIngressError("metadata")
        return {**data, "name": name, "size": size}

    def _verify_path(self, path, *, sha256=None, size=None):
        try:
            given = Path(path)
            resolved = given.resolve(strict=True)
            if (given.is_symlink() or not resolved.is_relative_to(self.root) or resolved.parent != self.root
                    or not resolved.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.(?:txt|md|json|pdf|docx|xlsx|xls|xlsm|png|jpe?g|webp)", resolved.name)):
                raise FileIngressError("cache")
            count = resolved.stat().st_size
            if not 0 < count <= self.max_bytes or size is not None and count != size:
                raise FileIngressError("cache")
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != resolved.stem or sha256 is not None and actual != sha256 or resolved.stat().st_size != count:
                raise FileIngressError("cache")
            return resolved, actual, count
        except FileIngressError:
            raise
        except (OSError, ValueError):
            raise FileIngressError("cache") from None

    def _asset(self, key, scope):
        row = self.store.db.execute("SELECT * FROM file_ingress_assets WHERE canonical_key=?", (key,)).fetchone()
        if not row or tuple(row[k] for k in ("self_id", "campaign_id", "group_id", "actor_id", "private")) != scope:
            raise FileIngressError("scope")
        return row

    def _active(self, campaign_id, group_id):
        try:
            campaign = self.store.campaign(campaign_id, group_id)
        except DomainError:
            raise FileIngressError("scope") from None
        if not campaign["active"]:
            raise FileIngressError("scope")

    async def _download(self, descriptor, group_id):
        ident, url = descriptor.get("id"), descriptor.get("url")
        try:
            if ident and not ident.startswith("image:"):
                try:
                    return await self.attachments.from_group_file(self.adapter, group_id, ident,
                        busid=descriptor.get("busid"), filename=descriptor.get("name") or None)
                except Exception:
                    if not url:
                        raise
            return await self.attachments.download(url, filename=descriptor.get("name") or None)
        except Exception:
            raise FileIngressError("acquisition") from None

    async def prepare(self, *, self_id, campaign_id, group_id, actor_id, private, event_id,
                      descriptor, index=0, source_kind="file"):
        scope = self._scope(self_id, campaign_id, group_id, actor_id, private)
        if (not isinstance(event_id, str) or not event_id or len(event_id) > 512 or type(index) is not int or not 0 <= index < 20
                or source_kind not in {"file", "group_upload", "message_file", "https", "legacy"}):
            raise FileIngressError("metadata")
        data = self._metadata(descriptor)
        metadata_hash = _digest(data)
        source = self.store.db.execute("SELECT * FROM file_ingress_sources WHERE self_id=? AND event_id=? AND file_index=?",
            (self_id, event_id, index)).fetchone()
        if source and (tuple(source[k] for k in ("self_id", "campaign_id", "group_id", "actor_id", "private")) != scope
                       or source["metadata_sha256"] != metadata_hash):
            raise FileIngressError("source")
        self._active(campaign_id, group_id)
        if data["size"] > self.max_bytes:
            raise FileIngressError("size")
        if source:
            asset = self._asset(source["canonical_key"], scope)
            path, content_hash, count = self._verify_path(self.root / asset["cache_name"], sha256=asset["sha256"], size=asset["bytes"])
        else:
            path, content_hash, count = self._verify_path(await self._download(data, group_id))
            if data["size"] and data["size"] != count:
                raise FileIngressError("size")
        key = _digest(["file-ingress-v1", *scope, content_hash])
        normalized = {**data, "size": count, "file_ingress_key": key, "file_ingress_sha256": content_hash}
        now = time.time()
        def commit():
            with self.store.transaction() as db:
                self._active(campaign_id, group_id)
                # Another prepare may have committed while download was awaiting.
                current = db.execute("SELECT * FROM file_ingress_sources WHERE self_id=? AND event_id=? AND file_index=?",
                    (self_id, event_id, index)).fetchone()
                if current and (tuple(current[k] for k in ("self_id", "campaign_id", "group_id", "actor_id", "private")) != scope
                                or current["metadata_sha256"] != metadata_hash or current["canonical_key"] != key):
                    raise FileIngressError("source")
                db.execute("INSERT OR IGNORE INTO file_ingress_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, *scope, content_hash, count, path.name, _json(normalized), None, None, 0, now, now))
                asset = self._asset(key, scope)
                if asset["sha256"] != content_hash or asset["bytes"] != count:
                    raise FileIngressError("cache")
                db.execute("UPDATE file_ingress_assets SET cache_name=?,updated_at=? WHERE canonical_key=?", (path.name, now, key))
                db.execute("INSERT OR IGNORE INTO file_ingress_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self_id, event_id, index, campaign_id, group_id, actor_id, int(private), key, source_kind,
                     _digest(["file_id" if data.get("id") else "url", data.get("id") or data.get("url")]), metadata_hash, now))
                # Deterministic job identity also recovers queue-before-bind crashes.
                existing = db.execute("SELECT * FROM import_jobs WHERE id=?", (key,)).fetchone() if not asset["job_id"] else None
                if existing:
                    self._check_job(existing, key, scope)
                    db.execute("UPDATE file_ingress_assets SET job_id=?,claim_token=NULL,claim_until=0,updated_at=? WHERE canonical_key=?", (key, now, key))
                    db.execute("INSERT OR IGNORE INTO file_ingress_jobs VALUES(?,?,NULL,?)", (key, key, now))
                    asset = self._asset(key, scope)
                token = None
                if not asset["job_id"] and (not asset["claim_token"] or asset["claim_until"] <= now):
                    token = uuid.uuid4().hex
                    db.execute("UPDATE file_ingress_assets SET claim_token=?,claim_until=?,updated_at=? WHERE canonical_key=?",
                        (token, now + self.claim_seconds, now, key))
                return PreparedFile(key, json.loads(asset["descriptor"]), asset["job_id"], token is not None,
                    token, event_id, index, scope, metadata_hash)
        return await self.store.arun(commit)

    @staticmethod
    def _check_job(row, key, scope):
        if (row["campaign_id"], row["group_id"], row["actor_id"], row["private"]) != (scope[1], scope[2], scope[3], scope[4]):
            raise FileIngressError("job")
        descriptor = json.loads(row["descriptor"])
        if descriptor.get("file_ingress_key") != key:
            raise FileIngressError("job")

    def prepare_cached_reference(self, *, self_id, campaign_id, group_id, actor_id,
                                 private, source_event_id, index=0):
        """Claim only an exact, already recorded public upload; never download.

        The caller must authenticate the new reference and recheck its context
        generation before queueing. The old source remains immutable evidence.
        """
        scope = self._scope(self_id, campaign_id, group_id, actor_id, private)
        if private or (not isinstance(source_event_id, str) or not 1 <= len(source_event_id) <= 512
                       or type(index) is not int or not 0 <= index < 20):
            raise FileIngressError("scope")
        with self.store.transaction() as db:
            self._active(campaign_id, group_id)
            source = db.execute("SELECT * FROM file_ingress_sources WHERE self_id=? AND event_id=? AND file_index=?",
                                (self_id, source_event_id, index)).fetchone()
            if not source or tuple(source[k] for k in ("self_id", "campaign_id", "group_id", "actor_id", "private")) != scope:
                raise FileIngressError("scope")
            key = source["canonical_key"]
            asset = self._asset(key, scope)
            if key != _digest(["file-ingress-v1", *scope, asset["sha256"]]):
                raise FileIngressError("cache")
            try:
                descriptor = json.loads(asset["descriptor"])
                data = self._metadata({k: v for k, v in descriptor.items()
                                       if k not in {"file_ingress_key", "file_ingress_sha256"}})
            except (ValueError, TypeError, AttributeError):
                raise FileIngressError("cache") from None
            if (descriptor.get("file_ingress_key") != key or descriptor.get("file_ingress_sha256") != asset["sha256"]
                    or data["size"] != asset["bytes"]):
                raise FileIngressError("cache")
            self._verify_path(self.root / asset["cache_name"], sha256=asset["sha256"], size=asset["bytes"])
            # Recover a queue-before-bind crash using only the canonical job.
            existing = db.execute("SELECT * FROM import_jobs WHERE id=?", (asset["job_id"] or key,)).fetchone()
            if asset["job_id"] and not existing:
                raise FileIngressError("job")
            if existing:
                self._check_job(existing, key, scope)
                if not asset["job_id"]:
                    db.execute("UPDATE file_ingress_assets SET job_id=?,claim_token=NULL,claim_until=0,updated_at=? WHERE canonical_key=?",
                               (existing["id"], time.time(), key))
                    db.execute("INSERT OR IGNORE INTO file_ingress_jobs VALUES(?,?,NULL,?)", (key, existing["id"], time.time()))
                    asset = self._asset(key, scope)
            token = None
            now = time.time()
            if not asset["job_id"] and (not asset["claim_token"] or asset["claim_until"] <= now):
                token = uuid.uuid4().hex
                db.execute("UPDATE file_ingress_assets SET claim_token=?,claim_until=?,updated_at=? WHERE canonical_key=?",
                           (token, now + self.claim_seconds, now, key))
            return PreparedFile(key, descriptor, asset["job_id"], token is not None, token,
                                source_event_id, index, scope, source["metadata_sha256"])

    def bind_job(self, prepared, job_id):
        with self.store.transaction() as db:
            asset = self._asset(prepared.canonical_key, prepared.scope)
            row = db.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise FileIngressError("job")
            self._check_job(row, prepared.canonical_key, prepared.scope)
            owner = asset["job_id"]
            if not owner:
                if prepared.claim_token != asset["claim_token"] and job_id != prepared.canonical_key:
                    raise FileIngressError("claim")
                owner = job_id
                db.execute("UPDATE file_ingress_assets SET job_id=?,claim_token=NULL,claim_until=0,updated_at=? WHERE canonical_key=?",
                    (owner, time.time(), prepared.canonical_key))
            db.execute("INSERT OR IGNORE INTO file_ingress_jobs VALUES(?,?,?,?)",
                (prepared.canonical_key, job_id, None if owner == job_id else owner, time.time()))
            return owner

    def register_existing_job(self, prepared, job_id):
        """Audit a legacy job only after preparing its actual saved descriptor.

        This adds a canonical/duplicate_of relation without editing old jobs,
        their status, role, result, source events or previously sent replies.
        Callers must prepare each legacy descriptor through the downloader;
        matching labels/size alone are never sufficient for this operation.
        """
        with self.store.transaction() as db:
            asset = self._asset(prepared.canonical_key, prepared.scope)
            row = db.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or (row["campaign_id"], row["group_id"], row["actor_id"], row["private"]) != (
                    prepared.scope[1], prepared.scope[2], prepared.scope[3], prepared.scope[4]):
                raise FileIngressError("job")
            original = json.loads(row["descriptor"])
            metadata = self._metadata({key: value for key, value in original.items()
                if key in {"id", "name", "size", "url", "busid", "mime_type"}})
            if _digest(metadata) != prepared.metadata_sha256:
                raise FileIngressError("job")
            self._verify_path(self.root / asset["cache_name"], sha256=asset["sha256"], size=asset["bytes"])
            owner = asset["job_id"] or job_id
            if not asset["job_id"]:
                db.execute("UPDATE file_ingress_assets SET job_id=?,claim_token=NULL,claim_until=0,updated_at=? WHERE canonical_key=?",
                    (owner, time.time(), prepared.canonical_key))
            db.execute("INSERT OR IGNORE INTO file_ingress_jobs VALUES(?,?,?,?)",
                (prepared.canonical_key, job_id, None if owner == job_id else owner, time.time()))
            return owner

    def release(self, prepared):
        if not prepared.claim_token:
            return False
        return bool(self.store.db.execute("UPDATE file_ingress_assets SET claim_token=NULL,claim_until=0 "
            "WHERE canonical_key=? AND claim_token=? AND job_id IS NULL", (prepared.canonical_key, prepared.claim_token)).rowcount)

    def cached_path(self, canonical_key, *, self_id, campaign_id, group_id, actor_id, private, job_id=None):
        scope = self._scope(self_id, campaign_id, group_id, actor_id, private)
        asset = self._asset(canonical_key, scope)
        if job_id is not None and asset["job_id"] != job_id:
            raise FileIngressError("job")
        return self._verify_path(self.root / asset["cache_name"], sha256=asset["sha256"], size=asset["bytes"])[0]

    def cached_path_for_job(self, job, *, self_id, legacy_self_ids=()):
        """Read a keyless historical owner only with complete byte/source proof.

        The optional legacy identity set comes from the trusted transport's
        explicit migration binding. It can never come from a chat descriptor.
        This neither rewrites an old job nor falls back to filename matching.
        """
        descriptor = json.loads(job["descriptor"])
        if descriptor.get("file_ingress_key") or not descriptor.get("id"):
            return None
        evidence = self.store.db.execute(
            "SELECT a.*,j.duplicate_of FROM file_ingress_jobs j JOIN file_ingress_assets a "
            "ON a.canonical_key=j.canonical_key WHERE j.job_id=?", (job["id"],)).fetchall()
        if not evidence:
            return None
        if len(evidence) != 1:
            raise FileIngressError("job")
        asset = evidence[0]
        expected = tuple(job[k] for k in ("campaign_id", "group_id", "actor_id", "private"))
        if (tuple(asset[k] for k in ("campaign_id", "group_id", "actor_id", "private")) != expected
                or asset["self_id"] not in {self_id, *legacy_self_ids}
                or asset["job_id"] != job["id"] or asset["duplicate_of"] is not None):
            raise FileIngressError("scope")
        self._active(job["campaign_id"], job["group_id"])
        base = self._metadata({k: v for k, v in descriptor.items()
                               if k in {"id", "name", "size", "url", "busid", "mime_type"}})
        proof = self.store.db.execute(
            "SELECT 1 FROM file_ingress_sources WHERE canonical_key=? AND self_id=? "
            "AND campaign_id=? AND group_id=? AND actor_id=? AND private=? "
            "AND metadata_sha256=? AND alias_sha256=?",
            (asset["canonical_key"], asset["self_id"], *expected, _digest(base),
             _digest(["file_id", base["id"]]))).fetchone()
        fingerprint = self.store.db.execute(
            "SELECT sha256,bytes FROM import_file_fingerprints WHERE campaign_id=? AND actor_id=? AND file_id=?",
            (job["campaign_id"], job["actor_id"], base["id"])).fetchone()
        if (proof is None or fingerprint is None or fingerprint["sha256"] != asset["sha256"]
                or fingerprint["bytes"] != asset["bytes"]):
            raise FileIngressError("source")
        return self._verify_path(self.root / asset["cache_name"], sha256=asset["sha256"], size=asset["bytes"])[0]

    def recover_unbound(self):
        """Single-owner service startup only, before accepting requests."""
        return self.store.db.execute("UPDATE file_ingress_assets SET claim_token=NULL,claim_until=0 WHERE job_id IS NULL").rowcount
