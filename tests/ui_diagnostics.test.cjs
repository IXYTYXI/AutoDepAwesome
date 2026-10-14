const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.listeners={};this.value='';this.hidden=false;this.open=false;this.scrollTop=0;}
  append(...nodes){this.children.push(...nodes);for(const node of nodes)node.parentElement=this;}
  replaceChildren(...nodes){this.children=nodes;this.textContent='';this.scrollTop=0;}
  addEventListener(name, callback){this.listeners[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name] ?? null;}
  removeAttribute(name){delete this[name];}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  focus(options){this.lastFocusOptions=options;if(this.ownerDocument)this.ownerDocument.activeElement=this;}
  scrollIntoView(){this.scrollCalls=(this.scrollCalls || 0)+1;}
  set innerHTML(value){throw new Error('Unsafe HTML rendering: '+value);}
}
function all(node){return [node,...node.children.flatMap(all)];}
function harness(){
  let nextRequestKey=0;
  const elements=new Map();
  const document={listeners:{},activeElement:null,addEventListener(name,callback){this.listeners[name]=callback;}};
  const get=id=>{if(!elements.has(id)){const element=new Element(id);element.ownerDocument=document;elements.set(id,element);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>{const element=new Element(tag);element.ownerDocument=document;return element;};
  get('app-picker').append(get('app-search'),get('app-picker-toggle'),get('app-menu'));
  get('app-menu').append(get('app-options'),get('app-options-empty'));
  const context=vm.createContext({document,
    crypto:{randomUUID:()=> `request-key-${++nextRequestKey}`},Option:function(label,value){const n=new Element('option');n.textContent=label;n.value=value;return n;},
    setTimeout:()=>1,clearTimeout:()=>{},FormData:class {},console,
    fetch:async url=>({ok:true,json:async()=>url.endsWith('capabilities') ? {baseDomain:'example.test',aiAdvice:{configured:false}} : {items:[]}})});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/awesome_deploy/static/app.js'),'utf8'),context);
  return {context,get,run:code=>vm.runInContext(code,context)};
}
const job={id:'one',subdomain:'demo',status:'failed',phase:'source',createdAt:1791855000.25,repository:'https://github.com/team/demo.git',error:{code:'INVALID_MANIFEST',title:'配置文件缺失',message:'请检查 awesome.yaml。',path:'awesome.yaml',steps:['在仓库根目录创建文件。','<img src=x onerror=attack()>']}};

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
  h.context.Date=class extends Date {static now(){return now;}};
  h.context.setTimeout=callback=>{now=300000;setImmediate(callback);return 1;};
  h.context.fetch=async()=>({ok:true,json:async()=>{calls++;return calls===1 ? {status:'pending'} : {text:'unexpected'};}});
  h.run("aiConfigured=true;selectedJob='one';showJob(job)");await h.run("analyzeJob('one')");
  assert.equal(calls,1);
  assert.ok(all(h.get('current')).some(n=>n.role==='alert' && n.textContent.includes('等待超时')));
});

async function loadRecords(h,jobs=[],apps=[]){
  h.context.fetch=async url=>({ok:true,json:async()=>({items:url.endsWith('/apps') ? apps : jobs})});
  await h.run('refresh()');
}
const apps=[
  {id:'app-alpha',subdomain:'alpha',repository:'https://github.com/team/alpha.git'},
  {id:'app-beta',subdomain:'beta',repository:'https://gitlab.com/team/beta.git'},
];
function key(element,value){let prevented=false;element.listeners.keydown({key:value,preventDefault(){prevented=true;}});return prevented;}

test('recent deployment window renders all loaded records and filters reset its scroll position',async()=>{
  const h=harness();await new Promise(setImmediate);
  const jobs=Array.from({length:12},(_,i)=>({...job,id:'job-'+i,subdomain:'app'+i,status:i%2 ? 'failed':'succeeded'}));
  await loadRecords(h,jobs);
  assert.equal(h.get('history').children.length,12);
  assert.ok(h.get('history-count').textContent.includes('12'));
  h.get('history').scrollTop=220;
  h.get('history-filters').children.find(n=>n['data-filter']==='failed').listeners.click();
  assert.equal(h.get('history').children.length,6);
  assert.ok(h.get('history-count').textContent.includes('当前筛选 6 条'));
  assert.equal(h.get('history').scrollTop,0);
  h.context.selected=jobs[11];h.run("selectedJob=selected.id;renderHistory()");
  assert.equal(h.get('history').children[5]['aria-pressed'],'true');
});

test('history refresh preserves its scroll offset and focused record',async()=>{
  const h=harness();await new Promise(setImmediate);
  const jobs=Array.from({length:50},(_,i)=>({...job,id:'job-'+i,subdomain:'app'+i}));
  await loadRecords(h,jobs);
  assert.equal(h.get('history').children.length,50);
  h.get('history').scrollTop=650;
  h.get('history').children[14].focus();
  await loadRecords(h,jobs);
  assert.equal(h.get('history').children.length,50);
  assert.equal(h.get('history').scrollTop,650);
  assert.equal(h.context.document.activeElement.getAttribute('data-job-id'),'job-14');
  assert.equal(h.context.document.activeElement.lastFocusOptions.preventScroll,true);
});

test('custom application picker searches and selects with arrow keys and Enter',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  const search=h.get('app-search');search.value='BETA';search.listeners.input();
  assert.equal(search['aria-expanded'],'true');
  assert.equal(h.get('app-options').children.length,2);
  assert.equal(key(search,'ArrowDown'),true);
  assert.ok(search['aria-activedescendant']);
  assert.equal(key(search,'Enter'),true);
  assert.equal(h.get('appId').value,'app-beta');
  assert.equal(h.get('repository').value,apps[1].repository);
  assert.equal(h.get('subdomain').value,'beta');
  assert.equal(search['aria-expanded'],'false');
  assert.equal(h.get('app-menu').hidden,true);
  assert.equal(h.context.document.activeElement,search);
});

test('application picker Escape and outside pointer preserve the selected identity',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  h.run("chooseApp('app-alpha')");
  const search=h.get('app-search');search.value='missing';search.listeners.input();
  assert.equal(h.get('app-options-empty').hidden,false);
  key(search,'Escape');
  assert.equal(h.get('appId').value,'app-alpha');
  assert.ok(search.value.includes('alpha'));
  assert.equal(search['aria-activedescendant'],undefined);
  h.get('app-picker-toggle').listeners.click();
  h.context.document.listeners.pointerdown({target:h.get('repository')});
  assert.equal(search['aria-expanded'],'false');
  h.get('app-picker-toggle').listeners.click();
  assert.equal(key(search,'Tab'),false);
  assert.equal(search['aria-expanded'],'false');
});

test('IME composition leaves application selection and picker navigation unchanged',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  h.run("chooseApp('app-alpha')");
  const search=h.get('app-search');search.value='应用';search.listeners.input();
  const initialActive=search['aria-activedescendant'];
  for(const composition of [{isComposing:true},{keyCode:229}]) {
    for(const value of ['ArrowDown','ArrowUp','Enter','Escape']) {
      let prevented=false;
      search.listeners.keydown({key:value,...composition,preventDefault(){prevented=true;}});
      assert.equal(prevented,false);
      assert.equal(h.get('appId').value,'app-alpha');
      assert.equal(search['aria-expanded'],'true');
      assert.equal(search['aria-activedescendant'],initialActive);
      assert.equal(search.value,'应用');
    }
  }
});

test('application refresh preserves manual form edits and supports creating a new app',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  h.run("chooseApp('app-alpha')");h.get('repository').value='https://github.com/team/edited.git';
  await loadRecords(h,[],apps);
  assert.equal(h.get('appId').value,'app-alpha');
  assert.equal(h.get('repository').value,'https://github.com/team/edited.git');
  h.get('app-picker-toggle').listeners.click();
  h.get('app-options').children[0].listeners.click();
  assert.equal(h.get('appId').value,'');
  assert.equal(h.get('app-search').value,'创建新应用');
});

test('unbound application selection retains its identity but requires a new domain prefix',async()=>{
  const h=harness();await new Promise(setImmediate);
  const released={...apps[0],subdomain:null,lastSubdomain:'alpha',domainBound:false};
  await loadRecords(h,[],[released,apps[1]]);h.get('subdomain').value='previous';
  h.run("chooseApp('app-alpha')");
  assert.equal(h.get('appId').value,'app-alpha');
  assert.equal(h.get('repository').value,released.repository);
  assert.equal(h.get('subdomain').value,'');
  assert.ok(h.get('app-picker-help').textContent.includes('当前未绑定域名'));
  assert.ok(h.get('app-picker-help').textContent.includes('alpha'));
  assert.ok(h.get('app-picker-help').textContent.includes('空闲前缀'));
  assert.ok(h.get('app-picker-help').textContent.includes('awesome.yaml'));
  h.get('subdomain').value='fresh';h.get('subdomain').listeners.input();
  await loadRecords(h,[],[released,apps[1]]);
  const before=h.run('requestKey');h.run("chooseApp('app-alpha')");
  assert.equal(h.get('subdomain').value,'fresh');
  assert.equal(h.get('domain-prefix').textContent,'fresh');
  assert.equal(h.run('requestKey'),before);
});

test('picker searches historical prefixes and distinguishes duplicate names by app identity',async()=>{
  const h=harness();await new Promise(setImmediate);
  const released=[{...apps[0],subdomain:null,lastSubdomain:'shared',domainBound:false},{...apps[1],subdomain:null,lastSubdomain:'shared',domainBound:false}];
  await loadRecords(h,[],released);h.get('app-search').value='shared';h.get('app-search').listeners.input();
  assert.equal(h.get('app-options').children.length,3);
  const labels=h.get('app-options').children.slice(1).map(option=>all(option).map(node=>node.textContent).join(' '));
  assert.ok(labels.every(label=>label.includes('shared') && label.includes('上次使用')));
  assert.ok(labels[0].includes('app-alph'));
  assert.ok(labels[1].includes('app-beta'));
  h.get('app-options').children[2].listeners.click();assert.equal(h.get('appId').value,'app-beta');
});

test('missing domainBound remains compatible with bound applications even without an active release',async()=>{
  const h=harness();await new Promise(setImmediate);
  await loadRecords(h,[],[{...apps[0],active_release_id:null}]);h.run("chooseApp('app-alpha')");
  assert.equal(h.get('subdomain').value,'alpha');
  assert.ok(!h.get('app-picker-help').textContent.includes('已释放'));
  assert.ok(!h.get('app-picker-help').textContent.includes('未绑定'));
});

test('search cancellation preserves the deployment idempotency key and real changes replace it',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  h.run("chooseApp('app-alpha')");
  const original=h.run('requestKey');
  const search=h.get('app-search');search.value='beta';search.listeners.input();
  h.get('deploy-form').listeners.input({target:search});
  key(search,'Escape');
  assert.equal(h.run('requestKey'),original);
  h.run("chooseApp('app-alpha')");
  assert.equal(h.run('requestKey'),original);
  h.run("chooseApp('app-beta')");
  const selected=h.run('requestKey');
  assert.notEqual(selected,original);
  h.get('repository').value='https://github.com/team/edited.git';
  h.get('deploy-form').listeners.input({target:h.get('repository')});
  const edited=h.run('requestKey');
  assert.notEqual(edited,selected);
  h.run("chooseApp('app-beta')");
  assert.notEqual(h.run('requestKey'),edited);
});

test('deployment polling preserves visible history-row focus without stealing other focus',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[job]);
  const previous=h.get('history').children[0];previous.focus();
  h.context.fetch=async url=>({ok:true,json:async()=>url==='/api/v1/deployments/one' ? {...job,status:'running'} : {items:[]}});
  await h.run("watch('one')");
  const next=h.get('history').children[0];
  assert.notEqual(next,previous);
  assert.equal(h.context.document.activeElement,next);
  assert.equal(next.getAttribute('data-job-id'),'one');
  assert.equal(next.lastFocusOptions.preventScroll,true);
  assert.equal(next.scrollCalls,undefined);
  h.get('repository').focus();await h.run("watch('one')");
  assert.equal(h.context.document.activeElement,h.get('repository'));
});

test('domain preview follows typed prefixes and selected applications',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[],apps);
  assert.equal(h.get('domain-prefix').textContent,'你的前缀');
  h.get('subdomain').value='draft';h.get('subdomain').listeners.input();
  assert.equal(h.get('domain-prefix').textContent,'draft');
  h.run("chooseApp('app-beta')");
  assert.equal(h.get('domain-prefix').textContent,'beta');
  h.get('subdomain').value='';h.get('subdomain').listeners.input();
  assert.equal(h.get('domain-prefix').textContent,'你的前缀');
});

test('job dates interpret the API timestamp as Unix seconds',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[job]);
  const expected=new Date(job.createdAt*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  assert.equal(all(h.get('history')).find(n=>n.className==='job-time').textContent,expected);
  assert.ok(all(h.get('history')).some(n=>n.textContent==='部署失败'));
  assert.ok(all(h.get('history')).some(n=>n.textContent==='获取代码'));
});

test('manual history selection moves focus to progress but updates never move it again',async()=>{
  const h=harness();await new Promise(setImmediate);await loadRecords(h,[job]);
  h.context.fetch=async url=>({ok:true,json:async()=>url==='/api/v1/deployments/one' ? job : {items:[]}});
  await h.get('history').children[0].listeners.click();
  assert.equal(h.context.document.activeElement,h.get('progress-panel'));
  assert.equal(h.get('progress-panel').scrollCalls,1);
  h.get('repository').focus();
  await h.run("watch('one')");
  assert.equal(h.context.document.activeElement,h.get('repository'));
  assert.equal(h.get('progress-panel').scrollCalls,1);
});

test('job metadata is collapsed initially and keeps its expansion during updates',async()=>{
  const h=harness();await new Promise(setImmediate);h.context.job=job;h.run('showJob(job)');
  let metadata=all(h.get('current')).find(n=>n.tagName==='details' && n.className==='job-metadata');
  assert.ok(metadata);assert.equal(metadata.open,false);
  metadata.open=true;metadata.listeners.toggle();h.run('showJob(job)');
  metadata=all(h.get('current')).find(n=>n.tagName==='details' && n.className==='job-metadata');
  assert.equal(metadata.open,true);
});

test('storage capability comes from server and states remaining storage limitations',async()=>{
  const h=harness();await new Promise(setImmediate);
  h.run("renderCapabilities({baseDomain:'example.test',persistentStorage:true,storage:{driver:'local',mountPath:'/data',perService:1,updates:'recreate'},aiAdvice:{configured:false}})");
  assert.ok(h.get('storage-capability').textContent.includes('已开放'));
  assert.ok(h.get('storage-capability').textContent.includes('/data'));
  assert.ok(h.get('storage-capability').textContent.includes('硬配额'));
  assert.ok(h.get('storage-capability').textContent.includes('备份'));
  h.run("renderCapabilities({baseDomain:'example.test',persistentStorage:false,aiAdvice:{configured:false}})");
  assert.ok(h.get('storage-capability').textContent.includes('暂未开放'));
});

test('application identity remains a hidden form field and the original select is removed',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../src/awesome_deploy/static/index.html'),'utf8');
  assert.match(html,/<input[^>]+id="appId"[^>]+name="appId"[^>]+type="hidden"/);
  assert.doesNotMatch(html,/<select\b/);
  assert.match(html,/id="app-search"[^>]+role="combobox"/);
  assert.match(html,/<details[^>]+id="event-details"/);
  assert.match(html,/<div[^>]+id="history"[^>]+tabindex="0"[^>]+role="region"[^>]+aria-label="[^"]+"/);
  assert.doesNotMatch(html,/history-(?:prev|next|page|pagination)/);
});
