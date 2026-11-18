"""Render only the program-produced, spoiler-free module guidance projection."""
from collections.abc import Mapping

from cockp.documents.module_analysis import MODULE_ANALYSIS_LABELS, MAX_ANALYSIS_COMPONENTS, public_module_analysis
from cockp.runtime.document_preview import markdown_literal


def module_guidance_preview(guidance=None):
    public=public_module_analysis(None)
    kinds={'source_fact':'原作信息','kp_inference':'整理建议','unknown':'未知'}
    if isinstance(guidance,Mapping):
        for field in MODULE_ANALYSIS_LABELS:
            value=guidance.get(field)
            # Do not turn a full private analysis object into display text.
            items=value if isinstance(value,list) else [value]
            if not 1<=len(items)<=MAX_ANALYSIS_COMPONENTS:continue
            if all(isinstance(item,Mapping) and set(item)=={'text','kind'}
                    and isinstance(item['text'],str) and 0<len(item['text'].strip())<=2000
                    and isinstance(item['kind'],str) and item['kind'] in kinds for item in items):
                public[field]=value
    lines=['**模组选团建议**']
    lines.extend('- '+label+'：'+markdown_literal(item['text'])
        +'（'+kinds[item['kind']]+'）'
        for field,label in MODULE_ANALYSIS_LABELS.items() if field in public
        for item in (public[field] if isinstance(public[field],list) else [public[field]]))
    return '\n\n'.join([lines[0],'\n'.join(lines[1:]),
        '内部资料不向用户展示：剧情真相、隐藏线索、未公开人物关系、事件触发、主持准备、分析理由与原文引据。'])
