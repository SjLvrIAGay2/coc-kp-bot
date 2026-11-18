"""Render configuration text locally as one PNG; no image provider or network.

The caller determines visibility and guards the complete source text before
rendering. A hash-only filename and immutable cache keep retries reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


class ConfigImageError(RuntimeError):
    pass


WIDTH = 1800
MAX_HEIGHT = 12000
MAX_CHARS = 30000
VERSION = "config-image-v3"
MAX_FILE_BYTES = 12 * 1024 * 1024
_RENDER_LOCK = threading.Lock()
_SETTING = re.compile(r"^\d+\s+\[[^\]]+\]")
_TOKENS = re.compile(r"\{[^{}\s]+\}|[A-Za-z0-9_]+")
_UI_TEXT = "COC7 · 群配置目录按编号查看设置从左列向右列阅读当前值说明"


def _font_candidates():
    configured = os.getenv("COCKP_CONFIG_FONT")
    if configured:
        return [Path(configured)]
    return [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    ]


def _supports_text(font, text):
    missing = font.getmask("\U0010ffff")
    missing_glyph = (missing.size, bytes(missing))
    for char in set(text):
        if char.isspace():
            continue
        mask = font.getmask(char)
        if (mask.size, bytes(mask)) == missing_glyph:
            return False
    return True


def _load_fonts(text):
    for path in _font_candidates():
        if not path.is_file():
            continue
        try:
            regular = ImageFont.truetype(str(path), 26)
            if not _supports_text(regular, text + _UI_TEXT):
                continue
            bold_path = path.with_name("msyhbd.ttc") if path.name.lower() == "msyh.ttc" else path
            bold = ImageFont.truetype(str(bold_path if bold_path.is_file() else path), 27)
            if not _supports_text(bold, text + _UI_TEXT):
                bold = regular
            return regular, bold
        except (OSError, ValueError):
            continue
    raise ConfigImageError("配置图片缺少支持全部文字的中文字体，请安装中文字体或设置 COCKP_CONFIG_FONT。")


def _ink_width(font, text):
    box = font.getbbox(text, anchor="lt")
    return max(float(font.getlength(text)), box[2] - min(0, box[0]))


def _wrap_line(line, font, width):
    if not line:
        return [""]
    result, start = [], 0
    while start < len(line):
        low, high = 1, len(line) - start
        best = 0
        while low <= high:
            count = (low + high) // 2
            if _ink_width(font, line[start:start + count]) <= width:
                best, low = count, count + 1
            else:
                high = count - 1
        if not best:
            raise ConfigImageError("配置图片文字无法在画布内排版。")
        # Move a complete identifier/placeholder to the next line if it fits
        # there. Only tokens wider than a whole line may be split.
        end = start + best
        if end < len(line):
            for token in _TOKENS.finditer(line):
                if start < token.start() < end < token.end() and _ink_width(font, token.group()) <= width:
                    best = token.start() - start
                    break
        # Prefer a nearby whitespace boundary without discarding characters.
        candidate = line[start:start + best]
        space = candidate.rfind(" ")
        if start + best < len(line) and space >= best * .75:
            best = space + 1
        result.append(line[start:start + best])
        start += best
    return result


@dataclass
class _Block:
    lines: list[tuple[str, str, int]]
    height: int
    setting: bool


def _columns(blocks, count=2):
    columns, remaining = [], list(blocks)
    for index in range(count):
        if not remaining:
            columns.append([])
            continue
        if index == count - 1:
            columns.append(remaining)
            break
        target = sum(block.height for block in remaining) / (count - index)
        current, height = [], 0
        while remaining:
            next_block = remaining[0]
            if current and abs(height - target) <= abs(height + next_block.height - target):
                break
            current.append(remaining.pop(0))
            height += next_block.height
        columns.append(current)
    return columns


def _setting_fields(source_lines):
    """Keep fields separate; retain multiline values and every continuation."""
    if "=" not in source_lines[0]:
        fields = [(source_lines[0], "title")]
        style = "value"
        for line in source_lines[1:]:
            if line.startswith("说明："):
                style = "description"
            fields.append((line, style))
        return fields
    header, _, body = "\n".join(source_lines).partition("=")
    body = body[1:] if body.startswith(" ") else body
    # Registered descriptions disambiguate semicolons inside configured values.
    from cockp.integrations.group_config import REGISTRY
    item = REGISTRY.get(int(source_lines[0].split()[0]))
    description = "仅运维可在部署端修改" if item and item.readonly else item.description if item else ""
    suffix = "；" + description
    if description and body.endswith(suffix):
        value = body[:-len(suffix)]
    else:
        value, separator, description = body.partition("；")
        if not separator:
            description = ""
    fields = [(header.rstrip(), "title"), ("当前值：" + value, "value")]
    if description:
        fields.append(("说明：" + description, "description"))
    return fields


def _draw_fitted_line(draw, left, top, text, font, color, right, bottom):
    box = font.getbbox(text, anchor="lt")
    x, y = left - min(0, box[0]), top - box[1]
    actual = draw.textbbox((x, y), text, font=font, anchor="lt")
    if actual[0] < left or actual[1] < top or actual[2] > right or actual[3] > bottom:
        raise ConfigImageError("配置图片文字超出可读区域，已停止生成。")
    draw.text((x, y), text, font=font, fill=color, anchor="lt")


def _make_image(text):
    regular, bold = _load_fonts(text)
    fonts = {"title": bold, "value": regular, "description": regular.font_variant(size=25)}
    colors = {"title": "#183d60", "value": "#263b50", "description": "#506274"}
    margin, gap, padding, line_height = 44, 24, 22, 38
    column_width = (WIDTH - 2 * margin - gap) // 2
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    first_setting = next((index for index, line in enumerate(lines) if _SETTING.match(line)), len(lines))
    prelude = [wrapped for line in lines[:first_setting]
               for wrapped in _wrap_line(line, regular, WIDTH - 2 * margin - 2 * padding)]
    source_blocks = []
    for line in lines[first_setting:]:
        if _SETTING.match(line):
            source_blocks.append([line])
        elif source_blocks:
            source_blocks[-1].append(line)
    blocks = []
    for source_lines in source_blocks:
        fields = _setting_fields(source_lines)
        wrapped_fields, previous_style = [], None
        for field, style in fields:
            for raw_line in field.split("\n"):
                for line in _wrap_line(raw_line, fonts[style], column_width - 2 * padding):
                    height = max(line_height, fonts[style].getbbox(line, anchor="lt")[3] + 10)
                    spacing = 8 if previous_style is not None and previous_style != style else 0
                    wrapped_fields.append((line, style, height + spacing))
                    previous_style = style
        blocks.append(_Block(wrapped_fields, sum(line[2] for line in wrapped_fields) + 2 * padding + 14, True))
    columns = _columns(blocks)
    prelude_height = len(prelude) * line_height + 2 * padding + gap if prelude else 0
    top = 160 + prelude_height
    height = max(300, top + max((sum(block.height for block in column) for column in columns), default=0) + margin)
    if height > MAX_HEIGHT:
        raise ConfigImageError("配置图片内容过长，无法生成可读的单张图片。")
    image = Image.new("RGB", (WIDTH, height), "#eef2f6")
    draw = ImageDraw.Draw(image)
    _draw_fitted_line(draw, margin, 30, "COC7 · 群配置目录", bold.font_variant(size=43), "#142940", WIDTH - margin, 86)
    _draw_fitted_line(draw, margin, 96, "按编号查看设置  ·  从左列向右列阅读", regular, "#52667d", WIDTH - margin, 132)
    if prelude:
        bottom = 146 + prelude_height - gap
        draw.rounded_rectangle((margin, 146, WIDTH - margin, bottom), radius=16, fill="#ffffff")
        for index, line in enumerate(prelude):
            _draw_fitted_line(draw, margin + padding, 160 + index * line_height, line, regular, "#273b50", WIDTH - margin - padding, bottom)
    for column_index, column in enumerate(columns):
        x, y = margin + column_index * (column_width + gap), top
        for block in column:
            bottom = y + block.height - 14
            draw.rounded_rectangle((x, y, x + column_width, bottom), radius=13, fill="#ffffff")
            draw.rounded_rectangle((x, y, x + 5, bottom), radius=2, fill="#4e87bb")
            position, previous_style = y + padding, None
            for line, style, advance in block.lines:
                if previous_style is not None and previous_style != style:
                    position += 8
                _draw_fitted_line(draw, x + padding, position, line, fonts[style], colors[style], x + column_width - padding, bottom - padding)
                position += advance - (8 if previous_style is not None and previous_style != style else 0)
                previous_style = style
            y += block.height
    return image


def _asset_digest(identity_hash, content_hash):
    return hashlib.sha256(json.dumps([VERSION, identity_hash, content_hash],
                                      separators=(",", ":")).encode("ascii")).hexdigest()


def _existing_image(path, content_hash, *, identity_hash=None, source_chars=None):
    if path.is_symlink():
        raise ConfigImageError("配置图片缓存路径不安全。")
    try:
        if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError()
        with Image.open(path) as image:
            saved_identity = image.info.get("identity_sha256", "")
            if (not re.fullmatch(r"[0-9a-f]{64}", saved_identity)
                    or path.name != "config-" + _asset_digest(saved_identity, content_hash) + ".png"
                    or identity_hash is not None and saved_identity != identity_hash
                    or source_chars is not None and image.info.get("source_chars") != str(source_chars)
                    or image.format != "PNG" or image.mode != "RGB"
                    or image.width != WIDTH or not 300 <= image.height <= MAX_HEIGHT
                    or image.info.get("content_sha256") != content_hash or image.info.get("renderer") != VERSION):
                raise ValueError()
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ConfigImageError("配置图片缓存不可读，原文件已保留。") from None
    return path


def verify_config_image(text: str, path: str | Path, output_dir: str | Path) -> Path:
    """Read-only provenance check; never render or repair an untrusted asset."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
        raise ConfigImageError("配置图片源文字不合法。")
    try:
        declared_root = Path(output_dir).absolute()
        if declared_root.is_symlink() or not declared_root.is_dir():
            raise ValueError()
        # systemd StateDirectory may alias a trusted ancestor through
        # /var/lib/private. Rendering returns this canonical path too. Keep
        # rejecting a linked image directory or asset, and require the asset's
        # literal parent to be the exact resolved service directory.
        root = declared_root.resolve(strict=True)
        if isinstance(path, Path):
            candidate = path
        else:
            parsed = urlsplit(path)
            decoded = unquote(parsed.path)
            if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
                    or "\\" in decoded or any(ord(char) < 32 for char in decoded)
                    or any(part in {".", ".."} for part in decoded.split("/"))):
                raise ValueError()
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded):
                decoded = decoded[1:]
            candidate = Path(decoded)
        if (not candidate.is_absolute() or not root.is_dir()
                or candidate.is_symlink() or candidate.parent.absolute() != root
                or candidate.resolve().parent != root
                or not re.fullmatch(r"config-[0-9a-f]{64}\.png", candidate.name)):
            raise ValueError()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (OSError, ValueError, TypeError):
        raise ConfigImageError("配置图片不属于本服务的配置图片目录。") from None
    return _existing_image(candidate, content_hash, source_chars=len(text))


def render_config_image(text: str, output_dir: str | Path, identity: str) -> Path:
    """Return an immutable, hash-named local PNG or raise a safe explicit error."""
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS
            or not isinstance(identity, str) or not identity.strip() or len(identity) > 4096):
        raise ConfigImageError("配置图片输入为空或超出允许长度。")
    try:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise ConfigImageError("配置图片文字编码不合法。") from None
    digest = _asset_digest(identity_hash, content_hash)
    temporary = None
    with _RENDER_LOCK:
        try:
            root = Path(output_dir).resolve()
            root.mkdir(parents=True, exist_ok=True)
            path = root / ("config-" + digest + ".png")
            if path.exists() or path.is_symlink():
                return _existing_image(path, content_hash, identity_hash=identity_hash, source_chars=len(text))
            image = _make_image(text)
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("renderer", VERSION)
            metadata.add_text("content_sha256", content_hash)
            metadata.add_text("identity_sha256", identity_hash)
            metadata.add_text("source_chars", str(len(text)))
            metadata.add_text("columns", "2")
            with tempfile.NamedTemporaryFile(prefix=".config-", suffix=".tmp", dir=root, delete=False) as stream:
                temporary = Path(stream.name)
                image.save(stream, format="PNG", pnginfo=metadata)
                stream.flush()
                os.fsync(stream.fileno())
            # Never replace an existing result when another process completed it.
            if path.exists() or path.is_symlink():
                return _existing_image(path, content_hash, identity_hash=identity_hash, source_chars=len(text))
            temporary.replace(path)
            return path
        except ConfigImageError:
            raise
        except (OSError, ValueError, TypeError):
            raise ConfigImageError("配置图片无法写入本地目录，未发送图片。") from None
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    raise ConfigImageError("配置图片临时文件无法清理。") from None
