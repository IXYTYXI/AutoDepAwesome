/* Secret values live only in the active input and the explicit write request. */
(() => {
  'use strict';
  const get=id=>document.getElementById(id);
  const operationLabels={queued:'吊销排队中',running:'吊销处理中',recovery_required:'需人工处理',succeeded:'平台内停用已确认',failed:'需人工处理'};
  const versionLabels={active:'可用',revoked:'已吊销'};
  let active=null,data=null,fresh=false,editing=null,submitting=false,reading=null,timer=null,epoch=0;
  let returnFocus=null,returnResolver=null,expanded=new Set();
  const canOpen=app=>{
    const session=window.AwesomeAuth?.session();
    return Boolean(session?.mode==='feishu' && session.capabilities?.vault_enabled===true && session.canDeploy===true && (app?.permissions?.manageSecrets===true || app?.permissions?.manageSecrets===undefined && app?.permissions?.deploy===true));
  };
  const element=(tag,value='',className='')=>{
    const node=document.createElement(tag);node.textContent=value;
    if(className)node.className=className;return node;
  };
  const button=(label,key,callback,className='secondary')=>{
    const node=element('button',label,className);node.type='button';node.setAttribute('data-vault-action',key);node.addEventListener('click',callback);return node;
  };
  const descendants=node=>[node,...Array.from(node.children).flatMap(descendants)];
  const find=(node,key)=>{
    if(!node || !key)return null;
    if(node.getAttribute('data-vault-action')===key)return node;
    for(const child of node.children){const match=find(child,key);if(match)return match;}return null;
  };
  function statusText(error,fallback){return typeof error?.message==='string'?error.message:fallback;}
  async function request(url,options={}){
    const controller=new AbortController();let timedOut=false;
    const timeout=setTimeout(()=>{timedOut=true;controller.abort();},30000);
    try{
      let response,result;
      try{response=await window.AwesomeAuth.fetch(url,{cache:'no-store',...options,signal:controller.signal});}
      catch(error){
        if(error?.auth)throw error;
        const failure=new Error(timedOut?'请求等待超时，无法确认本次操作是否已生效。请刷新版本和吊销进度后核对。':'网络中断，无法确认本次请求是否已生效。请刷新版本和吊销进度后核对。');
        failure.uncertain=true;throw failure;
      }
      try{result=await response.json();}
      catch{const error=new Error('无法读取服务器结果，不能确认操作是否生效。请刷新状态后核对。');error.uncertain=true;throw error;}
      if(!response.ok){
        const detail=result?.error || result?.detail;
        const error=new Error(statusText(detail,typeof detail==='string'?detail:'请求未完成，请刷新状态或联系管理员。'));
        error.status=response.status;error.code=detail?.code || result?.code;error.uncertain=response.status>=500;throw error;
      }
      return result;
    }finally{clearTimeout(timeout);}
  }
  function path(){return `/api/v1/apps/${encodeURIComponent(active.id)}/secrets`;}
  function clearInput(){get('vault-value').value='';get('vault-value').type='password';get('vault-show').textContent='显示';get('vault-show').setAttribute('aria-pressed','false');get('vault-reason').value='';}
  function clearErrors(){for(const id of ['vault-form-error','vault-value-error','vault-reason-error'])get(id).textContent='';get('vault-value').removeAttribute('aria-invalid');get('vault-reason').removeAttribute('aria-invalid');}
  function hideEditor(restore=true){
    const key=editing?.actionKey;editing=null;clearInput();clearErrors();get('vault-editor').hidden=true;get('vault-browser').hidden=false;get('vault-reauth').hidden=true;get('vault-conflict').hidden=true;
    if(restore){const target=find(get('vault-items'),key)||find(get('vault-operations'),key)||get('vault-refresh');target.focus({preventScroll:true});if(document.activeElement!==target)get('vault-close').focus({preventScroll:true});}
  }
  function retryable(operation){
    const item=data?.items.find(item=>item.name===operation?.name);
    return Boolean(data?.can_revoke===true && operation?.app_id===active?.id && operation?.status==='recovery_required' && item?.kind==='runtime_secret' && item.versions.some(version=>version.version===operation.version && version.status==='revoked'));
  }
  function editable(){
    const item=data?.items.find(item=>item.name===editing?.name);
    if(editing?.retryOperationId)return retryable(data?.operations.find(operation=>operation.id===editing.retryOperationId && operation.name===editing.name && operation.version===editing.version));
    return Boolean(item?.kind==='runtime_secret' && (editing?.type==='rotate'?data.can_rotate===true:data?.can_revoke===true && item.versions.some(version=>version.version===editing?.version && version.status==='active')));
  }
  function sync(){
    const locked=submitting || !fresh || Boolean(editing?.blocked) || Boolean(editing && !editable());
    get('vault-submit').disabled=locked;get('vault-submit').textContent=submitting?'正在提交…':editing?.retryOperationId?'确认重试停用核验':editing?.type==='revoke'?'确认紧急吊销':'创建新版本';
    get('vault-submit').className=editing?.type==='revoke'?'vault-danger':'';
    for(const id of ['vault-close','vault-back','vault-cancel','vault-value','vault-reason','vault-show','vault-reconfirm'])get(id).disabled=submitting;
    get('vault-refresh').disabled=submitting || Boolean(reading);
    get('vault-form').setAttribute('aria-busy',String(submitting));
  }
  function when(value){
    const date=new Date(typeof value==='number'?value*1000:value);
    if(!value || !Number.isFinite(date.getTime()))return '创建时间未记录';
    return new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short'}).format(date);
  }
  function startEditor(name,type,version,retryOperationId=null){
    if(submitting || !fresh || !active || !canOpen(active))return;
    const item=data?.items.find(item=>item.name===name);
    if(!item || item.kind!=='runtime_secret' || type==='rotate' && data.can_rotate!==true || type==='revoke' && data.can_revoke!==true)return;
    const record=item.versions.find(row=>row.version===version);
    if(retryOperationId){if(type!=='revoke' || !retryable(data.operations.find(operation=>operation.id===retryOperationId && operation.name===name && operation.version===version)))return;}
    else if(type==='revoke' && (!record || record.status!=='active'))return;
    clearInput();clearErrors();
    editing={name,type,version,retryOperationId,expected:item.current_version,blocked:false,actionKey:retryOperationId?`retry:${retryOperationId}`:type==='rotate'?`rotate:${name}`:`revoke:${name}:${version}`};
    get('vault-editor-title').textContent=retryOperationId?'重试停用核验':type==='rotate'?'轮换运行密钥':'紧急吊销密钥版本';
    get('vault-editor-target').textContent=retryOperationId?`${name} · 已吊销版本 ${version} · 操作 ${retryOperationId}`:type==='rotate'?`${name} · 当前版本 ${item.current_version || '无'}`:`${name} · 吊销版本 ${version}`;
    get('vault-editor-description').textContent=retryOperationId?'继续原范围的停用与清理核验，不会扩大受影响实例范围，也不会恢复已吊销版本。请先排除上次故障并填写重试原因；后台确认全部实例停用后才会显示完成。':type==='rotate'?'提交后创建新版本，供后续新部署使用。已运行的服务和已固定配置的任务不会自动更新；请完成新部署验收，再决定是否吊销旧版本。':'此操作不可撤回，可能导致应用中断。平台将阻止该版本继续启动、发布和恢复，并停止使用它的服务；只有后台核验结束后才会显示停用已确认。外部供应商凭证需另行撤销。';
    get('vault-value-field').hidden=type!=='rotate';get('vault-reason-field').hidden=type!=='revoke';
    get('vault-browser').hidden=true;get('vault-editor').hidden=false;get('vault-conflict').hidden=true;get('vault-reauth').hidden=true;get('vault-message').textContent='';
    sync();get('vault-editor-title').focus();get('vault-dialog').scrollTop=0;
  }
  function renderItems(){
    const container=get('vault-items'),scroll=container.scrollTop,focused=document.activeElement;
    const key=container.contains(focused)?focused.getAttribute('data-vault-action'):null;
    const scrollPositions=new Map(descendants(container).filter(node=>node.getAttribute('data-vault-scroll')).map(node=>[node.getAttribute('data-vault-scroll'),node.scrollTop]));
    const rows=(data?.items||[]).map(item=>{
      const row=element('article','','vault-card'),head=element('div','','vault-card-head'),info=element('div');
      const title=element('h4',item.name);title.setAttribute('translate','no');info.append(title);
      const protectedKey=item.kind==='platform_managed_master';
      const meta=element('div','','vault-meta');meta.append(element('span',protectedKey?'托管主密钥':item.kind==='runtime_secret'?'运行密钥':'类型待确认',`vault-badge${protectedKey?' protected':''}`),element('span','当前版本：'),element('code',item.current_version || '无','vault-version-id'));const current=item.versions.find(version=>version.version===item.current_version);
      if(current)meta.append(element('span',versionLabels[current.status] || '状态待确认',`vault-badge${current.status==='revoked'?' revoked':''}`));
      info.append(meta);
      head.append(info);
      if(data.can_rotate===true){
        const rotate=button('轮换密钥',`rotate:${item.name}`,()=>startEditor(item.name,'rotate'));
        rotate.disabled=!fresh || item.kind!=='runtime_secret';rotate.setAttribute('aria-label',`轮换 ${item.name}`);head.append(rotate);
      }
      row.append(head);
      if(protectedKey && item.data_generation)row.append(element('p',`数据代际：${item.data_generation}`,'vault-status-detail'));
      if(protectedKey)row.append(element('p','此主密钥保护应用数据，普通轮换或吊销均被禁止。更换需专项迁移；保留数据时必须保留原值。','vault-status-detail'));
      else if(item.kind!=='runtime_secret')row.append(element('p','密钥类型尚未确认，当前禁止变更。请联系管理员核对。','vault-status-detail'));
      const details=element('details'),summary=element('summary',`版本历史 · ${item.versions.length} 个`);
      summary.setAttribute('data-vault-action',`versions:${item.name}`);details.open=expanded.has(item.name);details.addEventListener('toggle',()=>{if(details.open)expanded.add(item.name);else expanded.delete(item.name);});details.append(summary);
      const versions=element('div','','vault-version-list');versions.setAttribute('role','region');versions.setAttribute('aria-label',`${item.name} 版本历史`);versions.setAttribute('tabindex','0');versions.setAttribute('data-vault-scroll',item.name);versions.setAttribute('data-vault-action',`scroll:${item.name}`);
      for(const version of item.versions){
        const line=element('div','','vault-version'),copy=element('div'),lineMeta=element('div','','vault-meta'),code=element('code',version.version);code.setAttribute('translate','no');
        lineMeta.append(code,element('span',versionLabels[version.status] || '状态待确认',`vault-badge${version.status==='revoked'?' revoked':''}`));
        if(version.version===item.current_version)lineMeta.append(element('span','当前','vault-badge'));
        copy.append(lineMeta,element('time',when(version.created_at)));line.append(copy);
        if(data.can_revoke===true){
          const revoke=button('紧急吊销',`revoke:${item.name}:${version.version}`,()=>startEditor(item.name,'revoke',version.version),'vault-danger');
          revoke.disabled=!fresh || item.kind!=='runtime_secret' || version.status!=='active';revoke.setAttribute('aria-label',`紧急吊销 ${item.name}，版本 ${version.version}`);line.append(revoke);
        }
        versions.append(line);
      }
      details.append(versions);row.append(details);return row;
    });
    container.replaceChildren(...(rows.length?rows:[element('p',fresh?'该应用尚未保存密钥。部署时可按缺失配置提示补录。':'元数据暂不可用，请刷新状态或联系管理员。','empty')]));
    container.scrollTop=scroll;for(const node of descendants(container)){const position=scrollPositions.get(node.getAttribute('data-vault-scroll'));if(position!==undefined)node.scrollTop=position;}const refocused=find(container,key);if(key)(refocused && !refocused.disabled?refocused:get('vault-close')).focus({preventScroll:true});
    get('vault-count').textContent=`${rows.length} 项 · 仅显示安全元数据 · 展开版本历史查看状态`;
  }
  function renderOperations(){
    const container=get('vault-operations'),scroll=container.scrollTop,focused=document.activeElement;
    const key=container.contains(focused)?focused.getAttribute('data-vault-action'):null;
    const rows=(data?.operations||[]).map(operation=>{
      const row=element('article','','vault-operation'),meta=element('div','','vault-meta');
      meta.append(element('code',operation.name || '密钥名称未记录'),element('code',operation.version || '版本未记录'),element('span',operationLabels[operation.status] || '状态待确认',`vault-badge${['recovery_required','failed'].includes(operation.status)?' attention':''}`));
      row.append(meta);
      const detail=operation.status==='succeeded'?'平台已确认相关服务停用；外部供应商凭证仍需自行撤销。':operation.status==='queued'?'请求已受理，等待后台执行停用与清理核验。':operation.status==='running'?'正在停止受影响服务、隔离路由并核验结果。':operation.status==='recovery_required' || operation.status==='failed'?'平台尚未确认全部停用，恢复仍被阻止。请联系管理员处理；不能把此状态视为完成。':'未确认停用结果，请刷新状态或联系管理员。';
      row.append(element('p',detail,'vault-status-detail'));
      if(operation.error)row.append(element('p',typeof operation.error==='string'?operation.error:statusText(operation.error,'处理未完成，请联系管理员。'),'vault-status-detail'));
      if(retryable(operation)){
        const retry=button('重试停用核验',`retry:${operation.id}`,()=>startEditor(operation.name,'revoke',operation.version,operation.id));
        retry.disabled=!fresh;retry.setAttribute('aria-label',`重试停用核验 ${operation.name}，版本 ${operation.version}`);row.append(retry);
      }
      row.append(element('p',`操作 ${operation.id || '未记录'}`,'vault-status-detail'));return row;
    });
    container.replaceChildren(...rows);container.scrollTop=scroll;get('vault-operation-section').hidden=rows.length===0;
    if(key){const refocused=find(container,key);(refocused && !refocused.disabled?refocused:get('vault-close')).focus({preventScroll:true});}
    const status=(data?.operations||[]).map(operation=>`${operation.name}，版本 ${operation.version}，${operationLabels[operation.status] || '状态待确认'}`).join('。');
    if(get('vault-operation-status').textContent!==status)get('vault-operation-status').textContent=status;
  }
  function validMetadata(value){
    return value?.appId===active?.id && Array.isArray(value.items) && Array.isArray(value.operations) && value.items.every(item=>typeof item.name==='string' && (typeof item.current_version==='string'||item.current_version===null) && Array.isArray(item.versions) && item.versions.every(version=>typeof version.version==='string'));
  }
  function schedule(){
    clearTimeout(timer);timer=null;
    if(active && get('vault-dialog').open && !submitting && (data?.operations||[]).some(op=>['queued','running'].includes(op.status)))timer=setTimeout(()=>refresh(),3000);
  }
  async function refresh(){
    if(!active || submitting)return false;
    if(reading)return reading;
    const readEpoch=epoch;get('vault-items').setAttribute('aria-busy','true');get('vault-refresh').textContent='正在读取…';
    reading=(async()=>{
      try{
        const value=await request(path());
        if(readEpoch!==epoch || submitting)return false;
        if(!validMetadata(value))throw new Error('密钥元数据不完整，请刷新状态或联系管理员。');
        data=value;fresh=true;get('vault-error').textContent='';renderItems();renderOperations();
        if(editing?.blocked)showConflict();return true;
      }catch(error){
        if(readEpoch!==epoch || submitting)return false;
        fresh=false;
        get('vault-error').textContent=error.status===404?'此应用的加密密钥管理尚未启用，或当前已不可访问。请联系管理员确认迁移和权限。':error.code==='VAULT_APP_UNREGISTERED'?'该应用尚未完成加密密钥迁移，请联系管理员处理。':error.auth?'登录状态已失效，请重新登录。':`无法读取最新状态。${data?'以下为上次数据，暂不可变更。':''} ${error.message}`;
        if(error.status===403 || error.status===404 || error.auth){data=null;hideEditor(false);}
        renderItems();renderOperations();return false;
      }finally{
        if(readEpoch===epoch){reading=null;get('vault-items').setAttribute('aria-busy','false');get('vault-refresh').textContent='刷新状态';sync();schedule();}
      }
    })();sync();return reading;
  }
  function showConflict(){
    if(!editing?.blocked)return;
    const item=data?.items.find(item=>item.name===editing.name);
    const permitted=fresh && editable();
    get('vault-conflict').hidden=false;get('vault-reconfirm').hidden=!permitted;
    get('vault-conflict-text').textContent=permitted?`提交时的当前版本为 ${editing.expected || '无'}，现在为 ${item.current_version || '无'}。${editing.retryOperationId?'该停用核验仍需人工处理；继续只会重试原范围，不会扩大范围。':''}${editing.uncertain?'上次请求结果不确定，先核对版本历史和吊销进度；继续会发起新的请求。':'输入内容已保留；先核对最新版本，再明确确认是否重新提交。'}`:'无法确认可操作的最新版本。请返回列表，刷新状态并核对密钥权限或吊销进度。';
  }
  function acknowledge(){
    if(!editing?.blocked || submitting || !fresh)return;
    const item=data?.items.find(item=>item.name===editing.name);
    if(!editable())return;
    editing.expected=item.current_version;editing.blocked=false;editing.uncertain=false;
    get('vault-conflict').hidden=true;get('vault-form-error').textContent='已采用核对后的当前版本。请再次点击提交按钮发起变更。';sync();get('vault-submit').focus();
  }
  function validate(){
    clearErrors();if(!editing)return false;
    if(editing.type==='rotate'){
      const value=get('vault-value').value;
      const message=!value?'请输入新密钥值。':value.includes('\0')?'密钥不能包含空字符，请核对粘贴内容。':new TextEncoder().encode(value).length>16384?'密钥超过 16 KB，请核对是否误粘贴了其他内容。':'';
      if(message){get('vault-value-error').textContent=message;get('vault-value').setAttribute('aria-invalid','true');get('vault-value').focus();return false;}
    }else if(!get('vault-reason').value.trim()){
      get('vault-reason-error').textContent='请填写吊销原因。';get('vault-reason').setAttribute('aria-invalid','true');get('vault-reason').focus();return false;
    }
    return true;
  }
  async function submit(event){
    event.preventDefault();if(!active || !editing || submitting || editing.blocked || !fresh || !canOpen(active) || !editable())return;
    if(!validate())return;
    const intent={...editing},submitEpoch=epoch,url=path()+`/${encodeURIComponent(intent.name)}/${intent.type}`;
    const payload=intent.type==='rotate'?{value:get('vault-value').value,expected_current_version:intent.expected}:{version:intent.version,expected_current_version:intent.expected,reason:get('vault-reason').value.trim()};
    submitting=true;clearTimeout(timer);sync();get('vault-reauth').hidden=true;
    let recover=false;
    try{
      const result=await request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(submitEpoch!==epoch)return;
      const valid=intent.type==='rotate'?result?.name===intent.name && typeof result.current_version==='string':typeof result?.id==='string' && (!intent.retryOperationId || result.id===intent.retryOperationId) && result.app_id===active.id && result.name===intent.name && result.version===intent.version && Boolean(operationLabels[result.status]);
      if(!valid){const error=new Error('服务器结果不完整，无法确认操作是否生效。请刷新状态后核对。');error.uncertain=true;throw error;}
      clearInput();submitting=false;sync();hideEditor();
      get('vault-message').textContent=intent.retryOperationId?'原范围的停用核验重试已受理，不会扩大范围。请查看后台处理进度；已受理不代表全部停用。':intent.type==='rotate'?'新版本已保存，后续新部署将使用它。现有服务和已固定配置的任务不会自动更新。':'吊销请求已受理。请查看后台处理进度，只有“平台内停用已确认”才表示平台核验完成。';
      if(intent.type==='revoke'){data.operations=[result,...data.operations.filter(op=>op.id!==result.id)];renderOperations();}
      if(intent.retryOperationId)get('vault-close').focus({preventScroll:true});
      recover=true;
    }catch(error){
      if(submitEpoch!==epoch)return;
      get('vault-form-error').textContent=error.message;
      if(error.code==='AUTH_REAUTH_REQUIRED' || error.code==='REAUTH_REQUIRED'){
        get('vault-reauth').hidden=false;get('vault-reauth-link').href='/login';get('vault-reauth-link').focus();
      }else if(error.status===409 || error.uncertain){
        editing.blocked=true;editing.uncertain=Boolean(error.uncertain);recover=true;get('vault-form-error').focus();
      }else get('vault-form-error').focus();
    }finally{
      payload.value=undefined;
      if(submitEpoch===epoch){submitting=false;sync();if(recover){if(reading)await reading;await refresh();if(editing?.blocked)showConflict();}schedule();}
    }
  }
  async function open(app,resolver){
    if(submitting || !canOpen(app))return;
    epoch++;active={...app};data=null;fresh=false;reading=null;expanded=new Set();returnFocus=document.activeElement;returnResolver=resolver;
    hideEditor(false);get('vault-message').textContent='';get('vault-error').textContent='';get('vault-target').textContent=`${app.subdomain || app.lastSubdomain || '未绑定域名'} · 应用 ${app.id}`;
    get('vault-items').replaceChildren(element('p','正在读取密钥元数据…','empty'));get('vault-count').textContent='正在读取…';get('vault-operation-section').hidden=true;
    get('vault-dialog').showModal();get('vault-close').focus();await refresh();
  }
  function close(){if(!submitting)get('vault-dialog').close();}
  get('vault-close').addEventListener('click',close);
  get('vault-dialog').addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
  get('vault-dialog').addEventListener('close',()=>{
    epoch++;clearTimeout(timer);hideEditor(false);active=null;data=null;fresh=false;reading=null;submitting=false;
    (returnResolver?.() || returnFocus || get('management-refresh'))?.focus({preventScroll:true});returnFocus=null;returnResolver=null;
  });
  get('vault-refresh').addEventListener('click',refresh);
  for(const id of ['vault-back','vault-cancel'])get(id).addEventListener('click',()=>{if(!submitting)hideEditor();});
  get('vault-form').addEventListener('submit',submit);
  get('vault-reconfirm').addEventListener('click',acknowledge);
  get('vault-show').addEventListener('click',()=>{const showing=get('vault-value').type==='password';get('vault-value').type=showing?'text':'password';get('vault-show').textContent=showing?'隐藏':'显示';get('vault-show').setAttribute('aria-pressed',String(showing));});
  for(const id of ['vault-value','vault-reason'])get(id).addEventListener('input',()=>{get(id).removeAttribute('aria-invalid');get(id+'-error').textContent='';});
  get('vault-reauth-check').addEventListener('click',async()=>{
    const control=get('vault-reauth-check');control.disabled=true;control.textContent='正在检查…';
    try{const value=await window.AwesomeAuth.refresh();if(value && active){get('vault-form-error').textContent='登录状态已更新，请再次点击提交按钮；平台会重新检查近期认证。';get('vault-reauth').hidden=true;await refresh();if(editing)(get('vault-submit').disabled?get('vault-form-error'):get('vault-submit')).focus();}}
    finally{control.disabled=false;control.textContent='重新检查登录';}
  });
  window.AwesomeAuth?.onChange(session=>{
    if(active && (!session || session.capabilities?.vault_enabled!==true || session.canDeploy!==true)){clearInput();get('vault-dialog').close();}
  });
  window.addEventListener('beforeunload',event=>{if(editing && (get('vault-value').value || get('vault-reason').value)){event.preventDefault();event.returnValue='';}});
  window.AwesomeVault={canOpen,open};
})();
