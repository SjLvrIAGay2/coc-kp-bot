"""Read-only creation capabilities and authenticated, recipient-visible dice."""
import json


CREATION_SUBMISSION = (
    "对话可以讨论和收集角色资料；正式提交需上传本人填写的角色卡文件，已导入的原文件也可按本人指定字段修改后重新提交。"
    "基础属性草稿保存初始骰据，后续聊天不会改写它；其中年龄调整待处理、幸运未定案只描述这份初始记录。"
    "年龄、年龄调整依据、幸运选择和最终数值随角色卡一起提交核对，没有独立的年龄登记或幸运确认接口，"
    "不要要求等待这些不存在的回执或声称已通过聊天补全草稿。"
    "reference_roll_receipts保存最近至多8条本人可见的真实参考骰（历史可能还有），仅证明掷骰者、时间、骰式及结果，未自动绑定用途、结算检定或修改角色。"
    "可结合原请求核对实际用途，把合法创建骰据随卡提交；不能仅因初始记录未更新而要求重骰，"
    "也不能替玩家挑选有利骰组、改数或把口述结果当真实骰据。"
)


def creation_roll_receipts(store, actor, cid, *, private):
    # Filter recipient scope in SQL before the bounded window, so private
    # rolls cannot displace public evidence or leak through a manager role.
    rows = store.db.execute(
        "SELECT id,action_id,visibility,payload FROM events WHERE campaign_id=? "
        "AND actor_id=? AND kind='dice_roll' AND (json_extract(visibility,'$.scope')='public' "
        "OR (? AND json_extract(visibility,'$.scope')='private' AND EXISTS "
        "(SELECT 1 FROM json_each(events.visibility,'$.recipients') WHERE value=?))) "
        "ORDER BY state_version DESC LIMIT 8", (cid, actor.user_id, private, actor.user_id)).fetchall()
    result = []
    for row in reversed(rows):
        payload = json.loads(row['payload'])
        if payload.get('standalone') is not True or payload.get('usage') != 'reference_only':
            continue
        rolls = payload.get('rolls', [payload.get('roll')])
        for roll in rolls:
            if (not isinstance(roll, dict) or roll.get('actor_id') != actor.user_id
                    or roll.get('campaign_id') != cid or roll.get('group_id') != actor.group_id):
                continue
            result.append({"source_event_id": row['id'], "source_request_id": row['action_id'],
                "visibility": json.loads(row['visibility']), "usage": "reference_only",
                "applied_to_character": False,
                **{k: roll[k] for k in ('request_id', 'actor_id', 'expression', 'total', 'source', 'rolled_at', 'reason')}})
    return result[-8:]
