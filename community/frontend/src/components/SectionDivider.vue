<template><div class="section-divider" aria-hidden="true"><span></span></div></template>
<style scoped>
.section-divider { height:1px; position:relative; overflow:hidden; margin:50px 0; background:linear-gradient(90deg,transparent,#9caec440 25%,#9caec440 75%,transparent); }
.section-divider span { position:absolute; top:0; left:-24%; width:24%; height:1px; background:linear-gradient(90deg,transparent,#94acc5,#fff,#94acc5,transparent); animation:section-sweep 6s ease-in-out infinite; }
@keyframes section-sweep { 0%,10% { transform:translateX(0); opacity:0; } 25% { opacity:1; } 90%,100% { transform:translateX(520%); opacity:0; } }
@media(max-width:700px) { .section-divider { margin:34px 0; } }
@media(prefers-reduced-motion:reduce) { .section-divider span { animation:none; left:38%; } }
</style>
