/* Real-Chrome checks for candidate CP first screen and patient navigation. */
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const [input, output] = process.argv.slice(2);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'candidate-cp-chrome-'));
fs.mkdirSync(output, { recursive: true });
const chrome = process.env.REVIEW_CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const port = 9333 + Math.floor(Math.random() * 300);
const child = spawn(chrome, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-address=127.0.0.1', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  pathToFileURL(path.resolve(input)).href,
], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  let socket;
  try {
    let pages;
    for (let attempt = 0; attempt < 50; attempt++) {
      try {
        pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        if (pages.some(page => page.type === 'page')) break;
      } catch {}
      await delay(100);
    }
    const page = pages && pages.find(item => item.type === 'page');
    assert(page, 'Chrome page was not available');
    socket = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    });
    let serial = 0;
    const pending = new Map();
    const errors = [], network = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const handler = pending.get(message.id);
        if (handler) {
          pending.delete(message.id);
          if (message.error) handler.reject(new Error(JSON.stringify(message.error)));
          else handler.resolve(message.result);
        }
      } else if (message.method === 'Runtime.exceptionThrown') {
        errors.push(JSON.stringify(message.params.exceptionDetails));
      } else if (message.method === 'Network.requestWillBeSent' && /^https?:/.test(message.params.request.url)) {
        network.push(message.params.request.url);
      }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++serial;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
      const response = await send('Runtime.evaluate', {
        expression, returnByValue: true, awaitPromise: true,
      });
      if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
      return response.result.value;
    };
    await send('Runtime.enable');
    await send('Network.enable');
    for (let attempt = 0; attempt < 50; attempt++) {
      if (await evaluate("typeof DATA !== 'undefined' && !!document.querySelector('.checkpoint-nav')")) break;
      await delay(100);
    }
    const initial = await evaluate(`(() => {
      const first=[...DATA].sort((a,b)=>a.display_order.index-b.display_order.index)[0];
      document.querySelector('#checkpoint').value=first.checkpoint_id;
      document.querySelector('#checkpoint').dispatchEvent(new Event('change'));
      return first.checkpoint_id;
    })()`);
    const visited = [initial];
    const count = await evaluate('DATA.length');
    assert(count > 1, 'This check requires multiple candidate CPs');
    const firstScreen = await evaluate(`(() => {
      const header=document.querySelector('.task-header');
      const original=document.querySelector('#workflow-original');
      const actions=document.querySelector('.candidate-actions');
      const nav=document.querySelector('.checkpoint-nav');
      return {
        originalVisible:!original.hidden && original.getClientRects().length>0,
        sourceCount:original.querySelectorAll('.source').length,
        maskSourceCount:document.querySelectorAll('#workflow-mask .source').length,
        targetCount:original.querySelectorAll('.target-source').length,
        targetExcludedCount:document.querySelectorAll('.target-cutoff-warning').length,
        contextCount:original.querySelectorAll('.context-source').length,
        contextText:[...original.querySelectorAll('.context-source pre')].map(node=>node.textContent.trim()).join('\\n'),
        heading:actions.querySelector('h3').textContent,
        candidateCount:actions.querySelectorAll('.candidate-action').length,
        actionText:actions.textContent,
        failureVisible:!!actions.querySelector('.technical-failure[role="alert"]'),
        headerText:header.textContent,
        includedSourceIds:[...original.querySelectorAll('.source .meta')].map(node=>
          (node.textContent.match(/来源\s+([^ ·]+)/)||[])[1]).filter(Boolean),
        navAria:nav.getAttribute('aria-label'),
        previousLabel:nav.querySelectorAll('button')[0].textContent,
        positionLabel:nav.querySelector('span').textContent,
        nextLabel:nav.querySelectorAll('button')[1].textContent,
        materialText:document.querySelector('#material').textContent,
        hasGlobalWarning:!document.querySelector('.notice,.migration-summary'),
        taskColumnCount:getComputedStyle(document.querySelector('.task-grid')).gridTemplateColumns.split(' ').length,
        taskCellsFit:[...document.querySelectorAll('.task-grid>div')].every(cell=>
          cell.scrollWidth<=cell.clientWidth+1 &&
          [...cell.children].every(field=>field.scrollWidth<=field.clientWidth+1)),
      };
    })()`);
    assert.equal(firstScreen.originalVisible, true, 'Visible-source panel is not the first screen');
    assert(firstScreen.sourceCount >= 1, 'First screen must include at least one visible source');
    assert.equal(firstScreen.sourceCount, firstScreen.maskSourceCount, 'First screen omitted included_sources');
    assert(firstScreen.targetCount >= 1 || firstScreen.targetExcludedCount >= 1,
      'Target source is neither labelled nor explicitly excluded as created after cutoff');
    if (firstScreen.contextCount > 0) {
      assert(firstScreen.contextText.length > 0, 'Context source text is blank on the first screen');
    }
    const beforeAcquisitionGate = firstScreen.headerText.includes('已移至“研究原文”页');
    if (!beforeAcquisitionGate) {
      assert(firstScreen.heading.includes('Agent候选动作（不是答案/Reference）'));
      assert(firstScreen.headerText.indexOf('评价时点') < firstScreen.headerText.indexOf('Agent候选动作'));
    }
    assert(firstScreen.candidateCount > 0 || firstScreen.failureVisible
      || beforeAcquisitionGate,
      'Candidate actions, technical-failure warning, and before-acquisition research gate are all absent');
    if (firstScreen.candidateCount > 0) {
      for (const label of ['action_type', 'target', '理由']) {
        assert(firstScreen.actionText.includes(label), `Candidate action lacks ${label}`);
      }
    }
    assert.equal(firstScreen.navAria, '本患者显示顺序节点切换');
    assert(firstScreen.previousLabel.includes('本患者上一 CP'));
    assert(firstScreen.positionLabel.includes('本患者 CP'));
    assert(firstScreen.nextLabel.includes('本患者下一 CP'));
    assert.equal(await evaluate("document.querySelectorAll('[data-navigation^=\"episode-\"]').length"), 0,
      'Duplicate Episode navigation remains visible');
    assert(!firstScreen.headerText.includes('全病例'), 'Navigation implies a cross-patient scope');
    assert.equal(firstScreen.hasGlobalWarning, true, 'Global warning or migration summary remains visible');
    for (const label of ['下一关键手术步骤', '允许时间精度', '知识版本', '实际 included source IDs']) {
      assert(firstScreen.headerText.includes(label), `Task header lacks ${label}`);
    }
    for (const sourceId of firstScreen.includedSourceIds) {
      assert(firstScreen.headerText.includes(sourceId), `Task header omits actual included source ${sourceId}`);
    }
    assert.equal(firstScreen.taskColumnCount, 3, 'Desktop task header is not a three-column grid');
    assert.equal(firstScreen.taskCellsFit, true, 'Task header text overflows its grid cell');
    const firstScreenshot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(path.join(output, 'first-screen-desktop.png'), Buffer.from(firstScreenshot.data, 'base64'));
    const actionBehavior = await evaluate(`(() => {
      const form=document.querySelector('.action-form'),status=form.querySelector('[name="plan_status"]');
      const mode=form.querySelector('[name="mode"]'),rationale=form.querySelector('[name="rationale"]');
      const before=exportData();
      mode.value='ADVANCE';rationale.value='浏览器合成检查';status.value='NO_NEW_ACTION';
      status.dispatchEvent(new Event('change'));
      const noNewHidden=form.querySelector('.actions').hidden&&document.querySelector('#add-action').hidden;
      const noNew=response(form);
      status.value='ABSTAIN';status.dispatchEvent(new Event('change'));
      const abstain=response(form),abstainModeDisabled=mode.disabled;
      status.value='ACTIONS_PROPOSED';status.dispatchEvent(new Event('change'));
      const row=form.querySelector('.action-row');
      row.querySelector('[name="action_type"]').value='BROWSER_TEST';
      row.querySelector('[name="target"]').value='合成目标';
      const proposed=response(form),actionsVisible=!form.querySelector('.actions').hidden;
      localStorage.setItem(key(current.checkpoint_id),JSON.stringify(noNew));
      const saved=exportData(),savedResponses=saved.responses.length+saved.non_primary_p2_review_responses.length;
      localStorage.setItem(key(current.checkpoint_id),JSON.stringify({...noNew,material_manifest_sha256:'stale-material'}));
      const stale=exportData(),staleResponses=stale.responses.length+stale.non_primary_p2_review_responses.length;
      localStorage.setItem(key(current.checkpoint_id),JSON.stringify(noNew));
      return {beforeResponses:before.responses.length+before.non_primary_p2_review_responses.length,
        beforeUnsubmitted:before.unsubmitted_checkpoints.length,noNewHidden,noNewActions:noNew.actions.length,
        abstainActions:abstain.actions.length,abstainHasMode:Object.hasOwn(abstain,'mode'),abstainModeDisabled,
        actionsVisible,timingOptional:!Object.hasOwn(proposed.actions[0],'timing'),
        responseStatus:proposed.response_status,savedResponses,savedUnsubmitted:saved.unsubmitted_checkpoints.length,
        staleResponses,staleUnsubmitted:stale.unsubmitted_checkpoints.length};
    })()`);
    assert.equal(actionBehavior.beforeResponses, 0, 'Unsubmitted forms entered exported responses');
    assert.equal(actionBehavior.beforeUnsubmitted, count, 'Unsubmitted CPs were not listed separately');
    assert.equal(actionBehavior.noNewHidden, true, 'NO_NEW_ACTION did not hide action rows');
    assert.equal(actionBehavior.noNewActions, 0, 'NO_NEW_ACTION exported actions');
    assert.equal(actionBehavior.abstainActions, 0, 'ABSTAIN exported actions');
    assert.equal(actionBehavior.abstainHasMode, false, 'ABSTAIN became a clinical Mode');
    assert.equal(actionBehavior.abstainModeDisabled, true, 'ABSTAIN did not disable clinical Mode');
    assert.equal(actionBehavior.actionsVisible, true, 'ACTIONS_PROPOSED did not restore action rows');
    assert.equal(actionBehavior.timingOptional, true, 'Blank optional timing was exported');
    assert.equal(actionBehavior.responseStatus, 'SUBMITTED');
    assert.equal(actionBehavior.savedResponses, 1, 'Submitted response was not exported');
    assert.equal(actionBehavior.savedUnsubmitted, count - 1);
    assert.equal(actionBehavior.staleResponses, 0, 'Stale material hash entered exported responses');
    assert.equal(actionBehavior.staleUnsubmitted, count, 'Stale material response was not marked unsubmitted');
    for (let index = 1; index < count; index++) {
      const button = await evaluate(`(() => {
        const button=document.querySelectorAll('.checkpoint-nav')[0].querySelectorAll('button')[1];
        return {disabled:button.disabled,target:button.dataset.goto};
      })()`);
      assert.equal(button.disabled, false, `next disabled at display index ${index}`);
      await evaluate("document.querySelectorAll('.checkpoint-nav')[0].querySelectorAll('button')[1].click()");
      const selected = await evaluate("document.querySelector('#checkpoint').value");
      assert.equal(selected, button.target);
      visited.push(selected);
    }
    assert.equal(new Set(visited).size, count, 'Patient navigation did not visit every CP exactly once');
    const last = await evaluate(`(() => {
      const button=document.querySelectorAll('.checkpoint-nav')[0].querySelectorAll('button')[1];
      return {disabled:button.disabled,label:button.textContent};
    })()`);
    assert.equal(last.disabled, true);
    for (let index = count - 1; index > 0; index--) {
      await evaluate("document.querySelectorAll('.checkpoint-nav')[0].querySelectorAll('button')[0].click()");
      assert.equal(await evaluate("document.querySelector('#checkpoint').value"), visited[index - 1]);
    }
    await evaluate("document.querySelector('[data-workflow=relations]').click()");
    assert.equal(await evaluate("document.querySelector('#workflow-relations').hidden"), false);
    const screenshot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(path.join(output, 'navigation-desktop.png'), Buffer.from(screenshot.data, 'base64'));
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true);
    assert.deepEqual(errors, []);
    assert.deepEqual(network, []);
    fs.writeFileSync(path.join(output, 'browser-receipt.json'), JSON.stringify({
      status: 'PASS', private_research_preview: true, doctor_release: false,
      candidate_count: count, visited_checkpoint_ids: visited,
      checks: ['real_chrome_first_screen_context_visible', 'real_chrome_candidate_actions_or_failure_visible',
        'real_chrome_patient_labels', 'real_chrome_patient_next_visits_all',
        'real_chrome_patient_previous_returns_all', 'relations_tab_click', 'desktop_task_header_three_columns_without_overflow',
        'task_header_upstream_fields_and_actual_included_sources', 'conditional_action_rows_and_optional_timing',
        'no_new_action_and_technical_abstain_export', 'manifest_bound_storage_and_unsubmitted_export',
        'global_warning_and_migration_summary_removed', 'mobile_no_horizontal_overflow'],
      page_errors: errors, network_requests: network,
    }, null, 2) + '\n');
    console.log(JSON.stringify({ status: 'PASS', candidates: count, visited }));
  } finally {
    if (socket) socket.close();
    const exited = new Promise(resolve => child.once('exit', resolve));
    child.kill('SIGTERM');
    await Promise.race([exited, delay(3000)]);
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
