/* Offline researcher review only. All source/model text enters DOM via textContent. */
(function(G){'use strict';
const FORMAT='checkpoint-researcher-material-assignment-0.1.0',RESPONSE='checkpoint-researcher-material-response-0.1.0';
const CHECKS={segmentation_semantics_reviewed:'逐段核对原文分区含义',permitted_information_scope_reviewed:'核对允许呈现的信息范围',cross_document_restatements_reviewed:'核对同篇与跨文书的目标临床决策回述',required_source_coverage_reviewed:'核对必要来源及覆盖缺口',decision_time_boundary_reviewed:'核对目标临床决策时间证据',all_segments_explicitly_assessed:'确认全部原文区间均已明确评估'};
const clone=x=>JSON.parse(JSON.stringify(x)),lines=s=>s.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
function fail(code){throw new Error(code);}
function nonempty(x){return typeof x==='string'&&x.trim().length>0;}
function string(x,code='MRA_NONEMPTY_STRING_REQUIRED'){if(!nonempty(x))fail(code);}
function object(x){if(!x||typeof x!=='object'||Array.isArray(x))fail('MRA_OBJECT_REQUIRED');}
function exactKeys(x,keys){object(x);if(Object.keys(x).sort().join('|')!==keys.slice().sort().join('|'))fail('MRA_FIELDS_MISMATCH');}
function compare(a,b){const aa=Array.from(a,c=>c.codePointAt(0)),bb=Array.from(b,c=>c.codePointAt(0));for(let i=0;i<Math.min(aa.length,bb.length);i++)if(aa[i]!==bb[i])return aa[i]-bb[i];return aa.length-bb.length;}
function canonical(value){
 if(value===null)return 'null';
 if(typeof value==='string'){for(let i=0;i<value.length;i++){const x=value.charCodeAt(i);if(x>=0xd800&&x<=0xdbff){const y=value.charCodeAt(++i);if(!(y>=0xdc00&&y<=0xdfff))fail('MRA_INVALID_UNICODE');}else if(x>=0xdc00&&x<=0xdfff)fail('MRA_INVALID_UNICODE');}return JSON.stringify(value);}
 if(typeof value==='number'){if(!Number.isFinite(value))fail('MRA_NONFINITE_JSON');return JSON.stringify(value);}
 if(typeof value==='boolean')return JSON.stringify(value);
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 if(value&&typeof value==='object')return '{'+Object.keys(value).sort(compare).map(k=>canonical(k)+':'+canonical(value[k])).join(',')+'}';
 fail('MRA_INVALID_JSON_VALUE');
}
function parseJsonUnique(raw){
 if(typeof raw!=='string')fail('MRA_JSON_TEXT_REQUIRED');let p=0;
 const ws=()=>{while(/[ \t\r\n]/.test(raw[p]||'!'))p++;};
 function str(){const start=p;if(raw[p++]!=='"')fail('MRA_INVALID_JSON');let escaped=false;while(p<raw.length){const c=raw[p++];if(!escaped&&c==='"'){let result;try{result=JSON.parse(raw.slice(start,p));}catch(e){fail('MRA_INVALID_JSON');}canonical(result);return result;}if(!escaped&&c==='\\')escaped=true;else escaped=false;}fail('MRA_INVALID_JSON');}
 function val(depth){if(depth>128)fail('MRA_JSON_DEPTH_EXCEEDED');ws();const c=raw[p];if(c==='"')return str();if(c==='{'){p++;ws();const result=Object.create(null),seen=new Set();if(raw[p]==='}'){p++;return result;}while(true){ws();const key=str();if(seen.has(key))fail('MRA_DUPLICATE_JSON_KEY');seen.add(key);ws();if(raw[p++]!==':')fail('MRA_INVALID_JSON');result[key]=val(depth+1);ws();if(raw[p]==='}'){p++;return result;}if(raw[p++]!==',')fail('MRA_INVALID_JSON');}}if(c==='['){p++;ws();const result=[];if(raw[p]===']'){p++;return result;}while(true){result.push(val(depth+1));ws();if(raw[p]===']'){p++;return result;}if(raw[p++]!==',')fail('MRA_INVALID_JSON');}}for(const [token,value]of [['true',true],['false',false],['null',null]])if(raw.startsWith(token,p)){p+=token.length;return value;}const number=raw.slice(p).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);if(!number)fail('MRA_INVALID_JSON');p+=number[0].length;const value=Number(number[0]);if(!Number.isFinite(value))fail('MRA_NONFINITE_JSON');return value;}
 const result=val(0);ws();if(p!==raw.length)fail('MRA_INVALID_JSON');return result;
}
async function sha(text){const result=await G.crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));return Array.from(new Uint8Array(result),x=>x.toString(16).padStart(2,'0')).join('');}
const keyOf=x=>x.source_id+'\u0000'+x.start+'\u0000'+x.end;
async function decodeAssignment(a){
 exactKeys(a,['format','assignment_id','researcher_only','doctor_packet_created','documents','bindings','coverage_sources_json','coverage_sources_sha256','source_fhir_sha256','assignment_sha256']);
 if(a.format!==FORMAT||a.researcher_only!==true||a.doctor_packet_created!==false)fail('MRA_RESEARCHER_ASSIGNMENT_REQUIRED');string(a.assignment_id);
 const unhashed=clone(a);delete unhashed.assignment_sha256;if(await sha(canonical(unhashed))!==a.assignment_sha256)fail('MRA_ASSIGNMENT_HASH_MISMATCH');
 const names=Object.keys(a.documents);if(['compiled','manifest','draft','preparation_audit'].some(k=>!names.includes(k))||names.some(k=>!['compiled','manifest','draft','preparation_audit','selected_event'].includes(k)))fail('MRA_REQUIRED_DOCUMENTS_MISMATCH');
 if(names.sort().join('|')!==Object.keys(a.bindings).sort().join('|'))fail('MRA_INPUT_BINDINGS_MISMATCH');
 const docs={};for(const name of names){const raw=a.documents[name];if(await sha(raw)!==a.bindings[name])fail('MRA_DOCUMENT_HASH_MISMATCH');docs[name]=parseJsonUnique(raw);}
 const c=docs.compiled,m=docs.manifest,d=docs.draft,p=docs.preparation_audit;
 const content=clone(c);delete content.compiled_sha256;if(await sha(canonical(content))!==c.compiled_sha256)fail('MRA_COMPILED_HASH_MISMATCH');
 if(c.mechanical_audit.status!=='PASS'||c.mechanical_audit.issues.length)fail('MRA_MECHANICAL_AUDIT_NOT_PASSED');
 if(d.compiled_sha256!==c.compiled_sha256||canonical(d.target)!==canonical(c.target)||p.compiled_sha256!==c.compiled_sha256)fail('MRA_INPUT_TARGET_BINDING_MISMATCH');
 const sources=new Map(),segments=new Map();
 for(const s of m.sources){if(sources.has(s.source_id)||await sha(s.text)!==s.sha256||Array.from(s.text).length!==s.length_codepoints)fail('MRA_MANIFEST_SOURCE_HASH_MISMATCH');sources.set(s.source_id,s);}
 if(sources.size!==c.sources.length)fail('MRA_MANIFEST_SOURCE_SET_MISMATCH');
 for(const s of c.sources){const original=sources.get(s.source_id);if(!original||s.source_sha256!==original.sha256||s.source_version!==original.source_version)fail('MRA_MANIFEST_COMPILED_BINDING_MISMATCH');let cursor=0,text='';for(const span of s.segments){if(!Number.isSafeInteger(span.start)||!Number.isSafeInteger(span.end)||span.start!==cursor||span.end<=cursor||Array.from(span.text).length!==span.end-span.start)fail('MRA_INVALID_SEGMENT_COORDINATES');segments.set(keyOf({...span,source_id:s.source_id}),{...span,source_id:s.source_id,source_sha256:s.source_sha256});cursor=span.end;text+=span.text;}if(text!==original.text||cursor!==original.length_codepoints)fail('MRA_PARTITION_SOURCE_TEXT_MISMATCH');}
 if(d.segment_assessments.length!==segments.size)fail('MRA_DRAFT_SEGMENT_SET_MISMATCH');const seen=new Set();
 for(const item of d.segment_assessments){const key=keyOf(item),span=segments.get(key);if(seen.has(key)||!span||span.text!==item.exact_quote||span.source_sha256!==item.source_sha256||span.disposition!==item.original_disposition)fail('MRA_DRAFT_SEGMENT_BINDING_MISMATCH');seen.add(key);}
 const included=p.source_routing.filter(x=>x.disposition==='INCLUDED_FULL_ORIGINAL_TEXT').map(x=>x.source_id).sort(),excluded=p.source_routing.filter(x=>x.disposition==='EXCLUDED_OUTSIDE_EVENT_SCOPE').map(x=>x.source_id).sort();
 if(canonical(included)!==canonical([...sources.keys()].sort())||canonical(excluded)!==canonical(d.review_scope.excluded_source_ids.slice().sort()))fail('MRA_PREPARATION_SCOPE_MISMATCH');
 if(await sha(a.coverage_sources_json)!==a.coverage_sources_sha256)fail('MRA_COVERAGE_SOURCE_SET_HASH_MISMATCH');
 const coverage=parseJsonUnique(a.coverage_sources_json),loaded=new Map(),scope=new Set([...included,...excluded]);
 for(const s of coverage){if(loaded.has(s.source_id)||!scope.has(s.source_id)||await sha(s.text)!==s.sha256)fail('MRA_COVERAGE_SOURCE_HASH_MISMATCH');if(sources.has(s.source_id)&&sources.get(s.source_id).text!==s.text)fail('MRA_COVERAGE_MANIFEST_TEXT_MISMATCH');loaded.set(s.source_id,s);}
 for(const [sid,s]of sources)loaded.set(sid,{...(loaded.get(sid)||{}),...s});
 return {assignment:a,docs,sources:loaded,segments,unloaded:new Set([...scope].filter(sid=>!loaded.has(sid)))};
}
function newAudit(ctx){
 const audit=clone(ctx.docs.draft);audit.assessment_id='MRA-'+G.crypto.randomUUID();audit.status='PENDING';audit.reviewer_id='';audit.reviewer_type='';audit.independence_attested=false;audit.assessed_at='';audit.audit_protocol_version='';
 audit.review_scope.scope_status='INCOMPLETE';audit.review_scope.scope_basis='本响应的实际审核范围尚未完成。';
 audit.decision_time_boundary={basis:'UNRESOLVED',precision:'unknown',exact_time_known:false,retrospective_reconstruction:true,uncertainties:['目标临床决策时间证据仍待审核者核对。']};
 for(const key of Object.keys(CHECKS))audit.checks[key]=false;
 for(const item of audit.segment_assessments){item.assessment='UNRESOLVED';delete item.revised_disposition;item.rationale='尚未由本响应的审核者作出判断。';item.issue_codes=[];}
 for(const item of audit.source_coverage_assessments){item.disposition='UNRESOLVED';item.rationale='尚未由本响应的审核者核对该来源。';item.availability_basis='UNRESOLVED_REVIEW_PENDING';}
 return audit;
}
function validateAudit(ctx,a){
 const d=ctx.docs.draft;
 exactKeys(a,['schema_version','assessment_id','compiled_sha256','target','status','reviewer_id','reviewer_type','independence_attested','assessed_at','audit_protocol_version','review_scope','decision_time_boundary','checks','segment_assessments','issue_records','source_coverage_assessments','required_actions']);
 if(a.schema_version!=='0.2.0'||a.compiled_sha256!==d.compiled_sha256||canonical(a.target)!==canonical(d.target))fail('MRA_AUDIT_BINDING_MISMATCH');
 for(const key of ['reviewer_id','assessment_id','assessed_at','audit_protocol_version'])string(a[key],'MRA_EXPLICIT_REVIEWER_METADATA_REQUIRED');
 if(a.reviewer_id===d.reviewer_id||a.assessment_id===d.assessment_id||!['human','independent_review_process'].includes(a.reviewer_type))fail('MRA_NEW_ACTUAL_REVIEWER_REQUIRED');
 if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(a.assessed_at)||!Number.isFinite(Date.parse(a.assessed_at)))fail('MRA_ACTUAL_REVIEW_TIMESTAMP_REQUIRED');
 if(!['PASS','FAIL','PENDING'].includes(a.status)||typeof a.independence_attested!=='boolean')fail('MRA_AUDIT_STATUS_REQUIRED');
 exactKeys(a.review_scope,['scope_status','included_source_ids','excluded_source_ids','scope_basis']);
 for(const key of ['included_source_ids','excluded_source_ids'])if(canonical(a.review_scope[key])!==canonical(d.review_scope[key]))fail('MRA_RESPONSE_REVIEW_SCOPE_MISMATCH');
 if(!['COMPLETE','INCOMPLETE'].includes(a.review_scope.scope_status))fail('MRA_INVALID_SCOPE_STATUS');string(a.review_scope.scope_basis);
 exactKeys(a.decision_time_boundary,['basis','precision','exact_time_known','retrospective_reconstruction','uncertainties']);
 const t=a.decision_time_boundary;string(t.basis);string(t.precision);if(typeof t.exact_time_known!=='boolean'||t.retrospective_reconstruction!==true||!Array.isArray(t.uncertainties)||t.uncertainties.some(x=>!nonempty(x)))fail('MRA_TIME_BOUNDARY_REQUIRED');
 exactKeys(a.checks,Object.keys(CHECKS));if(Object.values(a.checks).some(x=>typeof x!=='boolean'))fail('MRA_BOOLEAN_CHECK_REQUIRED');
 if(!Array.isArray(a.segment_assessments)||a.segment_assessments.length!==ctx.segments.size)fail('MRA_EVERY_SEGMENT_REQUIRES_ASSESSMENT');
 const seen=new Set();for(const item of a.segment_assessments){const key=keyOf(item),span=ctx.segments.get(key);if(!span||seen.has(key)||item.exact_quote!==span.text||item.source_sha256!==span.source_sha256||item.original_disposition!==span.disposition)fail('MRA_SEGMENT_BINDING_MISMATCH');seen.add(key);
  exactKeys(item,['source_id','start','end','source_sha256','exact_quote','original_disposition','assessment','rationale','issue_codes',...(Object.prototype.hasOwnProperty.call(item,'revised_disposition')?['revised_disposition']:[])]);string(item.rationale);if(!Array.isArray(item.issue_codes)||item.issue_codes.some(x=>!nonempty(x)))fail('MRA_ISSUE_CODES_REQUIRED');
  if(item.assessment==='ACCEPT'){if(item.original_disposition==='REVIEW'||'revised_disposition'in item)fail('ACCEPT_REQUIRES_PRIOR_KEEP_OR_MASK');}
  else if(item.assessment==='REVISE'){if(!['KEEP','MASK'].includes(item.revised_disposition))fail('REVISION_REQUIRES_KEEP_OR_MASK');}
  else if(item.assessment!=='UNRESOLVED'||'revised_disposition'in item)fail('UNRESOLVED_ASSESSMENT_CANNOT_PROPOSE_DISPOSITION');
 }
 const expected=new Set(d.source_coverage_assessments.map(x=>x.source_id)),covered=new Set();
 if(!Array.isArray(a.source_coverage_assessments))fail('MRA_SOURCE_COVERAGE_REQUIRED');
 for(const item of a.source_coverage_assessments){exactKeys(item,['source_id','disposition','rationale','availability_basis']);if(covered.has(item.source_id)||!expected.has(item.source_id))fail('SOURCE_COVERAGE_BINDING_MISMATCH');covered.add(item.source_id);string(item.rationale);string(item.availability_basis);if(!['REVIEWED_INCLUDED','REVIEWED_EXCLUDED','REQUIRED_MISSING','DELIVERY_NOT_FOUND','UNRESOLVED'].includes(item.disposition))fail('MRA_INVALID_COVERAGE_DISPOSITION');if(ctx.unloaded.has(item.source_id)&&item.disposition!=='UNRESOLVED')fail('MRA_UNLOADED_SOURCE_MUST_REMAIN_UNRESOLVED');}
 if(covered.size!==expected.size)fail('SOURCE_COVERAGE_BINDING_MISMATCH');
 if(!Array.isArray(a.issue_records))fail('MRA_ISSUE_RECORDS_ARRAY_REQUIRED');const issueIds=new Set();
 for(const issue of a.issue_records){exactKeys(issue,['issue_id','code','attribution','severity','status','description','source_ids','supporting_quotes']);for(const key of ['issue_id','code','description'])string(issue[key]);if(issueIds.has(issue.issue_id))fail('DUPLICATE_SEMANTIC_ISSUE_ID');issueIds.add(issue.issue_id);if(!['model_proposal','source_processing','material_assembly','source_availability','independent_review_unresolved','other'].includes(issue.attribution)||!['BLOCKING','NON_BLOCKING'].includes(issue.severity)||!['OPEN','RESOLVED'].includes(issue.status))fail('MRA_INVALID_ISSUE_CLASSIFICATION');if(!Array.isArray(issue.source_ids)||issue.source_ids.some(sid=>!expected.has(sid))||!Array.isArray(issue.supporting_quotes))fail('ISSUE_SOURCE_OUTSIDE_REVIEW_SCOPE');for(const q of issue.supporting_quotes){exactKeys(q,['source_id','start','end','exact_quote']);const span=ctx.segments.get(keyOf(q));if(!span||q.exact_quote!==span.text)fail('SEMANTIC_ISSUE_QUOTE_BINDING_MISMATCH');}}
 if(!Array.isArray(a.required_actions)||a.required_actions.some(x=>!nonempty(x)))fail('MRA_REQUIRED_ACTIONS_ARRAY_REQUIRED');
 if(a.status==='PASS'){
  if(!a.independence_attested)fail('INDEPENDENCE_ATTESTATION_REQUIRED');
  if(Object.values(a.checks).some(x=>!x))fail('SEMANTIC_CHECKS_INCOMPLETE');
  if(a.review_scope.scope_status!=='COMPLETE')fail('INCOMPLETE_REVIEW_SCOPE_CANNOT_PASS');
  if(a.source_coverage_assessments.some(x=>x.disposition==='UNRESOLVED'))fail('UNRESOLVED_SOURCE_COVERAGE_CANNOT_PASS');
  if(a.segment_assessments.some(x=>x.assessment==='UNRESOLVED'))fail('UNRESOLVED_SEGMENT_CANNOT_PASS');
  if(a.segment_assessments.some(x=>x.assessment==='REVISE'))fail('REVISION_REQUIRES_RECOMPILE_AND_REAUDIT');
  const included=new Set(a.review_scope.included_source_ids),excluded=new Set(a.review_scope.excluded_source_ids);
  if(a.source_coverage_assessments.some(x=>included.has(x.source_id)&&x.disposition==='REVIEWED_EXCLUDED'||excluded.has(x.source_id)&&x.disposition==='REVIEWED_INCLUDED'))fail('SOURCE_SCOPE_REVISION_REQUIRES_NEW_PARTITION_AND_REAUDIT');
  if(a.issue_records.some(x=>x.status==='OPEN'&&x.severity==='BLOCKING'))fail('OPEN_BLOCKING_ISSUE_CANNOT_PASS');
 }
 return a;
}
async function makeResponse(ctx,a){validateAudit(ctx,a);return {format:RESPONSE,assignment_id:ctx.assignment.assignment_id,assignment_sha256:ctx.assignment.assignment_sha256,input_bindings:clone(ctx.assignment.bindings),audit:clone(a),audit_sha256:await sha(canonical(a))};}
async function restoreResponse(ctx,r){exactKeys(r,['format','assignment_id','assignment_sha256','input_bindings','audit','audit_sha256']);if(r.format!==RESPONSE||r.assignment_id!==ctx.assignment.assignment_id||r.assignment_sha256!==ctx.assignment.assignment_sha256||canonical(r.input_bindings)!==canonical(ctx.assignment.bindings))fail('MRA_RESPONSE_ASSIGNMENT_BINDING_MISMATCH');if(await sha(canonical(r.audit))!==r.audit_sha256)fail('MRA_RESPONSE_AUDIT_HASH_MISMATCH');validateAudit(ctx,r.audit);return clone(r.audit);}
const api={canonical,parseJsonUnique,sha,decodeAssignment,newAudit,validateAudit,makeResponse,restoreResponse};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
G.MaterialResearcherReview=api;
if(typeof document==='undefined')return;
const $=id=>document.getElementById(id);
let ctx,audit,segmentIndex=0,coverageIndex=0,dirty=false;
const navigationOnly=new Set(['segment-filter','segment-nav','coverage-filter','coverage-status-filter','coverage-nav','context-document','context-field','context-item','response-file']);
for(const eventName of ['input','change'])document.addEventListener(eventName,e=>{if(audit&&!navigationOnly.has(e.target.id))dirty=true;});
G.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
function error(e){$('answer-error').textContent='尚未导出：'+e.message+'。请核对本次填写；Python 回收端还会独立核验。';$('answer-error').hidden=false;}
function option(value,label){const node=document.createElement('option');node.value=value;node.textContent=label;return node;}
function saveSegment(){if(!audit)return;const item=audit.segment_assessments[segmentIndex];item.assessment=$('segment-assessment').value;delete item.revised_disposition;if(item.assessment==='REVISE')item.revised_disposition=$('segment-revision').value;item.rationale=$('segment-rationale').value;item.issue_codes=lines($('segment-issue-codes').value);}
function saveCoverage(){if(!audit)return;const item=audit.source_coverage_assessments[coverageIndex];item.disposition=$('coverage-disposition').value;item.rationale=$('coverage-rationale').value;item.availability_basis=$('coverage-availability').value;}
function progress(){const done=audit.segment_assessments.filter(x=>x.assessment!=='UNRESOLVED').length,covered=audit.source_coverage_assessments.filter(x=>x.disposition!=='UNRESOLVED').length;$('progress').textContent=`区间明确填写 ${done}／${audit.segment_assessments.length} · 来源覆盖明确填写 ${covered}／${audit.source_coverage_assessments.length} · 未载入原文 ${ctx.unloaded.size} 份`;$('coverage-summary').textContent=`本任务范围含 ${audit.source_coverage_assessments.length} 份来源；已载入 ${ctx.sources.size} 份原文。加载原文不等于完成审核。`;}
function segmentNavigation(){const filter=$('segment-filter').value,nav=$('segment-nav');nav.replaceChildren();audit.segment_assessments.forEach((s,i)=>{if(filter==='all'||s.assessment===filter||s.original_disposition===filter)nav.append(option(String(i),`${i+1}. ${s.source_id} [${s.start},${s.end}) · ${s.original_disposition} → ${s.assessment}`));});if([...nav.options].some(o=>+o.value===segmentIndex))nav.value=String(segmentIndex);else if(nav.options.length){segmentIndex=+nav.value;renderSegment();}}
function renderSegment(){const item=audit.segment_assessments[segmentIndex];$('segment-binding').textContent=`${item.source_id} · [${item.start}, ${item.end}) · Unicode code point\n来源 SHA-256 ${item.source_sha256}\n原有分区 ${item.original_disposition}`;$('segment-text').textContent=item.exact_quote;$('source-text').textContent=ctx.sources.get(item.source_id).text;$('draft-segment').textContent=JSON.stringify(ctx.docs.draft.segment_assessments[segmentIndex],null,2);$('segment-assessment').value=item.assessment;const accept=[...$('segment-assessment').options].find(o=>o.value==='ACCEPT');accept.disabled=item.original_disposition==='REVIEW';$('segment-revision').value=item.revised_disposition||'';$('revision-label').hidden=item.assessment!=='REVISE';$('segment-rationale').value=item.rationale;$('segment-issue-codes').value=item.issue_codes.join('\n');$('previous-segment').disabled=segmentIndex===0;$('next-segment').disabled=segmentIndex===audit.segment_assessments.length-1;}
function coverageNavigation(){const query=$('coverage-filter').value.trim().toLowerCase(),status=$('coverage-status-filter').value,nav=$('coverage-nav');nav.replaceChildren();audit.source_coverage_assessments.forEach((s,i)=>{const original=ctx.sources.get(s.source_id),title=original?.title||'';if((!query||`${s.source_id} ${title} ${s.disposition}`.toLowerCase().includes(query))&&(status==='all'||s.disposition===status||status==='unloaded'&&!original))nav.append(option(String(i),`${s.source_id}${title?' · '+title:''} · ${s.disposition}${original?'':' · 未载入'}`));});if([...nav.options].some(o=>+o.value===coverageIndex))nav.value=String(coverageIndex);else if(nav.options.length){coverageIndex=+nav.value;renderCoverage();}}
function renderCoverage(){const item=audit.source_coverage_assessments[coverageIndex],source=ctx.sources.get(item.source_id);$('coverage-metadata').textContent=source?JSON.stringify({source_id:item.source_id,sha256:source.sha256,title:source.title||'',kind:source.kind||'',clocks:source.clocks||{},clock_basis:source.clock_basis||'unknown'},null,2):item.source_id+' · 仅有来源目录，无已载入原文';$('coverage-text').textContent=source?source.text:'';$('coverage-text').hidden=!source;$('coverage-missing').hidden=!!source;$('coverage-disposition').value=item.disposition;for(const opt of $('coverage-disposition').options)opt.disabled=!source&&opt.value!=='UNRESOLVED';$('coverage-rationale').value=item.rationale;$('coverage-availability').value=item.availability_basis;}
function populate(){
 for(const [id,key]of [['reviewer-id','reviewer_id'],['reviewer-type','reviewer_type'],['assessment-id','assessment_id'],['protocol-version','audit_protocol_version'],['assessed-at','assessed_at'],['audit-status','status']])$(id).value=audit[key];
 $('independence-attested').checked=audit.independence_attested;$('scope-status').value=audit.review_scope.scope_status;$('scope-basis').value=audit.review_scope.scope_basis;
 const t=audit.decision_time_boundary;$('time-basis').value=t.basis;$('time-precision').value=t.precision;$('exact-time-known').checked=t.exact_time_known;$('time-uncertainties').value=t.uncertainties.join('\n');
 for(const key of Object.keys(CHECKS))$('check-'+key).checked=audit.checks[key];$('issue-records').value=JSON.stringify(audit.issue_records,null,2);$('required-actions').value=audit.required_actions.join('\n');renderSegment();segmentNavigation();renderCoverage();coverageNavigation();progress();
}
function collect(){saveSegment();saveCoverage();for(const [id,key]of [['reviewer-id','reviewer_id'],['reviewer-type','reviewer_type'],['assessment-id','assessment_id'],['protocol-version','audit_protocol_version'],['assessed-at','assessed_at'],['audit-status','status']])audit[key]=$(id).value;audit.independence_attested=$('independence-attested').checked;audit.review_scope.scope_status=$('scope-status').value;audit.review_scope.scope_basis=$('scope-basis').value;audit.decision_time_boundary={basis:$('time-basis').value,precision:$('time-precision').value,exact_time_known:$('exact-time-known').checked,retrospective_reconstruction:true,uncertainties:lines($('time-uncertainties').value)};for(const key of Object.keys(CHECKS))audit.checks[key]=$('check-'+key).checked;audit.issue_records=parseJsonUnique($('issue-records').value);audit.required_actions=lines($('required-actions').value);return audit;}
function contextFields(){const data=ctx.docs[$('context-document').value]||{};$('context-field').replaceChildren(...Object.keys(data).map(k=>option(k,k)));contextItems();}
function contextItems(){const data=ctx.docs[$('context-document').value]||{},value=data[$('context-field').value],array=Array.isArray(value);$('context-item-label').hidden=!array;$('context-item').replaceChildren();if(array)for(let i=0;i<value.length;i++)$('context-item').append(option(String(i),String(i+1)+'／'+value.length));renderContext();}
function renderContext(){const data=ctx.docs[$('context-document').value]||{};let value=data[$('context-field').value];if(Array.isArray(value))value=value[+$('context-item').value];$('preparation-context').textContent=JSON.stringify(value===undefined?'无此字段或条目':value,null,2);}
async function boot(){
 try{ctx=await decodeAssignment(parseJsonUnique($('assigned-data').textContent));audit=newAudit(ctx);$('assignment-label').textContent=ctx.assignment.assignment_id;$('binding-details').textContent=JSON.stringify({assignment_sha256:ctx.assignment.assignment_sha256,input_bindings:ctx.assignment.bindings,compiled_sha256:ctx.docs.compiled.compiled_sha256,source_fhir_sha256:ctx.assignment.source_fhir_sha256},null,2);contextFields();
  for(const [key,label]of Object.entries(CHECKS)){const row=document.createElement('label');row.className='checkbox';const input=document.createElement('input');input.type='checkbox';input.id='check-'+key;row.append(input,document.createTextNode(label));$('checks').append(row);}
  populate();$('main').hidden=false;
 }catch(e){$('load-error').textContent='任务未通过核验，不能编辑：'+e.message;$('load-error').hidden=false;return;}
 $('segment-filter').addEventListener('change',()=>{saveSegment();segmentNavigation();renderSegment();});
 $('segment-nav').addEventListener('change',()=>{saveSegment();segmentIndex=+$('segment-nav').value;renderSegment();progress();});
 for(const [id,delta]of [['previous-segment',-1],['next-segment',1]])$(id).addEventListener('click',()=>{saveSegment();segmentIndex=Math.max(0,Math.min(audit.segment_assessments.length-1,segmentIndex+delta));$('segment-filter').value='all';segmentNavigation();renderSegment();progress();});
 $('segment-assessment').addEventListener('change',()=>{saveSegment();$('revision-label').hidden=$('segment-assessment').value!=='REVISE';progress();});
 for(const id of ['segment-revision','segment-rationale','segment-issue-codes'])$(id).addEventListener('input',()=>{saveSegment();progress();});
 $('coverage-filter').addEventListener('input',()=>{saveCoverage();coverageNavigation();renderCoverage();});
 $('coverage-status-filter').addEventListener('change',()=>{saveCoverage();coverageNavigation();renderCoverage();});
 $('coverage-nav').addEventListener('change',()=>{saveCoverage();coverageIndex=+$('coverage-nav').value;renderCoverage();progress();});
 for(const id of ['coverage-disposition','coverage-rationale','coverage-availability'])$(id).addEventListener('input',()=>{saveCoverage();progress();});
 $('stamp-time').addEventListener('click',()=>{$('assessed-at').value=new Date().toISOString();dirty=true;});
 $('context-document').addEventListener('change',contextFields);$('context-field').addEventListener('change',contextItems);$('context-item').addEventListener('change',renderContext);
 $('export-button').addEventListener('click',async()=>{try{const response=await makeResponse(ctx,collect()),blob=new Blob([JSON.stringify(response,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='researcher-material-response-'+Date.now()+'.json';link.click();dirty=false;setTimeout(()=>URL.revokeObjectURL(url),1000);$('answer-error').hidden=true;$('export-status').textContent='已导出 '+response.audit.status+' 响应；请保存在 private 并交研究协调者执行 Python 核验。本应用未生成医生包。';}catch(e){error(e);}});
 $('response-file').addEventListener('change',async()=>{try{const file=$('response-file').files[0];if(!file)return;const restored=await restoreResponse(ctx,parseJsonUnique(await file.text()));audit=restored;segmentIndex=0;coverageIndex=0;populate();dirty=false;$('answer-error').hidden=true;$('export-status').textContent='已恢复与本任务绑定的响应。恢复身份和声明来自导入文件，程序未认证实际人员。';}catch(e){error(e);}finally{$('response-file').value='';}});
}
boot();
})(globalThis);
