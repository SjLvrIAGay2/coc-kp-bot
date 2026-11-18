"""Strict known-family extraction and source-scoped, lossless input-cell edits.

No paths, arbitrary coordinates, Office engine, formula evaluator or model is
exposed by this API. Unknown layouts return None for the ordinary importer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import re
from functools import lru_cache
from pathlib import PurePosixPath

from defusedxml import ElementTree as SafeET

from cockp.documents.ooxml_cell_patch import TemplateError, package, patch_cells, patch_zip, mark_recalculation, require
from cockp.documents.coc7_template_profiles import PROFILES
from cockp.documents.coc7_native_family import (PROFILE as NATIVE_PROFILE, native_projection,
    native_readonly_registry, merge_anchor, skill_name)

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
CONTRACT = 'coc7-known-family-sparse-v1'
SOURCE_ROLE_CONTRACT = 'coc7-template-source-roles-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def xml(data, nodes=None):
    nodes = [0] if nodes is None else nodes
    try:
        iterator = SafeET.iterparse(io.BytesIO(data),events=('start','end'),forbid_dtd=True,forbid_entities=True,forbid_external=True)
        depth = 0
        for event,_ in iterator:
            if event == 'start':
                depth += 1; nodes[0] += 1
                require(depth <= 80 and nodes[0] <= 500_000, 'template_xml_resource_limit')
            else:depth -= 1
        root = iterator.root
    except TemplateError:
        raise
    except Exception:
        raise TemplateError('template_xml_invalid') from None
    return root


def _read(raw):
    with package(raw) as archive:
        nodes = [0]
        names = set(archive.namelist())
        needed = {'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', 'xl/styles.xml', '[Content_Types].xml'}
        require(needed <= names, 'template_workbook_parts_missing')
        workbook = xml(archive.read('xl/workbook.xml'),nodes)
        require(workbook.tag == NS+'workbook', 'template_namespace_unsupported')
        relations = {}
        for rel in xml(archive.read('xl/_rels/workbook.xml.rels'),nodes):
            require(rel.attrib.get('Id') not in relations, 'template_duplicate_relationship')
            relations[rel.attrib.get('Id')] = rel.attrib
        strings = []; rich_strings = set()
        if 'xl/sharedStrings.xml' in names:
            for index,item in enumerate(xml(archive.read('xl/sharedStrings.xml'),nodes).findall(NS+'si')):
                strings.append(''.join(e.text or '' for e in item.iter(NS+'t')))
                if any(e.tag in {NS+'r',NS+'rPr',NS+'rPh'} for e in item.iter()):rich_strings.add(index)
        style_root = xml(archive.read('xl/styles.xml'),nodes).find(NS+'cellXfs')
        styles = [] if style_root is None else list(style_root)
        sheets = {}; total = 0
        for item in workbook.findall(NS+'sheets/'+NS+'sheet'):
            require(len(sheets) < 64 and item.attrib.get('name') not in sheets, 'template_sheet_limit_or_duplicate')
            relation = relations.get(item.attrib.get(REL), {})
            target = relation.get('Target', '')
            require(relation.get('Type', '').endswith('/worksheet') and relation.get('TargetMode') != 'External'
                    and target and ':' not in target and '\\' not in target and '..' not in target.split('/') and '%' not in target,
                    'template_sheet_relation_invalid')
            part = target.lstrip('/') if target.startswith('/') else 'xl/'+target
            require(part.startswith('xl/worksheets/') and part in names, 'template_sheet_part_invalid')
            root = xml(archive.read(part),nodes); cells = {}
            for cell in root.findall(NS+'sheetData/'+NS+'row/'+NS+'c'):
                address = cell.attrib.get('r', '')
                require(re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', address) is not None and address not in cells,
                        'template_cell_address_invalid')
                total += 1; require(total <= 100_000, 'template_cell_limit')
                cells[address] = cell
            sheets[item.attrib['name']] = {'part': part, 'state': item.attrib.get('state', 'visible'), 'root': root,
                'cells': cells, 'merges': [e.attrib.get('ref') for e in root.findall(NS+'mergeCells/'+NS+'mergeCell')]}
        global_names = [e for e in workbook.findall(NS+'definedNames/'+NS+'definedName') if 'localSheetId' not in e.attrib]
        require(len({e.attrib['name'] for e in global_names}) == len(global_names), 'template_duplicate_global_name')
        defined = {e.attrib['name']: e.text for e in global_names}
        return {'sheets': sheets, 'strings': strings, 'rich_strings':rich_strings,'styles': styles, 'defined': defined, 'workbook': workbook}


def _value(cell, strings):
    if cell is None:
        return None
    tag = cell.attrib.get('t'); value = cell.find(NS+'v')
    if tag == 'inlineStr':
        return ''.join(e.text or '' for e in cell.iter(NS+'t'))
    if tag == 's':
        require(value is not None and value.text is not None, 'template_shared_string_invalid')
        text = value.text
        require(len(text) <= 10 and re.fullmatch(r'[0-9]+',text) is not None, 'template_shared_string_invalid')
        index = int(text)
        require(0 <= index < len(strings), 'template_shared_string_invalid')
        return strings[index]
    # OOXML explicitly represents a cached empty string as t=str + <v/>.
    # This is different from an absent cache; do not flood a known card with
    # false missing-cache warnings or manufacture a numeric zero.
    if tag == 'str' and value is not None and value.text is None:
        return ''
    if value is None or value.text is None:
        return None
    text = value.text
    if tag == 'b':
        require(text in {'0', '1'}, 'template_boolean_invalid'); return text == '1'
    if tag in {'str', 'e', 'd'}:
        return text
    try:
        # Preserve integer lexemes directly: a float intermediate rounds exact
        # OOXML/custom values above 2**53 before the source ledger sees them.
        if re.fullmatch(r'[+-]?[0-9]+', text) is not None:
            return int(text)
        number = float(text); require(math.isfinite(number), 'template_number_not_finite')
        return int(number) if number.is_integer() else number
    except ValueError:
        raise TemplateError('template_number_invalid') from None


def _formula(cell):
    formula = None if cell is None else cell.find(NS+'f')
    return None if formula is None else {'text': formula.text, 'attributes': formula.attrib}


def _profile(book):
    matches = []
    for profile in PROFILES:
        if not set(profile['required_sheets']) <= book['sheets'].keys():
            continue
        if any(digest(book['sheets'][sheet]['merges']) != checksum for sheet,checksum in profile['merge_hashes'].items()):
            continue
        if any(digest(_value(book['sheets'][sheet]['cells'].get(address),book['strings'])) != checksum
               for sheet,address,checksum in profile['labels']):
            continue
        if any(digest(_formula(book['sheets'][sheet]['cells'].get(address))) != checksum for sheet,address,checksum in profile['formulas']):
            continue
        if any(book['defined'].get(name) != value for name,value in profile['defined'].items()):
            continue
        matches.append(profile)
    require(len(matches) <= 1, 'template_profile_ambiguous')
    return matches[0] if matches else None


def _sheet_formula_text(book, name):
    """Expand shared references for comparison only; never evaluate a formula."""
    from cockp.documents.template_formula_sources import _expanded_formula
    result = {}
    for address, cell in book['sheets'][name]['cells'].items():
        formula = _formula(cell)
        if formula is not None:
            require(formula['attributes'].get('t') in {None, 'normal', 'shared'},
                    'template_formula_kind_unsupported')
            result[address] = _expanded_formula(book, name, address)
    return result


@lru_cache(maxsize=1)
def _stock_layout_identity():
    """Stable layout anchors of the bundled template, as distributed."""
    from cockp.documents.character_templates import CHARACTER_TEMPLATE_ID, load_character_template
    stock = load_character_template(CHARACTER_TEMPLATE_ID)
    book = _read(stock.data); profile = _profile(book)
    require(profile is not None and profile['id'] == 'coc7-v204-eight-sheet', 'template_stock_profile_invalid')
    order = list(book['sheets'])
    formula_text = {}
    for name in order:
        formula_text[name] = _sheet_formula_text(book, name)
    return {'sha256': stock.sha256, 'profile': profile, 'order': order,
            'states': {name: book['sheets'][name]['state'] for name in order},
            'merges': {name: tuple(sorted(book['sheets'][name]['merges'])) for name in order},
            'formula_text': formula_text, 'defined': dict(book['defined'])}


def _resaved_profile(book):
    """Recognise the bundled template after a benign re-save (openpyxl/Excel).

    Office re-serialisation may reorder merge ranges and expand shared
    formulas into per-cell formula text with normalised attributes. Neither
    changes the template's cells, labels, formulas or defined names, but the
    strict digests in PROFILES were taken from the distributed bytes, so a
    player-saved copy would otherwise be misread as an unknown workbook. This
    anchor compares semantic content only: sheet order and visibility, merge
    range sets, every formula (including shared children), fixed label values and
    defined-name targets. Any edit to those remains rejected, and no formula
    cache is created or guessed.
    """
    identity = _stock_layout_identity()
    if list(book['sheets']) != identity['order']:
        return None
    for name in identity['order']:
        spec = book['sheets'][name]
        if spec['state'] != identity['states'][name]:
            return None
        if tuple(sorted(spec['merges'])) != identity['merges'][name]:
            return None
        from cockp.documents.template_formula_sources import FormulaSourceError
        try:
            formulas = _sheet_formula_text(book, name)
        except (FormulaSourceError, TemplateError):
            return None
        if formulas != identity['formula_text'][name]:
            return None
    # Original v2.0.4 included extra named ranges for an external workbook.
    # Its known name table is also accepted when reading an older saved card.
    if book['defined'] != identity['defined'] and digest(book['defined']) != 'bac86f2115436f861d78d1bdf945a80b4198dff5fee2ed4df1864bd05958de42':
        return None
    profile = identity['profile']
    if any(digest(_value(book['sheets'][sheet]['cells'].get(address), book['strings'])) != checksum
           for sheet, address, checksum in profile['labels']):
        return None
    return profile


def profile_of(book):
    """Strict distributed-byte identity first, then the re-save-tolerant anchor."""
    return _profile(book) or _resaved_profile(book)


def _origin(book, sheet, address, source_sha):
    spec = book['sheets'][sheet]; cell = spec['cells'].get(address)
    value = _value(cell, book['strings']); formula = _formula(cell)
    return {'sheet': sheet, 'cell': address, 'part': spec['part'], 'source_sha256': source_sha,
        'cell_xml_sha256': hashlib.sha256(b'' if cell is None else SafeET.tostring(cell)).hexdigest(),
        'value': value, 'formula': formula, 'value_origin': 'stored_input' if formula is None else 'stored_formula_cache',
        'cached_available': formula is not None and value is not None, 'calculated': False}


def _native_origin_reader(book,profile,source_sha):
    """Keep original XML/cache metadata beside separately labelled calculations."""
    if profile['id']!=NATIVE_PROFILE:return lambda sheet,cell:_origin(book,sheet,cell,source_sha)
    from cockp.documents.template_formula_sources import TemplateFormulaResolver
    resolver=TemplateFormulaResolver(book,profile_id=profile['id'],read_value=_value,source_sha256=source_sha)
    cached={}
    def read(sheet,cell):
        key=(sheet,cell)
        if key not in cached:
            raw=_origin(book,sheet,cell,source_sha)
            element=book['sheets'][sheet]['cells'].get(cell)
            raw['raw_value']=raw['value']
            raw['cached_value_present']=element is not None and element.find(NS+'v') is not None
            raw.update(resolver.resolve(sheet,cell))
            raw['calculated']=raw.get('value_origin')=='program_formula'
            cached[key]=raw
        return cached[key]
    return read


@lru_cache(maxsize=1)
def _registered_stock_inputs():
    from cockp.documents.character_templates import load_character_template,CHARACTER_TEMPLATE_ID
    stock=load_character_template(CHARACTER_TEMPLATE_ID)
    book=_read(stock.data);profile=_profile(book)
    require(profile is not None and profile['id']=='coc7-v204-eight-sheet','template_stock_profile_invalid')
    return stock.sha256,{field:{'sheet':record['sheet'],'cell':record['cell'],
        'value':_value(book['sheets'][record['sheet']]['cells'].get(record['cell']),book['strings'])}
        for field,record in profile['fields'].items()}


def _source_roles(book,profile,source_sha,canonical,semantic):
    """Separate author examples/catalogues from player facts without word rules.

    A number coinciding with an example is not silently discarded: it is an
    explicit ambiguity, preserved for authenticated source-backed clarification.
    No text-language heuristic or caller-provided declaration assigns roles.
    """
    roles={};issues=[];excluded=set();whole_stock=False
    def mark(sheet,cell,role,reason,*,targets=()):
        item=_origin(book,sheet,cell,source_sha)
        roles[sheet,cell]={'sheet':sheet,'cell':cell,'role':role,'reason':reason,
                      'targets':list(targets),'origin':item};excluded.add((sheet,cell))
    if profile['id']=='coc7-v204-eight-sheet':
        stock_sha,stock=_registered_stock_inputs()
        def equal(field,record):
            cell=book['sheets'][record['sheet']]['cells'].get(record['cell'])
            value=_value(cell,book['strings']);old=stock[field]['value']
            return _formula(cell) is None and type(value) is type(old) and value==old
        whole_stock=(source_sha==stock_sha or all(equal(field,record) for field,record in profile['fields'].items()))
        if whole_stock:
            keys=set(semantic)
            keys.update((item['sheet'],item['cell']) for field in canonical for item in field.get('sources',[]))
            for sheet,cell in sorted(keys):mark(sheet,cell,'stock_unfilled','与登记原版模板的整组输入一致，尚无实际填卡依据')
            issues.append({'code':'template_stock_unfilled','severity':'clarification',
                'message':'这是与登记原版示例输入一致的未填写模板；示例初值未作为角色事实。请填写角色资料，或明确补充哪些原值确为本角色数据。',
                'targets':['/attributes/'+name for name in ('STR','CON','SIZ','DEX','APP','INT','POW','EDU')]})
        else:
            ambiguous=[]
            for field,record in profile['fields'].items():
                if field.startswith('attributes.') and equal(field,record):
                    target='/'+field.replace('.','/');ambiguous.append(target)
                    mark(record['sheet'],record['cell'],'ambiguous_stock_input',
                         '该输入仍与登记原版示例相同，无法仅凭数值判断是否由玩家填写',targets=[target])
            if ambiguous:
                issues.append({'code':'template_stock_attribute_ambiguous','severity':'clarification',
                    'message':'部分属性仍与登记原版示例相同，尚不能确定是否为本角色实填值；原值已保留，请明确补充相应属性依据。',
                    'targets':ambiguous})
        # This tab is a specialty definition/reference table, not a list of
        # skills possessed by an investigator. Changed definitions also remain
        # archived references, available to a selected main-card slot.
        if '技能专攻' in book['sheets']:
            for address,cell in book['sheets']['技能专攻']['cells'].items():
                value=_value(cell,book['strings'])
                if value is not None and _formula(cell) is None:
                    mark('技能专攻',address,'reference_catalog','模板专长定义表，不代表角色已选或已分配技能')
    elif profile['id']==NATIVE_PROFILE:
        # Structural label column, never the adjacent authored story fields.
        mark('人物卡','AR61','guidance','关键连接列标题，不是形象描述')
        story=_origin(book,'人物卡','W77',source_sha)
        if type(story['value']) is str and story['value'].strip():
            mark('人物卡','W77','ambiguous_story',
                 '额外故事区尚无登记的干净模板对照，不能确定是作者示例还是角色原文',targets=['/background/story'])
            issues.append({'code':'template_optional_story_ambiguous','severity':'review',
                'message':'额外故事区的原文已保留，但尚未确定是否为制作者示例；未自动当作角色背景。',
                'targets':['/background/story']})
    filtered=[entry for entry in canonical if not any((x['sheet'],x['cell']) in excluded
               for x in entry.get('sources',[]))]
    selected=[key for key in semantic if tuple(key) not in excluded]
    return filtered,selected,{'source_role_contract':SOURCE_ROLE_CONTRACT,'source_roles':list(roles.values()),
        'source_role_issues':issues,'stock_unfilled':whole_stock}


def _field_source_role_map(book,profile,source_sha):
    _,_,metadata=_source_roles(book,profile,source_sha,[],
        [(record['sheet'],record['cell']) for record in profile['fields'].values()])
    return {(row['sheet'],row['cell']):row for row in metadata['source_roles']}


def _with_source_role(origin,roles):
    role=roles.get((origin['sheet'],origin['cell']))
    return {**origin,'source_role':role['role'] if role else 'document_input',
        'source_role_reason':role['reason'] if role else '文件输入；不构成创建掷骰或审核证明',
        'source_role_targets':role.get('targets',[]) if role else []}


def _canonical(book, profile, source_sha):
    fields = []; skills = []; selected = {}; covered = set()
    def origin(sheet, address):
        return _origin(book, sheet, address, source_sha)
    def keep(item):
        key = (item['sheet'], item['cell']); selected[key] = item
    for field, record in profile['fields'].items():
        item = origin(record['sheet'], record['cell']); value = item['value']
        if not field.startswith('input.'):
            pointer = '/'+field.replace('.', '/')
            allowed = field in {'name','occupation','era'} and type(value) is str and bool(value.strip())
            allowed |= field in {'age','luck'} and type(value) is int
            allowed |= field.startswith('attributes.') and type(value) is int
            allowed |= field.startswith('background.') and type(value) is str and bool(value.strip())
            if allowed:
                fields.append({'pointer':pointer,'value':value,'operation':'copy','sources':[item]}); covered.add((item['sheet'],item['cell']))
        if value is not None and item['formula'] is None:
            keep(item)
    table = profile['skill_table']; sheet = table['sheet']
    for row in range(table['start'],table['stop']+1):
        for panel_index,(label,base,occupation,interest,total) in enumerate(table['panels']):
            parts = {key:origin(sheet,col+str(row)) for key,col in [('label_source',label),('base',base),('occupation',occupation),('interest',interest),('total',total)]}
            name = parts['label_source']['value']
            if type(name) is not str or not name.strip():
                # Unnamed occupied slots remain unresolved units; never discard them.
                for item in parts.values():
                    if item['value'] is not None:keep(item)
                continue
            skills.append({'name':name,**parts})
            escaped = name.replace('~','~0').replace('/','~1')
            if type(parts['total']['value']) is int:
                fields.append({'pointer':'/skills/'+escaped,'value':parts['total']['value'],'operation':'copy',
                    'sources':[parts['total']],'label_sources':[parts['label_source']]})
            components = {key:parts[key] for key in ('base','occupation','interest')}
            if 'growth_cols' in table:
                components['growth'] = origin(sheet,table['growth_cols'][panel_index]+str(row))
            for key,item in components.items():
                if key == 'growth':
                    if item['value'] is not None:keep(item)
                    continue
                if type(item['value']) is int:
                    fields.append({'pointer':'/allocations/'+escaped+'/'+key,'value':item['value'],'operation':'copy',
                        'sources':[item],'label_sources':[parts['label_source']]})
            for item in parts.values():
                if item['value'] is not None:keep(item)
    # Unknown sheets and every nonempty source entry are archived regardless.
    # The pipeline may remove *only* verified clean reference matches from the
    # model view, never hidden/custom content merely by its sheet name.
    for name,spec in book['sheets'].items():
        if name in {'建卡','人物卡','技能专攻'}:
            for address,cell in spec['cells'].items():
                style = int(cell.attrib.get('s','0'))
                require(0 <= style < len(book['styles']), 'template_cell_style_invalid')
                protection = book['styles'][style].find(NS+'protection')
                if (_formula(cell) is None and _value(cell,book['strings']) is not None
                        and protection is not None and protection.attrib.get('locked') == '0'):
                    keep(origin(name,address))
        # Auxiliary and previously unknown tabs remain fully archived/deferred;
        # they are available on demand, never automatically flooded into maps.
    return fields, skills, list(selected)


def _catalogs(book, profile):
    verified = []
    for catalog in profile.get('reference_catalogs',[]):
        spec = book['sheets'][catalog['sheet']]; semantic = []; structure = []
        for address,cell in spec['cells'].items():
            formula = _formula(cell)
            # Preserve lexical number representation used by the fixed catalog,
            # while resolving shared strings independently of their table index.
            stored = cell.find(NS+'v')
            value = None if stored is None else _value(cell,book['strings']) if cell.attrib.get('t') == 's' else stored.text
            if formula is None and value is None:continue
            semantic.append([address,formula if formula is not None else value]); structure.append([address,formula is not None])
        if digest(semantic) == catalog['semantic_sha256'] and digest(structure) == catalog['structure_sha256']:
            verified.append({**catalog,'part':spec['part']})
    return verified


def extract_known_profile(raw: bytes, suffix: str, *, max_bytes=20*1024*1024, max_chars=5_000_000):
    require(type(max_bytes) is int and max_bytes > 0 and type(max_chars) is int and max_chars > 0,
            'template_caller_limits_invalid')
    require(type(raw) is bytes and len(raw) <= min(max_bytes,20*1024*1024), 'template_caller_byte_limit')
    if suffix not in {'.xlsx', '.xlsm'}:
        return None
    try:
        book = _read(raw); profile = profile_of(book)
    except TemplateError:
        # Ordinary bounded importer remains responsible for validating unknown
        # documents; no alternate parser or lower validation limit is selected.
        return None
    if profile is None:
        return None
    source_sha = hashlib.sha256(raw).hexdigest(); blocks = []; issues = []; sheets = []
    # All nonempty source cells, hidden/custom sheets and formulas survive. No
    # helper tab is discarded just because of its name or visibility.
    for index,(name,spec) in enumerate(book['sheets'].items(),1):
        sheets.append({'name':name,'state':spec['state'],'index':index})
        base = {'sheet':name,'sheet_index':index,'sheet_state':spec['state'],'style':''}
        locator = 'excel:sheet:'+json.dumps(name,ensure_ascii=False)
        blocks.append({**base,'kind':'spreadsheet_sheet','text':'工作表 '+json.dumps(name,ensure_ascii=False)+'（'+spec['state']+'）',
            'locator':locator,'merged_ranges':spec['merges'],
            'merge_locations':[{'range':value,'locator':locator+':merge:'+value} for value in spec['merges']]})
        for address,cell in spec['cells'].items():
            value = _value(cell, book['strings']); formula = _formula(cell)
            if formula is None and value is None:
                continue
            row = int(re.search(r'\d+',address)[0]); col = 0
            for letter in re.match(r'[A-Z]+',address)[0]: col = col*26+ord(letter)-64
            text = address+' = '+json.dumps(value,ensure_ascii=False)
            extra = {}
            if formula is not None:
                extra = {'formula':formula['text'],'formula_source':formula,'cached_value':value,
                         'cached_available':value is not None,'cached_value_present':cell.find(NS+'v') is not None,'calculated':False}
                text = address+' = 公式 '+json.dumps(formula['text'],ensure_ascii=False)+('；已存缓存 '+json.dumps(value,ensure_ascii=False) if value is not None else '；无可用缓存，未计算')
                if value is None: issues.append({'sheet':name,'cell':address,'message':'公式无缓存，待Excel重算；不能猜值'})
            blocks.append({**base,'kind':'spreadsheet_cell','row':row,'col':col,'cell':address,'value':value,
                'value_type':type(value).__name__,'xml_type':cell.attrib.get('t'),
                'text':text,'locator':locator+':cell:'+address,**extra})
    fields = {field:_origin(book,record['sheet'],record['cell'],source_sha) for field,record in profile['fields'].items()}
    catalogs=_catalogs(book,profile)
    native=native_projection(book,profile,_native_origin_reader(book,profile,source_sha),catalogs=catalogs)
    if native is None:
        canonical_fields, canonical_skills, semantic_cells = _canonical(book,profile,source_sha)
    else:
        canonical_fields=native['canonical_fields'];canonical_skills=native['canonical_skills'];semantic_cells=native['semantic_cells']
    canonical_fields,semantic_cells,role_metadata=_source_roles(book,profile,source_sha,canonical_fields,semantic_cells)
    if native is not None:
        native.update(role_metadata)
        native['native_issues'] += role_metadata['source_role_issues']
    require(sum(len(block['text'])+1 for block in blocks) <= min(max_chars,5_000_000), 'template_caller_text_limit')
    selected = set(semantic_cells)
    deferred_cells = [(block['sheet'],block['cell']) for block in blocks if block['kind']=='spreadsheet_cell'
                      and (block['sheet'],block['cell']) not in selected]
    return {'blocks':blocks,'issues':issues,'sheets':sheets,'formula_policy':'stored_only_never_calculated',
        'trusted_instructions':False,'extraction_contract':CONTRACT+':'+profile['id']+(':'+native['native_contract'] if native else '')+':'+SOURCE_ROLE_CONTRACT,
        'projection_profile':profile['id'],'profile_fields':fields,'source_sha256':source_sha,
        'canonical_fields':canonical_fields,'canonical_skills':canonical_skills,'semantic_cells':semantic_cells,
        'deferred_cells':deferred_cells,'verified_lookup_catalogs':catalogs,
        'deferred_policy':'Complete source retained; unresolved non-primary data remains available for explicit on-demand review, not silently mapped or discarded.',
        'canonical_contract':'copy_stored_values_only_no_creation_proof_no_install_v1',
        **({key:value for key,value in native.items() if key not in {'canonical_fields','canonical_skills','semantic_cells'}} if native else role_metadata)}


@dataclass(frozen=True)
class SourceScope:
    actor_id: str
    group_id: str
    campaign_id: str
    job_id: str
    sha256: str


def _authorize(raw, source, requester):
    require(isinstance(source,SourceScope) and isinstance(requester,SourceScope) and source == requester,
            'template_owner_group_campaign_job_scope_mismatch')
    require(all(isinstance(v,str) and v for v in source.__dict__.values())
            and re.fullmatch('[a-f0-9]{64}',source.sha256) is not None
            and hashlib.sha256(raw).hexdigest() == source.sha256, 'template_source_sha_CAS_failed')


def editable_fields(raw: bytes, source: SourceScope, requester: SourceScope):
    _authorize(raw,source,requester); book = _read(raw); profile = profile_of(book)
    require(profile is not None, 'template_layout_unknown')
    roles=_field_source_role_map(book,profile,source.sha256)
    return [{'field':field,'label':_friendly(book,profile,field,record),'type':record['type'],'min':record.get('min'),'max':record.get('max'),
             'nullable':record.get('nullable',True),'category':record.get('category',''),'aliases':_aliases(book,profile,field,record),
             'editable':True,
             'current':_with_source_role(_origin(book,record['sheet'],record['cell'],source.sha256),roles)}
            for field,record in profile['fields'].items() if record.get('editable') and record['sheet'] in book['sheets']
            and record['cell'] in book['sheets'][record['sheet']]['cells']
            and merge_anchor(book,record['sheet'],record['cell'])==record['cell']
            and _formula(book['sheets'][record['sheet']]['cells'][record['cell']]) is None
            and _unlocked(book,book['sheets'][record['sheet']]['cells'][record['cell']])]


def template_fields(raw: bytes,source: SourceScope,requester: SourceScope,*,include_readonly=False):
    """One scoped directory for editable inputs and explicit read-only views."""
    require(type(include_readonly) is bool,'template_readonly_flag_invalid')
    result=editable_fields(raw,source,requester)
    if not include_readonly:return result
    book=_read(raw);profile=profile_of(book)
    require(profile is not None,'template_layout_unknown')
    read=_native_origin_reader(book,profile,source.sha256)
    roles=_field_source_role_map(book,profile,source.sha256)
    for field,record in native_readonly_registry(profile['id']).items():
        result.append({'field':'readonly.'+field,'label':record['label'],'category':record['category'],
            'type':'readonly','nullable':True,'aliases':[],'editable':False,'min':None,'max':None,
            'current':_with_source_role(read(record['sheet'],record['cell']),roles)})
    return result


def _skill_directory_name(book,profile,field):
    match=re.fullmatch(r'skills\.(left|right)\.([0-9]+)\.(name|specialty|growth|occupation|interest|check)',field)
    if profile['id']!=NATIVE_PROFILE or match is None:return None
    side,row,part=match.groups();label_col,special_col=('F','H') if side=='left' else ('AB','AD')
    cells=book['sheets']['人物卡']['cells']
    label=_value(cells.get(label_col+row),book['strings'])
    address=special_col+row
    specialty=_value(cells.get(address),book['strings']) if merge_anchor(book,'人物卡',address)==address else None
    name=skill_name(label,specialty)
    display=name or (label.strip()+'（未命名专长）' if type(label) is str and label.strip() else '未命名技能槽')
    role={'name':'名称','specialty':'专长','growth':'成长点','occupation':'职业加点','interest':'兴趣加点','check':'成功标记'}[part]
    aliases=[x for x in (name,label,specialty) if type(x) is str and x.strip()]
    return display+'·'+role,list(dict.fromkeys(aliases))


def _aliases(book,profile,field,record):
    skill=_skill_directory_name(book,profile,field)
    return list(dict.fromkeys(list(record.get('aliases',[]))+(skill[1] if skill else [])))


def _unlocked(book,cell):
    style=int(cell.attrib.get('s','0'))
    require(0<=style<len(book['styles']),'template_cell_style_invalid')
    protection=book['styles'][style].find(NS+'protection')
    return protection is not None and protection.attrib.get('locked')=='0'


def _friendly(book, profile, field, record):
    skill=_skill_directory_name(book,profile,field)
    if skill:return skill[0]
    if record.get('label'):return record['label']
    names = {'name':'姓名','player':'玩家','occupation':'职业','age':'年龄','gender':'性别','residence':'住地',
        'birthplace':'出身','luck':'幸运','era':'时代背景','STR':'力量','CON':'体质','SIZ':'体型','DEX':'敏捷',
        'APP':'外貌','INT':'智力','POW':'意志','EDU':'教育','description':'形象描述','beliefs':'思想与信念',
        'important_people':'重要之人','significant_places':'意义非凡之地','treasured_possessions':'宝贵之物',
        'traits':'特质','injuries':'伤口和疤痕','phobias':'恐惧症和狂躁症'}
    if not field.startswith('input.'):
        return names.get(field.rsplit('.',1)[-1],field)
    table = profile['skill_table']; address = record['cell']; row = int(re.search(r'\d+',address)[0]); col = re.match('[A-Z]+',address)[0]
    if record['sheet'] == table['sheet'] and table['start'] <= row <= table['stop']:
        for label,base,occupation,interest,total in table['panels']:
            if col in {label,base,occupation,interest,total}:
                name = _value(book['sheets'][record['sheet']]['cells'].get(label+str(row)),book['strings'])
                role = {label:'名称',base:'基础值',occupation:'职业加点',interest:'兴趣加点',total:'总值'}[col]
                return (str(name) if name not in (None,'') else '未命名技能')+'·'+role
    if record['sheet'] == '技能专攻':
        label = {'C':'B','F':'E','I':'H','L':'K','O':'N'}.get(col,col)
        name = _value(book['sheets']['技能专攻']['cells'].get(label+str(row)),book['strings'])
        return '技能专攻·'+(str(name) if name not in (None,'') else '未命名专长')+('·基础值' if label != col else '·名称')
    return record['sheet']+'·可填写项目'+field.rsplit('.',1)[-1]


def edit_template(raw: bytes, source: SourceScope, requester: SourceScope, edits: list[dict]):
    """Return a new byte artifact. The caller owns atomic, exclusive saving/sending."""
    _authorize(raw,source,requester); book = _read(raw); profile = profile_of(book)
    require(profile is not None, 'template_layout_unknown')
    require(type(edits) is list and 1 <= len(edits) <= 32, 'template_edit_count_invalid')
    updates = {}; seen = set()
    for edit in edits:
        require(type(edit) is dict and set(edit) <= {'field','value','expected_value','expected_cell_sha256'}
                and {'field','value'} <= set(edit) and type(edit['field']) is str and edit['field'] not in seen,
                'template_typed_edit_invalid')
        seen.add(edit['field']); record = profile['fields'].get(edit['field'])
        require(record is not None and record.get('editable') is True, 'template_field_not_editable')
        spec = book['sheets'][record['sheet']]; cell = spec['cells'].get(record['cell'])
        require(merge_anchor(book,record['sheet'],record['cell']) == record['cell'], 'template_merged_child_not_editable')
        require(cell is not None and _formula(cell) is None, 'template_formula_or_missing_cell_not_editable')
        if cell.attrib.get('t') == 's':
            stored = cell.find(NS+'v')
            require(stored is not None and stored.text is not None and re.fullmatch(r'[0-9]+',stored.text) is not None,
                    'template_shared_string_invalid')
            require(int(stored.text) not in book['rich_strings'], 'template_rich_text_edit_unsupported')
        require(not any(e.tag in {NS+'r',NS+'rPr',NS+'rPh'} for e in cell.iter()), 'template_rich_text_edit_unsupported')
        style = int(cell.attrib.get('s','0'))
        require(0 <= style < len(book['styles']), 'template_cell_style_invalid')
        protection = book['styles'][style].find(NS+'protection')
        require(protection is not None and protection.attrib.get('locked') == '0', 'template_input_cell_not_unlocked')
        origin = _origin(book,record['sheet'],record['cell'],source.sha256)
        require('expected_value' in edit or 'expected_cell_sha256' in edit, 'template_cell_CAS_required')
        if 'expected_value' in edit:
            require(type(edit['expected_value']) is type(origin['value']) and edit['expected_value'] == origin['value'], 'template_old_value_CAS_failed')
        if 'expected_cell_sha256' in edit:
            require(edit['expected_cell_sha256'] == origin['cell_xml_sha256'], 'template_cell_sha_CAS_failed')
        value = edit['value']; kind = record['type']
        require((value is None and record.get('nullable',True)) or (kind == 'text' and type(value) is str and len(value) <= record.get('max',2000))
                or (kind == 'integer' and type(value) is int and record['min'] <= value <= record['max'])
                or (kind == 'number' and type(value) in (int,float) and math.isfinite(value) and record['min'] <= value <= record['max'])
                or (kind == 'boolean' and type(value) is bool), 'template_typed_value_out_of_range')
        updates.setdefault(spec['part'],{})[record['cell']] = value
    changes = {}; cell_changes = {}
    with package(raw) as archive:
        shared_references = 0
        for spec in book['sheets'].values():
            old = archive.read(spec['part'])
            new, details = patch_cells(old,updates.get(spec['part'],{}),clear_formula_caches=True)
            if new != old: changes[spec['part']] = new; cell_changes[spec['part']] = details
            shared_references += sum(cell.attrib.get('t') == 's' for cell in xml(new).iter(NS+'c'))
        old = archive.read('xl/workbook.xml'); changes['xl/workbook.xml'] = mark_recalculation(old)
        if 'xl/sharedStrings.xml' in archive.namelist():
            old = archive.read('xl/sharedStrings.xml')
            # Keep every si payload/index, including now-unused entries. Only
            # update the count of remaining shared-string references.
            root_match = re.search(rb'<(?:[A-Za-z_][\w.-]*:)?sst\b[^>]*>',old)
            require(root_match is not None, 'template_shared_strings_root_missing')
            header = root_match.group()
            for key,number in (('count',shared_references),('uniqueCount',len(xml(old).findall(NS+'si')))):
                pattern = rb'(\s+'+key.encode()+rb'\s*=\s*)([\'"])[^\'"]*\2'
                found = re.search(pattern,header)
                if found is not None:
                    replacement = found[1]+found[2]+str(number).encode()+found[2]
                    header = header[:found.start()]+replacement+header[found.end():]
            updated = old[:root_match.start()]+header+old[root_match.end():]
            if updated != old:changes['xl/sharedStrings.xml'] = updated
    result = patch_zip(raw,changes)
    reread = _read(result); require(profile_of(reread) is not None, 'template_profile_lost_after_patch')
    for sheet in reread['sheets'].values():
        require(all(_formula(cell) is None or cell.find(NS+'v') is None for cell in sheet['cells'].values()), 'template_stale_formula_cache_remained')
    return {'data':result,'original_sha256':source.sha256,'sha256':hashlib.sha256(result).hexdigest(),
        'profile':profile['id'],'changed_parts':sorted(changes),'changed_cells':cell_changes,
        'recalculation_required':True,'recalculated':False,'formula_cache_policy':'all_formulas_invalidated_conservatively',
        'original_unchanged':hashlib.sha256(raw).hexdigest() == source.sha256,'source_scope':source}
