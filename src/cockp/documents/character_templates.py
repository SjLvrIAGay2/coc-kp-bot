"""Bundled character template with integrity checks."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


CHARACTER_TEMPLATE_ID = "coc7-character-v2.0.4"
CHARACTER_TEMPLATE_FILENAME = "COC七版半自动人物卡v2.0.4(通用).xlsx"
CHARACTER_TEMPLATE_SHA256 = "1806309dd5507c2c8bcd79128c9637f6dfb52d5161e3acfd730aaa2390868fc3"
CHARACTER_TEMPLATE_BYTES = 92246
_ASSET = Path(__file__).resolve().parent / "templates" / "coc7-character-v2.0.4.xlsx"


@dataclass(frozen=True)
class CharacterTemplate:
    filename: str
    data: bytes
    sha256: str


def load_character_template(asset_id: str) -> CharacterTemplate:
    """Return the registered workbook without opening or recalculating it."""
    if asset_id != CHARACTER_TEMPLATE_ID:
        raise ValueError("character_template_not_registered")
    if _ASSET.is_symlink() or any(parent.is_symlink() for parent in _ASSET.parents):
        raise ValueError("character_template_symlink_forbidden")
    try:
        if not _ASSET.is_file() or _ASSET.stat().st_size != CHARACTER_TEMPLATE_BYTES:
            raise ValueError("character_template_integrity_failed")
        data = _ASSET.read_bytes()
    except OSError:
        raise ValueError("character_template_unavailable") from None
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != CHARACTER_TEMPLATE_BYTES or digest != CHARACTER_TEMPLATE_SHA256:
        raise ValueError("character_template_integrity_failed")
    return CharacterTemplate(CHARACTER_TEMPLATE_FILENAME, data, digest)
