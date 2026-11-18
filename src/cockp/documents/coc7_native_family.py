"""Semantic authority registry for the structurally verified fifteen-tab card.

Coordinates are part of a recognised template contract, never caller input.
Unfilled cells, unnamed skill slots and auxiliary notes remain distinguishable.
This module does not calculate Excel, infer creation rolls, or approve a card.
"""
from __future__ import annotations

import copy
import re

PROFILE = 'coc7-fifteen-sheet-family'
CONTRACT = 'coc7-fifteen-native-v1'


def _record(sheet, cell, label, category, kind='text', *, minimum=None,
            maximum=None, nullable=True, aliases=(), pointer=None):
    result = {'sheet': sheet, 'cell': cell, 'label': label, 'category': category,
              'type': kind, 'nullable': nullable, 'editable': True,
              'aliases': list(aliases)}
    if minimum is not None: result['min'] = minimum
    if maximum is not None: result['max'] = maximum
    if kind == 'text' and maximum is None: result['max'] = 2000
    if pointer is not None: result['pointer'] = pointer
    return result


def native_field_registry(profile_id):
    """Return immutable-by-copy, typed input authorities, not mirror cells."""
    if profile_id != PROFILE: return {}
    fields = {}
    def add(key, sheet, cell, label, category, kind='text', **kwargs):
        fields[key] = _record(sheet, cell, label, category, kind, **kwargs)
    for key, cell, label in [('name','E25','姓名'),('player','E26','玩家'),
            ('gender','M27','性别'),('residence','E28','住地'),
            ('birthplace','M28','故乡'),('era','E8','时代'),('occupation','AB38','职业')]:
        add(key,'建卡',cell,label,'基本资料',maximum=100,
            pointer='/'+key if key in {'name','era','occupation'} else None)
    add('age','建卡','E27','年龄','基本资料','integer',minimum=15,maximum=90,pointer='/age')
    for key,cell,label in [('STR','D15','力量'),('CON','D17','体质'),('SIZ','D19','体型'),
            ('DEX','J15','敏捷'),('APP','J17','外貌'),('INT','J19','智力'),('POW','P15','意志'),('EDU','P17','教育')]:
        add('attributes.'+key,'建卡',cell,label,'基础属性','integer',minimum=0,maximum=99,
            aliases=(key,),pointer='/attributes/'+key)
    add('luck','建卡','D33','幸运','基础属性','integer',minimum=0,maximum=99,pointer='/luck')
    add('occupation.index','建卡','E38','职业序号','建卡设置','integer',minimum=1,maximum=9999)
    add('occupation.card_index','人物卡','M5','主卡职业序号','建卡设置','integer',minimum=1,maximum=9999)
    # The current date is a display/state input, not creation-roll evidence.
    for key,cell,label,kind,low,high in [('calendar','E29','历法','text',None,100),
            ('year','G29','年','integer',-9999,9999),('month','J29','月','text',None,100),
            ('day','L29','日','text',None,100),('time','N29','时间','text',None,100)]:
        add('date.'+key,'建卡',cell,'团内日期·'+label,'团内状态',kind,minimum=low,maximum=high)
    for key,cell,label,kind,low,high in [('calendar','E8','历法','text',None,100),
            ('year','G8','年','integer',-9999,9999),('month','J8','月','text',None,100),
            ('day','L8','日','text',None,100),('time','N8','时间','text',None,100),
            ('hp_status','I11','生命状态','text',None,100),('san_status','R11','理智状态','text',None,100),
            ('temporary_hp','I12','临时生命值','integer',0,999),('san_loss_today','N12','今日理智损失','integer',0,999),
            ('mp_used','X12','用过的魔法值','integer',0,999),('movement','AE12','移动方式','text',None,100),
            ('movement_note','AG12','移动方式说明','text',None,500),('movement_adjustment','AI12','移动调整说明','text',None,100),
            ('armor_type','AN12','护甲类型','text',None,100),('armor_note','AP10','护甲备注','text',None,500)]:
        add('state.'+key,'人物卡',cell,label,'团内状态',kind,minimum=low,maximum=high)
    special_left = {20,21,22,34,35,36,37,38,39,40,41,46,47,48,49}
    special_right = {27,31,32,33,37,47}
    for side,label_col,special_col,growth,occ,interest,mark in [
            ('left','F','H','L','N','P','B'),('right','AB','AD','AH','AJ','AL','X')]:
        for row in range(16,50):
            stem=f'skills.{side}.{row}'
            category='技能·左栏' if side=='left' else '技能·右栏'
            add(stem+'.name','人物卡',label_col+str(row),f'{category}第{row-15}槽·技能名称',category,maximum=100)
            if row in (special_left if side=='left' else special_right):
                add(stem+'.specialty','人物卡',special_col+str(row),f'{category}第{row-15}槽·专长',category,maximum=100)
            for part,col,title in [('growth',growth,'成长点'),('occupation',occ,'职业加点'),('interest',interest,'兴趣加点')]:
                add(stem+'.'+part,'人物卡',col+str(row),f'{category}第{row-15}槽·{title}',category,
                    'integer',minimum=0,maximum=999)
            add(stem+'.check','人物卡',mark+str(row),f'{category}第{row-15}槽·成功标记',category,maximum=20)
    for name,row,title in [('description',61,'形象描述'),('beliefs',63,'思想与信念'),
            ('important_people',65,'重要之人'),('significant_places',67,'意义非凡之地'),
            ('treasured_possessions',69,'宝贵之物'),('traits',71,'特质'),
            ('injuries',73,'伤口和疤痕'),('phobias',75,'恐惧症和躁狂症')]:
        add('background.'+name,'人物卡','AA'+str(row),title,'背景故事',maximum=4000,pointer='/background/'+name)
        # AR61 is the "key connection" column heading. Only following rows
        # are checkbox inputs; none is background prose.
        if row != 61:
            add('background.connection.'+name,'人物卡','AR'+str(row),title+'·关键连接标记','背景故事',maximum=20)
    add('background.story','人物卡','W77','额外背景故事','背景故事',maximum=4000,pointer='/background/story')
    add('finances.unit','人物卡','S62','货币单位','资产',maximum=100)
    add('finances.other_description','人物卡','L63','其他资产说明','资产',maximum=2000)
    for col,key,title in [('B','transport','交通工具'),('F','home','住所'),('J','luxury','奢侈品'),
            ('N','securities','股票或证券'),('R','other','其他资产')]:
        add('finances.'+key+'.description','人物卡',col+'70',title+'·说明','资产',maximum=2000)
        add('finances.'+key+'.value','人物卡',col+'75',title+'·数量或价值','资产','number',minimum=0,maximum=1e15)
    for row in range(79,94):
        for key,col,title in [('status','B','状态'),('location','D','部位'),('name','F','物品名称'),('backpack','N','背包物品')]:
            add(f'equipment.{row-78}.{key}','人物卡',col+str(row),f'随身物品{row-78}·{title}','随身物品',maximum=1000)
    for row in range(53,59):
        for key,col,title,kind in [('name','B','武器名称','text'),('type','G','类型','text'),
                ('skill','M','使用技能','text'),('regular','Q','普通成功率','integer'),
                ('hard','S','困难成功率','integer'),('extreme','U','极难成功率','integer'),
                ('damage','W','伤害','text'),('range','AA','基础射程','text'),('impale','AC','贯穿','text'),
                ('attacks','AE','次数','text'),('capacity','AG','装弹量','text'),('malfunction','AJ','故障值','text')]:
            # Rows 53..56 contain formula-backed preset columns. They are
            # retained in the semantic view, never offered as editable inputs.
            if (row==53 and key!='name') or (54<=row<=56 and key not in {'name','type'}): continue
            kwargs={'minimum':0,'maximum':999} if kind=='integer' else {'maximum':500}
            add(f'weapons.{row-52}.{key}','人物卡',col+str(row),f'武器{row-52}·{title}','武器',kind,**kwargs)
    for row in range(97,114):
        # The last row has two independent input anchors, not the earlier
        # two-column table geometry.
        for col,key,title in ([('B','module','经历模组'),('F','change','人物变化')] if row==113 else
                              [('B','module','经历模组'),('J','change','人物变化')]):
            add(f'experiences.{row-96}.{key}','人物卡',col+str(row),f'调查员经历{row-96}·{title}','调查员经历',maximum=2000)
    for row in range(98,111):
        for key,col,title,kind in [('kind','W','类别','text'),('name','AA','第三类接触','text'),
                ('note','AK','说明','text'),('count','AR','次数','integer')]:
            add(f'mythos.{row-97}.{key}','人物卡',col+str(row),f'神话相关{row-97}·{title}','神话相关',kind,
                **({'minimum':0,'maximum':999999} if kind=='integer' else {'maximum':2000}))
    for row in range(114,127):
        for key,col,title in [('index','W','编号'),('name','Y','法术名称'),('cost','AC','使用代价'),('effect','AH','作用')]:
            add(f'spells.{row-113}.{key}','人物卡',col+str(row),f'法术{row-113}·{title}','法术',maximum=2000)
    for row in range(130,145):
        for key,col,title in [('name','W','姓名'),('player','AA','玩家'),('note','AD','注释'),
                ('change','AL','造成改变'),('module','AP','相遇模组')]:
            add(f'companions.{row-129}.{key}','人物卡',col+str(row),f'伙伴{row-129}·{title}','调查员伙伴',maximum=1000)
    for key,cell,title,kind,low,high in [('custom_art','AU26','自定义技艺名称','text',None,100),
            ('custom_art_base','BA26','自定义技艺基础值','integer',0,99),
            ('custom_fighting','AU28','自定义格斗名称','text',None,100),('custom_fighting_base','BA28','自定义格斗基础值','integer',0,99),
            ('custom_firearms','AU30','自定义射击名称','text',None,100),('custom_firearms_base','BA30','自定义射击基础值','integer',0,99),
            ('custom_pilot','AU32','自定义驾驶名称','text',None,100),('custom_pilot_base','BA32','自定义驾驶基础值','integer',0,99),
            ('custom_science','AU34','自定义科学名称','text',None,100),('custom_science_base','BA34','自定义科学基础值','integer',0,99),
            ('age_adjustment','AU37','自定义年龄补正','text',None,1000),('currency','BK37','自定义货币名称','text',None,100),
            ('exchange_rate','BO37','与美元汇率','number',0,1e12),('experience_name','BC26','自定义经历包名称','text',None,100),
            ('experience_san','BC28','经历包理智减少','integer',0,999),('experience_skill','BG28','经历包技能增长','integer',0,999),
            ('experience_note','BC30','经历包备注','text',None,2000),('status_name','BK26','自定义状态名称','text',None,100),
            ('physical_status','BK28','自定义身体状态','text',None,1000),('mental_status','BO28','自定义精神状态','text',None,1000),
            ('occupation_specialty','AU22','任意特长选择','text',None,1000),('experience_selector','AU24','经历包选择','text',None,1000),
            ('invention_type','AZ129','发明或发现显示类型','text',None,100),('invention_parameter','BL129','发明或发现参数','integer',0,999999),
            ('madness_result','S139','疯狂症状出目记录','integer',0,100)]:
        add('settings.'+key,'人物卡',cell,title,'模板设置',kind,minimum=low,maximum=high)
    for row in range(18,23):
        for col,key in [('BA','any'),('BJ','occupation')]:
            add(f'choices.{key}.{row-17}','人物卡',col+str(row),f'本职任意特长选择{key}{row-17}','职业选择',maximum=100)
    return copy.deepcopy(fields)


def enrich_profile(profile):
    if profile['id'] != PROFILE: return profile
    result=copy.deepcopy(profile); result['fields']=native_field_registry(PROFILE)
    result['skill_table']['stop']=49
    result['native_contract']=CONTRACT
    return result


def native_readonly_registry(profile_id):
    """Visible calculated fields; editing must follow their input dependencies."""
    if profile_id != PROFILE:return {}
    result={}
    for key,cell,label in [('hp_current','E10','当前生命值'),('hp_max','G10','最大生命值'),
            ('san_start','N10','起始理智值'),('san_current','R12','当前理智值'),('san_max','P10','理智上限'),
            ('mp_current','W10','当前魔法值'),('mp_max','Y10','最大魔法值'),('mp_per_hour','AA11','每小时魔法恢复'),
            ('movement','AF10','移动力'),('movement_adjustment','AI11','移动力调整'),
            ('damage_bonus','AP52','伤害加值'),('build','AP55','体格'),
            ('cash','O62','当前现金'),('assets_display','L62','其他资产显示'),('credit','B62','信用评级'),
            ('living_standard','F62','生活水平'),('spending','I62','消费水平')]:
        result[key]={'sheet':'人物卡','cell':cell,'label':label,'category':'资源与派生数值','editable':False}
    return result


def merge_anchor(book, sheet, address):
    """Return the only real authority of a merged cell (including empty slots)."""
    def coord(cell):
        match=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',cell)
        if match is None:return None
        col=0
        for letter in match[1]:col=col*26+ord(letter)-64
        return col,int(match[2])
    indexes=book.setdefault('_native_merge_indexes',{})
    if sheet not in indexes:
        indexes[sheet]=[(coord(ends[0]),coord(ends[-1]),ends[0])
                        for merged in book['sheets'][sheet]['merges'] if (ends:=merged.split(':'))]
    point=coord(address)
    if point is None:return None
    for start,stop,anchor in indexes[sheet]:
        if start and stop and start[1]<=point[1]<=stop[1] and start[0]<=point[0]<=stop[0]:return anchor
    return address


_SPECIAL = re.compile(r'^(技艺|科学|格斗|射击|外语)[①②③]$')
_GROUPS = {'技艺':'艺术与手艺','外语':'其他语言','科学':'科学','格斗':'格斗',
           '射击':'射击','驾驶':'驾驶','生存':'生存','母语':'母语'}


def skill_name(label, specialty):
    """Names come from explicit slot inputs; empty placeholders are not names."""
    if type(label) is not str or not label.strip():return None
    label=label.strip(); match=_SPECIAL.fullmatch(label)
    group=match[1] if match else label.rstrip('：: ')
    special=(match is not None or label.endswith(('：',':')) or group=='母语')
    if special:
        if type(specialty) is not str or not specialty.strip():
            return '母语' if group=='母语' else None
        return _GROUPS.get(group,group)+'('+specialty.strip()+')'
    return label


def native_projection(book, profile, origin, *, catalogs=()):
    """Project a trusted recognised family; all original source remains elsewhere.

    ``origin`` supplies exact source/cache or a labelled strict resolver result.
    Native sections retain data not representable by the current card schema.
    The output never sets approval, owner, IDs, creation method, or dice proof.
    """
    if profile['id'] != PROFILE:return None
    registry=profile['fields']; sections={}; canonical=[]; slots=[]; used=set(); problems=[]
    def take(sheet,cell):
        item=origin(sheet,cell);used.add((sheet,cell));return item
    def add(pointer,item,*,labels=()):
        value=item['value']
        if value is None or type(value) is str and not value.strip():return
        if pointer in {'/age','/luck'} or pointer.startswith(('/attributes/','/skills/','/allocations/','/derived/')):
            if type(value) is not int:return
        elif pointer in {'/cash','/assets'}:
            if type(value) not in (int,float):return
        elif not pointer.startswith('/equipment/') and type(value) is not str:return
        canonical.append({'pointer':pointer,'value':value,'operation':'copy','sources':[item],
                          **({'label_sources':list(labels)} if labels else {})})
    for field,record in registry.items():
        sheet,cell=record['sheet'],record['cell']
        if sheet not in book['sheets'] or cell not in book['sheets'][sheet]['cells'] or merge_anchor(book,sheet,cell)!=cell:continue
        item=take(sheet,cell)
        sections.setdefault(record['category'],[]).append({'field':field,'label':record['label'],'source':item})
        if record.get('pointer'):add(record['pointer'],item)
    for field,record in native_readonly_registry(PROFILE).items():
        item=take(record['sheet'],record['cell'])
        sections.setdefault(record['category'],[]).append({'field':field,'label':record['label'],'source':item})
        if field=='cash':add('/cash',item)
    for side,label_col,special_col,base,growth,occ,interest,total,mark,job_mark in [
            ('left','F','H','J','L','N','P','R','B','D'),
            ('right','AB','AD','AF','AH','AJ','AL','AN','X','Z')]:
        for row in range(16,50):
            items={key:take('人物卡',col+str(row)) for key,col in [('label',label_col),('base',base),
                ('growth',growth),('occupation',occ),('interest',interest),('total',total),('success_mark',mark),('occupation_mark',job_mark)]}
            address=special_col+str(row)
            specialty=take('人物卡',address) if merge_anchor(book,'人物卡',address)==address else None
            name=skill_name(items['label']['value'],None if specialty is None else specialty['value'])
            slot={'id':f'{side}.{row}','row':row,'panel':side,'name':name,'sources':items,'specialty_source':specialty}
            slots.append(slot)
            if name:
                escaped=name.replace('~','~0').replace('/','~1');labels=[items['label']]+([specialty] if specialty is not None else [])
                add('/skills/'+escaped,items['total'],labels=labels)
                for key in ('base','occupation','interest'):add('/allocations/'+escaped+'/'+key,items[key],labels=labels)
            elif any(type(items[key]['value']) is int and items[key]['value']!=0 for key in ('occupation','interest','growth')):
                problems.append({'code':'native_skill_specialty_missing','slot':slot['id'],
                    'message':f'{"左栏" if side=="left" else "右栏"}第{row-15}个技能槽未填专长名，加点已保留。可填写原表，或让我修改Excel副本后再整理。'})
    names={slot['name'] for slot in slots if slot['name']}
    duplicates={name for name in names if sum(slot['name']==name for slot in slots)>1}
    if duplicates:
        prefixes=[('/skills/'+name.replace('~','~0').replace('/','~1'),
                   '/allocations/'+name.replace('~','~0').replace('/','~1')+'/') for name in duplicates]
        canonical=[row for row in canonical if not any(row['pointer']==skill or row['pointer'].startswith(allocation)
                                                      for skill,allocation in prefixes)]
        for name in sorted(duplicates):
            problems.append({'code':'native_duplicate_skill_name',
                'slots':[slot['id'] for slot in slots if slot['name']==name],
                'message':'多个技能槽使用相同名称；各槽名称、点数与来源完整保留，未擅自合并或选择其一。'})
    # Main-card authored tables, including empty slots, are structured rather
    # than passed wholesale through a language model.
    for category,row_range,columns in [('weapons',range(53,59),
            [('name','B'),('type','G'),('skill','M'),('regular','Q'),('hard','S'),('extreme','U'),
             ('damage','W'),('range','AA'),('impale','AC'),('attacks','AE'),('capacity','AG'),('malfunction','AJ')])]:
        sections[category]=[{'slot':row-52,'fields':{key:take('人物卡',col+str(row)) for key,col in columns}} for row in row_range]
    equipment=[]
    for row in range(79,94):
        items={key:take('人物卡',col+str(row)) for key,col in [('status','B'),('location','D'),('name','F'),('backpack','N')]}
        if type(items['name']['value']) is str and items['name']['value'].strip():
            equipment.append({key:item for key,item in items.items() if key!='backpack'})
        # N is an independent backpack inventory slot. Row alignment does not
        # assert that this item belongs to the left-hand item in F.
        if type(items['backpack']['value']) is str and items['backpack']['value'].strip():
            equipment.append({'name':items['backpack']})
    for weapon in sections['weapons']:
        if type(weapon['fields']['name']['value']) is str and weapon['fields']['name']['value'].strip():equipment.append(weapon['fields'])
    for number,items in enumerate(equipment):
        for key,item in items.items():add(f'/equipment/{number}/{key}',item)
    # Empty string caches are an explicit Excel value, not absent evidence.
    # Every nonempty/formula anchor receives an auditable disposition.
    verified={item['sheet'] for item in catalogs}
    dispositions=[]; unresolved=[]
    for sheet,spec in book['sheets'].items():
        for address,cell in spec['cells'].items():
            ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
            raw_formula=cell.find(ns+'f');raw_value=cell.find(ns+'v')
            if (raw_formula is None and cell.find(ns+'is') is None
                    and (raw_value is None or raw_value.text is None)
                    and not (cell.attrib.get('t')=='str' and raw_value is not None)):
                continue
            item=origin(sheet,address);value=item['value'];formula=item.get('formula')
            if value is None and formula is None:continue
            if merge_anchor(book,sheet,address)!=address:continue
            key=(sheet,address)
            if formula is not None and item.get('value_origin')=='unknown':
                # Changed definitions, active errors, missing numeric inputs,
                # cycles and random requests remain explicit unknowns. A raw
                # cache may be retained alongside them but is never adopted.
                kind='unresolved';unresolved.append((sheet,address))
            elif key in used:kind='known_input' if formula is None else 'mirror'
            elif sheet in verified:kind='reference'
            elif formula is not None:kind='mirror'
            elif sheet in {'建卡','人物卡'}:
                # Unregistered values are retained explicitly as notes, not
                # guessed into card fields or silently declared absent.
                style=int(cell.attrib.get('s','0'))
                protection=book['styles'][style].find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}protection')
                locked=protection is None or protection.attrib.get('locked','1')!='0'
                kind='known_label' if locked else 'preserved_note'
                if not locked:sections.setdefault('保留原文',[]).append({'source':item})
            else:kind='unresolved';unresolved.append((sheet,address))
            dispositions.append({'sheet':sheet,'cell':address,'kind':kind,'source':item})
    return {'canonical_fields':canonical,'canonical_skills':slots,'semantic_cells':sorted(used),
        'native_sections':sections,'skill_slots':slots,'semantic_dispositions':dispositions,
        'native_model_source_cells':[], 'native_unresolved_cells':unresolved,
        'native_issues':problems,'native_contract':CONTRACT,
        'native_policy':'Known regions are projected by program. Unfilled slots remain unfilled; other source remains indexed, never assumed absent or approved.'}
