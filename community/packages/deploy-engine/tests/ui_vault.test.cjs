const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../src/awesome_deploy/static');
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.textContent='';this.value='';this.hidden=false;this.open=false;this.disabled=false;this.scrollTop=0;}
  append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this.textContent='';this.append(...nodes);}
  setAttribute(name,value){this[name]=String(value);} getAttribute(name){return this[name]??null;} removeAttribute(name){delete this[name];}
  addEventListener(name,cb){this.listeners[name]=cb;} contains(node){return node===this||this.children.some(n=>n.contains(node));}
  focus(){if(!this.disabled)this.ownerDocument.activeElement=this;} showModal(){this.open=true;} close(){this.open=false;this.listeners.close?.();}
  scrollIntoView(){} set innerHTML(_){throw Error('Unsafe innerHTML');}
}
const all=node=>[node,...node.children.flatMap(all)];
const content=node=>all(node).map(n=>n.textContent).join(' ');
const app={id:'app-one',subdomain:'chatstd',permissions:{deploy:true}};
const runtime={name:'MODEL_API_KEY',kind:'runtime_secret',current_version:'v1',data_generation:null,versions:[{version:'v1',status:'active',created_at:1792022400},{version:'v0',status:'active',created_at:1791936000}]};
const master={name:'MASTER_KEY',kind:'platform_managed_master',current_version:'m1',data_generation:'g1',versions:[{version:'m1',status:'active'}]};
const metadata={appId:app.id,items:[runtime,master],operations:[],can_rotate:true,can_revoke:true};
const session={mode:'feishu',canDeploy:true,role:'member',capabilities:{vault_enabled:true}};
function harness({data=metadata,auth=session}={}){
  const nodes=new Map(),calls=[],timers=[];const doc={activeElement:null};
  const get=id=>{if(!nodes.has(id)){const node=new Element(id);node.ownerDocument=doc;nodes.set(id,node);}return nodes.get(id);};
  doc.getElementById=get;doc.createElement=tag=>{const node=new Element(tag);node.ownerDocument=doc;return node;};
  const window={listeners:{},addEventListener(name,cb){this.listeners[name]=cb;},AwesomeAuth:{session:()=>auth,onChange:fn=>{window.authChanged=fn;},ready:Promise.resolve(auth)}};
  window.AwesomeAuth.fetch=async(url,options={})=>{calls.push({url,options});return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(data))};};
  const context=vm.createContext({window,document:doc,console,Intl,TextEncoder,AbortController,URL,Date,setTimeout:(cb,delay)=>{timers.push({cb,delay});return timers.length;},clearTimeout:()=>{}});
  vm.runInContext(fs.readFileSync(path.join(root,'vault.js'),'utf8'),context);
  return {get,calls,timers,context,window,api:window.AwesomeVault,fetch:fn=>{window.AwesomeAuth.fetch=async(url,options={})=>{calls.push({url,options});return fn(url,options);};}};
}
const settle=()=>new Promise(setImmediate);
const button=(node,label)=>all(node).find(n=>n.tagName==='button'&&n.textContent===label);
async function open(h){await h.api.open(app);await settle();}
function startRotate(h){button(h.get('vault-items'),'轮换密钥').listeners.click();}
function startRevoke(h,version='v1'){all(h.get('vault-items')).find(n=>n.getAttribute('data-vault-action')===`revoke:MODEL_API_KEY:${version}`).listeners.click();}
const submit=h=>h.get('vault-form').listeners.submit({preventDefault(){}});

test('secret management entry is enabled only by explicit capability, deploy access and session',()=>{
  for(const auth of [null,{...session,capabilities:{}},{...session,canDeploy:false},{...session,mode:'basic',capabilities:{}}]){
    const h=harness({auth});assert.equal(h.api.canOpen(app),false);
  }
  const h=harness();assert.equal(h.api.canOpen(app),true);assert.equal(h.api.canOpen({...app,permissions:{deploy:false}}),false);assert.equal(h.api.canOpen({...app,permissions:{deploy:true,manageSecrets:false}}),false);
});
test('management markup gives modal, scrolling regions, meaningful labels and no native select',()=>{
  const html=fs.readFileSync(path.join(root,'manage.html'),'utf8');
  assert.match(html,/<dialog[^>]+id="vault-dialog"[^>]+aria-labelledby="vault-title"/);
  assert.match(html,/id="vault-items"[^>]+tabindex="0"[^>]+role="region"/);
  assert.match(html,/for="vault-value"/);assert.match(html,/name="secretValue"/);
  assert.match(html,/id="vault-value"[^>]+autocomplete="new-password"/);
  assert.doesNotMatch(html,/<select\b/);
});
test('metadata renders no values, immutable master protection, long identifiers as text',async()=>{
  const unsafe='<img src=x onerror=alert(1)>';
  const h=harness({data:{...metadata,items:[{...runtime,name:unsafe},master]}});await open(h);
  assert.equal(h.get('vault-dialog').open,true);
  assert.match(content(h.get('vault-items')),/专项迁移/);assert.match(content(h.get('vault-items')),/MASTER_KEY/);
  assert.ok(all(h.get('vault-items')).some(n=>n.textContent===unsafe));
  const masterActions=all(h.get('vault-items')).filter(n=>n.tagName==='button'&&(n.getAttribute('data-vault-action')||'').includes('MASTER_KEY'));
  assert.ok(masterActions.length>0&&masterActions.every(n=>n.disabled));
  assert.equal(h.get('vault-value').value,'');assert.equal(h.calls.length,1);
});
test('only permitted actions display, and disabled owner operation cannot be opened',async()=>{
  const h=harness({data:{...metadata,can_revoke:false}});await open(h);
  assert.ok(!all(h.get('vault-items')).some(n=>n.getAttribute('data-vault-action')?.startsWith('revoke:')));
  assert.ok(button(h.get('vault-items'),'轮换密钥'));
});
test('rotate sends exact value and expected version once and clears value after success',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='  private-value  ';
  let finish;h.fetch((url,options)=>options.method==='POST'?new Promise(resolve=>{finish=()=>resolve({ok:true,status:200,json:async()=>({name:'MODEL_API_KEY',current_version:'v2'})});}):Promise.resolve({ok:true,status:200,json:async()=>metadata}));
  const first=submit(h);await settle();await submit(h);
  const posts=h.calls.filter(c=>c.options.method==='POST');assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].options.body),{value:'  private-value  ',expected_current_version:'v1'});
  assert.equal(h.get('vault-submit').disabled,true);finish();await first;
  assert.equal(h.get('vault-value').value,'');assert.equal(h.get('vault-editor').hidden,true);
  assert.match(h.get('vault-message').textContent,/新部署/);assert.match(h.get('vault-message').textContent,/不会自动/);
});
test('form validation explains empty input, preserves exact value and allows paste',async()=>{
  const h=harness();await open(h);startRotate(h);await submit(h);
  assert.match(h.get('vault-value-error').textContent,/输入/);assert.equal(h.context.document.activeElement,h.get('vault-value'));
  assert.equal(h.get('vault-value').listeners.paste,undefined);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
});
test('CAS conflict refreshes metadata but requires explicit acknowledgment before resubmission',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='replacement';
  const newer={...metadata,items:[{...runtime,current_version:'v2'},master]};
  h.fetch(async(_url,options)=>options.method==='POST'?{ok:false,status:409,json:async()=>({error:{code:'SECRET_VERSION_CONFLICT',message:'版本已改变'}})}:{ok:true,status:200,json:async()=>newer});
  await submit(h);assert.equal(h.get('vault-value').value,'replacement');assert.equal(h.get('vault-submit').disabled,true);
  assert.match(h.get('vault-conflict-text').textContent,/v2/);assert.equal(h.get('vault-reconfirm').hidden,false);
  h.get('vault-reconfirm').listeners.click();assert.equal(h.get('vault-submit').disabled,false);
  h.fetch(async(_url,options)=>options.method==='POST'?{ok:true,status:200,json:async()=>({name:'MODEL_API_KEY',current_version:'v3'})}:{ok:true,status:200,json:async()=>newer});
  await submit(h);const posts=h.calls.filter(c=>c.options.method==='POST');assert.equal(posts.length,2);assert.equal(JSON.parse(posts[1].options.body).expected_current_version,'v2');
});
test('unknown write outcome never retries and requires fresh metadata plus deliberate confirmation',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='replacement';
  h.fetch(async(_url,options)=>{if(options.method==='POST')throw Error('offline');return {ok:true,status:200,json:async()=>metadata};});
  await submit(h);await submit(h);
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
  assert.equal(h.get('vault-submit').disabled,true);assert.match(h.get('vault-form-error').textContent,/无法确认/);
  assert.match(h.get('vault-conflict-text').textContent,/核对/);
});
test('reauth-required shows in-dialog login route and retains value without automatic resubmit',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='replacement';
  h.fetch(async()=>({ok:false,status:403,json:async()=>({error:{code:'AUTH_REAUTH_REQUIRED',message:'近期认证'}})}));
  await submit(h);assert.equal(h.get('vault-reauth').hidden,false);assert.equal(h.get('vault-value').value,'replacement');
  assert.equal(h.get('vault-reauth-link').href,'/login');
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
});
test('revoke uses selected immutable version, required reason and asynchronous status',async()=>{
  const h=harness();await open(h);startRevoke(h,'v0');await submit(h);
  assert.match(h.get('vault-reason-error').textContent,/原因/);
  h.get('vault-reason').value='怀疑外泄';
  const op={id:'revocation-one',app_id:app.id,name:runtime.name,version:'v0',status:'queued'};
  h.fetch(async(_url,options)=>options.method==='POST'?{ok:true,status:202,json:async()=>op}:{ok:true,status:200,json:async()=>({...metadata,operations:[op]})});
  await submit(h);const post=h.calls.find(c=>c.options.method==='POST');assert.ok(post.url.endsWith('/MODEL_API_KEY/revoke'));
  assert.deepEqual(JSON.parse(post.options.body),{version:'v0',expected_current_version:'v1',reason:'怀疑外泄'});
  assert.match(h.get('vault-message').textContent,/已受理/);assert.doesNotMatch(h.get('vault-message').textContent,/全部停用|吊销完成/);
  assert.match(content(h.get('vault-operations')),/排队/);
});
test('revocation operations distinguish running, manual intervention and verified completion',async()=>{
  const h=harness({data:{...metadata,operations:['queued','running','recovery_required','succeeded'].map((status,i)=>({id:'op'+i,name:runtime.name,version:'v'+i,status}))}});await open(h);
  const text=content(h.get('vault-operations'));assert.match(text,/排队/);assert.match(text,/处理中/);assert.match(text,/人工处理/);assert.match(text,/停用已确认/);
  assert.match(fs.readFileSync(path.join(root,'manage.html'),'utf8'),/外部供应商的凭证仍需在供应商处撤销/);
});
test('closing clears secrets and restores invoking action focus; escape is blocked while submitting',async()=>{
  const h=harness();h.get('launcher').focus();await open(h);startRotate(h);h.get('vault-value').value='private-value';
  h.get('vault-close').listeners.click();assert.equal(h.get('vault-value').value,'');assert.equal(h.context.document.activeElement,h.get('launcher'));
  await open(h);startRotate(h);h.get('vault-value').value='x';let finish;
  h.fetch((_url,options)=>options.method==='POST'?new Promise(resolve=>{finish=()=>resolve({ok:true,status:200,json:async()=>({name:runtime.name,current_version:'v2'})});}):Promise.resolve({ok:true,status:200,json:async()=>metadata}));
  const pending=submit(h);await settle();let prevented=false;h.get('vault-dialog').listeners.cancel({preventDefault(){prevented=true;}});assert.equal(prevented,true);finish();await pending;
});
test('empty and failed reads remain actionable and never show stale actions as available',async()=>{
  const h=harness({data:{...metadata,items:[]}});await open(h);assert.match(content(h.get('vault-items')),/尚未/);
  h.fetch(async()=>({ok:false,status:404,json:async()=>({error:{message:'not enabled'}})}));await h.get('vault-refresh').listeners.click();
  assert.match(h.get('vault-error').textContent,/未启用/);assert.equal(h.get('vault-refresh').disabled,false);
});
test('session revocation clears secret input and blocks mutations',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='private-value';
  h.window.authChanged(null);assert.equal(h.get('vault-value').value,'');assert.equal(h.get('vault-dialog').open,false);
});
test('scoped styles bound long lists, preserve focus and reduced motion',()=>{
  const css=fs.readFileSync(path.join(root,'vault.css'),'utf8');assert.match(css,/overflow-wrap:\s*anywhere/);assert.match(css,/overscroll-behavior:\s*contain/);assert.match(css,/:focus-visible/);assert.match(css,/prefers-reduced-motion/);assert.doesNotMatch(css,/transition:\s*all/);
});

test('unknown secret kinds and revoked versions never permit a mutation',async()=>{
  const h=harness({data:{...metadata,items:[{...runtime,kind:'unconfirmed'},{...runtime,name:'REVOKED_KEY',versions:[{version:'v1',status:'revoked'}]}]}});await open(h);
  const protectedActions=all(h.get('vault-items')).filter(n=>n.tagName==='button'&&(n.getAttribute('data-vault-action')?.includes('MODEL_API_KEY')||n.getAttribute('data-vault-action')?.startsWith('revoke:')));
  assert.ok(protectedActions.every(n=>n.disabled));
});
test('permissions updated during an open edit prevent a new write',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='value';
  h.fetch(async()=>({ok:true,status:200,json:async()=>({...metadata,can_rotate:false})}));
  await h.get('vault-refresh').listeners.click();await submit(h);
  assert.equal(h.get('vault-submit').disabled,true);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
});
test('malformed successful response is uncertain rather than falsely reporting completion',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='value';
  h.fetch(async(_url,options)=>({ok:true,status:200,json:async()=>options.method==='POST'?{success:true}:metadata}));
  await submit(h);assert.equal(h.get('vault-editor').hidden,false);assert.equal(h.get('vault-submit').disabled,true);assert.match(h.get('vault-form-error').textContent,/无法确认/);
});

test('a hanging write times out without replaying and keeps explicit verification required',async()=>{
  const h=harness();await open(h);startRotate(h);h.get('vault-value').value='value';
  h.fetch((_url,options)=>options.method==='POST'?new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')))):Promise.resolve({ok:true,status:200,json:async()=>metadata}));
  const pending=submit(h);await settle();h.timers.filter(timer=>timer.delay===30000).at(-1).cb();await pending;
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);assert.equal(h.get('vault-submit').disabled,true);assert.match(h.get('vault-form-error').textContent,/无法确认/);
});
test('version refresh preserves nested scroll and moves focus away from newly disabled actions',async()=>{
  const h=harness();await open(h);const list=all(h.get('vault-items')).find(n=>n.getAttribute('data-vault-scroll')==='MODEL_API_KEY');list.scrollTop=123;
  all(h.get('vault-items')).find(n=>n.getAttribute('data-vault-action')==='revoke:MODEL_API_KEY:v1').focus();
  h.fetch(async()=>({ok:true,status:200,json:async()=>({...metadata,items:[{...runtime,versions:[{version:'v1',status:'revoked'}]},master]})}));
  await h.get('vault-refresh').listeners.click();
  assert.equal(all(h.get('vault-items')).find(n=>n.getAttribute('data-vault-scroll')==='MODEL_API_KEY').scrollTop,123);
  assert.equal(h.context.document.activeElement,h.get('vault-close'));
});

const retryOperation={id:'op-retry',app_id:app.id,name:runtime.name,version:'v0',status:'recovery_required'};
const retryMetadata={...metadata,items:[{...runtime,versions:[runtime.versions[0],{version:'v0',status:'revoked'}]},master],operations:[retryOperation]};
test('revocation retry reuses the fixed operation target after confirmation and required reason',async()=>{
  const h=harness({data:retryMetadata});await open(h);
  const retry=button(h.get('vault-operations'),'重试停用核验');assert.ok(retry);retry.focus();retry.listeners.click();
  assert.match(h.get('vault-editor-description').textContent,/原范围/);assert.match(h.get('vault-editor-description').textContent,/不会扩大/);
  assert.match(h.get('vault-editor-target').textContent,/v0/);assert.match(h.get('vault-editor-target').textContent,/op-retry/);
  await submit(h);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  h.get('vault-reason').value='已排除停止实例时的故障';
  h.fetch(async(_url,options)=>({ok:true,status:options.method==='POST'?202:200,json:async()=>options.method==='POST'?{...retryOperation,status:'queued'}:{...retryMetadata,operations:[{...retryOperation,status:'queued'}]}}));
  await submit(h);const posts=h.calls.filter(c=>c.options.method==='POST');assert.equal(posts.length,1);
  assert.ok(posts[0].url.endsWith('/MODEL_API_KEY/revoke'));
  assert.deepEqual(JSON.parse(posts[0].options.body),{version:'v0',expected_current_version:'v1',reason:'已排除停止实例时的故障'});
  assert.match(h.get('vault-message').textContent,/原范围/);assert.match(content(h.get('vault-operations')),/排队/);
  assert.equal(button(h.get('vault-operations'),'重试停用核验'),undefined);
  assert.equal(h.context.document.activeElement,h.get('vault-close'));
});
test('retry is not offered to maintainer, protected master, another app or non-recovery operations',async()=>{
  for(const data of [{...retryMetadata,can_revoke:false},{...retryMetadata,items:[{...retryMetadata.items[0],kind:'platform_managed_master'}]},{...retryMetadata,operations:[{...retryOperation,app_id:'other-app'}]},...['queued','running','succeeded','failed'].map(status=>({...retryMetadata,operations:[{...retryOperation,status}]}))]){
    const h=harness({data});await open(h);assert.equal(button(h.get('vault-operations'),'重试停用核验'),undefined);
  }
});
test('retry CAS and uncertain results require fresh scope confirmation without automatic replay',async()=>{
  for(const uncertain of [false,true]){
    const h=harness({data:retryMetadata});await open(h);button(h.get('vault-operations'),'重试停用核验').listeners.click();h.get('vault-reason').value='已修复';
    const updated={...retryMetadata,items:[{...retryMetadata.items[0],current_version:'v2'},master]};
    h.fetch(async(_url,options)=>options.method==='POST'?{ok:false,status:uncertain?502:409,json:async()=>({error:{code:uncertain?'NETWORK':'SECRET_VERSION_CONFLICT',message:'请核对'}})}:{ok:true,status:200,json:async()=>updated});
    await submit(h);await submit(h);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);assert.equal(h.get('vault-submit').disabled,true);
    assert.equal(h.get('vault-reason').value,'已修复');assert.equal(h.get('vault-reconfirm').hidden,false);
    assert.match(h.get('vault-conflict-text').textContent,/原范围/);
    h.get('vault-reconfirm').listeners.click();assert.equal(h.get('vault-submit').disabled,false);
  }
});
test('retry editor cannot resubmit when operation is already running or permission is removed',async()=>{
  for(const updated of [{...retryMetadata,operations:[{...retryOperation,status:'running'}]},{...retryMetadata,can_revoke:false}]){
    const h=harness({data:retryMetadata});await open(h);button(h.get('vault-operations'),'重试停用核验').listeners.click();h.get('vault-reason').value='已修复';
    h.fetch(async()=>({ok:true,status:200,json:async()=>updated}));await h.get('vault-refresh').listeners.click();await submit(h);
    assert.equal(h.get('vault-submit').disabled,true);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
  }
});
