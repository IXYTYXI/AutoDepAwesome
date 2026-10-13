const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.listeners={};this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this.textContent='';}
  addEventListener(name, callback){this.listeners[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  set innerHTML(value){throw new Error('Unsafe HTML rendering: '+value);}
}
function all(node){return [node,...node.children.flatMap(all)];}
function harness(){
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  const context=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag)},
    crypto:{randomUUID:()=> 'request-key'},Option:function(label,value){const n=new Element('option');n.textContent=label;n.value=value;return n;},
    setTimeout:()=>1,clearTimeout:()=>{},FormData:class {},console,
    fetch:async url=>({ok:true,json:async()=>url.endsWith('capabilities') ? {baseDomain:'example.test',aiAdvice:{configured:false}} : {items:[]}})});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/awesome_deploy/static/app.js'),'utf8'),context);
  return {context,get,run:code=>vm.runInContext(code,context)};
}
const job={id:'one',subdomain:'demo',status:'failed',phase:'source',error:{code:'INVALID_MANIFEST',title:'配置文件缺失',message:'请检查 awesome.yaml。',path:'awesome.yaml',steps:['在仓库根目录创建文件。','<img src=x onerror=attack()>']}};

test('Chinese steps render as text; unconfigured AI remains disabled',async()=>{
  const h=harness();await new Promise(setImmediate);
  h.context.job=job;h.run('showJob(job)');
  const nodes=all(h.get('current'));
  assert.ok(nodes.some(n=>n.tagName==='li' && n.textContent==='在仓库根目录创建文件。'));
  assert.ok(nodes.some(n=>n.tagName==='li' && n.textContent==='<img src=x onerror=attack()>'));
  assert.ok(nodes.some(n=>n.tagName==='button' && n.textContent==='AI 分析修改建议' && n.disabled));
  assert.ok(nodes.some(n=>n.textContent.includes('AI 分析尚未配置')));
});

test('late AI result never overwrites a newly selected deployment',async()=>{
  const h=harness();await new Promise(setImmediate);h.context.job=job;
  let complete;
  h.context.fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{complete=resolve;})});
  h.run("aiConfigured=true;selectedJob='one';showJob(job)");
  const pending=h.run("analyzeJob('one')");await new Promise(setImmediate);
  assert.ok(all(h.get('current')).some(n=>n.tagName==='button' && n.disabled && n.textContent==='正在分析…'));
  h.context.other={...job,id:'two',subdomain:'other'};h.run("selectedJob='two';showJob(other)");
  complete({text:'<script>attack()</script>'});await pending;
  assert.ok(all(h.get('current')).some(n=>n.textContent.startsWith('other ·')));
  assert.ok(!all(h.get('current')).some(n=>n.textContent==='<script>attack()</script>'));
  h.run("selectedJob='one';showJob(job)");
  assert.ok(all(h.get('current')).some(n=>n.tagName==='pre' && n.textContent==='<script>attack()</script>'));
});

test('AI error keeps built-in advice and enables retry',async()=>{
  const h=harness();await new Promise(setImmediate);h.context.job=job;
  h.context.fetch=async()=>({ok:false,json:async()=>({error:{message:'AI 响应超时。',steps:['稍后重试。']}})});
  h.run("aiConfigured=true;selectedJob='one';showJob(job)");await h.run("analyzeJob('one')");
  const nodes=all(h.get('current'));
  assert.ok(nodes.some(n=>n.tagName==='li' && n.textContent==='在仓库根目录创建文件。'));
  assert.ok(nodes.some(n=>n.role==='alert' && n.textContent.includes('AI 响应超时。')));
  assert.ok(nodes.some(n=>n.tagName==='button' && n.textContent==='重试 AI 分析' && !n.disabled));
});

test('pending AI response is polled until advice arrives',async()=>{
  const h=harness();await new Promise(setImmediate);h.context.job=job;
  let calls=0;const bodies=[];
  h.context.setTimeout=callback=>{setImmediate(callback);return 1;};
  h.context.fetch=async(url,options)=>{bodies.push(JSON.parse(options.body));return {ok:true,json:async()=>++calls===1 ? {status:'pending'} : {text:'核对配置文件。'}};};
  h.run("aiConfigured=true;selectedJob='one';showJob(job)");await h.run("analyzeJob('one')");
  assert.equal(calls,2);assert.deepEqual(bodies,[{},{poll:true}]);
  assert.ok(all(h.get('current')).some(n=>n.tagName==='pre' && n.textContent==='核对配置文件。'));
});

test('background wake after deadline cannot send another AI request',async()=>{
  const h=harness();await new Promise(setImmediate);h.context.job=job;
  let calls=0,now=0;
  h.context.Date={now:()=>now};
  h.context.setTimeout=callback=>{now=300000;setImmediate(callback);return 1;};
  h.context.fetch=async()=>({ok:true,json:async()=>{calls++;return calls===1 ? {status:'pending'} : {text:'unexpected'};}});
  h.run("aiConfigured=true;selectedJob='one';showJob(job)");await h.run("analyzeJob('one')");
  assert.equal(calls,1);
  assert.ok(all(h.get('current')).some(n=>n.role==='alert' && n.textContent.includes('等待超时')));
});
