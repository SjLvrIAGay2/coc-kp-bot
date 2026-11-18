"""Generic, model-directed character mappings with verifiable source accounting.

No source field name carries meaning here. The model selects target fields and
source IDs; this module verifies references, arithmetic, coverage and merging.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


CHARACTER_MAPPING_VERSION = "character-mapping-v1"
_ID = Annotated[int, Field(strict=True, ge=1)]
_PATH = Annotated[str, Field(strict=True, min_length=1, max_length=300)]
_SCALAR = StrictStr | StrictInt | Annotated[float, Field(allow_inf_nan=False)] | StrictBool | None
_MAPPING = tuple[_PATH, Literal["copy", "sum", "text"], _SCALAR, list[_ID]]
_SKILL = tuple[Annotated[str, Field(strict=True, min_length=1, max_length=100)], list[_ID | None], list[_ID]]
_DISPOSITION = tuple[_ID, Literal["ignored", "preserved", "unresolved"], Annotated[str, Field(strict=True, min_length=1, max_length=300)]]


def mapping_format_feedback(error):
    errors = error.errors(include_input=False,include_context=False,include_url=False) if hasattr(error,'errors') else []
    rows = sorted({item['loc'][1] for item in errors
        if len(item.get('loc',()))>=3 and item['loc'][0]=='m' and type(item['loc'][1])is int
        and 0<=item['loc'][1]<2000 and item['loc'][2]==2})[:8]
    return {'policy':'character-scalar-mapping-feedback-v1',
        'issues':[{'path':['m',number,2],'code':'scalar_mapping_value_required'}for number in rows],
        'guidance':'m的第三格只接受字符串、数字、布尔或null，不接受数组/对象。'
        '数组或对象必须按target_schema的叶路径分别填写m行，并各自引用source_units真实ID；'
        '例如教育增强记录使用/age_evidence/education_rolls/0/0和/age_evidence/education_rolls/0/1，'
        '不得将整个数组作为/age_evidence/education_rolls的一行值。保留原值，不计算或补写掷骰；'
        '没有对应目标的原数据用d=preserved保留，真实未知才列q。只修正结构，返回完整ledger。'}


class CharacterMappingLedger(BaseModel):
    """Compact tables: m=[pointer,operation,value,IDs], k=[name,4 IDs,label IDs]."""

    model_config = ConfigDict(extra="forbid", strict=True)
    __cockp_source_format_feedback__ = staticmethod(mapping_format_feedback)
    m: list[_MAPPING] = Field(default_factory=list, max_length=2000)
    k: list[_SKILL] = Field(default_factory=list, max_length=500)
    d: list[_DISPOSITION] = Field(default_factory=list, max_length=10000)
    q: list[str] = Field(default_factory=list, max_length=100)
    issues: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(default="", max_length=2000)

    @field_validator("m", "k", "d", mode="before")
    @classmethod
    def json_rows(cls, value):
        # JSON arrays are the wire format, including when an already-decoded
        # dict is validated strictly instead of model_validate_json.
        if isinstance(value, list):
            return [tuple(row) if isinstance(row, list) else row for row in value]
        return value

    @field_validator("k")
    @classmethod
    def four_components(cls, value):
        if any(len(row[1]) != 4 for row in value):
            raise ValueError("k requires [base,occupation,interest,growth] source IDs")
        return value


class _Invalid(ValueError):
    pass


def _escape(key):
    return str(key).replace("~", "~0").replace("/", "~1")


class _JSONIndex:
    """Index original lexical spans without interpreting a single source key."""

    def __init__(self, text):
        self.text = text; self.leaves = []; self.errors = []; self.nodes = 0
        self.decoder = json.JSONDecoder(parse_constant=self.constant, parse_float=self.floating)
        _value, end = self.read(0, "", 0)
        if self.space(end) != len(text): raise _Invalid("JSON末尾存在额外内容")

    def constant(self, _value):
        self.errors.append("JSON不允许NaN或Infinity"); return None

    def floating(self, lexeme):
        # Preserve the exact decimal value as a string in the model's units;
        # binary float parsing must not turn 1.0000000000000001 into 1.
        number = Decimal(lexeme)
        if not number.is_finite() or abs(number.adjusted()) > 100:
            self.errors.append("JSON数字指数超过可靠十进制读取范围"); return None
        return format(number, "f")

    def space(self, pos):
        while pos < len(self.text) and self.text[pos] in " \t\r\n": pos += 1
        return pos

    def read(self, pos, pointer, depth):
        self.nodes += 1
        if depth > 64 or self.nodes > 100000: raise _Invalid("JSON结构超过安全读取限制")
        start = pos = self.space(pos)
        if pos >= len(self.text): raise _Invalid("JSON不完整")
        token = self.text[pos]
        if token == "{":
            value = {}; pos = self.space(pos + 1)
            if pos < len(self.text) and self.text[pos] == "}": return value, pos + 1
            while True:
                key, end = self.decoder.raw_decode(self.text, pos)
                if not isinstance(key, str): raise _Invalid("JSON键必须是字符串")
                self.unicode(key); pos = self.space(end)
                if pos >= len(self.text) or self.text[pos] != ":": raise _Invalid("JSON缺少冒号")
                child, pos = self.read(pos + 1, pointer + "/" + _escape(key), depth + 1)
                if key in value: self.errors.append("JSON存在重复键，未选择任何冲突值")
                else: value[key] = child
                pos = self.space(pos)
                if pos < len(self.text) and self.text[pos] == "}": return value, pos + 1
                if pos >= len(self.text) or self.text[pos] != ",": raise _Invalid("JSON对象不完整")
                pos = self.space(pos + 1)
        if token == "[":
            value = []; pos = self.space(pos + 1)
            if pos < len(self.text) and self.text[pos] == "]": return value, pos + 1
            while True:
                child, pos = self.read(pos, pointer + "/" + str(len(value)), depth + 1)
                value.append(child); pos = self.space(pos)
                if pos < len(self.text) and self.text[pos] == "]": return value, pos + 1
                if pos >= len(self.text) or self.text[pos] != ",": raise _Invalid("JSON数组不完整")
                pos = self.space(pos + 1)
        value, end = self.decoder.raw_decode(self.text, pos)
        if isinstance(value, str): self.unicode(value)
        if isinstance(value, float) and not math.isfinite(value): self.errors.append("JSON数字不是有限数")
        self.leaves.append((pointer, value, start, end))
        return value, end

    @staticmethod
    def unicode(value):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value): raise _Invalid("JSON含无效Unicode字符")


def build_character_source_index(doc, chunks):
    """Build source-ID units for any JSON shape; None means non-JSON text.

    Only units belong in the model payload. Locations and source values remain
    trusted private metadata, and must never be taken back from model output.
    """
    text = doc.get("text") if isinstance(doc, dict) else None
    if not isinstance(text, str) or not text.strip() or text.lstrip()[0] not in '{["-0123456789tfn': return None
    result = {"version": CHARACTER_MAPPING_VERSION, "units": [], "locations": {}, "issues": [],
              "source": copy.deepcopy(doc.get("source", {})), "chunk_spans": {}}
    if len(text) > 2000000:
        result["issues"] = ["JSON超过安全读取长度，原文未截断"]; return result
    try:
        parsed = _JSONIndex(text)
    except (ValueError, OverflowError, RecursionError):
        return None
    if parsed.errors:
        result["issues"] = list(dict.fromkeys(parsed.errors)); return result
    result["index_id"] = hashlib.sha256((CHARACTER_MAPPING_VERSION + "\0" + text).encode()).hexdigest()
    end = 0; digest = result["source"].get("sha256")
    source_chunks = [c for c in chunks if not str(c.get("id", "")).startswith("supplement-")]
    if not source_chunks: source_chunks = list(chunks)
    try:
        for chunk in sorted(source_chunks, key=lambda x: x.get("start_offset", -1)):
            start = chunk.get("start_offset"); stop = chunk.get("end_offset"); identity = chunk.get("id")
            if (type(start) is not int or type(stop) is not int or start != end or stop <= start or stop > len(text)
                    or chunk.get("text") != text[start:stop] or not isinstance(identity, str)
                    or identity in result["chunk_spans"] or digest is not None and chunk.get("sha256") != digest):
                raise _Invalid("JSON分块与当前原文不一致")
            result["chunk_spans"][identity] = {"start": start, "end": stop, "text": chunk["text"]}
            end = stop
        if end != len(text): raise _Invalid("JSON原文分块不完整")
        for identity, (pointer, value, start, stop) in enumerate(parsed.leaves, 1):
            evidence = []; kinds = set()
            for chunk in source_chunks:
                pos = max(start, chunk["start_offset"]); last = min(stop, chunk["end_offset"])
                if pos >= last: continue
                kinds.add("supplement" if chunk.get("source") == "authenticated_user_supplement"
                          and str(chunk.get("id", "")).startswith("supplement-") else "document")
                while pos < last:
                    edge = min(last, pos + 3000)
                    evidence.append({"chunk_id": chunk["id"], "start": pos, "end": edge, "quote": text[pos:edge]})
                    pos = edge
            if not evidence: raise _Invalid("JSON字段缺少可定位原文")
            result["units"].append([identity, pointer, value])
            result["locations"][str(identity)] = {"pointer": pointer, "start": start, "end": stop,
                "source_kind": "supplement" if kinds == {"supplement"} else "document",
                "chunk_ids": list(dict.fromkeys(e["chunk_id"] for e in evidence)), "evidence": evidence}
    except (ValueError, TypeError, KeyError):
        result["units"] = []; result["locations"] = {}; result["issues"] = ["JSON来源绑定失败，未采用不可靠证据"]
    return result


def build_character_supplement_index(chunks, *, base_index=None):
    """Append actual supplement text/decimal tokens without assigning meaning.

    Original IDs and namespace remain stable when a base index is supplied.
    Both paragraph units and decimal-token units quote unescaped input text.
    """
    result = copy.deepcopy(base_index) if base_index is not None else {"version": CHARACTER_MAPPING_VERSION,
        "units": [], "locations": {}, "issues": [], "source": {"kind": "authenticated_user_supplement"}, "chunk_spans": {}}
    if result.get("issues"): return result
    accepted = [c for c in chunks if c.get("source") == "authenticated_user_supplement"
                and str(c.get("id", "")).startswith("supplement-")]
    if not accepted:
        result.setdefault("issues", []).append("没有经过验证的用户补充原文"); return result
    digest = hashlib.sha256(); end = 0; seen = set()
    try:
        for chunk in sorted(accepted, key=lambda c: c.get("start_offset", -1)):
            text = chunk.get("text"); start = chunk.get("start_offset"); stop = chunk.get("end_offset")
            if (not isinstance(text, str) or type(start) is not int or type(stop) is not int
                    or start != end or stop - start != len(text) or chunk["id"] in seen or not text):
                raise _Invalid("补充原文分块不完整或冲突")
            seen.add(chunk["id"]); digest.update(text.encode()); end = stop
        if end > 16000: raise _Invalid("补充超过安全读取长度")
        supplement_sha = digest.hexdigest()
        if any(c.get("sha256") not in (None, supplement_sha) for c in accepted):
            raise _Invalid("补充原文摘要不一致")
        if "index_id" not in result:
            result["index_id"] = hashlib.sha256((CHARACTER_MAPPING_VERSION + supplement_sha).encode()).hexdigest()
        next_id = max((r[0] for r in result["units"]), default=0) + 1
        for chunk in accepted:
            if chunk["id"] in result["chunk_spans"]:
                raise _Invalid("补充chunk ID与已保存来源冲突")
            text = chunk["text"]; start = chunk["start_offset"]
            result["chunk_spans"][chunk["id"]] = {"start": start, "end": chunk["end_offset"], "text": text}
            spans = []
            for match in re.finditer(r"[^\r\n]+", text):
                if not match.group().strip(): continue
                for pos in range(match.start(), match.end(), 3000):
                    spans.append(("text", pos, min(match.end(), pos + 3000)))
            for match in re.finditer(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text):
                if "e" not in match.group().lower(): spans.append(("number", match.start(), match.end()))
            for order, (kind, left, right) in enumerate(spans):
                value = text[left:right]
                pointer = "/supplements/" + _escape(chunk["id"]) + "/" + kind + "/" + str(order)
                result["units"].append([next_id, pointer, value])
                result["locations"][str(next_id)] = {"pointer": pointer, "start": start + left, "end": start + right,
                    "source_kind": "supplement", "chunk_ids": [chunk["id"]], "evidence": [{"chunk_id": chunk["id"],
                        "start": start + left, "end": start + right, "quote": value}]}
                next_id += 1
        result["revision_id"] = hashlib.sha256(json.dumps(result["units"], ensure_ascii=False).encode()).hexdigest()
        return result
    except (ValueError, TypeError, KeyError, UnicodeError):
        # Preserve the supplied base but never expose partially appended units.
        failed = copy.deepcopy(base_index) if base_index is not None else {"version": CHARACTER_MAPPING_VERSION,
            "units": [], "locations": {}, "source": {}, "chunk_spans": {}}
        failed["issues"] = ["补充来源绑定失败，未采用不可靠证据"]
        return failed


def _decimal(value):
    if type(value) not in {str, int, float}: raise _Invalid("来源不是明确数字")
    text = str(value).strip()
    if len(text) > 100 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", text): raise _Invalid("来源不是明确十进制数")
    try: number = Decimal(text)
    except InvalidOperation: raise _Invalid("来源数字不合法") from None
    if not number.is_finite(): raise _Invalid("来源数字不是有限数")
    return number


def _parts(pointer):
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer): raise _Invalid("目标须为JSON pointer")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
    if any(not p or len(p) > 100 for p in parts): raise _Invalid("目标字段名称不合法")
    return parts


_ATTRS = {"STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU"}
_FORBIDDEN = {"id", "owner_id", "version", "locked", "sources", "recognized_confirmed", "review_history", "admin", "role", "permissions"}


def _target(parts):
    item_id = len(parts) == 3 and parts[0] == "equipment" and parts[1].isdigit() and parts[2] == "id"
    if any(p in _FORBIDDEN for p in parts) and not item_id: raise _Invalid("目标涉及身份、权限或来源元数据")
    head = parts[0]
    if len(parts) == 1:
        if head in {"name", "era", "occupation"}: return "text"
        if head in {"age", "luck"}: return "int"
        if head in {"cash", "assets"}: return "number"
    if head == "attributes" and len(parts) == 2 and parts[1] in _ATTRS: return "int"
    if head == "skills" and len(parts) == 2: return "int"
    if head == "allocations" and len(parts) == 3 and parts[2] in {"base", "occupation", "interest"}: return "int"
    if head == "derived" and len(parts) == 2:
        if parts[1] == "damage_bonus": return "text"
        if parts[1] in {"hp", "mp", "san", "mov", "build"}: return "int"
    if head == "background" and len(parts) == 2: return "text"
    if head == "occupation_skills" and len(parts) == 2 and parts[1].isdigit(): return "text"
    if head == "equipment" and len(parts) == 3 and parts[1].isdigit(): return "scalar"
    if head == "creation":
        if len(parts) == 2 and parts[1] in {"method", "occupation_formula", "source"}: return "text"
        if len(parts) == 3 and parts[1] == "raw_attributes" and parts[2] in _ATTRS: return "int"
        if len(parts) == 3 and parts[1] == "luck_rolls" and parts[2].isdigit(): return "int"
    if head == "age_evidence":
        if len(parts) == 3 and parts[1] == "deductions" and parts[2] in _ATTRS: return "int"
        if len(parts) == 4 and parts[1] == "education_rolls" and parts[2].isdigit() and parts[3] in {"0", "1"}: return "int"
    raise _Invalid("目标不是合法角色卡叶字段")


def _get(data, parts):
    try:
        for part in parts: data = data[int(part)] if isinstance(data, list) else data[part]
        return True, data
    except (KeyError, IndexError, TypeError, ValueError): return False, None


def _put(data, parts, value):
    current = data
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(current, list):
            if not part.isdigit() or int(part) > len(current): raise _Invalid("列表缺少前序来源，未猜补空位")
            pos = int(part)
            if last:
                if pos == len(current): current.append(copy.deepcopy(value))
                else: current[pos] = copy.deepcopy(value)
                return
            expected = _container(parts[:i + 1])
            if pos == len(current): current.append(expected())
            if not isinstance(current[pos], expected): raise _Invalid("目标结构冲突")
            current = current[pos]
        else:
            if not isinstance(current, dict): raise _Invalid("目标结构冲突")
            if last: current[part] = copy.deepcopy(value); return
            expected = _container(parts[:i + 1])
            if part not in current: current[part] = expected()
            if not isinstance(current[part], expected): raise _Invalid("目标结构冲突")
            current = current[part]


def _container(prefix):
    if (prefix == ["occupation_skills"] or prefix == ["equipment"] or prefix == ["creation", "luck_rolls"]
            or prefix == ["age_evidence", "education_rolls"]
            or len(prefix) == 3 and prefix[:2] == ["age_evidence", "education_rolls"]):
        return list
    return dict


def _delete(data, parts):
    exists, parent = _get(data, parts[:-1])
    if exists and isinstance(parent, dict): parent.pop(parts[-1], None)


def _evidence(index, pointer, ids):
    grouped = {}
    for source_id in ids:
        for item in index["locations"][str(source_id)]["evidence"]:
            grouped.setdefault(item["chunk_id"], []).append(item)
    field = ".".join(_parts(pointer))
    result = []
    for chunk_id, items in grouped.items():
        start = min(i["start"] for i in items); end = max(i["end"] for i in items)
        chunk = index.get("chunk_spans", {}).get(chunk_id)
        if chunk is not None and end - start <= 3000:
            quote = chunk["text"][start - chunk["start"]:end - chunk["start"]]
            result.append({"field_path": field, "chunk_id": chunk_id, "quote": quote, "confidence": 1.0})
        else:
            for item in items:
                result.append({"field_path": field, "chunk_id": chunk_id, "quote": item["quote"], "confidence": 1.0})
    return result


def _resolve(index, units, ids):
    if not ids or len(set(ids)) != len(ids) or any(type(i) is not int or i not in units for i in ids):
        raise _Invalid("来源ID缺失、重复或越界")
    if any(str(i) not in index["locations"] for i in ids): raise _Invalid("来源ID缺少绑定证据")
    return [units[i][1] for i in ids]


def _value(index, units, pointer, operation, proposed, ids):
    kind = _target(_parts(pointer)); values = _resolve(index, units, ids)
    if kind in {"int", "number"}:
        if operation not in {"copy", "sum"} or operation == "copy" and len(ids) != 1:
            raise _Invalid("数值仅接受有依据的copy或sum")
        numbers = [_decimal(v) for v in values]
        with localcontext() as context:
            context.prec = 128
            expected = numbers[0] if operation == "copy" else sum(numbers, Decimal(0))
        if _decimal(proposed) != expected: raise _Invalid("模型数值与所引来源不一致")
        if kind == "int":
            if expected != expected.to_integral_value(): raise _Invalid("目标须为整数")
            return int(expected)
        number = float(expected)
        if not math.isfinite(number) or Decimal(str(number)) != expected: raise _Invalid("目标数值超出可靠精度")
        return number
    if operation == "sum": raise _Invalid("文本或装备字段不能使用sum")
    if operation == "copy":
        if len(values) != 1: raise _Invalid("copy仅接受一项来源")
        expected = values[0]
        if kind == "text" and type(expected) is int: expected = str(expected)
        if type(proposed) is not type(expected) or proposed != expected: raise _Invalid("copy与原文值不一致")
    if kind == "text" and not isinstance(proposed, str): raise _Invalid("目标须为文本")
    if operation == "text" and (kind != "text" or not isinstance(proposed, str)
                                 or not any(isinstance(v, str) and v.strip() for v in values)):
        raise _Invalid("文本语义映射须有原文文本依据，不能生成数值")
    return copy.deepcopy(proposed)


def _empty(base):
    result = copy.deepcopy(base) if base is not None else {"data": {"creation": {}}, "evidence": [],
        "missing_fields": [], "issues": [], "summary": ""}
    for key in ("evidence", "missing_fields", "issues"): result.setdefault(key, [])
    result.setdefault("summary", ""); result.setdefault("data", {}).setdefault("creation", {})
    return result


def _meaningful(value):
    if type(value) in {int, float}: return True
    if isinstance(value, str):
        try: _decimal(value); return True
        except _Invalid: pass
    return value not in (None, "", [], {}, False)


def _satisfied(data, field):
    pointer = field if field.startswith("/") else "/" + field.replace(".", "/")
    try: parts = _parts(pointer)
    except _Invalid: return False
    exists, value = _get(data, parts)
    if not exists: return False
    if field in {"attributes", "creation.raw_attributes"}:
        return isinstance(value, dict) and set(value) == _ATTRS and all(type(v) is int for v in value.values())
    if field == "occupation_skills":
        return isinstance(value, list) and len(value) == 8 and all(isinstance(v, str) and v for v in value) and len(set(value)) == 8
    if field == "creation.luck_rolls": return isinstance(value, list) and bool(value) and all(type(v) is int for v in value)
    return value not in (None, "", [], {})


def validate_character_mapping(index, ledger, *, base_fragment=None, allow_replacements=False):
    return merge_character_mappings(index, [ledger], base_fragment=base_fragment, allow_replacements=allow_replacements)


def _diagnostic_category(message):
    if message.startswith("技能映射"): return "skill_rejection"
    if message.startswith("字段映射被拒"): return "field_rejection"
    if message.startswith(("局部纠正", "本次局部纠正")): return "repair"
    if "来源未映射" in message or message.startswith("待补字段过多"): return "coverage"
    if "冲突" in message: return "conflict"
    return "other"


def _diagnostic_metadata(fragment):
    metadata = fragment.setdefault("data", {}).setdefault("sources", {}).setdefault("$character_mapping", {})
    for key, value in {"version": CHARACTER_MAPPING_VERSION, "fields": {}, "skill_labels": {},
                       "unmapped": [], "indexes": {}, "conflicts": []}.items():
        metadata.setdefault(key, copy.deepcopy(value))
    if "diagnostics" not in metadata:
        metadata["diagnostics"] = [{"category": "other", "message": note}
                                   for note in dict.fromkeys(fragment.get("issues", []))]
    return metadata


def _project_diagnostics(fragment):
    metadata = _diagnostic_metadata(fragment)
    full = metadata["diagnostics"]
    groups = {}
    for row in full: groups.setdefault(row["category"], []).append(row["message"])
    if len(full) <= 100:
        projected = [row["message"] for row in full]
    else:
        labels = {"skill_rejection": "技能映射拒绝", "field_rejection": "字段映射拒绝", "model": "模型识别提醒",
                  "coverage": "未映射来源提醒", "conflict": "来源冲突", "other": "其他识别诊断"}
        projected = []
        for category, notes in groups.items():
            # Repair outcomes remain explicit, even when rejected mappings have
            # saturated the public fragment's fixed diagnostic capacity.
            if category == "repair" or len(notes) == 1:
                projected.extend(notes)
            else:
                projected.append(f"{labels.get(category, '其他识别诊断')}共{len(notes)}项，完整诊断与来源保存在私有映射账本，需核对。")
        projected.append(f"识别诊断共{len(full)}条，已按类别汇总；完整诊断及拒绝项保存在私有来源账本，需核对。")
    fragment["issues"] = projected
    metadata["diagnostic_projection"] = {"contract": "character-diagnostics-v1", "full_count": len(full),
        "projected_count": len(projected), "aggregated": len(full) > 100,
        "category_counts": {category: len(notes) for category, notes in groups.items()}}


def record_character_diagnostic(fragment, message, *, category=None):
    """Retain every active diagnostic privately; project a schema-bounded view."""
    metadata = _diagnostic_metadata(fragment)
    if not any(row["message"] == message for row in metadata["diagnostics"]):
        metadata["diagnostics"].append({"category": category or _diagnostic_category(message), "message": message})
    _project_diagnostics(fragment)


def _remove_character_diagnostic(fragment, message):
    metadata = _diagnostic_metadata(fragment)
    removed = [row for row in metadata["diagnostics"] if row["message"] == message]
    if removed:
        history = metadata.setdefault("diagnostic_history", [])
        entry = {"reason": "coverage_recomputed", "diagnostics": removed}
        if entry not in history: history.append(entry)
        metadata["diagnostics"] = [row for row in metadata["diagnostics"] if row["message"] != message]
    _project_diagnostics(fragment)


def archive_character_diagnostics(fragment, previous, *, reason):
    """A repaired/recomputed diagnostic is history, never an active blocker."""
    metadata = _diagnostic_metadata(fragment)
    old = _diagnostic_metadata(previous)
    history = metadata.setdefault("diagnostic_history", [])
    for entry in old.get("diagnostic_history", []):
        if entry not in history: history.append(copy.deepcopy(entry))
    if old["diagnostics"]:
        entry = {"reason": reason, "diagnostics": copy.deepcopy(old["diagnostics"])}
        if entry not in history: history.append(entry)


def merge_character_mappings(index, ledgers, *, base_fragment=None, allow_replacements=False):
    """Union verified map rows; conflicting values are never last-writer-wins."""
    result = _empty(base_fragment); data = result["data"]
    def issue(message, *, category=None):
        record_character_diagnostic(result, message, category=category)
    if not isinstance(index, dict) or index.get("issues"):
        for note in index.get("issues", []) if isinstance(index, dict) else []: issue(note)
        issue("来源索引不可用，未生成角色事实"); return result
    units = {row[0]: (row[1], row[2]) for row in index.get("units", [])}
    namespace = index.get("index_id", "")
    metadata = _diagnostic_metadata(result)
    previous_index = metadata["indexes"].get(namespace)
    if previous_index is not None and previous_index.get("units") != index.get("units", [])[:len(previous_index.get("units", []))]:
        issue("原有来源ID发生变化，未采用不一致修订"); return result
    metadata["indexes"][namespace] = {"source": copy.deepcopy(index.get("source", {})), "units": copy.deepcopy(index.get("units", []))}
    old_coverage = [row for row in metadata["unmapped"] if row["index_id"] == namespace]
    if old_coverage:
        metadata.setdefault("coverage_history", []).append(copy.deepcopy(old_coverage))
    metadata["unmapped"] = [row for row in metadata["unmapped"] if row["index_id"] != namespace]
    coverage_note = "仍有来源未映射为角色字段，原值与路径已保留，需核对资料完整性"
    # Only this module's aggregate note is recomputed; concrete ambiguities and
    # validation errors are never erased merely because a supplement exists.
    if not metadata["unmapped"]:
        _remove_character_diagnostic(result, coverage_note)
    candidates = {}; dispositions = {}; label_proofs = {}; used = set(); evidence_groups = {}
    def rejected(ledger_number, kind, row_number, target, source_ids, reason):
        metadata.setdefault("rejections", []).append({"ledger": ledger_number, "kind": kind,
            "row": row_number, "target": target, "source_ids": list(source_ids), "reason": str(reason)[:300]})
    for ledger_number, value in enumerate(ledgers):
        try:
            parsed = value if isinstance(value, CharacterMappingLedger) else CharacterMappingLedger.model_validate(value, strict=True)
        except ValueError:
            issue("模型映射账本结构不合法，未采用该片段"); continue
        if parsed.summary: result["summary"] = parsed.summary
        for note in parsed.issues: issue(note, category="model")
        result["missing_fields"].extend(q for q in parsed.q if q not in result["missing_fields"])
        for source_id, disposition, reason in parsed.d:
            if source_id not in units:
                issue("来源分类引用越界ID"); continue
            row = (disposition, reason)
            if source_id in dispositions and dispositions[source_id] != row:
                dispositions[source_id] = ("unresolved", "同一来源的分类存在冲突")
            else: dispositions[source_id] = row
        rows = [("m", row_number, row) for row_number, row in enumerate(parsed.m)]
        for skill_number, (name, components, proof_ids) in enumerate(parsed.k):
            try:
                if not name.strip(): raise _Invalid("技能名称为空")
                ids = [i for i in components if i is not None]
                numbers = _resolve(index, units, ids); _resolve(index, units, proof_ids)
                numeric = []
                for source_id, source_value in zip(ids, numbers):
                    try: numeric.append(_decimal(source_value))
                    except _Invalid: raise _Invalid("来源ID " + str(source_id) + " 不是明确十进制数字") from None
                if any(v != v.to_integral_value() for v in numeric): raise _Invalid("技能分量须为整数")
                if any(i is None for i in components[:3]): raise _Invalid("技能基础/职业/兴趣来源不完整，请只映射明确总值")
                with localcontext() as context:
                    context.prec = 128
                    total = int(sum(numeric, Decimal(0)))
                rows.append(("k", skill_number, ("/skills/" + _escape(name), "sum", total, ids)))
                for field, source_id in zip(("base", "occupation", "interest"), components[:3]):
                    rows.append(("k", skill_number, ("/allocations/" + _escape(name) + "/" + field, "copy", int(_decimal(units[source_id][1])), [source_id])))
                label_proofs[name] = {"index_id": namespace, "source_ids": list(proof_ids),
                    "original_labels": [[i, units[i][0], copy.deepcopy(units[i][1])] for i in proof_ids],
                    "semantic_authority": "model_selected_label_not_program_verified_alias"}
            except (ValueError, TypeError) as exc:
                rejected(ledger_number, "k", skill_number, name, list(dict.fromkeys(
                    [i for i in components if i is not None] + proof_ids)), exc)
                issue("技能映射「" + name + "」被拒：" + str(exc)[:160] + "；分量IDs="
                      + str(components) + "；名称IDs=" + str(proof_ids[:20])
                      + ("（其余名称ID见账本）" if len(proof_ids) > 20 else ""))
        for row_kind, row_number, (pointer, operation, proposed, ids) in rows:
            try:
                resolved = _value(index, units, pointer, operation, proposed, ids)
                candidates.setdefault(pointer, []).append({"value": resolved, "source_ids": list(ids), "operation": operation})
            except (ValueError, TypeError, OverflowError) as exc:
                target = pointer if row_kind == "m" else _parts(pointer)[1]
                rejected(ledger_number, row_kind, row_number, target, ids, exc)
                issue("字段映射被拒：" + pointer + "；" + str(exc)[:160] + "；来源IDs=" + str(ids[:20])
                      + ("（其余ID见账本）" if len(ids) > 20 else ""))
    attribute_sources = set()
    for pointer, values in candidates.items():
        if pointer.startswith("/attributes/"):
            attribute_sources.update((namespace, source_id) for row in values for source_id in row["source_ids"])
    for pointer, proofs in metadata["fields"].items():
        if pointer.startswith("/attributes/"):
            attribute_sources.update((p["index_id"], i) for p in proofs for i in p["source_ids"])
    for pointer, rows in candidates.items():
        parts = _parts(pointer)
        ids = list(dict.fromkeys(i for row in rows for i in row["source_ids"]))
        if pointer.startswith("/creation/raw_attributes/") and any((namespace, i) in attribute_sources for i in ids):
            issue("创建前属性与当前属性复用同一来源，缺少独立创建证据"); continue
        if any(type(row["value"]) is not type(rows[0]["value"]) or row["value"] != rows[0]["value"] for row in rows[1:]):
            metadata["conflicts"].append({"target": pointer, "index_id": namespace, "candidates": copy.deepcopy(rows)})
            issue("多个来源对同一字段映射冲突，未选择覆盖：" + pointer); continue
        exists, previous = _get(data, parts); value = rows[0]["value"]
        replacement = exists and (type(previous) is not type(value) or previous != value)
        if replacement and not (allow_replacements and all(index["locations"][str(i)].get("source_kind") == "supplement" for i in ids)):
            issue("原字段与新增来源冲突，原值保留待明确补充：" + pointer); continue
        # Allocation leaf arithmetic was individually verified above. A single
        # record citation may now cover its three leaves, while the private
        # fields ledger retains each leaf's exact source IDs and operation.
        citation_pointer = "/" + "/".join(_escape(p) for p in parts[:2]) if parts[0] == "allocations" else pointer
        previous_group = evidence_groups.get(citation_pointer, {"ids": [], "evidence": []})
        citation_ids = list(dict.fromkeys(previous_group["ids"] + ids))
        evidence = _evidence(index, citation_pointer, citation_ids)
        count = sum(len(group["evidence"]) for group in evidence_groups.values()) - len(previous_group["evidence"]) + len(evidence)
        if not evidence or len(result["evidence"]) + count > 500:
            issue("映射证据超过安全数量或不可定位，未截断原文或采用剩余字段"); continue
        candidate = copy.deepcopy(data)
        try: _put(candidate, parts, value)
        except _Invalid:
            issue("目标路径与已整理结构冲突，原值保留：" + pointer); continue
        data.clear(); data.update(candidate)
        # data was copied above; reacquire the mutable private metadata object.
        metadata = data["sources"]["$character_mapping"]
        if replacement:
            for previous in metadata["fields"].get(pointer, []):
                if previous.get("active", True):
                    previous["active"] = False
                    metadata.setdefault("superseded_sources", []).extend({"index_id": previous["index_id"], "id": old_id,
                        "target": pointer, "by_index_id": namespace, "by_source_ids": ids} for old_id in previous["source_ids"])
        metadata["fields"].setdefault(pointer, []).extend({"index_id": namespace, "active": True, **copy.deepcopy(row)} for row in rows)
        evidence_groups[citation_pointer] = {"ids": citation_ids, "evidence": evidence}; used.update(ids)
        if pointer.startswith("/skills/") and parts[1] in label_proofs:
            proof = label_proofs[parts[1]]; metadata["skill_labels"][parts[1]] = copy.deepcopy(proof)
            used.update(proof["source_ids"])
    # Enforce this invariant in both orders, including fields inherited from a
    # previous fragment: current values are not independent creation records.
    all_current = {(proof["index_id"], i) for pointer, proofs in metadata["fields"].items()
                   if pointer.startswith("/attributes/") for proof in proofs if proof.get("active", True)
                   for i in proof["source_ids"]}
    for pointer, proofs in metadata["fields"].items():
        if not pointer.startswith("/creation/raw_attributes/"): continue
        conflicting = [proof for proof in proofs if proof.get("active", True)
                       and any((proof["index_id"], i) in all_current for i in proof["source_ids"])]
        if conflicting:
            exists, original = _get(data, _parts(pointer))
            if exists: _delete(data, _parts(pointer))
            for proof in conflicting: proof["active"] = False
            metadata.setdefault("creation_evidence_conflicts", []).append({"target": pointer,
                "value": copy.deepcopy(original), "proofs": copy.deepcopy(conflicting)})
            issue("创建前属性与当前属性复用同一来源，缺少独立创建证据")
    existing_evidence = {(e["field_path"], e["chunk_id"], e["quote"]) for e in result["evidence"]}
    for group in evidence_groups.values():
        for evidence in group["evidence"]:
            identity = (evidence["field_path"], evidence["chunk_id"], evidence["quote"])
            if identity not in existing_evidence:
                result["evidence"].append(evidence); existing_evidence.add(identity)
    # Unchanged fields retain their original coverage in the combined index.
    for proofs in metadata["fields"].values():
        used.update(i for proof in proofs if proof["index_id"] == namespace and proof.get("active", True) for i in proof["source_ids"])
    for proof in metadata["skill_labels"].values():
        if proof["index_id"] == namespace: used.update(proof["source_ids"])
    used.update(row["id"] for row in metadata.get("superseded_sources", []) if row["index_id"] == namespace)
    # Keep every unused source value and its path. A nonzero value cannot be
    # hidden by an 'ignored' classification or a claim of complete extraction.
    for source_id, (pointer, value) in units.items():
        if source_id in used: continue
        disposition, reason = dispositions.get(source_id, ("unresolved", "模型未说明该来源的去向"))
        numeric = False
        try: _decimal(value); numeric = True
        except _Invalid: pass
        if disposition == "ignored" and numeric:
            disposition = "unresolved"; reason = "数值来源不得静默忽略；" + reason
        metadata["unmapped"].append({"index_id": namespace, "id": source_id, "pointer": pointer,
                                    "value": copy.deepcopy(value), "disposition": disposition, "reason": reason})
        if disposition == "unresolved" or disposition == "preserved" and _meaningful(value):
            issue(coverage_note)
    for field in ("name", "era", "age", "occupation", "attributes", "skills", "occupation_skills", "luck",
                  "cash", "assets", "creation.method", "creation.raw_attributes", "creation.luck_rolls"):
        if not _satisfied(data, field) and field not in result["missing_fields"]: result["missing_fields"].append(field)
    result["missing_fields"] = [f for f in result["missing_fields"] if not _satisfied(data, f)]
    result["missing_fields"] = list(dict.fromkeys(result["missing_fields"]))
    if len(result["missing_fields"]) > 100:
        metadata["all_missing_fields"] = copy.deepcopy(result["missing_fields"])
        result["missing_fields"] = result["missing_fields"][:99] + ["其余待补字段见完整来源账本"]
        issue("待补字段过多，完整问题已保留；未截断原文或声称资料完整")
    return result
