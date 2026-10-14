const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const staticRoot=path.join(__dirname,'../src/awesome_deploy/static');

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.textContent='';this.value='';this.scrollTop=0;this.open=false;this.hidden=false;}
  append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this.scrollTop=0;this.textContent='';this.append(...nodes);}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name] ?? null;}
  removeAttribute(name){delete this[name];}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  focus(options){this.ownerDocument.activeElement=this;this.lastFocusOptions=options;}
  scrollIntoView(){}
  showModal(){this.open=true;}
  close(){this.open=false;this.listeners.close?.();}
  set innerHTML(value){throw new Error('Unsafe HTML rendering: '+value);}
}
function all(node){return [node,...node.children.flatMap(all)];}
const online={id:'app-one',repository:'https://github.com/team/demo.git',subdomain:'demo',status:'online',activeReleaseId:'release-one',retainedReleaseId:null,commit:'123abc',ref:'main',url:'https://demo.example.test',updatedAt:1791855000.25,canOffline:true};
const job={id:'job-one',appId:online.id,subdomain:'demo',repository:online.repository,ref:'main',status:'running',phase:'build',createdAt:1791855000.25};
const empty={apps:[],deployments:[],offlineOperations:[]};
function harness({data=empty,script='manage.js',search=''}={}) {
  const elements=new Map(),calls=[],timers=[];let key=0;
  const document={activeElement:null,listeners:{},addEventListener(name,handler){this.listeners[name]=handler;}};
  const get=id=>{if(!elements.has(id)){const element=new Element(id);element.ownerDocument=document;elements.set(id,element);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>{const element=new Element(tag);element.ownerDocument=document;return element;};
  get('app-picker').append(get('app-search'),get('app-menu'));
  get('app-menu').append(get('app-options'));
  const context=vm.createContext({document,URL,URLSearchParams,location:{search},console,
    crypto:{randomUUID:()=>`key-${++key}`},
    setTimeout:(callback,delay)=>{timers.push({callback,delay});return timers.length;},clearTimeout:()=>{},
    fetch:async(url,options={})=>{calls.push({url,options});return {ok:true,status:200,json:async()=>url.endsWith('/management') ? data : url.endsWith('/capabilities') ? {aiAdvice:{configured:false}} : url==='/api/v1/deployments/job-one' ? job : {items:[]}};},
  });
  const filename=path.join(staticRoot,script);
  assert.ok(fs.existsSync(filename),`${script} must exist`);
  vm.runInContext(fs.readFileSync(filename,'utf8'),context);
  return {get,context,calls,timers,run:code=>vm.runInContext(code,context)};
}
async function settle(){await new Promise(setImmediate);}

test('management page has its own navigation, named scroll windows and confirmation dialog',()=>{
  const filename=path.join(staticRoot,'manage.html');assert.ok(fs.existsSync(filename));
  const html=fs.readFileSync(filename,'utf8');
  assert.match(html,/href="\/"[^>]*>部署控制台/);
  assert.match(html,/href="\/manage"[^>]*aria-current="page"/);
  assert.match(html,/<dialog[^>]+id="offline-dialog"/);
  assert.match(html,/下线后将停止服务并关闭公开访问。数据和部署记录保留。/);
  assert.doesNotMatch(html,/<select\b/);
  assert.match(html,/id="online-apps"[^>]+tabindex="0"[^>]+role="region"/);
  assert.doesNotMatch(fs.readFileSync(path.join(staticRoot,'index.html'),'utf8'),/运维控制台/);
});

test('management renders more than fifty apps and all active deployment states with live counts',async()=>{
  const apps=Array.from({length:61},(_,i)=>({...online,id:'app-'+i,subdomain:'app'+i}));
  apps.push({...online,id:'offline',status:'offline',canOffline:false,activeReleaseId:null,retainedReleaseId:'old'});
  apps.push({...online,id:'new',status:'not_deployed',canOffline:false,activeReleaseId:null});
  apps.push({...online,id:'attention',status:'attention',canOffline:false});
  const data={apps,deployments:['queued','running','recovery_required'].map((status,i)=>({...job,id:'job-'+i,status})),offlineOperations:[],counts:{online:0}};
  const h=harness({data});await settle();
  assert.equal(h.get('online-apps').children.length,62);
  assert.equal(h.get('retained-apps').children.length,2);
  assert.equal(h.get('deployment-list').children.length,3);
  assert.equal(h.get('count-online').textContent,'61');
  assert.equal(h.get('count-queued').textContent,'1');
  assert.equal(h.get('count-running').textContent,'1');
  assert.equal(h.get('count-attention').textContent,'2');
  assert.ok(h.timers.some(timer=>timer.delay===3000));
});

test('untrusted app names, errors and repository strings render as text and unsafe links are omitted',async()=>{
  const bad='<img src=x onerror=attack()>';
  const h=harness({data:{apps:[{...online,subdomain:bad,repository:bad,url:'javascript:attack()'}],deployments:[{...job,error:{message:bad}}],offlineOperations:[]}});await settle();
  const nodes=[...all(h.get('online-apps')),...all(h.get('deployment-list'))];
  assert.ok(nodes.some(node=>node.textContent===bad));
  assert.ok(!nodes.some(node=>node.href==='javascript:attack()'));
  assert.ok(nodes.some(node=>node.href==='/?deployment=job-one'));
});

test('offline confirmation cancellation sends no mutation',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();
  h.run("openOfflineDialog('app-one')");
  assert.equal(h.get('offline-dialog').open,true);
  assert.ok(h.get('offline-target').textContent.includes('demo'));
  h.get('offline-cancel').listeners.click();
  assert.equal(h.get('offline-dialog').open,false);
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
});

test('offline duplicate clicks are blocked and a failed request retries with the same key',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();h.run("openOfflineDialog('app-one')");
  let fail;const posts=[];
  h.context.fetch=async(url,options={})=>{
    if(options.method!=='POST')return {ok:true,json:async()=>({...empty,apps:[online]})};
    posts.push({url,options});
    if(posts.length===1)return new Promise((resolve,reject)=>{fail=reject;});
    return {ok:true,status:202,json:async()=>({id:'op-one',appId:'app-one',releaseId:'release-one',status:'queued'})};
  };
  const first=h.run('submitOffline()');await settle();await h.run('submitOffline()');
  assert.equal(posts.length,1);fail(new Error('network'));await first;
  assert.equal(h.get('offline-dialog').open,true);
  assert.ok(h.get('offline-error').textContent.includes('网络'));
  assert.equal(h.get('offline-confirm').disabled,false);
  await h.run('submitOffline()');
  assert.equal(posts.length,2);
  assert.equal(posts[0].options.headers['Idempotency-Key'],posts[1].options.headers['Idempotency-Key']);
  assert.deepEqual(JSON.parse(posts[1].options.body),{expectedReleaseId:'release-one'});
  assert.equal(h.get('offline-dialog').open,false);
  assert.ok(!h.get('management-notice').textContent.includes('下线成功'));
  assert.ok(all(h.get('online-apps')).some(node=>node.textContent.includes('下线排队中')));
});

test('confirmation cannot offline a different release that appeared while the dialog was open',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();h.run("openOfflineDialog('app-one')");
  h.context.changed={...empty,apps:[{...online,activeReleaseId:'release-two'}]};h.run('renderManagement(changed)');
  await h.run('submitOffline()');
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
  assert.ok(h.get('offline-error').textContent.includes('变化'));
});

test('failed refresh retains last known data and reports that it is stale',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();
  h.context.fetch=async()=>{throw new Error('network');};await h.run('refreshManagement()');
  assert.equal(h.get('online-apps').children.length,1);
  assert.equal(h.get('count-online').textContent,'1');
  assert.ok(h.get('management-error').textContent.includes('上次'));
});

test('refresh preserves list scroll and focused action without stealing form focus',async()=>{
  const data={...empty,apps:[online]};const h=harness({data});await settle();
  const action=all(h.get('online-apps')).find(node=>node.tagName==='button');action.focus();
  h.get('online-apps').scrollTop=180;
  await h.run('refreshManagement()');
  assert.equal(h.get('online-apps').scrollTop,180);
  assert.equal(h.context.document.activeElement.getAttribute('data-focus-key'),action.getAttribute('data-focus-key'));
  assert.equal(h.context.document.activeElement.lastFocusOptions.preventScroll,true);
  h.get('management-refresh').focus();await h.run('refreshManagement()');
  assert.equal(h.context.document.activeElement,h.get('management-refresh'));
});

test('offline applications stay visible and recovery is not displayed as success',async()=>{
  const operation={id:'op-one',appId:'app-one',releaseId:'release-one',status:'recovery_required',error:{message:'请检查路由。'},retryable:false};
  const h=harness({data:{apps:[{...online,status:'attention',canOffline:false,operation}],deployments:[],offlineOperations:[operation]}});await settle();
  assert.ok(all(h.get('online-apps')).some(node=>node.textContent.includes('请检查路由。')));
  assert.ok(!all(h.get('online-apps')).some(node=>node.tagName==='button' && node.textContent==='重试下线'));
  h.context.done={apps:[{...online,status:'offline',canOffline:false,activeReleaseId:null,retainedReleaseId:'release-one',operation:{...operation,status:'succeeded'}}],deployments:[],offlineOperations:[{...operation,status:'succeeded'}]};
  h.run('renderManagement(done)');
  assert.equal(h.get('count-online').textContent,'0');
  assert.equal(h.get('retained-apps').children.length,1);
  assert.ok(all(h.get('retained-apps')).some(node=>node.textContent.includes('下线已确认')));
  assert.ok(all(h.get('retained-apps')).some(node=>node.textContent.includes('域名前缀保留')));
});

test('recovery retry requires the server retry key and reuses it for the original release',async()=>{
  const operation={id:'old-op',appId:online.id,releaseId:'release-one',status:'recovery_required',retryable:true,retryKey:'original-retry-key',error:{message:'请重试停止服务。'}};
  const data={apps:[{...online,status:'attention',canOffline:false,activeReleaseId:null,retainedReleaseId:'release-one',operation}],deployments:[],offlineOperations:[operation]};
  const h=harness({data});await settle();
  const retry=all(h.get('online-apps')).find(node=>node.tagName==='button' && node.textContent==='重试下线');assert.ok(retry);
  retry.listeners.click();const posts=[];
  h.context.fetch=async(url,options={})=>{
    if(options.method!=='POST')return {ok:true,json:async()=>data};
    posts.push(options);return {ok:true,status:202,json:async()=>({...operation,status:'queued'})};
  };
  await h.run('submitOffline()');
  assert.equal(posts[0].headers['Idempotency-Key'],'original-retry-key');
  assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one'});
  h.context.unavailable={...data,apps:[{...data.apps[0],operation:{...operation,retryKey:null}}],offlineOperations:[{...operation,retryKey:null}]};h.run('renderManagement(unavailable)');
  assert.ok(!all(h.get('online-apps')).some(node=>node.tagName==='button' && node.textContent==='重试下线'));
});

test('an unusable server error body yields a Chinese error and keeps the last snapshot',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();
  h.context.fetch=async()=>({ok:false,status:500,json:async()=>null});await h.run('refreshManagement()');
  assert.ok(h.get('management-error').textContent.includes('请求未完成'));
  assert.ok(!h.get('management-error').textContent.includes('Cannot read'));
  assert.equal(h.get('count-online').textContent,'1');
});

test('management polling does not overlap a pending read or turn errors into empty state',async()=>{
  const h=harness({data:{...empty,apps:[online]}});await settle();
  let finish,reads=0;
  h.context.fetch=async()=>{reads++;return new Promise(resolve=>{finish=resolve;});};
  const first=h.run('refreshManagement()'),second=h.run('refreshManagement()');
  assert.equal(reads,1);
  finish({ok:true,json:async()=>({apps:[]})});await Promise.all([first,second]);
  assert.equal(h.get('count-online').textContent,'1');
  assert.ok(h.get('management-error').textContent.includes('数据不完整'));
});

test('a prior operation refresh cannot unlock a later app submission',async()=>{
  const other={...online,id:'app-two',subdomain:'other',activeReleaseId:'release-two'};
  const data={...empty,apps:[online,other]},h=harness({data});await settle();
  let refreshDone,secondDone,posts=0;
  h.context.fetch=async(url,options={})=>{
    if(options.method!=='POST')return new Promise(resolve=>{refreshDone=()=>resolve({ok:true,json:async()=>data});});
    posts++;
    if(posts===1)return {ok:true,status:202,json:async()=>({id:'op-one',appId:'app-one',releaseId:'release-one',status:'queued'})};
    return new Promise(resolve=>{secondDone=()=>resolve({ok:true,status:202,json:async()=>({id:'op-two',appId:'app-two',releaseId:'release-two',status:'queued'})});});
  };
  h.run("openOfflineDialog('app-one')");const first=h.run('submitOffline()');await settle();
  h.run("openOfflineDialog('app-two')");const second=h.run('submitOffline()');await settle();
  assert.equal(posts,2);refreshDone();await settle();
  assert.equal(h.get('offline-confirm').disabled,true);
  assert.equal(h.run('offlineSubmitting'),true);
  secondDone();await settle();refreshDone();await Promise.all([first,second]);
});

test('deployment deep link selects the requested job without creating a deployment',async()=>{
  const h=harness({script:'app.js',search:'?deployment=job-one'});await settle();
  assert.ok(h.calls.some(call=>call.url==='/api/v1/deployments/job-one'));
  assert.ok(!h.calls.some(call=>call.options.method==='POST'));
  assert.equal(h.run('selectedJob'),'job-one');
});
