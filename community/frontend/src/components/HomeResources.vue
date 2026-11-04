<script setup>
import SectionDivider from './SectionDivider.vue';
import TripleArrow from './TripleArrow.vue';
defineProps({state:Object});
defineEmits(['retry']);
</script>
<template>
  <SectionDivider />
  <section class="resource-section" aria-labelledby="skillhub-title">
    <h2 id="skillhub-title" class="resource-title">SkillHub<span>.</span></h2>
    <AppLink class="resource-hub" href="https://onionhub.yc345.tv/" target="_blank" rel="noopener noreferrer">
      <img class="hub-cover" src="/assets/skillhub-toolkit.png" alt="代码、文档、检索与工作流模块组成的技能工具箱" loading="lazy" width="2172" height="724" />
      <div class="hub-copy"><h3>发现技能，<br />组合成工作流<span>。</span></h3><span class="hub-link">探索 SkillHub <TripleArrow /></span></div>
    </AppLink>
  </section>
  <SectionDivider />
  <section class="home-faq" aria-labelledby="faq-title">
    <header class="faq-heading"><div><h2 id="faq-title">常见问题<i>.</i></h2></div><div class="faq-heading-line" aria-hidden="true"></div></header>
    <div class="faq-panel">
      <template v-if="state.faq?.length">
        <details v-for="(item,index) in state.faq" :key="item.id" class="faq-item">
          <summary><span class="faq-no">{{ String(index+1).padStart(2,'0') }}</span><h3>{{ item.question }}</h3><Icon name="plus" /></summary>
          <p>{{ item.answer }}</p>
        </details>
      </template>
      <div v-else-if="state.faqError" class="faq-error"><span>暂时无法加载</span><button @click="$emit('retry')">重试</button></div>
      <div v-else class="faq-quiet" aria-label="暂无常见问题"><span aria-hidden="true"></span></div>
    </div>
  </section>
</template>
<style scoped>
.resource-title { margin:0 0 24px; font-size:clamp(30px,2.6vw,36px); font-weight:650; line-height:1.15; letter-spacing:-1px; color:#172d49; }
.resource-title span { color:#2870ed; }
.resource-hub { position:relative; isolation:isolate; overflow:hidden; display:flex; align-items:center; min-height:340px; aspect-ratio:3.2; border:1px solid #ffffff80; border-radius:26px; background:#25231f; box-shadow:0 16px 35px -15px #27231f60; text-decoration:none; }
.hub-cover { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; transition:transform .8s cubic-bezier(.2,.7,.2,1); z-index:-2; }
.resource-hub::after { content:''; position:absolute; inset:0; z-index:-1; background:linear-gradient(90deg,#171716b8,transparent 65%); }
.hub-copy { padding:42px 48px; color:#fff9ef; }
.hub-copy h3 { margin:0 0 34px; font-size:clamp(28px,3vw,43px); line-height:1.4; font-weight:550; letter-spacing:-1px; }
.hub-copy h3 span { color:#ec6845; }
.hub-link { display:inline-flex; align-items:center; gap:24px; border-bottom:1px solid #e7d7c27a; padding-bottom:12px; color:#fff0dd; font-size:15px; }
.resource-hub:focus-visible { outline:3px solid #d66b45; outline-offset:5px; }
@media(hover:hover) { .resource-hub:hover .hub-cover { transform:scale(1.035); } }
.home-faq { margin:0 0 12px; }
.faq-heading { display:flex; align-items:center; gap:32px; margin-bottom:20px; }
.faq-heading > div:first-child { display:flex; align-items:baseline; gap:17px; }
.faq-heading > div > span { font-size:13px; letter-spacing:3px; color:#6d8cae; }
.faq-heading h2 { font-size:clamp(30px,2.6vw,36px); font-weight:600; color:#223e61; margin:0; letter-spacing:-.5px; }
.faq-heading-line { height:1px; background:linear-gradient(90deg,#a7bfd8,transparent); flex:1; }
.faq-panel { overflow:hidden; border:1px solid #ffffffed; border-radius:23px; background:linear-gradient(105deg,#dcecffb8,#ffffffd9); box-shadow:0 12px 30px -24px #456b924d; }
.faq-quiet { min-height:90px; display:flex; align-items:center; padding:30px; }
.faq-quiet span { display:block; width:100%; height:1px; background:linear-gradient(90deg,#abc7e84d,transparent); }
.faq-item { margin:0 28px; border-bottom:1px solid #bed1e745; }
.faq-item:last-child { border:0; }
.faq-item summary { display:flex; align-items:center; gap:18px; cursor:pointer; padding:23px 0; list-style:none; }
.faq-item summary::-webkit-details-marker { display:none; }
.faq-no { font-size:11px; font-variant-numeric:tabular-nums; color:#7395bd; }
.faq-item h3 { flex:1; font-size:19px; font-weight:550; color:#27496f; line-height:1.6; margin:0; }
.faq-item summary .ui-icon { width:18px; height:18px; color:#417fbd; transition:transform .2s; flex-shrink:0; }
.faq-item[open] summary .ui-icon { transform:rotate(45deg); }
.faq-item p { white-space:pre-wrap; overflow-wrap:anywhere; color:#536c88; font-size:17px; line-height:1.9; margin:0 36px 24px; }
.faq-error { display:flex; gap:18px; padding:30px; font-size:13px; color:#6e829a; }
.faq-error button { color:#2770bd; text-decoration:underline; cursor:pointer; }
.hub-link:focus-visible,.faq-item summary:focus-visible { outline:2px solid #4486db; outline-offset:5px; }
@media(max-width:700px) { .resource-hub { min-height:340px; aspect-ratio:auto; align-items:flex-end; } .hub-cover { object-position:65% center; } .resource-hub::after { background:linear-gradient(0deg,#151412ed, #15141230 90%); } .hub-copy { padding:28px; } .hub-copy h3 { font-size:30px; margin-bottom:22px; } .faq-item { margin:0 20px; } .faq-item p { margin-left:0; margin-right:0; } }
@media(prefers-reduced-motion:reduce) { .hub-cover,.faq-item summary .ui-icon { transition:none; } }
</style>
