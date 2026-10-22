(() => {
  'use strict';
  const get=id=>document.getElementById(id);
  const positive=value=>Number.isSafeInteger(value) && value>0;
  const size=value=>`${new Intl.NumberFormat('zh-CN').format(value)}\u00a0MiB`;
  function location() {
    for(const link of document.querySelectorAll('.manual-toc a')) {
      if(link.hash===(window.location.hash || '#quickstart'))link.setAttribute('aria-current','location');
      else link.removeAttribute('aria-current');
    }
  }
  async function load() {
    get('manual-storage').textContent='读取中…';get('manual-memory').textContent='读取中…';get('manual-pool').textContent='读取中…';
    get('manual-status').textContent='正在读取当前平台配置…';get('manual-retry').hidden=true;
    get('manual-network').textContent='正在读取运行网络配置…';
    try {
      const response=await window.AwesomeAuth.fetch('/api/v1/capabilities');
      if(!response.ok)throw new Error('capabilities unavailable');
      const data=await response.json(),quota=data.storageHardQuota;
      const storage=quota?.limits?.app_budget_mib,memory=data.resourceLimits?.maxAppMemoryMiB,pool=quota?.limits?.global_budget_mib;
      get('manual-storage').textContent=quota?.configured===false?'未启用硬配额':positive(storage)?size(storage):'暂未确认';
      get('manual-memory').textContent=positive(memory)?size(memory):'暂未确认';
      get('manual-pool').textContent=positive(pool)?(pool%1024===0?`${new Intl.NumberFormat('zh-CN').format(pool/1024)}\u00a0GiB`:size(pool)):'暂未确认';
      const ready=positive(storage) && positive(memory) && positive(pool) && quota?.enabled===true;
      get('manual-status').textContent=ready?'以上为当前服务器配置。MiB / GiB 为二进制容量单位。':'部分限制或存储配额状态暂未确认，请重新读取或联系管理员。';
      get('manual-retry').hidden=ready;
      get('manual-network').textContent=data.runtimeNetwork?.enabled===true?'当前运行网络允许受限公网 HTTPS（TCP 443）访问，可调用外部模型 API；不等于所有端口或私网直通。':'当前未确认允许公网 HTTPS 访问。需要外部模型 API 时，请先确认平台运行网络配置。';
    } catch {
      for(const id of ['manual-storage','manual-memory','manual-pool'])get(id).textContent='暂未确认';
      get('manual-status').textContent='暂时无法读取平台配置。使用说明仍可阅读；请重新读取或返回部署页确认限制。';
      get('manual-network').textContent='运行网络暂未确认；需要外部模型 API 时，请先确认平台运行网络配置。';
      get('manual-retry').hidden=false;
    }
  }
  get('manual-retry').addEventListener('click',load);
  window.addEventListener('hashchange',location);
  location();load();
})();
