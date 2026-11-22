// 仅针对独立 QA 栈执行，使用本机固定响应验证真实 API、模型协议和 Worker。
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { createServer } = require('node:http');
const { dirname, join } = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const projectFixture = require('./team_project_fixture.cjs');

const [composePath, credentialsPath, option] = process.argv.slice(2);
assert(composePath && credentialsPath, '请传入独立 QA Compose 与凭据 JSON 路径');
assert(!option || option === '--restart', '可选参数仅支持 --restart');
const manifest = JSON.parse(readFileSync(composePath, 'utf8'));
assert.match(manifest.name, /^agent-platform-document-qa(?:-[a-z0-9]+)?$/);
assert(!manifest.services.backend.container_name, 'QA 后端必须使用 Compose 隔离容器名');
const runtimeEnabled = Boolean(manifest.services.project_verifier && manifest.services.backend.environment.AGENT_PROJECT_VERIFIER_DIR);
const credentials = JSON.parse(readFileSync(credentialsPath, 'utf8'));
const browserChecks = process.env.DOCUMENT_QA_PLAYWRIGHT ? require('./document_intelligence_browser.cjs') : null;
const origin = new URL(credentials.backend || credentials.backendUrl || 'http://127.0.0.1:8002');
assert(['127.0.0.1', 'localhost'].includes(origin.hostname) && origin.port !== '8000' && origin.port,
  '测试仅连接独立本地 QA 后端');
assert(manifest.services.backend.ports.includes(`127.0.0.1:${origin.port}:8000`));

const stamp = randomUUID().slice(0, 8);
const tasks = [
  { id: 'research', role: '资料员', title: '梳理迁移事实', brief: '核对迁移目标 QA_RESEARCH', depends_on: [] },
  { id: 'risk', role: '风险分析员', title: '独立评估风险', brief: '评估迁移风险 QA_RISK', depends_on: [] },
  { id: 'review', role: '成果评审员', title: '交叉检验两份结果', brief: '验证结论 QA_REVIEW', depends_on: ['research', 'risk'] },
];
const outputs = { research: '核对证据 QA_R_EVIDENCE', risk: '风险证据 QA_K_EVIDENCE', review: '评审证据 QA_V_EVIDENCE' };
const calls = [];
const failures = [];
const runNames = {
  standard: 'QA_TEAM_STANDARD', complete: 'QA_TEAM_COMPLETE', partial: 'QA_TEAM_PARTIAL',
  cancelled: 'QA_TEAM_CANCEL', restarted: 'QA_TEAM_RESTART', document: 'QA_TEAM_DOCUMENT', deep: 'QA_TEAM_DEPTH',
  delivery: 'QA_LONG_DELIVERY', continued: 'QA_LONG_CONTINUED',
};
const entered = new Map();

function deferred() {
  let resolve;
  const done = new Promise(accept => { resolve = accept; });
  return { done, resolve };
}

const parallel = deferred();

function readContent(content) {
  return Array.isArray(content) ? content.map(part => part.text || '').join('\n') : content || '';
}

function writeCompletion(body, response, content, finishReason = 'stop', toolCalls = []) {
  const metadata = { id: `qa-team-${randomUUID()}`, model: body.model, created: Math.floor(Date.now() / 1000) };
  if (response.destroyed) return;
  if (body.stream) {
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    const delta = { role: 'assistant', content,
      ...(toolCalls.length ? { tool_calls: toolCalls.map((call, index) => ({ ...call, index })) } : {}) };
    for (const [part, finish_reason] of [[delta, null], [{}, finishReason]]) {
      response.write(`data: ${JSON.stringify({ ...metadata, object: 'chat.completion.chunk',
        choices: [{ index: 0, delta: part, finish_reason }] })}\n\n`);
    }
    response.end('data: [DONE]\n\n');
  } else {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ ...metadata, object: 'chat.completion',
      choices: [{ index: 0, finish_reason: finishReason, message: { role: 'assistant', content,
        ...(toolCalls.length ? { tool_calls: toolCalls } : {}) } }],
      usage: { prompt_tokens: 32, completion_tokens: 16, total_tokens: 48 },
    }));
  }
}

function waitForClose(response) {
  return new Promise(resolve => response.once('close', resolve));
}

function documentReply(operation, user) {
  const data = JSON.parse(user);
  if (operation === 'prepare_profile' || operation === 'write_section') {
    assert(data.team_advice?.includes(outputs.research) && data.team_advice.includes(outputs.review),
      '文档编写未获得团队的分工成果');
    assert(!data.sources?.some(source => JSON.stringify(source).includes(outputs.research)), '团队建议变成了资料来源');
  }
  switch (operation) {
    case 'prepare_profile':
      return { name: '库存迁移评估', document_type: 'report', sections: [{ id: 'assessment', title: '迁移结论', order: 1 }] };
    case 'identify_missing':
      return { title: '库存迁移评估', field_values: {} };
    case 'write_section':
      return { blocks: [{ type: 'paragraph', text: '迁移安排包括试迁移、业务核对及回滚演练。', source_ids: ['request'] }] };
    case 'review_draft':
      return { issues: [] };
    default:
      throw Error(`未预期的文档阶段 ${operation}`);
  }
}

const model = createServer(async (request, response) => {
  try {
    assert.equal(request.url, '/v1/chat/completions');
    assert.equal(request.method, 'POST');
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    assert.equal(body.model, `team-qa-${stamp}`);
    const system = body.messages.filter(row => row.role === 'system').map(row => readContent(row.content)).join('\n');
    const user = readContent(body.messages.findLast(row => row.role === 'user')?.content);
    const project = projectFixture.projectReply(system, user);
    if (project !== undefined) {
      if (typeof project === 'string') return writeCompletion(body, response, project);
      return writeCompletion(body, response, project.content, project.finishReason);
    }
    const name = body.messages.toReversed()
      .map(row => Object.values(runNames).find(marker => readContent(row.content).includes(marker))).find(Boolean);
    if (name === runNames.continued) {
      const write = (id, file_path, content) => writeCompletion(body, response, '', 'tool_calls', [{
        type: 'function', id, function: { name: 'write_file', arguments: JSON.stringify({ file_path, content }) },
      }]);
      const receipts = new Set(body.messages.filter(row => row.role === 'tool').map(row => row.tool_call_id));
      if (receipts.has('qa-resume-facts')) {
        return writeCompletion(body, response, '延续原任务，已补齐数据文件。');
      }
      if (user.trim() === '继续') {
        assert.match(system, /沿用可用内容/);
        return write('qa-resume-facts', '/workspace/resume-facts.json', '{"total":18}');
      }
      if (!receipts.has('qa-resume-report')) {
        return write('qa-resume-report', '/workspace/resume-report.md', '# 续做报告\n\n合计 18 件。');
      }
      response.writeHead(500, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: { message: '模拟模型中断' } }));
      return;
    }
    if (name === runNames.delivery) {
      const write = (id, file_path, content) => writeCompletion(body, response, '', 'tool_calls', [{
        type: 'function', id, function: { name: 'write_file', arguments: JSON.stringify({ file_path, content }) },
      }]);
      const receipts = new Set(body.messages.filter(row => row.role === 'tool').map(row => row.tool_call_id));
      if (!receipts.has('qa-durable-report')) {
        calls.push({ name, operation: 'report' });
        return write('qa-durable-report', '/workspace/audit-report.md', '# 盘点报告\n\n合计 42 件。');
      }
      if (!receipts.has('qa-durable-facts')) {
        if (!user.includes('facts.json') || !user.includes('完成条件')) {
          calls.push({ name, operation: 'premature' });
          return writeCompletion(body, response, '报告初稿已写，所有工作已结束。');
        }
        calls.push({ name, operation: 'supplement' });
        if (option === '--restart' && !entered.has(name)) {
          entered.set(name, 'supplement');
          await waitForClose(response);
          return;
        }
        return write('qa-durable-facts', '/workspace/facts.json', '{"total":42}');
      }
      calls.push({ name, operation: 'final' });
      return writeCompletion(body, response, '已核对 42 件库存。报告与数据均可下载。');
    }
    const documentOperation = /^文档工作流 (\w+)/.exec(system)?.[1];
    if (documentOperation) {
      calls.push({ name: runNames.document, operation: documentOperation });
      return writeCompletion(body, response, JSON.stringify(documentReply(documentOperation, user)));
    }
    if (name === runNames.deep && system.includes('团队复核。')) {
      const data = JSON.parse(user);
      assert.equal(data.acceptance.length, 2);
      const revised = Boolean(data.previous_review);
      calls.push({ name, operation: revised ? 'recheck' : 'review' });
      if (revised) assert.match(user, /QA_CORRECTED_EVIDENCE/);
      return writeCompletion(body, response, JSON.stringify({ checks: [
        { criterion: 1, status: revised ? 'supported' : 'gap',
          evidence: revised ? '工具上线阈值已明确' : '风险方案缺少量化回滚阈值', task_id: revised ? null : 'risk' },
        { criterion: 2, status: 'unverified', evidence: '暂无生产备份恢复记录', task_id: null },
      ] }));
    }
    if (name === runNames.deep && system.includes('团队返工。')) {
      const data = JSON.parse(user);
      assert.equal(data.task.id, 'risk');
      assert.match(user, /缺少量化回滚阈值/);
      calls.push({ name, operation: 'rework' });
      return writeCompletion(body, response, `${outputs.risk} QA_CORRECTED_EVIDENCE 回滚触发条件及验证步骤`);
    }
    const operation = system.includes('团队规划。') ? 'plan' : system.includes('团队成员。') ? 'member'
      : system.includes('团队综合。') ? 'synthesize' : 'standard';
    const member = operation === 'member' ? tasks.find(task => system.includes(task.role))?.id : undefined;
    calls.push({ name, operation, member });

    if (operation === 'standard') {
      assert.equal(name, runNames.standard, '团队运行进入了普通聊天模型');
      return writeCompletion(body, response, '单助手问候 QA_STANDARD_RESULT');
    }
    if (operation === 'plan') {
      assert(name && name !== runNames.standard, '没有显式启用团队却调用了规划模型');
      assert(!body.tools?.length, '规划阶段不调用业务工具');
      const acceptance = name === runNames.deep ? ['写明可核查的回滚阈值', '核对生产备份恢复证据'] : undefined;
      return writeCompletion(body, response, JSON.stringify({ tasks, acceptance }));
    }
    if (operation === 'member') {
      assert(member, '团队成员提示缺少已分派角色');
      const spec = tasks.find(task => task.id === member);
      assert(user.includes(spec.brief), '成员未收到自己的任务说明');
      assert(!body.tools?.some(tool => ['write_file', 'edit_file', 'execute', 'python_exec', 'task']
        .includes(tool.function?.name)), '成员拥有写工具或可继续派生团队');
      if (name === runNames.partial && member === 'risk') {
        response.writeHead(400, { 'content-type': 'application/json' });
        return response.end(JSON.stringify({ error: { message: '固定的 QA 成员失败', type: 'invalid_request_error' } }));
      }
      if (name === runNames.complete && member !== 'review') {
        // 首名成员只有在第二名成员也真正发起模型调用后才放行。
        if (calls.filter(call => call.name === name && call.operation === 'member').length === 2) parallel.resolve();
        await Promise.race([parallel.done, sleep(4500, 'not_parallel', { ref: false })]).then(result => {
          assert.notEqual(result, 'not_parallel', '两个无依赖角色没有并行调用模型');
        });
      }
      if (member === 'review') {
        assert(user.includes(outputs.research), '依赖角色未收到资料员成果');
        if (name !== runNames.partial) assert(user.includes(outputs.risk), '依赖角色未收到风险分析成果');
      }
      if (name === runNames.cancelled || (name === runNames.restarted && member === 'review')) {
        const count = calls.filter(call => call.name === name && call.operation === 'member' && call.member === member).length;
        if (name === runNames.cancelled || count === 1) {
          entered.set(name, member);
          await waitForClose(response);
          return;
        }
      }
      return writeCompletion(body, response, outputs[member]);
    }
    assert.notEqual(name, runNames.cancelled, '取消后仍启动汇总');
    assert(user.includes(outputs.research), '团队负责人汇总前未收到成功成员成果');
    if (name !== runNames.partial) {
      assert(user.includes(outputs.risk) && user.includes(outputs.review), '团队负责人未收到完整协作成果');
    }
    return writeCompletion(body, response, name === runNames.partial
      ? '部分任务未成功，汇总已核对的迁移要点 QA_PARTIAL_RESULT' : '团队汇总的迁移结论 QA_TEAM_RESULT');
  } catch (error) {
    failures.push(error);
    if (!response.destroyed && !response.headersSent) response.writeHead(500).end();
  }
});

let token;
async function api(path, { method = 'GET', json, status, headers = {} } = {}) {
  const response = await fetch(new URL(path, origin), {
    method, body: json === undefined ? undefined : JSON.stringify(json),
    headers: { ...(token && { authorization: `Bearer ${token}` }),
      ...(json !== undefined && { 'content-type': 'application/json' }), ...headers },
    signal: AbortSignal.timeout(90000),
  });
  if (!(status === undefined ? response.ok : response.status === status)) {
    throw Error(`${method} ${path} 返回 ${response.status} ${(await response.text()).slice(0, 350)}`);
  }
  const type = response.headers.get('content-type') || '';
  if (response.status === 204) return null;
  if (type.includes('text/event-stream')) {
    return (await response.text()).split('\n').filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
  }
  if (type.includes('application/json')) return response.json();
  return Buffer.from(await response.arrayBuffer());
}

async function until(read, condition, description, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let value;
  do {
    value = await read();
    if (condition(value)) return value;
    if (failures.length) throw failures[0];
    await sleep(150);
  } while (Date.now() < deadline);
  throw Error(`${description}：${JSON.stringify(value)?.slice(0, 500)}`);
}

const events = (run, seq) => api(run.events_url, { headers: seq ? { 'Last-Event-ID': String(seq) } : {} });
const createRun = (body, key = randomUUID(), status) => api('/api/v2/runs', {
  method: 'POST', json: { use_memory: false, ...body }, headers: { 'Idempotency-Key': key }, status,
});

async function finished(run, expected, timeout) {
  const terminal = new Set(['completed', 'failed', 'cancelled', 'limit_exceeded', 'needs_reconciliation']);
  const row = await until(() => api(`/api/v2/runs/${run.run_id}`), row => terminal.has(row.status), 'Run 未到达终态', timeout);
  if (failures.length) throw failures[0];
  assert.equal(row.status, expected, JSON.stringify(row).slice(0, 800));
  return row;
}

async function sendFromBrowser(ui, request, title) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${request.conversation_id}`);
  await page.getByLabel('当前会话', { exact: true }).locator('.thread-heading')
    .getByText(title, { exact: true }).waitFor();
  const teamToggle = page.getByRole('switch', { name: '智能体团队', exact: true });
  assert.equal(await teamToggle.getAttribute('aria-checked'), 'false');
  if (request.team_mode) {
    await teamToggle.click();
    assert.equal(await teamToggle.getAttribute('aria-checked'), 'true');
  }
  const created = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v2/runs'
    && response.request().method() === 'POST');
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  await composer.fill(request.message);
  await composer.press('Enter');
  const response = await created;
  assert.equal(Boolean(response.request().postDataJSON().team_mode), Boolean(request.team_mode));
  assert.equal(response.status(), 200, await response.text());
  return response.json();
}

async function verifyBrowser(ui, run) {
  const card = ui.page.locator(`[data-run-id="${run.run_id}"]`).getByLabel('智能体团队进度', { exact: true });
  await card.locator('[data-phase="completed"]').waitFor();
  const review = card.locator('details').filter({ hasText: '交叉检验两份结果' });
  await review.locator('summary').click();
  await review.getByText('梳理迁移事实', { exact: true }).waitFor();
  await review.getByText(outputs.review, { exact: true }).waitFor();
  await ui.page.screenshot({ path: join(dirname(credentialsPath), 'team-mode-e2e.png'), animations: 'disabled' });
  await ui.page.reload();
  await card.locator('[data-phase="completed"]').waitFor();
  await card.getByText('成果评审员', { exact: true }).waitFor();
}

function docker(...args) {
  const result = spawnSync('docker', ['compose', '-f', composePath, ...args], { timeout: 30000 });
  assert.equal(result.status, 0, result.stderr?.toString());
}

async function main() {
  const username = credentials.username || manifest.services.backend.environment.ADMIN_USERNAME;
  const password = credentials.password || manifest.services.backend.environment.ADMIN_PASSWORD;
  token = (await api('/api/v1/auth/login', { method: 'POST', json: { username, password } })).access_token;
  const provider = await api('/api/v1/models/providers', { method: 'POST', json: {
    name: `team-qa-${stamp}`, display_name: `团队验收 ${stamp}`, provider_type: 'openai_compatible',
    base_url: `http://host.docker.internal:${model.address().port}/v1`, api_key: 'qa-fixture',
  } });
  const config = await api('/api/v1/models/configs', { method: 'POST', json: {
    provider_id: provider.id, name: `team-qa-${stamp}`, display_name: `团队验收 ${stamp}`, model_type: 'chat',
    supports_function_call: true, supports_streaming: true, max_tokens: 1500, extra_params: { max_retries: 0 },
  } });
  const assistant = await api('/api/v2/assistants', { method: 'POST', json: {
    name: `团队验收 ${stamp}`, capability_ids: [], model_config_id: config.id, max_iterations: 16,
  } });
  const conversationIds = [];
  const documents = [];
  const runs = [];
  let ui;
  const newRequest = async name => {
    const row = await api('/api/v2/conversations', { method: 'POST', json: {
      assistant_id: assistant.id, title: `${name} ${stamp}`,
    } });
    conversationIds.push(row.id);
    return { conversation_id: row.id, message: `${name} 请评估库存系统迁移方案并核查风险。` };
  };
  const begin = async (...args) => {
    const run = await createRun(...args);
    runs.push(run);
    return run;
  };
  try {
    const normal = await newRequest(runNames.standard);
    const key = randomUUID();
    const standard = await begin(normal, key);
    const explicitFalse = await createRun({ ...normal, team_mode: false }, key);
    assert.equal(explicitFalse.run_id, standard.run_id, '关闭与省略应具有相同幂等语义');
    assert.equal(explicitFalse.idempotent_reuse, true);
    await createRun({ ...normal, team_mode: true }, key, 409);
    await createRun({ ...normal, team_mode: 'true' }, randomUUID(), 422);
    await finished(standard, 'completed');
    const soloEvents = await events(standard);
    assert(!soloEvents.some(event => event.event === 'team.updated'));
    assert.match(soloEvents.find(event => event.event === 'run.completed').data.final_text, /QA_STANDARD_RESULT/);

    const completeRequest = { ...await newRequest(runNames.complete), team_mode: true };
    let complete;
    if (browserChecks) {
      const frontend = credentials.frontend || credentials.frontendUrl || 'http://127.0.0.1:18080';
      const browserOrigin = new URL(frontend);
      assert(['127.0.0.1', 'localhost'].includes(browserOrigin.hostname) && browserOrigin.port);
      assert(manifest.services.frontend.ports.includes(`127.0.0.1:${browserOrigin.port}:80`));
      ui = await browserChecks.openBrowser({ username, password }, frontend);
      complete = await sendFromBrowser(ui, completeRequest, `${runNames.complete} ${stamp}`);
      runs.push(complete);
    } else {
      complete = await begin(completeRequest);
    }
    assert.equal((await finished(complete, 'completed')).execution_context.team_mode, true);
    const stream = await events(complete);
    const team = stream.filter(event => event.event === 'team.updated');
    assert(team.length >= 4);
    for (const phase of ['planning', 'working', 'synthesizing', 'completed']) {
      assert(team.some(event => event.data.phase === phase), `未发布团队阶段 ${phase}`);
    }
    const lastTeam = team.at(-1);
    assert.equal(lastTeam.data.phase, 'completed');
    assert.deepEqual(lastTeam.data.tasks.map(task => task.id).sort(), tasks.map(task => task.id).sort());
    for (const task of lastTeam.data.tasks) {
      assert.equal(task.status, 'completed');
      assert.deepEqual(task.depends_on, tasks.find(spec => spec.id === task.id).depends_on);
      assert(task.result.includes(outputs[task.id]));
    }
    const teamwork = calls.filter(call => call.name === runNames.complete);
    assert.equal(teamwork.length, 5, '一个规划、三个成员和一个汇总应各调用一次模型');
    assert.equal(teamwork[0].operation, 'plan');
    assert.deepEqual(teamwork.slice(1, 3).map(call => call.member).sort(), ['research', 'risk']);
    assert.equal(teamwork[3].member, 'review');
    assert.equal(teamwork[4].operation, 'synthesize');
    const finalText = stream.find(event => event.event === 'run.completed').data.final_text;
    assert.match(finalText, /QA_TEAM_RESULT/);
    assert.deepEqual(await events(complete, team[1].sequence), stream.filter(event => event.sequence > team[1].sequence));
    await until(() => api(`/api/v2/conversations/${complete.conversation_id}/messages`),
      row => row.items.some(message => message.role === 'assistant' && message.source_run_id === complete.run_id
        && message.content.includes(finalText)), '团队总结未归档到对话');
    if (ui) await verifyBrowser(ui, complete);

    // 团队设置仅作用于指定 Run，同一对话的下一次请求仍可默认走单助手。
    const next = await begin({ conversation_id: complete.conversation_id, message: `${runNames.standard} 你好` });
    await finished(next, 'completed');
    assert(!(await events(next)).some(event => event.event === 'team.updated'));

    const documentTask = await begin({ ...await newRequest(runNames.document),
      message: `${runNames.document} 请生成 Word 版库存迁移评估。实施范围确定为试迁移、业务核对及回滚演练。`,
      team_mode: true, task_mode: 'document_generate',
    });
    await finished(documentTask, 'completed');
    const documentEvents = await events(documentTask);
    const artifact = documentEvents.find(event => event.event === 'artifact.created');
    assert(artifact?.data.document_id && artifact.data.download_url, '团队参与的文档任务未生成真实文件');
    documents.push(artifact.data.document_id);
    const teamInDocument = documentEvents.findLast(event => event.event === 'team.updated');
    assert(teamInDocument?.data.phase === 'completed' && teamInDocument.sequence < artifact.sequence);
    assert(calls.some(call => call.name === runNames.document && call.operation === 'write_section'));
    const word = await api(artifact.data.download_url);
    assert.equal(word.subarray(0, 2).toString(), 'PK');

    const deep = await begin({ ...await newRequest(runNames.deep), team_mode: true,
      message: `${runNames.deep} 请深入评估迁移方案，交叉核对回滚阈值与生产备份恢复记录。` });
    await finished(deep, 'failed');
    const deepEvents = await events(deep);
    const deepPhases = deepEvents.filter(event => event.event === 'team.updated').map(event => event.data.phase);
    assert(deepPhases.includes('verifying') && deepPhases.includes('repairing'));
    const reviewCalls = calls.filter(call => call.name === runNames.deep && ['review', 'rework', 'recheck'].includes(call.operation));
    assert.deepEqual(reviewCalls.map(call => call.operation), ['review', 'rework', 'recheck']);
    assert.match(deepEvents.at(-1).data.final_text, /暂无生产备份恢复记录/);
    assert.equal(deepEvents.at(-1).event, 'run.failed');

    const deliveryRequest = { ...await newRequest(runNames.delivery),
      message: runNames.delivery + ' 核对库存为 42 件，保存为 audit-report.md 和 facts.json 两个文件。' };
    const deliveryRun = ui ? await sendFromBrowser(ui, deliveryRequest, runNames.delivery + ' ' + stamp)
      : await begin(deliveryRequest);
    if (ui) runs.push(deliveryRun);
    if (option === '--restart') {
      await until(() => entered.get(runNames.delivery), phase => phase === 'supplement', '长任务未发起补充执行');
      docker('kill', '--signal', 'SIGKILL', 'backend');
      docker('start', 'backend');
      await until(() => api('/api/health').catch(() => null), Boolean, '长任务接管前服务未恢复');
    }
    await finished(deliveryRun, 'completed', 90000);
    const deliveryEvents = await events(deliveryRun);
    const report = deliveryEvents.at(-1).data.summary.verification;
    assert.equal(report.status, 'passed');
    assert.equal(report.fix_rounds, 1);
    assert.equal(deliveryEvents.at(-1).data.final_text, '已核对 42 件库存。报告与数据均可下载。');
    assert.equal(calls.filter(call => call.name === runNames.delivery && call.operation === 'report').length, 1);
    assert.equal(calls.filter(call => call.name === runNames.delivery && call.operation === 'supplement').length, option === '--restart' ? 2 : 1);
    const facts = report.artifacts.find(row => row.name === 'facts.json');
    assert(facts?.download_url);
    assert.deepEqual(await api(facts.download_url), { total: 42 });
    if (ui) {
      await ui.page.reload();
      const card = ui.page.locator('[data-run-id="' + deliveryRun.run_id + '"]').getByLabel('交付验收', { exact: true });
      const button = card.getByRole('button', { name: '下载 facts.json', exact: true });
      await button.waitFor();
      const transfer = ui.page.waitForEvent('download');
      await button.click();
      assert.equal((await transfer).suggestedFilename(), 'facts.json');
      await ui.page.screenshot({ path: join(dirname(credentialsPath), 'long-task-delivery-e2e.png'), animations: 'disabled' });
    }

    const interrupted = await begin({
      ...await newRequest(runNames.continued),
      message: runNames.continued + ' 分阶段核对库存并分别保存为 resume-report.md 和 resume-facts.json 两个文件。',
    });
    await finished(interrupted, 'failed');
    const prior = (await events(interrupted)).at(-1).data.summary.verification;
    assert.equal(prior.stop_reason, 'execution');
    assert.deepEqual(prior.artifacts.map(row => row.path), ['resume-report.md']);
    const continuedRequest = { conversation_id: interrupted.conversation_id, message: '继续' };
    const continued = ui
      ? await sendFromBrowser(ui, continuedRequest, runNames.continued + ' ' + stamp)
      : await begin(continuedRequest);
    if (ui) runs.push(continued);
    await finished(continued, 'completed');
    const continuedEvents = await events(continued);
    const continuedFiles = continuedEvents.at(-1).data.summary.verification.artifacts;
    assert.deepEqual(continuedFiles.map(row => row.path).sort(), ['resume-facts.json', 'resume-report.md']);
    const resumedTools = continuedEvents.filter(event => event.event === 'tool.started' && event.data.tool_name === 'write_file');
    assert.deepEqual(resumedTools.map(event => event.data.tool_call_id), ['qa-resume-facts']);
    assert.deepEqual(await api(continuedFiles.find(row => row.path === 'resume-facts.json').download_url), { total: 18 });

    for (const marker of Object.values(projectFixture.MARKERS)) {
      if ([projectFixture.MARKERS.runtime, projectFixture.MARKERS.browser, projectFixture.MARKERS.browserInput,
        projectFixture.MARKERS.vue, projectFixture.MARKERS.coverage].includes(marker)
          && !runtimeEnabled) continue;
      const passed = ![projectFixture.MARKERS.failed, projectFixture.MARKERS.explicit].includes(marker);
      const browserProject = ui && [projectFixture.MARKERS.repaired, projectFixture.MARKERS.fallback,
        projectFixture.MARKERS.failed, projectFixture.MARKERS.browser, projectFixture.MARKERS.vue].includes(marker);
      const stackRequirement = marker === projectFixture.MARKERS.explicit ? '必须使用 Vue 3 和 FastAPI。'
        : marker === projectFixture.MARKERS.vue ? '前端必须使用 Vue 3、TypeScript 和 Vite。' : '';
      const acceptanceRequirement = marker === projectFixture.MARKERS.coverage
        ? '请用浏览器验证从页面删除已完成工单后，刷新列表仍不会出现该工单。' : '';
      const request = { ...await newRequest(marker), team_mode: true,
        message: marker === projectFixture.MARKERS.fallback ? projectFixture.DELEGATED_REQUEST
          : ['帮我构建一个前后端分离的项目，你随意发挥（' + marker + '）。', stackRequirement,
            '需要交付源码、启动说明与测试。', acceptanceRequirement].filter(Boolean).join(' ') };
      let projectRun;
      if (browserProject) {
        projectRun = await sendFromBrowser(ui, request, `${marker} ${stamp}`);
        runs.push(projectRun);
      } else {
        projectRun = await begin(request);
      }
      const status = await finished(projectRun, passed ? 'completed' : 'failed', 90000);
      assert.equal(status.execution_context.task_type, 'software_project');
      const delivery = projectFixture.verifyProjectEvents(marker, await events(projectRun), runtimeEnabled);
      if (!delivery) continue;
      const bundle = await api(delivery.download_url);
      assert.equal(bundle.subarray(0, 2).toString(), 'PK');
      if (marker === projectFixture.MARKERS.failed) {
        const verifyRepairCalls = projectFixture.prepareDraftRepair();
        const path = `/api/v2/runs/${projectRun.run_id}/project-repair`;
        let repaired;
        if (browserProject) {
          await ui.page.reload();
          const card = ui.page.locator(`[data-run-id="${projectRun.run_id}"]`).getByLabel('项目交付', { exact: true });
          await card.getByRole('list', { name: '需要修复的文件' }).getByText('backend/app.py', { exact: true }).waitFor();
          const response = ui.page.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'POST');
          await card.getByRole('button', { name: '继续修复草稿', exact: true }).click();
          const created = await response;
          assert.equal(created.status(), 200, await created.text());
          repaired = await created.json();
        } else {
          repaired = await api(path, { method: 'POST', body: {} });
        }
        runs.push(repaired);
        assert.equal(repaired.conversation_id, projectRun.conversation_id);
        await finished(repaired, 'completed', 90000);
        verifyRepairCalls();
        const repairedBundle = (await events(repaired)).find(event => event.event === 'project.delivery').data;
        assert.equal(repairedBundle.state, 'passed');
        assert.notEqual(repairedBundle.download_url, delivery.download_url);
        assert.deepEqual(await api(delivery.download_url), bundle, '修复不能覆盖原草稿');
        await projectFixture.verifyRunningBundle(marker, await api(repairedBundle.download_url), browserProject ? ui.browser : undefined, runtimeEnabled);
        if (browserProject) {
          const completedCard = ui.page.locator(`[data-run-id="${repaired.run_id}"]`).getByLabel('项目交付', { exact: true });
          await completedCard.getByRole('button', { name: '下载项目 ZIP', exact: true }).waitFor();
        }
      }
      if (passed) {
        await projectFixture.verifyRunningBundle(marker, bundle, browserProject ? ui.browser : undefined, runtimeEnabled);
        if (browserProject) {
          const card = ui.page.locator(`[data-run-id="${projectRun.run_id}"]`).getByLabel('项目交付', { exact: true });
          await card.getByRole('button', { name: '下载项目 ZIP', exact: true }).waitFor();
          if (runtimeEnabled) await card.getByText('浏览器操作场景 2 / 2 通过 · 隔离运行', { exact: true }).waitFor();
          await ui.page.reload();
          const downloadButton = card.getByRole('button', { name: '下载项目 ZIP', exact: true });
          await downloadButton.waitFor();
          const download = ui.page.waitForEvent('download');
          await downloadButton.click();
          assert.match((await download).suggestedFilename(), /\.zip$/);
          const screenshot = marker === projectFixture.MARKERS.fallback ? 'team-project-recovery-e2e.png' : 'team-project-e2e.png';
          await ui.page.screenshot({ path: join(dirname(credentialsPath), screenshot), animations: 'disabled' });
        }
      }
    }

    const partial = await begin({ ...await newRequest(runNames.partial), team_mode: true });
    await finished(partial, 'completed');
    const partialEvents = await events(partial);
    const partialTeam = partialEvents.findLast(event => event.event === 'team.updated');
    assert.equal(partialTeam.data.tasks.find(task => task.id === 'risk').status, 'failed');
    assert.equal(partialTeam.data.tasks.find(task => task.id === 'research').status, 'completed');
    assert.equal(partialTeam.data.tasks.find(task => task.id === 'review').status, 'skipped');
    assert.match(partialEvents.at(-1).data.final_text, /QA_PARTIAL_RESULT/);

    const cancelled = await begin({ ...await newRequest(runNames.cancelled), team_mode: true });
    await until(() => entered.has(runNames.cancelled), Boolean, '未开始需要取消的模型调用');
    await api(`/api/v2/runs/${cancelled.run_id}/cancel`, { method: 'POST' });
    await finished(cancelled, 'cancelled', 50000);
    const cancelledEvents = await events(cancelled);
    assert.equal(cancelledEvents.at(-1).event, 'run.cancelled');
    const cancelledTeam = cancelledEvents.findLast(event => event.event === 'team.updated');
    assert.equal(cancelledTeam.data.phase, 'cancelled');
    assert(cancelledTeam.data.tasks.every(task => !['running', 'pending'].includes(task.status)));
    assert(!calls.some(call => call.name === runNames.cancelled && call.operation === 'synthesize'));

    if (option === '--restart') {
      const restarted = await begin({ ...await newRequest(runNames.restarted), team_mode: true });
      await until(() => entered.get(runNames.restarted), member => member === 'review', '重启前未到依赖成员');
      docker('kill', '--signal', 'SIGKILL', 'backend');
      docker('start', 'backend');
      await until(() => api('/api/health').catch(() => null), Boolean, '重启后后端不可用');
      await finished(restarted, 'completed', 90000);
      const history = calls.filter(call => call.name === runNames.restarted);
      for (const participant of ['research', 'risk']) {
        assert.equal(history.filter(call => call.member === participant).length, 1, '恢复时重做了已完成的成员');
      }
      assert.equal(history.filter(call => call.operation === 'plan').length, 1, '恢复时重复规划');
      assert.equal(history.filter(call => call.member === 'review').length, 2, '恢复时没有重做被中断的成员');
      assert.equal(history.filter(call => call.operation === 'synthesize').length, 1, '恢复时重复汇总');
      const replay = await events(restarted);
      assert.equal(replay.filter(event => event.event === 'run.completed').length, 1);
    }
  } finally {
    if (ui) await ui.browser.close();
    for (const run of runs) await api(`/api/v2/runs/${run.run_id}/cancel`, { method: 'POST' }).catch(() => {});
    for (const id of documents) await api(`/api/v2/documents/${id}`, { method: 'DELETE' }).catch(() => {});
    for (const id of conversationIds) await api(`/api/v2/conversations/${id}`, { method: 'DELETE' }).catch(() => {});
    // 已完成的 Run 仍可追踪。停用 QA 助手与模型，防止其它验收任务选中模拟供应商。
    await api(`/api/v1/agents/${assistant.id}`, { method: 'DELETE' }).catch(() => {});
    await api(`/api/v1/models/configs/${config.id}`, { method: 'PUT', json: { is_active: false } }).catch(() => {});
    await api(`/api/v1/models/providers/${provider.id}`, { method: 'PUT', json: { is_active: false } }).catch(() => {});
  }
  if (failures.length) throw failures[0];
  console.log(`团队模式通过显式启用、实时分工、规划回复恢复、默认技术栈继续交付、工程 ZIP、验收返工、项目启动和交互、失败草稿、取消与回放${option ? '，含强制重启恢复' : ''}`);
}

model.listen(0, '0.0.0.0', () => {
  main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
    model.close();
    model.closeAllConnections();
  });
});
