"""Bounded, read-only document import and local provenance-preserving retrieval."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import io
import zipfile
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARS = 5_000_000
SUPPORTED_DOCUMENTS = {".txt", ".md", ".json", ".pdf", ".docx", ".xlsx", ".xls", ".xlsm", ".png", ".jpg", ".jpeg", ".webp"}
IMAGE_DOCUMENTS={".png", ".jpg", ".jpeg", ".webp"}
DOCX_EXTRACTION_CONTRACT = "docx-structure-v2"
PDF_EXTRACTION_CONTRACT = "pdf-content-stream-v2"
PDF_LAYOUT_CONTRACT = "pdf-layout-v1"
MAX_PDF_PAGES = 1500
MAX_PDF_LAYOUT_PAGES = 20
MAX_PDF_PAGE_STREAM_BYTES = 16 * 1024 * 1024
MAX_PDF_TOTAL_STREAM_BYTES = 80 * 1024 * 1024
MAX_PDF_LAYOUT_CELLS = 50_000


def _safe_xml(raw: bytes):
    # Also catches UTF16/32 DTDs, before ElementTree could expand entities.
    upper=raw.replace(b"\x00",b"").upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DOCX XML禁止DTD或实体声明")
    try: return ET.fromstring(raw)
    except ET.ParseError as exc: raise ValueError("DOCX XML格式损坏") from exc


def extract_docx(raw: bytes) -> list[dict]:
    """Read visible Word text and separate ancillary parts without running Office.

    Locators identify package parts, paragraphs and table rows; they never claim
    to be rendered Word page numbers. Body order and existing body locators are
    stable. Notes, headers, footers and editorial comments are appended once in
    their own section, rather than silently omitted or mixed into body text.
    """
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("DOCX文件超过大小限制")
    ns={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    w="{"+ns["w"]+"}"
    try: archive=zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc: raise ValueError("DOCX不是有效ZIP文档") from exc
    with archive:
        entries=archive.infolist();names=[entry.filename for entry in entries]
        if len(entries)>2048 or len(names)!=len(set(names)) or sum(e.file_size for e in entries)>40*1024*1024:
            raise ValueError("DOCX ZIP条目数量或展开大小超过限制")
        for entry in entries:
            name=entry.filename
            if "\\" in name or name.startswith("/") or ":" in name or ".." in name.split("/") or entry.flag_bits&1:
                raise ValueError("DOCX ZIP路径或加密条目不允许")
            if entry.file_size>10*1024*1024 or entry.file_size>max(1,entry.compress_size)*200:
                raise ValueError("DOCX ZIP压缩比或单项大小超过限制")
            if "vbaproject" in name.lower() or name.lower().endswith((".exe",".dll")):
                raise ValueError("DOCX中禁止宏或可执行内容")
            if name.endswith(".rels"):
                for relation in _safe_xml(archive.read(name)).iter():
                    target=relation.attrib.get("Target","")
                    if relation.attrib.get("TargetMode","").lower()=="external" or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|\\\\)",target,re.I):
                        # Read only the displayed w:t text of ordinary links.
                        # No relation target is opened, resolved, or downloaded.
                        link_type=relation.attrib.get("Type","")
                        if link_type in {"http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                                         "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink"}:
                            try:scheme=urlsplit(target).scheme.lower()
                            except ValueError:scheme=""
                            if scheme in {"http","https","mailto"}:continue
                        raise ValueError("DOCX包含外部引用，导入器不访问外部资源")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise ValueError("DOCX缺少正文或内容类型声明")
        types=archive.read("[Content_Types].xml")
        _safe_xml(types)
        normalized_types=types.replace(b"\x00",b"").lower()
        if b"macroenabled" in normalized_types or b"vbaproject" in normalized_types: raise ValueError("DOCX不允许宏内容类型")
        def xml(name):
            root = _safe_xml(archive.read(name))
            # ISO strict Word packages use the same local names and structure.
            strict = "{http://purl.oclc.org/ooxml/wordprocessingml/main}"
            for element in root.iter():
                if element.tag.startswith(strict):
                    element.tag = w + element.tag[len(strict):]
                element.attrib = {(w + key[len(strict):] if key.startswith(strict) else key): value
                                  for key, value in element.attrib.items()}
            return root

        styles = {}
        if "word/styles.xml" in names:
            for style in xml("word/styles.xml").findall("w:style",ns):
                identity = style.get(w+"styleId", "")
                label = style.find("w:name", ns)
                based_on = style.find("w:basedOn", ns)
                outline = style.find("w:pPr/w:outlineLvl", ns)
                styles[identity] = {
                    "name": label.get(w+"val", "") if label is not None else "",
                    "based_on": based_on.get(w+"val", "") if based_on is not None else "",
                    "outline": outline.get(w+"val", "") if outline is not None else "",
                }

        @lru_cache(maxsize=4096)
        def heading_level(identity):
            seen = set()
            while identity and identity not in seen:
                seen.add(identity)
                details = styles.get(identity, {})
                outline = details.get("outline", "")
                if outline.isdigit():
                    return int(outline)+1 if int(outline)<9 else None
                for label in (identity, details.get("name", "")):
                    match = re.fullmatch(r"(?:heading\s*|标题\s*)([1-9])", label, re.I)
                    if match:
                        return int(match[1])
                identity = details.get("based_on", "")
            return None

        root=xml("word/document.xml");body=root.find("w:body",ns)
        if body is None: raise ValueError("DOCX缺少正文")
        blocks=[];total_chars=0
        def append(block):
            nonlocal total_chars
            total_chars += len(block["text"]) + 1
            if total_chars > MAX_EXTRACTED_CHARS or len(blocks)>=100_000:
                raise ValueError("DOCX文本或结构超过资源限制")
            blocks.append(block)

        ignored = {w+"del", w+"moveFrom"}
        alternate = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
        def visible_children(element):
            children = list(element)
            if element.tag == alternate:
                # A Word fallback repeats the same content; take exactly one.
                return children[:1]
            return children

        def words(element):
            parts=[];stack=[element];first_paragraph=True
            while stack:
                item=stack.pop()
                if item.tag in ignored: continue
                if item.tag==w+"t": parts.append(item.text or "")
                elif item.tag==w+"tab": parts.append("\t")
                elif item.tag in {w+"br",w+"cr"}: parts.append("\n")
                elif item.tag==w+"noBreakHyphen": parts.append("\u2011")
                elif item.tag==w+"softHyphen": parts.append("\u00ad")
                elif item.tag==w+"tc" and item is not element and parts: parts.append("\t")
                elif item.tag==w+"p":
                    if not first_paragraph: parts.append("\n")
                    first_paragraph=False
                elif item.tag==w+"altChunk": raise ValueError("DOCX外部/嵌入替代正文不能直接提取")
                stack.extend(reversed(visible_children(item)))
            return "".join(parts)

        def references(element):
            result=[];stack=[element]
            tags={w+"footnoteReference":"footnote",w+"endnoteReference":"endnote",w+"commentReference":"comment"}
            while stack:
                item=stack.pop()
                if item.tag in ignored: continue
                if item.tag in tags:
                    result.append({"section":tags[item.tag],"id":item.get(w+"id","")})
                stack.extend(reversed(visible_children(item)))
            return result

        def walk(parent,locator,context,depth=0):
            if depth>100: raise ValueError("DOCX结构嵌套超过资源限制")
            for index,item in enumerate(parent):
                position=f"{locator}/{index}"
                if item.tag==w+"p":
                    style=item.find("w:pPr/w:pStyle",ns)
                    identity=style.attrib.get(w+"val","") if style is not None else ""
                    block={"kind":"paragraph","text":words(item),"locator":position,
                        "style":identity,**context}
                    if identity:
                        block["style_name"]=styles.get(identity,{}).get("name",identity)
                        block["heading_level"]=heading_level(identity)
                    outline=item.find("w:pPr/w:outlineLvl",ns)
                    if outline is not None:
                        level=outline.get(w+"val","")
                        if level.isdigit():block["heading_level"]=int(level)+1 if int(level)<9 else None
                    refs=references(item)
                    if refs: block["references"]=refs
                    append(block)
                elif item.tag==w+"tbl":
                    def wrapped_children(element,tag):
                        stack=list(reversed(list(element)))
                        while stack:
                            child=stack.pop()
                            if child.tag==tag:yield child
                            elif child.tag in {w+"sdt",w+"sdtContent",w+"customXml"}:
                                stack.extend(reversed(list(child)))
                    for row_number,row in enumerate(wrapped_children(item,w+"tr"),1):
                        cell_elements=list(wrapped_children(row,w+"tc"));cells=[words(cell) for cell in cell_elements]
                        spans=[]
                        for cell in cell_elements:
                            grid=cell.find("w:tcPr/w:gridSpan",ns);merge=cell.find("w:tcPr/w:vMerge",ns)
                            span=grid.get(w+"val", "1") if grid is not None else "1"
                            spans.append({"columns":int(span) if span.isdigit() and 0<int(span)<=16384 else 1,
                                "vertical_merge":merge.get(w+"val","continue") if merge is not None else None})
                        header=row.find("w:trPr/w:tblHeader",ns)
                        block={"kind":"table_row","text":"\t".join(cells),"cells":cells,
                            "locator":f"{position}/row:{row_number}","style":"",**context,
                            "table_locator":position,"row_number":row_number,"cell_spans":spans,
                            "cell_locators":[f"{position}/row:{row_number}/cell:{n}" for n in range(1,len(cells)+1)],
                            "is_header":header is not None and header.get(w+"val","1").lower() not in {"0","false","off"}}
                        refs=references(row)
                        if refs:block["references"]=refs
                        append(block)
                elif item.tag in {w+"sdt",w+"sdtContent",w+"customXml",alternate}:
                    if item.tag==alternate:
                        for choice in visible_children(item):walk(choice,position,context,depth+1)
                    else:walk(item,position,context,depth+1)
                elif item.tag==w+"altChunk": raise ValueError("DOCX外部/嵌入替代正文不能直接提取")
        walk(body,"docx:body",{"part":"word/document.xml","section":"body"})
        for name in sorted(names):
            match=re.fullmatch(r"word/(header|footer)[^/]*\.xml",name)
            if match:
                section=match.group(1)
                walk(xml(name),f"docx:{name}",{"part":name,"section":section})
        for section in ("footnote","endnote","comment"):
            name=f"word/{section}s.xml"
            if name not in names:continue
            note_ids=set()
            for note in xml(name).findall("w:"+section,ns):
                note_id=note.get(w+"id","")
                if section!="comment" and (note.get(w+"type") in {"separator","continuationSeparator","continuationNotice"}
                        or any(item.tag in {w+"separator",w+"continuationSeparator"} for item in note.iter())):continue
                if note_id in note_ids:raise ValueError("DOCX注释标识重复，无法准确定位原文")
                note_ids.add(note_id)
                context={"part":name,"section":section,"note_id":note_id}
                if section=="comment":context["editorial"]=True
                walk(note,f"docx:{name}/{section}:{note_id}",context)
        return blocks


def _pdf_reader(raw: bytes):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    if len(raw)>MAX_IMPORT_BYTES:raise ValueError("PDF文件超过大小限制")
    try: reader=PdfReader(io.BytesIO(raw))
    except PdfReadError as exc:raise ValueError("PDF格式损坏或无法读取") from exc
    if reader.is_encrypted:raise ValueError("PDF受密码保护，请提供可读取的授权副本")
    if len(reader.pages)>MAX_PDF_PAGES:raise ValueError(f"PDF 页数超过 {MAX_PDF_PAGES} 页限制")
    return reader


def _pdf_object(value):
    return value.get_object() if hasattr(value,"get_object") else value


def _pdf_stream_size(stream, limit: int) -> int:
    """Check content streams before the text/layout parsers see their bytes."""
    stream=_pdf_object(stream)
    if not hasattr(stream,"get_data"):return 0
    filters=_pdf_object(stream.get("/Filter",[]))
    if isinstance(filters,str):filters=[filters]
    # Common Flate content streams can be preflighted without allocating the
    # entire expanded stream (pypdf 5 also remains a supported dependency).
    if list(filters or []) in (["/FlateDecode"],["/Fl"]):
        compressed=getattr(stream,"_data",b"")
        try:
            expanded=zlib.decompressobj().decompress(compressed,limit+1)
        except zlib.error:
            # Let the PDF parser's recovery handle malformed compression.
            expanded=b""
        if len(expanded)>limit:raise ValueError("PDF页面内容流超过资源限制")
    data=stream.get_data()
    if len(data)>limit:raise ValueError("PDF页面内容流超过资源限制")
    return len(data)


def _pdf_page_resources(page) -> tuple[bool,int]:
    """Count text/Form streams and find images, including nested Form XObjects."""
    from pypdf.generic import ArrayObject
    total=0;has_images=False;seen=set();objects=0
    contents=_pdf_object(page.get("/Contents"))
    for stream in contents if isinstance(contents,(list,ArrayObject)) else [contents]:
        total+=_pdf_stream_size(stream,MAX_PDF_PAGE_STREAM_BYTES-total)
    stack=[(page.get("/Resources",{}),0,frozenset())]
    while stack:
        resources,depth,ancestors=stack.pop();resources=_pdf_object(resources)
        if not hasattr(resources,"get"):continue
        xobjects=_pdf_object(resources.get("/XObject",{}))
        if not hasattr(xobjects,"values"):continue
        for reference in xobjects.values():
            obj=_pdf_object(reference);identity=id(obj)
            if identity in ancestors:raise ValueError("PDF循环嵌套资源不允许")
            objects+=1
            if objects>5000 or depth>30:raise ValueError("PDF嵌套资源超过限制")
            if not hasattr(obj,"get"):continue
            if obj.get("/Subtype")=="/Image":has_images=True
            elif obj.get("/Subtype")=="/Form":
                if identity not in seen:total+=_pdf_stream_size(obj,MAX_PDF_PAGE_STREAM_BYTES-total)
                seen.add(identity)
                stack.append((obj.get("/Resources",{}),depth+1,ancestors|{identity}))
    return has_images,total


def extract_pdf_layout(raw: bytes, page_numbers: list[int] | None = None) -> dict[str,Any]:
    """On-demand, bounded local PDF layout/table extraction for authorized bytes.

    pdfplumber's geometric reading order is an alternate extraction, not an
    interchangeable offset or quote source for the primary pypdf page text.
    Page numbers are physical, one-based PDF page numbers; no printed-page
    arithmetic, OCR, external resource loading or image decompression is used.
    """
    reader=_pdf_reader(raw)
    count=len(reader.pages)
    if page_numbers is None:
        if count>MAX_PDF_LAYOUT_PAGES:
            raise ValueError(f"PDF布局查看须指定最多{MAX_PDF_LAYOUT_PAGES}个物理页")
        selected=list(range(1,count+1))
    else:
        if (not isinstance(page_numbers,list) or not 1<=len(page_numbers)<=MAX_PDF_LAYOUT_PAGES
                or any(type(n) is not int or not 1<=n<=count for n in page_numbers)
                or len(set(page_numbers))!=len(page_numbers)):
            raise ValueError(f"PDF布局页码必须是不重复的1起算物理页，单次最多{MAX_PDF_LAYOUT_PAGES}页")
        selected=page_numbers
    stream_size=0
    for number in selected:
        _,size=_pdf_page_resources(reader.pages[number-1]);stream_size+=size
        if stream_size>MAX_PDF_TOTAL_STREAM_BYTES:raise ValueError("PDF累计内容流超过资源限制")
    import pdfplumber
    pages=[];total_chars=0;total_cells=0
    with pdfplumber.open(io.BytesIO(raw),pages=selected) as pdf:
        for page in pdf.pages:
            number=page.page_number
            try:
                if any(not math.isfinite(float(value)) or not 0<float(value)<=20_000
                       for value in (page.width,page.height)):
                    raise ValueError("PDF布局页面尺寸超过资源限制")
                if len(page.chars)>200_000:raise ValueError("PDF页面布局字符超过资源限制")
                geometric_text=page.extract_text(layout=True) or ""
                text="\n".join(line.rstrip() for line in geometric_text.splitlines()).strip("\n")
                total_chars+=len(text)
                if total_chars>MAX_EXTRACTED_CHARS:raise ValueError("PDF布局文字超过资源限制")
                tables=[];issues=text_quality(text)
                # Dense drawings can generate quadratic table intersections.
                # Preserve the page text but require a smaller source for these.
                if len(page.edges)>3000:
                    issues.append("页面线段过多，未自动分析表格；请提供简化表格或人工核对")
                else:
                    for index,table in enumerate(page.find_tables(),1):
                        total_cells+=len(table.cells)
                        if total_cells>MAX_PDF_LAYOUT_CELLS:raise ValueError("PDF布局表格单元格超过资源限制")
                        rows=table.extract()
                        total_chars+=sum(len(cell or "") for row in rows for cell in row)
                        if total_chars>MAX_EXTRACTED_CHARS:raise ValueError("PDF布局文字超过资源限制")
                        tables.append({"table_index":index,"locator":f"pdf:{number}/table:{index}",
                            "bbox":[float(value) for value in table.bbox],"rows":rows,
                            "extraction_method":"pdfplumber_lines","alternate_extraction":True})
                pages.append({"pdf_page":number,"width":float(page.width),"height":float(page.height),
                    "text":text,"tables":tables,"issues":issues})
            finally:
                page.close()
    by_number={page["pdf_page"]:page for page in pages}
    return {"extraction_contract":PDF_LAYOUT_CONTRACT,"alternate_extraction":True,
        "citation_policy":"几何布局与表格属于替代提取；不可直接作为基础pypdf分块的原文引文或字符偏移，须回查对应物理页的基础原文。",
        "pages":[by_number[number] for number in selected]}


def checked_path(path: str | Path, allowed_root: str | Path | None = None,
                 max_bytes: int = MAX_IMPORT_BYTES) -> Path:
    path = Path(path).resolve(strict=True)
    if allowed_root is not None and not path.is_relative_to(Path(allowed_root).resolve()):
        raise ValueError("导入路径必须位于指定资料目录内")
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOCUMENTS:
        raise ValueError("仅支持TXT、Markdown、JSON、PDF、DOCX、Excel(XLSX/XLS/XLSM)和原生图像理解的PNG/JPEG/WebP")
    if path.stat().st_size > max_bytes:
        raise ValueError("导入文件超过大小限制")
    if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        from cockp.documents.spreadsheet_import import validate_spreadsheet
        with path.open('rb') as handle:raw=handle.read(max_bytes+1)
        validate_spreadsheet(raw,path.suffix.lower(),max_bytes=max_bytes)
    return path


def text_quality(text: str) -> list[str]:
    issues = []
    if len(text.strip()) < 30:
        issues.append("可提取文字过少：可能为空页、插图页或扫描件，需人工确认/OCR")
    if "\ufffd" in text or "(cid:" in text:
        issues.append("发现替换字符或未映射字形；原文需人工核对")
    if text.count("\x00"):
        issues.append("发现 NUL 字符，文本提取可能损坏")
    return issues


def load_document(path: str | Path, allowed_root: str | Path | None = None) -> dict[str, Any]:
    path = checked_path(path, allowed_root)
    raw = path.read_bytes()
    if len(raw)>MAX_IMPORT_BYTES:raise ValueError("导入文件超过大小限制")
    source = {"filename": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
              "format": path.suffix.lower()[1:], "bytes": len(raw)}
    pages = []
    blocks=[];spreadsheet=None
    if path.suffix.lower() in IMAGE_DOCUMENTS:
        text="";pages=[{"pdf_page":None,"text":"","issues":["图像附件需由模型原生多模态读取，并核对识别结果"]}]
    elif path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        from cockp.documents.spreadsheet_import import extract_spreadsheet
        from cockp.documents.coc7_template_adapter import extract_known_profile
        spreadsheet=extract_known_profile(raw,path.suffix.lower(),max_bytes=MAX_IMPORT_BYTES,max_chars=MAX_EXTRACTED_CHARS)
        if spreadsheet is None:
            spreadsheet=extract_spreadsheet(raw,path.suffix.lower(),max_bytes=MAX_IMPORT_BYTES,max_chars=MAX_EXTRACTED_CHARS)
        source["extraction_contract"]=spreadsheet["extraction_contract"]
        blocks=spreadsheet["blocks"];offset=0
        for index,block in enumerate(blocks):
            block.update(index=index,start_offset=offset,end_offset=offset+len(block["text"]))
            offset=block["end_offset"]+1
        text="\n".join(b["text"] for b in blocks)
        pages=[{"pdf_page":None,"text":text,"issues":text_quality(text)}]
    elif path.suffix.lower()==".docx":
        source["extraction_contract"]=DOCX_EXTRACTION_CONTRACT
        source["extraction_policy"]="正文顺序保留；页眉页脚、脚注尾注和编辑批注分区附后；修订删除文本不作当前正文；DOCX无可靠物理页码"
        blocks=extract_docx(raw);offset=0
        for index,block in enumerate(blocks):
            block.update(index=index,start_offset=offset,end_offset=offset+len(block["text"]))
            offset=block["end_offset"]+1
        text="\n".join(b["text"] for b in blocks)
        pages=[{"pdf_page":None,"text":text,"issues":text_quality(text)}]
    elif path.suffix.lower() == ".pdf":
        source["extraction_contract"]=PDF_EXTRACTION_CONTRACT
        source["extraction_policy"]="pypdf内容流阅读顺序；物理页码从1开始；布局和表格可按页另行读取，属于替代提取"
        reader = _pdf_reader(raw)
        total_chars = 0;total_stream_bytes=0
        for number, page in enumerate(reader.pages, 1):
            has_images,size=_pdf_page_resources(page);total_stream_bytes+=size
            if total_stream_bytes>MAX_PDF_TOTAL_STREAM_BYTES:raise ValueError("PDF累计内容流超过资源限制")
            text = page.extract_text() or ""
            total_chars += len(text)
            if total_chars > MAX_EXTRACTED_CHARS:
                raise ValueError("提取文本超过资源限制")
            pages.append({"pdf_page": number, "text": text, "issues": text_quality(text),"has_raster_images":has_images,
                "width":float(page.mediabox.width),"height":float(page.mediabox.height),
                "rotation":int(page.rotation),"layout_available":True,
                "layout_extraction_contract":PDF_LAYOUT_CONTRACT})
        text = "\n\n".join(p["text"] for p in pages)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本必须为 UTF-8；禁止猜测损坏的字符或数值") from exc
        pages = [{"pdf_page": None, "text": text, "issues": text_quality(text)}]
    if len(text)>MAX_EXTRACTED_CHARS: raise ValueError("提取文本超过资源限制")
    return {"source": source, "text": text, "pages": pages,"blocks":blocks,
            **({"spreadsheet":{k:v for k,v in spreadsheet.items() if k!="blocks"}} if spreadsheet else {}),
            "issues": [{"pdf_page": p["pdf_page"], "message": issue}
                       for p in pages for issue in p["issues"]]+(spreadsheet["issues"] if spreadsheet else []),
            "trusted_instructions": False}


def _semantic_chunks(text: str, limit: int = 1000):
    """Split at paragraph/sentence boundaries, preserving exact original offsets."""
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            candidates = [text.rfind(sep, start + limit // 2, end) for sep in ("\n\n", "。", "\n")]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def import_rulebook(path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Do not modify the source. Derived copyright material belongs in ignored storage."""
    document = load_document(path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    pages, chunks = [], []
    chapter = "前言/目录"
    for p in document["pages"]:
        raw = p["text"]
        lines = [s.strip() for s in raw.splitlines() if s.strip()]
        chapter_match = next((re.search(r"第[一二三四五六七八九十]+章\s+[^.。\n]{1,35}", s)
                              for s in lines[:3] if re.search(r"^第[一二三四五六七八九十]+章", s)), None)
        if chapter_match:
            chapter = chapter_match.group().strip()
        # Printed Chinese page label is in the header extraction on this edition.
        label = next((s for s in lines[:3] if re.fullmatch(r"\d{1,3}|[a-f]", s)), None)
        book_page = int(label) if label and label.isdigit() else label
        page = {**p, "book_page": book_page, "chapter": chapter,
                "source": document["source"]["filename"], "sha256": document["source"]["sha256"],
                "english_reference_pages": re.findall(r"(?:英文|原版|原书)[^\n]{0,12}?(\d{1,3})", raw)}
        pages.append(page)
        for start, end, content in _semantic_chunks(raw):
            if not content.strip():
                continue
            chunks.append({"id": f"pdf-{p['pdf_page']}-{start}", "text": content,
                           "pdf_page": p["pdf_page"], "book_page": book_page,
                           "chapter": chapter, "start_offset": start, "end_offset": end,
                           "source": page["source"], "sha256": page["sha256"],
                           "provenance": "source_fact"})
    report = {"source": document["source"], "pages": len(pages), "chunks": len(chunks),
              "extraction": "pypdf content-stream reading order; original offsets retained",
              "issues": document["issues"],
              "page_number_policy": "PDF 页从 1 开始；中文书内页取页眉/页脚提取标签，不能确定为 null；英文原书页仅作正文引用，绝不换算",
              "offsets_observed": sorted(set(p["pdf_page"] - p["book_page"] for p in pages
                                             if isinstance(p["book_page"], int))),
              "layout_caveats": ["双栏按内容流顺序提取；跨栏表格、图注、脚注不自动作为可执行规则",
                                  "文本索引保留重复页眉以保证原文定位；检索结果需结合上下文",
                                  "OCR 未自动运行：空白或扫描页进入人工审核，不能猜测缺失文字",
                                  "章节按页眉推断，章节封面/奇数页缺页眉时沿用上一页，需结合目录核对"],
              "visual_checks": []}
    for filename, payload in (("rulebook_pages.json", pages), ("rulebook_chunks.json", chunks),
                              ("rulebook_quality.json", report)):
        (output / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    db = sqlite3.connect(output / "rulebook_index.sqlite3")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("DELETE FROM chunks")
        db.executemany("INSERT INTO chunks VALUES (?,?)", [(c["id"], json.dumps(c, ensure_ascii=False)) for c in chunks])
        db.commit()
    finally:
        db.close()
    return report


def search_terms(text: str) -> Counter:
    lowered = text.casefold()
    terms = re.findall(r"[a-z0-9_]+", lowered)
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        terms.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return Counter(terms)


def local_search(records: list[dict], query: str, limit: int = 6) -> list[dict]:
    """Local BM25-style ranking supports Chinese without cloud embedding credentials."""
    q = search_terms(query)
    if not q:
        return []
    vectors = [search_terms(r.get("text", "")) for r in records]
    document_frequencies = Counter(term for vector in vectors for term in vector)
    avg = sum(sum(v.values()) for v in vectors) / max(len(vectors), 1) or 1
    ranked = []
    for record, vector in zip(records, vectors):
        score = 0.0
        size = sum(vector.values())
        for term in q:
            df = document_frequencies[term]
            tf = vector.get(term, 0)
            score += math.log(1 + (len(vectors) - df + .5) / (df + .5)) * (tf * 2.2) / (tf + 1.2 * (.25 + .75 * size / avg))
        if score:
            ranked.append((score, record))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [{**r, "retrieval_score": round(s, 4)} for s, r in ranked[:limit]]


class RulebookIndex:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def search(self, query: str, limit: int = 6, *, include_scenarios: bool = False) -> list[dict]:
        path = self.directory / "rulebook_chunks.json"
        if not path.exists():
            raise ValueError("规则书尚未导入，请由管理员执行本地规则导入")
        records=json.loads(path.read_text(encoding="utf-8"))
        if not include_scenarios:
            records=[r for r in records if not re.search(r"第十五章\s*模组",r.get("chapter",""))]
        return local_search(records, query, limit)
