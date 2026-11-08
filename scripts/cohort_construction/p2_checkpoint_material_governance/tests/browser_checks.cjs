/* Fictional Chrome rendering of automated opinions plus independent human export. */
const fs=require('fs'),path=require('path'),assert=require('assert'),{pathToFileURL}=require('url');
const {chromium}=require('playwright');
const [input,output,expectedState='AUTO_REVIEW_HOLD']=process.argv.slice(2);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.REVIEW_CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1440,height:1100},acceptDownloads:true});
 const errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url());});
 try{
  await page.goto(pathToFileURL(path.resolve(input)).href);await page.locator('#main').waitFor({state:'visible'});
  if(expectedState==='INVALID'){
   await page.locator('#auto-opinion-error').waitFor({state:'visible'});
   assert((await page.locator('#auto-opinion-error').textContent()).includes('整链核验'));
   assert.equal(await page.locator('#automated-material-opinion').count(),0);
   assert.equal(requests.length,0);assert.equal(errors.length,0);
   fs.writeFileSync(path.join(output,'browser_receipt.json'),JSON.stringify({status:'PASS',synthetic_only:true,network_requests:0,checks:['forged_history_hidden_at_boot']},null,2)+'\n');
   return;
  }
  await page.locator('#automated-material-opinion').waitFor({state:'visible'});
  assert((await page.locator('#auto-opinion-state').textContent()).includes(expectedState));
  const opinion=JSON.parse(await page.locator('#auto-opinion-data').textContent());
  if(opinion.chain.failure&&opinion.chain.failure.stage==='LOCAL_BUDGET_CONTROL'){
   assert.equal(await page.locator('#auto-opinion-rounds li').count(),0);
   assert((await page.locator('#automated-material-opinion').textContent()).includes('EXECUTION_FAILED'));
  }else assert((await page.locator('#automated-material-opinion').textContent()).includes('新旧版本对照'));
  const badPath=path.join(output,'bad-opinions.json');
  if(fs.existsSync(badPath)){
   const counterexamples=JSON.parse(fs.readFileSync(badPath,'utf8'));
   for(const fixture of counterexamples){
    const result=await page.evaluate(async fixture=>{const a=globalThis.MaterialResearcherReview,g=globalThis.MaterialGovernanceReview;const ctx=await a.decodeAssignment(a.parseJsonUnique(document.getElementById('assigned-data').textContent));try{await g.validateOpinion(ctx.assignment,ctx.docs,fixture.opinion);return {accepted:true};}catch(e){return {accepted:false,code:e.message};}},fixture);
    assert.equal(result.accepted,false,'forged history accepted: '+fixture.name);
    assert(result.code.startsWith('CHAIN_')||result.code.startsWith('AUTO_'),result.code);
   }
  }
  const atomicPath=path.join(output,'atomic-counterexamples.json');
  if(fs.existsSync(atomicPath)){
   const fixtures=JSON.parse(fs.readFileSync(atomicPath,'utf8'));
   for(const fixture of fixtures){
    const result=await page.evaluate(async fixture=>{
     const a=globalThis.MaterialResearcherReview,g=globalThis.MaterialGovernanceReview;
     const ctx=await a.decodeAssignment(a.parseJsonUnique(document.getElementById('assigned-data').textContent));
     try{await g.validateOpinion(ctx.assignment,ctx.docs,fixture.opinion);return {accepted:true};}
     catch(e){return {accepted:false,code:e.message};}
    },fixture);
    assert.equal(result.accepted,fixture.accepted,fixture.name+': '+JSON.stringify(result));
    if(!fixture.accepted)assert(result.code.startsWith('CHAIN_')||result.code.startsWith('AUTO_'),result.code);
   }
  }
  assert.equal(await page.locator('#reviewer-id').inputValue(),'');
  assert.equal(await page.locator('#reviewer-type').inputValue(),'');
  assert.equal(await page.locator('#audit-status').inputValue(),'PENDING');
  assert.equal(await page.locator('input:checked').count(),0);
  await page.locator('#reviewer-id').fill('SYNTHETIC-INDEPENDENT-HUMAN');await page.locator('#reviewer-type').selectOption('human');
  await page.locator('#assessed-at').fill('2026-10-29T12:00:00+08:00');await page.locator('#protocol-version').fill('SYNTHETIC-HUMAN-PROTOCOL');
  await page.locator('#segment-rationale').fill('实际虚构审核者独立填写，不接受自动结论替代。');
  const download=page.waitForEvent('download');await page.locator('#export-button').click();await(await download).saveAs(path.join(output,'browser-response.json'));
  const response=JSON.parse(fs.readFileSync(path.join(output,'browser-response.json'),'utf8'));
  assert.equal(response.audit.reviewer_type,'human');assert.equal(response.audit.status,'PENDING');assert.equal(response.audit.independence_attested,false);
  assert.equal(requests.length,0);assert.equal(errors.length,0);
  fs.writeFileSync(path.join(output,'browser_receipt.json'),JSON.stringify({status:'PASS',browser_version:browser.version(),synthetic_only:true,network_requests:0,page_errors:[],checks:['auto_opinion_rendered','version_diff_visible','no_human_prefill','independent_export','zero_http_requests']},null,2)+'\n');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1);});
