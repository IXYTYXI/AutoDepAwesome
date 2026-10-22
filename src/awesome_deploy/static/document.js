(() => {
  'use strict';
  const links = [...document.querySelectorAll('.manual-sidebar [data-doc-id]')];
  const docs = new Map(links.map(link => [link.dataset.docId, {
    title: link.dataset.title, url: link.dataset.url,
  }]));
  const content = document.getElementById('manual-content');
  const state = document.getElementById('manual-state');
  const title = document.getElementById('manual-title');
  const open = document.getElementById('manual-open');
  const reload = document.getElementById('manual-reload');
  let selected = null;
  let loggedIn = false;
  let requestNumber = 0;

  function requestedId() {
    const id = new URLSearchParams(window.location.search).get('doc');
    return docs.has(id) ? id : 'overview';
  }

  async function load(id) {
    const number = ++requestNumber;
    state.hidden = false;
    state.textContent = '正在读取文档…';
    content.hidden = true;
    content.replaceChildren();
    reload.disabled = true;
    try {
      const response = await window.AwesomeAuth.fetch('/document/content/' + encodeURIComponent(id));
      if (!response.ok) throw new Error('读取失败：HTTP ' + response.status);
      const body = await response.text();
      if (number !== requestNumber || selected !== id || !loggedIn) return;
      // The server serves only pre-rendered, allowlisted and sanitized document fragments.
      content.innerHTML = body;
      content.hidden = false;
      state.hidden = true;
    } catch (error) {
      if (number !== requestNumber || selected !== id) return;
      state.textContent = error.auth ? '请先登录部署平台，再查看文档。' : '文档暂时无法读取，请重试或在飞书打开。';
    } finally {
      if (number === requestNumber) reload.disabled = !loggedIn;
    }
  }

  function show(id, force = false) {
    const doc = docs.get(id);
    if (!doc) return;
    const changed = selected !== id;
    selected = id;
    title.textContent = doc.title;
    open.href = doc.url;
    for (const link of links) {
      if (link.dataset.docId === id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    if (!loggedIn) {
      ++requestNumber;
      content.replaceChildren();
      content.hidden = true;
      reload.disabled = true;
      state.hidden = false;
      state.textContent = '请先登录部署平台，再查看文档。';
      return;
    }
    if (changed || force || content.hidden) load(id);
  }

  for (const link of links) {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const id = link.dataset.docId;
      window.history.pushState({doc: id}, '', link.href);
      show(id);
    });
  }
  window.addEventListener('popstate', () => show(requestedId()));
  reload.addEventListener('click', () => { if (loggedIn && selected) show(selected, true); });
  window.AwesomeAuth.onChange(session => {
    loggedIn = !!session;
    show(requestedId(), !!session);
  });
  show(requestedId());
})();
