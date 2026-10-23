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
let secretEditor = null;
let deploymentSubmitting = false;
let authFailed = false;
function mayDeploy(appId=$('appId').value) {
  const session=typeof window!=='undefined' && window.AwesomeAuth ? window.AwesomeAuth.session() : null;
  if(typeof window!=='undefined' && window.AwesomeAuth && (!session || (session.mode==='feishu' ? session.canDeploy!==true : session.canDeploy===false)))return false;
  const app=allApps.find(item=>item.id===appId);
  return app?.permissions?.deploy!==false;
}
function syncDeployPermission() {
  const allowed=mayDeploy();$('submit').disabled=deploymentSubmitting || !allowed;
  const session=typeof window!=='undefined' && window.AwesomeAuth ? window.AwesomeAuth.session() : undefined;
  if(session && authFailed){$('form-error').textContent='';authFailed=false;}
  const hint=$('deploy-permission');if(hint){hint.hidden=allowed;hint.textContent=session===null?'登录尚未确认，请使用页面上方的登录提示恢复会话。当前填写内容会保留。':'当前账号没有此应用的部署权限。可在应用管理中查看已有应用，或联系管理员授权。';}
  if(secretEditor)secretControls(secretEditor);
}
const statusLabels = {queued:'已排队',running:'执行中',succeeded:'部署成功',failed:'部署失败',recovery_required:'需要处理恢复状态'};
const phaseLabels = {source:'获取代码',prepared:'配置已校验',freeze:'固定部署输入',frozen:'输入已固定',build:'构建镜像',built:'镜像构建完成',start:'启动服务',https:'准备 HTTPS 证书',publish:'切换路由',verify:'验证发布版本',rollback:'恢复上一版本',retire:'清理旧版本',failed:'失败清理完成',queued:'排队',fetching:'获取代码',preparing:'准备配置',validating:'校验',reserved:'资源已预留',building:'构建镜像',starting:'启动服务',health:'健康检查',certificate:'准备证书',publishing:'切换路由',verifying:'验证发布版本',cleanup:'清理旧版本',complete:'完成',recovery:'恢复运行状态'};
async function api(url, options={}) {
  let response;
  try {response = await (typeof window!=='undefined' && window.AwesomeAuth ? window.AwesomeAuth.fetch : fetch)(url, {credentials:'same-origin', ...options});}
  catch (error) {if(error?.auth){authFailed=true;throw error;}throw new Error('网络连接失败，请检查连接后重试。');}
  let data;
  try {data = await response.json();}
  catch {throw new Error('无法读取服务器响应，请稍后重试。');}
  if (!response.ok) {
    if(response.status===401)authFailed=true;
    const parts=[data.error?.message || (typeof data.detail === 'string' ? data.detail : '请求未通过校验，请检查输入。')];
    if(data.error?.steps?.length)parts.push(...data.error.steps);
    else if(data.error?.suggestion)parts.push(data.error.suggestion);
    throw new Error(parts.join('\n'));
  }
  return data;
}
function text(tag, value, className) {const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node;}
function secretEditorActive(editor) {return secretEditor===editor && currentJob?.id===editor.jobId;}
function clearSecretEditor() {
  if(secretEditor)for(const field of secretEditor.fields.values())field.input.value='';
  secretEditor=null;
}
function secretConfigurationBlocked(editor) {return editor.items.some(item=>item.state==='unavailable' || item.managed && item.state==='missing' && !editor.canGenerate);}
function secretControls(editor) {
  const missing=editor.items.some(item=>item.state==='missing');
  editor.submit.textContent=editor.busy ? '正在处理…' : missing ? '保存并继续部署' : '继续部署';
  editor.submit.disabled=!mayDeploy(editor.payload?.appId) || deploymentSubmitting || editor.busy || editor.loading || !editor.loaded || secretConfigurationBlocked(editor);
  editor.retry.disabled=editor.busy || editor.loading;
  for(const field of editor.fields.values()) {
    field.input.disabled=editor.busy || editor.loading;
    for(const button of field.buttons)button.disabled=editor.busy || editor.loading;
  }
}
function secretField(editor,item) {
  const row=text('div','','secret-field');
  const loginPassword=item.uses.length>0 && item.uses.every(use=>use.environmentVariable==='BOOTSTRAP_PASSWORD');
  const label=text('label',loginPassword ? '登录密码' : item.name);const input=text('input','');
  input.id=`secret-${editor.jobId}-${item.name}`;label.setAttribute('for',input.id);
  input.type='password';input.autocomplete='new-password';input.spellcheck=false;input.maxLength=16384;
  input.setAttribute('data-secret-name',item.name);
  const control=text('div','','secret-input');control.append(input);
  row.append(label,text('small',item.uses.map(use=>`${use.service} · ${use.environmentVariable}`).join('；')),control);
  if(loginPassword)row.append(text('small','设置你自己的登录密码，应用首次登录时使用。'));
  const actions=text('div','','secret-actions');const buttons=[];
  const addButton=(label,callback)=>{const button=text('button',label,'secondary');button.type='button';button.addEventListener('click',callback);actions.append(button);buttons.push(button);return button;};
  const reveal=addButton('显示',()=>{input.type=input.type==='password' ? 'text' : 'password';reveal.textContent=input.type==='password' ? '显示' : '隐藏';});
  row.append(actions);return {row,input,buttons};
}
function renderSecretRequirements(editor,data) {
  if(data.jobId!==editor.jobId || data.repository!==editor.payload.repository || data.subdomain!==editor.payload.subdomain
    || !Array.isArray(data.items) || data.items.some(item=>typeof item.name!=='string' || typeof item.managed!=='boolean' || !Array.isArray(item.uses)
      || !['missing','configured','unavailable'].includes(item.state))
    || new Set(data.items.map(item=>item.name)).size!==data.items.length)throw new Error('配置状态与当前部署不一致，请重新读取。');
  editor.canGenerate=data.canGenerate===true;editor.items=data.items;editor.loaded=true;
  const needed=new Set(data.items.filter(item=>item.state==='missing' && !item.managed).map(item=>item.name));
  for(const [name,field] of editor.fields)if(!needed.has(name)){field.input.value='';editor.fields.delete(name);}
  editor.list.replaceChildren();
  for(const item of data.items) {
    if(item.state==='missing' && !item.managed) {
      if(!editor.fields.has(item.name))editor.fields.set(item.name,secretField(editor,item));
      editor.list.append(editor.fields.get(item.name).row);
    } else {
      const automatic=item.managed && item.state==='missing' && editor.canGenerate;
      const status=item.state==='configured' ? (item.managed ? '平台保管' : '已配置') : automatic ? '自动配置' : '需要恢复';
      const explanation=item.state==='configured' ? (item.managed ? '由平台保管，后续部署沿用；无需查看或复制密钥。' : '已保存在服务器，不显示或覆盖现有值。')
        : automatic ? '由平台自动生成并保存，无需填写。' : item.managed ? '请恢复原配置，不能重新生成加密密钥，以免已有数据无法解密。' : '已有配置不可读取，请恢复原值或联系管理员；不能用随机值替换。';
      const row=text('div','','secret-status');row.append(text('strong',item.name),text('span',status,'status-chip'),text('small',explanation));
      editor.list.append(row);
    }
  }
  if(!data.items.length)editor.list.append(text('p','此部署没有需要填写的秘密配置。','hint'));
  editor.retry.hidden=true;secretControls(editor);
}
async function loadSecretRequirements(editor,{preserveMessage=false}={}) {
  if(!secretEditorActive(editor))return;
  editor.loading=true;secretControls(editor);
  if(!preserveMessage)editor.message.textContent='正在读取此部署需要的配置…';
  try {
    const data=await api(`/api/v1/deployments/${encodeURIComponent(editor.jobId)}/secrets`);
    if(!secretEditorActive(editor))return;
    renderSecretRequirements(editor,data);
    if(!preserveMessage)editor.message.textContent=secretConfigurationBlocked(editor) ? '存在需要恢复的原配置，请先恢复后再继续。'
      : editor.fields.size ? '填写以下缺失项即可继续，无需登录服务器或创建文件。'
      : editor.items.some(item=>item.state==='missing') ? '点击保存并继续部署，平台将自动完成剩余配置。' : '配置状态已更新。';
  } catch(error) {
    if(!secretEditorActive(editor))return;
    editor.loaded=false;editor.retry.hidden=false;editor.message.textContent=`无法读取配置：${error.message}`;
  } finally {if(secretEditorActive(editor)){editor.loading=false;secretControls(editor);}}
}
async function submitDeployment(body,key) {
  return api('/api/v1/deployments',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(body)});
}
async function saveSecretsAndContinue(editor) {
  if(!secretEditorActive(editor) || deploymentSubmitting || editor.busy || editor.loading || !editor.loaded || secretConfigurationBlocked(editor))return;
  const values=Object.create(null);
  for(const item of editor.items)if(item.state==='missing' && !item.managed) {
    const input=editor.fields.get(item.name).input;
    if(!input.value || /[\r\n\x00]/.test(input.value) || new TextEncoder().encode(input.value).length>16384) {
      editor.message.textContent=`请填写 ${item.name}：值不能为空，须为单行且不超过 16 KiB。`;input.focus();return;
    }
    values[item.name]=input.value;
  }
  deploymentSubmitting=true;$('submit').disabled=true;
  editor.busy=true;editor.message.textContent='正在安全保存配置…';secretControls(editor);
  let saving=editor.items.some(item=>item.state==='missing');
  try {
    if(saving) {
      const data=await api(`/api/v1/deployments/${encodeURIComponent(editor.jobId)}/secrets`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});
      if(!secretEditorActive(editor))return;
      renderSecretRequirements(editor,data);editor.saved=true;saving=false;
    }
    if(editor.items.some(item=>item.state!=='configured'))throw new Error('仍有配置未就绪，请核对配置状态后继续。');
    editor.message.textContent='配置已保存，正在提交新的部署…';
    const job=await submitDeployment(editor.payload,editor.requestKey);
    if(!secretEditorActive(editor))return;
    historyFilter='all';$('history').scrollTop=0;
    await refresh();
    if(secretEditorActive(editor))await watch(job.id);
  } catch(error) {
    if(!secretEditorActive(editor))return;
    editor.message.textContent=(editor.saved ? '配置已保存，无需重新填写。部署提交未完成，可点击继续部署重试。\n' : '')+error.message;
    if(saving)await loadSecretRequirements(editor,{preserveMessage:true});
  } finally {
    for(const name of Object.keys(values))delete values[name];
    deploymentSubmitting=false;syncDeployPermission();editor.busy=false;
    if(secretEditor)secretControls(secretEditor);
  }
}
function createSecretEditor(job) {
  const editor=text('section','','secret-editor');editor.jobId=job.id;
  editor.payload={repository:job.repository,ref:job.ref,subdomain:job.subdomain};if(job.appId)editor.payload.appId=job.appId;
  editor.fields=new Map();editor.items=[];editor.loaded=false;editor.loading=false;editor.busy=false;editor.saved=false;
  // One failed task has one continuation, including retries after navigation or a page reload.
  editor.requestKey=`configure-${job.id}`;
  editor.append(text('h4','补齐配置，继续部署'),text('p','直接在这里填写。平台会将值私密保存在部署服务器，并按项目声明传给应用；无需另找文件位置。','hint'));
  editor.append(text('p','平台管理的密钥会自动生成并保存。外部 API Key 请填写供应商提供的有效值。','hint'));
  editor.list=text('div','','secret-fields');editor.list.setAttribute('role','region');editor.list.setAttribute('aria-label','此部署需要的配置，可上下滚动');
  editor.message=text('p','','secret-message');editor.message.setAttribute('role','status');editor.message.setAttribute('aria-live','polite');
  editor.retry=text('button','重试读取配置','secondary');editor.retry.type='button';editor.retry.hidden=true;editor.retry.addEventListener('click',()=>loadSecretRequirements(editor));
  editor.submit=text('button','保存并继续部署','secret-submit');editor.submit.type='button';editor.submit.disabled=true;editor.submit.addEventListener('click',()=>saveSecretsAndContinue(editor));
  editor.append(editor.list,editor.message,editor.retry,editor.submit);return editor;
}
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
function appIdentityLabel(app) {
  const name=app.subdomain || app.lastSubdomain || '未绑定域名';
  const history=!app.subdomain && app.lastSubdomain ? ' · 上次使用' : '';
  return `${name}${history} · 应用 ${app.id.slice(0,8)}`;
}
function updateAppHelp() {
  const app=allApps.find(item=>item.id===$('appId').value);
  if(app?.domainBound===false) {
    $('app-picker-help').textContent=`当前未绑定域名${app.lastSubdomain ? '；上次使用：'+app.lastSubdomain : ''}。继续部署此应用；已保留的数据会沿用，已清除的数据不会恢复。请填写可用的项目名称，并与 awesome.yaml 的 subdomain 保持一致。`;
  } else if(app) {
    $('app-picker-help').textContent='更新此应用会沿用仓库和当前项目名称；名称须与 awesome.yaml 的 subdomain 保持一致。';
  } else $('app-picker-help').textContent='选择已有应用以沿用应用身份与部署记录；未绑定域名的应用需填写可用的项目名称。';
}
function selectedAppLabel() {
  const app=allApps.find(item=>item.id===$('appId').value);
  return app ? `${appIdentityLabel(app)} · ${app.domainBound===false ? '未绑定域名' : '更新已有应用'}` : '创建新应用';
}
function updateDomainPreview() {
  $('domain-prefix').textContent=$('subdomain').value || '项目名称';
}
function validateProjectName(showError=false) {
  const input=$('subdomain');
  const message=!input.value ? '请填写项目名称，例如 demo。' : /^[a-z0-9]([a-z0-9-]{0,5}[a-z0-9])?$/.test(input.value) ? ''
    : '请使用 1–7 位小写字母、数字或中划线，首尾为字母或数字；不能包含中文、空格或大写字母。';
  input.setCustomValidity?.(message);
  const error=$('project-name-error');
  if(error) {
    error.hidden=!showError || !message;
    error.textContent=showError ? message : '';
    if(showError && message)input.setAttribute('aria-invalid','true');else input.removeAttribute('aria-invalid');
  }
  return !message;
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
  const matching=allApps.filter(app=>app.permissions?.deploy!==false).filter(app=>`${app.subdomain || ''} ${app.lastSubdomain || ''} ${app.repository} ${app.id}`.toLocaleLowerCase().includes(query));
  appChoices=[{id:'',subdomain:'创建新应用',repository:'使用新的项目名称部署应用'},...matching];
  activeAppIndex=Math.max(0,Math.min(activeAppIndex,appChoices.length-1));
  $('app-options').replaceChildren();
  appChoices.forEach((app,index)=>{
    const option=text('li','','app-option');
    option.id=`app-option-${index}`;
    option.setAttribute('role','option');
    option.setAttribute('aria-selected',String(app.id===$('appId').value));
    const copy=text('span','','option-copy');
    copy.append(text('span',app.id ? appIdentityLabel(app) : app.subdomain,'option-label'));
    copy.append(text('span',(app.domainBound===false ? '未绑定域名 · ' : '')+app.repository,'option-description'));
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
  if(id && (!app || app.permissions?.deploy===false))return;
  const fields=['appId','repository','subdomain'];
  const previousValues=fields.map(field=>$(field).value);
  $('appId').value=id;
  if(app) {
    $('repository').value=app.repository;
    if(app.domainBound!==false)$('subdomain').value=app.subdomain || '';
    else if(previousValues[0]!==id)$('subdomain').value='';
  }
  updateDomainPreview();validateProjectName();updateAppHelp();syncDeployPermission();
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
  updateAppHelp();syncDeployPermission();
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
$('subdomain').addEventListener('input',()=>{updateDomainPreview();validateProjectName();});
$('subdomain').addEventListener('blur',()=>validateProjectName(true));
$('subdomain').addEventListener('invalid',()=>validateProjectName(true));
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
  const needsSecrets=job.status==='failed' && job.error?.code==='SECRET_NOT_FOUND';
  if(secretEditor && (secretEditor.jobId!==job.id || !needsSecrets))clearSecretEditor();
  const focused=secretEditor?.contains(document.activeElement) ? document.activeElement : null;
  currentJob=job;
  const container=$('current');container.replaceChildren();
  container.append(text('h3',`${job.subdomain} · ${statusLabels[job.status] || '未知状态'}`,'current-title'));
  container.append(text('p',`阶段：${phaseLabels[job.phase] || '处理部署任务'}`,'current-phase'));
  if(needsSecrets) {
    if(!secretEditor){secretEditor=createSecretEditor(job);loadSecretRequirements(secretEditor);}
    container.append(secretEditor);
    const editor=secretEditor;const diagnosis=text('details','','secret-diagnosis');diagnosis.open=editor.diagnosisOpen===true;
    diagnosis.addEventListener('toggle',()=>{editor.diagnosisOpen=diagnosis.open;});
    diagnosis.append(text('summary','错误详情与 AI 建议'));showDiagnosis(diagnosis,job);container.append(diagnosis);
  } else if(job.error)showDiagnosis(container,job);
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
  if(focused)focused.focus({preventScroll:true});
}
async function watch(id,{focus=false}={}) {
  clearTimeout(pollTimer);
  const changed=selectedJob!==id;selectedJob=id;renderHistory();
  if(changed) {
    clearSecretEditor();
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
  const quota=capabilities.storageHardQuota;
  const storageLimit=quota?.limits?.app_budget_mib;
  const memoryLimit=capabilities.resourceLimits?.maxAppMemoryMiB;
  const poolLimit=quota?.limits?.global_budget_mib;
  const positive=value=>Number.isSafeInteger(value) && value>0;
  const capacity=value=>`${new Intl.NumberFormat('zh-CN').format(value)}\u00a0MiB`;
  $('storage-limit').textContent=quota?.configured===false?'尚未启用硬配额':positive(storageLimit)?capacity(storageLimit):'暂未确认';
  $('memory-limit').textContent=positive(memoryLimit)?capacity(memoryLimit):'暂未确认';
  let pool=positive(poolLimit)?`平台共享存储池：${poolLimit%1024===0?`${new Intl.NumberFormat('zh-CN').format(poolLimit/1024)}\u00a0GiB`:capacity(poolLimit)}。`:'';
  if(quota?.configured===true && quota.enabled!==true)pool+='存储配额状态待确认，请稍后刷新。';
  else if(!positive(memoryLimit) || (quota?.configured!==false && !positive(storageLimit)))pool+='资源限制暂未读取成功，请刷新页面后确认。';
  $('resource-pool').textContent=pool || '资源上限以当前平台配置为准。';
  let storage;
  if(capabilities.persistentStorage===true) {
    storage='持久存储已开放。';
    if(typeof capabilities.storage?.mountPath==='string')storage+=`挂载路径为 ${capabilities.storage.mountPath}。`;
    if(Number.isInteger(capabilities.storage?.perService))storage+=`每个服务最多 ${capabilities.storage.perService} 个数据卷。`;
    if(capabilities.storage?.updates==='recreate')storage+='更新采用重建方式，可能短暂中断服务。';
    storage+=quota?.configured===true?'持久数据受存储硬配额限制，具体上限见部署按钮上方。':'当前未启用数据卷硬配额。';
    storage+='平台尚未提供自动备份，请自行安排备份。';
  } else if(capabilities.persistentStorage===false)storage='当前平台的持久存储暂未开放，请勿将需要保留的数据仅写入容器。';
  else storage='暂未确认持久存储支持情况，请以平台字段规范和运维配置为准。';
  $('storage-capability').textContent=storage;
  const network=capabilities.runtimeNetwork;
  let runtimeNetwork='暂未确认运行外连支持情况，请以平台能力和本次部署固定的策略为准。';
  if(network?.mode==='public-https-v1' && network.enabled===true) {
    runtimeNetwork='新部署允许直连公网 IPv4 的 TCP 443，并使用受控 DNS；不进行 TLS 检查，也不提供域名白名单。私网、宿主机、元数据地址、IPv6 及 HTTP 80 等其他业务外连端口仍禁止访问。已有部署保持原先固定的网络策略，构建网络限制不变。';
  } else if(network?.mode==='internal' && network.enabled===false) {
    runtimeNetwork='运行服务使用隔离网络，不能直连外部模型 API。已有部署保持原先固定的网络策略；构建网络限制不变。';
  }
  $('runtime-network-capability').textContent=runtimeNetwork;
  if(currentJob)showJob(currentJob);
}
form.addEventListener('input',event=>{
  if(['repository','ref','subdomain','appId'].some(id=>$(id)===event.target))requestKey=crypto.randomUUID();
});
form.addEventListener('submit',async(event)=>{
  event.preventDefault();if(deploymentSubmitting || !mayDeploy()){syncDeployPermission();return;}
  if(!validateProjectName(true)){$('subdomain').focus();return;}
  deploymentSubmitting=true;$('submit').disabled=true;$('form-error').textContent='';
  if(secretEditor)secretControls(secretEditor);
  try {
    const body=Object.fromEntries(new FormData(form));if(!body.appId)delete body.appId;
    const job=await submitDeployment(body,requestKey);
    requestKey=crypto.randomUUID();historyFilter='all';$('history').scrollTop=0;await refresh();await watch(job.id);
  } catch(error){$('form-error').textContent=error.message;}
  finally{deploymentSubmitting=false;syncDeployPermission();if(secretEditor)secretControls(secretEditor);}
});
$('refresh').addEventListener('click',()=>refresh().catch(error=>{$('form-error').textContent=error.message;}));
api('/api/v1/capabilities').then(renderCapabilities).catch(error=>{renderCapabilities({});$('form-error').textContent=error.message;});
updateDomainPreview();
renderHistory();
refresh().catch(error=>{$('form-error').textContent=error.message;}).then(()=>{
  if(globalThis.location?.search) {
    const id=new URLSearchParams(location.search).get('deployment');
    if(id)watch(id,{focus:true});
  }
});

if(typeof window!=='undefined' && window.AwesomeAuth){window.AwesomeAuth.onChange(syncDeployPermission);window.AwesomeAuth.ready.then(syncDeployPermission);}
