// 由 document_intelligence.cjs 在真实 QA Run 暂停和完成时调用。
const assert = require('node:assert/strict');
const { readFile } = require('node:fs/promises');

async function openBrowser(credentials, frontend) {
  const { chromium } = require(process.env.DOCUMENT_QA_PLAYWRIGHT);
  const browser = await chromium.launch({
    headless: true, ...(process.env.DOCUMENT_QA_BROWSER_CHANNEL && { channel: process.env.DOCUMENT_QA_BROWSER_CHANNEL }),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20000);
    await page.goto(frontend + '/login');
    await page.getByLabel('用户名或邮箱').fill(credentials.username);
    await page.getByLabel('密码', { exact: true }).fill(credentials.password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForURL('**/workspace');
    return { browser, page, frontend };
  } catch (error) {
    await browser.close();
    throw error;
  }
}

function runCard(page, id) {
  return page.locator(`[data-run-id="${id}"]`).first();
}

async function reviewPanes(dialog) {
  const proposed = dialog.getByRole('article', { name: '修改稿正文' });
  const original = dialog.getByRole('article', { name: '原文正文' });
  await Promise.all([proposed.waitFor(), original.waitFor()]);
  const [left, right] = await Promise.all([proposed.boundingBox(), original.boundingBox()]);
  assert(left && right && left.x + left.width <= right.x + 1 && Math.abs(left.y - right.y) <= 1,
    '修改稿应在左侧，原文应在同一行的右侧');
  return { proposed, original };
}

async function inspectPreviewResize(page) {
  const workspace = page.getByLabel('智能体工作空间', { exact: true });
  const preview = workspace.getByLabel('文件预览', { exact: true });
  const separator = preview.getByRole('separator', { name: '调整预览宽度' });
  const iframe = preview.locator('iframe[src^="blob:"]');
  await iframe.waitFor();

  await separator.press('Home');
  const minimum = (await preview.boundingBox()).width;
  await separator.press('ArrowLeft');
  assert((await preview.boundingBox()).width > minimum, '向左键应扩大右侧预览');
  await separator.press('ArrowRight');
  assert(Math.abs((await preview.boundingBox()).width - minimum) <= 2, '向右键应缩小右侧预览');
  await separator.press('End');
  assert((await preview.boundingBox()).width >= (await workspace.boundingBox()).width * 0.97, '预览应可扩至几乎整个工作区');
  await separator.press('Home');

  const handle = await separator.boundingBox();
  const frame = await iframe.boundingBox();
  const available = await workspace.boundingBox();
  const y = frame.y + Math.min(60, frame.height / 2);
  await page.mouse.move(handle.x + handle.width / 2, y);
  await page.mouse.down();
  try {
    assert.equal(await iframe.evaluate(node => getComputedStyle(node).pointerEvents), 'none');
    await page.mouse.move(frame.x + Math.min(40, frame.width / 2), y, { steps: 4 });
    await page.mouse.move(available.x + 4, y, { steps: 12 });
    assert((await preview.boundingBox()).width >= available.width * 0.97, '指针跨过 PDF 区域后应仍可拖到工作区左缘');
  } finally {
    await page.mouse.up();
  }
  assert.notEqual(await iframe.evaluate(node => getComputedStyle(node).pointerEvents), 'none');

  const viewport = page.viewportSize();
  try {
    await page.setViewportSize({ width: 1000, height: 850 });
    await separator.waitFor();
    await separator.press('Home');
    await separator.press('End');
    assert((await preview.boundingBox()).width >= (await workspace.boundingBox()).width * 0.97, '较窄桌面仍可扩大预览');
    await page.setViewportSize({ width: 390, height: 844 });
    await separator.waitFor({ state: 'hidden' });
    assert((await preview.boundingBox()).width >= 388, '手机上的预览应占满宽度');
    await preview.getByRole('button', { name: '关闭文件预览' }).click();
    await preview.waitFor({ state: 'hidden' });
  } finally {
    await page.setViewportSize(viewport);
  }
}

async function inspectWorkspaceInputs(ui, conversation, word) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${conversation.id}`);
  await page.getByLabel('当前会话', { exact: true }).locator('.thread-heading')
    .getByText(conversation.title, { exact: true }).waitFor();
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  await composer.fill('已有草稿');
  assert(await composer.evaluate(node => {
    const clipboardData = new DataTransfer();
    clipboardData.setData('text/plain', '纯文本');
    return node.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
  }), '纯文本应交由输入框处理');
  await composer.press('End');
  await page.keyboard.insertText('纯文本');
  assert.equal(await composer.inputValue(), '已有草稿纯文本');

  const wordName = 'clipboard-report.docx';
  const upload = matches => page.waitForResponse(async response => response.ok()
    && response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith(`/conversations/${conversation.id}/files`)
    && matches((await response.json()).name));
  const uploaded = [upload(name => /^粘贴图片-\d+-1\.png$/.test(name)), upload(name => name === wordName)];
  assert(await composer.evaluate((node, data) => {
    const clipboardData = new DataTransfer();
    const canvas = document.createElement('canvas');
    canvas.width = 80;
    canvas.height = 60;
    const context = canvas.getContext('2d');
    context.fillStyle = '#336699';
    context.fillRect(0, 0, 80, 60);
    const bytes = value => Uint8Array.from(atob(value), char => char.charCodeAt(0));
    clipboardData.items.add(new File([bytes(canvas.toDataURL('image/png').split(',')[1])], 'image.png', { type: 'image/png' }));
    clipboardData.items.add(new File([bytes(data.word)], data.wordName, {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    }));
    clipboardData.setData('text/plain', '文件复制附带的文本');
    return !node.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
  }, { wordName, word: word.toString('base64') }), '带文件的粘贴应进入附件上传流程');
  const [screenshotResponse] = await Promise.all(uploaded);
  const { name: screenshotName } = await screenshotResponse.json();
  assert.equal(await composer.inputValue(), '已有草稿纯文本', '粘贴附件应保留当前草稿');

  const attachments = page.getByLabel('待发送附件', { exact: true });
  const screenshot = attachments.getByRole('button', { name: screenshotName, exact: true });
  const report = attachments.getByRole('button', { name: wordName, exact: true });
  await screenshot.locator('img[src^="blob:"]').waitFor();
  await report.waitFor();
  await page.getByText(/^正在上传 \d+ 个附件…$/).waitFor({ state: 'hidden' });
  assert.equal(await attachments.getByRole('listitem').count(), 2, '同一份剪贴板文件只应附加一次');
  await report.click();
  await inspectPreviewResize(page);
}

async function submitDate(ui, task, values) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = page.getByLabel('文档资料补充', { exact: true }).filter({ hasText: '需要补充资料' });
  await card.locator('input[type=date]').fill(values.finish_date);
  const result = page.waitForResponse(response => response.url().endsWith(`/runs/${task.run_id}/input`)
    && response.request().method() === 'POST');
  await card.getByRole('button', { name: '提交并继续' }).click();
  const response = await result;
  assert(response.ok(), await response.text());
  await runCard(page, task.run_id).getByLabel('已生成的 Word 文档').waitFor();
}

async function submitReview(ui, task, pending, review, values) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = runCard(page, task.run_id).getByLabel('文档改动审阅');
  await card.getByText('等待审阅', { exact: true }).waitFor();
  const getContext = page.waitForResponse(response => response.url().endsWith(`/runs/${task.run_id}/document-review/context`));
  await card.getByRole('button', { name: '查看修改稿' }).click();
  const response = await getContext;
  assert(response.ok(), await response.text());
  const context = await response.json();
  const dialog = page.getByRole('dialog', { name: '审阅文档修改稿' });
  const finish = dialog.getByRole('button', { name: '保存所选' });
  assert(await finish.isDisabled());
  for (const group of context.groups) {
    await dialog.getByRole('navigation', { name: '受影响的章节' }).getByRole('button', { name: group.title, exact: false }).click();
    const changes = review.changes.filter(change => group.change_ids.includes(change.id));
    const { proposed: after, original: before } = await reviewPanes(dialog);
    if (changes.some(change => change.after_image_url)) await after.locator('img[src^="blob:"]').first().waitFor();
    for (const change of changes.filter(change => change.kind === 'text' && change.after)) {
      assert((await after.textContent()).includes(change.after));
    }
    for (const change of changes.filter(change => change.kind === 'text' && change.before)) {
      assert((await before.textContent()).includes(change.before));
    }
    if (changes.some(change => change.before_image_url)) await before.locator('img[src^="blob:"]').first().waitFor();
    const selected = group.change_ids.filter(id => values.accepted_change_ids.includes(id));
    if (selected.length === group.change_ids.length) {
      await dialog.getByRole('button', { name: '选中本章节' }).click();
    } else if (selected.length) {
      await dialog.getByText('逐处查看', { exact: true }).click();
      for (const id of selected) {
        await dialog.getByRole('checkbox', { name: `选中第 ${group.change_ids.indexOf(id) + 1} 处改动` }).check();
      }
    }
  }
  assert(await finish.isEnabled());
  const result = page.waitForResponse(response => response.url().endsWith(`/runs/${task.run_id}/input`)
    && response.request().method() === 'POST');
  await finish.click();
  const saved = await result;
  assert.deepEqual(saved.request().postDataJSON(), { request_id: pending.request_id, values });
  assert(saved.ok(), await saved.text());
  const status = values.accepted_change_ids.length === review.changes.length ? '已应用修改' : '已保存所选修改';
  await card.getByText(status, { exact: true }).waitFor();
}

async function inspectRewrite(ui, task) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = runCard(page, task.run_id).getByLabel('文档改动审阅');
  await card.getByRole('button', { name: '查看修改稿' }).click();
  const dialog = page.getByRole('dialog', { name: '审阅文档修改稿' });
  const { proposed, original } = await reviewPanes(dialog);
  await proposed.getByRole('table').getByRole('cell', { name: '17 项' }).waitFor();
  if (process.env.DOCUMENT_QA_REVIEW_CAPTURE) {
    await page.screenshot({ path: process.env.DOCUMENT_QA_REVIEW_CAPTURE, animations: 'disabled' });
  }
  assert.equal(await dialog.getByText('逐处查看', { exact: true }).count(), 0);
  await dialog.getByText('查看修改要求', { exact: true }).click();
  await dialog.getByText('重写第三章', { exact: false }).waitFor();
  await original.getByText('本季度交付了 17 项成果', { exact: false }).waitFor();
  assert.equal(await original.getByRole('table').count(), 0);
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    await reviewPanes(dialog);
  } finally {
    await page.setViewportSize(viewport);
  }
  await dialog.getByRole('button', { name: '保存全部修改' }).click();
  await card.getByText('已应用修改', { exact: true }).waitFor();
}

async function discardReview(ui, task) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = runCard(page, task.run_id).getByLabel('文档改动审阅');
  await card.getByRole('button', { name: '查看修改稿' }).click();
  const dialog = page.getByRole('dialog', { name: '审阅文档修改稿' });
  await reviewPanes(dialog);
  const result = page.waitForResponse(response => response.url().endsWith(`/runs/${task.run_id}/input`)
    && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '保留原文' }).click();
  const response = await result;
  assert.deepEqual(response.request().postDataJSON().values, { accepted_change_ids: [] });
  assert(response.ok(), await response.text());
  await card.getByText('已保留原文', { exact: true }).waitFor();
}

async function inspectVersion(ui, task, artifact, expectedWord) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = runCard(page, task.run_id).getByLabel('已生成的 Word 文档');
  await card.getByText('v2', { exact: true }).waitFor();
  const pdfEvent = page.waitForResponse(response => response.url().endsWith(`/documents/${artifact.document_id}/versions/2/preview.pdf`));
  await card.getByRole('button', { name: '预览', exact: true }).click();
  const pdf = await pdfEvent;
  assert.equal(pdf.status(), 200);
  assert.match(pdf.headers()['content-type'], /^application\/pdf/);
  assert.equal((await pdf.body()).subarray(0, 5).toString(), '%PDF-');
  await page.getByLabel('文件预览').locator('iframe[src^="blob:"]').waitFor();
  await page.getByRole('button', { name: '关闭文件预览' }).click();
  const downloadEvent = page.waitForEvent('download');
  await card.getByRole('button', { name: '下载', exact: true }).click();
  const download = await downloadEvent;
  assert.match(download.suggestedFilename(), /\.docx$/);
  assert.deepEqual(await readFile(await download.path()), expectedWord);
  await card.getByRole('link', { name: '版本历史' }).click();
  await page.waitForURL(url => url.pathname === '/documents' && url.searchParams.get('document') === String(artifact.document_id));
  const history = page.getByRole('dialog', { name: artifact.title, exact: true });
  await history.getByText('第三章从完成情况角度重组成果描述').waitFor();
  await history.getByRole('button', { name: '查看审阅记录' }).click();
  const audit = page.getByRole('dialog', { name: '审阅文档修改稿' });
  await reviewPanes(audit);
  await audit.getByRole('navigation').getByText('部分应用', { exact: true }).waitFor();
  await audit.getByRole('button', { name: '关闭', exact: true }).last().click();
  await history.getByLabel('版本历史').selectOption('1');
  await history.getByRole('link', { name: '继续修改此版本' }).click();
  await page.waitForURL(url => url.pathname === '/workspace' && url.searchParams.get('version') === '1');
  assert.equal(new URL(page.url()).searchParams.get('document'), String(artifact.document_id));
}

async function resumeDocument(ui, task) {
  const { page, frontend } = ui;
  await page.goto(`${frontend}/workspace?conversation=${task.conversation_id}&run=${task.run_id}`);
  const card = runCard(page, task.run_id);
  await card.getByText('已完成 2/4 个部分', { exact: false }).waitFor();
  const resume = page.waitForResponse(response => response.url().endsWith(`/runs/${task.run_id}/retry`)
    && response.request().method() === 'POST');
  await card.getByRole('button', { name: '继续文档任务' }).click();
  const response = await resume;
  assert(response.ok(), await response.text());
  assert.equal((await response.json()).run_id, task.run_id);
  await card.getByLabel('已生成的 Word 文档').waitFor();
  await page.reload();
  await runCard(page, task.run_id).getByLabel('已生成的 Word 文档').waitFor();
}

module.exports = { openBrowser, inspectWorkspaceInputs, submitDate, submitReview, discardReview, inspectRewrite, inspectVersion, resumeDocument };
