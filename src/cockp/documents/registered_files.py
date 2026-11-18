"""Public reference files approved for distribution, separate from imports/secrets."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from cockp.documents.character_templates import (
    CHARACTER_TEMPLATE_ID, CHARACTER_TEMPLATE_FILENAME, CharacterTemplate,
    load_character_template,
)


RegisteredFileId = Literal["coc7-character-v2.0.4", "coc7-core-rules-v1.2.1"]
RULEBOOK_ID = "coc7-core-rules-v1.2.1"
RULEBOOK_FILENAME = "COC7th核心规则书v1.2.1.pdf"
RULEBOOK_SHA256 = "22f5f56b7a0989cbded695d39c7d5eddddd809cfc9d2c47e4cf4c5d7edea6815"
RULEBOOK_BYTES = 7421161


def _rulebook_path() -> Path:
    configured = os.environ.get("COCKP_SHARED_FILES_ROOT")
    database = os.environ.get("COCKP_DATABASE")
    root = Path(configured) if configured else Path(database).resolve().parent / "shared-files" if database else Path.cwd()
    return root / RULEBOOK_FILENAME


def load_registered_file(file_id: str) -> CharacterTemplate:
    if file_id == CHARACTER_TEMPLATE_ID:
        return load_character_template(file_id)
    if file_id != RULEBOOK_ID:
        raise ValueError("reference_file_not_registered")
    path = _rulebook_path()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("reference_file_symlink_forbidden")
    try:
        if not path.is_file() or path.stat().st_size != RULEBOOK_BYTES:
            raise ValueError("reference_file_integrity_failed")
        data = path.read_bytes()
    except OSError:
        raise ValueError("reference_file_unavailable") from None
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != RULEBOOK_BYTES or digest != RULEBOOK_SHA256:
        raise ValueError("reference_file_integrity_failed")
    return CharacterTemplate(RULEBOOK_FILENAME, data, digest)


def file_catalog() -> list[dict]:
    """Only public names/purposes; no filesystem paths or imported player files."""
    return [
        {"id": CHARACTER_TEMPLATE_ID, "name": CHARACTER_TEMPLATE_FILENAME,
         "purpose": "COC7通用Excel车卡表格，原文件含示例初值，供自行填写。"},
        {"id": RULEBOOK_ID, "name": RULEBOOK_FILENAME,
         "purpose": "COC第七版核心规则参考书PDF，用户提供的原文件。"},
    ]


def resolve_file_name(name: str) -> str | None:
    aliases = {"规则书": RULEBOOK_ID, "核心规则书": RULEBOOK_ID, "规则参考书": RULEBOOK_ID,
        "车卡模板": CHARACTER_TEMPLATE_ID, "人物卡模板": CHARACTER_TEMPLATE_ID}
    for item in file_catalog():
        aliases[item["id"]] = item["id"]
        aliases[item["name"]] = item["id"]
    return aliases.get(name.strip())
