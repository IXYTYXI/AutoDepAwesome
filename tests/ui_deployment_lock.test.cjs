const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.listeners={};this.value='';this.hidden=false;this.scrollTop=0;}
  append(...nodes){this.children.push(...nodes);for(const node of nodes)node.parentElement=this;}
  replaceChildren(...nodes){this.children=[];this.textContent='';this.append(...nodes);}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name] ?? null;}
  removeAttribute(name){delete this[name];}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  focus(){this.ownerDocument.activeElement=this;}
  scrollIntoView(){}
}
const tick=()=>new Promise(setImmediate);
const response=(data,status=200)=>({ok:status>=200 && status<300,status,json:async()=>data});
const job=(status='queued',id='active-job')=>({id,status,phase:status==='queued'?'queued':'build',repository:'https://github.com/team/demo.git',ref:'main',subdomain:'demo'});
async function harness({storage=new Map(),handler,userId}={}) {
  const elements=new Map(),timers=new Map(),calls=[];let sequence=0;
  const document={activeElement:null,addEventListener(){}};
  const get=id=>{if(!elements.has(id)){const node=new Element(id);node.ownerDocument=document;elements.set(id,node);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>{const node=new Element(tag);node.ownerDocument=document;return node;};
  get('app-picker').append(get('app-search'),get('app-picker-toggle'),get('app-menu'));
  get('app-menu').append(get('app-options'),get('app-options-empty'));
  get('repository').value=job().repository;get('ref').value='main';get('subdomain').value='demo';
  const h={get,storage,timers,calls,status:'queued',failEvents:false};
  h.handler=handler || (async(url,options)=>{
    if(url==='/api/v1/deployments' && options.method==='POST')return response(job(h.status));
    if(url==='/api/v1/deployments/active-job')return response(job(h.status));
    if(url==='/api/v1/deployments/old-job')return response(job('succeeded','old-job'));
    if(url.endsWith('/events') && h.failEvents)return response({detail:'log timeout'},503);
    return response(url.endsWith('capabilities')?{}:{items:[]});
  });
  const context=vm.createContext({document,TextEncoder,console,
    crypto:{randomUUID:()=>`request-${++sequence}`},
    sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    FormData:class{*[Symbol.iterator](){for(const id of ['repository','ref','subdomain','appId'])yield[id,get(id).value];}},
    setTimeout:callback=>{const id=++sequence;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options={})=>{calls.push({url,options});return h.handler(url,options);}});
  if(userId) {
    h.userId=userId;
    context.window={AwesomeAuth:{session:()=>h.userId?{mode:'feishu',userId:h.userId,canDeploy:true}:null,
      onChange:callback=>{h.authChanged=callback;},ready:Promise.resolve(),fetch:context.fetch}};
    h.setUserId=id=>{h.userId=id;h.authChanged();};
  }
  h.context=context;h.run=code=>vm.runInContext(code,context);
  h.submit=()=>get('deploy-form').listeners.submit({preventDefault(){}});
  h.poll=async()=>{const pending=[...timers.values()];timers.clear();for(const callback of pending)await callback();await tick();};
  h.posts=()=>calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST');
  h.run(fs.readFileSync(path.join(__dirname,'../src/awesome_deploy/static/app.js'),'utf8'));
  await tick();return h;
}

test('deployment stays disabled after acceptance, ignores duplicate submits and unlocks only at its terminal status',async()=>{
  const h=await harness();let finish;const normal=h.handler;
  h.handler=(url,options)=>url==='/api/v1/deployments' && options.method==='POST'?new Promise(resolve=>{finish=resolve;}):normal(url,options);
  const submitting=h.submit();assert.equal(h.get('submit').disabled,true);assert.match(h.get('submit').textContent,/提交/);
  await h.submit();assert.equal(h.posts().length,1);
  finish(response(job()));await submitting;assert.equal(h.get('submit').disabled,true);assert.match(h.get('submit').textContent,/排队/);
  await h.submit();assert.equal(h.posts().length,1);
  h.status='running';await h.poll();assert.equal(h.get('submit').disabled,true);assert.match(h.get('submit').textContent,/正在部署/);
  h.status='succeeded';await h.poll();assert.equal(h.get('submit').disabled,false);assert.match(h.get('deployment-status').textContent,/成功/);
  assert.equal(h.storage.size,0);
});

test('history selection, auth synchronization and edits cannot release the active deployment lock',async()=>{
  const h=await harness();await h.submit();await h.run("watch('old-job')");
  h.get('repository').value='https://github.com/team/next.git';h.get('deploy-form').listeners.input({target:h.get('repository')});
  h.run('syncDeployPermission()');assert.equal(h.get('submit').disabled,true);await h.submit();assert.equal(h.posts().length,1);
  h.status='failed';await h.poll();assert.equal(h.get('submit').disabled,false);assert.equal(h.run('currentJob.id'),'old-job');
  assert.match(h.get('deployment-status').textContent,/失败/);
});

test('query or event failures keep polling and cannot unlock until failure is confirmed',async()=>{
  const h=await harness();await h.submit();const normal=h.handler;let unavailable=true;
  h.handler=(url,options)=>url==='/api/v1/deployments/active-job' && unavailable?response({detail:'temporary outage'},503):normal(url,options);
  await h.poll();assert.equal(h.get('submit').disabled,true);assert.ok(h.timers.size>0);
  unavailable=false;h.failEvents=true;h.status='running';await h.poll();assert.equal(h.get('submit').disabled,true);assert.ok(h.timers.size>0);
  h.status='failed';await h.poll();assert.equal(h.get('submit').disabled,false);
});

test('refresh restores this tab lock without submitting a second job',async()=>{
  const h=await harness();await h.submit();assert.ok(h.storage.size>0);
  const restored=await harness({storage:h.storage});assert.equal(restored.get('submit').disabled,true);assert.equal(restored.posts().length,0);
  restored.status='failed';await restored.poll();assert.equal(restored.get('submit').disabled,false);
});

test('recovery_required remains disabled with an explicit recovery message',async()=>{
  const h=await harness();await h.submit();h.status='recovery_required';await h.poll();
  assert.equal(h.get('submit').disabled,true);assert.match(h.get('deployment-status').textContent,/恢复|管理员/);
  assert.doesNotMatch(h.get('deployment-status').textContent,/已成功/);assert.ok(h.timers.size>0);
});

test('known validation failure unlocks while a lost response retries only the frozen payload and key',async()=>{
  const h=await harness();const normal=h.handler;
  h.handler=(url,options)=>options.method==='POST'?response({detail:'invalid repository'},422):normal(url,options);
  await h.submit();assert.equal(h.get('submit').disabled,false);assert.equal(h.storage.size,0);
  h.handler=(url,options)=>options.method==='POST'?Promise.reject(new Error('response lost')):normal(url,options);
  await h.submit();assert.equal(h.get('submit').disabled,true);assert.equal(h.get('deployment-retry').hidden,false);
  const first=h.posts().at(-1);h.get('subdomain').value='other';h.get('deploy-form').listeners.input({target:h.get('subdomain')});
  h.handler=normal;await h.get('deployment-retry').listeners.click();const retry=h.posts().at(-1);
  assert.equal(retry.options.body,first.options.body);assert.equal(retry.options.headers['Idempotency-Key'],first.options.headers['Idempotency-Key']);
  assert.equal(h.get('submit').disabled,true);assert.equal(h.get('deployment-retry').hidden,true);
});

test('unknown submission survives refresh and malformed stored state cannot submit a request',async()=>{
  const h=await harness();const normal=h.handler;
  h.handler=(url,options)=>options.method==='POST'?response({detail:'proxy failed'},502):normal(url,options);
  await h.submit();const original=h.posts()[0];const restored=await harness({storage:h.storage});
  assert.equal(restored.get('submit').disabled,true);assert.equal(restored.posts().length,0);
  await restored.get('deployment-retry').listeners.click();assert.equal(restored.posts()[0].options.body,original.options.body);
  assert.equal(restored.posts()[0].options.headers['Idempotency-Key'],original.options.headers['Idempotency-Key']);
  const [key]=h.storage.keys();const invalid=await harness({storage:new Map([[key,'not JSON']])});
  assert.equal(invalid.get('submit').disabled,false);assert.equal(invalid.posts().length,0);
});

test('an expired login retains its lock but a different authenticated account does not inherit it',async()=>{
  const h=await harness({userId:'member-one'});await h.submit();
  h.setUserId(null);assert.equal(h.get('submit').disabled,true);assert.ok(h.storage.size>0);
  h.setUserId('member-one');assert.equal(h.get('submit').disabled,true);
  h.setUserId('member-two');assert.equal(h.get('submit').disabled,false);assert.equal(h.storage.size,0);
  await h.poll();assert.equal(h.get('submit').disabled,false);
});

test('a late response from the previous account cannot reinstall its deployment lock',async()=>{
  const h=await harness({userId:'member-one'});let finish;const normal=h.handler;
  h.handler=(url,options)=>options.method==='POST'?new Promise(resolve=>{finish=resolve;}):normal(url,options);
  const submitting=h.submit();h.setUserId('member-two');finish(response(job()));await submitting;
  assert.equal(h.get('submit').disabled,false);assert.equal(h.storage.size,0);
});
