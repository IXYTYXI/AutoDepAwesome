const $ = (id) => document.getElementById(id);
const form = $('deploy-form');
let requestKey = crypto.randomUUID();
let selectedJob = null;
let currentJob = null;
let aiConfigured = false;
const adviceStates = new Map();
let pollTimer;
const statusLabels = {queued:'已排队',running:'执行中',succeeded:'部署成功',failed:'部署失败',recovery_required:'需要处理恢复状态'};
const phaseLabels = {source:'获取代码',prepared:'配置已校验',freeze:'固定部署输入',frozen:'输入已固定',build:'构建镜像',built:'镜像构建完成',start:'启动服务',https:'准备 HTTPS 证书',publish:'切换路由',verify:'验证发布版本',rollback:'恢复上一版本',retire:'清理旧版本',failed:'失败清理完成',queued:'排队',fetching:'获取代码',preparing:'准备配置',validating:'校验',reserved:'资源已预留',building:'构建镜像',starting:'启动服务',health:'健康检查',certificate:'准备证书',publishing:'切换路由',verifying:'验证发布版本',cleanup:'清理旧版本',complete:'完成'};
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
function showJob(job) {
  currentJob=job;
  const container=$('current');container.replaceChildren();
  container.append(text('h3', `${job.subdomain} · ${statusLabels[job.status] || job.status}`, job.status));
  container.append(text('p', `阶段：${phaseLabels[job.phase] || '处理部署任务'}`));
  container.append(text('p', `部署编号：${job.id}`));
  if(job.appId)container.append(text('p', `应用编号：${job.appId}`));
  if(job.commit)container.append(text('p', `代码版本：${job.commit.slice(0,12)}`));
  if(job.error)showDiagnosis(container,job);
  if(job.url){const link=text('a','打开已部署应用 ↗');link.href=job.url;link.target='_blank';link.rel='noopener';container.append(link);}
}
async function watch(id) {
  clearTimeout(pollTimer);selectedJob=id;
  try {
    const job=await api(`/api/v1/deployments/${encodeURIComponent(id)}`);
    if(selectedJob!==id)return;
    showJob(job);
    const events=await api(`/api/v1/deployments/${encodeURIComponent(id)}/events`);
    if(selectedJob!==id)return;
    $('events').textContent=events.items.map(e=>`${phaseLabels[e.phase] || '部署记录'}  ${e.message}`).join('\n');
    if(['queued','running'].includes(job.status))pollTimer=setTimeout(()=>watch(id),2500);
    else await refresh();
  } catch(error){if(selectedJob===id)$('form-error').textContent=error.message;}
}
async function refresh() {
  const [jobs, apps]=await Promise.all([api('/api/v1/deployments'),api('/api/v1/apps')]);
  $('history').replaceChildren();
  for(const job of jobs.items){const item=text('button','',`job secondary ${job.status}`);item.type='button';item.append(text('strong',job.subdomain));item.append(text('span',`${statusLabels[job.status] || job.status} · ${job.id.slice(0,8)}`));item.addEventListener('click',()=>watch(job.id));$('history').append(item);}
  if(!jobs.items.length)$('history').append(text('p','暂无部署记录。','empty'));
  const current=$('appId').value;$('appId').replaceChildren(new Option('创建新应用',''));
  for(const app of apps.items)$('appId').append(new Option(`${app.subdomain} · ${app.id.slice(0,8)}`,app.id));
  $('appId').value=current;
  $('appId').onchange=()=>{const app=apps.items.find(a=>a.id===$('appId').value);if(app){$('repository').value=app.repository;$('subdomain').value=app.subdomain;}requestKey=crypto.randomUUID();};
}
form.addEventListener('input',()=>{requestKey=crypto.randomUUID();});
form.addEventListener('submit',async(event)=>{event.preventDefault();$('submit').disabled=true;$('form-error').textContent='';try{const body=Object.fromEntries(new FormData(form));if(!body.appId)delete body.appId;const job=await api('/api/v1/deployments',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':requestKey},body:JSON.stringify(body)});requestKey=crypto.randomUUID();await refresh();await watch(job.id);}catch(error){$('form-error').textContent=error.message;}finally{$('submit').disabled=false;}});
$('refresh').addEventListener('click',()=>refresh().catch(e=>{$('form-error').textContent=e.message;}));
api('/api/v1/capabilities').then(c=>{$('domain-suffix').textContent='.'+c.baseDomain;aiConfigured=Boolean(c.aiAdvice?.configured);if(currentJob)showJob(currentJob);}).catch(e=>{$('form-error').textContent=e.message;});
refresh().catch(e=>{$('form-error').textContent=e.message;});
