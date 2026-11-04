(() => {
  'use strict';
  const get=id=>document.getElementById(id);
  const appId=new URLSearchParams(window.location.search).get('appId') || '';
  const base='/api/v1/apps/'+encodeURIComponent(appId);
  const roles={owner:'负责人',maintainer:'维护者',viewer:'查看者'};
  let permissions={},busy=false,loading=false,confirmation=null,dialogTrigger=null,hasSnapshot=false;
  let repositoryCredentialsApproved=null,repositoryCredentialsConfigured=false;
  function text(tag,value,className){const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node;}
  function action(label,handler){const button=text('button',label,'secondary');button.type='button';button.addEventListener('click',handler);return button;}
  function status(message){get('members-notice').textContent=message;}
  function errorMessage(data){return data?.error?.message || (typeof data?.detail==='string'?data.detail:data?.detail?.message) || '操作未完成，请刷新状态后重试。';}
  async function request(url,options={}) {
    const response=await window.AwesomeAuth.fetch(url,options);
    let data;try{data=await response.json();}catch{throw new Error('无法读取服务器响应，请刷新状态后确认结果。');}
    if(!response.ok){const error=new Error(errorMessage(data));error.code=data.code || data.error?.code || data.detail?.code;throw error;}return data;
  }
  function syncBusy(){
    get('member-add').disabled=busy || loading || !permissions.manageMembers;
    get('owner-assign').disabled=busy || loading || !permissions.assignOwner;
    get('members-refresh').disabled=busy || loading;
    const repositoryAllowed=permissions.manageRepositoryCredentials===true;
    get('repository-credentials-approve').disabled=busy || loading || !repositoryAllowed || repositoryCredentialsConfigured!==true || repositoryCredentialsApproved!==false;
    get('repository-credentials-revoke').disabled=busy || loading || !repositoryAllowed || repositoryCredentialsApproved!==true;
    get('repository-credentials-panel').setAttribute('aria-busy',String(busy || loading));
    get('member-confirm').disabled=busy;get('member-cancel').disabled=busy;
    get('member-confirm').textContent=busy?'正在保存…':'确认更改';
    get('member-dialog').setAttribute('aria-busy',String(busy));
    get('member-form').setAttribute('aria-busy',String(busy));
    get('member-user-id').disabled=busy;get('member-role').disabled=busy;get('owner-user-id').disabled=busy;
    get('member-list').setAttribute('aria-busy',String(busy || loading));
  }
  function showConfirmation({title,message,method,url,body,trigger}) {
    if(busy || loading)return;
    confirmation={method,url,body};dialogTrigger=trigger;
    get('member-dialog-title').textContent=title;get('member-dialog-message').textContent=message;
    get('member-dialog-error').textContent='';get('member-dialog-reauth').hidden=true;get('member-dialog').showModal();get('member-cancel').focus();
  }
  function closeConfirmation(){if(busy)return;confirmation=null;get('member-dialog').close();dialogTrigger?.focus();}
  function memberRow(member,index) {
    const row=text('article','','member-row');
    const info=text('div','','member-info');info.append(text('strong',member.displayName || '未命名成员'),text('code',member.userId,'member-id'));row.append(info);
    if(member.role==='owner' || !permissions.manageMembers)row.append(text('span',roles[member.role] || '未知角色','member-role-label'));
    else {
      const field=text('div','','member-role-field');const label=text('label','角色');label.setAttribute('for','role-'+index);
      const select=document.createElement('select');select.id='role-'+index;select.name='role';select.setAttribute('aria-label',(member.displayName || member.userId)+'的角色');
      for(const role of ['maintainer','viewer']){const option=text('option',roles[role]);option.value=role;select.append(option);}select.value=member.role;
      field.append(label,select);row.append(field);
      const actions=text('div','','member-actions');
      const update=action('保存角色',()=>{
        if(!['maintainer','viewer'].includes(select.value))return;
        if(select.value===member.role){status('角色没有变化。');return;}
        showConfirmation({title:'变更成员角色',message:`将 ${member.displayName || member.userId} 的角色改为${roles[select.value]}。新权限保存后生效。`,method:'PUT',url:base+'/members/'+encodeURIComponent(member.userId),body:{role:select.value},trigger:update});
      });
      const remove=action('移除成员',()=>showConfirmation({title:'移除应用成员',message:`移除 ${member.displayName || member.userId} 后，该成员将失去此应用的成员权限。平台管理员的全局权限不受影响。`,method:'DELETE',url:base+'/members/'+encodeURIComponent(member.userId),body:{},trigger:remove}));
      const transfer=action('转交负责人',()=>showConfirmation({title:'转交应用负责人',message:`将此应用的负责人转交给 ${member.displayName || member.userId}。你将不再是此应用的负责人，后续权限以新成员角色为准。`,method:'POST',url:base+'/transfer',body:{userId:member.userId},trigger:transfer}));
      actions.append(update,remove,transfer);row.append(actions);
    }
    return row;
  }
  async function load() {
    if(loading || busy)return;
    if(!appId){get('members-error').textContent='缺少应用编号。请从应用管理页重新打开成员管理。';get('member-count').textContent='未选择应用';get('member-list').setAttribute('aria-busy','false');get('members-refresh').disabled=true;return;}
    loading=true;syncBusy();get('members-error').textContent='';
    try {
      const data=await request(base+'/members');
      if(!Array.isArray(data.items) || !data.permissions)throw new Error('成员数据不完整，请稍后重试。');
      permissions=data.permissions;
      repositoryCredentialsApproved=typeof data.repositoryCredentialsApproved==='boolean'?data.repositoryCredentialsApproved:null;
      repositoryCredentialsConfigured=data.repositoryCredentialsConfigured===true;
      get('repository-credentials-panel').hidden=permissions.manageRepositoryCredentials!==true;
      get('repository-credentials-status').textContent=repositoryCredentialsApproved===true?'已批准：此应用可对固定仓库使用平台已配置的读取凭证。':repositoryCredentialsApproved===false?'未批准：此应用不会使用平台的仓库读取凭证。':'尚未确认授权状态，请刷新成员信息后重试。';
      get('repository-credentials-help').textContent=repositoryCredentialsConfigured?'此授权由平台管理员管理。批准或撤销均需要最近 10 分钟内的登录确认。':'平台尚未配置仓库读取凭证，请联系运维完成配置后再批准。现有授权仍可撤销。';
      get('member-form').hidden=permissions.manageMembers!==true;
      get('owner-form').hidden=permissions.assignOwner!==true;
      get('member-readonly').hidden=permissions.manageMembers===true || permissions.assignOwner===true;
      get('member-count').textContent=`当前 ${data.items.length} 位应用成员`;
      get('member-invite').hidden=permissions.manageMembers!==true && permissions.assignOwner!==true;
      get('member-list').replaceChildren(...data.items.map(memberRow));
      if(!data.items.length)get('member-list').append(text('p',permissions.assignOwner ? '此应用尚未指定负责人，请先完成负责人指派。' : '当前没有可显示的应用成员。','members-empty'));
      hasSnapshot=true;
    } catch(error){if(!hasSnapshot)get('member-count').textContent='尚未读取到成员，请刷新重试';get('members-error').textContent=(error.message || '读取成员失败，请稍后重试。')+(hasSnapshot?' 当前保留上次读取的成员信息，请刷新确认。':'');}
    finally {loading=false;syncBusy();}
  }
  async function mutate(url,method,body,{clearInput}={}) {
    if(busy || loading)return;
    busy=true;syncBusy();get('members-error').textContent='';get('member-dialog-error').textContent='';status('');
    try {
      await request(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(clearInput)get(clearInput).value='';
      confirmation=null;get('member-dialog').close();
      status('更改已保存。正在读取最新成员状态…');
      busy=false;await load();status('更改已保存。');get('members-refresh').focus();
    } catch(error) {
      const message=error.auth?error.message:(error.message || '保存失败，请刷新成员状态后确认结果。');
      get('members-error').textContent=message;if(get('member-dialog').open){get('member-dialog-error').textContent=message;get('member-dialog-reauth').hidden=!['AUTH_REAUTH_REQUIRED','REAUTH_REQUIRED'].includes(error.code) && !error.auth;}
    } finally {busy=false;syncBusy();if(get('member-dialog').open)get('member-confirm').focus();}
  }
  function confirmRepositoryCredentials(approved){
    if(busy || loading || permissions.manageRepositoryCredentials!==true)return;
    if(approved ? repositoryCredentialsConfigured!==true || repositoryCredentialsApproved!==false : repositoryCredentialsApproved!==true)return;
    showConfirmation({title:approved?'批准固定仓库读取权限':'撤销固定仓库读取权限',message:(approved?'确认批准此应用使用仓库读取凭证。':'确认撤销此应用的仓库读取授权，后续私有仓库部署可能无法读取源码。')+'仅允许此应用的固定仓库使用平台已配置的读取凭证；不会向成员展示凭证。变更会使旧排队授权失效。',method:'PUT',url:base+'/repository-credentials',body:{approved},trigger:get(approved?'repository-credentials-approve':'repository-credentials-revoke')});
  }
  get('repository-credentials-approve').addEventListener('click',()=>confirmRepositoryCredentials(true));
  get('repository-credentials-revoke').addEventListener('click',()=>confirmRepositoryCredentials(false));
  get('member-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!permissions.manageMembers || busy || loading)return;
    const userId=get('member-user-id').value.trim(),role=get('member-role').value;
    if(!userId || !['maintainer','viewer'].includes(role)){get('members-error').textContent='请填写已登录平台的成员 ID，并选择角色。';return;}
    await mutate(base+'/members/'+encodeURIComponent(userId),'PUT',{role},{clearInput:'member-user-id'});
  });
  get('owner-form').addEventListener('submit',event=>{
    event.preventDefault();if(!permissions.assignOwner || busy || loading)return;
    const userId=get('owner-user-id').value.trim();if(!userId){get('members-error').textContent='请填写新负责人的平台成员 ID。';return;}
    showConfirmation({title:'指定应用负责人',message:`将成员 ${userId} 设为此应用的负责人。请核对成员 ID。新负责人将获得此应用的管理权限，并可使用其已有配置和已绑定的仓库访问权限。`,method:'POST',url:base+'/owner',body:{userId},trigger:get('owner-assign')});
  });
  get('member-dialog-check').addEventListener('click',async()=>{
    const button=get('member-dialog-check');button.disabled=true;button.textContent='正在检查…';
    try {await window.AwesomeAuth.refresh();get('member-dialog-error').textContent=window.AwesomeAuth.session()?'登录状态已重新检查，请再次确认更改。':'登录尚未恢复，请先在新标签页完成登录。';}
    finally {button.disabled=false;button.textContent='重新检查登录';button.focus();}
  });
  get('member-confirm').addEventListener('click',async()=>{if(confirmation)await mutate(confirmation.url,confirmation.method,confirmation.body);});
  get('member-cancel').addEventListener('click',closeConfirmation);
  get('member-dialog').addEventListener('cancel',event=>{event.preventDefault();closeConfirmation();});
  get('members-refresh').addEventListener('click',load);
  get('member-app-id').textContent=appId || '未指定';
  window.AwesomeAuth.ready.then(()=>{get('member-self-id').textContent=window.AwesomeAuth.session()?.userId || '当前会话未提供成员 ID';return load();});
})();
