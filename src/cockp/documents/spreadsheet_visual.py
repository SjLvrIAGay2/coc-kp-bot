"""Cached workbook-native layout references for character extraction.

Read the original workbook, not the flattened extraction. These are layout
previews, not Excel screenshots: conditional formatting, charts and formula
recalculation are deliberately not simulated. Source cells remain authoritative.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont
from openpyxl.utils import get_column_letter, coordinate_to_tuple

CONTRACT = 'character-workbook-layout-reference-v1'
INSTRUCTIONS = (
    '附图按原工作簿单元格、合并范围、行列尺寸和静态样式生成，供核对表头、列归属和阅读顺序；'
    '不是Excel截图，不执行公式，不保证条件格式、图表或嵌入对象显示。'
    '图中范围可含相邻表头或示例，仅当前source_units/chunk范围可作为本次事实来源。'
    '优先使用程序读取的单元格数值；图文冲突须记录具体单元格和歧义，不自行替换数字。'
    '公式无缓存显示[formula: no cache]，并不等于零。截图截断或未附图不表示原表无内容。'
)


@lru_cache(maxsize=32)
def _font(size, bold=False):
    candidates = [os.environ.get('COCKP_SPREADSHEET_FONT', ''),
                  'C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _color(value, default):
    if value is not None and value.type == 'rgb' and isinstance(value.rgb, str):
        return '#' + value.rgb[-6:]
    return default


def _lines(text, font, width):
    result = []
    for paragraph in str(text).splitlines() or ['']:
        line = ''
        for char in paragraph:
            if line and font.getlength(line + char) > width:
                result.append(line); line = ''
            line += char
        result.append(line)
    return result


class WorkbookVisualReference:
    """Lazy, per-job images, shared by role classification and field mapping."""

    def __init__(self, source_path, source_sha256, output):
        self.path = Path(source_path)
        self.sha = source_sha256
        self.output = Path(output) / 'workbook-visuals'
        self.book = self.cached = None
        self.error = None
        self.rendered = self.cache_hits = 0
        self.records = {}

    def _open(self):
        if self.book is not None:
            return
        raw = self.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.sha:
            raise ValueError('workbook_visual_source_changed')
        if self.path.suffix.lower() not in {'.xlsx', '.xlsm', '.xls'}:
            raise ValueError('workbook_visual_format_not_supported')
        import openpyxl
        if self.path.suffix.lower() == '.xls':
            import xlrd
            from openpyxl.styles import Font, PatternFill, Alignment
            legacy = xlrd.open_workbook(file_contents=raw, formatting_info=True)
            self.book = openpyxl.Workbook(); self.book.remove(self.book.active)
            try:
                for old in legacy.sheets():
                    sheet = self.book.create_sheet(old.name)
                    sheet.sheet_state = {0:'visible',1:'hidden',2:'veryHidden'}.get(old.visibility,'hidden')
                    for col, info in old.colinfo_map.items():
                        sheet.column_dimensions[get_column_letter(col+1)].width = info.width / 256
                    for row, info in old.rowinfo_map.items():
                        sheet.row_dimensions[row+1].height = info.height / 20
                    for row in range(old.nrows):
                        for col in range(old.row_len(row)):
                            original = old.cell(row,col)
                            if original.ctype in (xlrd.XL_CELL_EMPTY,xlrd.XL_CELL_BLANK):continue
                            value = original.value
                            if original.ctype == xlrd.XL_CELL_DATE:
                                value = xlrd.xldate_as_datetime(value,legacy.datemode)
                            elif original.ctype == xlrd.XL_CELL_BOOLEAN:value = bool(value)
                            elif original.ctype == xlrd.XL_CELL_ERROR:value = xlrd.error_text_from_code.get(value,'#ERROR')
                            cell = sheet.cell(row+1,col+1,value)
                            xf = legacy.xf_list[original.xf_index]; font = legacy.font_list[xf.font_index]
                            def rgb(index,default):
                                color=legacy.colour_map.get(index)
                                return ''.join(f'{v:02x}' for v in color) if color else default
                            cell.font = Font(size=font.height/20,bold=bool(font.bold),color=rgb(font.colour_index,'182434'))
                            cell.fill = PatternFill('solid',fgColor=rgb(xf.background.pattern_colour_index,'ffffff'))
                            cell.alignment = Alignment(horizontal={1:'left',2:'center',3:'right'}.get(xf.alignment.hor_align,'general'))
                    for rlo,rhi,clo,chi in old.merged_cells:
                        sheet.merge_cells(start_row=rlo+1,end_row=rhi,start_column=clo+1,end_column=chi)
            finally:legacy.release_resources()
            self.cached = self.book
            return
        self.book = openpyxl.load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
        self.cached = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, keep_links=False)

    def close(self):
        for book in (self.book, self.cached):
            if book is not None:
                book.close()

    def references(self, cells_by_sheet, *, max_images=4):
        """Return selected 24-row/12-column tiles with nearby headers intact.

        The fixed grid lets adjacent extraction batches reuse the same files.
        Explicit coverage metadata prevents a bounded preview implying full
        workbook coverage. No model call is made here.
        """
        requested = {}
        for sheet, cells in cells_by_sheet.items():
            for address in cells:
                row, col = coordinate_to_tuple(address)
                key = (sheet, (row - 1) // 24, (col - 1) // 12)
                requested[key] = requested.get(key, 0) + 1
        # Most relevant tiles first; stable order breaks ties.
        keys = sorted(requested, key=lambda key: (-requested[key], key))
        records, images = [], []
        if not self.error and keys:
            try:
                self._open()
                self.output.mkdir(parents=True, exist_ok=True)
                # A compact overview reconnects headers and columns across
                # tiles, while detail images keep individual numbers readable.
                for name in dict.fromkeys(key[0] for key in keys):
                    if len(records) >= min(2,max_images-1):break
                    if sum(key[0] == name for key in keys) > 1:
                        record,raw=self._overview(name)
                        records.append(record);images.append(raw)
                for sheet, ri, ci in keys[:max_images-len(records)]:
                    record, raw = self._tile(sheet, ri, ci)
                    records.append(record); images.append(raw)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = type(exc).__name__ + ':' + str(exc)[:180]
        metadata = {'contract': CONTRACT, 'source_sha256': self.sha,
                    'renderer': 'original-workbook-static-layout', 'instructions': INSTRUCTIONS,
                    'images': records, 'unattached_tiles': len(keys) - sum(r['kind']=='detail' for r in records),
                    'hidden_rows_and_columns': 'included_for_source_comparison',
                    'formula_policy': 'stored_values_only_no_recalculation'}
        if self.error:
            metadata['unavailable'] = self.error
        for record in records:self.records[record['file']]=record
        self.output.mkdir(parents=True,exist_ok=True)
        manifest={**metadata,'images':list(self.records.values()),
                  'rendered_images':self.rendered,'cache_hits':self.cache_hits}
        temporary=self.output/'manifest.tmp'
        temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
        temporary.replace(self.output/'manifest.json')
        return metadata, images

    def _overview(self,name):
        sheet=self.book[name]
        occupied=[(cell.row,cell.column) for cell in sheet._cells.values() if cell.value is not None]
        rows=max((r for r,c in occupied),default=1);cols=max((c for r,c in occupied),default=1)
        r2,c2=min(rows,256),min(cols,64)
        bounds=f'A1:{get_column_letter(c2)}{r2}'
        identity=hashlib.sha256(json.dumps([CONTRACT,self.sha,name,bounds,'overview'],ensure_ascii=False).encode()).hexdigest()
        path=self.output/(identity+'.png')
        if path.is_file():raw=path.read_bytes();self.cache_hits+=1
        else:
            raw=self._render(sheet,self.cached[name],1,1,r2,c2,bounds,overview=True)
            temporary=path.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(path)
            self.rendered+=1
        return {'sheet':name,'range':bounds,'sheet_state':sheet.sheet_state,'kind':'overview',
                'complete_used_range':rows<=r2 and cols<=c2,'file':path.name,
                'sha256':hashlib.sha256(raw).hexdigest()},raw

    def _tile(self, name, ri, ci):
        sheet, cache = self.book[name], self.cached[name]
        r1, c1 = ri * 24 + 1, ci * 12 + 1
        r2, c2 = min(r1 + 23, sheet.max_row), min(c1 + 11, sheet.max_column)
        bounds = f'{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}'
        identity = hashlib.sha256(json.dumps([CONTRACT, self.sha, name, bounds], ensure_ascii=False).encode()).hexdigest()
        path = self.output / (identity + '.png')
        record = {'sheet': name, 'range': bounds, 'sheet_state': sheet.sheet_state,
                  'kind': 'detail', 'file': path.name}
        if path.is_file():
            raw = path.read_bytes(); self.cache_hits += 1
        else:
            raw = self._render(sheet, cache, r1, c1, r2, c2, bounds)
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(raw); temporary.replace(path)
            self.rendered += 1
        record['sha256'] = hashlib.sha256(raw).hexdigest()
        return record, raw

    @staticmethod
    def _render(sheet, cache, r1, c1, r2, c2, bounds, *, overview=False):
        widths = []
        for col in range(c1, c2 + 1):
            dimension = next((d for d in sheet.column_dimensions.values()
                              if d.min is not None and d.min <= col <= (d.max or d.min)), None)
            width = dimension.width if dimension and dimension.width else sheet.sheet_format.defaultColWidth or 10
            widths.append(max(35, min(230, round(width * 9 + 8))))
        heights = [max(30, min(130, round((sheet.row_dimensions[row].height or
                     sheet.sheet_format.defaultRowHeight or 15) * 1.8))) for row in range(r1, r2 + 1)]
        scale=min(1,1500/sum(widths),2300/sum(heights)) if overview else 1
        widths=[max(2,round(width*scale)) for width in widths]
        heights=[max(2,round(height*scale)) for height in heights]
        xs, ys = [54], [66]
        for width in widths: xs.append(xs[-1] + width)
        for height in heights: ys.append(ys[-1] + height)
        picture = Image.new('RGB', (xs[-1] + 3, ys[-1] + 3), 'white')
        draw = ImageDraw.Draw(picture)
        draw.text((8, 6), sheet.title + '  ' + bounds, font=_font(17, True), fill='#172b4d')
        for i, col in enumerate(range(c1, c2 + 1)):
            draw.text((xs[i] + 4, 40), get_column_letter(col), font=_font(14), fill='#526580')
        for i, row in enumerate(range(r1, r2 + 1)):
            draw.text((3, ys[i] + 4), str(row), font=_font(14), fill='#526580')
        merges = [m for m in sheet.merged_cells.ranges if m.max_row >= r1 and m.min_row <= r2
                  and m.max_col >= c1 and m.min_col <= c2]
        painted = set()
        for row in range(r1, r2 + 1):
            for col in range(c1, c2 + 1):
                merge = next((m for m in merges if m.min_row <= row <= m.max_row and m.min_col <= col <= m.max_col), None)
                anchor = (merge.min_row, merge.min_col) if merge else (row, col)
                if anchor in painted: continue
                painted.add(anchor)
                ar, ac = anchor
                cell = sheet.cell(ar, ac)
                left, top = max(c1, ac), max(r1, ar)
                right, bottom = min(c2, merge.max_col if merge else col), min(r2, merge.max_row if merge else row)
                x, y = xs[left - c1], ys[top - r1]
                x2, y2 = xs[right - c1 + 1], ys[bottom - r1 + 1]
                fill = _color(cell.fill.fgColor, '#ffffff') if cell.fill.patternType == 'solid' else '#ffffff'
                draw.rectangle((x, y, x2, y2), fill=fill, outline='#cbd3dd')
                value = cache.cell(ar, ac).value if cell.data_type == 'f' else cell.value
                if value is None: value = '[formula: no cache]' if cell.data_type == 'f' else ''
                if isinstance(value, float) and value.is_integer(): value = int(value)
                font = _font(max(5,round(max(13, min(23, (cell.font.sz or 11) * 1.4))*scale)), bool(cell.font.bold))
                # Clip each cell independently: overflowing text cannot become
                # a neighbouring field's apparent value.
                patch = Image.new('RGB', (max(1, x2-x-2), max(1, y2-y-2)), fill)
                ink = ImageDraw.Draw(patch)
                lines = _lines(value, font, max(8, x2-x-10))
                line_height = font.size + 4
                capacity = max(1, (y2-y-6)//line_height)
                visible = lines[:capacity]
                if len(lines) > capacity: visible[-1] = visible[-1][:-1] + '…'
                for n, line in enumerate(visible):
                    alignment = cell.alignment.horizontal
                    tx = max(3, (x2-x-font.getlength(line))/2) if alignment == 'center' else 3
                    if alignment == 'right': tx = max(3, x2-x-font.getlength(line)-6)
                    ink.text((tx, 2+n*line_height), line, font=font, fill=_color(cell.font.color, '#182434'))
                picture.paste(patch, (x+1, y+1))
        stream = io.BytesIO(); picture.save(stream, format='PNG', optimize=True)
        return stream.getvalue()
