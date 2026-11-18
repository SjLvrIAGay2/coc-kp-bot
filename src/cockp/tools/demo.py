"""Offline integration demonstration using real rules/store, a scripted model and test RNG.

The demo is explicitly a simulation. It is not a hosting-quality evaluation of
DeepSeek and cannot be selected as a production RNG through an API command.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from cockp.documents.characters import CharacterCard
from cockp.core.contracts import Actor,Role,Proposal,DomainError
from cockp.dice.dice import DiceService
from cockp.core.engine import Engine
from cockp.ai.llm import FakeLLMProvider
from cockp.documents.scenario import import_scenario
from cockp.runtime.service import CampaignService
from cockp.runtime.messaging.inbound import Message
from cockp.runtime.store import Store


class DemonstrationRandom:
    def randint(self,a,b):
        # Visible fixed simulation: median percentile, minimal other die face.
        return 37 if (a,b)==(1,100) else a


async def run_demo(database="data/demo.sqlite3",variant="full",*,record=True):
    project_root = os.getenv("COCKP_PROJECT_ROOT")
    examples = (Path(project_root) / "examples" if project_root else Path(__file__).parent / "demo_data").resolve()
    scenario=import_scenario(examples/"scenario.json",allowed_root=examples)
    cards=[CharacterCard.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted((examples/"characters").glob("*.json"))]
    if len(cards)<3: raise DomainError("完整演示需要三张示例角色卡")
    cards=cards[:3]
    cid="demo-"+variant
    gm=Actor(user_id="10001",group_id="20001",role=Role.ADMIN)
    players={c.id:Actor(user_id=c.owner_id,group_id="20001") for c in cards}
    store=Store(database)
    engine=Engine(store,DiceService(DemonstrationRandom()))
    fake=FakeLLMProvider()
    service=CampaignService(engine,fake,admin_ids=["10001"],group_ids=["20001"])
    transcript=[];steps=[]

    async def step(command,arguments=None,who=None,id=None):
        actor=who or gm;rid=id or f"{variant}-{len(steps)+1:03}"
        result=await service.command(actor,cid,rid,command,arguments or {})
        if result.get("error"): raise DomainError(result["error"])
        steps.append({"id":rid,"user_id":actor.user_id,"group_id":gm.group_id,"campaign_id":cid,"command":command,"arguments":arguments or {}})
        transcript.append({"step":len(steps),"command":command,"user_id":actor.user_id,"result":result})
        return result
    try:
        await step("create",{"id":cid,"name":"雾港铜铃 / "+variant})
        await step("load_module",{"module":scenario.model_dump(mode="json")})
        for card in cards:
            await step("import_character",{"card":card.model_dump(mode="json")},players[card.id])
            await step("review",{"character_id":card.id,"confirmation_note":"离线演示：已核对原创背景、时代装备与骰值来源。"})
        await step("persona",{"name":"guide"})
        await step("boundary",{"text":"不描写儿童受害与详细身体伤害"},players[cards[0].id])
        await step("start",{"preparation_note":"已核对准备摘要与所有问题；本模组为原创离线测试资料。"})
        first=cards[0];second=cards[1];third=cards[2]
        opening_npc=next((n for n in scenario.npcs if n.scene_id==scenario.start_scene),scenario.npcs[0])
        if opening_npc.scene_id!=scenario.start_scene:
            await step("move",{"character_id":first.id,"scene_id":opening_npc.scene_id},players[first.id])
        npc_reply=Proposal(kind="say",text="我看见那位搬运工往仓库去了。更晚的事情我就不知道了。")
        fake.script.append(npc_reply)
        steps.append({"op":"fake_proposal","proposal":npc_reply.model_dump(mode="json")})
        await step("interact",{"character_id":first.id,"npc_id":opening_npc.id,"topic":"询问最后一次听见铜铃的时间","roleplay":True},players[first.id])
        await step("place_item",{"scene_id":scenario.start_scene,"item_id":"lamp","name":"煤油灯","source":"原创演示：路灯下备用灯，可随手取用"})
        await step("routine",{"character_id":first.id,"operation":"take","item_id":"lamp"},players[first.id])
        public=next((c for c in scenario.clues if c.visibility.scope=="public"),scenario.clues[0])
        private=next((c for c in scenario.clues if c.visibility.scope=="private"),scenario.clues[-1])
        await step("investigate",{"character_id":first.id,"clue_id":public.id},players[first.id])
        destination=next(s for s in scenario.scenes if s.id!=scenario.start_scene and not s.entry_conditions)
        await step("move",{"character_id":second.id,"scene_id":destination.id},players[second.id])
        await step("reveal",{"clue_id":private.id,"visibility":"private","recipients":[second.owner_id]})
        await step("note",{"text":"玩家猜测铜铃是走私者的信号，尚未证实。","kind":"character_guess","visibility":"public"},players[third.id])
        if variant=="detour":
            detour=next(s for s in scenario.scenes if s.id=="riverside")
            await step("move",{"character_id":third.id,"scene_id":detour.id},players[third.id])
            await step("note",{"text":"调查员选择绕过原定会面，沿河岸寻找独立入口。","kind":"improvisation","visibility":"public"})
        if variant=="timeout":
            await step("external_config",{"trusted_user_id":"90001","protocol":"sim-text-v1","timeout_seconds":.01,"timeout_policy":"internal","unsupported_policy":"internal"})
            await step("rules",{"layer":"house","updates":{"dice_source":"external"},"explanation":"模拟骰娘超时降级验收"})
        proposal=Proposal(kind="check",character_id=first.id,skill="侦查",reason="对照公开足迹与现场门锁",risk="失败会耽误十分钟")
        fake.script.append(proposal)
        steps.append({"op":"fake_proposal","proposal":proposal.model_dump(mode="json")})
        model_result=await service.receive(Message("dock-check",gm.group_id,first.owner_id,"跑团:我对照足迹检查门锁",mentioned=True))
        if model_result.get("error"): raise DomainError(model_result["error"])
        steps.append({"op":"message","id":"dock-check","user_id":first.owner_id,"group_id":gm.group_id,"text":"跑团:我对照足迹检查门锁","mentioned":True})
        transcript.append({"step":len(steps),"command":"fake_model_proposal_validated","result":model_result})
        await step("roll",{"action_id":"dock-check"},players[first.id])
        if variant=="timeout":
            await asyncio.sleep(.02)
            steps.append({"op":"sleep","seconds":.02})
            await step("external_timeout",{"action_id":"dock-check"},players[first.id])
        await step("move",{"character_id":first.id,"scene_id":"warehouse"},players[first.id])
        await step("combat_start",{"characters":[first.id],"enemies":[{"id":"guard","name":"慌乱的守门人","dex":20,"hp":8,"dodge":15}]})
        await step("combat_attack",{"character_id":first.id,"target_id":"guard"},players[first.id])
        await step("note",{"text":"守门人退开，让出狭窄通道，选择向巷子逃跑。","kind":"improvisation","visibility":"public"})
        await step("combat_end")
        await step("move",{"character_id":first.id,"scene_id":"riverside"},players[first.id])
        # Speed roll succeeds on both sides and keeps their MOV equal, allowing
        # the demo to exercise actual pursuit AP rather than a speed-only escape.
        mov=store.campaign(cid)["state"]["characters"][first.id]["mov"]
        dex=store.campaign(cid)["state"]["characters"][first.id]["attributes"]["DEX"]
        await step("chase_start",{"participants":[{"id":first.id,"role":"pursuer"},{"id":"runner","role":"escapee","mov":mov,"dex":max(1,dex-5),"con":60}],
                                  "obstacles":[{"id":"wet_steps","after_position":0,"kind":"hazard","difficulty":"regular"}],
                                  "hazard_effects":{"wet_steps":{"skill":"跳跃","damage":"1D3"}}})
        await step("chase_move",{"character_id":first.id,"obstacle_id":"wet_steps","action":"move"},players[first.id])
        await step("chase_move",{"character_id":first.id,"action":"end_turn"},players[first.id])
        await step("chase_move",{"character_id":"runner","action":"move"})
        ruling=await step("resolve_manual",{"source":"原创模组：码头岔路","reason":"逃跑者钻进岔道，调查员决定返回证据地点，不再追赶。","decision":"逃跑者与调查员失去接触。"})
        await step("chase_escape",{"character_id":"runner","adjudication_id":ruling["event_id"]})
        steps[-1]["arguments"]["adjudication_id"]="$event:"+ruling["action_id"]
        await step("move",{"character_id":first.id,"scene_id":"warehouse"},players[first.id])
        await step("move",{"character_id":first.id,"scene_id":"clocktower"},players[first.id])
        await step("sanity",{"character_id":first.id,"success_loss":"1","failure_loss":"1D6","mythos_source":False})
        await step("save")
        before=store.campaign(cid)
        await service.close()
        store.close()
        store=Store(database);store.recover()
        engine=Engine(store,DiceService(DemonstrationRandom()))
        service=CampaignService(engine,FakeLLMProvider(),admin_ids=["10001"],group_ids=["20001"])
        assert store.campaign(cid)==before,"重启恢复状态不一致"
        steps.append({"op":"restart"});transcript.append({"restart":"verified","version":before["version"]})
        await step("recap",who=players[first.id])
        await step("end",{"ending_id":"evidence"})
        marks=store.campaign(cid)["state"]["characters"][first.id].get("growth_marks",[])
        for skill in marks.copy():
            await step("growth",{"character_id":first.id,"skill":skill},players[first.id])
        final=store.campaign(cid)
        events=store.db.execute("SELECT COUNT(*) FROM events WHERE campaign_id=?",(cid,)).fetchone()[0]
        roll_count=store.db.execute("SELECT COUNT(*) FROM rolls WHERE campaign_id=?",(cid,)).fetchone()[0]
        public_text="\n".join(o["text"] for entry in transcript for o in entry.get("result",{}).get("outputs",[]) if o["visibility"]["scope"]=="public")
        assert private.public_description not in public_text,"私人线索泄漏到公开回放"
        if record:
            output=Path(database).parent/"demo-artifacts";output.mkdir(parents=True,exist_ok=True)
            (output/f"{variant}-internal.json").write_text(json.dumps(transcript,ensure_ascii=False,indent=2),encoding="utf-8")
            (output/f"{variant}-public.txt").write_text(public_text,encoding="utf-8")
            (output/f"replay-{variant}.jsonl").write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in steps)+"\n",encoding="utf-8")
        return {"mode":"离线模拟，非真实模型主持验收","variant":variant,"campaign_id":cid,
                "players":len(cards),"phase":final["state"]["phase"],"events":events,"rolls":roll_count,
                "restart_verified":True,"private_clue_isolated":True,"database":str(database),
                "transcript":str(Path(database).parent/"demo-artifacts"/f"{variant}-public.txt") if record else None}
    finally:
        await service.close()
        store.close()
