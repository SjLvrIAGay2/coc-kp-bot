"""Certain dice-result prose cannot stand in for a program-issued receipt.

This is a narrow output contract, not a natural-language dice router. The
semantic reviewer still checks unsupported claims outside these exact forms.
Only Engine/Store create dice receipts; model-authored text never creates one.
"""
from __future__ import annotations

import re
import unicodedata


_RESULT_MARKER = re.compile(r"【(?:掷骰|骰子|暗骰|暗骰结果|参考结果|内置骰子|外部骰子)(?:[】·｜|：:]|\s)")
_DICE_EQUATION = re.compile(r"(?i)(?<![a-z0-9])(?:\d{0,3}d\d{1,5})(?:\[[^\]\n]{0,120}\])?[^\n。！？]{0,60}(?:=|→|->)\s*-?\d+")
_NUMBER = r"[0-9零〇一二两三四五六七八九十百]+"
_ASSERTED_DRAW = re.compile(
    rf"(?:掷出|掷得|骰出|投出|摇出)(?:了)?\s*{_NUMBER}\s*(?:点|[（(]|[，,。；;！!]|$)"
    rf"|(?:骰点|骰子点数|掷骰结果|骰子结果|本次点数)\s*(?:是|为|[:：=])\s*{_NUMBER}"
)
_FINISHED_DRAW = re.compile(r"已(?:经)?(?:完成|执行|进行|掷过|掷了).{0,4}(?:暗骰|掷骰)|(?:暗骰|掷骰)(?:已(?:经)?)?(?:完成|结束)")
_TEXT_FIELDS = {"text", "reason", "risk", "confirmation", "consequence"}


def dice_claim_error(candidate: dict) -> dict | None:
    """Reject recognizable result assertions before any model review/action.

    Rules such as ``掷1D100`` and plans such as ``准备投骰`` remain prose. This
    deliberately does not infer intent from user messages or sample a die.
    """
    def texts(value):
        if isinstance(value, dict):
            for key, entry in value.items():
                if key in _TEXT_FIELDS and isinstance(entry, str):
                    yield entry
                elif isinstance(entry, (dict, list)):
                    yield from texts(entry)
        elif isinstance(value, list):
            for entry in value:
                yield from texts(entry)

    for raw in texts(candidate):
        text = unicodedata.normalize("NFKC", raw)
        if any(pattern.search(text) for pattern in (_RESULT_MARKER, _DICE_EQUATION, _ASSERTED_DRAW, _FINISHED_DRAW)):
            return {"verdict": "reject", "reason_code": "unsupported_fact",
                    "brief_reason": "模型文字不能代替程序骰子回执；本候选未执行。",
                    "reference_code": "model_authored_dice_result"}
    return None
