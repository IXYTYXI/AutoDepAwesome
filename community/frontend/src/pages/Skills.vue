<script setup>
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import CaseCard from "../components/CaseCard.vue";
import CaseStatus from "../components/CaseStatus.vue";
const props = defineProps({ state: Object, user: Object });
const emit = defineEmits(["compose", "retry"]);
</script>
<template>
  <main class="portal-page wrap" id="main-content">
    <PageBreadcrumb current="SkillHub" />
    <div class="page-title">
      <div>
        <h1>SkillHub</h1>
        <span>团队技能库</span>
      </div>
    </div>
    <section class="hub-connect">
      <div>
        <span class="service-label">SkillHub</span>
        <h2>接入团队的工作流</h2>
        <p>团队技能与工作流</p>
        <AppLink
          class="hub-entry"
          href="https://onionhub.yc345.tv/"
          target="_blank"
          rel="noopener noreferrer"
          >进入 SkillHub <TripleArrow compact
        /></AppLink>
      </div>
      <div class="hub-flow" aria-hidden="true">
        <span>输入材料<small>文档 / 数据 / 需求</small></span
        ><i><TripleArrow compact /></i
        ><span>运行 Skill<small>团队工作流</small></span
        ><i><TripleArrow compact /></i
        ><span>核验结果<small>检查 / 导出</small></span>
      </div>
    </section>
    <div class="compact-heading hub-related-title">
      <h2>相关实践</h2>
      <AppLink href="cases.html">全部案例 <TripleArrow compact /></AppLink>
    </div>
    <CaseStatus :state="state" @retry="emit('retry')" />
    <div id="skill-cases">
      <CaseCard
        v-for="item in state.cases.filter(
          (c) => c.remote && c.product_type === 'Skill',
        )"
        :key="item.id"
        :item="item"
      />
    </div>
  </main>
</template>
