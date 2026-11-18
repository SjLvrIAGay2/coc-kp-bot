"""Associate a real quoted file with an existing upload; never guess by label."""
import hashlib
import json
from pathlib import Path

from cockp.core.contracts import DomainError


class FileReferenceMixin:
    def _canonical_reference_job(self, row, message, campaign, *, content_sha256=None):
        """Resolve only byte-verified ingress aliases within the real scope.

        Legacy jobs remain immutable. A label, matching hash alone, or a lone
        duplicate_of value cannot grant access to a different job.
        """
        db = self.store.db
        evidence = db.execute(
            "SELECT j.canonical_key,j.duplicate_of,a.self_id,a.campaign_id,a.group_id,"
            "a.actor_id,a.private,a.sha256,a.job_id AS owner_id FROM file_ingress_jobs j "
            "LEFT JOIN file_ingress_assets a ON a.canonical_key=j.canonical_key WHERE j.job_id=?",
            (row["id"],)).fetchall()
        if not evidence:
            return row
        expected = (getattr(self.adapter, "self_id", None), campaign["id"],
                    message.group_id, message.user_id, int(message.private))
        owners = {}
        for proof in evidence:
            scope = tuple(proof[key] for key in ("self_id", "campaign_id", "group_id", "actor_id", "private"))
            owner_id = proof["owner_id"]
            if (not expected[0] or scope != expected or not owner_id
                    or proof["duplicate_of"] != (None if row["id"] == owner_id else owner_id)
                    or content_sha256 is not None and proof["sha256"] != content_sha256):
                raise DomainError("引用文件的原任务关联范围不一致；未继续或创建资料任务。")
            owner = db.execute(
                "SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND private=?",
                (owner_id, *expected[1:])).fetchone()
            owner_proof = db.execute(
                "SELECT duplicate_of FROM file_ingress_jobs WHERE canonical_key=? AND job_id=?",
                (proof["canonical_key"], owner_id)).fetchone()
            if not owner or not owner_proof or owner_proof["duplicate_of"] is not None:
                raise DomainError("引用文件的原任务关联范围不一致；未继续或创建资料任务。")
            owners[owner_id] = dict(owner)
        if len(owners) != 1:
            raise DomainError("引用文件未能唯一匹配原资料任务；请用 /资料 指定本人的任务号，未创建新任务。")
        return next(iter(owners.values()))

    async def _resolve_document_reference(self,message,campaign):
        if (message.private or not message.reply_to or message.files or not campaign
                or message.text.startswith("/") or not hasattr(self.adapter,"quoted_files")):
            return
        cached=self.store.db.execute("SELECT job_id FROM import_file_references WHERE campaign_id=? AND group_id=? AND actor_id=? AND native_message_id=?",
            (campaign["id"],message.group_id,message.user_id,str(message.reply_to))).fetchone()
        if cached:
            row=self.store.db.execute("SELECT * FROM import_jobs WHERE id=? AND campaign_id=? AND group_id=? AND actor_id=? AND private=0",
                (cached["job_id"],campaign["id"],message.group_id,message.user_id)).fetchone()
            if row:
                row=self._canonical_reference_job(row,message,campaign)
                await self.store.awrite("UPDATE import_file_references SET job_id=? WHERE campaign_id=? AND group_id=? AND actor_id=? AND native_message_id=?",
                    (row["id"],campaign["id"],message.group_id,message.user_id,str(message.reply_to)))
                message.document_job_id=row["id"]
                return
        from cockp.integrations.onebot import OneBotError
        try:files=await self.adapter.quoted_files(message.group_id,message.user_id,message.reply_to)
        except OneBotError as exc:
            if str(exc).startswith("quoted_file_"):raise DomainError("只能引用本人在当前团上传的原文件；没有继续或创建资料任务。") from None
            return
        if not files:return
        if len(files)!=1:raise DomainError("请分别引用一份原文件；没有合并或新建资料任务。")
        file=files[0]
        rows=[dict(row) for row in self.store.db.execute("SELECT * FROM import_jobs WHERE campaign_id=? AND group_id=? AND actor_id=? AND private=0",
              (campaign["id"],message.group_id,message.user_id))]
        exact=[row for row in rows if json.loads(row["descriptor"]).get("id")==file["id"]]
        if exact:
            matches=exact
            digest="exact_file_id"
        else:
            # Name/size only bound the work; association requires fetched bytes.
            candidates=[row for row in rows if (descriptor:=json.loads(row["descriptor"])).get("id")
                        and descriptor.get("name")==file["name"] and descriptor.get("size")==file["size"]]
            if not candidates or len(candidates)>4:
                raise DomainError("未能唯一关联本人在本团上传的资料；请用 /资料 查看并指定原任务号。")
            path=await self.attachments.from_group_file(self.adapter,message.group_id,file["id"],busid=file.get("busid"),filename=file["name"])
            if Path(path).stat().st_size!=file["size"]:raise DomainError("引用文件大小校验不一致，未继续任务。")
            digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            matches=[]
            for row in candidates:
                descriptor=json.loads(row["descriptor"])
                saved=self.store.db.execute("SELECT sha256 FROM import_file_fingerprints WHERE campaign_id=? AND actor_id=? AND file_id=?",
                    (campaign["id"],message.user_id,descriptor["id"])).fetchone()
                if saved:source_digest=saved["sha256"]
                else:
                    original=await self.attachments.from_group_file(self.adapter,message.group_id,descriptor["id"],busid=descriptor.get("busid"),filename=descriptor.get("name"))
                    source_digest=hashlib.sha256(Path(original).read_bytes()).hexdigest()
                    await self.store.awrite("INSERT OR REPLACE INTO import_file_fingerprints VALUES(?,?,?,?,?)",
                        (campaign["id"],message.user_id,descriptor["id"],source_digest,Path(original).stat().st_size))
                if digest==source_digest:matches.append(row)
        # Hash matches may include immutable legacy duplicate rows. Collapse
        # only after the ingress audit proves their common scoped owner.
        owners={}
        for matched in matches:
            owner=self._canonical_reference_job(matched,message,campaign,
                content_sha256=None if digest=="exact_file_id" else digest)
            owners[owner["id"]]=owner
        if len(owners)!=1:raise DomainError("引用文件未能唯一匹配原资料任务；请用 /资料 指定本人的任务号，未创建新任务。")
        row=next(iter(owners.values()))
        await self.store.awrite("INSERT OR REPLACE INTO import_file_references VALUES(?,?,?,?,?,?)",
            (campaign["id"],message.group_id,message.user_id,str(message.reply_to),row["id"],digest))
        message.document_job_id=row["id"]
