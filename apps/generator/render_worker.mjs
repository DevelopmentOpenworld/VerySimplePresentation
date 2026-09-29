// Model in the analysis of the sample (owner decisions 27-28, 26.09.2026): pictures of documents drawn by renderer.js in
// headless Chromium, one PNG per slide, for the scene marker of the preparation stage (scenes.py). The server has no
// PowerPoint; these are the pictures the model sees there. Same runtime as quality_worker.mjs (Node + Playwright of
// apps/generator/package.json). Input on stdin: {"documents":[doc,...]}; output: one report per document with the PNG of
// every slide (base64) and the status of its fonts.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const pkg=JSON.parse(await fs.readFile(path.join(here,'package.json'),'utf8'));
const base=process.env.VSP_NODE_MODULES;
const entry=base?path.join(base,'playwright/index.mjs'):path.join(here,'node_modules/playwright/index.mjs');
const installed=JSON.parse(await fs.readFile(path.join(path.dirname(entry),'package.json'),'utf8'));
if(installed.version!==pkg.dependencies.playwright)throw Error('Playwright version does not match the registered package.json');
const {chromium}=await import(pathToFileURL(entry));
process.stdin.setEncoding('utf8');
let input='';for await(const chunk of process.stdin)input+=chunk;
const payload=JSON.parse(input);
const browser=await chromium.launch({headless:true});
const result={schema:'vsp.render-pictures/1',runtime:{node:process.version,playwright:installed.version,chromium:browser.version()},outputs:[]};
try{
  for(const doc of payload.documents){
    const page=await browser.newPage({viewport:{width:Math.ceil(doc.width),height:Math.ceil(doc.height)}});
    // The document carries its fonts and images as data; nothing may load.
    await page.route('**/*',route=>route.abort());
    await page.setContent('<!doctype html><meta charset="utf-8"><style>body{margin:0}.slide{margin:0}</style><main id="host"></main>');
    await page.addScriptTag({content:await fs.readFile(path.join(here,'renderer.js'),'utf8')});
    const fontStatus=await page.evaluate(async d=>(await renderDocument(d,document.getElementById('host'))).fontStatus,doc);
    const slides=[];
    for(const slide of doc.slides){
      const shot=await page.locator(`.slide[data-slide="${slide.id}"]`).screenshot({type:'png'});
      slides.push({id:slide.id,png:shot.toString('base64')});
    }
    result.outputs.push({slides,font_status:fontStatus});
    await page.close();
  }
}finally{await browser.close();}
process.stdout.write(JSON.stringify(result));
