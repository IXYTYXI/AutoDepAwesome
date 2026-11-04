<script setup>
import SectionDivider from './SectionDivider.vue';
import TripleArrow from './TripleArrow.vue';
</script>
<template>
  <footer class="brand-footer wrap">
    <SectionDivider /><div class="footer-surface">
      <div class="footer-watermark" aria-hidden="true">OWS</div>
      <div class="footer-constellation" aria-hidden="true"><i v-for="n in 7" :key="n" :style="{'--n':n}"><Icon name="sparkles" /></i><span></span></div>
      <div class="footer-main">
        <div class="footer-brand"><AppLink href="index.html" aria-label="OWS Community 首页"><img src="/assets/ows-community-logo.png" alt="OWS Community" width="2172" height="724" /></AppLink><h2>洋葱 AI 实践社区<span>.</span></h2></div>
        <div class="footer-owner"><span class="footer-owner-dot" aria-hidden="true"></span><span>Powered By @人力团队 · 组织效能</span></div>
      </div>
      <div class="footer-baseline"><span></span><nav aria-label="页尾导航"><AppLink href="cases.html">案例库</AppLink><AppLink href="news.html">AI 资讯</AppLink><AppLink href="officers.html">AI 领航团</AppLink></nav><AppLink href="index.html" class="footer-home">回到首页 <TripleArrow /></AppLink></div>
    </div>
  </footer>
</template>
<style scoped>
.brand-footer { display:block; margin-top:0; padding:0 0 26px; border-top:0; text-align:left; }
.footer-surface { display:block; width:100%; position:relative; isolation:isolate; overflow:hidden; padding:38px 42px 22px; border:1px solid #ffffffed; border-radius:28px; background:linear-gradient(110deg,#dfe7f2e8,#f1f5faed 48%,#e5edf6e8); backdrop-filter:blur(20px); box-shadow:0 16px 40px -26px #42648b5c,inset 0 1px 0 #fff; }
.footer-surface::before { content:''; position:absolute; top:0; left:42px; width:150px; height:2px; background:linear-gradient(90deg,#2368df,#79caf3,transparent); }
.footer-main { display:flex; align-items:center; justify-content:space-between; gap:30px; padding-bottom:32px; }
.footer-brand { display:block; }
.footer-brand img { display:block; width:230px; height:auto; margin:-13px 0 2px -11px; }
.footer-brand h2 { font-size:clamp(22px,2vw,28px); font-weight:550; letter-spacing:1px; color:#25466e; margin:0; }
.footer-brand h2 span { color:#3281e9; }
.footer-owner { position:relative; z-index:1; margin-top:72px; display:flex; align-items:center; gap:10px; color:#4d7198; font-size:13px; border:1px solid #ffffffd9; background:#ffffffc9; border-radius:40px; padding:13px 20px; }
.footer-owner-dot { width:6px; height:6px; border-radius:50%; background:#4385d8; box-shadow:0 0 0 5px #d8e9ff8c; margin-right:3px; }
.footer-baseline { display:flex; justify-content:space-between; align-items:center; gap:24px; padding-top:19px; border-top:1px solid #8aaed02e; font-size:11px; color:#6484a6; }
.footer-baseline > span { font-size:9px; letter-spacing:2.5px; }
.footer-baseline nav { display:flex; flex-wrap:wrap; gap:26px; }
.footer-baseline a:hover { color:#1e68c3; }
.footer-home { white-space:nowrap; flex-shrink:0; display:flex; align-items:center; gap:8px; }
.footer-watermark { position:absolute; z-index:-1; right:8%; bottom:8px; font-size:150px; font-weight:750; letter-spacing:-14px; color:#86aeda0c; line-height:1; }
.footer-constellation { position:absolute; top:14px; right:48px; width:310px; height:130px; pointer-events:none; }
.footer-constellation i { position:absolute; left:calc(var(--n) * 35px); top:35px; color:#6b91bc; animation:star-hop 3.4s cubic-bezier(.4,0,.2,1) infinite; animation-delay:calc(var(--n) * -.38s); }
.footer-constellation .ui-icon { width:27px; height:27px; fill:#d6e6fa77; stroke-width:1.2; filter:drop-shadow(0 4px 5px #5278a325); }
.footer-constellation i:nth-child(2n) { top:65px; color:#889db4; }
.footer-constellation i:nth-child(3n) { top:15px; color:#3d7fcd; }
.footer-constellation i:nth-child(3n) .ui-icon { width:36px; height:36px; }
.footer-constellation span { position:absolute; bottom:15px; left:50px; width:220px; height:12px; background:radial-gradient(ellipse,#7d9abc33,transparent 70%); }
@keyframes star-hop { 0%,100% { transform:translateY(0) rotate(-10deg); } 45% { transform:translateY(-18px) rotate(15deg); } 65% { transform:translateY(3px) rotate(-6deg); } 80% { transform:translateY(-5px) rotate(3deg); } }
@media(max-width:650px) { .footer-constellation { position:relative; top:auto; right:auto; margin:0 0 18px auto; width:280px; max-width:100%; height:100px; } .footer-constellation i { left:calc(var(--n) * 30px); } .footer-owner { margin-top:0; flex-wrap:wrap; } }
@media(prefers-reduced-motion:reduce) { .footer-constellation i { animation:none; } }
.footer-surface a:focus-visible { outline:2px solid #4486db; outline-offset:5px; border-radius:4px; }
@media(max-width:800px) { .brand-footer { margin-top:0; } .footer-surface { padding:28px 24px 20px; border-radius:24px; } .footer-main { align-items:flex-start; flex-direction:column; gap:24px; padding-bottom:26px; } .footer-brand img { width:210px; } .footer-baseline { flex-wrap:wrap; gap:18px; } .footer-baseline > span { width:100%; } .footer-baseline nav { gap:16px; } .footer-owner { padding:10px 16px; font-size:12px; } .footer-home { gap:3px; } }
</style>
