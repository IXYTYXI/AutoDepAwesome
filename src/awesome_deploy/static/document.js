(() => {
  'use strict';
  const links = [...document.querySelectorAll('.manual-sidebar [data-doc-id]')];
  const docs = new Map(links.map(link => [link.dataset.docId, {
    title: link.dataset.title,
    url: link.dataset.url,
    link,
  }]));
  const frame = document.getElementById('manual-frame');
  const state = document.getElementById('manual-state');
  const title = document.getElementById('manual-title');
  const open = document.getElementById('manual-open');
  const reload = document.getElementById('manual-reload');
  let selected = null;
  let loggedIn = false;

  function requestedId() {
    const id = new URLSearchParams(window.location.search).get('doc');
    return docs.has(id) ? id : 'overview';
  }

  function show(id) {
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
      frame.hidden = true;
      frame.removeAttribute('src');
      reload.disabled = true;
      state.hidden = false;
      state.textContent = '请先登录部署平台，再查看文档。';
      return;
    }
    if (!changed && frame.hasAttribute('src')) return;
    state.hidden = false;
    state.textContent = '正在载入飞书文档…';
    frame.hidden = false;
    frame.title = doc.title + ' · 飞书文档内容';
    frame.src = doc.url;
    reload.disabled = false;
  }

  for (const link of links) {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const id = link.dataset.docId;
      window.history.pushState({ doc: id }, '', link.href);
      show(id);
    });
  }
  window.addEventListener('popstate', () => show(requestedId()));
  frame.addEventListener('load', () => {
    if (loggedIn && frame.getAttribute('src')) state.hidden = true;
  });
  reload.addEventListener('click', () => {
    if (!loggedIn || !selected) return;
    state.hidden = false;
    state.textContent = '正在重新载入飞书文档…';
    frame.src = docs.get(selected).url;
  });
  window.AwesomeAuth.onChange(session => {
    loggedIn = !!session;
    show(requestedId());
  });
  show(requestedId());
  window.AwesomeAuth.ready.then(session => {
    loggedIn = !!session;
    show(requestedId());
  });
})();
