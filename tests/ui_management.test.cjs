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
function harness({data=empty,script='manage.js',search='',sessionStore=new Map()}={}) {
  const elements=new Map(),calls=[],timers=[];let key=0;
  const document={activeElement:null,listeners:{},addEventListener(name,handler){this.listeners[name]=handler;}};
  const get=id=>{if(!elements.has(id)){const element=new Element(id);element.ownerDocument=document;elements.set(id,element);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>{const element=new Element(tag);element.ownerDocument=document;return element;};
  get('app-picker').append(get('app-search'),get('app-menu'));
  get('app-menu').append(get('app-options'));
  const context=vm.createContext({document,URL,URLSearchParams,location:{search},console,
    sessionStorage:{getItem:key=>sessionStore.get(key) ?? null,setItem:(key,value)=>sessionStore.set(key,value),removeItem:key=>sessionStore.delete(key)},
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
  assert.match(html,/下线后将停止服务并关闭公开访问/);
  assert.match(html,/下线完成后释放域名和运行端口/);
  assert.doesNotMatch(html,/域名前缀(?:继续)?保留/);
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
  assert.deepEqual(JSON.parse(posts[1].options.body),{expectedReleaseId:'release-one',dataPolicy:'retain'});
  assert.equal(h.get('offline-dialog').open,false);
  assert.ok(!h.get('management-notice').textContent.includes('下线成功'));
  assert.ok(all(h.get('online-apps')).some(node=>node.textContent.includes('下线排队中')));
  h.context.completed={apps:[{...online,status:'offline',canOffline:false,activeReleaseId:null,retainedReleaseId:'release-one',operation:{id:'op-one',appId:'app-one',releaseId:'release-one',status:'succeeded'}}],deployments:[],offlineOperations:[]};
  h.run('renderManagement(completed)');
  assert.doesNotMatch(h.get('management-notice').textContent,/等待平台确认/);
  assert.ok(all(h.get('retained-apps')).some(node=>node.textContent.includes('下线已确认')));
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
  assert.ok(!all(h.get('retained-apps')).some(node=>node.textContent.includes('已释放')));
});

test('completed unbound apps show historical identity and confirmed resource release',async()=>{
  const operation={id:'done',appId:'app-one',releaseId:'release-one',status:'succeeded'};
  const released={...online,subdomain:null,lastSubdomain:'demo',domainBound:false,status:'offline',activeReleaseId:null,retainedReleaseId:'release-one',canOffline:false,operation};
  const h=harness({data:{...empty,apps:[released]}});await settle();
  const nodes=all(h.get('retained-apps'));
  assert.ok(nodes.some(node=>node.className==='management-name' && node.textContent==='demo'));
  assert.ok(nodes.some(node=>node.textContent.includes('上次使用')));
  assert.ok(nodes.some(node=>node.textContent.includes('应用 app-one')));
  assert.ok(nodes.some(node=>node.textContent.includes('域名与端口已释放')));
  assert.ok(!nodes.some(node=>node.textContent.includes('域名前缀保留')));
  assert.ok(!h.get('retained-count').textContent.includes('域名前缀'));
});

test('pending or recovery states never claim release even when a domain is unbound',async()=>{
  for(const status of ['queued','running','recovery_required']) {
    const operation={id:'op',appId:'app-one',releaseId:'release-one',status};
    const h=harness({data:{...empty,apps:[{...online,subdomain:null,lastSubdomain:'demo',domainBound:false,status:'attention',canOffline:false,operation}]}});await settle();
    assert.ok(!all(h.get('online-apps')).some(node=>node.textContent.includes('已释放')));
    assert.ok(all(h.get('online-apps')).some(node=>node.textContent==='demo'));
  }
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
  assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one',dataPolicy:'retain'});
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

function selectPolicy(h,policy) {
  h.get('offline-retain').checked=policy==='retain';h.get('offline-delete').checked=policy==='delete';
  const control=h.get(policy==='delete' ? 'offline-delete' : 'offline-retain');
  assert.equal(typeof control.listeners.change,'function','Data policy controls must handle changes');control.listeners.change();
}
function enterDeletion(h,value) {
  const input=h.get('offline-delete-confirmation');input.value=value;
  assert.equal(typeof input.listeners.input,'function','Deletion confirmation must validate user input');input.listeners.input();
}
function acceptPosts(h,data,policy='retain') {
  const posts=[];h.context.fetch=async(url,options={})=>{
    if(options.method!=='POST')return {ok:true,json:async()=>data};
    posts.push(options);return {ok:true,status:202,json:async()=>({id:'op-one',appId:online.id,releaseId:'release-one',status:'queued',dataPolicy:policy})};
  };return posts;
}

test('data policy has accessible radio cards and a bounded scrollable confirmation dialog',()=>{
  const html=fs.readFileSync(path.join(staticRoot,'manage.html'),'utf8'),css=fs.readFileSync(path.join(staticRoot,'manage.css'),'utf8');
  assert.match(html,/<fieldset[^>]+id="offline-data-options"/);
  assert.match(html,/<input[^>]+id="offline-retain"[^>]+type="radio"[^>]+checked/);
  assert.match(html,/<input[^>]+id="offline-delete"[^>]+type="radio"/);
  assert.match(html,/<label[^>]+for="offline-delete-confirmation"/);
  assert.match(html,/不可恢复/);assert.match(html,/业务文件/);assert.match(html,/数据库/);
  assert.match(html,/API Key/);assert.match(html,/该应用的密钥/);assert.match(html,/外部数据库不会自动删除/);assert.match(html,/源码、部署历史和构建缓存/);
  assert.match(css,/\.offline-dialog\s*\{[^}]*max-height:/);
  assert.match(css,/\.offline-dialog\s*\{[^}]*overflow-y:auto/);
});

test('a fresh offline request defaults to retaining data with no deletion confirmation',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();h.run("openOfflineDialog('app-one')");
  assert.equal(h.get('offline-retain').checked,true);assert.equal(h.get('offline-delete').checked,false);
  assert.equal(h.get('offline-delete-fields').hidden,true);
  assert.equal(h.context.document.activeElement,h.get('offline-cancel'));
  const posts=acceptPosts(h,data);await h.run('submitOffline()');
  assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one',dataPolicy:'retain'});
});

test('deleting data requires the exact current target phrase before any mutation',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');
  const posts=acceptPosts(h,data,'delete');
  assert.equal(h.get('offline-delete-fields').hidden,false);
  assert.ok(h.get('offline-delete-label').textContent.includes('删除 demo'));
  for(const value of ['', '删除 other', '删除 demo ']) {
    enterDeletion(h,value);assert.equal(h.get('offline-confirm').disabled,true);await h.run('submitOffline()');
  }
  assert.equal(posts.length,0);enterDeletion(h,'删除 demo');assert.equal(h.get('offline-confirm').disabled,false);
  await h.run('submitOffline()');
  assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one',dataPolicy:'delete',deleteConfirmation:'删除 demo'});
});

test('uncertain delete requests retain their policy body and key across close and page reload',async()=>{
  const data={...empty,apps:[online]},sessionStore=new Map(),h=harness({data,sessionStore});await settle();
  h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');let first;
  h.context.fetch=async(url,options={})=>{first=options;throw new Error('lost response');};
  await h.run('submitOffline()');assert.ok(h.get('offline-error').textContent.includes('网络'));
  assert.equal(h.get('offline-retain').disabled,true);assert.equal(h.get('offline-delete').disabled,true);
  selectPolicy(h,'retain');assert.equal(h.get('offline-delete').checked,true);
  h.run('closeOfflineDialog()');h.run("openOfflineDialog('app-one')");assert.equal(h.get('offline-delete').checked,true);
  const reloaded=harness({data,sessionStore});await settle();reloaded.context.crypto.randomUUID=()=> 'must-not-create-another-key';
  reloaded.run("openOfflineDialog('app-one')");assert.equal(reloaded.get('offline-delete').checked,true);
  assert.equal(reloaded.get('offline-retain').disabled,true);
  const posts=acceptPosts(reloaded,data,'delete');await reloaded.run('submitOffline()');
  assert.equal(posts.length,1);assert.equal(posts[0].headers['Idempotency-Key'],first.headers['Idempotency-Key']);
  assert.equal(posts[0].body,first.body);
});

test('deletion retry restores server policy and key and confirms the last domain after release',async()=>{
  const operation={id:'old-op',appId:online.id,releaseId:'release-one',status:'recovery_required',retryable:true,retryKey:'server-delete-key',dataPolicy:'delete'};
  const data={...empty,apps:[{...online,subdomain:null,lastSubdomain:'demo',domainBound:false,status:'attention',canOffline:false,activeReleaseId:null,operation}],offlineOperations:[operation]};
  const h=harness({data});await settle();h.run("openOfflineDialog('app-one',true)");
  assert.equal(h.get('offline-delete').checked,true);assert.equal(h.get('offline-retain').disabled,true);
  const posts=acceptPosts(h,data,'delete');await h.run('submitOffline()');assert.equal(posts.length,0);
  selectPolicy(h,'retain');assert.equal(h.get('offline-delete').checked,true);
  enterDeletion(h,'删除 demo');await h.run('submitOffline()');
  assert.equal(posts[0].headers['Idempotency-Key'],'server-delete-key');
  assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one',dataPolicy:'delete',deleteConfirmation:'删除 demo'});
});

test('a changed domain cannot receive an already confirmed destructive request',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');
  h.context.changed={...empty,apps:[{...online,subdomain:'other'}]};h.run('renderManagement(changed)');
  const posts=acceptPosts(h,data,'delete');await h.run('submitOffline()');assert.equal(posts.length,0);
  assert.match(h.get('offline-error').textContent,/变化/);
});

test('completed deletion and retention have distinct application data status without losing history',async()=>{
  const done={...online,status:'offline',subdomain:null,lastSubdomain:'demo',domainBound:false,canOffline:false,activeReleaseId:null};
  for(const deleted of [true,false]) {
    const app={...done,dataDeleted:deleted,operation:{id:'done',appId:online.id,releaseId:'release-one',status:'succeeded',dataPolicy:deleted ? 'delete' : 'retain'}};
    const h=harness({data:{...empty,apps:[app]}});await settle();const content=all(h.get('retained-apps')).map(node=>node.textContent).join('\n');
    assert.match(content,deleted ? /数据已清除/ : /数据已保留/);assert.match(content,/部署历史保留/);
    if(deleted){assert.doesNotMatch(content,/数据(?:和部署历史)?保留/);assert.match(content,/上次下线/);assert.match(content,/后续部署可能产生新数据/);}
    assert.doesNotMatch(h.get('retained-count').textContent,/数据和部署历史保留/);
  }
  const h=harness({data:{...empty,apps:[{...done,dataDeleted:true}]}});await settle();
  assert.ok(all(h.get('retained-apps')).some(node=>node.textContent.includes('数据已清除')));
});

test('pending deletion and partial failure never claim data was retained or fully cleared',async()=>{
  for(const status of ['queued','running','recovery_required']) {
    const operation={id:'op',appId:online.id,releaseId:'release-one',status,dataPolicy:'delete'};
    const h=harness({data:{...empty,apps:[{...online,status:'attention',canOffline:false,dataDeleted:false,operation}]}});await settle();
    const content=all(h.get('online-apps')).map(node=>node.textContent).join('\n');
    assert.match(content,/删除数据/);assert.doesNotMatch(content,/数据已清除|数据已保留/);
  }
});

test('a mismatched operation policy does not falsely confirm the destructive request',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');
  acceptPosts(h,data,'retain');await h.run('submitOffline()');
  assert.equal(h.get('offline-dialog').open,true);assert.match(h.get('offline-error').textContent,/未能确认/);
  assert.equal(h.get('management-notice').textContent,'');
});

test('accepted offline requests restore focus to an enabled control',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();h.run("openOfflineDialog('app-one')");
  acceptPosts(h,data);await h.run('submitOffline()');
  assert.equal(h.context.document.activeElement,h.get('management-refresh'));
});

test('unknown data policies are neither retryable nor described as retained',async()=>{
  const operation={id:'op',appId:online.id,releaseId:'release-one',status:'succeeded',dataPolicy:'unexpected'};
  const data={...empty,apps:[{...online,status:'offline',activeReleaseId:null,operation,dataDeleted:false}]};
  const h=harness({data});await settle();const content=all(h.get('retained-apps')).map(node=>node.textContent).join('\n');
  assert.doesNotMatch(content,/数据已保留|数据已清除/);assert.match(content,/未确认/);
  h.context.badOperation={...operation,status:'recovery_required',retryable:true,retryKey:'server-key'};
  assert.equal(h.run('retryAllowed(badOperation)'),false);
});

test('an explicitly rejected delete can switch to retain only after fresh state proves no operation exists',async()=>{
  const data={...empty,apps:[online]},sessionStore=new Map(),h=harness({data,sessionStore});await settle();
  h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');
  const requests=[];
  h.context.fetch=async(url,options={})=>{
    requests.push({url,options});
    if(options.method!=='POST')return {ok:true,json:async()=>data};
    const posts=requests.filter(item=>item.options.method==='POST');
    if(posts.length===1)return {ok:false,status:409,json:async()=>({error:{code:'OFFLINE_DATA_CONFLICT',message:'数据归属冲突。'}})};
    return {ok:true,status:202,json:async()=>({id:'retained-op',appId:online.id,releaseId:'release-one',status:'queued',dataPolicy:'retain'})};
  };
  await h.run('submitOffline()');
  assert.equal(requests[1]?.url,'/api/v1/management');
  assert.equal(h.get('offline-retain').disabled,false);
  assert.equal(sessionStore.size,0);
  selectPolicy(h,'retain');await h.run('submitOffline()');
  const posts=requests.filter(item=>item.options.method==='POST');
  assert.equal(posts.length,2);
  assert.notEqual(posts[0].options.headers['Idempotency-Key'],posts[1].options.headers['Idempotency-Key']);
  assert.deepEqual(JSON.parse(posts[1].options.body),{expectedReleaseId:'release-one',dataPolicy:'retain'});
});

test('explicit rejection keeps intent locked if the verification read fails or a prior operation exists',async()=>{
  for(const exists of [false,true]) {
    const data={...empty,apps:[online]},h=harness({data});await settle();
    h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');
    const operation={id:'prior',appId:online.id,releaseId:'release-one',status:'recovery_required',dataPolicy:'retain',retryable:true,retryKey:'original-server-key'};
    h.context.fetch=async(url,options={})=>{
      if(options.method==='POST')return {ok:false,status:409,json:async()=>({error:{code:'APP_BUSY',message:'应用正忙。'}})};
      if(!exists)throw new Error('verification unavailable');
      return {ok:true,json:async()=>({...data,apps:[{...online,status:'attention',canOffline:false,operation}],offlineOperations:[operation]})};
    };
    await h.run('submitOffline()');
    assert.equal(h.get('offline-retain').disabled,true);
    if(exists) {
      assert.equal(h.get('offline-retain').checked,true);
      const posts=acceptPosts(h,data,'retain');await h.run('submitOffline()');
      assert.equal(posts[0].headers['Idempotency-Key'],'original-server-key');
      assert.deepEqual(JSON.parse(posts[0].body),{expectedReleaseId:'release-one',dataPolicy:'retain'});
    } else assert.equal(h.get('offline-delete').checked,true);
  }
});

test('idempotency conflicts never unlock an uncertain data policy',async()=>{
  const data={...empty,apps:[online]},h=harness({data});await settle();
  h.run("openOfflineDialog('app-one')");selectPolicy(h,'delete');enterDeletion(h,'删除 demo');
  h.context.fetch=async()=>({ok:false,status:409,json:async()=>({error:{code:'IDEMPOTENCY_CONFLICT',message:'请求身份冲突。'}})});
  await h.run('submitOffline()');assert.equal(h.get('offline-retain').disabled,true);
  selectPolicy(h,'retain');assert.equal(h.get('offline-delete').checked,true);
});
