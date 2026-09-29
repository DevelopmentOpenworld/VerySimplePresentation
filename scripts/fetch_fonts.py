"""Open fonts of the server, kept outside Git (owner decision 24, plan item 38, 25.09.2026).

The lock profiles/mvp/fonts.json pins google/fonts to one commit and lists every file of a set with its size and Git
blob id, so a download is checked byte for byte against that commit without a second hash list. Font files go to
var/fonts/google/<path of the repository> with the licence texts of their folders; `index` then writes var/fonts/index.json
(vsp_core.font_catalog.build_index), which the importer reads. The service never downloads fonts while it generates.

    fetch_fonts.py lock <set> [--families ofl/carlito ...]   developer: write the file list of a set from the pinned commit
    fetch_fonts.py fetch <set> [--dest var/fonts]            download a set of the lock and check every file
    fetch_fonts.py index [--dest var/fonts]                  describe every font file under a directory

Sets: substitutes (metric-compatible open substitutes of Microsoft fonts, owner answer Р4 (в), about 8 MB); catalog — the
whole catalog without Chinese, Japanese and Korean fonts (about 851 MB), owner decision 34 (27.09.2026: "шрифты туда
ставить"), downloaded on the server into its own volume, never into the image:

    fetch_fonts.py lock catalog --families-from <families.json> --without-cjk   developer: families by their subsets
    fetch_fonts.py fetch catalog --dest /fonts                                  server: once, into the volume of fonts

<families.json>: {folder name: {"subsets": [...]}} of the Google Fonts metadata (the inventory of 25.09.2026:
analysis/style-experiments/20260925-fonts-inventory/gf_families.json).
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'packages/core'))
from vsp_core.font_catalog import LOCK,build_index

RAW='https://raw.githubusercontent.com/{repository}/{commit}/{path}'
TREE='https://api.github.com/repos/{repository}/git/trees/{commit}?recursive=1'


def blob_id(data):
    return hashlib.sha1(b'blob %d\0'%len(data)+data).hexdigest()


def get(url):
    with urllib.request.urlopen(url,timeout=120) as response:
        return response.read()


def save_lock(lock):
    # Bytes, not write_text: a sealed text file keeps LF on Windows (tests/test_release_files.py).
    LOCK.write_bytes((json.dumps(lock,ensure_ascii=False,indent=1)+'\n').encode('utf-8'))


parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['lock','fetch','index'])
parser.add_argument('set',nargs='?')
parser.add_argument('--families',nargs='*',help='lock: folders of the repository (ofl/carlito)')
parser.add_argument('--families-from',help='lock: JSON {folder name: {"subsets": [...]}} of the families of the set')
parser.add_argument('--without-cjk',action='store_true',help='lock with --families-from: leave out Chinese, Japanese, Korean')
parser.add_argument('--dest',default=str(ROOT/'var/fonts'))
args=parser.parse_args()
lock=json.loads(LOCK.read_bytes().decode('utf-8'))
source=lock['source'];dest=Path(args.dest)
CJK={'chinese-simplified','chinese-traditional','chinese-hongkong','japanese','korean'}
if args.command=='lock':
    answer=json.loads(get(TREE.format(**source)))
    if answer.get('truncated'):raise SystemExit('Дерево коммита усечено GitHub: список файлов неполон')
    tree=answer['tree']
    if args.families_from:
        # Folders of the licences of the repository whose name is a family of the list (and, --without-cjk, not CJK).
        known=json.loads(Path(args.families_from).read_bytes().decode('utf-8'))
        wanted={k for k,v in known.items() if not (args.without_cjk and set(v.get('subsets') or [])&CJK)}
        args.families=sorted({'/'.join(e['path'].split('/')[:2]) for e in tree if e['type']=='tree' and e['path'].count('/')==1
                              and e['path'].split('/')[0] in ('ofl','apache','ufl') and e['path'].split('/')[1] in wanted})
    folders=tuple(f.rstrip('/')+'/' for f in args.families)
    files=[{'path':e['path'],'bytes':e['size'],'git_blob':e['sha']} for e in tree
           if e['type']=='blob' and e['path'].startswith(folders) and e['path'].lower().endswith(('.ttf','.otf','.txt'))]
    missing=[f for f in folders if not any(x['path'].startswith(f) for x in files)]
    if missing:raise SystemExit('Нет в коммите: '+', '.join(missing))
    lock['sets'][args.set]={**lock['sets'].get(args.set,{}),'families':list(args.families),'files':files,
        **({'selection':'families of '+Path(args.families_from).name+(' without CJK' if args.without_cjk else '')} if args.families_from else {}),
        'bytes':sum(f['bytes'] for f in files)}
    save_lock(lock)
    print(args.set,len(files),'files',round(lock['sets'][args.set]['bytes']/2**20,2),'MB')
elif args.command=='fetch':
    done=0
    for item in lock['sets'][args.set]['files']:
        target=dest/'google'/item['path']
        if target.is_file() and blob_id(target.read_bytes())==item['git_blob']:
            continue
        data=get(RAW.format(**source,path=item['path']))
        if len(data)!=item['bytes'] or blob_id(data)!=item['git_blob']:
            raise SystemExit(f"{item['path']}: не совпадает с коммитом {source['commit']}")
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);done+=1
    index=build_index(dest)
    print(args.set,'downloaded',done,'files; index:',len(index['faces']),'faces,',len(index['skipped']),'skipped')
else:
    index=build_index(dest)
    print('index:',len(index['faces']),'faces,',len(index['skipped']),'skipped')
