"""Decode a font PowerPoint embeds (EOT) into TrueType or OpenType, the same way on every platform (plan item 38).

Compressed data (MicroType Express, EOT flag 4) is unpacked by mtx.py; before 25.09.2026 this used Windows' private
embedding library, and a Linux server read no embedded font at all. Subset fonts (flag 1) are read too: they hold only the
glyphs of the original presentation, so font_catalog records them and the pipeline reports the characters of the new text
they lack. Decoded fonts are kept by the SHA-256 of their part in var/generator/cache/fonts: the same template decodes once.
"""
import hashlib
import os
from pathlib import Path
import struct

from .mtx import decompress

CACHE=Path(__file__).resolve().parents[3]/'var/generator/cache/fonts'


def eot_facts(raw):
    """(data, flags, fsType) of an EOT, or None when it is not one."""
    if len(raw)<82 or len(raw)>32*1024**2:
        return None
    total,length,version,flags=struct.unpack_from('<IIII',raw)
    if total!=len(raw) or not 0<length<=len(raw)-82:
        return None
    return raw[-length:],flags,struct.unpack_from('<H',raw,32)[0]


def decode_eot(raw, family, weight=None):
    """sfnt bytes of an embedded font whose embedding the template allows, or None.

    fsType: 0 installable, 8 editable embedding (owner answer Р2 (а), 25.09.2026: the font is loaded only to draw the
    document it came with and its derivatives, never installed or published). Restricted (2), preview and print only (4)
    and bitmap only (0x200) stay refused. The family must be the one the template names (any name of the font)."""
    facts=eot_facts(raw)
    if facts is None:
        return None
    data,flags,fs_type=facts
    if fs_type&0x0206:
        return None
    if flags&0x10000000:
        data=bytes(b^0x50 for b in data)
    key=hashlib.sha256(raw).hexdigest()
    cached=CACHE/(key+'.ttf')
    if cached.is_file():
        sfnt=cached.read_bytes()
    else:
        try:
            sfnt=decompress(data) if flags&4 else data
        except (ValueError,IndexError,struct.error):
            return None
        if sfnt[:4] not in (b'\0\1\0\0',b'OTTO'):
            return None
        try:
            CACHE.mkdir(parents=True,exist_ok=True)
            part=cached.with_suffix('.part');part.write_bytes(sfnt);os.replace(part,cached)
        except OSError:
            pass
    from .font_catalog import describe
    faces=describe(sfnt)
    from .font_catalog import key
    if not faces or key(family) not in {key(a) for a in faces[0]['aliases']}:
        return None
    return sfnt
