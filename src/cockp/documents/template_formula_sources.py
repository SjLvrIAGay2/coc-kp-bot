"""Finite, source-accounted formula reads for one verified template family.

This is not an Excel engine. Only exact registered formulas may run. Random,
external, changed, cyclic and active broken references always remain unknown.
Stored caches are kept distinct from deterministic program results.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Any

from .template_formula_registry import CONTRACT, DEFINED_NAMES, FORMULA_SHA256, PROFILE_ID

NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_ADDRESS=re.compile(r'\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6})')
_STRINGS=re.compile(r'"(?:[^"]|"")*"')
_CELL_TOKEN=re.compile(r'(?<![A-Za-z0-9_.])(?P<c>\$?[A-Z]{1,3})(?P<r>\$?[1-9][0-9]*)(?![A-Za-z0-9_.(])')
_TOKENS=re.compile(r'\s*(?:'
    r'(?P<string>"(?:[^"]|"")*")|(?P<quoted>\'(?:[^\']|\'\')*\')|'
    r'(?P<error>\#[A-Z0-9/?!]+)|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)|'
    r'(?P<name>[$A-Za-z_\u0080-\uffff][$A-Za-z0-9_.\u0080-\uffff]*)|'
    r'(?P<operator><=|>=|<>|[+*/&=<>%,:!(){};\-]))')
_FUNCTIONS={'IF','IFERROR','AND','OR','NOT','ISBLANK','ISTEXT','ISNUMBER','TRUE',
    'INT','SUM','MAX','AVERAGE','ABS','CEILING','CHAR','LOOKUP','VLOOKUP','INDEX','MATCH'}
_MAX_GRAPH=6000
_MAX_RANGE=20000


class FormulaSourceError(ValueError):
    def __init__(self,code,*,catchable=False):
        super().__init__(code);self.code=code;self.catchable=catchable


@dataclass(frozen=True)
class _Blank:
    """An absent source value, never an invented numeric zero."""


BLANK=_Blank()


def _fail(code):raise FormulaSourceError(code)


def _address(value):
    match=_ADDRESS.fullmatch(str(value))
    if match is None:_fail('formula_address_invalid')
    column=0
    for char in match[1]:column=column*26+ord(char)-64
    row=int(match[2])
    if not 1<=column<=16384 or not 1<=row<=1048576:_fail('formula_address_outside_bounds')
    return column,row


def _column(number):
    if not 1<=number<=16384:_fail('formula_address_outside_bounds')
    result=''
    while number:number,digit=divmod(number-1,26);result=chr(65+digit)+result
    return result


def _canonical_address(value):
    column,row=_address(value);return _column(column)+str(row)


def _translate(formula,anchor,target):
    ac,ar=_address(anchor);tc,tr=_address(target)
    def replace(match):
        col,row=match['c'],match['r'];cc,rr=_address(col+row)
        cc+=0 if col.startswith('$') else tc-ac
        rr+=0 if row.startswith('$') else tr-ar
        if not 1<=rr<=1048576:_fail('formula_shared_reference_outside_bounds')
        return ('$' if col.startswith('$') else '')+_column(cc)+('$' if row.startswith('$') else '')+str(rr)
    result=[];end=0
    for quoted in _STRINGS.finditer(formula):
        result.extend([_CELL_TOKEN.sub(replace,formula[end:quoted.start()]),quoted[0]]);end=quoted.end()
    result.append(_CELL_TOKEN.sub(replace,formula[end:]));return ''.join(result)


def _formula(cell):
    f=None if cell is None else cell.find(NS+'f')
    return None if f is None else {'text':f.text,'attributes':dict(f.attrib)}


def _expanded_formula(book,sheet,address):
    cells=book['sheets'][sheet]['cells'];record=_formula(cells.get(address))
    if record is None:_fail('formula_missing')
    if record['text']:return record['text']
    attrs=record['attributes']
    if attrs.get('t')!='shared' or 'si' not in attrs:_fail('formula_shared_master_missing')
    masters=[(a,_formula(c)) for a,c in cells.items()
        if _formula(c) is not None and _formula(c)['attributes'].get('t')=='shared'
        and _formula(c)['attributes'].get('si')==attrs['si'] and _formula(c)['text']]
    if len(masters)!=1:_fail('formula_shared_master_ambiguous')
    anchor,master=masters[0];extent=master['attributes'].get('ref')
    if not extent or ':' not in extent:_fail('formula_shared_range_missing')
    left,right=extent.split(':');lc,lr=_address(left);rc,rr=_address(right);cc,cr=_address(address)
    if not lc<=cc<=rc or not lr<=cr<=rr:_fail('formula_shared_cell_outside_range')
    return _translate(master['text'],anchor,address)


def _signature(formula):return hashlib.sha256(formula.encode()).hexdigest()


def _registered(book,sheet,address):
    text=_expanded_formula(book,sheet,address)
    if _signature(text)!=FORMULA_SHA256.get(sheet,{}).get(address):_fail('formula_not_registered')
    return text


def _number(value):
    if isinstance(value,_Blank):_fail('formula_input_blank')
    if type(value) not in (int,float,Decimal):_fail('formula_input_not_number')
    result=value if isinstance(value,Decimal) else Decimal(str(value))
    if not result.is_finite() or abs(result.adjusted())>100:_fail('formula_number_outside_bounds')
    return result


def _external(value):
    if isinstance(value,_Blank):return None
    if isinstance(value,Decimal):
        _number(value)
        if value==value.to_integral_value():return int(value)
        value=float(value)
    if type(value) is float and not math.isfinite(value):_fail('formula_number_outside_bounds')
    if value is not None and type(value) not in (str,int,float,bool):_fail('formula_result_type_invalid')
    return value


def _equal(left,right):
    if isinstance(left,_Blank) or isinstance(right,_Blank):
        # Only an explicit empty-text comparison is a blank guard. It cannot
        # turn a missing numeric input into zero.
        other=right if isinstance(left,_Blank) else left
        if type(other) is str:return other==''
        _fail('formula_input_blank')
    if type(left) is str and type(right) is str:return left.casefold()==right.casefold()
    if type(left) in (int,float,Decimal) and type(right) in (int,float,Decimal):return _number(left)==_number(right)
    return type(left) is type(right) and left==right


def _boolean(value):
    if type(value) is bool:return value
    return _number(value)!=0


class _Parser:
    def __init__(self,text):
        if not isinstance(text,str) or len(text)>20000:_fail('formula_text_limit')
        text=text.strip()
        self.tokens=[];pos=0
        while pos<len(text):
            match=_TOKENS.match(text,pos)
            if match is None:_fail('formula_syntax_unsupported')
            self.tokens.append((match.lastgroup,match[match.lastgroup]));pos=match.end()
            if len(self.tokens)>10000:_fail('formula_token_limit')
        self.pos=0;self.depth=0
    def take(self,value=None):
        if self.pos>=len(self.tokens):_fail('formula_incomplete')
        item=self.tokens[self.pos]
        if value is not None and item[1]!=value:_fail('formula_syntax_unsupported')
        self.pos+=1;return item
    def peek(self,value):return self.pos<len(self.tokens) and self.tokens[self.pos][1]==value
    def parse(self):
        node=self.expression()
        if self.pos!=len(self.tokens):_fail('formula_extra_tokens')
        return node
    def expression(self,minimum=0):
        self.depth+=1
        if self.depth>80:_fail('formula_depth_limit')
        kind,value=self.take()
        if value in ('+','-'):left=('unary',value,self.expression(50))
        elif value=='(':
            left=self.expression();self.take(')')
        elif kind=='string':left=('literal',value[1:-1].replace('""','"'))
        elif kind=='number':left=('literal',_number(Decimal(value)))
        elif kind=='error':left=('error',value)
        elif kind in ('name','quoted'):
            if self.peek('!'):
                self.take('!');address=self.take()[1]
                if address.startswith('#'):left=('error',address)
                else:
                    sheet=value[1:-1].replace("''", "'") if kind=='quoted' else value
                    left=('cell',sheet,_canonical_address(address))
            elif self.peek('('):
                name=value.upper()
                if name not in _FUNCTIONS:_fail('formula_function_not_allowed')
                self.take('(');args=[]
                if not self.peek(')'):
                    while True:
                        args.append(('literal',Decimal(0)) if self.peek(',') or self.peek(')') else self.expression())
                        if not self.peek(','):break
                        self.take(',')
                self.take(')');left=('call',name,args)
            elif kind=='quoted':_fail('formula_sheet_reference_missing')
            elif _ADDRESS.fullmatch(value):left=('cell',None,_canonical_address(value))
            elif value.upper() in {'TRUE','FALSE'}:left=('literal',value.upper()=='TRUE')
            else:left=('name',value)
        elif value=='{':
            rows=[[]]
            while True:
                item=self.expression()
                if item[0]!='literal':_fail('formula_array_constant_required')
                rows[-1].append(item)
                if self.peek('}'):
                    self.take('}');break
                if self.peek(';'):self.take(';');rows.append([])
                else:self.take(',')
            if len({len(row) for row in rows})!=1 or sum(map(len,rows))>_MAX_RANGE:_fail('formula_array_limit')
            left=('array',rows)
        else:_fail('formula_syntax_unsupported')
        if self.peek(':'):
            self.take(':');right=self.take()[1]
            if left[0]!='cell':_fail('formula_range_invalid')
            left=('range',left[1],left[2],_canonical_address(right))
        priorities={'=':10,'<>':10,'<':10,'>':10,'<=':10,'>=':10,'&':20,'+':30,'-':30,'*':40,'/':40}
        while self.pos<len(self.tokens) and priorities.get(self.tokens[self.pos][1],-1)>=minimum:
            op=self.take()[1];left=('binary',op,left,self.expression(priorities[op]+1))
        self.depth-=1;return left


@dataclass
class _Resolved:
    value:Any
    graph:dict


class TemplateFormulaResolver:
    """Call only after the adapter has verified the fixed profile identity."""
    def __init__(self,book,*,profile_id,read_value,source_sha256):
        if profile_id!=PROFILE_ID:_fail('formula_profile_not_supported')
        self.book=book;self.profile_id=profile_id;self.read_value=read_value;self.source_sha256=source_sha256
        self.cache={};self.active=set();self.total_reads=0

    def resolve(self,sheet,address):
        try:
            result=self._get(sheet,_canonical_address(address));value=_external(result.value)
            root=self._key(sheet,address);node=result.graph[root]
            computation=({'contract':CONTRACT,'profile':self.profile_id,'source_sha256':self.source_sha256,
                'root':[sheet,address],'value':value,'nodes':list(result.graph.values())}
                if node['kind']=='program_formula' and not isinstance(result.value,_Blank) else None)
            return {'value':value,'available':not isinstance(result.value,_Blank),'value_origin':node['kind'],
                'expanded_formula':node.get('expanded_formula'),'computation':computation,
                'dependencies':[copy.deepcopy(n) for key,n in result.graph.items() if key!=root],
                'diagnostics':[] if not isinstance(result.value,_Blank) else ['formula_input_blank']}
        except (FormulaSourceError,ArithmeticError,ValueError,KeyError,TypeError,OverflowError) as exc:
            return {'value':None,'available':False,'value_origin':'unknown','expanded_formula':None,
                'computation':None,'dependencies':[],'diagnostics':[exc.code if isinstance(exc,FormulaSourceError) else 'formula_source_invalid']}

    @staticmethod
    def _key(sheet,address):return sheet+'!'+address

    def _get(self,sheet,address):
        key=self._key(sheet,address)
        if key in self.cache:return self.cache[key]
        if key in self.active:_fail('formula_dependency_cycle')
        self.total_reads+=1
        if self.total_reads>100000 or len(self.active)>80:_fail('formula_dependency_limit')
        if sheet not in self.book['sheets']:_fail('formula_sheet_missing')
        cell=self.book['sheets'][sheet]['cells'].get(address);formula=_formula(cell)
        node={'sheet':sheet,'cell':address,'source_sha256':self.source_sha256}
        self.active.add(key)
        try:
            if cell is not None and cell.attrib.get('t')=='e':_fail('formula_source_error_value')
            value=self.read_value(cell,self.book.get('strings',[]))
            if formula is None:
                value=BLANK if value is None else value;node.update(kind='stored_input',value=_external(value))
                result=_Resolved(value,{key:node})
            else:
                text=_registered(self.book,sheet,address)
                node.update(formula=formula,expanded_formula=text,formula_sha256=_signature(text))
                stored=cell.find(NS+'v')
                if stored is not None:
                    if value is None:_fail('formula_cache_empty_untyped')
                    node.update(kind='stored_formula_cache',value=_external(value));result=_Resolved(value,{key:node})
                else:
                    graph={};children=[]
                    parsed=_Parser(text).parse()
                    skill_terms=self._skill_addition(sheet,address,parsed)
                    if skill_terms is None:value=self._evaluate(parsed,sheet,graph,children)
                    else:
                        # These registered four-component total formulas use
                        # Excel's blank-neutral addition, equivalent to SUM.
                        # A real empty string is never converted into zero,
                        # nor is a missing attribute/input filled as a fact.
                        values=[self._evaluate(term,sheet,graph,children) for term in skill_terms]
                        numbers=[]
                        for item in values:
                            if isinstance(item,_Blank):continue
                            if type(item) is str and re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',item):
                                item=Decimal(item)
                            numbers.append(_number(item))
                        if not numbers:_fail('formula_numeric_inputs_missing')
                        value=sum(numbers,Decimal(0))
                    node.update(kind='program_formula',value=_external(value),dependencies=sorted(set(children)))
                    graph[key]=node
                    if len(graph)>_MAX_GRAPH:_fail('formula_proof_limit')
                    result=_Resolved(value,graph)
            self.cache[key]=result;return result
        finally:self.active.remove(key)

    @staticmethod
    def _skill_addition(sheet,address,node):
        match=re.fullmatch(r'(R|AN)([1-9][0-9]*)',address)
        if sheet!='人物卡' or match is None or not 16<=int(match[2])<=49:return None
        terms=[]
        def flatten(item):
            if item[0]=='binary' and item[1]=='+':return flatten(item[2]) and flatten(item[3])
            if item[0]!='cell' or item[1] not in (None,sheet):return False
            terms.append(item);return True
        if not flatten(node):return None
        columns=('J','L','N','P') if match[1]=='R' else ('AF','AH','AJ','AL')
        expected={col+match[2] for col in columns}
        return terms if len(terms)==4 and {item[2] for item in terms}==expected else None

    def _refer(self,sheet,address,graph,children):
        result=self._get(sheet,address);graph.update(result.graph);children.append(self._key(sheet,address))
        if len(graph)>_MAX_GRAPH:_fail('formula_proof_limit')
        return result.value

    def _evaluate(self,node,sheet,graph,children):
        kind=node[0]
        if kind=='literal':return node[1]
        if kind=='array':return [[item[1] for item in row] for row in node[1]]
        if kind=='error':_fail('formula_active_error_reference')
        if kind=='cell':return self._refer(node[1] or sheet,node[2],graph,children)
        if kind=='range':
            sc=node[1] or sheet;c1,r1=_address(node[2]);c2,r2=_address(node[3])
            if c2<c1 or r2<r1 or (c2-c1+1)*(r2-r1+1)>_MAX_RANGE:_fail('formula_range_limit')
            return [[self._refer(sc,_column(col)+str(row),graph,children) for col in range(c1,c2+1)] for row in range(r1,r2+1)]
        if kind=='name':
            name=node[1];definition=self.book.get('defined',{}).get(name)
            if name not in DEFINED_NAMES or definition!=DEFINED_NAMES[name]:_fail('formula_defined_name_changed')
            return self._evaluate(_Parser(definition).parse(),sheet,graph,children)
        if kind=='unary':
            value=_number(self._evaluate(node[2],sheet,graph,children));return value if node[1]=='+' else -value
        if kind=='binary':
            op=node[1];left=self._evaluate(node[2],sheet,graph,children);right=self._evaluate(node[3],sheet,graph,children)
            if op in ('=','<>'):return _equal(left,right) if op=='=' else not _equal(left,right)
            if op in ('<','>','<=','>='):
                left,right=_number(left),_number(right)
                return {'<':left<right,'>':left>right,'<=':left<=right,'>=':left>=right}[op]
            if op=='&':
                if isinstance(left,_Blank) or isinstance(right,_Blank):_fail('formula_input_blank')
                def text(v):return ('TRUE' if v else 'FALSE') if type(v) is bool else str(_external(v))
                result=text(left)+text(right)
                if len(result)>20000:_fail('formula_text_limit')
                return result
            left,right=_number(left),_number(right)
            with localcontext() as context:
                context.prec=40
                if op=='+':return _number(left+right)
                if op=='-':return _number(left-right)
                if op=='*':return _number(left*right)
                if op=='/':
                    if not right:_fail('formula_division_by_zero')
                    return _number(left/right)
            _fail('formula_operator_not_allowed')
        if kind!='call':_fail('formula_node_invalid')
        name,args=node[1:]
        def at(index):return self._evaluate(args[index],sheet,graph,children)
        def arity(minimum,maximum=None):
            if not minimum<=len(args)<=(minimum if maximum is None else maximum):_fail('formula_function_arity')
        if name=='IF':arity(2,3);return at(1) if _boolean(at(0)) else at(2) if len(args)==3 else False
        if name=='IFERROR':
            arity(2)
            try:return at(0)
            except FormulaSourceError as exc:
                if not exc.catchable:raise
                return at(1)
        if name=='ISBLANK':arity(1);return isinstance(at(0),_Blank)
        if name=='ISTEXT':arity(1);return type(at(0)) is str
        if name=='ISNUMBER':arity(1);return type(at(0)) in (int,float,Decimal)
        if name=='TRUE':arity(0);return True
        if name=='NOT':arity(1);return not _boolean(at(0))
        if name in ('AND','OR'):
            arity(1,1000);values=[_boolean(at(i)) for i in range(len(args))]
            return all(values) if name=='AND' else any(values)
        if name in ('SUM','MAX','AVERAGE'):
            arity(1,1000);numbers=[]
            def collect(value):
                if isinstance(value,list):
                    for child in value:collect(child)
                elif type(value) in (int,float,Decimal):numbers.append(_number(value))
                elif not isinstance(value,_Blank) and type(value) not in (str,bool):_fail('formula_input_type_invalid')
            for i in range(len(args)):collect(at(i))
            if not numbers:_fail('formula_numeric_inputs_missing')
            if name=='SUM':return sum(numbers,Decimal(0))
            if name=='MAX':return max(numbers)
            return sum(numbers,Decimal(0))/len(numbers)
        if name=='INT':arity(1);return _number(at(0)).to_integral_value(rounding=ROUND_FLOOR)
        if name=='ABS':arity(1);return abs(_number(at(0)))
        if name=='CEILING':
            arity(2);value,step=_number(at(0)),_number(at(1))
            if step<=0 or value<0:_fail('formula_ceiling_sign_unsupported')
            return (value/step).to_integral_value(rounding=ROUND_CEILING)*step
        if name=='CHAR':
            arity(1);value=_number(at(0))
            if value!=value.to_integral_value() or not 1<=value<=127:_fail('formula_character_code_unsupported')
            return chr(int(value))
        def vector(value):
            if not isinstance(value,list) or not value or not all(isinstance(row,list) for row in value):_fail('formula_lookup_range_required')
            if all(len(row)==1 for row in value):return [row[0] for row in value]
            if len(value)==1:return value[0]
            _fail('formula_lookup_vector_required')
        def exact(key,values):
            matches=[i for i,value in enumerate(values) if not isinstance(value,_Blank) and _equal(key,value)]
            if not matches:raise FormulaSourceError('formula_lookup_no_match',catchable=True)
            if len(matches)!=1:_fail('formula_lookup_ambiguous')
            return matches[0]
        def approximate(key,values,descending=False):
            key=_number(key);values=[_number(v) for v in values]
            if any((a<=b if descending else a>=b) for a,b in zip(values,values[1:])):_fail('formula_lookup_keys_not_strictly_sorted')
            matches=[i for i,value in enumerate(values) if value>=key] if descending else [i for i,value in enumerate(values) if value<=key]
            if not matches:raise FormulaSourceError('formula_lookup_no_match',catchable=True)
            return matches[-1]
        def range_descriptor(item):
            if item[0]=='range':
                sc=item[1] or sheet;c1,r1=_address(item[2]);c2,r2=_address(item[3])
                if c2<c1 or r2<r1 or (c2-c1+1)*(r2-r1+1)>_MAX_RANGE:_fail('formula_range_limit')
                return sc,c1,r1,c2,r2
            return None
        def range_cell(desc,row,column):
            sc,c1,r1,c2,r2=desc
            if not 0<=row<=r2-r1 or not 0<=column<=c2-c1:_fail('formula_index_outside_range')
            return self._refer(sc,_column(c1+column)+str(r1+row),graph,children)
        def range_vector(desc):
            sc,c1,r1,c2,r2=desc
            if c1==c2:return [range_cell(desc,i,0) for i in range(r2-r1+1)]
            if r1==r2:return [range_cell(desc,0,i) for i in range(c2-c1+1)]
            _fail('formula_lookup_vector_required')
        if name=='MATCH':
            arity(2,3);key=at(0);values=vector(at(1));mode=_number(at(2)) if len(args)==3 else Decimal(1)
            if mode not in (-1,0,1):_fail('formula_match_mode_unsupported')
            return Decimal((exact(key,values) if mode==0 else approximate(key,values,mode==-1))+1)
        if name=='INDEX':
            arity(2,3);row=_number(at(1));column=_number(at(2)) if len(args)==3 else Decimal(1)
            if row!=int(row) or column!=int(column) or row<1 or column<1:_fail('formula_index_outside_range')
            desc=range_descriptor(args[0])
            if desc is not None:return range_cell(desc,int(row)-1,int(column)-1)
            table=at(0)
            if not isinstance(table,list) or row!=int(row) or column!=int(column) or row<1 or column<1:_fail('formula_index_outside_range')
            if int(row)>len(table) or not isinstance(table[int(row)-1],list) or int(column)>len(table[int(row)-1]):_fail('formula_index_outside_range')
            return table[int(row)-1][int(column)-1]
        if name=='VLOOKUP':
            arity(3,4);key=at(0);column=_number(at(2));mode=_boolean(at(3)) if len(args)==4 else True
            desc=range_descriptor(args[1])
            if desc is not None:
                if column!=int(column) or not 1<=column<=desc[3]-desc[1]+1:_fail('formula_index_outside_range')
                keys=[range_cell(desc,i,0) for i in range(desc[4]-desc[2]+1)]
                position=approximate(key,keys) if mode else exact(key,keys)
                return range_cell(desc,position,int(column)-1)
            table=at(1)
            if not isinstance(table,list) or not table or column!=int(column) or column<1 or any(not isinstance(r,list) or len(r)<column for r in table):_fail('formula_lookup_range_required')
            keys=[r[0] for r in table];position=approximate(key,keys) if mode else exact(key,keys)
            return table[position][int(column)-1]
        if name=='LOOKUP':
            arity(3);key=at(0);lookup_desc=range_descriptor(args[1]);result_desc=range_descriptor(args[2])
            keys=range_vector(lookup_desc) if lookup_desc is not None else vector(at(1))
            position=approximate(key,keys)
            if result_desc is not None:
                size=result_desc[4]-result_desc[2]+1 if result_desc[1]==result_desc[3] else result_desc[3]-result_desc[1]+1
                if size!=len(keys):_fail('formula_lookup_range_mismatch')
                return range_cell(result_desc,position,0) if result_desc[1]==result_desc[3] else range_cell(result_desc,0,position)
            results=vector(at(2))
            if len(keys)!=len(results):_fail('formula_lookup_range_mismatch')
            return results[position]
        _fail('formula_function_not_allowed')


class ProgramFormulaVerifier:
    """One bounded block-backed resolver reused for all origins in a document."""
    def __init__(self,blocks,*,source_sha256,profile_id):
        import xml.etree.ElementTree as ET
        self.source_sha256=source_sha256;self.profile_id=profile_id
        sheets={};values={};by_cell={}
        for block in blocks:
            if block.get('kind')!='spreadsheet_cell':continue
            key=(block.get('sheet'),block.get('cell'))
            if key in by_cell:_fail('formula_duplicate_source_cell')
            by_cell[key]=block;cell=ET.Element(NS+'c',{'r':key[1]})
            if block.get('xml_type') is not None:cell.set('t',block['xml_type'])
            formula=block.get('formula_source')
            if 'formula' in block:
                if not isinstance(formula,dict):_fail('formula_source_declaration_missing')
                f=ET.SubElement(cell,NS+'f',formula.get('attributes',{}));f.text=formula.get('text')
                value=block.get('cached_value')
                present=block.get('cached_value_present',value is not None)
                if present:ET.SubElement(cell,NS+'v').text='' if value=='' else 'present'
            else:value=block.get('value')
            values[id(cell)]=value;sheets.setdefault(key[0],{'cells':{}})['cells'][key[1]]=cell
        self.resolver=TemplateFormulaResolver({'sheets':sheets,'strings':[],'defined':dict(DEFINED_NAMES)},
            profile_id=profile_id,read_value=lambda cell,strings:None if cell is None else values[id(cell)],source_sha256=source_sha256)

    def verify(self,origin):
        if not isinstance(origin,dict) or origin.get('value_origin')!='program_formula':_fail('formula_origin_kind_invalid')
        proof=origin.get('computation')
        if (not isinstance(proof,dict) or proof.get('contract')!=CONTRACT or proof.get('profile')!=self.profile_id
                or proof.get('source_sha256')!=self.source_sha256):_fail('formula_proof_contract_invalid')
        root=[origin.get('sheet'),origin.get('cell')]
        if proof.get('root')!=root or type(origin.get('value')) is not type(proof.get('value')) or origin.get('value')!=proof.get('value'):_fail('formula_proof_root_mismatch')
        checked=self.resolver.resolve(*root)
        same_proof=(json.dumps(checked['computation'],sort_keys=True,ensure_ascii=True,allow_nan=False,separators=(',',':'))
            ==json.dumps(proof,sort_keys=True,ensure_ascii=True,allow_nan=False,separators=(',',':')))
        if not checked['available'] or checked['value_origin']!='program_formula' or not same_proof:_fail('formula_proof_replay_mismatch')
        if type(checked['value']) is not type(origin['value']) or checked['value']!=origin['value']:_fail('formula_proof_value_mismatch')
        return checked


def verify_program_formula_origin(origin,blocks,*,source_sha256,profile_id):
    """Replay one computed origin without treating its value as a file cache."""
    return ProgramFormulaVerifier(blocks,source_sha256=source_sha256,profile_id=profile_id).verify(origin)
