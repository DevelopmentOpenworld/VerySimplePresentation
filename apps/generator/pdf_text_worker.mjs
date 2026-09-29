// Item 34 of the plan, owner decision 37 (27.09.2026): the text of the PDF materials of a content package, read by pdf.js
// (Mozilla, Apache-2.0; the version pinned in package.json, the legacy build meant for Node). Input on stdin:
// {"files":[{"data":<base64>}],"max_pages":N}; output: per file its text items page by page (string, position of the
// baseline, size, width, end of line), or the reason it was not read. Nothing is drawn, no font is loaded, no script of the
// PDF runs, nothing is fetched. Paragraphs are built from the items by packages/core/vsp_core/materials.py.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
// pdf.js writes its warnings with console.log; stdout carries only the JSON answer.
console.log=console.info=console.warn=(...args)=>process.stderr.write(args.join(' ')+'\n');
const here=path.dirname(fileURLToPath(import.meta.url));
const pkg=JSON.parse(await fs.readFile(path.join(here,'package.json'),'utf8'));
const base=process.env.VSP_NODE_MODULES;
const root=base?path.join(base,'pdfjs-dist'):path.join(here,'node_modules/pdfjs-dist');
const installed=JSON.parse(await fs.readFile(path.join(root,'package.json'),'utf8'));
if(installed.version!==pkg.dependencies['pdfjs-dist'])throw Error('pdf.js version does not match the registered package.json');
const pdfjs=await import(pathToFileURL(path.join(root,'legacy/build/pdf.mjs')));
pdfjs.GlobalWorkerOptions.workerSrc=pathToFileURL(path.join(root,'legacy/build/pdf.worker.mjs')).href;
process.stdin.setEncoding('utf8');
let input='';for await(const chunk of process.stdin)input+=chunk;
const {files,max_pages:maxPages}=JSON.parse(input);
const round=v=>Math.round(v*100)/100;
// pdf.js reads its tables with fs from "<folder>/<name>" and wants the folder to end with "/", on Windows too.
const folder=name=>path.join(root,name).split(path.sep).join('/')+'/';
const outputs=[];
for(const file of files){
  let doc=null;
  try{
    doc=await pdfjs.getDocument({data:new Uint8Array(Buffer.from(file.data,'base64')),isEvalSupported:false,disableFontFace:true,
      useSystemFonts:false,enableXfa:false,verbosity:0,cMapUrl:folder('cmaps'),cMapPacked:true,
      standardFontDataUrl:folder('standard_fonts')}).promise;
    const pages=[],count=Math.min(doc.numPages,maxPages);
    for(let n=1;n<=count;n++){
      const page=await doc.getPage(n),view=page.getViewport({scale:1}),content=await page.getTextContent();
      pages.push({n,width:round(view.width),height:round(view.height),items:content.items.filter(i=>typeof i.str==='string').map(i=>({
        s:i.str,x:round(i.transform[4]),y:round(i.transform[5]),h:round(Math.hypot(i.transform[2],i.transform[3])||i.height),w:round(i.width),eol:!!i.hasEOL}))});
      page.cleanup();
    }
    outputs.push({pages:doc.numPages,read:count,page_list:pages});
  }catch(error){
    outputs.push({error:error?.name==='PasswordException'?'encrypted':'invalid',message:String(error?.message||error).slice(0,300)});
  }finally{
    if(doc)await doc.destroy();
  }
}
process.stdout.write(JSON.stringify({schema:'vsp.pdf-text/1',runtime:{node:process.version,pdfjs:installed.version},outputs}));
