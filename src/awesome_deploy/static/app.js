const $ = (id) => document.getElementById(id);
const form = $('deploy-form');
let requestKey = crypto.randomUUID();
let selectedJob = null;
let currentJob = null;
let aiConfigured = false;
const adviceStates = new Map();
const metadataStates = new Map();
let pollTimer;
let recentJobs = [];
let allApps = [];
let historyFilter = 'all';
let refreshVersion = 0;
const filterButtons = new Map();
let pickerOpen = false;
let pickerQuery = '';
let appChoices = [];
let activeAppIndex = 0;
const statusLabels = {queued:'已排队',running:'执行中',succeeded:'部署成功',failed:'部署失败',recovery_required:'需要处理恢复状态'};
const phaseLabels = {source:'获取代码',prepared:'配置已校验',freeze:'固定部署输入',frozen:'输入已固定',build:'构建镜像',built:'镜像构建完成',start:'启动服务',https:'准备 HTTPS 证书',publish:'切换路由',verify:'验证发布版本',rollback:'恢复上一版本',retire:'清理旧版本',failed:'失败清理完成',queued:'排队',fetching:'获取代码',preparing:'准备配置',validating:'校验',reserved:'资源已预留',building:'构建镜像',starting:'启动服务',health:'健康检查',certificate:'准备证书',publishing:'切换路由',verifying:'验证发布版本',cleanup:'清理旧版本',complete:'完成',recovery:'恢复运行状态'};
async function api(url, options={}) {
  let response;
  try {response = await fetch(url, {credentials:'same-origin', ...options});}
  catch {throw new Error('网络连接失败，请检查连接后重试。');}
  let data;
  try {data = await response.json();}
  catch {throw new Error('无法读取服务器响应，请稍后重试。');}
  if (!response.ok) {
    const parts=[data.error?.message || (typeof data.detail === 'string' ? data.detail : '请求未通过校验，请检查输入。')];
    if(data.error?.steps?.length)parts.push(...data.error.steps);
    else if(data.error?.suggestion)parts.push(data.error.suggestion);
    throw new Error(parts.join('\n'));
  }
  return data;
}
function text(tag, value, className) {const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node;}
function showDiagnosis(container, job) {
  const error=job.error;
  const box=text('section','','diagnosis');
  box.append(text('h4',error.title || '部署遇到问题'));
  box.append(text('p',error.message,'failed'));
  const steps=error.steps?.length ? error.steps : (error.suggestion ? [error.suggestion] : []);
  if(steps.length){box.append(text('h4','修改建议'));const list=text('ol','');for(const step of steps)list.append(text('li',step));box.append(list);}
  const details=text('details','');details.append(text('summary','错误详情'));
  details.append(text('p',`错误编号：${error.code}`));
  if(error.path)details.append(text('p',`问题位置：${error.path}`));
  box.append(details);
  const ai=text('div','','ai-advice');
  const state=adviceStates.get(job.id) || {};
  const button=text('button',state.loading ? '正在分析…' : (state.error ? '重试 AI 分析' : 'AI 分析修改建议'),'secondary');
  button.type='button';button.disabled=!aiConfigured || Boolean(state.loading);
  button.addEventListener('click',()=>analyzeJob(job.id));
  ai.append(button);
  if(state.loading)ai.append(text('p','分析可能需要几分钟，请稍候。','hint'));
  ai.append(text('p',aiConfigured ? '仅发送错误摘要与平台规则，不发送源码、环境变量或原始日志。AI 建议需结合项目核对。' : 'AI 分析尚未配置，以上内置建议可直接使用。','hint'));
  if(state.error){const alert=text('p',state.error,'failed');alert.setAttribute('role','alert');ai.append(alert);}
  if(state.text){ai.append(text('h4','AI 修改建议'));ai.append(text('pre',state.text,'ai-result'));}
  box.append(ai);container.append(box);
}
async function analyzeJob(id) {
  if(!aiConfigured || adviceStates.get(id)?.loading)return;
  adviceStates.set(id,{loading:true});
  if(currentJob?.id===id)showJob(currentJob);
  try {
    const deadline=Date.now()+270000;
    let result;
    do {
      if(Date.now()>=deadline)throw new Error('AI 分析等待超时，请稍后重试。');
      const body=JSON.stringify(result ? {poll:true} : {});
      result=await api(`/api/v1/deployments/${encodeURIComponent(id)}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body});
      if(result.status==='pending'){
        if(Date.now()>=deadline)throw new Error('AI 分析等待超时，请稍后重试。');
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
    } while(result.status==='pending');
    adviceStates.set(id,{text:result.text});
  } catch(error){adviceStates.set(id,{error:error.message});}
  if(adviceStates.size>50){for(const [key,state] of adviceStates){if(key!==id && !state.loading){adviceStates.delete(key);break;}}}
  if(selectedJob===id && currentJob?.id===id)showJob(currentJob);
}
function selectedAppLabel() {
  const app=allApps.find(item=>item.id===$('appId').value);
  return app ? `${app.subdomain} · 更新已有应用` : '创建新应用';
}
function updateDomainPreview() {
  $('domain-prefix').textContent=$('subdomain').value || '你的前缀';
}
function highlightAppOption() {
  for(let i=0;i<$('app-options').children.length;i++) {
    $('app-options').children[i].className='app-option'+(i===activeAppIndex ? ' is-active' : '');
  }
  const active=$('app-options').children[activeAppIndex];
  if(pickerOpen && active) {
    $('app-search').setAttribute('aria-activedescendant',active.id);
    active.scrollIntoView({block:'nearest'});
  } else $('app-search').removeAttribute('aria-activedescendant');
}
function renderAppOptions() {
  const query=pickerQuery.trim().toLocaleLowerCase();
  const matching=allApps.filter(app=>`${app.subdomain} ${app.repository} ${app.id}`.toLocaleLowerCase().includes(query));
  appChoices=[{id:'',subdomain:'创建新应用',repository:'使用新的域名前缀部署项目'},...matching];
  activeAppIndex=Math.max(0,Math.min(activeAppIndex,appChoices.length-1));
  $('app-options').replaceChildren();
  appChoices.forEach((app,index)=>{
    const option=text('li','','app-option');
    option.id=`app-option-${index}`;
    option.setAttribute('role','option');
    option.setAttribute('aria-selected',String(app.id===$('appId').value));
    const copy=text('span','','option-copy');
    copy.append(text('span',app.subdomain,'option-label'));
    copy.append(text('span',app.repository,'option-description'));
    option.append(copy,text('span',app.id===$('appId').value ? '✓' : '', 'option-check'));
    option.addEventListener('mousedown',event=>event.preventDefault());
    option.addEventListener('click',()=>chooseApp(app.id));
    $('app-options').append(option);
  });
  $('app-options-empty').hidden=!query || matching.length>0;
  $('app-picker-status').textContent=`找到 ${matching.length} 个已有应用，也可创建新应用。`;
  highlightAppOption();
}
function openAppPicker(query='') {
  pickerOpen=true;pickerQuery=query;activeAppIndex=0;
  $('app-menu').hidden=false;
  $('app-search').value=query;
  $('app-search').setAttribute('aria-expanded','true');
  $('app-picker-toggle').setAttribute('aria-expanded','true');
  $('app-picker-toggle').setAttribute('aria-label','收起应用列表');
  renderAppOptions();
}
function closeAppPicker() {
  pickerOpen=false;
  $('app-menu').hidden=true;
  $('app-search').value=selectedAppLabel();
  $('app-search').setAttribute('aria-expanded','false');
  $('app-search').removeAttribute('aria-activedescendant');
  $('app-picker-toggle').setAttribute('aria-expanded','false');
  $('app-picker-toggle').setAttribute('aria-label','展开应用列表');
}
function chooseApp(id) {
  const app=allApps.find(item=>item.id===id);
  if(id && !app)return;
  const fields=['appId','repository','subdomain'];
  const previousValues=fields.map(field=>$(field).value);
  $('appId').value=id;
  if(app) {
    $('repository').value=app.repository;
    $('subdomain').value=app.subdomain;
  }
  updateDomainPreview();
  if(fields.some((field,index)=>$(field).value!==previousValues[index]))requestKey=crypto.randomUUID();
  closeAppPicker();
  $('app-search').focus();
}
function updateApps(items) {
  allApps=items;
  if($('appId').value && !allApps.some(app=>app.id===$('appId').value)) {
    $('appId').value='';requestKey=crypto.randomUUID();
  }
  if(pickerOpen)renderAppOptions();
  else $('app-search').value=selectedAppLabel();
}
$('app-search').addEventListener('input',()=>openAppPicker($('app-search').value));
$('app-search').addEventListener('click',()=>{if(!pickerOpen)openAppPicker();});
$('app-picker-toggle').addEventListener('mousedown',event=>event.preventDefault());
$('app-picker-toggle').addEventListener('click',()=>{
  if(pickerOpen)closeAppPicker();else openAppPicker();
  $('app-search').focus();
});
$('app-search').addEventListener('keydown',event=>{
  if(event.isComposing || event.keyCode===229)return;
  if(event.key==='ArrowDown' || event.key==='ArrowUp') {
    event.preventDefault();
    if(!pickerOpen) {
      openAppPicker();
      if(event.key==='ArrowUp')activeAppIndex=appChoices.length-1;
    } else activeAppIndex=(activeAppIndex+(event.key==='ArrowDown' ? 1 : -1)+appChoices.length)%appChoices.length;
    highlightAppOption();
  } else if(event.key==='Enter' && pickerOpen) {
    event.preventDefault();chooseApp(appChoices[activeAppIndex].id);
  } else if(event.key==='Escape' && pickerOpen) {
    event.preventDefault();closeAppPicker();
  } else if(event.key==='Tab')closeAppPicker();
});
$('app-picker').addEventListener('focusout',event=>{
  if(!$('app-picker').contains(event.relatedTarget))closeAppPicker();
});
document.addEventListener('pointerdown',event=>{
  if(pickerOpen && !$('app-picker').contains(event.target))closeAppPicker();
});
$('subdomain').addEventListener('input',updateDomainPreview);
function formatJobTime(value) {
  if(typeof value!=='number' || !Number.isFinite(value))return '时间未记录';
  return new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
}
function renderHistory({resetScroll=false}={}) {
  const scrollTop=resetScroll ? 0 : $('history').scrollTop;
  const activeElement=document.activeElement;
  const focusedJobId=$('history').contains(activeElement) ? activeElement.getAttribute('data-job-id') : null;
  let focusedRow;
  const filters=[['all','全部'],['queued','排队'],['running','执行中'],['succeeded','成功'],['failed','失败'],['recovery_required','待恢复']];
  if(!filterButtons.size) {
    for(const [value,label] of filters) {
      const button=text('button',label,'filter-chip');button.type='button';
      button.setAttribute('data-filter',value);
      button.addEventListener('click',()=>{
        const changed=historyFilter!==value;historyFilter=value;renderHistory({resetScroll:changed});
      });
      filterButtons.set(value,button);$('history-filters').append(button);
    }
  }
  for(const [value,button] of filterButtons)button.setAttribute('aria-pressed',String(historyFilter===value));
  const filtered=historyFilter==='all' ? recentJobs : recentJobs.filter(job=>job.status===historyFilter);
  $('history-count').textContent=`最近已载入 ${recentJobs.length} 条记录 · 在列表内滚动查看`+(historyFilter==='all' ? '' : ` · 当前筛选 ${filtered.length} 条`);
  $('history').replaceChildren();
  for(const job of filtered) {
    const row=text('button','','history-row');row.type='button';
    row.setAttribute('data-job-id',job.id);
    row.setAttribute('aria-pressed',String(job.id===selectedJob));
    row.setAttribute('aria-label',`${job.subdomain}，${statusLabels[job.status] || '未知状态'}，查看部署详情`);
    const main=text('span','','job-main');
    main.append(text('strong',job.subdomain,'job-name'),text('span',job.repository || '仓库信息未记录','job-repo'));
    const stamp=text('span',formatJobTime(job.createdAt),'job-time');stamp.append(text('span',job.id.slice(0,8),'job-id'));
    row.append(main,text('span',statusLabels[job.status] || '未知状态',`status-chip status-${job.status}`),text('span',phaseLabels[job.phase] || '处理部署任务','job-stage'),stamp);
    row.addEventListener('click',()=>watch(job.id,{focus:true}));$('history').append(row);
    if(job.id===focusedJobId)focusedRow=row;
  }
  if(!filtered.length)$('history').append(text('p',recentJobs.length ? '最近记录中，没有符合该状态的部署。' : '暂无部署记录，完成第一次提交后会显示在这里。','empty'));
  if(focusedRow)focusedRow.focus({preventScroll:true});
  $('history').scrollTop=scrollTop;
}
function showJob(job) {
  currentJob=job;
  const container=$('current');container.replaceChildren();
  container.append(text('h3',`${job.subdomain} · ${statusLabels[job.status] || '未知状态'}`,'current-title'));
  container.append(text('p',`阶段：${phaseLabels[job.phase] || '处理部署任务'}`,'current-phase'));
  if(job.error)showDiagnosis(container,job);
  if(job.url) {
    const link=text('a','打开已部署应用 ↗','app-link');link.href=job.url;link.target='_blank';link.rel='noopener';container.append(link);
  }
  const metadata=text('details','','job-metadata');metadata.open=metadataStates.get(job.id)===true;
  metadata.append(text('summary','部署信息'));
  const entries=[['部署编号',job.id],['应用编号',job.appId],['代码版本',job.commit],['仓库地址',job.repository],['分支 / 版本',job.ref],['提交时间',formatJobTime(job.createdAt)]];
  const values=text('dl','','metadata-grid');
  for(const [label,value] of entries)if(value)values.append(text('dt',label),text('dd',value,label.includes('编号') || label==='代码版本' ? 'mono' : ''));
  metadata.append(values);metadata.addEventListener('toggle',()=>metadataStates.set(job.id,metadata.open));
  container.append(metadata);
}
async function watch(id,{focus=false}={}) {
  clearTimeout(pollTimer);
  const changed=selectedJob!==id;selectedJob=id;renderHistory();
  if(changed) {
    currentJob=null;
    $('current').replaceChildren(text('p','正在读取部署详情…','empty'));
    $('events').textContent='正在读取执行记录…';$('event-details').open=false;
  }
  if(focus) {
    $('progress-panel').focus({preventScroll:true});
    $('progress-panel').scrollIntoView({block:'start'});
  }
  try {
    const job=await api(`/api/v1/deployments/${encodeURIComponent(id)}`);
    if(selectedJob!==id)return;
    showJob(job);
    const events=await api(`/api/v1/deployments/${encodeURIComponent(id)}/events`);
    if(selectedJob!==id)return;
    $('events').textContent=events.items.map(e=>`${phaseLabels[e.phase] || '部署记录'}  ${e.message}`).join('\n') || '暂无执行记录。';
    if(['queued','running'].includes(job.status))pollTimer=setTimeout(()=>watch(id),2500);
    else await refresh();
  } catch(error) {
    if(selectedJob===id) {
      $('form-error').textContent=error.message;
      if(!currentJob)$('current').replaceChildren(text('p',error.message,'failed'));
    }
  }
}
async function refresh() {
  const version=++refreshVersion;
  const [jobs,apps]=await Promise.all([api('/api/v1/deployments'),api('/api/v1/apps')]);
  if(version!==refreshVersion)return;
  recentJobs=jobs.items;updateApps(apps.items);renderHistory();
}
function renderCapabilities(capabilities) {
  if(capabilities.baseDomain)$('domain-suffix').textContent='.'+capabilities.baseDomain;
  aiConfigured=capabilities.aiAdvice?.configured===true;
  let storage;
  if(capabilities.persistentStorage===true) {
    storage='持久存储已开放。';
    if(typeof capabilities.storage?.mountPath==='string')storage+=`挂载路径为 ${capabilities.storage.mountPath}。`;
    if(Number.isInteger(capabilities.storage?.perService))storage+=`每个服务最多 ${capabilities.storage.perService} 个数据卷。`;
    if(capabilities.storage?.updates==='recreate')storage+='更新采用重建方式，可能短暂中断服务。';
    storage+='当前没有每个应用的数据卷硬配额和自动备份，请自行规划容量与备份。';
  } else if(capabilities.persistentStorage===false)storage='当前平台的持久存储暂未开放，请勿将需要保留的数据仅写入容器。';
  else storage='暂未确认持久存储支持情况，请以平台字段规范和运维配置为准。';
  $('storage-capability').textContent=storage;
  if(currentJob)showJob(currentJob);
}
form.addEventListener('input',event=>{
  if(['repository','ref','subdomain','appId'].some(id=>$(id)===event.target))requestKey=crypto.randomUUID();
});
form.addEventListener('submit',async(event)=>{
  event.preventDefault();$('submit').disabled=true;$('form-error').textContent='';
  try {
    const body=Object.fromEntries(new FormData(form));if(!body.appId)delete body.appId;
    const job=await api('/api/v1/deployments',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':requestKey},body:JSON.stringify(body)});
    requestKey=crypto.randomUUID();historyFilter='all';$('history').scrollTop=0;await refresh();await watch(job.id);
  } catch(error){$('form-error').textContent=error.message;}
  finally{$('submit').disabled=false;}
});
$('refresh').addEventListener('click',()=>refresh().catch(error=>{$('form-error').textContent=error.message;}));
api('/api/v1/capabilities').then(renderCapabilities).catch(error=>{renderCapabilities({});$('form-error').textContent=error.message;});
updateDomainPreview();
renderHistory();
refresh().catch(error=>{$('form-error').textContent=error.message;});
