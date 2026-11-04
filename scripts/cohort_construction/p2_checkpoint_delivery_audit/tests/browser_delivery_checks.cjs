/* Fictional delivery only. No browser result is a clinical or release decision. */
const fs=require('fs'),path=require('path'),assert=require('assert'),{pathToFileURL}=require('url');
const {chromium}=require('playwright');
const [input,output]=process.argv.slice(2);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.REVIEW_CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1440,height:1100},acceptDownloads:true});
 const errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url());});
 try{
  await page.goto(pathToFileURL(path.resolve(input)).href);
  await page.locator('#main').waitFor({state:'visible'});
  assert.equal(await page.locator('input:checked').count(),0,'No prefilled expert answers');
  const serialized=await page.content();
  const text=await page.locator('body').innerText();
  const hiddenDecision=!serialized.includes('当前决定：先行血管重建评估。')&&!serialized.includes('决定：暂缓切除并补充病理证据。')&&!serialized.includes('决定乙。')&&!serialized.includes('SYNTHETIC_HIDDEN_DECISION_AND_OUTCOME_43fb93');
  assert(hiddenDecision,'No target/current/future decision in actual HTML DOM');
  assert(!text.includes('决定乙。'),'No hidden fixture decision in visible body');
  const assigned=await page.evaluate(()=>JSON.parse(document.getElementById('assigned-data').textContent));
  const assignment=assigned.assignment_id;
  const basic=assigned.bundle.entry[0].resource;
  const checkpoint=basic.extension.find(e=>e.url.endsWith('/packet-binding')).extension.find(e=>e.url==='checkpoint-id').valueString;
  const questionnaire=assigned.bundle.entry.find(e=>e.resource.resourceType==='Questionnaire').resource;
  await page.screenshot({path:path.join(output,'delivery-desktop.png'),fullPage:true});
  const flatten=items=>items.flatMap(item=>[item,...flatten(item.item||[])]);
  for(const item of flatten(questionnaire.item).filter(item=>item.required)){
   if(item.type==='choice'){
    const option=item.answerOption[0],value=option.valueCoding||option.valueString;
    const label=typeof value==='string'?value:(value.display||value.code);
    await page.getByLabel(label,{exact:true}).check();
   }else if(item.type==='boolean')await page.getByLabel('是',{exact:true}).check();
  }
  const download=page.waitForEvent('download');
  await page.locator('#export-button').click();
  const file=await download;await file.saveAs(path.join(output,'browser-submission.json'));
  assert(!await page.locator('#answer-error').isVisible());
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),'No mobile overflow');
  await page.screenshot({path:path.join(output,'delivery-mobile.png'),fullPage:true});
  assert.equal(requests.length,0);assert.equal(errors.length,0);
  fs.writeFileSync(path.join(output,'browser-receipt.json'),JSON.stringify({status:'PASS',synthetic_only:true,
   browser_version:browser.version(),checkpoint_id:checkpoint,assignment_id:assignment,
   visible_masked_choice_absent:hiddenDecision,hidden_decision_absent_from_dom:hiddenDecision,
   actual_download:true,python_authoritative_replay_required:true,network_requests:requests.length,page_errors:errors},null,2)+'\n');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1);});
