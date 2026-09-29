"""Development commands. Sealing is explicit; the server never reseals itself."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'packages/core'))
from vsp_core.pipeline import generate, write_json
from vsp_core.importer import digest

TEXT_SUFFIXES={'.py','.js','.mjs','.html','.css','.json','.md','.txt'}

def crlf_files(paths):
    return [p for p in paths if p.suffix.lower() in TEXT_SUFFIXES and b'\r' in p.read_bytes()]

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['seal','demo','activate'])
parser.add_argument('release',nargs='?',help='activate: id of a registered release, structured-mvp-<12 hex>')
parser.add_argument('--apply',action='store_true',help='activate: write the files; without it only the plan is printed')
parser.add_argument('--template',help='demo: path of a sample PPTX')
args=parser.parse_args()
if args.command=='seal':
    files=list((ROOT/'packages/core/vsp_core').glob('*.py'))+list((ROOT/'apps/generator').glob('*'))+[ROOT/'profiles/mvp/layouts.json']
    files+=list((ROOT/'assets/fonts').glob('*'))
    # Stage B (plan J): prompts, response schemas and the model and agent profiles are part of the release, so a changed
    # prompt changes the release id. Recorded answers (tests/fixtures/llm) are test data, not part of it.
    files+=list((ROOT/'prompts').glob('*/*.md'))+list((ROOT/'schemas').glob('*.json'))+list((ROOT/'profiles/llm').glob('*.json'))
    # C1: the registry of checks sets the severity and the fixer of every finding.
    files+=[ROOT/'audit/checks.json']
    # Item 38: the lock of the open fonts of the server and their metric-compatible substitutes (the fonts stay outside Git).
    files+=[ROOT/'profiles/mvp/fonts.json']
    files=[p for p in files if p.is_file()]
    crlf=crlf_files(sorted(files))
    if crlf:
        raise ValueError('Файлы с CR в переводах строк: '+', '.join(p.relative_to(ROOT).as_posix() for p in crlf)+'. Git хранит их с LF по .gitattributes, чистый клон не пройдет проверку состава')
    hashes={p.relative_to(ROOT).as_posix():digest(p.read_bytes()) for p in sorted(files)}
    fingerprint=digest(json.dumps(hashes,sort_keys=True).encode())
    manifest={'id':'structured-mvp-'+fingerprint[:12],'version':'0.1.0-dev.'+fingerprint[:12],'schema':'vsp.release/1','files':hashes}
    registry=ROOT/'profiles/mvp/releases'
    registry.mkdir(exist_ok=True)
    frozen=registry/(manifest['id']+'.json')
    if frozen.exists() and json.loads(frozen.read_text('utf-8'))!=manifest:
        raise ValueError('Запрещена подмена зарегистрированного состава')
    if not frozen.exists():
        write_json(frozen,manifest)
    write_json(ROOT/'profiles/mvp/release.json',manifest)
    print('Sealed',len(files),'files')
elif args.command=='activate':
    # Plan J: rollback to a registered release. Its files are the ones of the commit that added its manifest to
    # profiles/mvp/releases/; every file is read from that commit and checked against the SHA-256 of the manifest before
    # anything is written. Without --apply only the plan is printed. Files newer releases added stay on disk: the
    # release checks only its own files. A running server must be restarted (it refuses POST on a changed release).
    if not args.release or not re.fullmatch(r'structured-mvp-[a-f0-9]{12}',args.release):
        raise ValueError('Укажите идентификатор зарегистрированного состава: structured-mvp-<12 hex>')
    record=f'profiles/mvp/releases/{args.release}.json'
    def git(*command):
        return subprocess.run(['git','-C',str(ROOT),*command],capture_output=True,check=True).stdout
    added=git('log','--diff-filter=A','--format=%H','--',record).decode().split()
    if not added:
        raise ValueError(f'Состав {args.release} не зарегистрирован в Git: откат возможен только к закоммиченному составу')
    commit=added[-1]
    manifest=json.loads(git('show',f'{commit}:{record}'))
    changed=[]
    for name,expected in manifest['files'].items():
        data=git('show',f'{commit}:{name}')
        if digest(data)!=expected:
            raise ValueError(f'{name}: в коммите {commit[:12]} содержимое не совпадает с составом {args.release}')
        target=ROOT/name
        if not target.is_file() or target.read_bytes()!=data:
            changed.append((name,data))
    print(json.dumps({'release':args.release,'commit':commit[:12],'files':len(manifest['files']),'changed':[n for n,_ in changed],
                      'applied':args.apply},ensure_ascii=False,indent=1))
    if args.apply:
        for name,data in changed:
            (ROOT/name).parent.mkdir(parents=True,exist_ok=True);(ROOT/name).write_bytes(data)
        write_json(ROOT/'profiles/mvp/release.json',manifest)
else:
    if not args.template:raise ValueError('demo: укажите --template <образец.pptx>')
    result=generate(Path(args.template).read_bytes(),json.loads((ROOT/'examples/brief-pilot.json').read_text('utf-8')),ROOT/'var/generator')
    print(json.dumps({'id':result['run']['id'],'timings':result['run']['timings'],'audits':result['audits'],'diagnostics':result['diagnostics']},ensure_ascii=False,indent=2))
