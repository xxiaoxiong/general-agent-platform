const endpoint = new URL(location.href).searchParams.get('api') || 'http://127.0.0.1:8765';
const tickets = document.querySelector('#tickets');
const search = document.querySelector('#search');
const filter = document.querySelector('#filter');
const form = document.querySelector('form');
const notice = document.querySelector('#notice');
let generation = 0;

async function api(path, options = {}) {
  const response = await fetch(`${endpoint}${path}`, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function action(label, callback) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await callback();
      await refresh();
    } catch (error) {
      notice.textContent = error.message;
      button.disabled = false;
    }
  });
  return button;
}

async function refresh() {
  const revision = ++generation;
  try {
    const query = new URLSearchParams({ q: search.value, status: filter.value });
    const { items } = await api(`/api/tickets?${query}`);
    if (revision !== generation) return;
    tickets.replaceChildren(...items.map(item => {
      const row = document.createElement('li');
      const title = document.createElement('strong');
      title.textContent = item.title;
      const status = document.createElement('span');
      status.textContent = item.status === 'done' ? '已完成' : '待处理';
      const next = item.status === 'done' ? 'open' : 'done';
      row.append(title, status, action(next === 'done' ? '标为完成' : '重新打开', () =>
        api(`/api/tickets/${item.id}`, { method: 'PATCH', body: JSON.stringify({ status: next }) })),
      action('删除工单', () => api(`/api/tickets/${item.id}`, { method: 'DELETE' })));
      return row;
    }));
    notice.textContent = items.length ? `显示 ${items.length} 条工单` : '没有符合条件的工单';
  } catch (error) {
    if (revision === generation) notice.textContent = `加载失败 · ${error.message}`;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const title = new FormData(form).get('title').trim();
    await api('/api/tickets', { method: 'POST', body: JSON.stringify({ title }) });
    form.reset();
    search.value = '';
    filter.value = '';
    await refresh();
  } catch (error) {
    notice.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
search.addEventListener('input', refresh);
filter.addEventListener('change', refresh);
void refresh();
