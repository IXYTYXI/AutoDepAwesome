(() => {
  'use strict';
  const links = [...document.querySelectorAll('.manual-sidebar [data-doc-id]')];
  const docs = new Map(links.map(link => [link.dataset.docId, {
    title: link.dataset.title, url: link.dataset.url, status: link.dataset.status,
  }]));
  const content = document.getElementById('manual-content');
  const state = document.getElementById('manual-state');
  const title = document.getElementById('manual-title');
  const status = document.getElementById('manual-status');
  const open = document.getElementById('manual-open');
  const reload = document.getElementById('manual-reload');
  let selected = null;
  let loggedIn = false;
  let requestNumber = 0;

  function requestedId() {
    const id = new URLSearchParams(window.location.search).get('doc');
    return docs.has(id) ? id : 'overview';
  }

  function scrollToSection(fragment) {
    if (!/^doxcn[A-Za-z0-9]{1,128}$/.test(fragment)) return false;
    const section = document.getElementById(fragment);
    if (!section || !content.contains(section)) return false;
    if (content.scrollHeight > content.clientHeight) {
      content.scrollTop += section.getBoundingClientRect().top - content.getBoundingClientRect().top - 18;
    } else {
      section.scrollIntoView({block: 'start'});
    }
    section.focus({preventScroll: true});
    return true;
  }

  async function load(id, preserveScroll = false) {
    const previousScroll = preserveScroll ? {top: content.scrollTop, left: content.scrollLeft} : null;
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
      content.scrollTop = previousScroll ? previousScroll.top : 0;
      content.scrollLeft = previousScroll ? previousScroll.left : 0;
      if (!previousScroll) scrollToSection(window.location.hash.slice(1));
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
    status.textContent = doc.status === '历史文档'
      ? '历史文档 · 仅供追溯；正文中的“当前”指编写当时，请以当前可用文档为准。'
      : doc.status === '待实施规范'
        ? '待实施规范 · 文中描述的能力尚未全部上线，请勿据此判断平台已支持。'
        : doc.status;
    status.dataset.status = doc.status;
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
    if (changed || force || content.hidden) load(id, !changed && !content.hidden);
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
  content.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href^="#doxcn"]');
    if (!link || !content.contains(link)) return;
    const fragment = link.getAttribute('href').slice(1);
    if (!scrollToSection(fragment)) return;
    event.preventDefault();
    window.history.pushState({doc: selected}, '', '#' + fragment);
  });
  window.addEventListener('popstate', () => show(requestedId()));
  window.addEventListener('hashchange', () => {
    if (!content.hidden) scrollToSection(window.location.hash.slice(1));
  });
  reload.addEventListener('click', () => { if (loggedIn && selected) show(selected, true); });
  window.AwesomeAuth.onChange(session => {
    loggedIn = !!session;
    show(requestedId(), !!session);
  });
  show(requestedId());
})();
