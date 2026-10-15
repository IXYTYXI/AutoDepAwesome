const $=id=>document.getElementById(id);
const appLabels={online:'在线',offlining:'正在下线',offline:'已下线',attention:'需要处理',not_deployed:'尚未部署'};
const deploymentLabels={queued:'已排队',running:'执行中',recovery_required:'需要恢复处理'};
const phaseLabels={queued:'等待执行',source:'获取代码',prepared:'配置已校验',freeze:'固定部署输入',frozen:'输入已固定',build:'构建镜像',built:'镜像构建完成',start:'启动服务',https:'准备 HTTPS 证书',publish:'切换路由',verify:'验证发布版本',rollback:'恢复上一版本',retire:'清理旧版本',health:'健康检查',complete:'完成'};
const operationLabels={queued:'下线排队中',running:'正在停止服务并验证公开访问',succeeded:'下线已确认',recovery_required:'下线未确认，需要恢复处理'};
let managementData=null;
let refreshPromise=null;
let pollTimer;
let offlineTarget=null;
let offlineSubmitting=false;
let returnFocusKey=null;
const offlineIntents=new Map();
const acceptedOperations=new Map();

function text(tag,value,className) {
  const node=document.createElement(tag);node.textContent=value ?? '';
  if(className)node.className=className;return node;
}
function errorMessage(error,fallback='操作未完成，请稍后重试。') {
  if(typeof error==='string')return error;
  if(!error || typeof error!=='object')return fallback;
  const parts=[typeof error.message==='string' ? error.message : fallback];
  if(Array.isArray(error.steps))parts.push(...error.steps.filter(step=>typeof step==='string'));
  else if(typeof error.suggestion==='string')parts.push(error.suggestion);
  return parts.join('\n');
}
async function request(url,options={}) {
  let response;
  try {response=await fetch(url,{credentials:'same-origin',...options});}
  catch {throw new Error('网络连接失败，未能确认操作结果。请重试或刷新状态。');}
  let result;
  try {result=await response.json();}
  catch {throw new Error('无法读取服务器响应，未能确认操作结果。请重试或刷新状态。');}
  if(!response.ok) {
    const error=new Error(errorMessage(result?.error,typeof result?.detail==='string' ? result.detail : '请求未完成，请检查当前状态后重试。'));
    error.status=response.status;error.code=result?.error?.code;throw error;
  }
  return result;
}
function focusTarget(container,key) {
  if(!key)return null;
  if(container.getAttribute('data-focus-key')===key)return container;
  for(const child of container.children){const found=focusTarget(child,key);if(found)return found;}
  return null;
}
function renderList(id,rows,emptyMessage) {
  const container=$(id),scrollTop=container.scrollTop,active=document.activeElement;
  const key=container.contains(active) ? active.getAttribute('data-focus-key') : null;
  container.replaceChildren(...(rows.length ? rows : [text('p',emptyMessage,'empty')]));
  focusTarget(container,key)?.focus({preventScroll:true});container.scrollTop=scrollTop;
}
function action(tag,label,key) {
  const node=text(tag,label,tag==='a' ? 'management-link' : 'offline-button');
  node.setAttribute('data-focus-key',key);if(tag==='button')node.type='button';return node;
}
function publicURL(value) {
  try {const url=new URL(value);return ['https:','http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;}
  catch {return null;}
}
function operationFor(app) {
  const matching=(managementData?.offlineOperations || []).filter(operation=>operation.appId===app.id);
  let operation=app.operation || matching.sort((a,b)=>(b.createdAt || 0)-(a.createdAt || 0))[0];
  const accepted=acceptedOperations.get(app.id);
  if(accepted) {
    const verified=matching.find(item=>item.id===accepted.id) || (app.operation?.id===accepted.id ? app.operation : null);
    if(verified){acceptedOperations.delete(app.id);operation=verified;}
    else operation=accepted;
  }
  if(operation && app.status==='online' && app.activeReleaseId && operation.releaseId!==app.activeReleaseId)return null;
  return operation;
}
function retryAllowed(operation) {
  return operation?.status==='recovery_required' && operation.retryable===true && typeof operation.retryKey==='string' && Boolean(operation.retryKey) && operationPolicy(operation)!==null;
}
function operationPolicy(operation) {return operation?.dataPolicy===undefined ? 'retain' : ['retain','delete'].includes(operation.dataPolicy) ? operation.dataPolicy : null;}
function dataStatus(app,operation) {
  const policy=operationPolicy(operation);
  if(operation && operation.status!=='succeeded') {
    if(policy==='delete')return operation.status==='recovery_required' ? '已选择删除数据；可能已部分清理，尚未确认全部完成。恢复时将继续原删除请求。' : '已选择删除数据，等待平台完成并确认清理。';
    if(policy==='retain')return '已选择保留数据，等待下线完成。';
    return '数据处理方式未确认，请刷新状态。';
  }
  if(app.dataDeleted===true)return '上次下线的数据已清除：当时的业务文件、数据库及平台保存的密码、API Key 和该应用的密钥。部署历史保留；后续部署可能产生新数据。';
  if(operation && policy===null)return '数据处理结果未确认，请刷新状态。部署历史保留。';
  if(operation?.status==='succeeded' && policy==='delete')return '删除数据结果尚未同步，请刷新状态确认。部署历史保留。';
  if(app.status==='offline' || operation?.status==='succeeded')return '数据已保留：业务数据及平台保存的密码、API Key 和密钥。部署历史保留。';
  return '';
}
function appName(app) {return app.subdomain || app.lastSubdomain || '未绑定域名';}
function appRow(app) {
  const row=text('article','','management-row');row.setAttribute('data-app-id',app.id);
  const info=text('div','');info.append(text('strong',appName(app),'management-name'),text('span',app.repository || '仓库信息未记录','management-repository'));
  const meta=text('div','');meta.append(text('p',`${app.ref || '版本未记录'}${app.commit ? ' · '+app.commit.slice(0,10) : ''}`,'management-meta'));
  meta.append(text('p',`应用 ${app.id.slice(0,8)}`,'management-meta'));
  if(!app.subdomain && app.lastSubdomain)meta.append(text('p',`上次使用：${app.lastSubdomain}`,'management-meta'));
  const operation=operationFor(app),pending=['queued','running'].includes(operation?.status);
  const state=text('div','');const status=pending ? 'offlining' : (appLabels[app.status] ? app.status : 'attention');
  state.append(text('span',appLabels[status],`status-chip status-${status}`));
  if(operation) {
    state.append(text('p',operationLabels[operation.status] || '下线状态未确认',`management-state ${operation.status==='succeeded' ? 'confirmed' : operation.status==='recovery_required' ? 'attention' : ''}`));
    if(operation.error)state.append(text('p',errorMessage(operation.error),'management-state attention'));
  }
  const dataMessage=dataStatus(app,operation);if(dataMessage)state.append(text('p',dataMessage,'management-state'));
  if(app.domainBound===false && app.status==='offline' && operation?.status==='succeeded') {
    state.append(text('p','域名与端口已释放，重新部署时请选择空闲前缀。','management-state'));
  } else if(app.domainBound===false) {
    state.append(text('p','当前未绑定域名。重新部署时请选择空闲前缀。','management-state'));
  } else if(['offline','not_deployed'].includes(app.status)) {
    state.append(text('p','重新部署前请核对当前域名状态。','management-state'));
  }
  if(app.offlineBlockReason && !pending)state.append(text('p',errorMessage(app.offlineBlockReason),'management-state'));
  const actions=text('div','','management-actions');
  const url=publicURL(app.url);
  if(url && app.status==='online' && app.domainBound!==false && !pending) {
    const link=action('a','访问 ↗',`visit:${app.id}`);link.href=url;link.target='_blank';link.rel='noopener';actions.append(link);
  }
  if(retryAllowed(operation)) {
    const retry=action('button','重试下线',`offline:${app.id}`);retry.addEventListener('click',()=>openOfflineDialog(app.id,true));actions.append(retry);
  } else if(app.status==='online') {
    const button=action('button',pending ? '下线处理中' : '下线',`offline:${app.id}`);
    button.disabled=app.canOffline!==true || !app.activeReleaseId || pending;
    button.setAttribute('aria-label',`${appName(app)}，应用 ${app.id.slice(0,8)}，下线应用`);
    button.addEventListener('click',()=>openOfflineDialog(app.id));actions.append(button);
  } else if(['offline','not_deployed'].includes(app.status)) {
    const link=action('a','去部署控制台 ↗',`deploy:${app.id}`);link.href='/';actions.append(link);
  }
  row.append(info,meta,state,actions);return row;
}
function deploymentRow(job) {
  const row=text('article','','management-row');row.setAttribute('data-job-id',job.id);
  const info=text('div','');info.append(text('strong',job.subdomain,'management-name'),text('span',job.repository || '仓库信息未记录','management-repository'));
  const meta=text('div','');meta.append(text('p',job.ref || '版本未记录','management-meta'),text('p',`部署 ${job.id.slice(0,8)}`,'management-meta'));
  const state=text('div','');state.append(text('span',deploymentLabels[job.status] || '需要处理',`status-chip status-${job.status==='recovery_required' ? 'attention' : job.status}`));
  state.append(text('p',phaseLabels[job.phase] || '处理部署任务','management-state'));
  if(job.error)state.append(text('p',errorMessage(job.error),'management-state attention'));
  const actions=text('div','','management-actions'),link=action('a','查看进度 ↗',`job:${job.id}`);
  link.href='/?deployment='+encodeURIComponent(job.id);actions.append(link);row.append(info,meta,state,actions);return row;
}
function renderManagement(data) {
  if(!data || !Array.isArray(data.apps) || !Array.isArray(data.deployments) || !Array.isArray(data.offlineOperations))throw new Error('管理数据不完整，请稍后刷新。');
  managementData=data;
  const current=data.apps.filter(app=>!['offline','not_deployed'].includes(app.status));
  const retained=data.apps.filter(app=>['offline','not_deployed'].includes(app.status));
  const jobs=data.deployments.filter(job=>['queued','running','recovery_required'].includes(job.status));
  $('count-online').textContent=String(data.apps.filter(app=>app.status==='online').length);
  $('count-queued').textContent=String(jobs.filter(job=>job.status==='queued').length);
  $('count-running').textContent=String(jobs.filter(job=>job.status==='running').length);
  $('count-attention').textContent=String(data.apps.filter(app=>app.status==='attention').length+jobs.filter(job=>job.status==='recovery_required').length);
  $('online-count').textContent=`全部应用 ${data.apps.length} 个 · 当前列表 ${current.length} 个 · 在窗口内滚动查看`;
  $('deployment-count').textContent=`当前 ${jobs.length} 项 · 包含排队、执行中和需要恢复处理的部署`;
  $('retained-count').textContent=`保留 ${retained.length} 个应用记录 · 数据处理结果见各应用，重新部署需使用可用域名`;
  renderList('online-apps',current.map(appRow),'暂无在线应用或待处理服务。');
  renderList('deployment-list',jobs.map(deploymentRow),'当前没有排队、执行中或待恢复的部署。');
  renderList('retained-apps',retained.map(appRow),'暂无已下线或未部署的应用。');
}
function refreshManagement() {
  if(refreshPromise)return refreshPromise;
  clearTimeout(pollTimer);
  refreshPromise=(async()=>{
    try {
      const data=await request('/api/v1/management');renderManagement(data);
      $('management-error').textContent='';
      $('management-updated').textContent='状态已更新 · 每 3 秒自动刷新';
      return data;
    } catch(error) {
      $('management-error').textContent=error.message+(managementData ? '\n当前保留上次读取的状态，尚未确认最新结果。' : '\n尚未读取到平台状态，请刷新重试。');
    } finally {
      refreshPromise=null;pollTimer=setTimeout(refreshManagement,3000);
    }
  })();
  return refreshPromise;
}
function intentIdentity(target) {return JSON.stringify([target.appId,target.releaseId]);}
function readOfflineIntent(target) {
  const identity=intentIdentity(target);
  if(offlineIntents.has(identity))return offlineIntents.get(identity);
  try {
    const saved=JSON.parse(sessionStorage.getItem('awesome.offline.intent:'+target.appId));
    if(saved?.appId!==target.appId || saved.releaseId!==target.releaseId || !['retain','delete'].includes(saved.dataPolicy) || typeof saved.key!=='string' || !saved.key || saved.key.length>256 || /[\u0000-\u001f\u007f]/.test(saved.key))return null;
    if(saved.dataPolicy==='delete' && (typeof saved.deleteConfirmation!=='string' || saved.deleteConfirmation.length>256))return null;
    const intent={key:saved.key,dataPolicy:saved.dataPolicy,...(saved.dataPolicy==='delete' ? {deleteConfirmation:saved.deleteConfirmation} : {})};
    offlineIntents.set(identity,intent);return intent;
  } catch {return null;}
}
function saveOfflineIntent(target,intent) {
  offlineIntents.set(intentIdentity(target),intent);
  try {sessionStorage.setItem('awesome.offline.intent:'+target.appId,JSON.stringify({appId:target.appId,releaseId:target.releaseId,...intent}));} catch {}
}
function clearOfflineIntent(target) {
  offlineIntents.delete(intentIdentity(target));
  try {sessionStorage.removeItem('awesome.offline.intent:'+target.appId);} catch {}
}
async function reconcileRejectedIntent(target,error) {
  const rejected=new Set(['OFFLINE_DATA_CONFLICT','OFFLINE_DELETE_CONFIRMATION','OFFLINE_DATA_POLICY','APP_BUSY','OFFLINE_RELEASE_CHANGED','OFFLINE_STATE']);
  if(![409,422].includes(error.status) || !rejected.has(error.code))return;
  // A refusal can also concern a previously accepted retry. Only a fresh,
  // complete management read can prove there is no durable operation to keep.
  if(refreshPromise)await refreshPromise;
  const data=await refreshManagement();
  if(!data || !offlineTarget || intentIdentity(offlineTarget)!==intentIdentity(target))return;
  const app=data.apps.find(item=>item.id===target.appId);
  if(!app)return;
  const operations=[...data.offlineOperations,app.operation,acceptedOperations.get(target.appId)]
    .filter(item=>item && item.appId===target.appId && item.releaseId===target.releaseId)
    .sort((a,b)=>(b.createdAt || 0)-(a.createdAt || 0));
  const operation=operations[0];
  if(operation) {
    if(!retryAllowed(operation)){offlineTarget.conflict=true;return;}
    offlineTarget.retry=true;offlineTarget.retryKey=operation.retryKey;offlineTarget.dataPolicy=operationPolicy(operation);
    const saved=offlineTarget.intent;
    clearOfflineIntent(target);delete offlineTarget.intent;
    if(offlineTarget.dataPolicy==='retain' || saved?.dataPolicy==='delete' && saved.deleteConfirmation===target.expectedConfirmation) {
      const intent={key:operation.retryKey,dataPolicy:offlineTarget.dataPolicy,...(offlineTarget.dataPolicy==='delete' ? {deleteConfirmation:target.expectedConfirmation} : {})};
      saveOfflineIntent(target,intent);offlineTarget.intent=intent;
    }
    $('offline-delete-confirmation').value=offlineTarget.intent?.deleteConfirmation || '';
    return;
  }
  clearOfflineIntent(target);delete offlineTarget.intent;
  offlineTarget.retry=false;offlineTarget.retryKey=null;
}
function syncOfflineControls() {
  if(!offlineTarget)return;
  const target=offlineTarget,locked=target.retry || Boolean(target.intent),deleting=target.dataPolicy==='delete';
  $('offline-retain').checked=!deleting;$('offline-delete').checked=deleting;
  $('offline-retain').disabled=offlineSubmitting || locked;$('offline-delete').disabled=offlineSubmitting || locked;
  $('offline-delete-fields').hidden=!deleting;
  $('offline-delete-confirmation').disabled=offlineSubmitting || Boolean(target.intent);
  $('offline-delete-label').textContent=target.expectedConfirmation ? `请输入“${target.expectedConfirmation}”确认` : '无法确认目标域名，请刷新状态后重试。';
  const confirmed=!deleting || Boolean(target.expectedConfirmation) && $('offline-delete-confirmation').value===target.expectedConfirmation;
  $('offline-confirm').disabled=offlineSubmitting || !confirmed || target.conflict;
  $('offline-cancel').disabled=offlineSubmitting;
  $('offline-confirm').textContent=offlineSubmitting ? '正在提交…' : `${target.retry || target.intent ? '重试下线' : '确认下线'}${deleting ? '并删除数据' : '并保留数据'}`;
  $('offline-policy-lock').textContent=locked ? `此请求已选择${deleting ? '删除' : '保留'}数据，重试将沿用原请求，不能更改数据处理方式。` : '';
}
function changeOfflinePolicy() {
  if(!offlineTarget || offlineSubmitting)return;
  if(!offlineTarget.retry && !offlineTarget.intent) {
    offlineTarget.dataPolicy=$('offline-delete').checked ? 'delete' : 'retain';
    if(offlineTarget.dataPolicy==='retain')$('offline-delete-confirmation').value='';
    $('offline-error').textContent='';
  }
  syncOfflineControls();
}
function openOfflineDialog(id,retry=false) {
  if(offlineSubmitting)return;
  const app=managementData?.apps.find(item=>item.id===id);if(!app)return;
  const operation=operationFor(app);
  if(retry ? !retryAllowed(operation) : app.status!=='online' || !app.canOffline || !app.activeReleaseId || ['queued','running'].includes(operation?.status))return;
  const domain=app.subdomain || app.lastSubdomain || '';
  offlineTarget={appId:id,releaseId:retry ? operation.releaseId : app.activeReleaseId,retry,retryKey:retry ? operation.retryKey : null,domain,expectedConfirmation:domain ? '删除 '+domain : null,dataPolicy:retry ? operationPolicy(operation) : 'retain',conflict:false};
  const saved=readOfflineIntent(offlineTarget);
  if(saved && (!retry || saved.key===operation.retryKey && saved.dataPolicy===offlineTarget.dataPolicy)) {
    offlineTarget.intent=saved;offlineTarget.dataPolicy=saved.dataPolicy;
    offlineTarget.conflict=saved.dataPolicy==='delete' && saved.deleteConfirmation!==offlineTarget.expectedConfirmation;
  }
  returnFocusKey=`offline:${id}`;
  $('offline-title').textContent=retry ? '重试下线应用' : '确认下线应用';
  $('offline-target').textContent=`${appName(app)} · 应用 ${app.id.slice(0,8)} · 版本 ${offlineTarget.releaseId.slice(0,12)}`;
  $('offline-delete-confirmation').value=offlineTarget.intent?.deleteConfirmation || '';
  $('offline-error').textContent=offlineTarget.conflict ? '应用域名已经变化，不能更改之前的删除请求，请刷新状态确认。' : '';
  syncOfflineControls();
  $('offline-dialog').showModal();$('offline-cancel').focus();
}
function restoreDialogFocus() {
  offlineTarget=null;
  const previous=focusTarget($('online-apps'),returnFocusKey) || focusTarget($('retained-apps'),returnFocusKey);
  const target=previous && !previous.disabled ? previous : $('management-refresh');
  target.focus({preventScroll:true});returnFocusKey=null;
}
function closeOfflineDialog() {if(!offlineSubmitting)$('offline-dialog').close();}
async function submitOffline() {
  if(!offlineTarget || offlineSubmitting)return;
  const target={...offlineTarget};
  const app=managementData?.apps.find(item=>item.id===target.appId),operation=app && operationFor(app);
  const valid=app && !target.conflict && (app.subdomain || app.lastSubdomain || '')===target.domain && (target.retry ? retryAllowed(operation) && operation.releaseId===target.releaseId && operation.retryKey===target.retryKey && operationPolicy(operation)===target.dataPolicy : app.status==='online' && app.canOffline===true && app.activeReleaseId===target.releaseId && !['queued','running'].includes(operation?.status));
  if(!valid){$('offline-error').textContent='应用版本或运行状态已经变化，请关闭窗口并刷新后重试。';return;}
  if(target.dataPolicy==='delete' && (!target.expectedConfirmation || $('offline-delete-confirmation').value!==target.expectedConfirmation)) {
    $('offline-error').textContent='请准确输入目标确认文字后再删除数据。';$('offline-delete-confirmation').focus();return;
  }
  const intent=target.intent || {key:target.retryKey || crypto.randomUUID(),dataPolicy:target.dataPolicy,...(target.dataPolicy==='delete' ? {deleteConfirmation:target.expectedConfirmation} : {})};
  saveOfflineIntent(target,intent);offlineTarget.intent=intent;
  const body={expectedReleaseId:target.releaseId,dataPolicy:intent.dataPolicy,...(intent.dataPolicy==='delete' ? {deleteConfirmation:intent.deleteConfirmation} : {})};
  offlineSubmitting=true;syncOfflineControls();$('offline-error').textContent='';
  try {
    const result=await request(`/api/v1/apps/${encodeURIComponent(target.appId)}/offline`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':intent.key},body:JSON.stringify(body)});
    if(!result || typeof result.id!=='string' || result.appId!==target.appId || result.releaseId!==target.releaseId || !operationLabels[result.status] || operationPolicy(result)!==intent.dataPolicy)throw new Error('未能确认下线请求结果，请刷新状态后重试。');
    acceptedOperations.set(target.appId,result);renderManagement(managementData);
    $('management-notice').textContent=result.status==='succeeded' ? `下线已确认，${intent.dataPolicy==='delete' ? '业务数据及平台保存的密码和密钥已清除' : '数据已保留'}，部署历史保留。` : result.status==='recovery_required' ? '下线尚未确认，请查看应用的恢复处理提示。' : '下线请求已受理，请在应用列表中查看执行状态。';
    offlineSubmitting=false;closeOfflineDialog();refreshManagement();
  } catch(error) {
    await reconcileRejectedIntent(target,error);
    $('offline-error').textContent=error.message;
  } finally {
    offlineSubmitting=false;syncOfflineControls();
  }
}
$('offline-retain').addEventListener('change',changeOfflinePolicy);
$('offline-delete').addEventListener('change',changeOfflinePolicy);
$('offline-delete-confirmation').addEventListener('input',syncOfflineControls);
$('offline-confirm').addEventListener('click',submitOffline);
$('offline-cancel').addEventListener('click',closeOfflineDialog);
$('offline-dialog').addEventListener('close',restoreDialogFocus);
$('offline-dialog').addEventListener('cancel',event=>{if(offlineSubmitting)event.preventDefault();});
$('management-refresh').addEventListener('click',refreshManagement);
refreshManagement();
