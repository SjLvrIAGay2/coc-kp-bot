"""Deterministic CoC 7 numerical rules, verified against the supplied v1.2.1.

PDF references are one-based physical pages; Chinese printed page = PDF - 8
for the referenced core chapters. No function generates random numbers. The
caller obtains/audits dice first and commits results in a campaign transaction.
Open consequences are explicitly returned as adjudication requirements.
"""
from __future__ import annotations

from enum import IntEnum
from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cockp.dice.dice import expression_bounds

Difficulty = Literal["regular", "hard", "extreme", "critical", "impossible"]


class SuccessLevel(IntEnum):
    FUMBLE = -1
    FAILURE = 0
    REGULAR = 1
    HARD = 2
    EXTREME = 3
    CRITICAL = 4


class CheckResult(BaseModel):
    roll: int
    target: int
    difficulty: Difficulty
    threshold: int
    level: SuccessLevel
    success: bool
    label: str
    source: str = "PDF71,75-76 / 中文63,67-68"

    def __getitem__(self, key: str):
        return getattr(self, key)


def _integer(value: int, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须为 {low}–{high} 的整数")
    return value


def threshold(target: int, difficulty: Difficulty = "regular") -> int:
    _integer(target, 0, 10000, "技能/属性")
    if difficulty not in ("regular", "hard", "extreme", "critical", "impossible"):
        raise ValueError("未知难度")
    return {"regular": target, "hard": target // 2, "extreme": target // 5,
            "critical": 1, "impossible": 0}[difficulty]


def evaluate_check(roll: int, target: int, difficulty: Difficulty = "regular") -> CheckResult:
    _integer(roll, 1, 100, "百分骰")
    cutoff = threshold(target, difficulty)
    # This translation explicitly uses the effective required threshold, PDF75.
    if roll == 100 or (cutoff < 50 and roll >= 96):
        level = SuccessLevel.FUMBLE
    elif roll == 1:
        level = SuccessLevel.CRITICAL
    elif roll <= target // 5:
        level = SuccessLevel.EXTREME
    elif roll <= target // 2:
        level = SuccessLevel.HARD
    elif roll <= target:
        level = SuccessLevel.REGULAR
    else:
        level = SuccessLevel.FAILURE
    success = difficulty != "impossible" and level > 0 and (roll <= cutoff or roll == 1)
    labels = {-1: "大失败", 0: "失败", 1: "成功", 2: "困难成功", 3: "极难成功", 4: "大成功"}
    return CheckResult(roll=roll, target=target, difficulty=difficulty, threshold=cutoff,
                       level=level, success=success, label=labels[int(level)])


def opposition_difficulty(opposing_value: int) -> Difficulty:
    _integer(opposing_value, 0, 10000, "对方技能")
    return "extreme" if opposing_value >= 90 else "hard" if opposing_value >= 50 else "regular"


def resolve_opposed(a_roll: int, a_target: int, b_roll: int, b_target: int) -> dict:
    a, b = evaluate_check(a_roll, a_target), evaluate_check(b_roll, b_target)
    # Failure and fumble both fail their declared objective; neither creates success.
    ar, br = max(0, int(a.level)), max(0, int(b.level))
    winner = "a" if ar > br else "b" if br > ar else "a" if a_target > b_target else "b" if b_target > a_target else None
    return {"winner": winner, "a": a.model_dump(), "b": b.model_dump(),
            "stalemate": winner is None, "resolution": "僵局或双方重骰" if winner is None else "成功等级，同级比较技能"}


def combined_check(roll: int, targets: list[int], mode: Literal["all", "any"] = "all", difficulty: Difficulty = "regular") -> dict:
    if not targets or mode not in ("all", "any"):
        raise ValueError("组合检定需要技能及 all/any 模式")
    results = [evaluate_check(roll, value, difficulty) for value in targets]
    return {"success": (all if mode == "all" else any)(r.success for r in results),
            "checks": [r.model_dump() for r in results], "roll_count": 1}


def physical_cooperation(opposition: int, values: list[int], helpers: int) -> dict:
    """Lowest attributes reduce opposition; remaining investigators roll (PDF74–75)."""
    _integer(opposition, 1, 10000, "阻力")
    if not values or not 0 <= helpers < len(values):
        raise ValueError("必须至少保留一人检定")
    ordered = sorted(_integer(v, 1, 10000, "属性") for v in values)
    remaining = opposition - sum(ordered[:helpers])
    if remaining <= 0:
        raise ValueError("协作不能把阻力减到零或以下")
    return {"opposition": remaining, "difficulty": opposition_difficulty(remaining),
            "eligible": [v for v in ordered[helpers:] if v + 100 >= remaining]}


def validate_push(kind: str, *, already_pushed: bool = False, previous_success: bool = False,
                  extra_action: str = "", risk: str = "", consequence: str = "",
                  goal_possible: bool = True, previous_fumble: bool = False) -> dict:
    forbidden = {"luck", "sanity", "damage", "sanity_loss", "combat", "opposed", "chase", "growth"}
    if kind in forbidden or kind not in {"skill", "attribute", "spell_learning", "spell_casting"}:
        raise ValueError("此类检定不能孤注一骰")
    if already_pushed or previous_success or not goal_possible:
        raise ValueError("只能对仍可实现的失败目标孤注一次")
    if not extra_action.strip() or not risk.strip() or not consequence.strip():
        raise ValueError("须先声明额外行动、公开风险及已记录的失败后果")
    return {"allowed": True, "fumble_consequences_remain": previous_fumble,
            "extra_action": extra_action, "risk": risk, "consequence": consequence}


def spend_luck(roll: int, target: int, luck: int, points: int, *, enabled: bool = False,
               kind: str = "skill", difficulty: Difficulty = "regular", pushed: bool = False,
               malfunction: bool = False) -> dict:
    check = evaluate_check(roll, target, difficulty)
    _integer(luck, 0, 99, "幸运")
    _integer(points, 1, 99, "消耗幸运")
    if not enabled or kind not in {"skill", "attribute", "combat", "opposed"} or pushed:
        raise ValueError("未启用幸运消耗或此类检定不允许")
    if check.level in {SuccessLevel.CRITICAL, SuccessLevel.FUMBLE} or malfunction:
        raise ValueError("幸运不能修改大成功、大失败或故障")
    if points > luck or roll - points < 2:
        raise ValueError("幸运不足或试图制造大成功")
    changed = evaluate_check(roll - points, target, difficulty)
    return {"luck": luck - points, "roll": roll - points, "check": changed.model_dump(), "growth_mark": False}


def age_requirements(age: int) -> dict:
    _integer(age, 15, 89, "创建年龄")
    if age < 20:
        return dict(physical_deduction=5, physical_choices=["STR", "SIZ"], app_deduction=0,
                    edu_deduction=5, education_checks=0, luck_rolls=2, mov_penalty=0)
    band = max(0, age // 10 - 3)
    return dict(physical_deduction=[0, 5, 10, 20, 40, 80][band], physical_choices=["STR", "CON", "DEX"],
                app_deduction=5 * band, edu_deduction=0, education_checks=min(4, band + 1),
                luck_rolls=1, mov_penalty=band)


def apply_age(attributes: dict[str, int], age: int, deductions: dict[str, int],
              education_rolls: list[tuple[int, ...]]) -> dict[str, int]:
    values = {k.upper(): v for k, v in attributes.items()}
    req = age_requirements(age)
    if any(k not in req["physical_choices"] or v < 0 for k, v in deductions.items()):
        raise ValueError("年龄减值所选属性非法")
    if sum(deductions.values()) != req["physical_deduction"]:
        raise ValueError("年龄减值总数不符")
    if len(education_rolls) != req["education_checks"]:
        raise ValueError("教育增强检定次数不符")
    for key, value in deductions.items():
        values[key] -= value
    values["APP"] -= req["app_deduction"]
    values["EDU"] -= req["edu_deduction"]
    for roll in education_rolls:
        if not isinstance(roll, (list, tuple)) or len(roll) not in {1, 2}:
            raise ValueError("教育增强检定记录应为[D100]或[D100,D10]")
        d100 = roll[0]
        _integer(d100, 1, 100, "教育增强百分骰")
        if d100 > values["EDU"]:
            if len(roll) != 2 or roll[1] is None:
                raise ValueError("教育增强成功时须记录1D10增长值")
            d10 = roll[1]
            _integer(d10, 1, 10, "教育增长骰")
            values["EDU"] = min(99, values["EDU"] + d10)
        elif len(roll) == 2 and roll[1] is not None:
            _integer(roll[1], 1, 10, "教育增长骰")
    if any(v < 1 for v in values.values()):
        raise ValueError("年龄调整后属性不可低于1；需重新分配减值")
    return values


def derived_attributes(attributes: dict[str, int], age: int = 25) -> dict:
    a = {key.upper(): value for key, value in attributes.items()}
    for key in ("STR", "CON", "SIZ", "DEX", "POW"):
        _integer(a[key], 1, 10000, key)
    total = a["STR"] + a["SIZ"]
    if total <= 64:
        db, build = "-2", -2
    elif total <= 84:
        db, build = "-1", -1
    elif total <= 124:
        db, build = "0", 0
    elif total <= 164:
        db, build = "1D4", 1
    elif total <= 204:
        db, build = "1D6", 2
    else:
        build = 3 + (total - 205) // 80
        db = f"{build - 1}D6"
    mov = 7 if a["STR"] < a["SIZ"] and a["DEX"] < a["SIZ"] else 9 if a["STR"] > a["SIZ"] and a["DEX"] > a["SIZ"] else 8
    return {"hp": (a["CON"] + a["SIZ"]) // 10, "mp": a["POW"] // 5, "san": a["POW"],
            "mov": mov - age_requirements(age)["mov_penalty"], "build": build, "damage_bonus": db}


def creation_points(attributes: dict[str, int], formula: str = "EDU*4") -> dict:
    """Whitelisted occupation formula; never evaluate source text as code."""
    a = {k.upper(): v for k, v in attributes.items()}
    if formula == "EDU*4":
        career = a["EDU"] * 4
    elif formula in {f"EDU*2+{key}*2" for key in ("STR", "DEX", "APP", "POW")}:
        career = a["EDU"] * 2 + a[formula.split("+")[1].split("*")[0]] * 2
    elif formula == "EDU*2+MAX(DEX,STR)*2":
        career = a["EDU"] * 2 + max(a["DEX"], a["STR"]) * 2
    elif formula == "EDU*2+MAX(APP,DEX)*2":
        career = a["EDU"] * 2 + max(a["APP"], a["DEX"]) * 2
    else:
        raise ValueError("未登记职业公式，需管理员按来源审核")
    return {"occupation": career, "personal": a["INT"] * 2}


def resolve_melee(attack_roll: int, attack_skill: int, defense_roll: int, defense_skill: int,
                  defense: Literal["fight_back", "dodge", "maneuver"] = "fight_back") -> dict:
    if defense not in {"fight_back", "dodge", "maneuver"}:
        raise ValueError("未知防御方式")
    a, b = evaluate_check(attack_roll, attack_skill), evaluate_check(defense_roll, defense_skill)
    winner = None
    if a.level > 0 and (a.level > b.level or (a.level == b.level and defense != "dodge")):
        winner = "attacker"
    elif b.level > 0 and defense != "dodge" and b.level > a.level:
        winner = "defender"
    return {"winner": winner, "attacker": a.model_dump(), "defender": b.model_dump(),
            "extreme_damage": winner == "attacker" and a.level >= SuccessLevel.EXTREME,
            "defender_maneuver": winner == "defender" and defense == "maneuver"}


def maneuver_penalty(attacker_build: int, defender_build: int) -> int:
    difference = defender_build - attacker_build
    if difference >= 3:
        raise ValueError("体格差至少3，无法发动战技")
    return max(0, difference)


def combat_order(combatants: list[dict]) -> list[str]:
    """Equal DEX and combat skill remain a declared tie, stable input order."""
    return [c["id"] for c in sorted(combatants, key=lambda c: (-(c["dex"] + (50 if c.get("firearm_ready") else 0)), -c.get("combat_skill", 0)))]


def attack_damage(weapon_expression: str, weapon_roll: int, damage_bonus: str = "0", bonus_roll: int = 0,
                  *, extreme: bool = False, impaling: bool = False, fighting_back: bool = False) -> int:
    wmin, wmax = expression_bounds(weapon_expression)
    bmin, bmax = expression_bounds(damage_bonus)
    if not wmin <= weapon_roll <= wmax or not bmin <= bonus_roll <= bmax:
        raise ValueError("伤害结果超出骰式范围")
    if extreme and not fighting_back:
        return max(0, wmax + bmax + (weapon_roll if impaling else 0))
    return max(0, weapon_roll + bonus_roll)


def firearm_parameters(distance: float, base_range: float, *, dex: int = 0, distance_unit: str = "yards",
                       aim: bool = False, diving_cover: bool = False, concealed_half: bool = False,
                       target_mov: int = 0, target_build: int = 0, multiple_shots: bool = False,
                       reload_and_fire: bool = False, into_melee: bool = False, moving_shooter: bool = False) -> dict:
    if distance < 0 or base_range <= 0 or distance_unit not in {"yards", "feet", "meters"}:
        raise ValueError("射程或单位非法")
    difficulty: Difficulty = "regular" if distance <= base_range else "hard" if distance <= 2 * base_range else "extreme" if distance <= 4 * base_range else "impossible"
    feet = distance * {"yards": 3, "feet": 1, "meters": 3.280839895}[distance_unit]
    bonus = int(aim) + int(dex > 0 and feet <= dex / 5) + int(target_build >= 4)
    penalty = sum(map(int, [diving_cover, concealed_half, target_mov >= 8, target_build <= -2,
                           multiple_shots, reload_and_fire, into_melee, moving_shooter]))
    net = bonus - penalty
    return {"difficulty": difficulty, "bonus_dice": min(2, max(0, net)),
            "penalty_dice": min(2, max(0, -net)), "impale_requires_critical": difficulty == "extreme"}


def resolve_firearm(roll: int, skill: int, difficulty: Difficulty, malfunction: int = 100) -> dict:
    check = evaluate_check(roll, skill, difficulty)
    _integer(malfunction, 1, 100, "故障值")
    broken = roll >= malfunction
    return {"hit": check.success and not broken, "malfunction": broken, "check": check.model_dump(),
            "impale": check.success and not broken and (check.level == SuccessLevel.CRITICAL or (difficulty not in {"extreme", "critical"} and check.level >= SuccessLevel.EXTREME))}


def automatic_volley_plan(skill: int, bullets: int, ammo: int, *, volley_size: int | None = None,
                          wasted_between_targets: int = 0, difficulty: Difficulty = "regular",
                          initial_penalty: int = 0, initial_bonus: int = 0, burst: bool = False) -> dict:
    _integer(bullets, 1, 1000, "射击发数")
    _integer(wasted_between_targets, 0, 1000, "转移目标浪费弹药")
    if bullets + wasted_between_targets > ammo:
        raise ValueError("弹药不足")
    maximum = max(3, skill // 10)
    size = volley_size or maximum
    if size < (2 if burst else 3) or size > (3 if burst else maximum):
        raise ValueError("弹幕大小不符合技能/短点射规则")
    if burst and bullets != size:
        raise ValueError("短点射必须为单阵武器指定的2–3发弹幕")
    levels = ["regular", "hard", "extreme", "critical", "impossible"]
    base = levels.index(difficulty)
    volleys = []
    remainder = bullets
    while remainder:
        count = min(size, remainder)
        stacked = initial_penalty - initial_bonus + len(volleys)
        raised = max(0, stacked - 2)
        volleys.append({"bullets": count, "penalty_dice": min(2, max(0, stacked)), "bonus_dice": min(2,max(0,-stacked)),
                        "difficulty": levels[min(4, base + raised)]})
        remainder -= count
    return {"volleys": volleys, "ammo": ammo - bullets - wasted_between_targets,
            "spent": bullets + wasted_between_targets}


def resolve_volley(bullets: int, roll: int, skill: int, difficulty: Difficulty, malfunction: int = 100) -> dict:
    result = resolve_firearm(roll, skill, difficulty, malfunction)
    _integer(bullets, 1, 1000, "弹幕发数")
    hits = 0
    if result["hit"]:
        hits = bullets if result["impale"] else max(1, bullets // 2)
    return {**result, "hits": hits, "impaling_hits": max(1, bullets // 2) if result["impale"] else 0,
            "armor_applies_per_bullet": True}


class HealthState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hp: int = Field(ge=0)
    max_hp: int = Field(gt=0)
    major_wound: bool = False
    unconscious: bool = False
    dying: bool = False
    stabilized: bool = False
    dead: bool = False


def apply_damage(hp: int, max_hp: int, damage: int, armor: int = 0, *, major_wound: bool = False,
                 con_roll: int | None = None, con: int = 50, ignore_armor: bool = False) -> dict:
    for value, name in [(damage, "伤害"), (armor, "护甲")]:
        _integer(value, 0, 100000, name)
    if not 0 <= hp <= max_hp or max_hp <= 0:
        raise ValueError("生命状态非法")
    effective = max(0, damage - (0 if ignore_armor else armor))
    major = effective * 2 >= max_hp and effective > 0
    dead = effective > max_hp
    after = max(0, hp - effective)
    needs_con = major and after > 0 and not dead
    unconscious = after == 0 or (needs_con and con_roll is not None and not evaluate_check(con_roll, con).success)
    state = HealthState(hp=after, max_hp=max_hp, major_wound=major_wound or major,
                        unconscious=unconscious, dying=after == 0 and (major_wound or major) and not dead, dead=dead)
    return {**state.model_dump(), "damage": effective, "prone": major,
            "needs_con_check": needs_con and con_roll is None}


def dying_check(state: HealthState, roll: int, con: int) -> HealthState:
    if not state.dying and not state.stabilized:
        raise ValueError("仅濒死/暂稳者需要此检定")
    result = state.model_copy(deep=True)
    if not evaluate_check(roll, con).success:
        if state.stabilized:
            result.hp, result.stabilized, result.dying = 0, False, True
        else:
            result.dead, result.dying = True, False
    return result


def heal(state: HealthState, method: Literal["first_aid", "medicine", "natural", "weekly"], *,
         successful: bool = True, amount: int = 1, hours_since_wound: int = 0,
         already_treated: bool = False, check_level: int = 1, days: int = 1) -> dict:
    if state.dead:
        raise ValueError("普通治疗不能复活")
    result = state.model_copy(deep=True)
    if method in {"first_aid", "medicine"} and already_treated and not state.dying:
        raise ValueError("同一伤口已受此类成功治疗")
    if method == "first_aid":
        if hours_since_wound > 1 and not state.dying:
            raise ValueError("急救须在受伤一小时内")
        if successful:
            if state.dying:
                result.hp, result.dying, result.stabilized = 1, False, True
            else:
                result.hp = min(state.max_hp, state.hp + 1)
                result.unconscious = False
    elif method == "medicine":
        _integer(amount, 1, 3, "医学回复")
        if state.dying:
            raise ValueError("须先急救稳定伤势")
        if successful and hours_since_wound > 24 and check_level < SuccessLevel.HARD:
            raise ValueError("非当天医学需困难成功")
        if successful:
            result.hp = min(state.max_hp, state.hp + amount)
            result.stabilized = False
            result.unconscious = False
    elif method == "natural":
        _integer(days, 0, 10000, "恢复天数")
        if state.major_wound or state.dying or state.stabilized:
            raise ValueError("重伤/濒死不能每日自然恢复")
        result.hp = min(state.max_hp, state.hp + days)
        if result.hp > 0:
            result.unconscious = False
    elif method == "weekly":
        if not state.major_wound or state.dying or state.stabilized:
            raise ValueError("须解除濒死后才能每周重伤恢复")
        if successful:
            _integer(amount, 2 if check_level >= 3 else 1, 6 if check_level >= 3 else 3, "每周回复")
            result.hp = min(state.max_hp, state.hp + amount)
            if check_level >= 3 or result.hp * 2 >= result.max_hp:
                result.major_wound = False
    else:
        raise ValueError("未知治疗方式")
    return {**result.model_dump(), "needs_adjudication": check_level == -1,
            "con_check_interval": "hour" if result.stabilized else "round" if result.dying else None}


def growth(skill: int, roll: int, increase: int, *, skill_name: str = "", marked: bool = True) -> dict:
    _integer(roll, 1, 100, "成长骰")
    _integer(increase, 1, 10, "成长增值")
    if not marked or skill_name in {"克苏鲁神话", "信用评级", "cthulhu_mythos", "credit_rating"}:
        raise ValueError("该技能无成长标记")
    improved = roll > skill or roll > 95
    after = skill + increase if improved else skill
    return {"skill": after, "improved": improved, "mark": False,
            "sanity_reward_expression": "2D6" if skill < 90 <= after else "0"}


def can_mark_growth(check: CheckResult, *, bonus_dice: int = 0, luck_spent: int = 0,
                    opposed_winner: bool = True, skill_name: str = "") -> bool:
    return check.success and not bonus_dice and not luck_spent and opposed_winner and skill_name not in {"克苏鲁神话", "信用评级", "cthulhu_mythos", "credit_rating"}


def recover_luck(luck: int, roll: int, increase: int) -> int:
    _integer(luck, 0, 99, "幸运")
    _integer(roll, 1, 100, "幸运增强")
    _integer(increase, 1, 10, "增强点数")
    return min(99, luck + increase) if roll > luck else luck


def sanity_check(san: int, roll: int, success_loss: str, failure_loss: str,
                 loss_roll: int | None = None, day_start_san: int | None = None,
                 day_loss: int = 0, int_roll: int | None = None, intelligence: int = 50,
                 *, mythos: int = 0, current_insanity: str = "none", in_bout: bool = False,
                 mythos_source: bool = False, previous_mythos_insanity: bool = False,
                 habituated_loss: int = 0, creature_max_loss: int | None = None) -> dict:
    _integer(san, 0, 99, "理智")
    _integer(mythos, 0, 99, "神话技能")
    if current_insanity not in {"none", "temporary", "indefinite", "permanent"}:
        raise ValueError("未知疯狂状态")
    baseline = san if day_start_san is None else _integer(day_start_san, 0, 99, "当日初始理智")
    _integer(day_loss, 0, 10000, "当日累计损失")
    check = evaluate_check(roll, san)
    expression = success_loss if check.success else failure_loss
    lower, upper = expression_bounds(expression)
    if lower < 0:
        raise ValueError("理智损失骰式不可产生负数")
    if in_bout:
        loss = 0
    elif check.level == SuccessLevel.FUMBLE:
        loss = upper
    elif lower == upper:
        loss = lower
    else:
        if loss_roll is None or not lower <= loss_roll <= upper:
            raise ValueError("须提供有效的已审计理智损失骰")
        loss = loss_roll
    if creature_max_loss is not None:
        loss = min(loss, max(0, creature_max_loss - habituated_loss))
    loss = min(san, loss)
    after = max(0, san - loss)
    daily = day_loss + loss
    insanity = current_insanity
    needs_int = False
    bout = False
    if after == 0:
        insanity = "permanent"
    elif loss > 0 and baseline > 0 and daily * 5 >= baseline:
        insanity, bout = "indefinite", True
    elif loss >= 5 and current_insanity == "none":
        needs_int = int_roll is None
        if int_roll is not None and evaluate_check(int_roll, intelligence).success:
            insanity, bout = "temporary", True
    elif loss > 0 and current_insanity in {"temporary", "indefinite"}:
        bout = True
    gain = (1 if previous_mythos_insanity else 5) if bout and mythos_source else 0
    mythos_after = min(99, mythos + gain)
    after = min(after, 99 - mythos_after)
    if after == 0:
        insanity = "permanent"
    return {"san": after, "loss": loss, "day_loss": daily, "day_start_san": baseline,
            "check": check.model_dump(), "insanity": insanity, "needs_int_check": needs_int,
            "bout": bout, "needs_background_change": bout, "mythos": mythos_after,
            "max_san": 99 - mythos_after, "involuntary_action": not check.success and not in_bout,
            "temporary_duration_expression": "1D10 hours" if insanity == "temporary" else None,
            "habituated_loss": habituated_loss + loss if creature_max_loss is not None else habituated_loss,
            "source": "PDF133–138,144,148 / 中文125–130,136,140"}


def madness_bout(roll: int, duration: int, *, summary: bool = False, hp: int | None = None) -> dict:
    _integer(roll, 1, 10, "发作类型")
    _integer(duration, 1, 10, "发作持续骰")
    immediate = ["失忆", "假性残疾", "暴力倾向", "偏执", "人际依赖", "昏厥", "逃避行为", "歇斯底里", "恐惧", "狂躁"]
    summaries = ["失忆", "被窃", "遍体鳞伤", "暴力倾向", "极端信念", "重要之人", "被收容", "逃避行为", "恐惧", "狂躁"]
    return {"symptom": (summaries if summary else immediate)[roll - 1], "duration": duration,
            "unit": "hours" if summary else "rounds", "immune_to_sanity_loss": True,
            "hp": hp // 2 if summary and roll == 3 and hp is not None else hp,
            "major_wound_from_bout": False, "needs_background_change": True,
            "needs_adjudication": True, "source": "PDF136–137 / 中文128–129"}


def recover_sanity(san: int, mythos: int, method: str, *, roll: int, amount: int = 1,
                   sanity_roll: int | None = None, key_connection: bool = False) -> dict:
    _integer(roll, 1, 100, "恢复检定")
    _integer(san, 1, 99, "理智")
    maximum = max(0, 99 - mythos)
    recovered = False
    changed_background = False
    if method == "self_help":
        _integer(amount, 1, 6, "自救回复")
        ok = evaluate_check(roll, san).success
        delta = amount if ok else -1
        recovered = ok and key_connection
        changed_background = not ok
    elif method in {"private_care", "institution"}:
        cutoff = 95 if method == "private_care" else 50
        if roll <= cutoff:
            if method == "institution":
                _integer(amount, 1, 3, "机构恢复")
            delta = 3 if method == "private_care" else amount
            if sanity_roll is None:
                raise ValueError("成功护理后需要理智检定决定是否康复")
            recovered = evaluate_check(sanity_roll, min(maximum, san + delta)).success
        elif roll >= 96:
            _integer(amount, 1, 6, "恶化损失")
            delta = -amount
        else:
            delta = 0
    elif method == "psychoanalysis":
        _integer(amount, 1, 3, "心理治疗恢复")
        # roll is the already-judged skill result flag represented separately via threshold below.
        raise ValueError("心理治疗请使用 psychoanalysis_month，提供治疗者技能")
    else:
        raise ValueError("未知理智恢复方式")
    return {"san": max(0, min(maximum, san + delta)), "delta": delta,
            "recovered_indefinite": recovered, "needs_background_change": changed_background,
            "lost_key_connection": changed_background and key_connection}


def psychoanalysis_month(san: int, mythos: int, roll: int, skill: int, amount: int) -> dict:
    check = evaluate_check(roll, skill)
    _integer(amount, 1, 6 if check.level == SuccessLevel.FUMBLE else 3, "心理治疗骰")
    delta = amount if check.success else -amount if check.level == SuccessLevel.FUMBLE else 0
    return {"san": max(0, min(99 - mythos, san + delta)), "delta": delta,
            "discontinue_therapist": check.level == SuccessLevel.FUMBLE}


class Tome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    cmi: int = Field(ge=0, le=99)
    cmf: int = Field(ge=0, le=99)
    mythos_rating: int = Field(ge=0, le=100)
    study_weeks: int = Field(gt=0, le=10000)
    sanity_loss: str
    language: str
    source_pdf_page: int
    reading_difficulty: Literal["regular", "hard", "extreme", "automatic"] = "regular"
    initial_minutes: int = Field(default=60, ge=1, le=100800)
    retry_minutes: int = Field(default=10080, ge=1, le=100800)


def study_tome(tome: Tome, mythos: int, san: int, loss_roll: int, *, initial: bool = False,
               reading_success: bool = True, initial_read_done: bool = False,
               prior_full_studies: int = 0, elapsed_weeks: int = 0, believer: bool = True,
               other_active_study: bool = False) -> dict:
    if other_active_study:
        raise ValueError("同一时段只能精读一本典籍")
    _integer(prior_full_studies, 0, 20, "精读次数")
    lower, upper = expression_bounds(tome.sanity_loss)
    if not lower <= loss_roll <= upper:
        raise ValueError("典籍理智损失超出骰式")
    required = tome.study_weeks * 2 ** prior_full_studies
    if initial:
        if initial_read_done:
            raise ValueError("泛读奖励不可重复取得")
        gain = tome.cmi if reading_success else 0
    else:
        if not initial_read_done or elapsed_weeks < required:
            raise ValueError("未泛读或精读时间不足")
        gain = tome.cmf if mythos < tome.mythos_rating else tome.cmi
    if initial and not reading_success:
        return {"mythos": mythos, "san": san, "gain": 0, "loss": 0, "completed": False}
    new_mythos = min(99, mythos + gain)
    loss = loss_roll if believer else 0
    return {"mythos": new_mythos, "san": max(0, min(99 - new_mythos, san - loss)),
            "gain": gain, "loss": loss, "completed": True, "needs_insanity_resolution": loss >= 5,
            "language_growth_mark": None if initial else tome.language,
            "next_study_weeks": required if initial else required * 2,
            "source": "PDF152–154 / 中文144–146"}


def learn_spell(intelligence: int, roll: int, *, source: str = "tome", initial_read_done: bool = True,
                elapsed_days: int = 0, required_days: int = 14) -> dict:
    if source not in {"tome", "teacher", "mythos_entity"}:
        raise ValueError("未知法术来源")
    if source == "tome" and not initial_read_done:
        raise ValueError("从典籍学习必须先完成泛读")
    if elapsed_days < required_days:
        raise ValueError("学习时间不足；时长由已记录裁定指定")
    check = evaluate_check(roll, intelligence, "regular" if source == "mythos_entity" else "hard")
    return {"learned": check.success, "check": check.model_dump(), "sanity_cost": 0,
            "entity_contact_cost_requires_adjudication": source == "mythos_entity"}


def cast_spell(mp: int, hp: int, san: int, pow: int, *, mp_cost: int, san_cost: int,
               pow_cost: int = 0, first_cast: bool = False, roll: int | None = None,
               pushed: bool = False, backlash_multiplier: int | None = None,
               backlash_san_roll: int | None = None, known: bool = True,
               believer: bool = True, components_available: bool = True,
               interrupted: bool = False) -> dict:
    if not known or not believer or not components_available:
        raise ValueError("法术未知、不相信神话或缺少必要材料")
    for v, name in [(mp, "MP"), (hp, "HP"), (san, "SAN"), (pow, "POW"),
                    (mp_cost, "MP代价"), (san_cost, "SAN代价"), (pow_cost, "POW代价")]:
        _integer(v, 0, 10000, name)
    if mp_cost > mp + hp:
        raise ValueError("MP与可替代HP不足以支付法术基本代价")
    check = None
    if first_cast:
        if roll is None:
            raise ValueError("首次施法需要困难POW检定")
        check = evaluate_check(roll, pow, "hard")
    elif pushed:
        raise ValueError("已熟练施法不需要首次施法孤注")
    failed_push = pushed and check is not None and not check.success
    extra_mp = extra_pow = extra_san = 0
    if failed_push:
        if backlash_multiplier is None:
            raise ValueError("失败孤注需要1D6额外代价倍数")
        _integer(backlash_multiplier, 1, 6, "反噬倍数")
        if backlash_san_roll is None:
            raise ValueError("SAN反噬须独立掷骰，不能用一次出目乘倍数")
        _integer(backlash_san_roll, 0, 10000, "反噬SAN")
        extra_mp = mp_cost * backlash_multiplier
        extra_pow = pow_cost * backlash_multiplier
        extra_san = backlash_san_roll
    if pow_cost > pow:
        raise ValueError("POW不足以支付法术基本代价")
    full_mp = mp_cost + extra_mp
    overflow = max(0, full_mp - mp)
    return {"mp": max(0, mp - full_mp), "hp": max(0, hp - overflow),
            "san": max(0, san - san_cost - extra_san), "pow": max(0,pow - pow_cost - extra_pow),
            "unpaid_pow": max(0,pow_cost+extra_pow-pow), "failed_push":failed_push,
            "hp_damage": overflow, "san_loss": san_cost + extra_san,
            "effect_applies": not interrupted and (check is None or check.success or pushed),
            "needs_damage_resolution": overflow > 0, "needs_insanity_resolution": san_cost + extra_san > 0,
            "needs_adjudication": failed_push or interrupted,
            "check": check.model_dump() if check else None,
            "source": "PDF154–157 / 中文146–149"}


def recover_mp(mp: int, pow: int, hours: int) -> int:
    _integer(hours, 0, 100000, "恢复小时")
    # Existing surplus is retained but natural recovery never increases it.
    rate = 1 + max(0, (pow - 1) // 100)
    return max(mp, min(pow // 5, mp + hours * rate))


def become_believer(san: int, mythos: int) -> dict:
    return {"san": max(0, san - mythos), "loss": min(san, mythos),
            "believer": True, "needs_insanity_resolution": mythos > 0}


def credit_assets(credit: int, era: str = "1920s") -> dict:
    _integer(credit, 0, 99, "信用评级")
    if era not in {"1920s", "modern"}:
        raise ValueError("此时代资金表需要管理员核对")
    modern = era == "modern"
    if credit == 0:
        cash, assets, spending, lifestyle = (10, 0, 10, "身无分文") if modern else (.5, 0, .5, "身无分文")
    elif credit < 10:
        cash, assets, spending, lifestyle = credit, credit * 10, 2, "贫穷"
    elif credit < 50:
        cash, assets, spending, lifestyle = credit * 2, credit * 50, 10, "标准"
    elif credit < 90:
        cash, assets, spending, lifestyle = credit * 5, credit * 500, 50, "小康"
    elif credit < 99:
        cash, assets, spending, lifestyle = credit * 20, credit * 2000, 250, "富裕"
    else:
        cash, assets, spending, lifestyle = 50000, 5000000, 5000, "富豪"
    if modern and credit > 0:
        cash, assets, spending = cash * 20, assets * 20, spending * 20
    return {"cash": cash, "assets": assets, "spending_level": spending, "lifestyle": lifestyle,
            "assets_is_minimum": credit == 99, "currency": "USD", "source": "PDF37 / 中文29"}


def purchase(cash: float, price: float, spending_level: float, *, daily_total: float | None = None) -> dict:
    if min(cash, price, spending_level) < 0:
        raise ValueError("资金不可为负")
    cost = price if price > spending_level else 0
    if daily_total is not None:
        if daily_total < price:
            raise ValueError("日累计不可小于此次支出")
        cost = daily_total if daily_total > spending_level else 0
    if cost > cash:
        raise ValueError("现金不足；资产变现须事先完成有时长的裁定")
    return {"cash": round(cash - cost, 2), "deduction": cost}


class ChaseParticipant(BaseModel):
    id: str
    role: Literal["pursuer", "escapee"]
    mov: int = Field(ge=0, le=100)
    dex: int = Field(ge=0, le=1000)
    position: int = 0
    action_points: int = Field(default=1, ge=0)
    remaining: int = 1
    debt: int = Field(default=0, ge=0)
    attacks_used: int = Field(default=0, ge=0)
    attacks_per_round: int = Field(default=1, ge=1, le=20)
    active: bool = True
    vehicle: bool = False
    vehicle_build: int | None = Field(default=None, ge=0, le=100)
    passenger_of: str | None = None


class ChaseObstacle(BaseModel):
    id: str
    after_position: int
    kind: Literal["hazard", "barrier"]
    difficulty: Difficulty = "regular"
    hp: int = Field(default=0, ge=0)
    removed: bool = False


class ChaseState(BaseModel):
    participants: list[ChaseParticipant]
    obstacles: list[ChaseObstacle] = Field(default_factory=list)
    order: list[str]
    turn: int = 0
    round: int = 1
    ended: bool = False
    escaped: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    route_id: str = "main"


def chase_speed(mov: int, roll: int, skill: int) -> int:
    check = evaluate_check(roll, skill)
    return max(0, mov + (1 if check.level >= SuccessLevel.EXTREME else 0 if check.success else -1))


def start_chase(participants: list[ChaseParticipant], *, obstacles: list[ChaseObstacle] | None = None,
                tie_order: list[str] | None = None, gap: int = 2, route_id: str = "main") -> ChaseState:
    if gap not in (1, 2):
        raise ValueError("起始距离默认2，可裁定为1")
    members = [p.model_copy(deep=True) for p in participants]
    if len({p.id for p in members}) != len(members):
        raise ValueError("追逐者身份重复")
    drivers={p.id:p for p in members if p.vehicle and not p.passenger_of}
    for p in members:
        if p.passenger_of and p.passenger_of not in drivers: raise ValueError("乘客须关联参与本次追逐的载具司机")
    pursuers = [p for p in members if p.role == "pursuer" and not p.passenger_of]
    runners = [p for p in members if p.role == "escapee" and not p.passenger_of]
    if not pursuers or not runners:
        raise ValueError("需要追逐与逃离双方")
    fastest = max(p.mov for p in pursuers)
    slowest = min(p.mov for p in runners)
    escaped = [p.id for p in runners if p.mov > fastest]
    dropped = [p.id for p in pursuers if p.mov < slowest]
    for p in members:
        p.active = p.id not in escaped + dropped
        if p.passenger_of: p.active=p.passenger_of not in escaped+dropped;p.mov=drivers[p.passenger_of].mov
    active = [p for p in members if p.active]
    live_p = [p for p in active if p.role == "pursuer" and not p.passenger_of]
    live_e = [p for p in active if p.role == "escapee" and not p.passenger_of]
    if not live_p or not live_e:
        return ChaseState(participants=members, obstacles=obstacles or [], order=[], ended=True,
                          escaped=escaped, dropped=dropped, route_id=route_id)
    if len({p.dex for p in active}) < len(active) and tie_order is None:
        raise ValueError("敏捷平局须先敏捷对抗，提供已裁定 tie_order")
    if tie_order is not None and set(tie_order) != {p.id for p in active}:
        raise ValueError("tie_order必须恰好包含参与者")
    ordered = sorted(active, key=lambda p: (-p.dex, tie_order.index(p.id) if tie_order else 0))
    min_mov = min(p.mov for p in active)
    back = min(p.mov for p in live_p)
    lead = max(p.mov for p in live_p) - back
    min_runner = min(p.mov for p in live_e)
    for p in active:
        p.position = p.mov - back if p.role == "pursuer" else lead + gap + p.mov - min_runner
        p.action_points = p.remaining = 1 + p.mov - min_mov
    for p in active:
        if p.passenger_of: p.position=drivers[p.passenger_of].position;p.action_points=0;p.remaining=1
    return ChaseState(participants=members, obstacles=obstacles or [], order=[p.id for p in ordered],
                      escaped=escaped, dropped=dropped, route_id=route_id)


def chase_action(state: ChaseState, actor_id: str, action: str, *, target_id: str | None = None,
                 obstacle_id: str | None = None, roll: int | None = None, skill: int | None = None,
                 delay_roll: int | None = None, careful_points: int = 0, damage: int = 0,
                 moving_fire: bool = False) -> dict:
    """One authorized action; results are a new state, never a mutation of input."""
    result = state.model_copy(deep=True)
    if result.ended or result.order[result.turn] != actor_id:
        raise ValueError("不是此角色行动回合")
    actor = next(p for p in result.participants if p.id == actor_id)
    if actor.passenger_of and action not in {"ranged_attack","other","end_turn"}: raise ValueError("乘客只能在其回合射击或协助，不能独立移动")
    if actor.remaining <= 0:
        raise ValueError("本轮没有剩余行动点，须结束回合")
    _integer(careful_points, 0, 2, "谨慎额外行动点")
    if action == "end_turn":
        actor.remaining = 0
        _advance_chase(result)
        return {"state": result, "damage": 0, "bonus_dice": 0}
    cost = 1
    suffered = 0
    if action in {"move", "break_barrier"}:
        obstacle = next((o for o in result.obstacles if o.after_position == actor.position and not o.removed), None)
        if obstacle:
            if obstacle_id != obstacle.id:
                raise ValueError("须处理当前位置的险境/障碍")
            cost += careful_points
            if actor.remaining < cost:
                raise ValueError("没有足够行动点换取奖励骰")
            if action == "break_barrier":
                if obstacle.kind != "barrier" or obstacle.hp <= 0 or damage <= 0:
                    raise ValueError("不可破坏该险境或缺少有效伤害")
                obstacle.hp = max(0, obstacle.hp - damage)
                obstacle.removed = obstacle.hp == 0
                passed = obstacle.removed
            else:
                if roll is None or skill is None:
                    raise ValueError("通过险境需要预先声明的检定")
                passed = evaluate_check(roll, skill, obstacle.difficulty).success
                if not passed:
                    if delay_roll is None:
                        raise ValueError("失败需1D3延迟骰")
                    _integer(delay_roll, 1, 3, "险境延迟")
                    cost += delay_roll
                    suffered = damage
            if passed or obstacle.kind == "hazard":
                actor.position += 1
        elif action == "break_barrier":
            raise ValueError("当前位置没有障碍")
        else:
            actor.position += 1
    elif action in {"attack", "ranged_attack"}:
        target = next((p for p in result.participants if p.id == target_id and p.active), None)
        if target is None or target.id == actor.id:
            raise ValueError("无效目标")
        if action == "attack" and actor.position != target.position:
            raise ValueError("近战须同一位置")
        if actor.attacks_used >= actor.attacks_per_round:
            raise ValueError("本轮攻击次数用尽")
        actor.attacks_used += 1
        if moving_fire and action == "ranged_attack":
            cost = 0
    elif action not in {"spell", "other"}:
        raise ValueError("未知追逐行动")
    after = actor.remaining - cost
    actor.remaining = max(0, after)
    actor.debt += max(0, -after)
    for passenger in result.participants:
        if passenger.passenger_of==actor_id: passenger.position=actor.position
    return {"state": result, "damage": suffered, "bonus_dice": careful_points,
            "penalty_dice": 1 if moving_fire else 0, "action_cost": cost}


def _advance_chase(state: ChaseState) -> None:
    state.turn += 1
    if state.turn >= len(state.order):
        state.turn = 0
        state.round += 1
        for p in state.participants:
            if p.active:
                available = p.action_points - p.debt
                p.remaining, p.debt = (1,0) if p.passenger_of else (max(0, available), max(0, -available))
                p.attacks_used = 0


def chase_end_turn(state: ChaseState, actor_id: str) -> ChaseState:
    result = state.model_copy(deep=True)
    if result.ended or result.order[result.turn] != actor_id:
        raise ValueError("非当前回合")
    _advance_chase(result)
    return result


def chase_escape(state: ChaseState, actor_id: str, *, lost_contact: bool, adjudication_id: str) -> ChaseState:
    if not lost_contact or not adjudication_id:
        raise ValueError("逃脱须记录断线/隐藏成功的来源裁定，不按任意距离自动成功")
    result = state.model_copy(deep=True)
    actor = next(p for p in result.participants if p.id == actor_id)
    if actor.role != "escapee" or not actor.active:
        raise ValueError("角色不是仍在被追逐的逃脱者")
    actor.active = False
    result.escaped.append(actor_id)
    current = result.order[result.turn]
    result.order.remove(actor_id)
    result.ended = not any(p.active and p.role == "escapee" for p in result.participants)
    result.turn = result.order.index(current) if current in result.order else result.turn % max(1, len(result.order))
    return result


def vehicle_collision(build: int, damage: int, *, target_initial_build: int | None = None,
                      inflicted_damage: int | None = None) -> dict:
    _integer(damage, 0, 100000, "车辆伤害")
    lost = damage // 10
    if inflicted_damage is not None:
        if target_initial_build is None:
            raise ValueError("撞击反伤须知道目标原体格")
        lost = min((inflicted_damage // 2) // 10, max(0, target_initial_build))
    return {"build": build - lost, "lost_build": lost, "wrecked": build - lost <= 0}


def acceleration_penalty(locations: int, *, assisted: bool = False) -> int:
    _integer(locations, 2, 5, "加速位置数")
    return max(0, (1 if locations <= 3 else 2) - int(assisted))


def chase_accelerate(state: ChaseState, actor_id: str, locations: int, checks: dict[str,dict]) -> dict:
    """PDF122–123: one AP for a declared 2–5 locations; first failed hazard stops it."""
    acceleration_penalty(locations)
    current=state.model_copy(deep=True)
    participant=next(p for p in current.participants if p.id==actor_id)
    if not participant.vehicle or participant.passenger_of or participant.remaining<1: raise ValueError("只有有行动点的载具司机能加速")
    available=participant.remaining;debt=participant.debt;cost=1;damage=0;crossed=0
    for _ in range(locations):
        participant=next(p for p in current.participants if p.id==actor_id)
        obstacle=next((o for o in current.obstacles if o.after_position==participant.position and not o.removed),None)
        check=checks.get(obstacle.id,{}) if obstacle else {}
        if obstacle and not check: raise ValueError("路径中的每个险境必须提供预先声明的检定")
        participant.remaining=1000;participant.debt=0
        result=chase_action(current,actor_id,"move",obstacle_id=obstacle.id if obstacle else None,**check)
        current=result["state"];damage+=result["damage"];cost+=result["action_cost"]-1
        crossed+=int(next(p for p in current.participants if p.id==actor_id).position>participant.position)
        if obstacle and not evaluate_check(check["roll"],check["skill"],obstacle.difficulty).success: break
    participant=next(p for p in current.participants if p.id==actor_id)
    participant.remaining=max(0,available-cost);participant.debt=debt+max(0,cost-available)
    return {"state":current,"damage":damage,"action_cost":cost,"crossed":crossed}


def vehicle_accident(build: int, roll: int, severity: str) -> dict:
    expression={"minor":"1D3-1","moderate":"1D6","severe":"1D10","deliberate":"2D10","catastrophic":"5D10"}.get(severity)
    if expression is None: raise ValueError("未知车辆事故等级")
    lower,upper=expression_bounds(expression)
    _integer(roll,lower,upper,"事故体格损失")
    return {"build":max(0,build-roll),"lost_build":min(build,roll),"wrecked":roll>=build,"expression":expression}


def random_chase_obstacle(roll: int) -> str | None:
    _integer(roll,1,100,"随机险境骰")
    return "extreme" if roll>=96 else "hard" if roll>=85 else "regular" if roll>=60 else None


def special_damage(kind: str, *, damage_roll: int | None = 0, con: int = 50, con_roll: int | None = None,
                   already_suffocating: bool = False, strenuous: bool = False,
                   height_feet: float = 0, surface: str = "grass", severity: str = "moderate",
                   fire_round: int = 1, minimum_weapon_damage: int = 1,
                   distance: float = 0, blast_radius: float = 3, critical_poison_immunity: bool = False) -> dict:
    """PDF107,356–357. Severity/environment are recorded KP decisions before dice."""
    expressions = {"minor": "1D3", "moderate": "1D6", "severe": "1D10", "deadly": "2D10", "terminal": "4D10", "splatter": "8D10"}
    if kind == "fall":
        if height_feet < 0 or surface not in {"mud", "grass", "concrete"}:
            raise ValueError("坠落参数非法")
        # Less than a full 10ft is an explicit KP severity choice, not silently rounded upward.
        count = int(height_feet // 10)
        expression = f"{count}D{ {'mud': 3, 'grass': 6, 'concrete': 10}[surface]}" if count else "0"
    elif kind in {"drowning", "suffocation"}:
        if not already_suffocating and con_roll is None:
            raise ValueError("首次窒息需体质检定")
        harmed = already_suffocating or not evaluate_check(con_roll, con, "hard" if strenuous else "regular").success
        expression = ("1D6" if kind == "drowning" else "1D3") if harmed else "0"
    elif kind == "poison":
        if con_roll is None or severity not in {"severe", "deadly", "terminal"}:
            raise ValueError("毒素需体质检定与已声明毒性")
        expression = expressions[severity]
    elif kind == "burning":
        _integer(fire_round, 1, 30, "燃烧轮数")
        _integer(minimum_weapon_damage, 1, 1000, "武器最小伤害")
        return {"damage": minimum_weapon_damage * 2 ** (fire_round - 1), "ignores_armor": False,
                "ongoing": True, "source": "PDF356；译注上限不是默认规则"}
    elif kind == "explosion":
        if distance < 0 or blast_radius <= 0:
            raise ValueError("爆炸距离非法")
        expression = "4D10" if distance <= blast_radius else "2D10" if distance <= 2 * blast_radius else "1D10" if distance <= 3 * blast_radius else "0"
    elif kind == "environment" and severity in expressions:
        expression = expressions[severity]
    else:
        raise ValueError("未知特殊伤害类型")
    lower, upper = expression_bounds(expression)
    if damage_roll is None:
        return {"expression":expression,"dice_required":True}
    if not lower <= damage_roll <= upper:
        raise ValueError(f"需要 {expression} 的已审计伤害骰")
    damage = damage_roll
    if kind == "poison":
        check = evaluate_check(con_roll, con)
        if check.level >= SuccessLevel.EXTREME:
            damage //= 2
        if critical_poison_immunity and check.level == SuccessLevel.CRITICAL:
            damage = 0
    return {"damage": damage, "expression": expression,
            "ignores_armor": kind in {"poison", "drowning", "suffocation"},
            "ongoing": kind in {"drowning", "suffocation"} and expression != "0",
            "needs_adjudication": kind == "poison" or (kind == "fall" and height_feet % 10 != 0)}


def outnumbered_bonus(defenses_used: int, attacks_per_round: int = 1, immune: bool = False) -> int:
    return int(not immune and defenses_used >= attacks_per_round)


def ranged_damage_bonus(rolled_bonus: int) -> int:
    """Muscle-powered projectiles / thrown weapons get half DB (PDF90)."""
    return rolled_bonus // 2


def reload_weapon(ammo: int, capacity: int, reserve: int, *, kind: str = "loose", fire_immediately: bool = False) -> dict:
    if not 0 <= ammo <= capacity or reserve < 0:
        raise ValueError("弹药状态非法")
    if kind not in {"loose", "magazine", "belt"}:
        raise ValueError("未知装弹方式")
    loaded = min(capacity - ammo, reserve, 1 if fire_immediately else 2 if kind == "loose" else capacity)
    if fire_immediately and (loaded != 1 or kind != "loose"):
        raise ValueError("只能装填一发后击发")
    return {"ammo": ammo + loaded - int(fire_immediately), "reserve": reserve - loaded,
            "rounds": 2 if kind == "belt" else 1, "penalty_dice": int(fire_immediately),
            "shot_requires_check": fire_immediately}


def power_growth(pow: int, roll: int, increase: int, *, won_spell_opposition: bool = False,
                 luck_critical: bool = False) -> int:
    if not won_spell_opposition and not luck_critical:
        raise ValueError("没有POW成长触发依据")
    _integer(roll, 1, 100, "POW成长")
    _integer(increase, 1, 10, "POW增加")
    return pow + increase if roll > pow or roll >= 96 else pow


def tome_reference(tome: Tome, roll: int, hours: int, *, full_study_done: bool) -> dict:
    _integer(hours, 1, 4, "查阅小时骰")
    _integer(roll, 1, 100, "查阅百分骰")
    if not full_study_done:
        raise ValueError("作参考前须完成精读")
    return {"found": roll <= tome.mythos_rating, "hours": hours,
            "information_requires_source": True}
