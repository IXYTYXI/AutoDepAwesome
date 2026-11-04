(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const form=$('credential-form');
  const dialog=$('revoke-dialog');
  let selectedCredential=null;
  let busy=false;
  const date=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'});
  const node=(tag,value,className)=>{const item=document.createElement(tag);item.textContent=value;if(className)item.className=className;return item;};
  function message(error) {return error?.error?.message || (typeof error?.detail==='string' ? error.detail : '操作未完成，请检查登录状态后重试。');}
  async function request(url,options={}) {
    const response=await window.AwesomeAuth.fetch(url,{...options,credentials:'same-origin'});
    let data;
    try {data=await response.json();} catch {throw new Error('无法读取平台响应，请稍后重试。');}
    if(!response.ok){const error=new Error(message(data));error.code=data?.error?.code;throw error;}
    return data;
  }
  function clearIssued() {$('issued-token').textContent='';$('issued-panel').hidden=true;$('copy-status').textContent='';}
  function syncScope() {$('app-scope').hidden=form.elements.scope.value!=='app';}
  function selectedApp() {return document.querySelector('input[name="selectedApp"]:checked')?.value || null;}
  async function loadApps() {
    const area=$('app-scope-list');
    try {
      const data=await request('/api/v1/apps');
      const items=(data.items || []).filter(item=>item.permissions?.deploy===true);
      area.replaceChildren();
      if(!items.length){$('app-scope-help').textContent='当前没有可更新的应用。可选择“我有权部署的应用”或“仅创建新应用”。';return;}
      $('app-scope-help').textContent=`可更新 ${new Intl.NumberFormat('zh-CN').format(items.length)} 个应用。`;
      for(const app of items){
        const label=node('label','','app-choice');
        const input=document.createElement('input');input.type='radio';input.name='selectedApp';input.value=app.id;
        const copy=node('span','','app-choice-copy');
        copy.append(node('strong',app.subdomain || app.lastSubdomain || '未绑定域名'),node('small',app.repository || app.id));
        label.append(input,copy);area.append(label);
      }
      area.querySelector('input')?.setAttribute('checked','');
    } catch(error) {$('app-scope-help').textContent=`无法读取应用：${error.message}`;area.replaceChildren();}
  }
  function scopeText(item) {
    if(item.allowAnyApp)return '本人有权部署的应用 + 创建新应用';
    if(item.allowCreate)return '仅创建新应用';
    return `仅应用 ${item.appId || '未知'}`;
  }
  async function loadCredentials() {
    $('list-status').textContent='正在读取凭证…';
    try {
      const data=await request('/api/v1/api-credentials');
      const list=$('credential-list');list.replaceChildren();
      const items=data.items || [];
      if(!items.length)list.append(node('p','还没有个人凭证。可在左侧创建。','api-empty'));
      for(const item of items){
        const row=node('article','','credential-row');
        const content=node('div','','credential-copy');
        const title=node('strong',item.name);
        const status=item.revokedAt ? '已撤销' : item.expiresAt<=Date.now()/1000 ? '已到期' : '有效';
        const badge=node('span',status,`credential-status ${status==='有效'?'is-active':'is-inactive'}`);
        const headline=node('div','','credential-headline');headline.append(title,badge);
        content.append(headline,node('p',scopeText(item)),node('small',`到期：${date.format(new Date(item.expiresAt*1000))} · 编号：${item.id}`));
        row.append(content);
        if(status==='有效'){
          const button=node('button','撤销凭证','secondary revoke-button');button.type='button';
          button.addEventListener('click',()=>{selectedCredential=item;$('revoke-description').textContent=`将撤销“${item.name}”（${scopeText(item)}）。`;$('revoke-error').textContent='';dialog.showModal();$('revoke-cancel').focus();});
          row.append(button);
        }
        list.append(row);
      }
      $('list-status').textContent=`共 ${new Intl.NumberFormat('zh-CN').format(items.length)} 枚个人凭证；完整值不会出现在列表。`;
    } catch(error) {$('list-status').textContent=`读取失败：${error.message}`;}
  }
  form.addEventListener('change',event=>{if(event.target.name==='scope')syncScope();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;
    $('create-error').textContent='';
    const name=$('credential-name').value.trim();
    const days=Number($('credential-days').value);
    const scope=form.elements.scope.value;
    const appId=scope==='app'?selectedApp():null;
    if(!name || name.length>80){$('create-error').textContent='请填写 1–80 字符的凭证名称。';$('credential-name').focus();return;}
    if(!Number.isInteger(days) || days<1 || days>90){$('create-error').textContent='有效期须为 1–90 天的整数。';$('credential-days').focus();return;}
    if(scope==='app' && !appId){$('create-error').textContent='请先选择一个有部署权限的应用。';$('app-scope-list').focus();return;}
    busy=true;$('create-button').disabled=true;$('create-button').textContent='正在创建…';
    try {
      const body={name,days,allowAnyApp:scope==='all',allowCreate:scope==='create'};
      if(appId)body.appId=appId;
      const issued=await request('/api/v1/api-credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      clearIssued();$('issued-token').textContent=issued.token;$('issued-panel').hidden=false;$('issued-title').focus();
      form.reset();syncScope();await loadCredentials();
    } catch(error) {
      $('create-error').textContent=error.code==='AUTH_REAUTH_REQUIRED'?'请在新标签页重新使用飞书登录，再返回创建凭证。':error.message;
      $('create-error').focus();
    } finally {busy=false;$('create-button').disabled=false;$('create-button').textContent='创建凭证';}
  });
  $('copy-token').addEventListener('click',async()=>{
    try {await navigator.clipboard.writeText($('issued-token').textContent);$('copy-status').textContent='已复制，请立即保存到调用方的秘密配置。';}
    catch {$('copy-status').textContent='复制失败，请手动选中并复制上方凭证。';}
  });
  $('dismiss-token').addEventListener('click',clearIssued);
  window.addEventListener('pagehide',clearIssued);
  $('refresh-credentials').addEventListener('click',loadCredentials);
  $('revoke-cancel').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{selectedCredential=null;});
  $('revoke-confirm').addEventListener('click',async()=>{
    if(!selectedCredential || busy)return;
    busy=true;$('revoke-confirm').disabled=true;$('revoke-confirm').textContent='正在撤销…';
    try {await request(`/api/v1/api-credentials/${encodeURIComponent(selectedCredential.id)}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:'{}'});dialog.close();await loadCredentials();}
    catch(error) {$('revoke-error').textContent=error.code==='AUTH_REAUTH_REQUIRED'?'请重新使用飞书登录后再撤销。':error.message;}
    finally {busy=false;$('revoke-confirm').disabled=false;$('revoke-confirm').textContent='确认撤销';}
  });
  window.AwesomeAuth.ready.then(session=>{
    if(!session || session.mode!=='feishu' || !session.canDeploy){
      form.querySelectorAll('input,button').forEach(input=>input.disabled=true);
      $('create-error').textContent='当前账号没有创建 API 凭证的权限。请确认飞书登录和部署权限。';
      if(session?.mode==='feishu')loadCredentials();
      return;
    }
    loadApps();loadCredentials();
  });
})();
