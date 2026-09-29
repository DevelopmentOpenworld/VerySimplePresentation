"""Local-only prototype server; standard library, no external service calls."""
import argparse
import base64
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'packages/core'))
from vsp_core.pipeline import generate, prepare, revise, fix, write_json, verify_release
from vsp_core.llm import component_versions
from vsp_core.progress import Progress

OUTPUT=ROOT/'var/generator'
LOCK=threading.Lock()
# I4: one optional configuration file (--config, example: config.example.json). Keys: port, output (directory of runs,
# relative to the project), provider (null: default_provider of profiles/llm/models.json; replay; openrouter, whose
# key stays in the environment variable of models.json, never in this file), node and node_modules (the browser workers;
# null: PATH and apps/generator/node_modules); font_dirs (item 38: directories of fonts of the server with an index.json of
# scripts/fetch_fonts.py, such as var/fonts and installed Microsoft core fonts; null: var/fonts).
# host (null: 127.0.0.1; 0.0.0.0 inside a container, the port published by Docker); origins (the addresses the browser
# of the user opens the service at, scheme://host[:port]; null: http://127.0.0.1:<port>). A request whose Host or Origin is
# not one of them is refused: another page or a rebound domain cannot drive the service.
CONFIG_KEYS={'host','port','output','provider','node','node_modules','font_dirs','origins'}
ORIGINS=None
# Owner decision 34 (27.09.2026): the experts enter by a secret link. When the environment variable VSP_ACCESS_KEY holds a
# key (at least 24 signs; never in a file of the project), the service answers only a browser that opened
# <address>/?key=<key> once: that request sets an HttpOnly cookie and sends the browser to the address without the key; every
# other request without the cookie is refused. Without the variable the service is open, as on the machine of a developer.
ACCESS_ENV='VSP_ACCESS_KEY'
ACCESS_COOKIE='vsp_access'
ACCESS_KEY=None


def access_key():
    key=os.environ.get(ACCESS_ENV) or None
    if key is not None and len(key)<24:raise ValueError(f'{ACCESS_ENV}: ключ доступа короче 24 знаков')
    return key


def access_token(key):
    """What the cookie holds: a value derived from the key, so the key of the link is not kept in the browser."""
    return hmac.new(key.encode('utf-8'),b'vsp-access',hashlib.sha256).hexdigest()


def admitted(headers,key):
    """Whether a request carries the cookie of the access key (always when no key is set)."""
    if key is None:return True
    cookie=SimpleCookie()
    try:cookie.load(headers.get('Cookie') or '')
    except Exception:return False
    given=cookie.get(ACCESS_COOKIE)
    return given is not None and hmac.compare_digest(given.value.encode('utf-8'),access_token(key).encode('utf-8'))


def allowed(host,origin,origins):
    """Whether a request with these Host and Origin headers comes from one of the allowed origins."""
    hosts={o.split('://',1)[1].rstrip('/') for o in origins}
    return host in hosts and (not origin or origin.rstrip('/') in {o.rstrip('/') for o in origins})
PROVIDER=None


def load_config(path):
    config=json.loads(Path(path).read_text('utf-8'))
    unknown=set(config)-CONFIG_KEYS-{'$comment'}
    if unknown:raise ValueError('Неизвестные ключи конфигурации: '+', '.join(sorted(unknown)))
    if config.get('provider') not in (None,'replay','openrouter'):raise ValueError('provider: null, replay или openrouter')
    if 'port' in config and not (isinstance(config['port'],int) and 0<config['port']<65536):raise ValueError('port: целое от 1 до 65535')
    return config
RUN=re.compile(r'^\d{8}T\d{6}Z-[a-f0-9]{8}$')
# Owner decision 55 (29.09.2026): the page watches the work it started. It names the work with a number of its own (32 hex
# signs, made in the browser), sends the number with the request and asks /api/progress for the events after the last one
# it has; the answer waits for the next event (a long poll), so the page shows a step when the server begins it. The events
# live in the memory of the process; the last WATCHED works are kept.
WATCH=re.compile(r'^[a-f0-9]{32}$')
WATCHED=40
PROGRESS={}
PROGRESS_LOCK=threading.Lock()


def watched(number,work=None):
    """The events of the work the page named `number`; made when first asked for, by the request itself or by the page."""
    if number is None:return None
    if not isinstance(number,str) or not WATCH.fullmatch(number):raise ValueError('Неверный номер наблюдения')
    with PROGRESS_LOCK:
        found=PROGRESS.get(number)
        if found is None:
            found=PROGRESS[number]=Progress(work)
            for old in sorted(PROGRESS,key=lambda k:PROGRESS[k].created)[:-WATCHED]:del PROGRESS[old]
        elif work and found.work is None:found.work=work
        return found


class Handler(BaseHTTPRequestHandler):
    def respond(self,status,data,kind='application/json; charset=utf-8'):
        if isinstance(data,(dict,list)):
            data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        if isinstance(data,str):
            data=data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(data)

    def refuse_access(self):
        self.respond(403,'<!doctype html><meta charset="utf-8"><title>VerySimplePresentation</title><p>Нужна ссылка доступа к сервису.</p>','text/html; charset=utf-8')

    def do_GET(self):
        parts=urlsplit(self.path);path=unquote(parts.path)
        if ACCESS_KEY is not None:
            given=(parse_qs(parts.query).get('key') or [''])[0]
            if path=='/' and given and hmac.compare_digest(given.encode('utf-8'),ACCESS_KEY.encode('utf-8')):
                # The link of the experts: remember the key in a cookie and drop it from the address bar.
                self.send_response(303)
                self.send_header('Set-Cookie',f'{ACCESS_COOKIE}={access_token(ACCESS_KEY)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000'
                                 +('; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''))
                self.send_header('Location','/')
                self.send_header('Content-Length','0')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                return
            if not admitted(self.headers,ACCESS_KEY):
                return self.refuse_access()
        if path=='/api/demo':
            return self.respond(200,json.loads((ROOT/'examples/brief-pilot.json').read_text('utf-8')))
        if path=='/api/versions':
            # Plan J, read only: the release the server loaded, the registered ones, agents and providers.
            loaded=self.server.loaded_release
            registered=sorted(p.stem for p in (ROOT/'profiles/mvp/releases').glob('structured-mvp-*.json'))
            return self.respond(200,{'release':{'id':loaded['id'],'version':loaded['version'],'files':len(loaded['files'])},
                                     'registered_releases':registered,**component_versions()})
        if path.startswith('/runs/'):
            target=(OUTPUT/path.removeprefix('/runs/')).resolve()
            if not target.is_relative_to(OUTPUT.resolve()) or not target.is_file():
                return self.respond(404,{'error':'Нет файла'})
            return self.respond(200,target.read_bytes(),mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        allowed={'/':'index.html','/app.js':'app.js','/renderer.js':'renderer.js','/progress.js':'progress.js','/style.css':'style.css'}
        if path in allowed:
            target=ROOT/'apps/generator'/allowed[path]
            return self.respond(200,target.read_bytes(),mimetypes.guess_type(target.name)[0] or 'text/plain')
        self.respond(404,{'error':'Нет маршрута'})

    def do_POST(self):
        self.watching=None
        try:
            if not admitted(self.headers,ACCESS_KEY):
                return self.refuse_access()
            # The events of a work are read many times while it runs; they change nothing and call nothing.
            if self.path!='/api/progress' and verify_release()!=self.server.loaded_release:
                raise ValueError('Состав файлов изменился. Перезапустите сервер для нового релиза.')
            origins=ORIGINS or [f'http://127.0.0.1:{self.server.server_port}']
            if not allowed(self.headers.get('Host'),self.headers.get('Origin'),origins):
                return self.respond(403,{'error':'Запрос не с разрешённого адреса сервиса'})
            if not self.headers.get('Content-Type','').startswith('application/json'):
                return self.respond(415,{'error':'Ожидается JSON'})
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=58*1024**2:
                return self.respond(413,{'error':'Слишком большой запрос'})
            payload=json.loads(self.rfile.read(length))
            if self.path=='/api/progress':
                after=payload.get('after',0)
                if not isinstance(after,int) or isinstance(after,bool) or after<0:raise ValueError('Неверный номер события')
                found=watched(payload.get('watch'))
                if found is None:raise ValueError('Неверный номер наблюдения')
                events,closed=found.after(after,timeout=20.0)
                return self.respond(200,{'schema':'vsp.progress/1','work':found.work,'events':events,'closed':closed})
            works={'/api/prepare':'prepare','/api/generate':'generate','/api/fix':'fix'}
            if self.path in works:self.watching=watched(payload.get('watch'),works[self.path])
            if self.path=='/api/prepare':
                # Owner decisions 27-28: the preparation stage of the template the user chose, before any generation.
                template=base64.b64decode(payload['template'],validate=True)
                return self.respond(200,prepare(template,OUTPUT,provider=PROVIDER,template_name=payload.get('template_name'),progress=self.watching))
            if self.path=='/api/generate':
                if not payload.get('template'):raise ValueError('Загрузите образец PPTX')
                template=base64.b64decode(payload['template'],validate=True)
                # Item 34: the content package, files of the user as {name, data (base64)}.
                materials=[(str(m['name']),base64.b64decode(m['data'],validate=True)) for m in payload.get('materials') or []]
                # A generation waits for the one before it: the page is told so, with the time the wait took.
                waiting=time.perf_counter()
                if self.watching is not None and LOCK.locked():self.watching.tell('queue',state='waiting')
                with LOCK:
                    if self.watching is not None:self.watching.tell('queue',state='started',waited=round(time.perf_counter()-waiting,2))
                    result=generate(template,payload['brief'],OUTPUT,provider=PROVIDER,materials=materials or None,
                                    template_name=payload.get('template_name'),progress=self.watching)
                return self.respond(200,result)
            run_id=payload.get('run_id','')
            if not RUN.fullmatch(run_id):
                raise ValueError('Неверный запуск')
            directory=OUTPUT/run_id
            if self.path=='/api/display':
                with LOCK:
                    path=directory/'run.json'
                    run=json.loads(path.read_text('utf-8'))
                    measurement={k:payload[k] for k in ('click_to_display_ms','fit','font_status')}
                    run['client_display_measurements'].append(measurement)
                    write_json(path,run)
                return self.respond(200,{'saved':True})
            if self.path=='/api/revise':
                variant=payload['document']['variant']
                if variant not in ('sequence','split','columns'):
                    raise ValueError('Неизвестный вариант')
                revision=payload['document']['revision']
                if not isinstance(revision,int) or revision<0:
                    raise ValueError('Неизвестная версия')
                with LOCK:
                    original=json.loads((directory/variant/f'r{revision}'/'document.json').read_text('utf-8'))
                    result=revise(directory/variant,original,payload['document'])
                return self.respond(200,result)
            if self.path=='/api/fix':
                # C5: fixers of the findings the user chose; a new revision audited again (pipeline.fix).
                variant=payload['variant']
                if variant not in ('sequence','split','columns'):
                    raise ValueError('Неизвестный вариант')
                revision=payload['revision']
                ids=payload['findings']
                if not isinstance(revision,int) or revision<0 or not isinstance(ids,list) or not all(isinstance(i,str) for i in ids):
                    raise ValueError('Неверный запрос исправлений')
                with LOCK:
                    original=json.loads((directory/variant/f'r{revision}'/'document.json').read_text('utf-8'))
                    result=fix(directory/variant,original,ids,provider=PROVIDER,progress=self.watching)
                return self.respond(200,result)
            self.respond(404,{'error':'Нет маршрута'})
        except (ValueError,KeyError,TypeError,OSError,IndexError) as exc:
            self.refused(exc);self.respond(400,{'error':str(exc)})
        except Exception as exc:
            self.refused(exc);self.respond(500,{'error':f'{type(exc).__name__}: {exc}'})

    def refused(self,exc):
        """A request refused before its work began (a wrong field, a template that is not read) closes what the page watches."""
        found=getattr(self,'watching',None)
        if found is not None and not found.closed:found.failed(exc)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',help='JSON file of settings (config.example.json)')
    parser.add_argument('--port',type=int,help='overrides the port of the configuration; default 8770')
    args=parser.parse_args()
    config=load_config(args.config) if args.config else {}
    args.port=args.port or config.get('port',8770)
    if config.get('output'):OUTPUT=(ROOT/config['output']).resolve()
    PROVIDER=config.get('provider')
    ORIGINS=config.get('origins')
    for key,variable in (('node','VSP_NODE_EXECUTABLE'),('node_modules','VSP_NODE_MODULES')):
        if config.get(key):os.environ[variable]=str(Path(config[key]))
    if config.get('font_dirs'):os.environ['VSP_FONT_DIRS']=os.pathsep.join(str((ROOT/d).resolve()) for d in config['font_dirs'])
    OUTPUT.mkdir(parents=True,exist_ok=True)
    ACCESS_KEY=access_key()
    host=config.get('host') or '127.0.0.1'
    server=ThreadingHTTPServer((host,args.port),Handler)
    server.loaded_release=verify_release()
    print(f'VerySimplePresentation: http://{host}:{args.port}'+(' (вход по секретной ссылке)' if ACCESS_KEY else ''),flush=True)
    server.serve_forever()
