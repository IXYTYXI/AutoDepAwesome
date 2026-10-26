const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../src/awesome_deploy/static');
class Element {
  constructor(){this.textContent='';this.hidden=false;this.disabled=false;this.listeners={};this.attributes={};}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  getAttribute(name){return this.attributes[name] ?? null;}
  set innerHTML(value){throw new Error('Unsafe HTML rendering');}
}
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
const session={mode:'feishu',authenticated:true,displayName:'测试员工',role:'admin',csrfToken:'opaque-csrf-value'};
function harness({script='auth.js',data=session,search=''}={}) {
  const elements=new Map(),calls=[],navigations=[],events={};
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const h={get,calls,navigations};h.handler=async url=>response(url==='/auth/session'?data:{});
  const window={location:{origin:'https://console.example.test',search,assign:url=>navigations.push(url)},addEventListener:(name,callback)=>{events[name]=callback;}};
  window.fetch=async(url,options={})=>{calls.push({url,options});return h.handler(url,options);};
  const context=vm.createContext({window,document:{getElementById:get},URL,URLSearchParams,Headers,console,
    localStorage:{getItem(){throw new Error('No browser credential storage');},setItem(){throw new Error('No browser credential storage');}},
    sessionStorage:{getItem(){throw new Error('No browser credential storage');},setItem(){throw new Error('No browser credential storage');}},
  });
  vm.runInContext(fs.readFileSync(path.join(root,script),'utf8'),context);
  h.auth=window.AwesomeAuth;h.events=events;return h;
}
async function tick(){await new Promise(setImmediate);}

test('all console requests wait for session; writes alone carry a fresh CSRF token',async()=>{
  const h=harness();await h.auth.ready;
  assert.equal(h.get('auth-name').textContent,'测试员工');assert.equal(h.get('auth-role').textContent,'管理员');
  assert.equal(h.get('auth-logout').hidden,false);
  await h.auth.fetch('/api/v1/apps');await h.auth.fetch('/api/v1/deployments',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  assert.equal(h.calls[1].options.headers.get('X-CSRF-Token'),null);
  assert.equal(h.calls[2].options.headers.get('X-CSRF-Token'),session.csrfToken);
  assert.equal(h.calls[2].options.headers.get('Content-Type'),'application/json');
  assert.equal(h.calls[2].options.credentials,'same-origin');
});

test('a deployment bound to the previous account cannot pass a pending authentication refresh',async()=>{
  const h=harness({data:{...session,userId:'member-one'}});await h.auth.ready;let finish;
  h.handler=async url=>url==='/auth/session'?new Promise(resolve=>{finish=resolve;}):response({});
  const refreshing=h.auth.refresh();await tick();
  const write=h.auth.fetch('/api/v1/deployments',{method:'POST',body:'{}',expectedUserId:'member-one'});
  finish(response({...session,userId:'member-two'}));await refreshing;
  await assert.rejects(write,error=>error.auth===true && /账号已切换/.test(error.message));
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
  h.handler=async()=>response({});
  await h.auth.fetch('/api/v1/deployments',{method:'POST',body:'{}',expectedUserId:'member-two'});
  assert.equal(h.calls.at(-1).options.expectedUserId,undefined);
});

test('external destinations cannot receive a session or CSRF token',async()=>{
  const h=harness();await h.auth.ready;
  await assert.rejects(h.auth.fetch('https://evil.example/action',{method:'POST'}),/同源/);
  assert.equal(h.calls.length,1);
});

test('Basic mode remains usable without CSRF or a misleading logout button',async()=>{
  const h=harness({data:{mode:'basic',authenticated:true,displayName:'legacy',role:'admin'}});await h.auth.ready;
  await h.auth.fetch('/api/v1/apps',{method:'POST',body:'{}'});
  assert.equal(h.get('auth-logout').hidden,true);assert.equal(h.calls[1].options.headers.get('X-CSRF-Token'),null);
});

test('expired authentication retains the current page and offers login in a separate tab',async()=>{
  const h=harness();await h.auth.ready;h.handler=async()=>response({detail:'Session expired'},401);
  await h.auth.fetch('/api/v1/apps');
  assert.equal(h.get('auth-notice').hidden,false);assert.match(h.get('auth-notice-text').textContent,/登录已失效/);
  assert.equal(h.navigations.length,0);
  await assert.rejects(h.auth.fetch('/api/v1/deployments',{method:'POST'}),error=>error.auth===true);
  assert.equal(h.calls.length,2);
});

test('temporary session check failure blocks writes, shows retry, and can recover',async()=>{
  const h=harness();h.handler=async()=>response({detail:'Unavailable'},503);
  await h.auth.ready;await h.auth.refresh();
  await assert.rejects(h.auth.fetch('/api/v1/deployments',{method:'POST'}),error=>error.auth===true);
  assert.match(h.get('auth-notice-text').textContent,/暂时无法/);
  h.handler=async()=>response(session);await h.auth.refresh();
  assert.equal(h.get('auth-notice').hidden,true);
});

test('logout is a CSRF protected JSON request, redirects only after confirmed success',async()=>{
  const h=harness();await h.auth.ready;await h.get('auth-logout').listeners.click();
  const call=h.calls.find(item=>item.url==='/auth/logout');
  assert.equal(call.options.method,'POST');assert.equal(call.options.body,'{}');
  assert.equal(call.options.headers.get('X-CSRF-Token'),session.csrfToken);
  assert.deepEqual(h.navigations,['/login']);
});

test('logout failure preserves session and shows an actionable error',async()=>{
  const h=harness();await h.auth.ready;h.handler=async()=>response({},503);
  await h.get('auth-logout').listeners.click();
  assert.equal(h.navigations.length,0);assert.match(h.get('auth-notice-text').textContent,/退出未完成/);
  assert.equal(h.get('auth-logout').disabled,false);
});

test('display names render as text; missing names have a readable fallback',async()=>{
  const h=harness({data:{...session,displayName:'<img src=x onerror=alert(1)>'}});await h.auth.ready;
  assert.equal(h.get('auth-name').textContent,'<img src=x onerror=alert(1)>');
  h.handler=async()=>response({...session,displayName:''});await h.auth.refresh();assert.equal(h.get('auth-name').textContent,'飞书用户');
});

test('login errors are an allowlist and never reflect query input',()=>{
  const h=harness({script:'login.js',search:'?error=%3Cscript%3Ealert(1)%3C/script%3E'});
  assert.equal(h.get('login-error').hidden,true);assert.equal(h.get('login-error').textContent,'');
  const denied=harness({script:'login.js',search:'?error=denied'});
  assert.equal(denied.get('login-error').hidden,false);assert.match(denied.get('login-error').textContent,/联系管理员/);
});

test('login returns to an allowed page after authentication and ignores external destinations',()=>{
  const page=harness({script:'login.js',search:'?error=session&return=%2Fapi-access'});
  assert.equal(page.get('feishu-login').href,'/auth/feishu/start?return=%2Fapi-access');
  const external=harness({script:'login.js',search:'?return=https%3A%2F%2Fevil.example'});
  assert.equal(external.get('feishu-login').href,undefined);
});

test('login navigation has loading feedback, blocks duplicate clicks, and resets on back navigation',()=>{
  const h=harness({script:'login.js'});let prevented=0;
  const event={button:0,preventDefault(){prevented++;}};
  h.get('feishu-login').listeners.click(event);
  assert.equal(h.get('feishu-login').attributes['aria-busy'],'true');
  assert.match(h.get('feishu-login-label').textContent,/正在前往飞书/);
  h.get('feishu-login').listeners.click(event);assert.equal(prevented,1);
  h.events.pageshow();assert.equal(h.get('feishu-login').attributes['aria-busy'],undefined);
});

test('console assets load auth before application and login does not depend on JavaScript for navigation',()=>{
  for(const file of ['index.html','manage.html']) {
    const html=fs.readFileSync(path.join(root,file),'utf8');
    assert.ok(html.indexOf('/assets/auth.js')<html.indexOf(`/assets/${file==='index.html'?'app':'manage'}.js`));
    assert.match(html,/id="auth-login"[^>]+href="\/login"[^>]+target="_blank"[^>]+rel="noopener"/);
    assert.match(html,/id="auth-retry"/);
  }
  const login=fs.readFileSync(path.join(root,'login.html'),'utf8');
  assert.match(login,/id="feishu-login"[^>]+href="\/auth\/feishu\/start"/);
  assert.match(login,/id="login-error"[^>]+role="alert"/);
});

class MemberElement extends Element {
  constructor(tag='div'){super();this.tagName=tag;this.children=[];this.value='';this.open=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[];this.textContent='';this.append(...nodes);}
  focus(){this.focused=true;}
  scrollIntoView(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const nodes=node=>[node,...node.children.flatMap(nodes)];
function memberHarness({data={items:[{userId:'user-one',displayName:'林晓',role:'owner'},{userId:'user-two',displayName:'苏敏',role:'viewer'}],permissions:{manageMembers:true},revision:1},appId='app-one'}={}) {
  const elements=new Map(),calls=[];const get=id=>{if(!elements.has(id))elements.set(id,new MemberElement());return elements.get(id);};
  const h={get,calls};h.handler=async()=>response(data);
  const window={location:{search:'?appId='+encodeURIComponent(appId)},AwesomeAuth:{ready:Promise.resolve(),session:()=>({...session,userId:'user-one'}),refresh:async()=>session,fetch:async(url,options={})=>{calls.push({url,options});return h.handler(url,options);}}};
  const context=vm.createContext({window,document:{getElementById:get,createElement:tag=>new MemberElement(tag)},URLSearchParams,console});
  vm.runInContext(fs.readFileSync(path.join(root,'members.js'),'utf8'),context);return h;
}

test('member list offers explicit role choices, no owner removal, and renders untrusted names safely',async()=>{
  const h=memberHarness();await tick();
  const list=nodes(h.get('member-list'));
  assert.ok(list.some(node=>node.textContent==='负责人'));assert.ok(list.some(node=>node.tagName==='select'));
  assert.equal(list.filter(node=>node.textContent==='移除成员').length,1);
  h.handler=async()=>response({items:[{userId:'<x>',displayName:'<img src=x>',role:'viewer'}],permissions:{manageMembers:true}});
  await h.get('members-refresh').listeners.click();assert.ok(nodes(h.get('member-list')).some(node=>node.textContent==='<img src=x>'));
});

test('adding a member sends only explicit user identity and selected role',async()=>{
  const h=memberHarness();await tick();h.get('member-user-id').value='user-three';h.get('member-role').value='maintainer';
  await h.get('member-form').listeners.submit({preventDefault(){}});
  const call=h.calls.find(call=>call.options.method==='PUT');assert.equal(call.url,'/api/v1/apps/app-one/members/user-three');
  assert.equal(call.options.body,JSON.stringify({role:'maintainer'}));
});

test('member removal is reviewable and sends nothing until confirmation',async()=>{
  const h=memberHarness();await tick();
  await nodes(h.get('member-list')).find(node=>node.textContent==='移除成员').listeners.click();
  assert.equal(h.get('member-dialog').open,true);assert.equal(h.calls.some(call=>call.options.method==='DELETE'),false);
  await h.get('member-confirm').listeners.click();
  assert.ok(h.calls.some(call=>call.options.method==='DELETE' && call.options.body==='{}'));
});

test('forbidden member management stays read-only and missing app identity does not request a global directory',async()=>{
  const h=memberHarness({data:{items:[],permissions:{manageMembers:false}}});await tick();
  assert.equal(h.get('member-form').hidden,true);assert.equal(h.get('owner-form').hidden,true);
  const missing=memberHarness({appId:''});await tick();assert.equal(missing.calls.length,0);assert.equal(missing.get('member-count').textContent,'未选择应用');assert.equal(missing.get('members-refresh').disabled,true);assert.match(missing.get('members-error').textContent,/应用编号/);
});

test('legacy owner assignment is only shown when explicitly allowed and requires confirmation',async()=>{
  const h=memberHarness({data:{items:[],permissions:{manageMembers:false,assignOwner:true}}});await tick();
  assert.equal(h.get('owner-form').hidden,false);h.get('owner-user-id').value='user-three';
  h.get('owner-form').listeners.submit({preventDefault(){}});assert.equal(h.get('member-dialog').open,true);assert.match(h.get('member-dialog-message').textContent,/可使用其已有配置和已绑定的仓库访问权限/);
  await h.get('member-confirm').listeners.click();
  assert.equal(h.calls.find(call=>call.url.endsWith('/owner')).options.body,JSON.stringify({userId:'user-three'}));
});

test('fresh-login authorization error explains reauthentication without navigating away',async()=>{
  const h=harness();await h.auth.ready;
  h.handler=async()=>({...response({detail:{code:'AUTH_REAUTH_REQUIRED'}},403),clone:()=>({json:async()=>({detail:{code:'AUTH_REAUTH_REQUIRED'}})})});
  await h.auth.fetch('/api/v1/apps/app-one/transfer',{method:'POST'});
  assert.match(h.get('auth-notice-text').textContent,/最近 10 分钟/);assert.equal(h.navigations.length,0);
});

test('session invalidation clears both logout and copy-identity actions',async()=>{
  const h=harness({data:{...session,userId:'member-one'}});await h.auth.ready;
  assert.equal(h.get('auth-copy-id').hidden,false);h.handler=async()=>response({},401);await h.auth.fetch('/api/v1/apps');
  assert.equal(h.get('auth-copy-id').hidden,true);assert.equal(h.get('auth-logout').hidden,true);
});

function consoleHarness({script='app.js',canDeploy=true,apps=[]}={}) {
  const elements=new Map(),calls=[];
  const document={addEventListener(){},activeElement:null};
  const get=id=>{if(!elements.has(id)){const node=new MemberElement();node.scrollIntoView=()=>{};node.contains=()=>false;elements.set(id,node);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>new MemberElement(tag);
  let session={...sessionDefaults(),canDeploy};
  function sessionDefaults(){return {mode:'feishu',authenticated:true,displayName:'成员',userId:'user-one',role:'member'};}
  const window={AwesomeAuth:{session:()=>session,onChange:()=>{},ready:Promise.resolve(),fetch:async(url,options={})=>{calls.push({url,options});return response(url.endsWith('/management')?{apps,deployments:[],offlineOperations:[]}:url.endsWith('/apps')?{items:apps}:url.endsWith('/capabilities')?{}:{items:[]});}}};
  const context=vm.createContext({window,document,URL,URLSearchParams,console,crypto:{randomUUID:()=> 'test-key'},setTimeout:()=>1,clearTimeout(){},FormData:class {},Uint8Array,TextEncoder});
  vm.runInContext(fs.readFileSync(path.join(root,script),'utf8'),context);
  return {get,calls,run:code=>vm.runInContext(code,context)};
}

test('deployment form rejects a member without global deployment permission',async()=>{
  const h=consoleHarness({canDeploy:false});await tick();
  assert.equal(h.get('submit').disabled,true);assert.equal(h.get('deploy-permission').hidden,false);
  await h.get('deploy-form').listeners.submit({preventDefault(){}});assert.equal(h.calls.some(call=>call.options.method==='POST'),false);
});

test('deployment selector excludes viewer-only apps and cannot select them programmatically',async()=>{
  const h=consoleHarness({apps:[{id:'viewer-app',subdomain:'view',permissions:{deploy:false}},{id:'own-app',subdomain:'own',repository:'https://github.com/team/own',permissions:{deploy:true}}]});await tick();
  h.run('openAppPicker()');assert.equal(h.run('appChoices.some(app=>app.id==="viewer-app")'),false);
  h.run('chooseApp("viewer-app")');assert.equal(h.get('appId').value,'');
});

test('management overview hides viewer deployment actions and shows explicit owner controls',async()=>{
  const h=consoleHarness({script:'manage.js',canDeploy:false,apps:[{id:'viewer-app',subdomain:'view',status:'online',permissions:{viewDetails:false,deploy:false,offline:false}},{id:'owner-app',subdomain:'own',status:'offline',permissions:{manageMembers:true}},{id:'legacy-app',subdomain:'old',status:'offline',permissions:{assignOwner:true}}]});await tick();
  const viewer=nodes(h.get('online-apps'));
  assert.equal(h.get('management-deploy').hidden,true);assert.ok(viewer.some(node=>node.textContent==='仅有概览权限'));assert.equal(viewer.some(node=>node.textContent==='下线'),false);
  const retained=nodes(h.get('retained-apps'));
  assert.ok(retained.some(node=>node.href==='/members?appId=owner-app'));assert.ok(retained.some(node=>node.textContent==='指定负责人'));
});


test('sensitive member update failure keeps reauthentication accessible inside the modal and restores focus',async()=>{
  const h=memberHarness();await tick();
  nodes(h.get('member-list')).find(node=>node.textContent==='转交负责人').listeners.click();
  h.handler=async()=>response({detail:{code:'AUTH_REAUTH_REQUIRED',message:'请重新登录'}},403);
  await h.get('member-confirm').listeners.click();
  assert.equal(h.get('member-dialog').open,true);assert.equal(h.get('member-dialog-reauth').hidden,false);
  assert.equal(h.get('member-confirm').focused,true);
  await h.get('member-dialog-check').listeners.click();assert.match(h.get('member-dialog-error').textContent,/再次确认/);
});

test('repository credential controls require explicit administrator permission and configured credentials',async()=>{
  const member=memberHarness();await tick();assert.equal(member.get('repository-credentials-panel').hidden,true);
  const unconfigured=memberHarness({data:{items:[],permissions:{manageMembers:true,manageRepositoryCredentials:true},repositoryCredentialsApproved:true,repositoryCredentialsConfigured:false}});await tick();
  assert.equal(unconfigured.get('repository-credentials-panel').hidden,false);
  assert.equal(unconfigured.get('repository-credentials-approve').disabled,true);
  assert.equal(unconfigured.get('repository-credentials-revoke').disabled,false);
  assert.match(unconfigured.get('repository-credentials-help').textContent,/联系运维/);
});

test('repository credential approval is reviewable, scoped, and sends only an approval flag',async()=>{
  const h=memberHarness({data:{items:[],permissions:{manageMembers:true,manageRepositoryCredentials:true},repositoryCredentialsApproved:false,repositoryCredentialsConfigured:true}});await tick();
  h.get('repository-credentials-approve').listeners.click();
  assert.equal(h.get('member-dialog').open,true);
  assert.match(h.get('member-dialog-message').textContent,/仅允许此应用的固定仓库使用平台已配置的读取凭证/);
  assert.match(h.get('member-dialog-message').textContent,/不会向成员展示凭证/);
  assert.match(h.get('member-dialog-message').textContent,/旧排队授权失效/);
  assert.equal(h.calls.some(call=>call.options.method==='PUT'),false);
  await h.get('member-confirm').listeners.click();
  const call=h.calls.find(call=>call.url.endsWith('/repository-credentials'));
  assert.equal(call.options.method,'PUT');assert.equal(call.options.body,JSON.stringify({approved:true}));
});

test('repository credential approval cannot run before operator configuration; revocation still works',async()=>{
  const h=memberHarness({data:{items:[],permissions:{manageRepositoryCredentials:true},repositoryCredentialsApproved:true,repositoryCredentialsConfigured:false}});await tick();
  h.get('repository-credentials-approve').listeners.click();assert.equal(h.get('member-dialog').open,false);
  h.get('repository-credentials-revoke').listeners.click();await h.get('member-confirm').listeners.click();
  assert.equal(h.calls.find(call=>call.url.endsWith('/repository-credentials')).options.body,JSON.stringify({approved:false}));
});
