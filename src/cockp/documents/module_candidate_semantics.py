"""Static, shared field meanings for source-bound generation and review."""
from __future__ import annotations

import hashlib
from typing import Final


CANDIDATE_SOURCE_COPY_WIRE_RULES: Final[str] = (
    "以下只规定Map或修订生成JSON时的复制占位格式，不用于评判已编译候选："
    "source_mode=copy时value必须为null，sources至少一项，由程序完整展开所选来源。"
    "source_mode=copy_text时value.text必须为null，其余成员按schema提供，sources至少一项。"
    "生成非空Clue.discovery必须选择copy；literal分支只允许value为空字符串，不能提交非空概括或复制文本。"
)


CANDIDATE_FIELD_SEMANTICS: Final[str] = (
    "模组字段的固定语义：Clue.name是玩家合法取得该线索后可见的中性展示索引，不是技术ID或新增知识。"
    "可用原文完整支持的简短概括，不要求原作者给出逐字题名；原文没有题名不等于没有来源。"
    "名称中的实际含义、指代、因果、唯一性、身份、人物说法或幻觉的认识层级及公开范围都须有完整来源支持，"
    "不能借中性标签泄露秘密或把角色所见所信升级为客观事实。"
    "Clue.scene_id只对应原文规定的固定取得场景；须结合完整上下文确认没有强制唯一固定取得场景，才显式为null。"
    "不能仅凭所选短段没写地点就判null，也不能用null表示尚未查明或尚未选择的地点。"
    "null不表示随处可得、已经取得或可以公开；原文各地点分支、取得途径、检定及成功条件仍须完整保留在discovery或access_condition。"
    "来源置信度衡量所填值与所选原文的完整语义支持，不是逐字相同程度；概括、无原题名和null都不免除来源义务。"
    "真实疑问仍须如实保留低信，不得建议、默认或强制某个分数，不得为了通过更改或省略分数。"
    "Clue.discovery是完整获得协议，须保留所选原文中的全部替代途径、"
    "前置条件、检定及成功条件、例外和作者可选方案；不能用一个概括动作替代多个获得分支。"
    "非空discovery使用source_mode=copy逐字保留完整所选来源；这不会自动满足条件或取得线索。"
    "审核目标、候选账本和Final使用的是已编译值：程序已将copy的null占位展开为所选来源的完整原文字符串，"
    "copy_text的text也已展开；source_mode保留生成来源信息，不要求已编译的value或text仍为null。"
    "审核须核对展开值与完整所选来源、上下文、获得协议和受众，不得仅因正确展开后的值非空而否决，"
    "也不得把复制成功当作语义支持、条件满足或公开授权。"
    "Clue.public_description只包含在该条线索的同一合法获得门槛下已经能知道的内容；"
    "另一次行动、检定、对话或后续事件才会获知的内容不得提前合入。"
    "不同知识门槛需要模型显式建立独立线索并保留各自原文依据，不能程序拆段或猜补世界事实。"
    "取得协议须核对完整原文跨段的统领条件与后续说明，不能只取清单条目或检定短句。"
    "括注、例外和可选分支若限制某句陈述的适用范围，公开文字也不得将它改成无条件事实；"
    "尚待实际状态选择的私密分支保留在取得协议内，不预判玩家关系、选择、检定或后续事件。"
    "原文明示带取得条件的独立线索或调查所得，须登记供正常获取机制使用的对应线索及完整discovery，"
    "不能仅复制进KP正文；普通静态环境文字不强造线索或实体。"
    "KP开放事件候选筛选及激活入口会用TimelineEvent.scene_id约束当前角色所在地，"
    "不能用结果地点代替该触发地点；该路径没有原文触发位置限制时显式为null。"
    "普通固定时钟world_tick不凭scene_id自动添加位置前置；原文要求的地点条件仍须在condition中明确保留。"
    "结果位置由原有kp_effect或已支持的effects等对应字段表达，不能挪成触发条件，不能凭空构造数值效果。"
    "TimelineEvent.condition与Ending.condition的原文可逐字保留为私密功能文本；"
    "复制并不让自然语言条件变成已满足事实、开放触发或公开权限，仍须原有条件判定和独立审核。"
    "原文明示的每个结局分支均需可独立装配的Ending.condition候选，保留该分支的完整前置、成功/失败和替代条件；"
    "另一结局的条件、special_rule或完整正文副本不能代替本结局的必填触发字段。"
    "候选在Final中只可选择一次；同一原文支持多个实体时可分别选择真实来源生成各自候选，不能共享一个候选引用或猜补原文没有的结局。"
    "引用字段有明确阶段边界：Map、Forest和修订中的scene_id/connections/linked_clues仍是有来源的地点称呼或候选符号，"
    "不是已安装实体ID。对于同一原文地点，名称与local_id写法不同本身不等于取得地点矛盾。"
    "Forest应核对原文地点语义与是否存在可明确关联的真实地点候选；缺少对应场景、地点含义歧义或固定门槛错误仍须否决。"
    "只有Final模型按其固定目录显式填写relation=entity/entities及targets后，程序才将选定目标转换成技术ID；"
    "程序不按名称猜目标或自动统一原值。单纯尚待Final完成的技术绑定不属于修订未解决问题，也不授权修订未被否定的旧字段。"
    "null仍表示原文没有固定地点，不表示等待绑定或尚未查明。完整Whole与SSP须核对编译后实际绑定是否忠实于原文，"
    "不能把技术绑定完成当作来源支持、已取得或公开许可；有真实未决歧义仍须如实保留并停止。"
    "所有复制、索引、公开拆分与审核均须保留原文的否定、条件、传闻、人物说法、未知和可选性；"
    "不得将它们升级为无条件、唯一或已经发生的客观事实。"
)


def candidate_field_semantics_contract():
    return {"policy": "shared-candidate-field-semantics-v7", "text": CANDIDATE_FIELD_SEMANTICS,
            "text_sha256": hashlib.sha256(CANDIDATE_FIELD_SEMANTICS.encode("utf-8")).hexdigest(),
            "generation_wire_rules": CANDIDATE_SOURCE_COPY_WIRE_RULES,
            "generation_wire_rules_sha256": hashlib.sha256(CANDIDATE_SOURCE_COPY_WIRE_RULES.encode("utf-8")).hexdigest()}
