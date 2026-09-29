"""Exact-family, offline font assets. Reference-embedded faces always take priority."""
import base64
import hashlib
import json
from pathlib import Path
import struct

ASSETS=Path(__file__).resolve().parents[3]/'assets/fonts'

def sfnt_tables(raw):
    if raw[:4]!=b'\0\1\0\0':raise ValueError('Only static TrueType font assets are supported')
    result={}
    for i in range(struct.unpack_from('>H',raw,4)[0]):
        tag,_,offset,length=struct.unpack_from('>4sIII',raw,12+16*i)
        if offset+length>len(raw):raise ValueError('Truncated font table')
        result[tag.decode('ascii')]=raw[offset:offset+length]
    if 'fvar' in result:raise ValueError('Variable font assets need an explicit static build')
    return result

def font_names(tables):
    data=tables['name'];_,count,offset=struct.unpack_from('>HHH',data)
    names={}
    for i in range(count):
        platform,encoding,language,key,length,start=struct.unpack_from('>6H',data,6+i*12)
        if platform==3 and language==0x409:
            names[key]=data[offset+start:offset+start+length].decode('utf-16-be')
    return names

def wrap_eot(raw):
    """Uncompressed EOT in the header PowerPoint writes; never changes the licensed TrueType bytes.

    Version 0x00020002 with an empty root string, RootStringCheckSum 0x50475342, no signature and no EUDC font, as in the
    Play parts PowerPoint embedded in template A (there MicroType Express compressed, flags 4; here uncompressed, flags 0).
    Owner decision 46 (28.09.2026): the EOT 0x00010000 written before was refused by the Windows font embedding library
    (t2embed TTLoadEmbeddedFont: E_READFROMSTREAMFAILED), so no PPTX of the product carried a usable font; the same bytes in
    this header load, and GDI then draws the family (analysis/style-experiments/20260928-pdf-fix, t2embed_probe.py)."""
    tables=sfnt_tables(raw);os2=tables['OS/2'];names=font_names(tables)
    fs_type=struct.unpack_from('>H',os2,8)[0]
    if fs_type!=0:raise ValueError('Font asset embedding is restricted')
    header=struct.pack('<4I10sBBIHH11IH',0,len(raw),0x20002,0,os2[32:42],1,
        struct.unpack_from('>H',os2,62)[0]&1,struct.unpack_from('>H',os2,4)[0],fs_type,0x504c,
        *struct.unpack_from('>4I',os2,42),*struct.unpack_from('>2I',os2,78),struct.unpack_from('>I',tables['head'],8)[0],0,0,0,0,0)
    # FamilyName, StyleName, VersionName, FullName, each followed by its padding (the last one is Padding5 of version 2.1).
    strings=b''.join(struct.pack('<H',len(v))+v+b'\0\0' for v in (names.get(key,'').encode('utf-16-le') for key in (1,2,5,4)))
    # RootStringSize 0, RootStringCheckSum, EUDCCodePage, Padding6, SignatureSize 0, EUDCFlags, EUDCFontSize 0.
    tail=struct.pack('<HIIHHII',0,0x50475342,0,0,0,0,0)
    result=bytearray(header+strings+tail+raw);struct.pack_into('<I',result,0,len(result))
    return bytes(result)

def resolve_fonts(style,asset_root=ASSETS):
    manifest=json.loads((asset_root/'manifest.json').read_text('utf-8'))
    family=style['font'];existing={(f['family'],f['weight']) for f in style['fonts']}
    # A partial embedded family must not be mixed with a different release.
    if any(f==family for f,_ in existing):return style
    entries=[e for e in manifest['faces'] if e['family']==family]
    if not entries:return style
    for item in entries:
        blobs={}
        for field in ('ttf','eot','license'):
            path=(asset_root/item[field]['file']).resolve()
            if not path.is_relative_to(asset_root.resolve()):raise ValueError('Invalid font asset path')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=item[field]['sha256']:raise ValueError('Font asset hash mismatch: '+item[field]['file'])
            blobs[field]=raw
        tables=sfnt_tables(blobs['ttf']);names=font_names(tables)
        if names[1]!=family or struct.unpack_from('>H',tables['OS/2'],8)[0]!=0:raise ValueError('Font asset identity or embedding mismatch')
        if wrap_eot(blobs['ttf'])!=blobs['eot']:raise ValueError('Embedded font differs from browser font')
        style['fonts'].append({'family':family,'weight':item['weight'],'sha256':item['ttf']['sha256'],
            'data':base64.b64encode(blobs['ttf']).decode(),'eot':base64.b64encode(blobs['eot']).decode(),
            'source_part':'assets/fonts/'+item['ttf']['file'],'origin':'licensed-asset','version':item['version'],
            'license':item['license'],'license_text':blobs['license'].decode('utf-8'),'upstream':item['upstream']})
    style['diagnostics']=[d for d in style['diagnostics'] if d.get('code')!='FONT_AVAILABILITY']
    style['diagnostics'].append({'level':'warning','code':'PINNED_FONT_ASSET','family':family,
        'message':'Гарнитура указана в образце, но ее файл не встроен. Использована закрепленная локальная версия той же гарнитуры; точное совпадение версии шрифта автора не подтверждено.'})
    return style
