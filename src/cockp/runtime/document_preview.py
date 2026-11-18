"""Pure display of recognized document data; never changes recognition/audit."""

import re


_LABELS = {
    'id': '角色编号', 'name': '姓名', 'owner_id': '归属账号', 'version': '版本',
    'age': '年龄', 'occupation': '职业', 'era': '时代', 'attributes': '属性', 'skills': '技能',
    'occupation_skills': '职业技能', 'allocations': '技能点分配', 'derived': '派生数值',
    'luck': '幸运', 'cash': '现金', 'assets': '资产', 'equipment': '装备', 'background': '背景',
    'creation': '创建记录', 'age_evidence': '年龄调整依据', 'sources': '来源依据',
    'review_history': '审核记录', 'locked': '锁定状态', 'recognized_confirmed': '识别核对',
    'method': '创建方式', 'source': '来源', 'raw_attributes': '创建前属性',
    'attribute_rolls': '属性掷骰记录', 'luck_rolls': '初始幸运骰记录',
    'education_rolls': '教育增强记录', 'deductions': '年龄属性扣减',
    'occupation_formula': '职业点计算方式', 'base': '基础值', 'interest': '兴趣点',
    'occupation_points': '职业点', 'interest_points': '兴趣点',
    'occupation_budget': '职业点预算', 'interest_budget': '兴趣点预算',
    'quantity': '数量', 'kind': '类型', 'item_id': '物品编号', 'catalog_id': '目录编号',
    'current': '当前值', 'max': '上限', 'start': '初始值', 'value': '数值', 'note': '说明',
    'hp': '生命', 'mp': '魔法', 'san': '理智', 'sanity': '理智',
    'damage_bonus': '伤害加值', 'db': '伤害加值', 'build': '体格',
    'mov': '移动力', 'move': '移动力', 'movement': '移动力',
    'STR': '力量', 'CON': '体质', 'SIZ': '体型', 'DEX': '敏捷',
    'APP': '外貌', 'INT': '智力', 'POW': '意志', 'EDU': '教育',
    'gender': '性别', 'player': '玩家', 'residence': '住地', 'birthplace': '故乡',
    'description': '形象描述', 'beliefs': '思想与信念', 'important_people': '重要之人',
    'significant_places': '意义非凡之地', 'treasured_possessions': '宝贵之物',
    'traits': '特质', 'injuries': '伤口和疤痕', 'phobias': '恐惧症和躁狂症', 'story': '背景故事',
    'growth': '成长点', 'total': '总值', 'specialty': '专长', 'success_mark': '成功标记',
    'occupation_mark': '职业标记', 'status': '状态', 'location': '部位',
    'type': '类型', 'skill': '使用技能', 'regular': '普通成功率', 'hard': '困难成功率',
    'extreme': '极难成功率', 'damage': '伤害', 'range': '射程', 'impale': '贯穿',
    'attacks': '攻击次数', 'capacity': '装弹量', 'malfunction': '故障值',
    'weapons': '武器', 'native_sections': '模板栏目', 'unmapped': '未映射字段',
    'label': '技能名称', 'specialty_source': '专长', 'disposition': '处置', 'reason': '说明',
}
_DYNAMIC_FIELDS = {'skills', 'allocations', 'attributes', 'raw_attributes', 'deductions', 'background', 'sources'}
_ATTRIBUTES = ('STR', 'CON', 'SIZ', 'DEX', 'APP', 'INT', 'POW', 'EDU')
_DERIVED = (('hp',), ('mp',), ('san', 'sanity'), ('damage_bonus', 'db'), ('build',), ('mov', 'move', 'movement'))
_INTERNAL = {'id', 'owner_id', 'actor_id', 'campaign_id', 'group_id', 'version', 'sources',
             'provenance', 'review_history', 'locked', 'recognized_confirmed', 'prepared',
             'import_issues', 'source_excerpts', 'audit', 'audit_report', 'computation',
             'dependencies', 'source_sha256', 'sha256', 'cell_xml', 'part',
             'indexes', 'rejected_mappings', 'diagnostic_history', 'model_incomplete'}
_NON_FACT_ROLES = {'guidance', 'example', 'template_example', 'stock_unfilled',
                   'ambiguous_stock_input', 'ambiguous_story'}


def _label(key, *, allocation=False):
    if allocation and key == 'occupation':
        return '职业点'
    return _LABELS.get(str(key), str(key))


def _field_label(path):
    parts = str(path).split('.')
    return ' · '.join(_label(part, allocation=parts[0] == 'allocations' and index > 1)
                      for index, part in enumerate(parts))


def _exact_field_text(value):
    """Translate whole schema paths; never substitute words inside prose."""
    value = str(value)
    parts = value.split('.')
    if parts[0] not in _LABELS:
        return value
    for index, part in enumerate(parts):
        if not re.fullmatch(r'[\w()（）/\-]+', part):
            return value
        if part not in _LABELS and not part.isdigit() and (index == 0 or parts[index - 1] not in _DYNAMIC_FIELDS):
            return value
    return _field_label(value)


def markdown_literal(value):
    """Preserve literal card/file text inside program-generated Markdown."""
    text=re.sub(r'([\\`*_{}\[\]()<>#+!|~])',r'\\\1',str(value))
    return re.sub(r'(?m)^(\s*)-(?=\s)',r'\1\\-',text)


def markdown_summary(text):
    """A short heading over already-authorized plain program data."""
    lines=str(text).splitlines()
    if not lines:return ''
    title=lines[0].rstrip('：:')
    return '**'+markdown_literal(title)+'**\n\n'+'\n'.join(markdown_literal(line) for line in lines[1:])


def issue_text(issues, *, markdown=False):
    """Render every issue; transport owns delivery splitting, never this view."""
    lines = []
    # Keep the complete issue records elsewhere. Display actual missing/error
    # items before review-only annotations such as deferred reference material.
    ordered=sorted(enumerate(issues),key=lambda pair:(
        pair[1].get('severity') in {'review','warning'} if isinstance(pair[1],dict) else False,pair[0]))
    for _,issue in ordered:
        if not isinstance(issue, dict):
            lines.append(_exact_field_text(issue))
            continue
        field = str(issue.get('field') or '')
        reason = issue.get('reason') or issue.get('message') or '请核对该项资料'
        suggestion = issue.get('suggestion')
        lines.append((_field_label(field) + '：' if field else '') + _exact_field_text(reason)
                     + ('；修改方法：' + str(suggestion) if suggestion else ''))
    text='\n'.join(lines)
    return markdown_literal(text) if markdown else text


def _value(value, *, allocation=False):
    if value is None:
        return '未识别'
    if value == '':
        return '（空字符串）'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, dict):
        return ('；'.join(_label(key, allocation=allocation) + ' ' + _value(item, allocation=allocation)
                         for key, item in value.items() if not _internal(key)) if value else '（空对象）')
    if isinstance(value, (list, tuple)):
        return '、'.join(_value(item, allocation=allocation) for item in value) if value else '（空列表）'
    return str(value)


def _attributes(values):
    if not isinstance(values, dict):
        return _value(values)
    items = [(_LABELS[key] if key in _ATTRIBUTES else str(key)) + ' ' + _value(value) for key, value in values.items()]
    for key in _ATTRIBUTES:
        if key not in values and _LABELS[key] not in values:
            items.append(_LABELS[key] + ' 未识别')
    return '；'.join(items)


def _skills(values):
    if not isinstance(values, dict):
        return _value(values)
    return '；'.join(str(key) + ' ' + _value(value) for key, value in values.items()) if values else '未识别'


def _allocations(values):
    if not isinstance(values, dict):
        return _value(values)
    return ('；'.join(str(key) + ' ' + _value(value, allocation=True) for key, value in values.items())
            if values else '未识别')


def _resource(value):
    if not isinstance(value, dict):
        return _value(value)
    fields = [_label(key) + ' ' + _value(value[key]) for key in ('current', 'max', 'start') if key in value]
    fields += [str(key) + ' ' + _value(item) for key, item in value.items()
               if key not in {'current', 'max', 'start'} and not _internal(key)]
    return '，'.join(fields) if fields else '未识别'


def _internal(key):
    return str(key) in _INTERNAL or str(key).startswith('$') or str(key).endswith('_metadata')


def _section(title, rows, *, markdown, headings=('项目', '内容')):
    if not rows:
        rows = [('内容', '未识别')]
    if markdown:
        return '**'+markdown_literal(title)+'**\n\n'+'\n'.join(
            '- '+markdown_literal(row[0])+'：'+markdown_literal(row[1]).replace('\n','\n  ') for row in rows)
    if title=='技能点分配':
        return title+'：'+'；'.join(str(row[0])+' '+str(row[1]) for row in rows)
    if title in {'属性','技能'}:
        if rows==[('技能','未识别')]:return '技能：未识别'
        return title+'：\n'+'\n'.join(str(row[0])+' '+str(row[1]) for row in rows)
    return title+'：\n'+'\n'.join(str(row[0])+'：'+str(row[1]) for row in rows)


def _flat_rows(value, prefix='', *, allocation=False):
    """Flatten actual user data without printing raw provenance dictionaries."""
    if isinstance(value, dict) and value:
        rows=[]
        for key,item in value.items():
            if _internal(key):continue
            label=_label(key,allocation=allocation)
            rows.extend(_flat_rows(item,prefix+' · '+label if prefix else label,allocation=allocation))
        return rows
    if isinstance(value,(list,tuple)) and value:
        return [row for i,item in enumerate(value,1)
                for row in _flat_rows(item,(prefix+' · ' if prefix else '')+str(i),allocation=allocation)]
    return [(prefix or '内容',_value(value,allocation=allocation))]


def _source_rows(value, prefix='', *, seen=None, review_rows=None):
    """Only source values become prose; paths, hashes and proofs never do."""
    if seen is None:seen=set()
    def walk(item,label):return _source_rows(item,label,seen=seen,review_rows=review_rows)
    if isinstance(value,dict):
        if 'value' in value and ('cell' in value or 'source_role' in value or 'value_origin' in value):
            key=(value.get('sheet'),value.get('cell'),repr(value['value']),value.get('source_role'))
            if key in seen:return []
            seen.add(key)
            label=str(value.get('label') or value.get('pointer') or prefix or '原字段')
            if value.get('sheet') and value.get('cell'):label+='（'+str(value['sheet'])+'!'+str(value['cell'])+'）'
            if value.get('source_role') in _NON_FACT_ROLES:
                if review_rows is not None:review_rows.append((label,'模板示例／待确认'))
                return []
            if value['value'] is None or isinstance(value['value'],str) and not value['value'].strip():return []
            return _flat_rows(value['value'],label)
        if 'pointer' in value and 'value' in value:
            rows=_flat_rows(value['value'],str(value['pointer']))
            if value.get('reason'):rows.append((str(value['pointer'])+' · 说明',str(value['reason'])))
            if value.get('disposition'):rows.append((str(value['pointer'])+' · 处置',str(value['disposition'])))
            return rows
        if 'source' in value and isinstance(value['source'],dict):
            return walk(value['source'],value.get('label') or prefix or value.get('field','原字段'))
        if 'fields' in value and isinstance(value['fields'],dict):
            return [row for key,item in value['fields'].items()
                    for row in walk(item,(prefix+' · ' if prefix else '')+_label(key))]
        if 'sources' in value and isinstance(value['sources'],dict):
            slot=re.fullmatch(r'(left|right)\.(\d+)',str(value.get('id','')))
            if slot:prefix=('左栏' if slot[1]=='left' else '右栏')+'第'+str(int(slot[2])-15)+'个技能槽'
            rows=[]
            for key,item in value['sources'].items():
                rows.extend(walk(item,(prefix+' · ' if prefix else '')+_label(key,allocation=True)))
            if value.get('specialty_source') is not None:
                specialty=walk(value['specialty_source'],(prefix+' · ' if prefix else '')+'专长')
                rows.extend(specialty or [(prefix+' · 专长','未填写')])
            return rows
        return [row for key,item in value.items() if not _internal(key)
                for row in walk(item,(prefix+' · ' if prefix else '')+_label(key))]
    if isinstance(value,(list,tuple)):
        return [row for i,item in enumerate(value,1)
                for row in walk(item,(prefix+' · ' if prefix else '')+str(i))]
    return [(prefix or '内容',_value(value))]


def character_preview(draft, *, compact=False, markdown=False, native_sections=None,
                      skill_slots=None, sources=None, unmapped=None, issues=None):
    """Render a complete, fixed-order view. ``compact`` is compatibility only.

    Additional source material must already be authorized by the caller. This
    pure renderer neither reads artifacts nor promotes examples to card facts.
    """
    draft=draft if isinstance(draft,dict) else {}
    sections=[]
    def add(title,rows,**kwargs):sections.append(_section(title,rows,markdown=markdown,**kwargs))
    basic=('name','age','occupation','era','gender','player','residence','birthplace')
    add('基本资料',[(_label(key),_value(draft.get(key))) for key in basic])
    attrs=draft.get('attributes'); attrs=attrs if isinstance(attrs,dict) else {}
    add('属性',[(_LABELS.get(key,str(key)) if key in _ATTRIBUTES else str(key),_value(value)) for key,value in attrs.items()]
        +[(_LABELS[key],'未识别') for key in _ATTRIBUTES if key not in attrs and _LABELS[key] not in attrs])
    skills=draft.get('skills')
    add('技能',[(str(key),_value(value)) for key,value in skills.items()] if isinstance(skills,dict) else [('技能',_value(skills))],headings=('技能','数值'))
    derived=draft.get('derived');derived=derived if isinstance(derived,dict) else {}
    aliases={key for keys in _DERIVED for key in keys}
    add('派生数值',[( _label(key) if key in aliases else str(key),
        _resource(value) if key in {'hp','mp','san','sanity'} else _value(value)) for key,value in derived.items()]
        +[(_LABELS[keys[0]],'未识别') for keys in _DERIVED if not any(key in derived for key in keys)])
    add('幸运与资产',[(_label(key),_value(draft.get(key))) for key in ('luck','cash','assets')])
    add('职业技能',_flat_rows(draft.get('occupation_skills')))
    allocations=draft.get('allocations')
    add('技能点分配',[(str(key),_value(value,allocation=True)) for key,value in allocations.items()]
        if isinstance(allocations,dict) else [('技能点分配',_value(allocations))])
    for key in ('equipment','background','creation','age_evidence'):
        add(_LABELS[key],_flat_rows(draft.get(key)))
    known=set(basic)|{'attributes','skills','derived','luck','cash','assets','occupation_skills','allocations',
                      'equipment','background','creation','age_evidence','native_sections','skill_slots','unmapped'}
    other={key:value for key,value in draft.items() if key not in known and not _internal(key)}
    if other:add('其他角色字段',_flat_rows(other))
    native_sections=draft.get('native_sections') if native_sections is None else native_sections
    review_rows=[]
    if native_sections is not None:add('模板完整栏目',_source_rows(native_sections,review_rows=review_rows))
    skill_slots=draft.get('skill_slots') if skill_slots is None else skill_slots
    if skill_slots is not None:add('模板技能槽',_source_rows(skill_slots,review_rows=review_rows))
    if sources is not None:add('其他原字段',_source_rows(sources,review_rows=review_rows))
    unmapped=draft.get('unmapped') if unmapped is None else unmapped
    if unmapped is not None:add('未映射字段',_source_rows(unmapped,review_rows=review_rows))
    if review_rows:add('待核原字段',list(dict.fromkeys(review_rows)))
    rendered_issues=issue_text(issues or [],markdown=markdown)
    # Passive reply limits may defer later parts. The actionable review must
    # precede the detailed card rather than disappearing behind continuation.
    sections.insert(0,('**待补充与核对**' if markdown else '待补充与核对：')+'\n\n'+(rendered_issues or '无'))
    return ('**识别预览**' if markdown else '识别预览：')+'\n\n'+'\n\n'.join(sections)
