/* Researcher-only chain mirror. Python replay remains authoritative; hashes do not authenticate people. */
(function(G){'use strict';
const api=G.MaterialResearcherReview;
const same=(a,b,code)=>{if(api.canonical(a)!==api.canonical(b))throw new Error(code);};
const demand=(x,code)=>{if(!x)throw new Error(code);};
const clone=x=>JSON.parse(JSON.stringify(x));
function show(node,value){node.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);}
async function validateOpinion(assignment,docs,data){
 const expected=['format','assignment_sha256','automatic_state','stop_reason','chain','canonical_documents','validation_receipt','reviewer_type','independence_attested','human_sample_status','clinical_release_permitted','doctor_packet_created'];
 same(Object.keys(data).sort(),expected.sort(),'AUTO_OPINION_FIELDS_MISMATCH');
 demand(data.format==='researcher-automated-material-opinion-0.2.0'&&data.assignment_sha256===assignment.assignment_sha256&&data.reviewer_type==='llm_material_review'&&data.independence_attested===false&&data.human_sample_status==='NOT_ASSESSED'&&data.clinical_release_permitted===false&&data.doctor_packet_created===false,'AUTO_OPINION_GOVERNANCE_MISMATCH');
 const chain=data.chain,raw=data.canonical_documents,hashes={};
 const current=chain.format==='automated-material-review-chain-0.3.0',legacy=chain.format==='automated-material-review-chain-0.2.0';
 demand((current&&chain.protocol_version==='checkpoint-automated-material-review-0.3.0')||(legacy&&chain.protocol_version==='checkpoint-automated-material-review-0.2.0'),'CHAIN_VERSION_REQUIRES_REPLAYABLE_V02_OR_V03');
 same(chain.compiled,docs.compiled,'AUTO_OPINION_INPUT_BINDING_MISMATCH');same(chain.manifest,docs.manifest,'AUTO_OPINION_INPUT_BINDING_MISMATCH');
 demand(data.automatic_state===chain.state&&data.stop_reason===chain.stop_reason,'AUTO_OPINION_INPUT_BINDING_MISMATCH');
 demand(chain.clinical_release_permitted===false&&chain.event_graph_modified===false&&chain.clinical_reference_created===false&&chain.human_sample_status==='NOT_ASSESSED','CHAIN_GOVERNANCE_FLAGS_MISMATCH');
 demand(Array.isArray(chain.rounds)&&Number.isInteger(chain.max_rounds)&&chain.max_rounds>=1&&chain.max_rounds<=20&&chain.rounds.length<=chain.max_rounds,'CHAIN_ROUND_BUDGET_INVALID');
 const paths=['chain','chain.initial_manifest','chain.initial_compiled','chain.source_registry','chain.manifest','chain.compiled','chain.doctor_view','chain.task_context'];
 if(chain.task_context.candidate_graph_snapshot)paths.push('chain.task_context.candidate_graph_snapshot');
 chain.rounds.forEach((r,i)=>{for(const k of ['input_manifest','input_compiled','request','review','manifest','compiled','ledger','diff','application_receipt'])paths.push('chain.rounds.'+i+'.'+k);paths.push('chain.rounds.'+i+'.request_without_binding');});
 if(chain.failure)paths.push('chain.failure.request','chain.failure.request_without_binding');
 same(Object.keys(raw).sort(),paths.sort(),'CHAIN_CANONICAL_DOCUMENTS_MISMATCH');
 for(const path of paths){
  let value;
  if(path.endsWith('.request_without_binding')){value=clone(path.startsWith('chain.failure.')?chain.failure.request:chain.rounds[Number(path.split('.')[2])].request);delete value.response_binding;}
  else{value={chain};for(const key of path.split('.'))value=value[key];}
  demand(typeof raw[path]==='string','CHAIN_CANONICAL_DOCUMENT_TEXT_REQUIRED');
  same(api.parseJsonUnique(raw[path]),value,'CHAIN_CANONICAL_DOCUMENT_CONTENT_MISMATCH');
  hashes[path]=await api.sha(raw[path]);
 }
 demand(hashes.chain===data.validation_receipt.chain_sha256&&data.validation_receipt.deterministic_replay_verified===true&&data.validation_receipt.clinical_visibility_certified===false&&data.validation_receipt.clinical_release_permitted===false,'CHAIN_REPLAY_RECEIPT_BINDING_MISMATCH');
 demand(hashes['chain.source_registry']===chain.source_registry_sha256,'CHAIN_SOURCE_REGISTRY_HASH_MISMATCH');
 const registry=new Map();for(const s of chain.source_registry){demand(!registry.has(s.source_id)&&['INCLUDED','CANDIDATE','EXCLUDED'].includes(s.origin_scope)&&await api.sha(s.text)===s.sha256,'CHAIN_SOURCE_REGISTRY_INVALID');registry.set(s.source_id,s);}
 async function verifyMaterial(m,c){
  demand(m.manifest_id===c.manifest_id,'CHAIN_MANIFEST_ID_MISMATCH');same(m.sources.map(s=>s.source_id).sort(),c.sources.map(s=>s.source_id).sort(),'CHAIN_MANIFEST_SCOPE_MISMATCH');
  const unhashed=clone(c);delete unhashed.compiled_sha256;demand(await api.sha(api.canonical(unhashed))===c.compiled_sha256,'CHAIN_COMPILED_HASH_MISMATCH');
  for(const s of m.sources){const r=registry.get(s.source_id),p=c.sources.find(x=>x.source_id===s.source_id);demand(r&&r.text===s.text&&r.source_version===s.source_version&&r.sha256===s.sha256&&await api.sha(s.text)===s.sha256,'CHAIN_SOURCE_REGISTRY_BINDING_MISMATCH');demand(p.source_version===s.source_version&&p.source_sha256===s.sha256&&p.segments.map(x=>x.text).join('')===s.text,'CHAIN_ORIGINAL_COMPILED_MISMATCH');let cursor=0;for(const x of p.segments){demand(x.start===cursor&&x.end>x.start&&Array.from(x.text).length===x.end-x.start&&['KEEP','MASK','REVIEW'].includes(x.disposition),'CHAIN_PARTITION_INVALID');cursor=x.end;}demand(cursor===p.length_codepoints,'CHAIN_PARTITION_INVALID');}
 }
 function mirrorOperations(input,request,round){
  const original=new Map(input.sources.map(s=>[s.source_id,s])),active=new Map(input.sources.map(s=>[s.source_id,s.segments.map(p=>({start:p.start,end:p.end,disposition:p.disposition,text:p.text}))]));
  const available=new Map(request.candidate_sources.concat(request.excluded_sources).map(s=>[s.source_id,s]));
  const unresolved=[],graphIssues=[],human=[],holds=[];
  for(let index=0;index<round.review.operations.length;index++){
   const op=round.review.operations[index],record=round.ledger.operations[index],sid=op.source_id,kind=op.operation;
   if(['INCLUDE_SOURCE','EXCLUDE_SOURCE','MARK_SOURCE_UNRESOLVED'].includes(kind)){
    demand(active.has(sid)||available.has(sid),'CHAIN_UNAUTHORIZED_SOURCE_OPERATION');
    if(kind==='INCLUDE_SOURCE'){
     if(active.has(sid)){demand(record.status==='REJECTED'&&record.reason==='SOURCE_ALREADY_INCLUDED','CHAIN_LEDGER_STATUS_REPLAY_MISMATCH');continue;}
     const source=available.get(sid);demand(source,'CHAIN_INCLUDE_SOURCE_NOT_SUPPLIED');active.set(sid,[{start:0,end:Array.from(source.text).length,disposition:'REVIEW',text:source.text}]);
    }else if(kind==='EXCLUDE_SOURCE'){
     if(!active.has(sid)){demand(record.status==='REJECTED'&&record.reason==='SOURCE_NOT_CURRENTLY_INCLUDED','CHAIN_LEDGER_STATUS_REPLAY_MISMATCH');continue;}active.delete(sid);
    }else unresolved.push({source_id:sid,reason:op.reason,scope:'specific_source_not_patient_or_hospital'});
    demand(record.status==='APPLIED','CHAIN_LEDGER_STATUS_REPLAY_MISMATCH');
   }else if(kind==='SET_DISPOSITION'){
    const source=original.get(sid),segments=active.get(sid);demand(source&&segments&&['KEEP','MASK','REVIEW'].includes(op.disposition),'CHAIN_INVALID_DISPOSITION_OPERATION');
    const text=Array.from(source.segments.map(p=>p.text).join('')),quote=Array.from(op.exact_quote||''),matches=[];demand(quote.length>0,'CHAIN_EMPTY_EXACT_QUOTE');
    for(let start=0;start+quote.length<=text.length;start++){const end=start+quote.length;if(text.slice(start,end).join('')!==op.exact_quote)continue;if(op.prefix!==undefined&&text.slice(Math.max(0,start-Array.from(op.prefix).length),start).join('')!==op.prefix)continue;if(op.suffix!==undefined&&text.slice(end,end+Array.from(op.suffix).length).join('')!==op.suffix)continue;matches.push({start,end});}
    demand(matches.length===1,'CHAIN_DISPOSITION_QUOTE_NOT_UNIQUE');const {start,end}=matches[0],found=segments.map((p,i)=>({p,i})).filter(({p})=>p.start<=start&&end<=p.end);demand(found.length===1,'CHAIN_DISPOSITION_NOT_IN_ONE_SEGMENT');const {p,i}=found[0];
    if(p.disposition===op.disposition&&p.start===start&&p.end===end){demand(record.status==='NO_CHANGE'&&record.reason==='SAME_DISPOSITION_AND_SPAN','CHAIN_LEDGER_STATUS_REPLAY_MISMATCH');continue;}
    demand(record.status==='APPLIED'&&record.resolved_anchor&&record.resolved_anchor.start===start&&record.resolved_anchor.end===end&&record.resolved_anchor.source_id===sid&&record.resolved_anchor.source_sha256===source.source_sha256&&record.resolved_anchor.exact_quote===op.exact_quote,'CHAIN_ANCHOR_REPLAY_MISMATCH');
    const replacement=[];for(const [a,b]of [[p.start,start],[start,end],[end,p.end]])if(a<b)replacement.push({start:a,end:b,disposition:a===start&&b===end?op.disposition:p.disposition,text:text.slice(a,b).join('')});segments.splice(i,1,...replacement);
   }else{demand(record.status==='RECORDED_REQUIRES_EXTERNAL_ACTION'&&record.no_graph_or_permission_change===true,'CHAIN_EXTERNAL_ACTION_STATUS_MISMATCH');if(kind==='FLAG_EVENT_GRAPH_ISSUE')graphIssues.push(op);else if(kind==='REQUEST_HUMAN_REVIEW')human.push(op);else if(kind==='HOLD_PACKET')holds.push(op);else demand(false,'CHAIN_UNKNOWN_OPERATION');}
  }
  same(round.ledger.unresolved_sources,unresolved,'CHAIN_UNRESOLVED_LEDGER_REPLAY_MISMATCH');same(round.ledger.graph_issues,graphIssues,'CHAIN_GRAPH_LEDGER_REPLAY_MISMATCH');same(round.ledger.human_requests,human,'CHAIN_HUMAN_LEDGER_REPLAY_MISMATCH');same(round.ledger.holds,holds,'CHAIN_HOLD_LEDGER_REPLAY_MISMATCH');
  same(Array.from(active.keys()),round.compiled.sources.map(s=>s.source_id),'CHAIN_OUTPUT_SOURCE_REPLAY_MISMATCH');
  for(const source of round.compiled.sources)same(active.get(source.source_id),source.segments.map(p=>({start:p.start,end:p.end,disposition:p.disposition,text:p.text})),'CHAIN_OUTPUT_PARTITION_REPLAY_MISMATCH');
 }
 async function verifyView(view,compiled){
  demand(view.format==='checkpoint-doctor-view-preview-0.1.0'&&view.compiled_sha256===compiled.compiled_sha256&&view.researcher_original_included===false,'CHAIN_DOCTOR_VIEW_BINDING_MISMATCH');same(view.target,compiled.target,'CHAIN_DOCTOR_VIEW_TARGET_MISMATCH');
  const projected=[];for(const s of compiled.sources){let text='',masking=false;for(const p of s.segments){if(p.disposition==='MASK'||p.disposition==='REVIEW'){if(!masking)text+='[资料按方案遮蔽]';masking=true;}else{text+=p.text;masking=false;}}projected.push({source_id:s.source_id,source_version:s.source_version,text,sha256:await api.sha(text)});}
  same(view.materials,projected,'CHAIN_DOCTOR_VIEW_PROJECTION_MISMATCH');const material=clone(view);delete material.view_sha256;demand(await api.sha(api.canonical(material))===view.view_sha256,'CHAIN_DOCTOR_VIEW_HASH_MISMATCH');
 }
 let m=chain.initial_manifest,c=chain.initial_compiled,terminal=null;
 await verifyMaterial(m,c);
 const contentSignature=c=>api.canonical(c.sources.map(s=>({source_id:s.source_id,source_version:s.source_version,source_sha256:s.source_sha256,segments:s.segments.map(p=>({start:p.start,end:p.end,disposition:p.disposition,text:p.text}))})));
 const newSourcePending=(compiled,previous)=>!!(current&&previous&&previous.diff.source_scope_changed===true&&previous.review.status==='AUTO_REVIEW_REVISE'&&compiled.sources.filter(s=>!previous.input_compiled.sources.some(old=>old.source_id===s.source_id)).every(s=>s.segments.every(p=>p.disposition==='REVIEW'))&&compiled.sources.some(s=>!previous.input_compiled.sources.some(old=>old.source_id===s.source_id)));
 const visibleSignature=v=>api.canonical(v.materials.filter(m=>m.text!=='[资料按方案遮蔽]'));
 const seen=new Set([contentSignature(c)]),previouslyIncluded=new Set(m.sources.map(s=>s.source_id));
 for(let i=0;i<chain.rounds.length;i++){
  demand(terminal===null,'CHAIN_ROUND_AFTER_TERMINAL_STATE');const r=chain.rounds[i],path='chain.rounds.'+i,request=r.request,review=r.review;
  same(r.input_manifest,m,'CHAIN_INPUT_MANIFEST_CONTINUITY_MISMATCH');same(r.input_compiled,c,'CHAIN_INPUT_COMPILED_CONTINUITY_MISMATCH');
  demand(r.input_manifest_sha256===hashes[path+'.input_manifest']&&r.input_compiled_sha256===c.compiled_sha256,'CHAIN_INPUT_HASH_MISMATCH');
  demand(r.request_sha256===hashes[path+'.request']&&r.request_binding_sha256===hashes[path+'.request_without_binding']&&r.source_registry_sha256===chain.source_registry_sha256,'CHAIN_REQUEST_HASH_MISMATCH');
  await verifyView(request.doctor_view,c);
  same(request.target,c.target,'CHAIN_REQUEST_TARGET_MISMATCH');same(request.task_context,chain.task_context,'CHAIN_REQUEST_CONTEXT_MISMATCH');same(request.researcher_sources,m.sources.map(s=>({source_id:s.source_id,source_version:s.source_version,sha256:s.sha256,text:s.text})),'CHAIN_REQUEST_SOURCE_MISMATCH');
  demand(request.protocol_version===chain.protocol_version&&request.response_binding.protocol_version===chain.protocol_version&&request.response_binding.compiled_sha256===c.compiled_sha256&&request.response_binding.request_sha256===r.request_binding_sha256&&request.response_binding.doctor_view_sha256===request.doctor_view.view_sha256,'CHAIN_RESPONSE_ENVELOPE_MISMATCH');
  const included=new Set(m.sources.map(s=>s.source_id)),candidates=chain.source_registry.filter(s=>!included.has(s.source_id)&&s.origin_scope==='CANDIDATE'&&!previouslyIncluded.has(s.source_id)),excluded=chain.source_registry.filter(s=>!included.has(s.source_id)&&(s.origin_scope!=='CANDIDATE'||previouslyIncluded.has(s.source_id)));
  same(request.candidate_sources.map(s=>s.source_id),candidates.map(s=>s.source_id),'CHAIN_CANDIDATE_SOURCE_SCOPE_MISMATCH');same(request.excluded_sources.map(s=>s.source_id),excluded.map(s=>s.source_id),'CHAIN_EXCLUDED_SOURCE_SCOPE_MISMATCH');
  for(const s of request.candidate_sources.concat(request.excluded_sources)){const original=registry.get(s.source_id);demand(original&&s.text===original.text&&s.sha256===original.sha256&&s.source_version===original.source_version,'CHAIN_REQUEST_REGISTRY_TEXT_MISMATCH');}
  demand(review.format===(current?'material-review-0.3.0':'material-review-0.2.0')&&review.reviewer_type==='llm_material_review'&&review.protocol_version===chain.protocol_version&&review.compiled_sha256===c.compiled_sha256&&review.request_sha256===r.request_binding_sha256&&review.doctor_view_sha256===request.doctor_view.view_sha256,'CHAIN_REVIEW_INPUT_BINDING_MISMATCH');same(review.target,c.target,'CHAIN_REVIEW_TARGET_MISMATCH');
  if(current){same(review.review_round,request.review_round,'CHAIN_REVIEW_ROUND_BINDING_MISMATCH');demand(request.review_round.round_index===i+1&&request.review_round.max_rounds===chain.max_rounds,'CHAIN_REQUEST_ROUND_MISMATCH');if(i===0){demand(request.review_round.is_first_review&&!request.review_round.current_state_produced_by_previous_review&&!request.previous_round,'CHAIN_INITIAL_HISTORY_MISMATCH');}else{const previous=chain.rounds[i-1];demand(!request.review_round.is_first_review&&request.review_round.current_state_produced_by_previous_review,'CHAIN_CONTINUATION_ATTRIBUTION_MISMATCH');same(request.previous_round.previous_review,previous.review,'CHAIN_PREVIOUS_REVIEW_MISMATCH');same(request.previous_round.operation_receipts,previous.ledger.operations,'CHAIN_PREVIOUS_OPERATION_RECEIPTS_MISMATCH');same(request.previous_round.actual_program_diff,previous.diff,'CHAIN_PREVIOUS_DIFF_MISMATCH');same(request.previous_round.program_execution_receipt,previous.application_receipt,'CHAIN_PREVIOUS_APPLICATION_MISMATCH');demand(request.previous_round.round_index===i&&request.previous_round.current_compiled_sha256===c.compiled_sha256&&request.previous_round.current_doctor_view_sha256===request.doctor_view.view_sha256,'CHAIN_PREVIOUS_STATE_HASH_MISMATCH');}}
  demand(Array.isArray(r.ledger.operations)&&r.ledger.operations.length===review.operations.length,'CHAIN_LEDGER_OPERATION_COUNT_MISMATCH');
  const operationIds=new Set();for(let j=0;j<review.operations.length;j++){const op=review.operations[j],applied=r.ledger.operations[j];demand(!operationIds.has(op.operation_id),'CHAIN_DUPLICATE_OPERATION_ID');operationIds.add(op.operation_id);same(applied.operation,op,'CHAIN_LEDGER_OPERATION_IDENTITY_MISMATCH');demand(['APPLIED','NO_CHANGE','REJECTED','RECORDED_REQUIRES_EXTERNAL_ACTION'].includes(applied.status),'CHAIN_LEDGER_STATUS_INVALID');if(['REQUEST_HUMAN_REVIEW','FLAG_EVENT_GRAPH_ISSUE','HOLD_PACKET'].includes(op.operation))demand(applied.status==='RECORDED_REQUIRES_EXTERNAL_ACTION'&&applied.no_graph_or_permission_change===true,'CHAIN_EXTERNAL_ACTION_STATUS_MISMATCH');}
  mirrorOperations(c,request,r);
  await verifyMaterial(r.manifest,r.compiled);same(r.compiled.target,c.target,'CHAIN_OUTPUT_TARGET_MISMATCH');
  const receipt=clone(r.application_receipt),digest=receipt.application_sha256;delete receipt.application_sha256;
  demand(await api.sha(api.canonical(receipt))===digest&&receipt.input_manifest_sha256===hashes[path+'.input_manifest']&&receipt.input_compiled_sha256===c.compiled_sha256&&receipt.output_manifest_sha256===hashes[path+'.manifest']&&receipt.output_compiled_sha256===r.compiled.compiled_sha256&&receipt.ledger_sha256===hashes[path+'.ledger']&&receipt.changed===r.changed,'CHAIN_APPLICATION_RECEIPT_MISMATCH');
  const changed=r.compiled.compiled_sha256!==c.compiled_sha256;demand(r.changed===changed&&r.compiled_changed_requires_new_review===changed&&r.human_review_created===false&&r.clinical_release_permitted===false&&r.event_graph_modified===false,'CHAIN_ROUND_FLAGS_MISMATCH');
  const outputMaterials=[];for(const s of r.compiled.sources){let text='',masking=false;for(const p of s.segments){if(p.disposition==='MASK'||p.disposition==='REVIEW'){if(!masking)text+='[资料按方案遮蔽]';masking=true;}else{text+=p.text;masking=false;}}outputMaterials.push({source_id:s.source_id,source_version:s.source_version,text,sha256:await api.sha(text)});}
  demand(r.diff.doctor_visible_material_changed===(visibleSignature(request.doctor_view)!==api.canonical(outputMaterials.filter(m=>m.text!=='[资料按方案遮蔽]')))&&r.diff.source_scope_changed===(api.canonical([...new Set(c.sources.map(s=>s.source_id))].sort())!==api.canonical([...new Set(r.compiled.sources.map(s=>s.source_id))].sort())),'CHAIN_DIFF_VISIBILITY_OR_SCOPE_MISMATCH');
  const blocking=['unresolved_sources','graph_issues','human_requests','holds'].some(k=>r.ledger[k].length);
  if(review.status==='AUTO_REVIEW_PASS'){demand(!changed&&!blocking&&review.required_actions.length===0&&!r.ledger.operations.some(o=>o.status==='APPLIED')&&!r.compiled.sources.some(s=>s.segments.some(p=>p.disposition==='REVIEW'))&&!review.findings.some(f=>f.status==='OPEN'&&f.severity==='BLOCKING')&&review.dimensions.length===7&&new Set(review.dimensions.map(d=>d.dimension)).size===7&&review.dimensions.every(d=>['RESOLVED','NOT_APPLICABLE'].includes(d.status)&&d.applicability!=='UNKNOWN'),'CHAIN_INVALID_PASS');terminal='AUTO_PASS';}
  else if(blocking||review.required_actions.length||review.status==='AUTO_REVIEW_HOLD')terminal='REVIEW_HOLD';
  else{demand(review.status==='AUTO_REVIEW_REVISE'&&changed,'CHAIN_INVALID_REVISION');const pending=newSourcePending(c,chain.rounds[i-1]);if(current)demand(r.diff.doctor_visible_material_changed===true||r.diff.source_scope_changed===true||pending,'CHAIN_REVISE_REQUIRES_DOCTOR_VISIBLE_OR_SOURCE_SCOPE_CHANGE');const signature=contentSignature(r.compiled);if(seen.has(signature)&&!pending)terminal='REVISION_CYCLE';seen.add(signature);if(terminal===null&&i+1===chain.max_rounds)terminal='BUDGET_EXHAUSTED';}
  m=r.manifest;c=r.compiled;m.sources.forEach(s=>previouslyIncluded.add(s.source_id));
 }
 if(chain.failure){
  const failure=chain.failure,request=failure.request;
  demand(terminal===null&&failure.round_index===chain.rounds.length+1&&chain.rounds.length<chain.max_rounds&&request&&failure.reason_code&&['PROVIDER_REQUEST','RESPONSE_VALIDATION'].includes(failure.stage),'CHAIN_FAILURE_INVALID');
  same(request.target,c.target,'CHAIN_FAILURE_TARGET_MISMATCH');same(request.task_context,chain.task_context,'CHAIN_FAILURE_CONTEXT_MISMATCH');
  same(request.researcher_sources,m.sources.map(s=>({source_id:s.source_id,source_version:s.source_version,sha256:s.sha256,text:s.text})),'CHAIN_FAILURE_SOURCE_MISMATCH');
  same(request.current_dispositions,c.sources.flatMap(s=>s.segments.map(p=>({source_id:s.source_id,start:p.start,end:p.end,disposition:p.disposition,exact_quote:p.text}))),'CHAIN_FAILURE_PARTITION_MISMATCH');
  demand(failure.request_sha256===hashes['chain.failure.request']&&request.response_binding.request_sha256===hashes['chain.failure.request_without_binding']&&request.response_binding.compiled_sha256===c.compiled_sha256&&request.protocol_version===chain.protocol_version,'CHAIN_FAILURE_REQUEST_HASH_MISMATCH');
  await verifyView(request.doctor_view,c);
  const included=new Set(m.sources.map(s=>s.source_id)),candidates=chain.source_registry.filter(s=>!included.has(s.source_id)&&s.origin_scope==='CANDIDATE'&&!previouslyIncluded.has(s.source_id)),excluded=chain.source_registry.filter(s=>!included.has(s.source_id)&&(s.origin_scope!=='CANDIDATE'||previouslyIncluded.has(s.source_id)));
  same(request.candidate_sources.map(s=>s.source_id),candidates.map(s=>s.source_id),'CHAIN_FAILURE_CANDIDATE_SOURCE_SCOPE_MISMATCH');same(request.excluded_sources.map(s=>s.source_id),excluded.map(s=>s.source_id),'CHAIN_FAILURE_EXCLUDED_SOURCE_SCOPE_MISMATCH');
  demand(request.response_binding.protocol_version===chain.protocol_version&&request.response_binding.doctor_view_sha256===request.doctor_view.view_sha256,'CHAIN_FAILURE_RESPONSE_ENVELOPE_MISMATCH');
  if(current){demand(request.review_round.round_index===failure.round_index&&request.review_round.max_rounds===chain.max_rounds,'CHAIN_FAILURE_ROUND_MISMATCH');if(chain.rounds.length===0){demand(request.review_round.is_first_review&&!request.review_round.current_state_produced_by_previous_review&&!request.previous_round,'CHAIN_FAILURE_INITIAL_HISTORY_MISMATCH');}else{const previous=chain.rounds[chain.rounds.length-1];demand(!request.review_round.is_first_review&&request.review_round.current_state_produced_by_previous_review,'CHAIN_FAILURE_CONTINUATION_ATTRIBUTION_MISMATCH');same(request.previous_round.previous_review,previous.review,'CHAIN_FAILURE_PREVIOUS_REVIEW_MISMATCH');same(request.previous_round.operation_receipts,previous.ledger.operations,'CHAIN_FAILURE_PREVIOUS_OPERATION_RECEIPTS_MISMATCH');same(request.previous_round.actual_program_diff,previous.diff,'CHAIN_FAILURE_PREVIOUS_DIFF_MISMATCH');same(request.previous_round.program_execution_receipt,previous.application_receipt,'CHAIN_FAILURE_PREVIOUS_APPLICATION_MISMATCH');demand(request.previous_round.round_index===chain.rounds.length&&request.previous_round.current_compiled_sha256===c.compiled_sha256&&request.previous_round.current_doctor_view_sha256===request.doctor_view.view_sha256,'CHAIN_FAILURE_PREVIOUS_STATE_HASH_MISMATCH');}}
  terminal='EXECUTION_FAILED';
 }
 demand(terminal!==null&&chain.stop_reason===terminal&&chain.state===(terminal==='AUTO_PASS'?'AUTO_REVIEW_PASS':'AUTO_REVIEW_HOLD'),'CHAIN_TERMINAL_STATE_MISMATCH');
 await verifyView(chain.doctor_view,c);
 same(chain.manifest,m,'CHAIN_FINAL_MANIFEST_MISMATCH');same(chain.compiled,c,'CHAIN_FINAL_COMPILED_MISMATCH');
 same(chain.final_binding,{manifest_sha256:hashes['chain.manifest'],compiled_sha256:c.compiled_sha256,doctor_view_sha256:chain.doctor_view.view_sha256,source_registry_sha256:chain.source_registry_sha256,round_count:chain.rounds.length},'CHAIN_FINAL_BINDING_MISMATCH');same(data.validation_receipt.final_binding,chain.final_binding,'CHAIN_REPLAY_FINAL_BINDING_MISMATCH');
 if(chain.task_context.candidate_graph_binding){const b=chain.task_context.candidate_graph_binding,snapshot=chain.task_context.candidate_graph_snapshot;demand(hashes['chain.task_context.candidate_graph_snapshot']===b.graph_sha256&&b.checkpoint_id===c.target.checkpoint_id&&snapshot.events.filter(e=>e.event_id===b.target_event_id).length===1,'CHAIN_CANDIDATE_GRAPH_BINDING_MISMATCH');if(docs.selected_event)same(docs.selected_event,snapshot.events.find(e=>e.event_id===b.target_event_id),'CHAIN_SELECTED_EVENT_MISMATCH');}
 return true;
}
G.MaterialGovernanceReview={validateOpinion};
async function boot(){
 const status=document.getElementById('auto-opinion-error');
 try{
  const data=api.parseJsonUnique(document.getElementById('auto-opinion-data').textContent),ctx=await api.decodeAssignment(api.parseJsonUnique(document.getElementById('assigned-data').textContent));
  await validateOpinion(ctx.assignment,ctx.docs,data);
  const panel=document.createElement('section');panel.id='automated-material-opinion';panel.className='auto-opinion';
  const heading=document.createElement('h2');heading.textContent='自动材料审核与修订历史（非人工结论）';panel.append(heading);
  const warning=document.createElement('p');warning.className='warning';warning.textContent='这里展示材料审核 LLM 的意见和程序执行记录。程序核验整条审核链的身份、摘要和连续性；Python 执行权威操作重放。摘要不能认证人员或临床时间。自动通过不等于独立人工审核、临床正确或医生许可；人工判断须实际人员逐项填写。';panel.append(warning);
  const summary=document.createElement('p');summary.id='auto-opinion-state';show(summary,{state:data.automatic_state,stop_reason:data.stop_reason,human_sample_status:data.human_sample_status});panel.append(summary);
  const list=document.createElement('ol');list.id='auto-opinion-rounds';
  data.chain.rounds.forEach((round,index)=>{
   const item=document.createElement('li'),title=document.createElement('h3');title.textContent='第 '+(index+1)+' 轮 · '+round.review.status;item.append(title);
   for(const [label,value]of [['七维结论',round.review.dimensions],['审核发现',round.review.findings],['LLM 提议的操作',round.review.operations],['程序执行账本',round.ledger.operations],['新旧版本对照',round.diff],['待处理事项',{unresolved_sources:round.ledger.unresolved_sources,graph_issues:round.ledger.graph_issues,human_requests:round.ledger.human_requests,holds:round.ledger.holds,required_actions:round.review.required_actions}]]){
    const detail=document.createElement('details'),name=document.createElement('summary'),pre=document.createElement('pre');name.textContent=label;show(pre,value);detail.append(name,pre);item.append(detail);
   }
   const binding=document.createElement('pre');show(binding,{input_compiled_sha256:round.input_compiled_sha256,output_compiled_sha256:round.compiled.compiled_sha256,changed:round.changed});item.append(binding);list.append(item);
  });
  panel.append(list);document.querySelector('.binding').after(panel);status.hidden=true;
 }catch(e){status.textContent='自动意见未通过整链核验：'+e.message;status.hidden=false;}
}
if(typeof document!=='undefined')boot();
})(globalThis);
