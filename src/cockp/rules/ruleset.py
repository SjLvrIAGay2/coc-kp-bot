"""Versioned configuration, separate from narrative style."""
from copy import deepcopy
from cockp.core.contracts import DomainError

CORE = {"luck_spending": False, "chase_optional": False, "pulp": False, "max_skill": 99, "allow_mythos_creation": False,
        "creation_methods": ["rolled"], "max_import_bytes": 10_000_000,
        "secret_atmosphere": True, "secret_atmosphere_interval": 60,
        "dice_source": "builtin", "images_enabled": False}
ALLOWED = set(CORE)


def initial_ruleset():
    return {"version": "core-1", "core": deepcopy(CORE), "optional": {}, "house": {}, "module": {}, "history": []}


def effective(ruleset):
    out = {}
    for layer in ("core", "optional", "house", "module"):
        out.update(ruleset[layer])
    return out


def configure(ruleset, layer, updates, actor_id, explanation):
    if layer not in ("optional", "house", "module"):
        raise DomainError("默认核心规则不可修改；请选择 optional/house/module 层")
    if not updates or set(updates) - ALLOWED or not explanation.strip():
        raise DomainError("未知规则项或缺少向玩家说明的理由")
    for key, value in updates.items():
        expected = type(CORE[key])
        if type(value) is not expected:
            raise DomainError("规则配置类型不匹配")
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            raise DomainError("规则数值不能为负")
        if key == "max_skill" and not 1 <= value <= 99:
            raise DomainError("创建技能上限必须在 1..99")
        if key == "dice_source" and value not in ("builtin", "external"):
            raise DomainError("骰子来源必须为 builtin/external")
        if key == "pulp" and value:
            raise DomainError("本项目依据所提供核心规则书；尚未实现 Pulp 补充规则")
    ruleset[layer].update(updates)
    ruleset["history"].append({"actor_id": actor_id, "layer": layer, "updates": updates, "explanation": explanation})
    ruleset["version"] = "core-1+r" + str(len(ruleset["history"]))
