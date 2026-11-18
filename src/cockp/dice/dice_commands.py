"""Parse explicit dice-bot commands without sampling dice or calling a model.

Only this module recognizes the dot-command syntax. Permission checks, skill
values, random draws and durable settlement belong to the command executor.
"""
from __future__ import annotations

import re
import unicodedata

from cockp.core.contracts import DomainError
from cockp.dice.dice import normalize_expression, parse_expression_prefix


# A Latin word such as ``.random`` or ``.roll`` is not a dice command. Compact
# forms are accepted for dice expressions and Chinese skill names. English
# skill names start after whitespace or a quote.
_PREFIX = re.compile(r"^[.．](?:(ra)(?=$|[^A-Za-z])|(r)(?=$|[^A-Za-z]|[dD](?=[0-9\s]|$)))", re.I)
_LINE_BREAKS = re.compile(r"[\r\n\v\f\x85\u2028\u2029]")
_ANOTHER_COMMAND = re.compile(r"(?:^|[\s;；])[.．](?:ra(?=$|[^A-Za-z])|r(?=$|[^A-Za-z]|[dD][0-9]))", re.I)
_R_HELP = '格式：.r 1d100、.r 3d6x5 或 .r (2d6+6)*5 原因；数字原因请加引号。'
_RA_HELP = '格式：.ra 技能名 [原因]；带空格的技能名请加引号。技能值从角色卡读取，不能在命令中指定。'


def is_dice_command(text: str) -> bool:
    """Return whether text explicitly starts a supported dot command.

    Deliberately malformed parameters still return true so adapters can show
    the parser's public error instead of sending the input to an LLM.
    """
    return isinstance(text, str) and _PREFIX.match(text.lstrip()) is not None


def _quoted(text: str) -> tuple[str, str]:
    quote = text[0]
    end = text.find(quote, 1)
    if end == -1:
        raise DomainError("引号没有闭合，请用成对的单引号或双引号。")
    if end + 1 < len(text) and not text[end + 1].isspace():
        raise DomainError("引号后的内容请用空格分隔。")
    return text[1:end], text[end + 1:].strip()


def _reason(text: str, *, skill: bool = False) -> str:
    if not text:
        return ""
    if _ANOTHER_COMMAND.search(text):
        raise DomainError("一次只能发送一条骰子命令，请将多条命令分开发送。")
    if text[0] in "\"'":
        reason, trailing = _quoted(text)
        if trailing:
            raise DomainError("原因加引号后不能再追加参数，请将完整原因放入同一对引号。")
    else:
        if (text[0].isdigit() or text[0] in "+-*/%^=;；)）"
                or re.match(r"[dD]\s*[0-9]|[（(]\s*[0-9dD]", text)):
            raise DomainError(_RA_HELP if skill else _R_HELP)
        reason = text
    if len(reason) > 500:
        raise DomainError("掷骰原因最长 500 字符，请缩短后重试。")
    return reason


def _parse_roll(payload: str) -> tuple[str, dict[str, str]]:
    if not payload:
        return "dice_roll", {"expression": "1D100", "reason": ""}

    # The core parser consumes arithmetic without joining a numeric reason to
    # a die's sides (``1d100 123`` must never become a 100123-sided die).
    try:
        terms, expression_end = parse_expression_prefix(payload)
    except ValueError as exc:
        raise DomainError(str(exc)) from None
    if expression_end < len(payload) and not payload[expression_end].isspace():
        raise DomainError("骰式与原因之间需要空格；骰式支持骰子、整数、加减、乘法与括号。")
    expression = normalize_expression(payload[:expression_end])
    if not any(term.count for term in terms):
        raise DomainError("骰式至少需要一个骰子，例如 1d100 或 1d8+2。")
    return "dice_roll", {"expression": expression, "reason": _reason(payload[expression_end:].strip())}


def _parse_skill(payload: str) -> tuple[str, dict[str, str]]:
    if not payload:
        raise DomainError(_RA_HELP)
    if payload[0] in "\"'":
        skill, remaining = _quoted(payload)
    else:
        parts = payload.split(maxsplit=1)
        skill, remaining = parts[0], parts[1] if len(parts) == 2 else ""
    skill = skill.strip()
    if not skill or len(skill) > 100:
        raise DomainError("技能名须为 1–100 字符。" + _RA_HELP)
    if any(not (unicodedata.category(char).startswith("L") or char in " ()（）·_-") for char in skill):
        raise DomainError("技能名不能包含数值或运算符。" + _RA_HELP)
    return "skill_roll", {"skill": skill, "reason": _reason(remaining, skill=True)}


def parse_dice_command(text: str) -> tuple[str, dict[str, str]]:
    """Return an internal command plus arguments, or a safe Chinese error.

    No random state, campaign state or provider is touched by parsing.
    """
    if not isinstance(text, str):
        raise DomainError("骰子命令必须是文字。")
    if _LINE_BREAKS.search(text):
        raise DomainError("一次只能发送一条骰子命令，请勿使用换行或拼接多条命令。")
    if len(text) > 750:
        raise DomainError("骰子命令过长，请缩短骰式和原因。")
    clean = text.strip()
    prefix = _PREFIX.match(clean)
    if prefix is None:
        raise DomainError("不支持的骰子命令，请使用 .r 或 .ra。")
    payload = clean[prefix.end():].strip()
    return _parse_skill(payload) if prefix.group(1) else _parse_roll(payload)
