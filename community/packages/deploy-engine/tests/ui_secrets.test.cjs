const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.listeners={};this.value='';this.hidden=false;this.open=false;this.scrollTop=0;}
  append(...nodes){for(const node of nodes){if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(item=>item!==node);this.children.push(node);node.parentElement=this;}}
  replaceChildren(...nodes){for(const node of this.children)node.parentElement=null;this.children=[];this.textContent='';this.append(...nodes);}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name] ?? null;}
  removeAttribute(name){delete this[name];}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  focus(){this.ownerDocument.activeElement=this;}
  scrollIntoView(){}
  set innerHTML(value){throw new Error('Unsafe HTML rendering');}
}
const all=node=>[node,...node.children.flatMap(all)];
const tick=()=>new Promise(setImmediate);
const response=(data,ok=true)=>({ok,json:async()=>data});
const job={id:'failed-one',repository:'https://github.com/team/project.git',ref:'codex/project',subdomain:'demo',appId:null,commit:'a'.repeat(40),status:'failed',phase:'freeze',error:{code:'SECRET_NOT_FOUND',title:'缺少可用的秘密配置',message:'请配置所需秘密。',steps:['补齐配置后继续。']}};
const requirements=(id=job.id)=>({jobId:id,repository:job.repository,subdomain:job.subdomain,canGenerate:true,items:[
  {name:'password',managed:false,uses:[{service:'web',environmentVariable:'BOOTSTRAP_PASSWORD'}],state:'missing'},
  {name:'MASTER_KEY',managed:true,uses:[{service:'web',environmentVariable:'MASTER_KEY'}],state:'missing'},
]});
async function harness(){
  let next=0;const elements=new Map();const calls=[];const copied=[];
  const document={activeElement:null,addEventListener(){}};
  const get=id=>{if(!elements.has(id)){const node=new Element(id);node.ownerDocument=document;elements.set(id,node);}return elements.get(id);};
  document.getElementById=get;document.createElement=tag=>{const node=new Element(tag);node.ownerDocument=document;return node;};
  get('app-picker').append(get('app-search'),get('app-picker-toggle'),get('app-menu'));
  get('app-menu').append(get('app-options'),get('app-options-empty'));
  const h={calls,copied,get};
  h.handler=async url=>response(url.endsWith('capabilities') ? {aiAdvice:{configured:true}} : {items:[]});
  const context=vm.createContext({document,Uint8Array,TextEncoder,
    crypto:{randomUUID:()=>`key-${++next}`,getRandomValues(){throw new Error('Managed keys must only be generated on the server');}},
    navigator:{clipboard:{writeText:async value=>copied.push(value)}},
    localStorage:{setItem(){throw new Error('Secret must not enter storage');}},
    sessionStorage:{setItem(){throw new Error('Secret must not enter storage');}},
    setTimeout:()=>1,clearTimeout(){},FormData:class {},console,
    fetch:async(url,options={})=>{calls.push({url,options});return h.handler(url,options);}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/awesome_deploy/static/app.js'),'utf8'),context);
  h.context=context;h.run=code=>vm.runInContext(code,context);
  h.nodes=()=>all(get('current'));
  h.input=name=>h.nodes().find(node=>node.tagName==='input' && node['data-secret-name']===name);
  h.button=label=>h.nodes().find(node=>node.tagName==='button' && node.textContent===label);
  h.show=async(value=job)=>{context.testJob=value;h.run('selectedJob=testJob.id;showJob(testJob)');await tick();};
  await tick();calls.length=0;return h;
}
function secretRouter(data,extra=()=>response({items:[]})){
  return (url,options)=>url.endsWith('/secrets') && !options.method ? response(data) : extra(url,options);
}

test('missing fields are actionable; configured and unavailable values are never inputs',async()=>{
  const h=await harness();const data=requirements();
  data.items.push({name:'api-token',managed:false,uses:[{service:'web',environmentVariable:'OPENAI_API_KEY'}],state:'configured'},
    {name:'old-key',managed:false,uses:[{service:'web',environmentVariable:'OLD_KEY'}],state:'unavailable'});
  h.handler=secretRouter(data);await h.show();
  assert.equal(h.input('password')?.type,'password');assert.equal(h.input('MASTER_KEY'),undefined);
  assert.equal(h.input('api-token'),undefined);assert.equal(h.input('old-key'),undefined);
  assert.ok(h.nodes().some(node=>node.textContent.includes('部署服务器')));
  assert.ok(h.nodes().some(node=>node.textContent.includes('恢复原值')));
  assert.equal(h.button('保存并继续部署')?.disabled,true);
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
});

test('login password is user entered while managed keys have no fields, generation or retention confirmation',async()=>{
  const h=await harness();const data=requirements();data.items.push({name:'provider',managed:false,uses:[{service:'web',environmentVariable:'OPENAI_API_KEY'}],state:'missing'});
  h.handler=secretRouter(data);await h.show();
  assert.equal(h.input('password')?.value,'');assert.equal(h.input('MASTER_KEY'),undefined);
  assert.equal(h.input('provider')?.type,'password');
  assert.ok(h.nodes().some(node=>node.textContent==='登录密码'));
  assert.ok(h.nodes().some(node=>node.textContent.includes('首次登录')));
  assert.ok(h.nodes().some(node=>node.textContent.includes('由平台自动生成并保存')));
  assert.equal(h.nodes().some(node=>node.type==='checkbox'),false);
  assert.equal(h.nodes().some(node=>node.textContent==='生成随机值' || node.textContent==='复制'),false);
  const row=h.input('password').parentElement.parentElement;
  await all(row).find(node=>node.textContent==='显示').listeners.click();assert.equal(h.input('password').type,'text');
  await all(row).find(node=>node.textContent==='隐藏').listeners.click();assert.equal(h.input('password').type,'password');
  assert.ok(h.nodes().some(node=>node.textContent.includes('外部 API Key')));
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
});

test('same-task refresh, AI and capabilities retain the same draft input nodes',async()=>{
  const h=await harness();h.handler=secretRouter(requirements(),url=>response(url.endsWith('/analyze') ? {text:'建议'} : {items:[]}));await h.show();
  const input=h.input('password');assert.ok(input);input.value='typed-password';input.focus();
  h.run('renderCapabilities({aiAdvice:{configured:true}})');await h.run("analyzeJob('failed-one')");await h.run('refresh()');await h.show();
  assert.equal(h.input('password'),input);assert.equal(input.value,'typed-password');
  assert.equal(h.calls.filter(call=>call.url.endsWith('/secrets')).length,1);
  const analysis=h.calls.find(call=>call.url.endsWith('/analyze'));assert.equal(analysis.options.body,'{}');
});

test('switching tasks clears drafts immediately and ignores late requirement responses',async()=>{
  const h=await harness();h.handler=secretRouter(requirements());await h.show();
  const input=h.input('password');assert.ok(input);input.value='discard-me';
  let finish;h.handler=async()=>new Promise(resolve=>{finish=resolve;});
  const waiting=h.run("watch('other')");await tick();assert.equal(input.value,'');
  finish(response({...job,id:'other',error:null}));await tick();
  // The pending events request is deliberately not awaited; it cannot restore the editor.
  assert.equal(h.input('password'),undefined);void waiting;
  const second=await harness();let complete;
  second.handler=()=>new Promise(resolve=>{complete=resolve;});await second.show();
  await second.show({...job,id:'next',error:null});complete(response(requirements()));await tick();
  assert.equal(second.input('password'),undefined);
});

test('save precedes a fresh deployment with the failed task identity and clears plaintext',async()=>{
  const h=await harness();const original={...job,appId:'original-app'};
  h.handler=secretRouter(requirements(),(url,options)=>{
    if(url.endsWith('/secrets'))return response({...requirements(),items:requirements().items.map(item=>({...item,state:'configured'}))});
    if(url==='/api/v1/deployments' && options.method==='POST')return response({...job,id:'new-job',status:'queued',error:null});
    if(url==='/api/v1/deployments/new-job')return response({...job,id:'new-job',status:'queued',error:null});
    return response({items:[]});
  });await h.show(original);
  const password=h.input('password');assert.ok(password);password.value='private-password';
  h.get('repository').value='https://github.com/other/wrong.git';h.get('ref').value='wrong';
  await h.button('保存并继续部署').listeners.click();
  const writes=h.calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,2);
  assert.ok(writes[0].url.endsWith('/secrets'));
  assert.deepEqual(JSON.parse(writes[0].options.body),{values:{password:'private-password'}});
  assert.deepEqual(JSON.parse(writes[1].options.body),{repository:job.repository,ref:job.ref,subdomain:job.subdomain,appId:'original-app'});
  assert.equal(password.value,'');assert.equal(h.input('MASTER_KEY'),undefined);
  assert.equal(h.get('submit').disabled,true);
  await h.get('deploy-form').listeners.submit({preventDefault(){}});
  assert.equal(h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length,1);
});

test('a failed deployment submission retries the same key without saving or generating again',async()=>{
  const h=await harness();let attempts=0;
  h.handler=secretRouter(requirements(),(url,options)=>{
    if(url.endsWith('/secrets'))return response({...requirements(),items:requirements().items.map(item=>({...item,state:'configured'}))});
    if(url==='/api/v1/deployments' && options.method==='POST'){attempts++;return response({error:{message:'暂时不能提交。'}},false);}
    return response({items:[]});
  });await h.show();assert.ok(h.input('password'));h.input('password').value='password';
  await h.button('保存并继续部署').listeners.click();assert.equal(attempts,1);
  assert.ok(h.nodes().some(node=>node.textContent.includes('已保存')));
  await h.button('继续部署').listeners.click();assert.equal(attempts,2);
  const saves=h.calls.filter(call=>call.url.endsWith('/secrets') && call.options.method==='POST');assert.equal(saves.length,1);
  const deploys=h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST');
  assert.equal(deploys[0].options.headers['Idempotency-Key'],deploys[1].options.headers['Idempotency-Key']);
});

test('a lost continuation response retains its request identity after leaving and returning',async()=>{
  const h=await harness();let saved=false;
  h.handler=(url,options)=>{
    if(url.endsWith('/secrets')){
      if(options.method==='POST')saved=true;
      return response({...requirements(),items:requirements().items.map(item=>({...item,state:saved ? 'configured' : 'missing'}))});
    }
    if(url==='/api/v1/deployments' && options.method==='POST')throw new Error('Response lost after acceptance');
    return response({items:[]});
  };
  await h.show();h.input('password').value='password';
  await h.button('保存并继续部署').listeners.click();
  await h.show({...job,id:'other',error:null});await h.show();
  assert.equal(h.input('password'),undefined);
  await h.button('继续部署').listeners.click();
  const deploys=h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST');
  assert.equal(deploys.length,2);
  assert.equal(deploys[0].options.headers['Idempotency-Key'],deploys[1].options.headers['Idempotency-Key']);
  assert.equal(h.calls.filter(call=>call.url.endsWith('/secrets') && call.options.method==='POST').length,1);
});

test('continuation identity survives a page reload and differs for a new failed job',async()=>{
  const configured=id=>({...requirements(id),items:requirements(id).items.map(item=>({...item,state:'configured'}))});
  const submit=async(h,failedJob)=>{
    h.handler=secretRouter(configured(failedJob.id),(url,options)=>{
      if(url==='/api/v1/deployments' && options.method==='POST')throw new Error('Response lost after acceptance');
      return response({items:[]});
    });
    await h.show(failedJob);await h.button('继续部署').listeners.click();
    return h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').at(-1).options.headers['Idempotency-Key'];
  };
  const original=await harness();const before=await submit(original,job);
  const reloaded=await harness();reloaded.run('crypto.randomUUID();crypto.randomUUID()');
  assert.equal(await submit(reloaded,job),before);
  // A response-lost request must be resolved before another failed task can continue.
  const attempts=reloaded.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length;
  await submit(reloaded,{...job,id:'failed-next'});
  assert.equal(reloaded.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length,attempts);
  const next=await harness();assert.notEqual(await submit(next,{...job,id:'failed-next'}),before);
});

test('GET failure is visible and retryable; saving failure never starts a deployment',async()=>{
  const h=await harness();h.handler=async()=>response({error:{message:'配置空间不可访问。'}},false);await h.show();
  assert.ok(h.nodes().some(node=>node.textContent.includes('配置空间不可访问')));assert.ok(h.button('重试读取配置'));
  h.handler=secretRouter(requirements(),()=>response({error:{message:'配置暂时无法保存。'}},false));
  await h.button('重试读取配置').listeners.click();assert.ok(h.input('password'));h.input('password').value='password';
  await h.button('保存并继续部署').listeners.click();
  assert.equal(h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length,0);
  assert.ok(h.nodes().some(node=>node.textContent.includes('无法保存')));
});

test('late save after navigating away cannot submit or overwrite the new task',async()=>{
  const h=await harness();let finish;
  h.handler=secretRouter(requirements(),()=>new Promise(resolve=>{finish=resolve;}));await h.show();
  assert.ok(h.input('password'));h.input('password').value='password';
  const pending=h.button('保存并继续部署').listeners.click();await tick();
  await h.show({...job,id:'other',error:null});finish(response({...requirements(),items:[]}));await pending;
  assert.equal(h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length,0);
  assert.ok(h.nodes().some(node=>node.textContent.startsWith('demo ·')));
});

test('switching accounts during a secret save clears its draft and cannot continue the old deployment',async()=>{
  const h=await harness();let userId='member-one',finish;
  h.context.window={AwesomeAuth:{session:()=>({mode:'feishu',canDeploy:true,userId}),fetch:h.context.fetch}};
  h.handler=secretRouter(requirements(),()=>new Promise(resolve=>{finish=resolve;}));await h.show();
  const password=h.input('password');password.value='old-account-password';
  const pending=h.button('保存并继续部署').listeners.click();await tick();
  userId='member-two';h.run('syncDeployPermission()');assert.equal(password.value,'');
  finish(response({...requirements(),items:requirements().items.map(item=>({...item,state:'configured'}))}));await pending;
  assert.equal(h.calls.filter(call=>call.url==='/api/v1/deployments' && call.options.method==='POST').length,0);
  assert.equal(h.run('secretEditor'),null);
});

test('partial saving refreshes state, clears only saved fields and keeps unsaved drafts',async()=>{
  const h=await harness();let saved=false;
  h.handler=(url,options)=>{
    if(url.endsWith('/secrets') && options.method==='POST'){saved=true;return response({error:{message:'第二项保存失败。'}},false);}
    const data=requirements();data.items.push({name:'provider',managed:false,uses:[{service:'web',environmentVariable:'OPENAI_API_KEY'}],state:'missing'});
    if(saved){data.items[0].state='configured';data.items[1].state='configured';}return response(data);
  };
  await h.show();const first=h.input('password'),second=h.input('provider');assert.ok(first);first.value='first';second.value='second';
  await h.button('保存并继续部署').listeners.click();
  assert.equal(first.value,'');assert.equal(h.input('password'),undefined);assert.equal(h.input('provider'),second);assert.equal(second.value,'second');
  assert.equal(h.input('MASTER_KEY'),undefined);
  assert.ok(h.nodes().some(node=>node.textContent.includes('平台保管')));
  assert.equal(h.calls.filter(call=>call.url==='/api/v1/deployments').length,0);
});

test('missing managed keys block saving when refreshed scope no longer allows generation',async()=>{
  const h=await harness();let saved=false;
  h.handler=(url,options)=>{
    if(options.method==='POST'){saved=true;return response({error:{message:'请刷新状态。'}},false);}
    return response({...requirements(),canGenerate:!saved});
  };
  await h.show();h.input('password').value='password';
  await h.button('保存并继续部署').listeners.click();
  assert.equal(h.button('保存并继续部署').disabled,true);
  assert.equal(h.input('MASTER_KEY'),undefined);
  assert.ok(h.nodes().some(node=>node.textContent.includes('恢复原配置')));
  await h.button('保存并继续部署').listeners.click();
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,1);
});

test('empty or multiline user configuration cannot be submitted',async()=>{
  const h=await harness();h.handler=secretRouter(requirements());await h.show();assert.ok(h.input('password'));
  h.input('password').value='';await h.button('保存并继续部署').listeners.click();
  h.input('password').value='line\nbreak';await h.button('保存并继续部署').listeners.click();
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
});

test('the left deployment form cannot race a pending configuration save',async()=>{
  const h=await harness();let finish;
  h.context.FormData=class { *[Symbol.iterator](){yield ['repository',job.repository];yield ['ref',job.ref];yield ['subdomain',job.subdomain];} };
  h.handler=secretRouter(requirements(),(url,options)=>url.endsWith('/secrets') ? new Promise(resolve=>{finish=resolve;}) : response({...job,id:'unexpected'}));
  await h.show();assert.ok(h.input('password'));h.input('password').value='password';
  const pending=h.button('保存并继续部署').listeners.click();await tick();
  await h.get('deploy-form').listeners.submit({preventDefault(){}});
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,1);
  await h.show({...job,id:'other',error:null});finish(response({...requirements(),items:[]}));await pending;
});

test('failure from the left deployment form does not destroy a configuration draft',async()=>{
  const h=await harness();h.context.FormData=class { *[Symbol.iterator](){yield ['repository',job.repository];yield ['ref',job.ref];yield ['subdomain',job.subdomain];} };
  h.handler=secretRouter(requirements(),()=>response({error:{message:'提交暂时失败。'}},false));await h.show();
  const input=h.input('password');assert.ok(input);input.value='keep-draft';
  await h.get('deploy-form').listeners.submit({preventDefault(){}});
  assert.equal(h.input('password'),input);assert.equal(input.value,'keep-draft');
  assert.equal(h.button('保存并继续部署').disabled,false);
});

test('only missing managed configuration saves an empty values object before continuation',async()=>{
  const h=await harness();const data=requirements();data.items[0].state='configured';
  h.handler=secretRouter(data,(url,options)=>{
    if(url.endsWith('/secrets'))return response({...data,items:data.items.map(item=>({...item,state:'configured'}))});
    if(url==='/api/v1/deployments' && options.method==='POST')return response({error:{message:'暂时不能提交。'}},false);
    return response({items:[]});
  });
  await h.show();assert.equal(h.nodes().filter(node=>node.tagName==='input').length,0);
  assert.equal(h.button('保存并继续部署').disabled,false);
  await h.button('保存并继续部署').listeners.click();
  const writes=h.calls.filter(call=>call.options.method==='POST');assert.equal(writes.length,2);
  assert.ok(writes[0].url.endsWith('/secrets'));assert.deepEqual(JSON.parse(writes[0].options.body),{values:{}});
  assert.ok(h.nodes().some(node=>node.textContent.includes('平台保管')));
  await h.button('继续部署').listeners.click();
  assert.equal(h.calls.filter(call=>call.url.endsWith('/secrets') && call.options.method==='POST').length,1);
});

test('unavailable managed keys require restoration without any replacement input or submission',async()=>{
  const h=await harness();const data=requirements();data.items[1].state='unavailable';
  h.handler=secretRouter(data);await h.show();h.input('password').value='password';
  assert.equal(h.input('MASTER_KEY'),undefined);
  assert.ok(h.nodes().some(node=>node.textContent.includes('恢复原配置')));
  assert.equal(h.button('保存并继续部署').disabled,true);
  await h.button('保存并继续部署').listeners.click();
  assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
});

test('opening diagnostic advice stays open across analysis and capability updates',async()=>{
  const h=await harness();h.handler=secretRouter(requirements(),()=>response({text:'配置建议'}));await h.show();
  const details=h.nodes().find(node=>node.tagName==='details' && node.className==='secret-diagnosis');assert.ok(details);
  details.open=true;if(details.listeners.toggle)details.listeners.toggle();
  await h.run("analyzeJob('failed-one')");h.run('renderCapabilities({aiAdvice:{configured:true}})');
  assert.equal(h.nodes().find(node=>node.className==='secret-diagnosis').open,true);
});
