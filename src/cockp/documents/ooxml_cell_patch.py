"""Bounded OOXML ZIP surgery: opaque entries are copied, never reserialized."""
from __future__ import annotations

import binascii
import io
import math
import re
import struct
import zipfile
import zlib
from xml.sax.saxutils import escape

from defusedxml import ElementTree as SafeET


class TemplateError(ValueError):
    """Content-free diagnostics, safe for a user-facing adapter boundary."""


def require(value, code):
    if not value:
        raise TemplateError(code)


def package(raw: bytes):
    require(type(raw) is bytes and 0 < len(raw) <= 20 * 1024 * 1024, "template_size_invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist()
        require(len(entries) <= 2048 and len({e.filename for e in entries}) == len(entries), "template_zip_entries_invalid")
        require(sum(e.file_size for e in entries) <= 40 * 1024 * 1024, "template_zip_expansion_limit")
        for entry in entries:
            name = entry.filename
            require(not (name.startswith('/') or '\\' in name or ':' in name or '..' in name.split('/')),
                    "template_zip_path_invalid")
            require(not entry.flag_bits & 1 and entry.compress_type in (0, 8), "template_zip_encoding_unsupported")
            require((entry.external_attr >> 16) & 0o170000 != 0o120000, "template_zip_link_forbidden")
            require(entry.file_size <= 10 * 1024 * 1024 and entry.file_size <= max(1, entry.compress_size) * 200,
                    "template_zip_part_limit")
            require(not name.lower().startswith('_xmlsignatures/'), "template_digital_signature_cannot_be_preserved")
        return archive
    except (zipfile.BadZipFile, OSError):
        raise TemplateError("template_zip_invalid") from None


_CELL = re.compile(rb'<(?P<tag>(?:[A-Za-z_][\w.-]*:)?c)\b(?P<attrs>[^<>]*?)(?:\s*/>|>.*?</(?P=tag)\s*>)', re.S)
_REF = re.compile(rb'\br\s*=\s*([\'"])([A-Z]{1,3}[1-9][0-9]{0,6})\1')


def patch_cells(xml: bytes, replacements: dict[str, object], *, clear_formula_caches=False):
    """Keep every byte outside changed c elements, including extension markup."""
    require(b'<![CDATA[' not in xml and b'<!--' not in xml, "template_patch_xml_shape_unsupported")
    try:
        parsed = SafeET.fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception:
        raise TemplateError("template_patch_xml_invalid") from None
    matches = list(_CELL.finditer(xml))
    parsed_cells = [e for e in parsed.iter() if e.tag == '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c']
    require(len(matches) == len(parsed_cells),
            "template_patch_cell_spans_unsupported")
    by_address = {e.attrib.get('r'):e for e in parsed_cells}
    require(len(by_address) == len(parsed_cells), "template_cell_address_duplicate")
    found = set(); changes = []; output = []; cursor = 0
    for match in matches:
        ref = _REF.search(match['attrs'])
        require(ref is not None, "template_cell_address_missing")
        coordinate = ref[2].decode('ascii')
        require(coordinate not in found, "template_cell_address_duplicate")
        found.add(coordinate)
        old = match.group(); new = old
        tag = match['tag']; prefix = tag[:-1]
        has_formula = re.search(rb'<' + re.escape(prefix) + rb'f(?:\s|>|/)', old) is not None
        if coordinate in replacements or (clear_formula_caches and has_formula):
            # Our byte surgery recognizes only direct cell value children.
            # Extension descendants may legally use the same local element
            # names: reject that uncommon shape rather than damage its bytes.
            element = by_address.get(coordinate)
            require(element is not None, "template_patch_cell_spans_unsupported")
            direct = {id(child) for child in element}
            require(not any(node is not element and id(node) not in direct and node.tag.rsplit('}',1)[-1] in {'v','is'}
                            for node in element.iter()), "template_nested_extension_value_unsupported")
        if coordinate in replacements:
            require(not has_formula, "template_formula_cell_not_editable")
            value = replacements[coordinate]
            require(value is None or type(value) in (str, int, bool, float), "template_value_type_invalid")
            require(type(value) is not float or math.isfinite(value), "template_number_not_finite")
            attrs = re.sub(rb'\s+t\s*=\s*([\'"]).*?\1', b'', match['attrs']).rstrip()
            if type(value) is str:
                require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value), "template_text_control_character")
                body = b'<' + prefix + b'is><' + prefix + b't xml:space="preserve">' + escape(value).encode() + b'</' + prefix + b't></' + prefix + b'is>'
                attrs += b' t="inlineStr"'
            elif value is None:
                body = b''
            else:
                body = b'<' + prefix + b'v>' + (str(int(value)) if type(value) is bool else str(value)).encode() + b'</' + prefix + b'v>'
                if type(value) is bool:
                    attrs += b' t="b"'
            # Only existing value/inline-string children may be replaced. Preserve
            # extension children and attributes, styles, comments and namespace data.
            inner = b'' if old.rstrip().endswith(b'/>') else old[old.find(b'>')+1:old.rfind(b'</')]
            inner = re.sub(rb'<' + re.escape(prefix) + rb'(v|is)\b[^>]*(?:/>|>.*?</' + re.escape(prefix) + rb'\1\s*>)', b'', inner, flags=re.S)
            new = b'<' + tag + attrs + b'>' + body + inner + b'</' + tag + b'>'
        elif clear_formula_caches and has_formula:
            new = re.sub(rb'<' + re.escape(prefix) + rb'v\b[^>]*(?:/>|>.*?</' + re.escape(prefix) + rb'v\s*>)', b'', old, flags=re.S)
        if new != old:
            output.extend((xml[cursor:match.start()], new)); cursor = match.end()
            changes.append({'cell': coordinate, 'kind': 'input' if coordinate in replacements else 'formula_cache_invalidated'})
    require(set(replacements) <= found, "template_target_input_cell_missing")
    output.append(xml[cursor:])
    result = b''.join(output)
    try:
        SafeET.fromstring(result, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception:
        raise TemplateError("template_patch_xml_verification_failed") from None
    return result, changes


def mark_recalculation(xml: bytes):
    pattern = rb'<(?P<tag>(?:[A-Za-z_][\w.-]*:)?calcPr)\b[^>]*(?:/>|>.*?</(?P=tag)\s*>)'
    matches = list(re.finditer(pattern, xml, re.S))
    require(len(matches) == 1, "template_calculation_properties_missing")
    match = matches[0]; old = match.group()
    require(old.rstrip().endswith(b'/>'), "template_calculation_properties_unsupported")
    old = re.sub(rb'\s+(?:fullCalcOnLoad|forceFullCalc|calcMode)\s*=\s*([\'"]).*?\1', b'', old)
    new = old[:-2].rstrip() + b' fullCalcOnLoad="1" forceFullCalc="1" calcMode="auto"/>'
    return xml[:match.start()] + new + xml[match.end():]


def patch_zip(raw: bytes, changed: dict[str, bytes]) -> bytes:
    """Copy raw local records and central metadata; patch CRC/sizes/offsets only.

    Finite classic ZIP is deliberately required. ZIP64/multidisk/ambiguous layouts
    fail closed instead of silently rewriting unsupported container features.
    """
    with package(raw) as archive:
        entries = archive.infolist(); names = {e.filename for e in entries}
        require(set(changed) <= names, "template_patch_part_missing")
        end = raw.rfind(b'PK\x05\x06', max(0, len(raw)-65557))
        require(end >= 0 and end+22 <= len(raw), "template_zip_end_missing")
        disk, cd_disk, disk_count, count, cd_size, cd_offset, comment_size = struct.unpack_from('<4H2IH', raw, end+4)
        require(disk == cd_disk == 0 and disk_count == count == len(entries) and count < 65535
                and cd_offset+cd_size == end and end+22+comment_size == len(raw), "template_zip_layout_unsupported")
        local_order = sorted(entries, key=lambda item:item.header_offset)
        require(local_order and local_order[0].header_offset == 0, "template_zip_prefix_unsupported")
        output = bytearray(); offsets = {}; sizes = {}
        for index, entry in enumerate(local_order):
            start = entry.header_offset
            stop = local_order[index+1].header_offset if index+1 < len(local_order) else cd_offset
            segment = raw[start:stop]; offsets[entry.filename] = len(output)
            require(segment[:4] == b'PK\x03\x04' and len(segment) >= 30, "template_zip_local_header_invalid")
            if entry.filename not in changed:
                output.extend(segment); continue
            _, version, flags, method, _, _, _, _, _, name_length, extra_length = struct.unpack_from('<I5H3I2H',segment)
            require(version < 45 and flags == entry.flag_bits and method == entry.compress_type, "template_zip64_unsupported")
            body_start = 30 + name_length + extra_length
            old_end = body_start + entry.compress_size
            require(old_end <= len(segment), "template_zip_local_size_invalid")
            content = changed[entry.filename]; crc = binascii.crc32(content) & 0xffffffff
            if method == 8:
                compressor = zlib.compressobj(wbits=-15); compressed = compressor.compress(content) + compressor.flush()
            else:
                compressed = content
            require(max(len(content),len(compressed)) < 0xffffffff, "template_zip64_unsupported")
            header = bytearray(segment[:body_start]); tail = segment[old_end:]
            if flags & 8:
                prefix = 4 if tail.startswith(b'PK\x07\x08') else 0
                require(len(tail) >= prefix+12, "template_zip_descriptor_invalid")
                tail = tail[:prefix] + struct.pack('<III',crc,len(compressed),len(content)) + tail[prefix+12:]
            else:
                struct.pack_into('<III',header,14,crc,len(compressed),len(content))
            output.extend(header); output.extend(compressed); output.extend(tail)
            sizes[entry.filename] = (crc,len(compressed),len(content))
        new_cd = len(output); cursor = cd_offset
        for entry in entries:
            require(raw[cursor:cursor+4] == b'PK\x01\x02', "template_zip_central_header_invalid")
            lengths = struct.unpack_from('<3H',raw,cursor+28); size = 46 + sum(lengths)
            record = bytearray(raw[cursor:cursor+size])
            require(len(record) == size and struct.unpack_from('<I',record,42)[0] == entry.header_offset, "template_zip_central_order_invalid")
            struct.pack_into('<I',record,42,offsets[entry.filename])
            if entry.filename in sizes:
                struct.pack_into('<III',record,16,*sizes[entry.filename])
            output.extend(record); cursor += size
        require(cursor == end, "template_zip_central_trailing_data")
        ending = bytearray(raw[end:]); struct.pack_into('<II',ending,12,len(output)-new_cd,new_cd); output.extend(ending)
    result = bytes(output)
    with package(result) as check:
        require(check.testzip() is None, "template_patch_crc_invalid")
        for entry in check.infolist():
            expected = changed.get(entry.filename)
            if expected is not None:
                require(check.read(entry) == expected, "template_patch_verification_failed")
    return result
