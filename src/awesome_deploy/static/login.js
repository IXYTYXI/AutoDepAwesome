(() => {
  'use strict';
  const errors={
    cancelled:'你已取消飞书授权。准备好后可重新登录。',
    expired:'这次登录已过期，请重新发起飞书登录。',
    denied:'当前账号尚未获得平台访问权限，请联系管理员确认后重新登录。',
    unavailable:'飞书登录暂时不可用，请稍后重试；若持续失败，请联系管理员。',
    invalid:'无法验证这次登录，请从此页面重新发起飞书登录。',
    session:'登录已失效，请重新登录后继续。',
  };
  const error=document.getElementById('login-error');
  error.hidden=true;error.textContent='';
  const code=new URLSearchParams(window.location.search).get('error');
  if(Object.prototype.hasOwnProperty.call(errors,code)){error.textContent=errors[code];error.hidden=false;}
  const button=document.getElementById('feishu-login');
  const label=document.getElementById('feishu-login-label');
  let leaving=false;
  button.addEventListener('click',event=>{
    if(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)return;
    if(leaving){event.preventDefault();return;}
    leaving=true;button.setAttribute('aria-busy','true');button.setAttribute('aria-disabled','true');label.textContent='正在前往飞书…';
  });
  window.addEventListener('pageshow',()=>{
    leaving=false;button.removeAttribute('aria-busy');button.removeAttribute('aria-disabled');label.textContent='使用飞书登录';
  });
})();
