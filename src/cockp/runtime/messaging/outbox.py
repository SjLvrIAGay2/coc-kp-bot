"""Claim and deliver committed messages; unknown sends never auto-replay."""
import asyncio
from cockp.core.contracts import DomainError
from cockp.runtime.store import dumps

class OutboxDispatcher:
    def __init__(self,store,adapter,interval=.5):
        self.store,self.adapter,self.interval=store,adapter,interval
        self.lock=asyncio.Lock()

    async def flush(self,limit=30):
        async with self.lock:
            sent=[]
            for _ in range(limit):
                row=await self.store.arun(self._claim)
                if not row: break
                kind,target=row["destination"].split(":",1)
                try:
                    if row["event_id"].startswith(("image:","config-image:")):
                        receipts=await self.adapter.send(target,private=kind=="user",segments=[{"type":"image","data":{"file":row["content"]}}])
                    else:
                        receipts=await self.adapter.send(target,row["content"],private=kind=="user")
                    records=[r.model_dump(mode="json") if hasattr(r,"model_dump") else r for r in receipts]
                    if kind=="group" and getattr(self.adapter,"self_id",None):
                        from cockp.integrations.group_admin import GroupAdmin
                        admin=GroupAdmin(self.store,self.adapter)
                        for record in records:
                            if record.get("status")=="sent" and record.get("message_id"):
                                try:await self.store.arun(admin.record_message,target,record["message_id"],self.adapter.self_id)
                                except (DomainError,ValueError):pass
                    statuses=[r.get("status","unknown") for r in records]
                    status="sent" if statuses and all(s in {"sent","ok"} for s in statuses) else "unknown" if "unknown" in statuses else "failed"
                except (TimeoutError,ConnectionError):
                    status,records="unknown",[]
                except Exception:
                    status,records="failed",[]
                await self.store.arun(self._finish,row["id"],status,records)
                sent.append({"id":row["id"],"status":status})
                if self.interval: await asyncio.sleep(self.interval)
            return sent

    def _claim(self):
        with self.store.transaction() as db:
            row=db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?",(row["id"],))
            return dict(row) if row else None

    def _finish(self, ident, status, records):
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status=?,receipt=? WHERE id=?",(status,dumps(records),ident))
            if status=="sent":
                from cockp.runtime.external_delivery import bind_external_command_receipt
                bind_external_command_receipt(db,ident)
