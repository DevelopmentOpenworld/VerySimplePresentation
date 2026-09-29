// H2 (docs/SOLUTION_PLAN_2026-09-21.md): print the self-contained HTML of revisions to PDF in headless Chromium, one page
// per slide of the slide size. Same runtime as quality_worker.mjs (Node + Playwright of apps/generator/package.json).
// Input on stdin: {"jobs":[{"html":<path>,"pdf":<path>}]}; output: one report per job.
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
const {jobs}=JSON.parse(input);
const browser=await chromium.launch({headless:true});
const result={schema:'vsp.pdf-export/1',runtime:{node:process.version,playwright:installed.version,chromium:browser.version()},outputs:[]};
try{
  for(const job of jobs){
    const started=Date.now(),url=pathToFileURL(path.resolve(job.html)).href;
    const page=await browser.newPage();
    // The HTML carries its fonts and images as data URLs; nothing else may load.
    await page.route('**/*',route=>route.request().url()===url?route.continue():route.abort());
    await page.goto(url);
    const info=await page.evaluate(async()=>{const r=await window.ready;return {width:doc.width,height:doc.height,slides:doc.slides.length,fit:r.fit,font_status:r.fontStatus};});
    await page.emulateMedia({media:'print'});
    const pdf=await page.pdf({width:info.width+'px',height:info.height+'px',printBackground:true,preferCSSPageSize:true,margin:{top:'0',right:'0',bottom:'0',left:'0'}});
    await fs.writeFile(job.pdf,pdf);
    await page.close();
    result.outputs.push({html:job.html,pdf:job.pdf,bytes:pdf.length,milliseconds:Date.now()-started,...info});
  }
}finally{await browser.close();}
process.stdout.write(JSON.stringify(result));
