"""Human-readable document labels; IDs remain secondary stable handles."""
import json
import re
from pathlib import Path


def document_label(row, *, public=False, state=None):
    row=dict(row)
    if public and row.get("private"):
        return "私聊提交的资料"
    descriptor=row.get("descriptor",{})
    if isinstance(descriptor,str):
        try:descriptor=json.loads(descriptor)
        except ValueError:descriptor={}
    value=str(descriptor.get("name") or "粘贴资料")
    value=re.split(r"[/\\]",value)[-1]
    value=" ".join("".join(character for character in value if ord(character)>=32 and character not in "\u2028\u2029").split())
    if len(value)>100:
        suffix=Path(value).suffix[:12]
        value=value[:85]+"…"+suffix
    value=value or "未命名资料"
    if public and state is not None:
        from cockp.core.contracts import DomainError, Output
        from cockp.core.security import guard_outputs
        try:guard_outputs([Output(text=value)],state)
        except DomainError:return "该群文件"
    return value


def document_failure(exc):
    """Known safe diagnostics only; never forward a parser/provider body."""
    from cockp.core.contracts import DomainError
    from cockp.integrations.attachment_files import AttachmentError
    if isinstance(exc,DomainError):return str(exc),"validation_failed"
    if isinstance(exc,AttachmentError):
        messages={"attachment_http_failed":"文件下载暂不可用，请稍后重试原任务。",
            "attachment_group_extension_unavailable":"网关未能取得这个群文件的下载地址，请稍后重试原任务。",
            "attachment_transport_failed":"文件下载连接失败，请稍后重试原任务。",
            "attachment_too_large":"文件超过本次导入大小上限，请拆分后提交。",
            "attachment_content_signature_mismatch":"文件内容与格式不符，请将原文另存为TXT或重新导出文档。"}
        code=str(exc)
        return messages.get(code,"文件下载校验未通过，请检查文件格式或重新上传。"),code if code.startswith("attachment_") else "attachment_failed"
    messages={"DOCX包含外部引用，导入器不访问外部资源":"DOCX含外部图片、模板或其他资源引用，暂未导入；请移除这些外部资源或另存为TXT后重试。",
        "DOCX不是有效ZIP文档":"这个文件不是可读取的DOCX，请重新导出或另存为TXT。",
        "DOCX XML格式损坏":"DOCX正文格式损坏，请重新导出或另存为TXT。"}
    if isinstance(exc,ValueError) and str(exc) in messages:return messages[str(exc)],"document_format_rejected"
    return "资料读取或解析失败，请检查格式、模型配置后重试原任务。","unexpected_"+type(exc).__name__
