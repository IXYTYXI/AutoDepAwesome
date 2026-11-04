<script setup>
import PageHeading from "../components/PageHeading.vue";
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import { ref, computed, onMounted, nextTick } from "vue";
import { auth } from "../services/auth.js";
import FilterResults from "../components/FilterResults.vue";
import PersonPhoto from "../components/PersonPhoto.vue";
import PersonTags from "../components/PersonTags.vue";
const people = ref([]),
  group = ref("all"),
  query = ref(""),
  loading = ref(true),
  error = ref(""),
  stale = ref(false),
  selected = ref(null),
  dialog = ref(null);
const groups = ["all", "组织效能团队", "业务 AI 先锋"];
const groupIcons = {
  all: "users",
  组织效能团队: "building-2",
  "业务 AI 先锋": "sparkles",
};
const groupCounts = computed(() =>
  Object.fromEntries(
    groups.map((value) => [
      value,
      people.value.filter((p) => value === "all" || p.title === value).length,
    ]),
  ),
);
const filtered = computed(() =>
  people.value
    .filter(
      (p) =>
        (group.value === "all" || p.title === group.value) &&
        [
          p.name,
          p.team,
          p.position,
          p.title,
          ...p.tags,
          ...p.tags.map((t) => t.replace(/AI\s*官/g, "AI 领航团")),
        ]
          .join(" ")
          .toLowerCase()
          .includes(query.value.trim().toLowerCase()),
    )
    .sort(
      (a, b) => (a.title !== "组织效能团队") - (b.title !== "组织效能团队"),
    ),
);
const teams = computed(
  () => new Set(people.value.map((p) => p.team).filter(Boolean)).size,
);
const contact = computed(() =>
  /^https:\/\/applink\.feishu\.cn\/client\/chat\/open\?openId=ou_[A-Za-z0-9_]{1,128}$/.test(
    selected.value?.chat_url || "",
  )
    ? selected.value.chat_url
    : "",
);
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await auth.request("/api/v1/officers");
    people.value = data.items;
    stale.value = data.stale;
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
async function open(person) {
  selected.value = person;
  await nextTick();
  dialog.value.showModal();
}
function outside(e) {
  if (e.target !== dialog.value) return;
  const r = dialog.value.getBoundingClientRect();
  if (
    e.clientX < r.left ||
    e.clientX > r.right ||
    e.clientY < r.top ||
    e.clientY > r.bottom
  )
    dialog.value.close();
}
onMounted(load);
</script>
<template>
  <main class="officers-page wrap" id="main-content">
    <PageBreadcrumb current="AI 领航团" />
    <div class="officers-intro">
      <div>
        <PageHeading>认识我们的 <span>AI 领航团</span><i>.</i></PageHeading>
      </div>
      <p class="officers-count" aria-live="polite">
        <span
          ><Icon name="users" /><b>{{ people.length }}</b> 位成员</span
        ><i></i
        ><span
          ><b>{{ teams }}</b> 个团队</span
        >
      </p>
    </div>
    <div class="people-toolbar">
      <div class="people-tabs" role="group" aria-label="人员分组">
        <UButton
          color="neutral"
          :variant="group === value ? 'soft' : 'ghost'"
          v-for="value in groups"
          :key="value"
          :class="{ selected: group === value }"
          :aria-pressed="group === value"
          @click="group = value"
        >
          <Icon :name="groupIcons[value]" />
          <span>{{ value === "all" ? "全部" : value }}</span>
          <span class="group-count">{{ groupCounts[value] }}</span>
        </UButton>
      </div>
      <label class="people-search"
        ><Icon name="search" /><input
          v-model="query"
          type="search"
          placeholder="搜索姓名、团队、方向"
          aria-label="搜索姓名、团队、方向" /><button
          v-if="query"
          type="button"
          class="people-search-clear"
          aria-label="清除搜索"
          @click="query = ''"
        >
          <Icon name="x" /></button
      ></label>
    </div>
    <p v-if="error || stale" class="people-notice" role="status">
      {{ error || "资料更新暂不可用，正在展示上次同步的内容。" }}
    </p>
    <FilterResults :filter-key="`${group}:${query}`">
      <div id="people-directory" aria-live="polite">
        <div v-if="loading" class="people-loading">正在加载人员资料…</div>
        <div v-else-if="error" class="people-empty">人员资料暂时无法加载</div>
        <div v-else-if="!filtered.length" class="people-empty">
          没有找到匹配的成员
        </div>
        <div v-else class="people-grid people-section">
          <article
            v-for="(person, index) in filtered"
            :key="person.id"
            class="person-card"
            tabindex="0"
            role="button"
            :aria-label="`查看${person.name}的个人介绍`"
            @click="open(person)"
            @keydown.enter.prevent="open(person)"
            @keydown.space.prevent="open(person)"
          >
            <PersonPhoto :person="person" :eager="index < 5" />
            <div class="person-caption">
              <div class="person-name-line">
                <h3>{{ person.name }}</h3>
                <TripleArrow compact />
              </div>
              <div v-if="person.position" class="person-team">
                {{ person.position }}
              </div>
              <p v-if="person.bio" class="person-bio">{{ person.bio }}</p>
              <PersonTags :tags="person.tags" />
            </div>
          </article>
        </div>
      </div>
    </FilterResults>
    <button v-if="error" class="people-retry" @click="load">重新加载</button>
  </main>
  <dialog
    ref="dialog"
    class="person-dialog"
    aria-labelledby="person-dialog-name"
    @click="outside"
  >
    <button
      class="person-close"
      aria-label="关闭个人介绍"
      @click="dialog.close()"
    >
      <Icon name="x" />
    </button>
    <div v-if="selected" id="person-detail">
      <div class="person-detail-layout">
        <div class="person-detail-visual">
          <span class="person-visual-label">OWS COMMUNITY</span>
          <figure class="person-detail-print">
            <PersonPhoto :person="selected" eager />
            <figcaption class="person-print-caption">
              <span>{{ selected.name }}</span
              ><span class="person-print-edition">OWS / PEOPLE</span>
            </figcaption>
          </figure>
        </div>
        <div class="person-detail-copy">
          <div class="person-detail-eyebrow">AI 领航团</div>
          <h2 id="person-dialog-name">{{ selected.name }}</h2>
          <div v-if="selected.position" class="person-team">
            {{ selected.position }}
          </div>
          <p v-if="selected.bio" class="person-bio">{{ selected.bio }}</p>
          <PersonTags :tags="selected.tags" />
          <div class="person-contact-actions">
            <component
              :is="contact ? 'a' : 'button'"
              class="person-contact"
              :href="contact || undefined"
              :disabled="!contact"
              target="_blank"
              rel="noopener noreferrer"
              :aria-label="`联系${selected.name}（打开飞书聊天）`"
              ><span class="person-contact-words"
                ><strong>联系我</strong><span>飞书 · 开始聊天</span></span
              ><span class="person-contact-arrow"
                ><TripleArrow compact /></span></component
            ><span v-if="!contact" class="person-contact-hint"
              >暂未关联飞书账号</span
            >
          </div>
        </div>
      </div>
    </div>
  </dialog>
</template>
