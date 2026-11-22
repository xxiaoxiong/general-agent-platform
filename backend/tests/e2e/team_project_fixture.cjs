// 模型返回本仓库的测试夹具。只有核对过字节的测试代码才会在验收环境运行。
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const { mkdirSync, mkdtempSync, readFileSync, writeFileSync } = require('node:fs');
const { createServer } = require('node:http');
const { tmpdir } = require('node:os');
const { dirname, join } = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');

const MARKERS = {
  repaired: 'QA_PROJECT_REPAIRED', failed: 'QA_PROJECT_NO_PASS', normalized: 'QA_PROJECT_PLAN_FENCE',
  fallback: 'QA_PROJECT_PLAN_FALLBACK', explicit: 'QA_PROJECT_PLAN_EXPLICIT_STACK',
  runtime: 'QA_PROJECT_RUNTIME_REPAIR', browser: 'QA_PROJECT_BROWSER_REPAIR', browserInput: 'QA_PROJECT_BROWSER_INPUT',
  vue: 'QA_PROJECT_VUE_BUILD_REPAIR',
  coverage: 'QA_PROJECT_COVERAGE_DELETE',
};
const DELEGATED_REQUEST = '帮我构建一个足够复杂的前后端分离项目，你随意发挥';
const plan = {
  name: '巡检工单台',
  summary: '为车间创建前后端分离的巡检工单台，SQLite 持久化并支持筛选和状态更新。',
  features: ['创建巡检工单并校验必填输入', '按状态筛选', '完成工单', '刷新后读取持久化状态'],
  api_contract: 'GET /api/tickets?status=&q= 返回 {items}；POST /api/tickets 创建工单；PATCH /api/tickets/{id} 修改状态；DELETE /api/tickets/{id} 删除工单。',
  tasks: [
    { id: 'contract', role: '接口设计', title: '制定接口契约', brief: '明确列表筛选和变更操作的接口契约。',
      depends_on: [], files: ['openapi.json'] },
    { id: 'backend', role: '后端工程', title: '实现持久化工单服务', brief: '实现校验、状态更新、搜索与 SQLite 持久化。',
      depends_on: ['contract'], files: ['backend/app.py'] },
    { id: 'frontend', role: '前端工程', title: '实现工单工作台', brief: '实现列表、表单、状态筛选及错误反馈。',
      depends_on: ['contract'], files: ['frontend/index.html', 'frontend/app.js', 'frontend/style.css'] },
    { id: 'quality', role: '集成验收', title: '交付启动说明与接口测试', brief: '核对两端接口并提供启动命令与可执行接口测试。',
      depends_on: ['backend', 'frontend'], files: ['README.md', 'tests/test_api.py'] },
  ],
};
const files = plan.tasks.flatMap(task => task.files);
const browserChecks = 'tests/browser-checks.json';
const fixtureRoot = join(__dirname, 'fixtures', 'team-project');
const fileText = path => readFileSync(join(fixtureRoot, path === 'tests/test_api.py' ? `${path}.fixture` : path), 'utf8')
  .replaceAll('\r\n', '\n');
const operations = [];
let resumeFailedDraft = false;

const vueSources = Object.fromEntries(['App.vue', 'main.ts', 'index.html', 'package.json', 'tsconfig.json', 'vite.config.ts']
  .map(path => [`frontend/${path}`, fileText(`vue/${path}`)]));
vueSources['frontend/style.css'] = fileText('frontend/style.css');
const vuePlan = { ...plan, tasks: plan.tasks.map(task => task.id === 'frontend'
  ? { ...task, brief: `${task.brief} 使用 Vue、TypeScript 与 Vite。`, files: Object.keys(vueSources) }
  : task) };
const vueReadme = fileText('README.md')
  .replace('前端使用原生 JavaScript，后端使用 Python 标准库，无需安装额外依赖。在项目根目录分别运行。',
    '后端在项目根目录启动，前端在 frontend 中安装依赖后运行生产构建。')
  .replace('python -m http.server 8766 --bind 127.0.0.1 --directory frontend',
    'cd frontend\nnpm install\nnpm run build\nnpm run preview -- --port 5173')
  .replaceAll('127.0.0.1:8766', '127.0.0.1:5173')
  .replace('前端可通过 `?api=http://127.0.0.1:端口` 指定 API 地址。', '页面使用 Vite 的同源 API 代理。');
const vueTest = `${fileText('tests/test_api.py')}\n\nimport pytest\n\n@pytest.fixture(scope="function")\ndef isolated_database(tmp_path):\n    return tmp_path / "tickets.db"\n\ndef test_new_storage_starts_empty(isolated_database):\n    assert not isolated_database.exists()\n`;

function fallbackSources() {
  const original = fileText('backend/app.py');
  const database = original.match(/def database\(\):[\s\S]*?(?=\n\nclass Tickets)/)?.[0];
  assert(database, '后端夹具没有可用于分工测试的数据库入口');
  const frontend = fileText('frontend/app.js');
  const contract = JSON.parse(fileText('openapi.json'));
  contract.paths['/api/health'] = { get: { responses: { 200: { description: '服务健康' } } } };
  return {
    'openapi.json': JSON.stringify(contract, null, 2),
    'backend/store.py': `import os\nimport sqlite3\n\n\n${database}\n`,
    'backend/domain.py': 'ALLOWED_STATUSES = ("open", "done")\n',
    'backend/app.py': original.replace(database, 'from domain import ALLOWED_STATUSES\nfrom store import database\n')
      .replace('import sqlite3\n', '').replace('status not in ("open", "done")', 'status not in ALLOWED_STATUSES')
      .replace('        if url.path != "/api/tickets":', [
        '        if url.path == "/api/health":', '            self.reply(200, {"status": "ok"})',
        '            return', '        if url.path != "/api/tickets":',
      ].join('\n')),
    'frontend/index.html': fileText('frontend/index.html'),
    'frontend/state.mjs': 'export const initialStatus = \'\';\n',
    'frontend/app.js': `import { initialStatus } from './state.mjs';\n${frontend}`.replace("filter.value = '';", 'filter.value = initialStatus;'),
    'frontend/style.css': fileText('frontend/style.css'),
    'tests/test_app.py': fileText('tests/test_api.py'),
    'README.md': fileText('README.md'),
  };
}

const recoveryFiles = fallbackSources();
const missingDelete = "action('删除工单', () => Promise.resolve())";
const deleteHandler = /action\('删除工单', \(\) => api\([^,\n]+, \{ method: 'DELETE' \}\)\)/;
const deleteScenario = '删除已完成工单并核对刷新后的列表';

function coverageBrowserSource() {
  const contract = JSON.parse(fileText(browserChecks));
  contract.scenarios.push({ name: deleteScenario, steps: [
    { action: 'open', path: '/' },
    { action: 'fill', selector: 'input[name=title]', value: '验收删除工单' },
    { action: 'click', selector: 'form button[type=submit]' },
    { action: 'expect_text', selector: '#tickets', value: '验收删除工单' },
    { action: 'click', selector: '#tickets li button:first-of-type' },
    { action: 'expect_text', selector: '#tickets li span', value: '已完成' },
    { action: 'click', selector: '#tickets li button:last-of-type' },
    { action: 'expect_response', method: 'DELETE', path: '/api/tickets/1', status: 200 },
    { action: 'expect_text', selector: '#notice', value: '没有符合条件的工单' },
    { action: 'reload' },
    { action: 'expect_text', selector: '#notice', value: '没有符合条件的工单' },
    { action: 'expect_hidden', selector: '#tickets li' },
  ] });
  return JSON.stringify(contract, null, 2);
}

function reviewCoverage(marker, request) {
  const { criteria, checks, sources, objective } = request;
  assert(criteria.length > 0, '复核应锚定至少一项原始需求');
  const passed = new Set(checks.filter(check => check.status === 'passed').map(check => check.id));
  const evidence = (path, pattern) => {
    const source = sources[path] || '';
    const line = source.split('\n').find(value => pattern.test(value));
    return line ? { path, excerpt: line.trim() } : undefined;
  };
  const quote = (path, excerpt) => sources[path]?.includes(excerpt) ? { path, excerpt } : undefined;
  const verify = (id, detail, ids, proofs) => {
    const cited = [ids].flat();
    const anchors = [proofs].flat();
    return cited.every(check => passed.has(check)) && anchors.every(Boolean)
      ? { id, status: 'verified', detail, checks: cited, evidence: anchors, files: [] }
      : { id, status: 'unverified', detail, checks: [], evidence: [], files: [] };
  };
  const deletionRequired = criterion => /删除|刷新列表/.test(criterion.text);
  if (marker === MARKERS.coverage) {
    assert.match(objective, /删除已完成工单/);
    assert(criteria.some(deletionRequired), '显式删除需求必须进入独立复核');
    assert(passed.has('runtime.browser.flow.1') && passed.has('runtime.browser.flow.2'),
      '覆盖回归须模拟原有浏览器验收正常通过');
    operations.push({ marker, stage: 'coverage', flows: [...passed].filter(id => id.startsWith('runtime.browser.flow.')).length });
  } else {
    operations.push({ marker, stage: 'coverage' });
  }
  return JSON.stringify({ criteria: criteria.map(criterion => {
    const { id, kind, text } = criterion;
    if (marker === MARKERS.coverage && deletionRequired(criterion)) {
      const gap = evidence('frontend/app.js', /删除工单.*Promise\.resolve/);
      if (gap) {
        return { id, status: 'gap', detail: '删除按钮仅解析空 Promise。补齐实际 DELETE 调用，并加入完成后删除及刷新检查。',
          evidence: [gap], checks: [], files: ['frontend/app.js', browserChecks] };
      }
      const responseProof = quote(browserChecks, '/api/tickets/1');
      if (!responseProof) {
        const testAnchor = quote(browserChecks, '核对刷新后的完成状态');
        return { id, status: 'unverified', detail: '还需隔离浏览器验证删除及刷新后的结果。',
          evidence: [testAnchor].filter(Boolean), checks: [], files: [browserChecks] };
      }
      return verify(id, '清单中完成工单后执行 DELETE，刷新后仍为空。', 'runtime.browser.flow.3',
        [responseProof, quote(browserChecks, '没有符合条件的工单')]);
    }
    if (kind === 'browser') {
      const inputCheck = /必填|校验|无效/.test(text);
      return verify(id, '已核对操作后的页面状态。',
        inputCheck ? 'runtime.browser.flow.2' : 'runtime.browser.flow.1',
        quote(browserChecks, inputCheck ? 'input[name=title]' : '已完成'));
    }
    const testFile = marker === MARKERS.fallback ? 'tests/test_app.py' : 'tests/test_api.py';
    if (kind === 'behavior') {
      if (/筛选|完成|刷新/.test(text)) {
        return verify(id, '页面已执行完成、按状态筛选及刷新后的状态核对。',
          'runtime.browser.flow.1', quote(browserChecks, '已完成'));
      }
      return verify(id, '实际接口测试完成。', 'runtime.tests.1', evidence(testFile, /assertEqual/));
    }
    if (/源码.*启动.*测试/.test(text)) {
      return verify(id, '源码、启动说明及测试文件均已核对。',
        ['planned_files', 'run_instructions', 'test_sources'], [evidence('backend/app.py', /class Tickets/),
          evidence('README.md', /python backend\/app\.py/), evidence(testFile, /assertEqual/)]);
    }
    if (/启动|说明|README/i.test(text)) {
      return verify(id, 'ZIP 包含核对过的启动说明。', 'run_instructions', evidence('README.md', /python backend\/app\.py/));
    }
    if (/测试/.test(text)) {
      return verify(id, 'ZIP 包含带断言的测试文件。', 'test_sources', evidence(testFile, /assertEqual/));
    }
    if (/Vue|TypeScript|Vite/i.test(text)) {
      const versions = [['Vue', '"vue"'], ['TypeScript', '"typescript"'], ['Vite', '"vite"']];
      return verify(id, '核对前端依赖与生产构建。', 'runtime.web_build', versions
        .filter(([technology]) => text.toLowerCase().includes(technology.toLowerCase()))
        .map(([, dependency]) => quote('frontend/package.json', dependency)));
    }
    return verify(id, 'ZIP 包含项目源码。', 'planned_files', evidence('backend/app.py', /class Tickets/));
  }) });
}

const filesFor = (marker, runtimeEnabled) => [
  ...(marker === MARKERS.fallback ? Object.keys(recoveryFiles) : marker === MARKERS.vue
    ? vuePlan.tasks.flatMap(task => task.files) : files), ...(runtimeEnabled ? [browserChecks] : []),
];
function sourceFor(marker, path) {
  if (marker === MARKERS.fallback && path !== browserChecks) return recoveryFiles[path];
  if (marker === MARKERS.coverage && path === browserChecks) return coverageBrowserSource();
  if (marker === MARKERS.vue) {
    if (path === 'README.md') return vueReadme;
    if (path === 'tests/test_api.py') return vueTest;
    if (path in vueSources) return vueSources[path];
  }
  return fileText(path);
}

function projectReply(system, user) {
  const text = system + user;
  const marker = Object.values(MARKERS).find(key => text.includes(key))
    || (text.includes(DELEGATED_REQUEST) ? MARKERS.fallback : undefined);
  if (!marker) return undefined;
  if (system.includes('工程需求复核')) return reviewCoverage(marker, JSON.parse(user));
  if (system.includes('项目立项')) {
    operations.push({ marker, stage: 'plan' });
    if (marker === MARKERS.normalized) {
      return `项目规划如下。\n\n\`\`\`json\n${JSON.stringify(plan, null, 2)}\n\`\`\`\n\n按接口契约推进实现。`;
    }
    if (marker === MARKERS.fallback || marker === MARKERS.explicit) {
      const attempts = operations.filter(item => item.marker === marker && item.stage === 'plan').length;
      if (attempts === 1) return { content: '{"name":"规划未写完","tasks":[', finishReason: 'length' };
      return '建议使用前后端分离结构。具体分工与文件仍未确定。';
    }
    return JSON.stringify(marker === MARKERS.vue ? vuePlan : plan);
  }
  assert(system.includes('项目文件') || system.includes('项目返工'), '工程运行进入了报告型提示');
  const stage = system.includes('项目返工') ? 'repair' : 'write';
  const request = JSON.parse(user);
  const path = request.path;
  assert(filesFor(marker, path === browserChecks).includes(path), `项目工作区出现未分派文件 ${path}`);
  operations.push({ marker, stage, path });
  if (path === browserChecks) {
    assert(request.committed_files['frontend/index.html'], '页面验收需要看到已经提交的实际页面');
    const component = marker === MARKERS.vue ? 'frontend/App.vue' : 'frontend/app.js';
    assert(request.committed_files[component], '页面验收需要看到已经提交的真实交互');
    if (marker === MARKERS.coverage) {
      if (stage === 'write') return fileText(path);
      assert(!request.committed_files[component].includes(missingDelete), '浏览器证据须基于修复后的实际源码');
      return coverageBrowserSource();
    }
    if (marker === MARKERS.vue && stage === 'write') {
      const fixture = JSON.parse(fileText(path));
      const assertion = fixture.scenarios[0].steps.find(step => step.selector === '#tickets li span');
      assertion.action = 'expect_value';
      return JSON.stringify(fixture);
    }
    if (marker === MARKERS.browserInput) {
      if (stage === 'write') {
        const fixture = JSON.parse(fileText(path));
        fixture.scenarios[0].steps.splice(1, 1);
        return JSON.stringify(fixture);
      }
      assert(request.verification_findings.some(finding => finding.includes('场景输入未满足表单要求 title')),
        '缺少必填输入应指向浏览器场景');
    }
  }
  if (marker === MARKERS.vue && path === 'frontend/App.vue') {
    if (stage === 'write') return vueSources[path].replace("ref<string>('')", 'ref<string>(42)');
    assert(request.verification_findings.some(finding => finding.includes('构建退出码') && /error TS\d+/.test(finding)),
      '构建失败应包含类型检查结果并指向项目组件');
    assert(request.committed_files[browserChecks]?.includes('核对刷新后的完成状态'));
  }
  if (marker === MARKERS.vue && path === 'tests/test_api.py' && stage === 'write') {
    return vueTest.replace('scope="function"', 'scope="module"');
  }
  if (marker === MARKERS.coverage && path === 'frontend/app.js') {
    if (stage === 'write') {
      const current = fileText(path);
      assert.match(current, deleteHandler);
      return current.replace(deleteHandler, missingDelete);
    }
    assert(request.verification_findings.some(finding => /删除/.test(finding)));
  }
  if (marker === MARKERS.browser && path === 'frontend/app.js') {
    if (stage === 'write') {
      return fileText(path).replace('JSON.stringify({ status: next })', 'JSON.stringify({ status: item.status })');
    }
    assert(request.verification_findings.some(finding => finding.includes('第 7 步') && finding.includes('expect_text')),
      '只有真实页面状态断言失败才会触发返工');
    assert(request.committed_files[browserChecks]?.includes('核对刷新后的完成状态'));
  }
  if (marker === MARKERS.runtime && path === 'backend/app.py') {
    if (stage === 'write') {
      return fileText(path).replace('if __name__ == "__main__":\n',
        'if __name__ == "__main__":\n    raise RuntimeError("离线启动故障夹具")\n');
    }
    assert(request.verification_findings.some(finding => finding.includes('RuntimeError: 离线启动故障夹具')),
      '必须把实际启动异常定位到后端入口后才触发修复');
  }
  if (path === 'backend/app.py' && ((marker === MARKERS.failed && !resumeFailedDraft) || (marker === MARKERS.repaired && stage === 'write'))) {
    return fileText(path).replaceAll('/api/tickets', '/api/draft-tickets');
  }
  return sourceFor(marker, path);
}

function prepareDraftRepair() {
  resumeFailedDraft = true;
  const before = operations.length;
  return () => assert.deepEqual(operations.slice(before).filter(item => item.stage !== 'coverage'),
    [{ marker: MARKERS.failed, stage: 'repair', path: 'backend/app.py' }]);
}

function packageSources(zip, expectedFiles) {
  const target = mkdtempSync(join(tmpdir(), 'team-project-payload-'));
  const archive = join(target, 'project.zip');
  writeFileSync(archive, zip);
  const reader = spawnSync('python', ['-c', [
    'import json, sys, zipfile',
    'archive, *names = sys.argv[1:]',
    'with zipfile.ZipFile(archive) as z:',
    '    assert z.testzip() is None',
    '    assert len(z.namelist()) == len(set(z.namelist()))',
    '    assert set(z.namelist()) == set(names + ["verification.json"])',
    '    source = {name: z.read(name).decode("utf-8") for name in names}',
    '    print(json.dumps({"sources": source, "verification": json.loads(z.read("verification.json"))}))',
  ].join('\n'), archive, ...expectedFiles], { encoding: 'utf8', timeout: 15000 });
  assert.equal(reader.status, 0, reader.stderr);
  return { target, ...JSON.parse(reader.stdout) };
}

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

async function verifyRunningBundle(marker, zip, browser, runtimeEnabled) {
  const expectedFiles = filesFor(marker, runtimeEnabled);
  const { target, sources, verification } = packageSources(zip, expectedFiles);
  assert.equal(verification.passed, true);
  assert.equal(verification.scope, runtimeEnabled ? 'sandbox' : 'static');
  if (runtimeEnabled) {
    const runtimeChecks = verification.checks.filter(check => check.id.startsWith('runtime.'));
    assert.deepEqual(runtimeChecks.map(check => check.id), [
      'runtime.start', 'runtime.api', ...(marker === MARKERS.vue ? ['runtime.web_build'] : []),
      'runtime.web', 'runtime.tests.1', 'runtime.browser', 'runtime.browser.flow.1', 'runtime.browser.flow.2',
      ...(marker === MARKERS.coverage ? ['runtime.browser.flow.3'] : []),
    ]);
    assert(runtimeChecks.every(check => check.status === 'passed'), JSON.stringify(runtimeChecks));
    if (marker === MARKERS.vue) assert(runtimeChecks.find(check => check.id === 'runtime.web').detail.includes('生产构建'));
    if (marker === MARKERS.coverage) {
      assert(verification.checks.some(check => check.id.startsWith('coverage.') && check.status === 'passed'));
    }
    const digest = spawnSync('python', ['-c', [
      'import hashlib, json, sys',
      'source = json.loads(sys.stdin.buffer.read())',
      'data = json.dumps(sorted(source.items()), ensure_ascii=False, separators=(",", ":"))',
      'print(hashlib.sha256(data.encode()).hexdigest())',
    ].join('\n')], { input: JSON.stringify(sources), encoding: 'utf8', timeout: 10000 });
    assert.equal(digest.status, 0, digest.stderr);
    assert.equal(verification.source_digest, digest.stdout.trim(), '报告与压缩包内的源码不是同一版本');
  }
  for (const path of expectedFiles) {
    const normalized = text => text.replaceAll('\r\n', '\n').trimEnd();
    const original = sourceFor(marker, path);
    if (path === browserChecks) assert.deepEqual(JSON.parse(sources[path]), JSON.parse(original));
    else assert.equal(normalized(sources[path]), normalized(original), `下载内容与角色源码 ${path} 不一致`);
    const destination = join(target, ...path.split('/'));
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, sources[path]);
  }
  const reservation = createServer();
  const port = await listen(reservation);
  await new Promise(resolve => reservation.close(resolve));
  const backendUrl = `http://127.0.0.1:${port}`;
  const child = spawn('python', ['-B', 'backend/app.py'], {
    cwd: target, env: { ...process.env, PORT: String(port), TICKET_DB: join(target, 'tickets.db') },
    stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true,
  });
  let errors = '';
  child.stderr.on('data', data => { errors += data; });
  try {
    for (let attempt = 0; attempt < 80; attempt++) {
      if (await fetch(`${backendUrl}/api/tickets`).then(response => response.ok, () => false)) break;
      assert.equal(child.exitCode, null, errors);
      await sleep(100);
    }
    const test = spawnSync('python', ['-B', '-m', 'unittest', 'discover', '-s', 'tests'], {
      cwd: target, env: { ...process.env, TICKET_API: backendUrl }, encoding: 'utf8', timeout: 20000,
    });
    assert.equal(test.status, 0, test.stderr);
    if (marker === MARKERS.fallback) {
      const health = await fetch(`${backendUrl}/api/health`);
      assert.equal(health.status, 200);
      assert.deepEqual(await health.json(), { status: 'ok' });
    }
    if (!browser || marker === MARKERS.vue) return;
    const frontend = createServer((request, response) => {
      const path = new URL(request.url, 'http://localhost').pathname;
      const entry = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'],
        '/state.mjs': ['state.mjs', 'text/javascript'], '/style.css': ['style.css', 'text/css'] }[path];
      if (!entry || !expectedFiles.includes(`frontend/${entry[0]}`)) return response.writeHead(404).end();
      response.writeHead(200, { 'content-type': `${entry[1]}; charset=utf-8` });
      response.end(readFileSync(join(target, 'frontend', entry[0])));
    });
    const frontendPort = await listen(frontend);
    const page = await browser.newPage({ viewport: { width: 390, height: 820 } });
    try {
      await page.goto(`http://127.0.0.1:${frontendPort}/?api=${encodeURIComponent(backendUrl)}`);
      await page.getByLabel('工单标题').fill('检查装配线传感器');
      await page.getByRole('button', { name: '创建工单' }).click();
      const row = page.getByRole('listitem').filter({ hasText: '检查装配线传感器' });
      await row.getByRole('button', { name: '标为完成' }).click();
      await row.getByText('已完成', { exact: true }).waitFor();
      await page.getByLabel('状态', { exact: true }).selectOption('open');
      await row.waitFor({ state: 'hidden' });
      await page.getByLabel('状态', { exact: true }).selectOption('done');
      await row.getByText('已完成', { exact: true }).waitFor();
      await page.reload();
      await row.getByText('已完成', { exact: true }).waitFor();
      await page.getByLabel('搜索工单').fill('不匹配');
      await row.waitFor({ state: 'hidden' });
      await page.getByLabel('搜索工单').fill('传感器');
      await row.getByRole('button', { name: '删除工单' }).click();
      await row.waitFor({ state: 'hidden' });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    } finally {
      await page.close();
      frontend.close();
      frontend.closeAllConnections();
    }
  } finally {
    child.kill();
  }
}

function verifyProjectEvents(marker, events, runtimeEnabled) {
  if (marker === MARKERS.explicit) {
    assert.equal(events.at(-1).event, 'run.failed');
    assert.equal(operations.filter(item => item.marker === marker && item.stage === 'plan').length, 2);
    assert(!events.some(event => event.event === 'project.delivery' || event.data.planning_status === 'fallback'),
      '用户指定技术栈时不能换成默认项目');
    assert(!operations.some(item => item.marker === marker && item.stage !== 'plan'),
      '规划未通过时不能开始生成源码');
    return null;
  }
  const passed = marker !== MARKERS.failed;
  const delivery = events.find(event => event.event === 'project.delivery');
  assert(delivery?.data.download_url, '缺少可下载的项目 ZIP');
  assert.equal(delivery.data.state, passed ? 'passed' : 'failed', JSON.stringify(delivery.data));
  assert.equal(delivery.data.file_count, filesFor(marker, runtimeEnabled).length);
  assert.equal(delivery.data.verification_scope, runtimeEnabled && passed ? 'sandbox' : 'static');
  assert(delivery.data.checks.some(check => check.status === (passed ? 'passed' : 'failed')));
  if (runtimeEnabled && passed) {
    assert(events.some(event => event.event === 'project.progress' && event.data.detail?.includes('隔离环境')));
    assert(delivery.data.checks.some(check => check.id === 'runtime.tests.1' && check.status === 'passed'));
    assert(delivery.data.checks.some(check => check.id === 'runtime.browser.flow.1' && check.status === 'passed'));
    assert(delivery.data.checks.some(check => check.id === 'runtime.browser.flow.2' && check.status === 'passed'));
  }
  if (marker === MARKERS.normalized) {
    assert.equal(operations.filter(item => item.marker === marker && item.stage === 'plan').length, 1,
      '只含前言和代码围栏的规划应直接恢复，避免多一次模型调用');
  } else if (marker === MARKERS.fallback) {
    assert.equal(operations.filter(item => item.marker === marker && item.stage === 'plan').length, 2);
    const progress = events.filter(event => event.event === 'project.progress');
    assert(progress.some(event => event.data.planning_status === 'repairing' && event.data.issue === 'response_truncated'),
      '截断后的规划重试没有对用户可见的反馈');
    const accepted = progress.find(event => event.data.planning_status === 'fallback');
    assert.match(accepted?.data.detail || '', /默认技术栈/);
    assert(accepted.sequence < delivery.sequence, '继续实施前应先告知默认技术栈');
    assert.deepEqual([...new Set(operations.filter(item => item.marker === marker && item.stage === 'write')
      .map(item => item.path))].sort(), filesFor(marker, runtimeEnabled).sort(), '默认分工必须真实生成所有源码');
  } else {
    assert(events.some(event => event.event === 'project.progress' && event.data.phase === 'repairing'));
    const target = { [MARKERS.browserInput]: browserChecks, [MARKERS.browser]: 'frontend/app.js',
      [MARKERS.vue]: 'frontend/App.vue', [MARKERS.coverage]: 'frontend/app.js' }[marker] || 'backend/app.py';
    assert(operations.some(item => item.marker === marker && item.stage === 'repair' && item.path === target));
  }
  if (marker === MARKERS.runtime) {
    assert.deepEqual(operations.filter(item => item.marker === marker && item.stage === 'repair').map(item => item.path),
      ['backend/app.py'], '运行失败只应改动因启动故障而受影响的文件');
  }
  if (marker === MARKERS.browser) {
    assert.deepEqual(operations.filter(item => item.marker === marker && item.stage === 'repair').map(item => item.path),
      ['frontend/app.js'], '应修复应用行为并复用原浏览器场景');
  }
  if (marker === MARKERS.browserInput) {
    assert.deepEqual(operations.filter(item => item.marker === marker && item.stage === 'repair').map(item => item.path),
      [browserChecks], '修正场景输入时保留应用源码');
  }
  if (marker === MARKERS.vue) {
    assert.deepEqual(operations.filter(item => item.marker === marker && item.stage === 'repair').map(item => item.path),
      ['frontend/App.vue'], '应修复实际构建错误并保留其他源码和验收场景');
  }
  if (marker === MARKERS.coverage) {
    const reviewed = operations.filter(item => item.marker === marker && ['repair', 'coverage'].includes(item.stage));
    assert.equal(reviewed[0].flows, 2, '验收通过的两个基础场景无法证明删除需求');
    assert.equal(reviewed.at(-1).flows, 3, '须复核新增的真实浏览器操作');
    assert.deepEqual(reviewed.filter(item => item.stage === 'repair').map(item => item.path), ['frontend/app.js', browserChecks]);
  }
  assert.equal(events.at(-1).event, passed ? 'run.completed' : 'run.failed');
  return delivery.data;
}

module.exports = { DELEGATED_REQUEST, MARKERS, prepareDraftRepair, projectReply, verifyProjectEvents, verifyRunningBundle };
