"""Bounded dice expressions and CoC percentile dice; no rule judgment or IO.

DiceService's cache prevents accidental repeats within a process. Persistent
idempotency and transactional settlement belong to the campaign service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import re
import secrets
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RandomSource(Protocol):
    def randint(self, a: int, b: int) -> int: ...


class RollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    check_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    group_id: str = "local"
    campaign_id: str = "local"
    character_id: str | None = None
    actor_id: str = "system"
    reason: str = Field(default="掷骰", max_length=500)
    expression: str = Field(default="1D100", min_length=1, max_length=120)
    visibility: Literal["public", "private", "kp"] = "public"
    ruleset_version: str = "coc7-core-1"
    bonus_dice: int = Field(default=0, ge=0, le=2)
    penalty_dice: int = Field(default=0, ge=0, le=2)


class DiceTerm(BaseModel):
    sign: Literal[-1, 1]
    count: int = 0
    sides: int = 0
    constant: int = 0
    faces: list[int] = Field(default_factory=list)


class RollResult(RollRequest):
    total: int
    terms: list[DiceTerm] = Field(default_factory=list)
    units: int | None = None
    tens: list[int] = Field(default_factory=list)
    candidates: list[int] = Field(default_factory=list)
    source: str = "builtin"
    rolled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    settlement_status: Literal["rolled", "settled", "cancelled"] = "rolled"

    @property
    def raw_faces(self) -> list[int]:
        return [face for term in self.terms for face in term.faces]


_ATOM = re.compile(r"(?:([0-9]*)[ \t]*[dD][ \t]*([0-9]+)|([0-9]+))")
_SYNTAX_ERROR = "骰式仅支持骰子、整数、加减、乘法与括号"
_MAX_MAGNITUDE = 1_000_000_000
MAX_REFERENCE_ROLLS = 20
MAX_REFERENCE_DICE = 100
MAX_REFERENCE_RECEIPT_CHARS = 4800


@dataclass(frozen=True)
class _Expression:
    op: str
    value: int | None = None
    left: _Expression | None = None
    right: _Expression | None = None


def normalize_expression(expression: str) -> str:
    """Canonical spelling only; validation always goes through the parser."""
    return (re.sub(r"[ \t]", "", expression).upper().replace("X", "*").replace("×", "*")
            .replace("（", "(").replace("）", ")"))


def _bounds(node: _Expression, terms: list[DiceTerm]) -> tuple[int, int]:
    if node.op == "atom":
        term = terms[node.value]
        values = (term.count, term.count * term.sides) if term.count else (term.constant, term.constant)
    elif node.op == "group":
        values = _bounds(node.left, terms)
    elif node.op == "negative":
        low, high = _bounds(node.left, terms)
        values = -high, -low
    else:
        a, b = _bounds(node.left, terms)
        c, d = _bounds(node.right, terms)
        if node.op == "+":
            values = a + c, b + d
        elif node.op == "-":
            values = a - d, b - c
        else:
            products = (a * c, a * d, b * c, b * d)
            values = min(products), max(products)
    if max(abs(value) for value in values) > _MAX_MAGNITUDE:
        raise ValueError("骰式中间结果或总值不得超过正负 1000000000")
    return values


class _ExpressionParser:
    """Small arithmetic parser, deliberately independent of Python evaluation.

    Every leaf is sampled once, so ``3D6*5`` scales one three-die total. Keeping
    the tree transient preserves the persisted DiceTerm/RollResult schema.
    """

    def __init__(self, text: str):
        self.text = text
        self.position = 0
        self.end = 0
        self.terms: list[DiceTerm] = []
        self.dice_count = 0

    def _spaces(self):
        while self.position < len(self.text) and self.text[self.position] in " \t":
            self.position += 1

    def _mark(self):
        self.end = self.position
        if self.end > 120:
            raise ValueError("骰式最长 120 字符")

    def _atom(self, depth: int, sign: int = 1) -> _Expression:
        self._spaces()
        if self.position >= len(self.text):
            raise ValueError(_SYNTAX_ERROR)
        if self.text[self.position] in "(（":
            if depth >= 16:
                raise ValueError("骰式括号最多嵌套 16 层")
            closing = ")" if self.text[self.position] == "(" else "）"
            self.position += 1
            self._mark()
            node = self._sum(depth + 1, sign)
            self._spaces()
            if self.position >= len(self.text) or self.text[self.position] != closing:
                raise ValueError("骰式括号不匹配")
            self.position += 1
            self._mark()
            return _Expression("group", left=node)
        match = _ATOM.match(self.text, self.position)
        if match is None:
            raise ValueError(_SYNTAX_ERROR)
        self.position = match.end()
        self._mark()
        if match.group(2) is not None:
            count, sides = int(match.group(1) or "1"), int(match.group(2))
            if not 1 <= count <= 100 or not 2 <= sides <= 10000:
                raise ValueError("骰数限 1–100，面数限 2–10000")
            self.dice_count += count
            term = DiceTerm(sign=sign, count=count, sides=sides)
        else:
            value = int(match.group(3))
            if value > 1_000_000:
                raise ValueError("常数过大")
            term = DiceTerm(sign=sign, constant=value)
        if len(self.terms) >= 32 or self.dice_count > 100:
            raise ValueError("非法骰式或超出计算预算")
        self.terms.append(term)
        return _Expression("atom", value=len(self.terms) - 1)

    def _product(self, depth: int, sign: int, *, unary: bool) -> _Expression:
        self._spaces()
        negative = False
        if unary and self.position < len(self.text) and self.text[self.position] in "+-":
            negative = self.text[self.position] == "-"
            self.position += 1
        node = self._atom(depth, -sign if negative else sign)
        if negative:
            node = _Expression("negative", left=node)
        while True:
            previous = self.position
            self._spaces()
            if self.position >= len(self.text) or self.text[self.position] not in "*xX×":
                return node
            if self.text[self.position] in "xX" and self.position > previous:
                # Preserve ordinary spaced reasons such as "xylophone". A
                # multiplier x must be followed by an arithmetic operand.
                following = self.text[self.position + 1:].lstrip(" \t")
                if not following or following[0] not in "0123456789dD(（":
                    return node
            self.position += 1
            # A second unary operator needs parentheses, as with signed sums.
            right = self._atom(depth)
            node = _Expression("*", left=node, right=right)

    def _sum(self, depth: int, sign: int = 1) -> _Expression:
        node = self._product(depth, sign, unary=True)
        while True:
            self._spaces()
            if self.position >= len(self.text) or self.text[self.position] not in "+-":
                return node
            operator = self.text[self.position]
            self.position += 1
            right = self._product(depth, -sign if operator == "-" else sign, unary=False)
            node = _Expression(operator, left=node, right=right)

    def parse(self) -> _Expression:
        if not self.text or not self.text.strip(" \t"):
            raise ValueError("空骰式")
        node = self._sum(0)
        _bounds(node, self.terms)
        return node


def _parsed(expression: str) -> tuple[_Expression, list[DiceTerm]]:
    if not isinstance(expression, str) or len(expression) > 120:
        raise ValueError("骰式最长 120 字符")
    parser = _ExpressionParser(expression)
    node = parser.parse()
    if parser.position != len(expression):
        raise ValueError(_SYNTAX_ERROR)
    return node, parser.terms


def parse_expression(expression: str) -> list[DiceTerm]:
    """Validate bounded arithmetic and return its leaves, without sampling.

    The leaves retain legacy sign metadata; consumers must use DiceService or
    expression_bounds to evaluate arithmetic, rather than summing the leaves.
    """
    return _parsed(expression)[1]


def validate_reference_batch(expression: str, count: int = 1, reason: str = "") -> list[DiceTerm]:
    """Bound the whole reference request before consuming any randomness.

    The text bound includes repeated purpose/labels and worst-case face widths;
    it is deliberately conservative and never constructs a claimed roll.
    """
    if type(count) is not int or not 1 <= count <= MAX_REFERENCE_ROLLS:
        raise ValueError("参考掷骰次数须为1–20的整数。")
    terms = parse_expression(expression)
    dice = sum(term.count for term in terms)
    if not dice:
        raise ValueError("骰式至少包含一个骰子，例如1d100")
    if dice * count > MAX_REFERENCE_DICE:
        raise ValueError("一次参考掷骰请求合计最多100颗骰子；请减少次数或骰数。")
    width = sum(term.count * (len(str(term.sides)) + 1) for term in terms)
    upper = count * (128 + 2 * len(normalize_expression(expression)) + width + len(reason))
    if count > 1 and upper > MAX_REFERENCE_RECEIPT_CHARS:
        raise ValueError("本批掷骰回执过长；请减少次数或缩短用途。")
    return terms


def parse_expression_prefix(text: str) -> tuple[list[DiceTerm], int]:
    """Parse the expression at the start of a command; return its exact end.

    A caller may accept a trailing reason only when separated by whitespace.
    The same syntax, numeric bounds and draw budget apply to both entry points.
    """
    if not isinstance(text, str):
        raise ValueError(_SYNTAX_ERROR)
    parser = _ExpressionParser(text)
    parser.parse()
    return parser.terms, parser.end


def expression_bounds(expression: str) -> tuple[int, int]:
    node, terms = _parsed(expression)
    return _bounds(node, terms)


def _total(node: _Expression, terms: list[DiceTerm]) -> int:
    if node.op == "atom":
        term = terms[node.value]
        return sum(term.faces) if term.count else term.constant
    if node.op == "group":
        return _total(node.left, terms)
    if node.op == "negative":
        return -_total(node.left, terms)
    left, right = _total(node.left, terms), _total(node.right, terms)
    return left + right if node.op == "+" else left - right if node.op == "-" else left * right


def format_expression_roll(result: RollResult) -> str:
    """Render actual faces; the caller must enforce recipient visibility."""
    node, expected = _parsed(result.expression)
    if len(expected) != len(result.terms) or any(
        (left.count, left.sides, left.constant) != (right.count, right.sides, right.constant)
        for left, right in zip(expected, result.terms)
    ):
        raise ValueError("掷骰明细与骰式不匹配")

    def render(part: _Expression) -> str:
        if part.op == "atom":
            term = result.terms[part.value]
            return f"{term.count}D{term.sides}[" + ",".join(map(str, term.faces)) + "]" if term.count else str(term.constant)
        if part.op == "group":
            return "(" + render(part.left) + ")"
        if part.op == "negative":
            return "-" + render(part.left)
        return render(part.left) + ("×" if part.op == "*" else part.op) + render(part.right)

    return render(node) + f" = {result.total}"


class DiceService:
    def __init__(self, rng: RandomSource | None = None):
        self.rng = rng or secrets.SystemRandom()
        self._results: dict[str, tuple[str, RollResult]] = {}

    def roll(self, request: RollRequest) -> RollResult:
        fingerprint = request.model_dump_json()
        existing = self._results.get(request.request_id)
        if existing:
            if existing[0] != fingerprint:
                raise ValueError("同一掷骰请求 ID 不允许更改参数")
            result = existing[1].model_copy(deep=True)
            from cockp.dice.receipts import record_roll
            record_roll(result)
            return result
        node, terms = _parsed(request.expression)
        modifier = request.bonus_dice - request.penalty_dice
        if request.bonus_dice or request.penalty_dice:
            if len(terms) != 1 or terms[0].count != 1 or terms[0].sides != 100 or terms[0].sign != 1:
                raise ValueError("奖励/惩罚骰仅支持单个百分骰")
            units = self.rng.randint(0, 9)
            tens = [self.rng.randint(0, 9) for _ in range(1 + abs(modifier))]
            candidates = [((ten * 10 + units) or 100) for ten in tens]
            total = min(candidates) if modifier >= 0 else max(candidates)
            result = RollResult(**request.model_dump(), total=total, units=units, tens=tens, candidates=candidates)
        else:
            for term in terms:
                term.faces = [self.rng.randint(1, term.sides) for _ in range(term.count)]
            total = _total(node, terms)
            result = RollResult(**request.model_dump(), total=total, terms=terms)
        self._results[request.request_id] = (fingerprint, result.model_copy(deep=True))
        from cockp.dice.receipts import record_roll
        record_roll(result)
        return result


def format_roll(result: RollResult) -> str:
    """Only call after recipient visibility checks; hidden rolls never format publicly."""
    if result.visibility != "public":
        raise ValueError("私密掷骰必须通过授权管理者专用视图读取")
    modifier = result.bonus_dice - result.penalty_dice
    extra = ""
    if result.candidates:
        kind = "奖励" if modifier > 0 else "惩罚" if modifier < 0 else "抵消"
        extra = f"（{kind}骰：{result.candidates}）"
    return f"【内置骰子】{result.expression.upper()} → {result.total}{extra}。"
