"""Windows/Linux terminal, replay, migration, backup and HTTP server entry points."""
from __future__ import annotations

import argparse
from contextlib import closing
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from cockp.core.contracts import DomainError,Role,Proposal,can_view
from cockp.core.engine import Engine
from cockp.ai.llm import DeepSeekProvider,FakeLLMProvider
from cockp.ai.context_budget import DEFAULT_HOSTING_CONTEXT_TOKENS
from cockp.runtime.service import CampaignService
from cockp.runtime.messaging.inbound import Message, parse_command
from cockp.runtime.store import Store,dumps


def display(result,user_id="10001",manager=False):
    if result.get("error"): print("错误："+result["error"])
    for output in result.get("outputs",[]):
        vis=output.get("visibility",{"scope":"public"})
        if can_view(vis,user_id,manager):
            prefix="" if vis["scope"]=="public" else f"【{vis['scope']}：仅本终端授权视图】"
            print(prefix+output["text"])


def make_service(database,real=False):
    store=Store(database)
    try:
        store.acquire_runtime()
        store.recover()
        saved=store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:model'").fetchone()
        provider=DeepSeekProvider(max_context_tokens=DEFAULT_HOSTING_CONTEXT_TOKENS,
                                 state=json.loads(saved[0]) if saved else None) if real else FakeLLMProvider()
        service=CampaignService(Engine(store),provider,admin_ids=os.getenv("COCKP_ADMIN_IDS","10001").split(","),
                                group_ids=os.getenv("COCKP_GROUP_IDS","20001").split(","))
    except BaseException:
        store.close()
        raise
    return store,service


def import_args(command,args):
    """Filesystem imports are only exposed to the operator's local terminal."""
    if command=="load_module" and "path" in args:
        from cockp.documents.scenario import import_scenario
        scenario=import_scenario(args["path"],allowed_root=Path.cwd())
        print("【KP 准备摘要】"+json.dumps(scenario.preparation_summary(),ensure_ascii=False,indent=2))
        return {"module":scenario.model_dump(mode="json")}
    if command in {"import_character","revise_character"} and "path" in args:
        from cockp.documents.characters import import_character
        imported=import_character(args["path"],allowed_root=Path.cwd())
        if imported.card is None or imported.needs_confirmation:
            print(json.dumps(imported.model_dump(mode="json"),ensure_ascii=False,indent=2))
            raise DomainError("提取结果须核对并另存 JSON 后再导入；原卡不被改写")
        return {**{k:v for k,v in args.items() if k!="path"},"card":imported.card.model_dump(mode="json")}
    return args


async def terminal(args):
    from cockp.tools.simulation import LocalSession,parse_local_line
    def open_session():
        saved_store=Store(args.database)
        saved=saved_store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:model'").fetchone()
        saved_state=json.loads(saved[0]) if saved else None
        saved_store.close()
        provider=DeepSeekProvider(max_context_tokens=DEFAULT_HOSTING_CONTEXT_TOKENS,state=saved_state) if args.real else FakeLLMProvider()
        return LocalSession(args.database,provider,groups=os.getenv("COCKP_GROUP_IDS","20001").split(","),
                            admins=os.getenv("COCKP_ADMIN_IDS","10001").split(","))
    session=open_session();store,service=session.store,session.service
    try:
        await service.resume_document_jobs()
        user=args.user;group=args.group
        print("COC7 本地群聊模拟；身份由本机操作者切换，不代表QQ认证。exit 退出。")
        print("模型："+("真实 deepseek-flash（会产生 API 调用）" if args.real else "FakeLLMProvider（离线模拟，无真实主持能力声明）"))
        print("普通发言默认不@KP。用 10002 > 发言 切换玩家；10002@20001 > 切换群。")
        print("@KP 发言 / [私聊]发言 / [机器人]发言 / [引用:消息号]发言；/help 查看当前下一步。")
        print("/上传 文件路径 | 资料用途；/等待后台；/模拟发送失败 timeout或failed；/重启 可验证恢复。")
        while True:
            try: line=await asyncio.to_thread(input,f"{user}@{group}> ")
            except EOFError: break
            if line.strip() in {"exit","quit"}: break
            line,options=parse_local_line(line,user,group);user,group=options['user'],options['group']
            if not line.strip(): continue
            rid=uuid.uuid4().hex
            try:
                if line=="/等待后台":
                    await session.drain();result={"outputs":[]}
                elif line=="/重启":
                    files=dict(session.gateway.files)
                    await session.close();session=open_session();store,service=session.store,session.service
                    session.gateway.files.update(files)
                    await service.resume_document_jobs()
                    print("已重新载入存档及排队任务；可用 /状态、/资料 查看。")
                    continue
                elif line.startswith("/模拟发送失败 "):
                    failure=line.partition(" ")[2].strip()
                    if failure not in {'timeout','failed'}:raise DomainError("用 /模拟发送失败 timeout 或 /模拟发送失败 failed")
                    session.gateway.receipts.append(failure);print("下一次发送将模拟："+failure);continue
                elif line.startswith("/上传 "):
                    path,_,description=line[4:].partition(" | ")
                    attachment=session.gateway.attach(path.strip().strip('"'))
                    result=await session.receive(description or "这份资料请帮我整理",files=[attachment],message_id=rid,**options)
                elif line.startswith("/") and '"path"' in line:
                    cmd,kw=parse_command(line)
                    campaign=store.active(group)
                    actor=service.actor(user,group,campaign["state"] if campaign else None)
                    if "path" in kw and actor.role not in {Role.ADMIN,Role.OWNER}: raise DomainError("终端文件导入需要管理者权限")
                    result=await service.command(actor,campaign["id"] if campaign else None,rid,cmd,import_args(cmd,kw))
                else:
                    result=await session.receive(line,message_id=rid,**options)
                display(result,user,user in service.admin_ids)
                for sent in session.gateway.sent:
                    target=str(sent.get('user_id',sent.get('group_id','')))
                    if ('user_id' in sent and target==user) or ('group_id' in sent and target==group):
                        # Immediate result was just printed; background replies are
                        # available through /资料 and the durable delivery records.
                        if line=="/等待后台":
                            print("【模拟送达 · 消息号 "+sent['message_id']+"】"+"".join(s.get('data',{}).get('text','') for s in sent.get('message',[])))
                        else:
                            print("【模拟送达 · 消息号 "+sent['message_id']+"】")
                session.gateway.sent.clear()
            except (ValueError,KeyError) as exc:
                print("错误："+(str(exc) if isinstance(exc,DomainError) else "无法读取这条输入；用 /help 查看示例，资料可用 /上传 路径 | 用途 重新提交。"))
    finally:
        await session.close()


def require_empty_simulation_database(database,*,resume_simulation=False):
    """Fixed demo dice must never be attached to an existing campaign database."""
    path=Path(database)
    if str(database)==":memory:" or not path.exists(): return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
            states=[json.loads(row[0]) for row in db.execute("SELECT state FROM campaigns")]
            if states and not (resume_simulation and all(s.get("runtime_mode")=="offline_simulation" for s in states)):
                raise DomainError("模拟/回放必须使用空数据库；只有带离线标记的回放库可显式 --resume-simulation，不能把确定性骰子用于正式战役")
    except sqlite3.DatabaseError:
        raise DomainError("模拟数据库不是已识别的空战役库，请使用新路径") from None


def restore_database(source_path,destination_path):
    source=Path(source_path).resolve();target=Path(destination_path).resolve()
    if not source.is_file(): raise DomainError("恢复源备份文件不存在")
    if target.exists() or source==target: raise DomainError("恢复目标必须是尚不存在的新文件，避免覆盖原始日志")
    try:
        with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)) as original:
            if original.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise DomainError("备份完整性检查失败")
            tables={row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"campaigns","events","schema_migrations"}<=tables: raise DomainError("文件不是 COC KP 备份")
            target.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(target)) as restored: original.backup(restored)
    except sqlite3.DatabaseError:
        raise DomainError("恢复源不是有效 SQLite 备份，原文件未修改") from None
    return target


async def replay_file(path,database,quiet=False,*,resume_simulation=False):
    require_empty_simulation_database(database,resume_simulation=resume_simulation)
    store,service=make_service(database)
    from cockp.tools.demo import DemonstrationRandom
    from cockp.dice.dice import DiceService
    service.engine.dice=DiceService(DemonstrationRandom())
    event_references={row["action_id"]:row["id"] for row in store.db.execute("SELECT action_id,id FROM events ORDER BY seq")}
    results=[]
    try:
        for number,line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(),1):
            if not line.strip() or line.startswith("#"): continue
            step=json.loads(line)
            if step.get("op")=="fake_proposal":
                if not isinstance(service.llm,FakeLLMProvider): raise DomainError("回放提案仅可用于 FakeLLMProvider")
                service.llm.script.append(Proposal.model_validate(step["proposal"]))
                results.append({"simulation":"scripted_fake_proposal"});continue
            if step.get("op")=="restart":
                await service.close();store.close();store,service=make_service(database);service.engine.dice=DiceService(DemonstrationRandom());results.append({"restart":"ok"});continue
            if step.get("op")=="sleep":
                await asyncio.sleep(min(float(step["seconds"]),2));continue
            user=str(step.get("user_id","10001"));group=str(step.get("group_id","20001"))
            cid=step.get("campaign_id") or (store.active(group) or {}).get("id")
            if step.get("op")=="message":
                result=await service.receive(Message(step.get("id",f"replay-{number}"),group,user,step["text"],time.time(),mentioned=step.get("mentioned",False)))
            else:
                actor=service.actor(user,group,store.campaign(cid,group)["state"] if cid and step["command"]!="create" else None)
                arguments=step.get("arguments",{})
                if "path" in arguments and actor.role not in {Role.ADMIN,Role.OWNER}: raise DomainError("终端文件导入需要管理者权限")
                def resolve(value):
                    if isinstance(value,str) and value.startswith("$event:"):
                        if value[7:] not in event_references: raise DomainError("回放引用了尚不存在的事件")
                        return event_references[value[7:]]
                    if isinstance(value,dict): return {k:resolve(v) for k,v in value.items()}
                    if isinstance(value,list): return [resolve(v) for v in value]
                    return value
                result=await service.command(actor,cid,step.get("id",f"replay-{number}"),step["command"],import_args(step["command"],resolve(arguments)))
                if step["command"]=="create":
                    created_id=result["campaign_id"]
                    def mark_simulation():
                        with store.transaction() as db:
                            simulation_state=store.campaign(created_id,group)["state"]
                            simulation_state["runtime_mode"]="offline_simulation"
                            db.execute("UPDATE campaigns SET state=? WHERE id=?",(dumps(simulation_state),created_id))
                    await store.arun(mark_simulation)
            if result.get("error") and not step.get("expect_error"): raise DomainError(f"回放第 {number} 步失败："+result["error"])
            results.append(result)
            if result.get("event_id"): event_references[step.get("id",f"replay-{number}")]=result["event_id"]
            if not quiet: display(result,user,user in service.admin_ids)
        return results
    finally:
        await service.close()
        store.close()


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"): stream.reconfigure(encoding="utf-8")
    load_dotenv(override=False)
    parser=argparse.ArgumentParser(description="COC7 QQ KP：可信规则与本地模拟")
    parser.add_argument("--database",default=os.getenv("COCKP_DATABASE","data/cockp.sqlite3"))
    sub=parser.add_subparsers(dest="mode",required=True)
    term=sub.add_parser("terminal",help="可交互的多人终端")
    term.add_argument("--real",action="store_true");term.add_argument("--user",default="10001");term.add_argument("--group",default="20001")
    serve=sub.add_parser("serve",help="启动 FastAPI/OneBot HTTP")
    serve.add_argument("--host",default="127.0.0.1");serve.add_argument("--port",type=int,default=8080)
    replay=sub.add_parser("replay",help="离线重放 JSONL");replay.add_argument("path");replay.add_argument("--quiet",action="store_true")
    replay.add_argument("--resume-simulation",action="store_true",help="只恢复由回放工具明确标记的离线模拟库")
    demo=sub.add_parser("demo",help="完整三玩家演示");demo.add_argument("--variant",choices=["full","detour","timeout"],default="full")
    imp=sub.add_parser("import-rules",help="只读导入规则书全部页面");imp.add_argument("path");imp.add_argument("--output",default="data/private/rulebook")
    backup=sub.add_parser("backup",help="SQLite 一致性备份");backup.add_argument("destination")
    restore=sub.add_parser("restore",help="停服后从备份恢复到一个新数据库文件");restore.add_argument("source");restore.add_argument("destination")
    sub.add_parser("migrate",help="创建/迁移数据库")
    args=parser.parse_args(argv)
    try:
        if args.mode=="terminal": asyncio.run(terminal(args))
        elif args.mode=="serve":
            os.environ["COCKP_DATABASE"]=args.database
            import uvicorn
            from cockp.interfaces.app import create_app
            uvicorn.run(create_app(),host=args.host,port=args.port,log_level="warning")
        elif args.mode=="replay":
            result=asyncio.run(replay_file(args.path,args.database,args.quiet,resume_simulation=args.resume_simulation));print(f"回放完成：{len(result)} 个步骤。")
        elif args.mode=="demo":
            require_empty_simulation_database(args.database)
            from cockp.tools.demo import run_demo
            result=asyncio.run(run_demo(args.database,args.variant));print(json.dumps(result,ensure_ascii=False,indent=2))
        elif args.mode=="import-rules":
            from cockp.documents.importers import import_rulebook
            result=import_rulebook(args.path,args.output)
            print(json.dumps(result,ensure_ascii=False,indent=2))
        elif args.mode=="backup":
            if not Path(args.database).is_file(): raise DomainError("备份源数据库不存在，请先建团或迁移")
            store=Store(args.database);print(store.backup(args.destination));store.close()
        elif args.mode=="restore":
            target=restore_database(args.source,args.destination)
            print("恢复完成，请将 COCKP_DATABASE 指向："+str(target))
        else:
            store=Store(args.database);store.close();print("数据库迁移完成。")
    except (DomainError,ValueError,KeyError) as exc:
        print("失败："+(str(exc) if isinstance(exc,DomainError) else "输入或数据验证失败"),file=sys.stderr)
        raise SystemExit(1)


if __name__=="__main__": main()
