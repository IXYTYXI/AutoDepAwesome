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
const requestKeys=new Map();
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
  if(!response.ok)throw new Error(errorMessage(result?.error,typeof result?.detail==='string' ? result.detail : '请求未完成，请检查当前状态后重试。'));
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
  return operation?.status==='recovery_required' && operation.retryable===true && typeof operation.retryKey==='string' && Boolean(operation.retryKey);
}
function appRow(app) {
  const row=text('article','','management-row');row.setAttribute('data-app-id',app.id);
  const info=text('div','');info.append(text('strong',app.subdomain,'management-name'),text('span',app.repository || '仓库信息未记录','management-repository'));
  const meta=text('div','');meta.append(text('p',`${app.ref || '版本未记录'}${app.commit ? ' · '+app.commit.slice(0,10) : ''}`,'management-meta'));
  meta.append(text('p',`应用 ${app.id.slice(0,8)}`,'management-meta'));
  const operation=operationFor(app),pending=['queued','running'].includes(operation?.status);
  const state=text('div','');const status=pending ? 'offlining' : (appLabels[app.status] ? app.status : 'attention');
  state.append(text('span',appLabels[status],`status-chip status-${status}`));
  if(operation) {
    state.append(text('p',operationLabels[operation.status] || '下线状态未确认',`management-state ${operation.status==='succeeded' ? 'confirmed' : operation.status==='recovery_required' ? 'attention' : ''}`));
    if(operation.error)state.append(text('p',errorMessage(operation.error),'management-state attention'));
  }
  if(['offline','not_deployed'].includes(app.status))state.append(text('p','域名前缀保留，可更新此应用。','management-state'));
  if(app.offlineBlockReason && !pending)state.append(text('p',errorMessage(app.offlineBlockReason),'management-state'));
  const actions=text('div','','management-actions');
  const url=publicURL(app.url);
  if(url && app.status==='online' && !pending) {
    const link=action('a','访问 ↗',`visit:${app.id}`);link.href=url;link.target='_blank';link.rel='noopener';actions.append(link);
  }
  if(retryAllowed(operation)) {
    const retry=action('button','重试下线',`offline:${app.id}`);retry.addEventListener('click',()=>openOfflineDialog(app.id,true));actions.append(retry);
  } else if(app.status==='online') {
    const button=action('button',pending ? '下线处理中' : '下线',`offline:${app.id}`);
    button.disabled=app.canOffline!==true || !app.activeReleaseId || pending;
    button.setAttribute('aria-label',`${app.subdomain}，下线应用`);
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
  $('retained-count').textContent=`保留 ${retained.length} 个应用 · 域名前缀和部署记录保留，可返回部署控制台更新`;
  renderList('online-apps',current.map(appRow),'暂无在线应用或待处理服务。');
  renderList('deployment-list',jobs.map(deploymentRow),'当前没有排队、执行中或待恢复的部署。');
  renderList('retained-apps',retained.map(appRow),'暂无已下线或未部署的应用。');
}
function refreshManagement() {
  if(refreshPromise)return refreshPromise;
  clearTimeout(pollTimer);
  refreshPromise=(async()=>{
    try {
      renderManagement(await request('/api/v1/management'));
      $('management-error').textContent='';
      $('management-updated').textContent='状态已更新 · 每 3 秒自动刷新';
    } catch(error) {
      $('management-error').textContent=error.message+(managementData ? '\n当前保留上次读取的状态，尚未确认最新结果。' : '\n尚未读取到平台状态，请刷新重试。');
    } finally {
      refreshPromise=null;pollTimer=setTimeout(refreshManagement,3000);
    }
  })();
  return refreshPromise;
}
function openOfflineDialog(id,retry=false) {
  if(offlineSubmitting)return;
  const app=managementData?.apps.find(item=>item.id===id);if(!app)return;
  const operation=operationFor(app);
  if(retry ? !retryAllowed(operation) : app.status!=='online' || !app.canOffline || !app.activeReleaseId || ['queued','running'].includes(operation?.status))return;
  offlineTarget={appId:id,releaseId:retry ? operation.releaseId : app.activeReleaseId,retry,retryKey:retry ? operation.retryKey : null};
  returnFocusKey=`offline:${id}`;
  $('offline-title').textContent=retry ? '重试下线应用' : '确认下线应用';
  $('offline-target').textContent=`${app.subdomain} · 版本 ${offlineTarget.releaseId.slice(0,12)}`;
  $('offline-error').textContent='';$('offline-confirm').textContent=retry ? '确认重试下线' : '确认下线';
  $('offline-confirm').disabled=false;$('offline-cancel').disabled=false;
  $('offline-dialog').showModal();$('offline-cancel').focus();
}
function restoreDialogFocus() {
  offlineTarget=null;
  const target=focusTarget($('online-apps'),returnFocusKey) || focusTarget($('retained-apps'),returnFocusKey) || $('management-refresh');
  target.focus({preventScroll:true});returnFocusKey=null;
}
function closeOfflineDialog() {if(!offlineSubmitting)$('offline-dialog').close();}
async function submitOffline() {
  if(!offlineTarget || offlineSubmitting)return;
  const target={...offlineTarget};
  const app=managementData?.apps.find(item=>item.id===target.appId),operation=app && operationFor(app);
  const valid=app && (target.retry ? retryAllowed(operation) && operation.releaseId===target.releaseId && operation.retryKey===target.retryKey : app.status==='online' && app.canOffline===true && app.activeReleaseId===target.releaseId && !['queued','running'].includes(operation?.status));
  if(!valid){$('offline-error').textContent='应用版本或运行状态已经变化，请关闭窗口并刷新后重试。';return;}
  const identity=JSON.stringify([target.appId,target.releaseId]);
  if(!requestKeys.has(identity))requestKeys.set(identity,crypto.randomUUID());
  const key=target.retryKey || requestKeys.get(identity);
  offlineSubmitting=true;$('offline-confirm').disabled=true;$('offline-cancel').disabled=true;
  $('offline-confirm').textContent='正在提交…';$('offline-error').textContent='';
  try {
    const result=await request(`/api/v1/apps/${encodeURIComponent(target.appId)}/offline`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify({expectedReleaseId:target.releaseId})});
    if(!result || typeof result.id!=='string' || result.appId!==target.appId || result.releaseId!==target.releaseId || !operationLabels[result.status])throw new Error('未能确认下线请求结果，请刷新状态后重试。');
    acceptedOperations.set(target.appId,result);renderManagement(managementData);
    $('management-notice').textContent=result.status==='succeeded' ? '下线已确认，应用与部署记录已保留。' : result.status==='recovery_required' ? '下线尚未确认，请查看应用的恢复处理提示。' : '下线请求已受理，正在等待平台确认。';
    offlineSubmitting=false;closeOfflineDialog();refreshManagement();
  } catch(error) {
    $('offline-error').textContent=error.message;
  } finally {
    offlineSubmitting=false;$('offline-confirm').disabled=false;$('offline-cancel').disabled=false;
    $('offline-confirm').textContent='重试下线请求';
  }
}
$('offline-confirm').addEventListener('click',submitOffline);
$('offline-cancel').addEventListener('click',closeOfflineDialog);
$('offline-dialog').addEventListener('close',restoreDialogFocus);
$('offline-dialog').addEventListener('cancel',event=>{if(offlineSubmitting)event.preventDefault();});
$('management-refresh').addEventListener('click',refreshManagement);
refreshManagement();
