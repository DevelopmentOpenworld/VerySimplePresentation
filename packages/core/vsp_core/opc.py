"""Own OPC (Open Packaging Conventions) layer for PPTX; moved from spikes/template_carrier/opc.py (21.09.2026). Standard library only.

The package is kept as a dictionary of parts. Parts we do not understand
(EMF decor, gradients, SmartArt of layouts, embedded fonts, themes) are never
parsed or re-serialized: they travel to the output byte for byte. XML we must
change is edited with narrow text surgery instead of a parse/serialize round
trip, because ElementTree drops namespace declarations that `mc:Ignorable`
still refers to, and PowerPoint then asks to repair the file.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

MAX_PACKAGE_BYTES = 60 * 1024**2
MAX_UNPACKED_BYTES = 250 * 1024**2
MAX_PARTS = 8000
STORED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "mp4", "m4a", "mp3", "fntdata"}


def parse_xml(data: bytes) -> ET.Element:
    """Parse untrusted XML. DTDs and entities are rejected, as in the importer."""
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DTD и сущности XML не поддерживаются")
    return ET.fromstring(data)


@dataclass
class Rel:
    id: str
    type: str
    target: str
    external: bool = False

    @property
    def kind(self) -> str:
        return self.type.rsplit("/", 1)[-1]


class Package:
    def __init__(self, data: bytes):
        if len(data) > MAX_PACKAGE_BYTES:
            raise ValueError("Образец слишком большой")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("Файл не является PPTX") from None
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_PARTS or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise ValueError("Слишком большой распакованный PPTX")
        self.parts: dict[str, bytes] = {i.filename: archive.read(i) for i in infos}
        self.defaults: dict[str, str] = {}
        self.overrides: dict[str, str] = {}
        if "[Content_Types].xml" not in self.parts:
            raise ValueError("В пакете нет [Content_Types].xml")
        types = parse_xml(self.parts.pop("[Content_Types].xml"))
        for node in types:
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "Default":
                self.defaults[node.get("Extension", "").lower()] = node.get("ContentType", "")
            elif tag == "Override":
                self.overrides[node.get("PartName", "").lstrip("/")] = node.get("ContentType", "")
        self._rels_cache: dict[str, list[Rel]] = {}

    # ---- relationships -------------------------------------------------
    @staticmethod
    def rels_name(part: str) -> str:
        if not part:
            return "_rels/.rels"
        return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")

    def rels(self, part: str) -> list[Rel]:
        if part not in self._rels_cache:
            raw = self.parts.get(self.rels_name(part))
            items = []
            if raw:
                for node in parse_xml(raw):
                    items.append(Rel(node.get("Id"), node.get("Type", ""), node.get("Target", ""),
                                     node.get("TargetMode") == "External"))
            self._rels_cache[part] = items
        return self._rels_cache[part]

    def set_rels(self, part: str, rels: list[Rel]) -> None:
        self._rels_cache[part] = list(rels)
        body = "".join(
            f"<Relationship Id={quoteattr(r.id)} Type={quoteattr(r.type)} Target={quoteattr(r.target)}"
            + (' TargetMode="External"' if r.external else "") + "/>"
            for r in rels)
        xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{REL_NS}">{body}</Relationships>'
        self.parts[self.rels_name(part)] = xml.encode("utf-8")

    @staticmethod
    def resolve(part: str, target: str) -> str:
        if target.startswith("/"):
            return posixpath.normpath(target.lstrip("/"))
        return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))

    @staticmethod
    def relative(from_part: str, to_part: str) -> str:
        return posixpath.relpath(to_part, posixpath.dirname(from_part))

    def related(self, part: str, kind: str) -> list[str]:
        return [self.resolve(part, r.target) for r in self.rels(part) if not r.external and r.kind == kind]

    def next_rel_id(self, part: str) -> str:
        used = {r.id for r in self.rels(part)}
        number = 1 + max([int(m.group(1)) for r in self.rels(part) if (m := re.fullmatch(r"rId(\d+)", r.id))] or [0])
        while f"rId{number}" in used:
            number += 1
        return f"rId{number}"

    # ---- parts ---------------------------------------------------------
    def content_type(self, part: str) -> str | None:
        return self.overrides.get(part) or self.defaults.get(part.rsplit(".", 1)[-1].lower())

    def put(self, part: str, data: bytes | str, content_type: str | None = None) -> None:
        self.parts[part] = data.encode("utf-8") if isinstance(data, str) else data
        if content_type:
            self.overrides[part] = content_type

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        queue = [""]
        while queue:
            part = queue.pop()
            for rel in self.rels(part):
                if rel.external:
                    continue
                target = self.resolve(part, rel.target)
                if target in self.parts and target not in seen:
                    seen.add(target)
                    queue.append(target)
        return seen

    def collect_garbage(self) -> list[str]:
        """Drop parts that no relationship chain from the package root reaches."""
        keep = self.reachable()
        keep |= {self.rels_name(p) for p in keep} | {"_rels/.rels"}
        removed = [p for p in self.parts if p not in keep]
        for part in removed:
            del self.parts[part]
            self.overrides.pop(part, None)
            self._rels_cache.pop(part, None)
        return removed

    def save(self) -> bytes:
        types = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="{CT_NS}">']
        types += [f"<Default Extension={quoteattr(e)} ContentType={quoteattr(c)}/>" for e, c in sorted(self.defaults.items())]
        types += [f"<Override PartName={quoteattr('/' + p)} ContentType={quoteattr(c)}/>"
                  for p, c in sorted(self.overrides.items()) if p in self.parts]
        types.append("</Types>")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(zipfile.ZipInfo("[Content_Types].xml"), "".join(types).encode("utf-8"), zipfile.ZIP_DEFLATED)
            for part in sorted(self.parts):
                stored = part.rsplit(".", 1)[-1].lower() in STORED_EXTENSIONS
                archive.writestr(zipfile.ZipInfo(part), self.parts[part],
                                 zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        return out.getvalue()
