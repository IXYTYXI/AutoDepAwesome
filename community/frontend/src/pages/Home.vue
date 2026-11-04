<script setup>
import SectionDivider from "../components/SectionDivider.vue";
import TripleArrow from "../components/TripleArrow.vue";
import HomeResources from "../components/HomeResources.vue";
import HomeNews from "../components/HomeNews.vue";
import { computed, ref, watch } from "vue";
import CommunityArtwork from "../components/CommunityArtwork.vue";
import { defaultBanner } from "../services/defaults.js";
import HomeCaseStudy from "../components/HomeCaseStudy.vue";
import "../styles/home-stories.css";
import CaseStatus from "../components/CaseStatus.vue";
import { imageUrl, safeUrl } from "../services/urls.js";
const props = defineProps({ state: Object, user: Object });
const emit = defineEmits(["compose", "retry"]);

const banner = computed(() => props.state.banner || defaultBanner);
const imageFailed = ref(false);
const bannerImage = computed(() => imageFailed.value ? "" : imageUrl(banner.value.image));
const bannerLink = computed(() => safeUrl(banner.value.url));
watch(() => banner.value.image, () => (imageFailed.value = false));
const featuredCases = computed(() => props.state.cases.filter(c => c.remote).slice(0, 3));
</script>
<template>
  <main class="community-home wrap" id="main-content">
    <section
      class="campaign-banner"
      :class="{ 'has-image': bannerImage }"
      :aria-labelledby="banner.title ? 'campaign-title' : undefined"
      :aria-label="banner.title ? undefined : '社区精选'"
    >
      <div v-if="bannerImage" class="campaign-media">
        <img class="campaign-cover" :src="bannerImage" :alt="banner.title || 'OWS Community 社区海报'" @error="imageFailed = true" />
      </div>
      <div v-if="!bannerImage" class="campaign-grid" aria-hidden="true"></div>
      <div v-if="!bannerImage" class="campaign-topline">
        <span><i></i> OWS COMMUNITY <b>/</b> 社区精选</span>
      </div>
      <div v-if="banner.title || banner.summary || bannerLink" class="campaign-copy">
        <h1 v-if="banner.title" id="campaign-title">{{ banner.title }}</h1>
        <p v-if="banner.summary">{{ banner.summary }}</p>
        <UButton v-if="bannerLink" class="campaign-cta" :to="bannerLink" target="_blank" rel="noopener noreferrer" size="xl"
          >查看详情 <span><TripleArrow compact  /></span
        ></UButton>
      </div>
      <CommunityArtwork v-if="!bannerImage" class="campaign-art" />
      <div v-if="!bannerImage" class="campaign-baseline">
        <span>洋葱 AI 实践社区</span
        ><span class="campaign-issue"><i></i> SHARE SOMETHING NEW</span>
      </div>
    </section>
    <UAlert v-if="state.bannerError" color="warning" variant="soft" class="mt-4" title="Banner 暂时无法加载" :description="state.bannerError" />
    <SectionDivider />
    <HomeNews :state="state" @retry="emit('retry')" />
    <SectionDivider />
    <section class="home-stories" aria-labelledby="home-cases-title">
      <div class="stories-heading">
        <div><h2 id="home-cases-title">精选案例<span>.</span></h2></div>
        <AppLink class="stories-all" href="cases.html">探索全部案例 <TripleArrow /></AppLink>
      </div>
      <CaseStatus :state="state" @retry="emit('retry')" />
      <div id="home-cases" class="stories-grid">
        <HomeCaseStudy v-for="(item, index) in featuredCases" :key="item.id" :item="item" :index="index" />
        <p v-if="!featuredCases.length && !state.casesError" class="empty">暂无案例</p>
      </div>
    </section>
    <HomeResources :state="state" @retry="emit('retry')" />
  </main>
</template>

<style scoped>
.community-home :deep(.home-newsroom),.community-home .home-stories { margin:0; }
</style>
