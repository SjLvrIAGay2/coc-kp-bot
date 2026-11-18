"""Finite read-only Excel extraction. Never calculates, executes or fetches.

OOXML is inspected before openpyxl sees it; sparse cell grids and legacy BIFF
dimensions are bounded before library sheet iteration/allocation. Sheet/cell
coordinates remain source evidence, never inferred character fields.
"""
from __future__ import annotations

import io
import json
import math
import re
import struct
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import PurePosixPath

from defusedxml import ElementTree as SafeET

SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
EXCEL_EXTRACTION_CONTRACT = "spreadsheet-sparse-v1"
MAX_SPREADSHEET_CELLS = 100_000
MAX_SPREADSHEET_SHEETS = 64
MAX_SPREADSHEET_GRID = 250_000
MAX_SPREADSHEET_XML_NODES = 500_000
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_XLSM = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
_WORKBOOK_MAIN_TYPES = {
    _XLSX, _XLSM,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "application/vnd.ms-excel.template.macroEnabled.main+xml",
}


class SpreadsheetError(ValueError):
    """Fixed diagnostics only; no cell contents, links or parser traces."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _address(row, col):
    from openpyxl.utils.cell import get_column_letter
    return get_column_letter(col) + str(row)


def _bounds(rows, cols):
    if rows < 0 or cols < 0 or rows > 1_048_576 or cols > 16_384 or rows * cols > MAX_SPREADSHEET_GRID:
        raise SpreadsheetError("Excel行列跨度超过读取资源限制，请拆分工作表")


def _xml(raw, node_count):
    try:
        iterator = SafeET.iterparse(io.BytesIO(raw), events=("start", "end"),
                                   forbid_dtd=True, forbid_entities=True, forbid_external=True)
        depth = 0
        for event, _ in iterator:
            if event == "start":
                depth += 1; node_count[0] += 1
                if depth > 80 or node_count[0] > MAX_SPREADSHEET_XML_NODES:
                    raise SpreadsheetError("Excel XML结构超过读取资源限制")
            else:
                depth -= 1
        return iterator.root
    except SpreadsheetError:
        raise
    except Exception:
        raise SpreadsheetError("Excel XML损坏或包含禁止的DTD或实体") from None


def _workbook_content_type(root):
    """OPC resolves a part Override first, then its extension Default."""
    namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    if root.tag != namespace + "Types":
        raise SpreadsheetError("Excel内容类型声明不合法")
    defaults = {}; overrides = {}
    for item in root:
        if item.tag == namespace + "Default":
            if set(item.attrib) != {"Extension", "ContentType"}:
                raise SpreadsheetError("Excel内容类型声明不合法")
            key = item.attrib["Extension"].lower(); target = defaults
            if not key or any(c in key for c in "/\\.:") or key.strip() != key:
                raise SpreadsheetError("Excel内容类型扩展名不合法")
        elif item.tag == namespace + "Override":
            if set(item.attrib) != {"PartName", "ContentType"}:
                raise SpreadsheetError("Excel内容类型声明不合法")
            key = item.attrib["PartName"]; target = overrides
            if not key.startswith("/") or "\\" in key or ":" in key or ".." in key.split("/"):
                raise SpreadsheetError("Excel内容类型部件名称不合法")
        else:
            raise SpreadsheetError("Excel内容类型声明不合法")
        mime = item.attrib["ContentType"]
        if key in target or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime):
            raise SpreadsheetError("Excel内容类型声明重复或不合法")
        # openpyxl searches every recognized workbook-main declaration. Bind
        # its selection to the same canonical part inspected by our preflight.
        if mime in _WORKBOOK_MAIN_TYPES and key != ("xml" if target is defaults else "/xl/workbook.xml"):
            raise SpreadsheetError("Excel主工作簿声明指向非受检部件")
        target[key] = mime
    return overrides.get("/xl/workbook.xml", defaults.get("xml"))


def _package(raw, suffix):
    if not raw.startswith(b"PK\x03\x04"):
        raise SpreadsheetError("Excel OOXML文件签名不符")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist(); names = [e.filename for e in entries]
            if len(entries) > 2048 or len(names) != len(set(names)) or sum(e.file_size for e in entries) > 40 * 1024 * 1024:
                raise SpreadsheetError("Excel ZIP展开资源超过限制")
            roots = {}; nodes = [0]; external = False; macros = False
            for entry in entries:
                name = entry.filename; lower = name.lower()
                if ("\\" in name or name.startswith("/") or ":" in name or ".." in name.split("/")
                        or entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise SpreadsheetError("Excel ZIP路径、链接或加密条目不允许")
                if entry.file_size > 10 * 1024 * 1024 or entry.file_size > max(1, entry.compress_size) * 200:
                    raise SpreadsheetError("Excel ZIP单项大小或压缩比超过限制")
                if "/embeddings/" in lower or "/activex/" in lower or lower.endswith((".exe", ".dll")):
                    raise SpreadsheetError("Excel嵌入对象或活动控件不支持")
                if "vbaproject" in lower:
                    macros = True
                    if suffix != ".xlsm": raise SpreadsheetError("Excel宏内容与文件类型不符")
                if lower.endswith((".xml", ".rels")):
                    roots[name] = _xml(archive.read(entry), nodes)
                    if lower.endswith(".rels"):
                        external |= any(r.attrib.get("TargetMode", "").lower() == "external" for r in roots[name])
            needed = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not needed <= roots.keys(): raise SpreadsheetError("Excel缺少Workbook或内容类型声明")
            mime = _workbook_content_type(roots["[Content_Types].xml"])
            if mime != (_XLSM if suffix == ".xlsm" else _XLSX):
                raise SpreadsheetError("Excel Workbook内容类型与扩展名不符")
            relations = {}
            for rel in roots["xl/_rels/workbook.xml.rels"]:
                ident = rel.attrib.get("Id")
                if not ident or ident in relations: raise SpreadsheetError("Excel关系标识不合法")
                relations[ident] = rel.attrib
            sheets = []; count = 0; total_grid = 0; seen = set()
            for element in roots["xl/workbook.xml"].iter():
                if _local(element.tag) != "sheet": continue
                title = element.attrib.get("name", "")
                if not title or len(title) > 256 or title.casefold() in seen:
                    raise SpreadsheetError("Excel工作表名称不合法")
                seen.add(title.casefold())
                rid = next((v for k, v in element.attrib.items() if k.endswith("}id")), None)
                rel = relations.get(rid, {})
                if not rel.get("Type", "").endswith("/worksheet") or rel.get("TargetMode", "").lower() == "external":
                    raise SpreadsheetError("Excel仅支持本文件内的普通工作表")
                target = rel.get("Target", "")
                if "\\" in target or ":" in target or ".." in target.split("/") or "%" in target:
                    raise SpreadsheetError("Excel工作表关系路径不合法")
                target = target.lstrip("/") if target.startswith("/") else str(PurePosixPath("xl") / target)
                if target not in roots or not target.startswith("xl/worksheets/"):
                    raise SpreadsheetError("Excel缺少工作表正文")
                coords = set(); formulas = {}; merges = []; mr = mc = 0
                for source_row in roots[target].iter():
                    if _local(source_row.tag) != "row": continue
                    number = source_row.attrib.get("r", "")
                    if not re.fullmatch(r"[1-9][0-9]{0,6}", number):
                        raise SpreadsheetError("Excel行缺少可审计坐标")
                    for source_cell in source_row:
                        if _local(source_cell.tag) == "c" and not re.fullmatch(r"[A-Z]{1,3}"+number, source_cell.attrib.get("r", "")):
                            raise SpreadsheetError("Excel行与单元格坐标不一致")
                for item in roots[target].iter():
                    kind = _local(item.tag)
                    if kind == "c":
                        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", item.attrib.get("r", ""))
                        if not match: raise SpreadsheetError("Excel单元格缺少可审计坐标")
                        from openpyxl.utils.cell import column_index_from_string
                        row, col = int(match[2]), column_index_from_string(match[1])
                        if (row, col) in coords: raise SpreadsheetError("Excel单元格坐标重复")
                        coords.add((row, col)); count += 1; mr = max(mr, row); mc = max(mc, col)
                        f = next((v for v in item if _local(v.tag) == "f"), None)
                        if f is not None: formulas[(row, col)] = {"source_text": f.text, "attributes": dict(f.attrib)}
                    elif kind == "mergeCell":
                        merged = item.attrib.get("ref", "")
                        from openpyxl.utils.cell import range_boundaries
                        try:
                            c1,r1,c2,r2 = range_boundaries(merged)
                            _bounds(r2,c2)
                            if c1>c2 or r1>r2: raise ValueError()
                        except (ValueError,TypeError):
                            raise SpreadsheetError("Excel合并范围不合法或超过限制") from None
                        merges.append(merged)
                _bounds(mr, mc); total_grid += mr * mc
                if count > MAX_SPREADSHEET_CELLS or total_grid > MAX_SPREADSHEET_GRID:
                    raise SpreadsheetError("Excel单元格数量或总行列跨度超过限制")
                sheets.append({"name": title, "state": element.attrib.get("state", "visible"),
                    "coordinates": coords, "formulas": formulas, "merges": merges, "rows": mr, "cols": mc})
                if len(sheets) > MAX_SPREADSHEET_SHEETS: raise SpreadsheetError("Excel工作表数量超过限制")
            if not sheets: raise SpreadsheetError("Excel没有可读取的工作表")
            return sheets, external, macros
    except SpreadsheetError:
        raise
    except Exception:
        raise SpreadsheetError("Excel ZIP结构损坏或不受支持") from None


def _open_xls(raw):
    if not raw.startswith(_OLE): raise SpreadsheetError("XLS文件签名不符；不接受改名HTML或文本")
    import xlrd
    book = xlrd.open_workbook(file_contents=raw, on_demand=True, ragged_rows=True,
        formatting_info=True, use_mmap=False, logfile=io.StringIO(), ignore_workbook_corruption=False)
    try:
        if not 1 <= book.nsheets <= MAX_SPREADSHEET_SHEETS: raise SpreadsheetError("Excel工作表数量超过限制")
        # xlrd 2.x exposes the bounded Workbook stream before loading sheets.
        # Scan dimensions and cell records first; never allocate a sparse grid.
        count = grid = 0
        for start in book._sh_abs_posn:
            pos = start; records = 0; mr = mc = 0
            while pos + 4 <= len(book.mem):
                code, length = struct.unpack_from("<HH", book.mem, pos); pos += 4
                if pos + length > len(book.mem): raise SpreadsheetError("XLS记录长度损坏")
                data = book.mem[pos:pos + length]; pos += length; records += 1
                if records > 250_000: raise SpreadsheetError("XLS记录数量超过限制")
                if code == 0x000A: break
                if code == 0x0200:
                    if book.biff_version >= 80 and length >= 12:
                        _, rows, _, cols = struct.unpack_from("<IIHH", data)
                    elif length >= 8:
                        _, rows, _, cols = struct.unpack_from("<HHHH", data)
                    else: raise SpreadsheetError("XLS维度记录损坏")
                    mr = max(mr, rows); mc = max(mc, cols); _bounds(mr, mc)
                if code in {0x0203, 0x027E, 0x00BD, 0x0201, 0x00BE, 0x0205, 0x0204, 0x00FD, 0x0006, 0x00D6}:
                    if length < 4: raise SpreadsheetError("XLS单元格记录损坏")
                    row, col = struct.unpack_from("<HH", data)
                    last_col = struct.unpack_from("<H", data, length - 2)[0] if code in {0x00BD, 0x00BE} else col
                    if last_col < col: raise SpreadsheetError("XLS单元格范围损坏")
                    count += last_col - col + 1; mr = max(mr, row + 1); mc = max(mc, last_col + 1); _bounds(mr, mc)
                    if count > MAX_SPREADSHEET_CELLS: raise SpreadsheetError("Excel单元格数量超过限制")
            else: raise SpreadsheetError("XLS工作表未正常结束")
            grid += mr * mc
            if grid > MAX_SPREADSHEET_GRID: raise SpreadsheetError("Excel总行列跨度超过限制")
        return book
    except BaseException:
        book.release_resources(); raise


def validate_spreadsheet(raw, suffix, *, max_bytes=20 * 1024 * 1024):
    """Content gate shared by local import and controlled attachment download."""
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= max_bytes or suffix not in SPREADSHEET_SUFFIXES:
        raise SpreadsheetError("Excel文件大小或类型不符合限制")
    try:
        if suffix == ".xls":
            book = _open_xls(bytes(raw)); book.release_resources()
        else: _package(bytes(raw), suffix)
    except SpreadsheetError:
        raise
    except Exception:
        raise SpreadsheetError("Excel文件损坏、加密或格式不支持") from None


def _scalar(value):
    if value is None or type(value) in {str, bool, int}: return value
    if isinstance(value, (datetime, date, time)): return value.isoformat()
    if isinstance(value, timedelta): return str(value)
    if type(value) is float and math.isfinite(value): return value
    raise SpreadsheetError("Excel单元格数值或类型不支持")


def extract_spreadsheet(raw, suffix, *, max_bytes=20 * 1024 * 1024, max_chars=5_000_000):
    validate_spreadsheet(raw, suffix, max_bytes=max_bytes)
    blocks = []; issues = []; sheets = []; chars = 0
    def append(block):
        nonlocal chars
        chars += len(block["text"]) + 1
        if chars > max_chars: raise SpreadsheetError("Excel提取文本超过资源限制")
        blocks.append(block)
    def begin(name, state, merges):
        index = len(sheets) + 1; sheets.append({"name": name, "state": state, "index": index})
        base = {"sheet": name, "sheet_index": index, "sheet_state": state, "style": ""}
        loc = "excel:sheet:" + json.dumps(name, ensure_ascii=False)
        heading = f"工作表 {json.dumps(name,ensure_ascii=False)}（{state}）"
        if merges: heading += "；合并范围仅保留源单元格，不补齐空格"
        append({**base, "kind": "spreadsheet_sheet", "text": heading, "locator": loc})
        for merged in merges:
            append({**base, "kind": "spreadsheet_merge", "range": merged, "text": f"合并范围 {merged}", "locator": loc + ":merge:" + merged})
        return base, loc
    def cell(base, loc, row, col, value, **extra):
        # A styled empty cell has no source value. Do not serialize its layout
        # as a null character field; formulas with missing caches are evidence.
        if value is None and "formula" not in extra: return
        address = _address(row, col); scalar = _scalar(value)
        display = json.dumps(scalar, ensure_ascii=False, allow_nan=False)
        if "formula" in extra:
            display = "公式 " + json.dumps(extra["formula"], ensure_ascii=False) + ("；已存缓存 " + json.dumps(extra["cached_value"],ensure_ascii=False) if extra["cached_available"] else "；无可用缓存，未计算")
        append({**base, "kind": "spreadsheet_cell", "row": row, "col": col, "cell": address,
            "value": scalar, "value_type": type(value).__name__, "text": address + " = " + display,
            "locator": loc + ":cell:" + address, **extra})
    try:
        if suffix == ".xls":
            import xlrd
            book = _open_xls(bytes(raw))
            issues.append({"message": "XLS仅读取已存储或公式缓存值；原公式文本不可得，未重新计算；宏和外部链接未执行或访问"})
            try:
                for index in range(book.nsheets):
                    sheet = book.sheet_by_index(index); _bounds(sheet.nrows, sheet.ncols)
                    merges = [_address(r1+1,c1+1)+":"+_address(r2,c2) for r1,r2,c1,c2 in sheet.merged_cells]
                    base, loc = begin(sheet.name, {0:"visible",1:"hidden",2:"veryHidden"}.get(sheet.visibility,"hidden"), merges)
                    for row in range(sheet.nrows):
                        for col in range(sheet.row_len(row)):
                            item = sheet.cell(row,col)
                            if item.ctype == xlrd.XL_CELL_EMPTY: continue
                            value = item.value
                            if item.ctype == xlrd.XL_CELL_BOOLEAN: value = bool(value)
                            elif item.ctype == xlrd.XL_CELL_DATE: value = xlrd.xldate_as_datetime(value,book.datemode)
                            elif item.ctype == xlrd.XL_CELL_ERROR: value = xlrd.error_text_from_code.get(value,"Excel错误")
                            cell(base,loc,row+1,col+1,value,value_origin="stored_or_formula_cache_unclassified")
                    book.unload_sheet(index)
            finally: book.release_resources()
        else:
            import openpyxl
            specs, external, macros = _package(bytes(raw),suffix)
            if external: issues.append({"message": "外部链接未访问；只读取本文件现有文本、公式及缓存"})
            if macros or suffix == ".xlsm": issues.append({"message": "宏未加载或执行；仅提取普通工作表资料"})
            formula_book = openpyxl.load_workbook(io.BytesIO(raw),read_only=True,data_only=False,keep_vba=False,keep_links=False)
            cached_book = None
            try:
                cached_book = openpyxl.load_workbook(io.BytesIO(raw),read_only=True,data_only=True,keep_vba=False,keep_links=False)
                for spec in specs:
                    base,loc = begin(spec["name"],spec["state"],spec["merges"])
                    sheet, cached = formula_book[spec["name"]],cached_book[spec["name"]]
                    sheet.reset_dimensions(); cached.reset_dimensions()
                    if not spec["rows"]: continue
                    options = dict(min_row=1,max_row=spec["rows"],min_col=1,max_col=spec["cols"])
                    for row_index,(a,b) in enumerate(zip(sheet.iter_rows(**options),cached.iter_rows(**options)),1):
                        for col_index,(item,cache) in enumerate(zip(a,b),1):
                            if (row_index,col_index) not in spec["coordinates"]: continue
                            if item.data_type == "f" or (row_index,col_index) in spec["formulas"]:
                                formula = item.value if isinstance(item.value,str) else getattr(item.value,"text",None)
                                if formula is None: formula = "[公式定义文本不可展开，未计算]"
                                available = cache.value is not None
                                extra = {"formula":formula,"cached_value":_scalar(cache.value),"cached_available":available,
                                    "formula_source":spec["formulas"].get((row_index,col_index)),"calculated":False}
                                if not available: issues.append({"sheet":spec["name"],"cell":_address(row_index,col_index),"message":"公式无可用缓存；未计算，不能当成零或审核依据"})
                                cell(base,loc,row_index,col_index,None,**extra)
                            else: cell(base,loc,row_index,col_index,item.value)
            finally:
                formula_book.close()
                if cached_book is not None: cached_book.close()
    except SpreadsheetError:
        raise
    except Exception:
        raise SpreadsheetError("Excel工作表内容损坏或格式不支持") from None
    return {"blocks":blocks,"issues":issues,"sheets":sheets,"extraction_contract":EXCEL_EXTRACTION_CONTRACT,
            "formula_policy":"stored_only_never_calculated","trusted_instructions":False}
