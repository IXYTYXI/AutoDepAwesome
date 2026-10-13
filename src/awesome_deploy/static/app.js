const $ = (id) => document.getElementById(id);
const form = $('deploy-form');
let requestKey = crypto.randomUUID();
let selectedJob = null;
let pollTimer;
const statusLabels = {queued:'已排队',running:'执行中',succeeded:'部署成功',failed:'部署失败',recovery_required:'需要处理恢复状态'};
const phaseLabels = {source:'获取代码',prepared:'配置已校验',freeze:'固定部署输入',frozen:'输入已固定',build:'构建镜像',built:'镜像构建完成',start:'启动服务',https:'准备 HTTPS 证书',publish:'切换路由',verify:'验证发布版本',rollback:'恢复上一版本',retire:'清理旧版本',failed:'失败清理完成',queued:'排队',fetching:'获取代码',preparing:'准备配置',validating:'校验',reserved:'资源已预留',building:'构建镜像',starting:'启动服务',health:'健康检查',certificate:'准备证书',publishing:'切换路由',verifying:'验证发布版本',cleanup:'清理旧版本',complete:'完成'};
async function api(url, options={}) {
  const response = await fetch(url, {credentials:'same-origin', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : '请求未通过校验，请检查输入。'));
  return data;
}
function text(tag, value, className) {const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node;}
function showJob(job) {
  const container=$('current');container.replaceChildren();
  container.append(text('h3', `${job.subdomain} · ${statusLabels[job.status] || job.status}`, job.status));
  container.append(text('p', `阶段：${phaseLabels[job.phase] || job.phase}`));
  container.append(text('p', `部署编号：${job.id}`));
  if(job.appId)container.append(text('p', `应用编号：${job.appId}`));
  if(job.commit)container.append(text('p', `代码版本：${job.commit.slice(0,12)}`));
  if(job.error)container.append(text('p', `${job.error.code}：${job.error.message}`, 'failed'));
  if(job.url){const link=text('a','打开已部署应用 ↗');link.href=job.url;link.target='_blank';link.rel='noopener';container.append(link);}
}
async function watch(id) {
  clearTimeout(pollTimer);selectedJob=id;
  try {
    const job=await api(`/api/v1/deployments/${encodeURIComponent(id)}`);
    if(selectedJob!==id)return;
    showJob(job);
    const events=await api(`/api/v1/deployments/${encodeURIComponent(id)}/events`);
    $('events').textContent=events.items.map(e=>`${e.phase || ''}  ${e.message}`).join('\n');
    if(['queued','running'].includes(job.status))pollTimer=setTimeout(()=>watch(id),2500);
    else await refresh();
  } catch(error){$('form-error').textContent=error.message;}
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
api('/api/v1/capabilities').then(c=>{$('domain-suffix').textContent='.'+c.baseDomain;}).catch(e=>{$('form-error').textContent=e.message;});
refresh().catch(e=>{$('form-error').textContent=e.message;});
