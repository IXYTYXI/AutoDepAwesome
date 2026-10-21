/* Platform sessions and CSRF values stay in memory; the session cookie is HttpOnly. */
(() => {
  'use strict';
  const nativeFetch=window.fetch.bind(window);
  const get=id=>document.getElementById(id);
  const listeners=new Set();
  let current=null;
  let pending=null;
  let failure='正在检查登录状态，请稍后重试。';
  const notices={
    expired:'登录已失效。请在新标签页重新登录，再返回这里检查登录；当前填写内容会保留。',
    denied:'当前账号没有访问权限，请联系管理员。可在新标签页切换账号后重新检查登录。',
    unavailable:'暂时无法检查登录状态，请稍后重试。当前填写内容会保留。',
  };
  function notice(message) {
    const panel=get('auth-notice');
    if(panel)panel.hidden=!message;
    if(get('auth-notice-text'))get('auth-notice-text').textContent=message || '';
  }
  function blocked(message) {
    current=null;failure=message;notice(message);
    for(const listener of listeners)listener(null);
    if(get('auth-name'))get('auth-name').textContent='登录待确认';
    if(get('auth-role'))get('auth-role').textContent='';
    if(get('auth-logout'))get('auth-logout').hidden=true;
    if(get('auth-copy-id'))get('auth-copy-id').hidden=true;
  }
  function authError() {const error=new Error(failure);error.auth=true;return error;}
  function render(session) {
    if(get('auth-name'))get('auth-name').textContent=session.displayName || (session.mode==='basic'?'管理员':'飞书用户');
    if(get('auth-role'))get('auth-role').textContent=session.role==='admin'?'管理员':'成员';
    if(get('auth-logout'))get('auth-logout').hidden=session.mode!=='feishu';
    if(get('auth-session'))get('auth-session').setAttribute('aria-busy','false');
    notice('');
    if(get('auth-copy-id'))get('auth-copy-id').hidden=!session.userId;
    for(const listener of listeners)listener(session);
  }
  async function refresh() {
    if(pending)return pending;
    const retry=get('auth-retry');
    if(retry){retry.disabled=true;retry.textContent='正在检查…';}
    if(get('auth-session'))get('auth-session').setAttribute('aria-busy','true');
    pending=(async()=>{
      try {
        const response=await nativeFetch('/auth/session',{credentials:'same-origin',cache:'no-store'});
        if(!response.ok){blocked(response.status===401?notices.expired:response.status===403?notices.denied:notices.unavailable);return null;}
        const value=await response.json();
        if(value.authenticated!==true || !['basic','feishu'].includes(value.mode) || (value.mode==='feishu' && typeof value.csrfToken!=='string') || (value.mode==='feishu' && !value.csrfToken)) {
          blocked(notices.expired);return null;
        }
        current=value;render(value);return value;
      } catch {blocked(notices.unavailable);return null;}
      finally {
        if(retry){retry.disabled=false;retry.textContent='重新检查登录';}
        if(get('auth-session'))get('auth-session').setAttribute('aria-busy','false');
        pending=null;
      }
    })();
    return pending;
  }
  async function authenticatedFetch(url,options={}) {
    const destination=new URL(url,window.location.origin);
    if(destination.origin!==window.location.origin)throw new Error('平台请求必须使用同源地址。');
    await (pending || ready);
    if(!current)throw authError();
    const headers=new Headers(options.headers || {});
    headers.delete('X-CSRF-Token');
    const method=(options.method || 'GET').toUpperCase();
    if(current.mode==='feishu' && !['GET','HEAD','OPTIONS'].includes(method))headers.set('X-CSRF-Token',current.csrfToken);
    const response=await nativeFetch(url,{...options,headers,credentials:'same-origin'});
    if(response.status===401)blocked(notices.expired);
    else if(response.status===403) {
      let code;
      try {const data=await response.clone().json();code=data.code || data.error?.code || data.detail?.code;} catch {}
      notice(['AUTH_REAUTH_REQUIRED','REAUTH_REQUIRED'].includes(code)?'此操作需要最近 10 分钟内的登录确认。请在新标签页重新登录，再返回检查登录后重试。':'此操作未获授权，请联系管理员，或重新检查登录状态后重试。');
    }
    return response;
  }
  get('auth-copy-id')?.addEventListener('click',async()=>{
    if(!current?.userId)return;
    try {await window.navigator.clipboard.writeText(current.userId);if(get('auth-copy-status'))get('auth-copy-status').textContent='成员 ID 已复制';}
    catch {if(get('auth-copy-status'))get('auth-copy-status').textContent='复制失败，请在成员管理页查看你的成员 ID。';}
  });
  get('auth-retry')?.addEventListener('click',()=>refresh());
  get('auth-logout')?.addEventListener('click',async()=>{
    const button=get('auth-logout');
    if(button.disabled)return;
    button.disabled=true;button.textContent='正在退出…';button.setAttribute('aria-busy','true');
    try {
      const response=await authenticatedFetch('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      if(!response.ok)throw new Error('logout failed');
      current=null;window.location.assign('/login');
    } catch {notice('退出未完成，请检查连接后重试。也可以重新检查登录状态。');}
    finally {button.disabled=false;button.textContent='退出登录';button.removeAttribute('aria-busy');}
  });
  const ready=refresh();
  window.AwesomeAuth={ready,refresh,fetch:authenticatedFetch,session:()=>current,onChange:listener=>{listeners.add(listener);return ()=>listeners.delete(listener);}};
})();
