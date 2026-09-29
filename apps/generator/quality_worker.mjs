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
// Without an encoding each chunk is decoded alone, and a UTF-8 sign split between two chunks becomes U+FFFD.
process.stdin.setEncoding('utf8');
let input='';for await(const chunk of process.stdin)input+=chunk;
const payload=JSON.parse(input);
const browser=await chromium.launch({headless:true});
const result={schema:'vsp.render-quality/1',runtime:{node:process.version,playwright:installed.version,chromium:browser.version()},outputs:[]};
try{
  for(const doc of payload.documents){
    const page=await browser.newPage({viewport:{width:Math.ceil(doc.width+40),height:Math.ceil(doc.height+40)}});
    await page.route('**/*',route=>route.abort());
    await page.setContent('<!doctype html><meta charset="utf-8"><style>body{margin:0}.slide{margin:0}</style><main id="quality-host"></main>');
    await page.addScriptTag({content:await fs.readFile(path.join(here,'renderer.js'),'utf8')});
    await page.addScriptTag({content:await fs.readFile(path.join(here,'quality.js'),'utf8')});
    result.outputs.push(await page.evaluate(async p=>checkDocumentQuality(p.doc,p.options),{doc,options:payload.options||{}}));
    await page.close();
  }
}finally{await browser.close();}
process.stdout.write(JSON.stringify(result));
