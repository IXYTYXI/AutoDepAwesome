<script setup>
import { useRouter } from "vue-router";
import { ref, reactive, nextTick } from "vue";
import { repository } from "../services/content.js";
import { safeUrl, detailUrl } from "../services/urls.js";
const router = useRouter();
const props = defineProps({ state: Object });
const emit = defineEmits(["saved"]);
const fields = reactive({title:"",summary:"",source:"",url:"",category:"产品设计"});
const dialog = ref(null),
  form = ref(null),
  error = ref(""),
  saving = ref(false);
async function open(value) {
  if (value !== "case") return;
  error.value = "";
  await nextTick();
  form.value.reset();
  Object.assign(fields, {title:"",summary:"",source:"",url:"",category:"产品设计"});
  dialog.value.showModal();
}
defineExpose({ open });
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
function readImage(file) {
  return new Promise((resolve, reject) => {
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type))
      return reject(Error("请选择 PNG、JPEG 或 WebP 图片。"));
    if (file.size > 2 * 1024 * 1024)
      return reject(Error("请选择小于 2 MB 的图片。"));
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(Error("图片读取失败，请重试。"));
    r.readAsDataURL(file);
  });
}
async function save() {
  if (saving.value) return;
  error.value = "";
  const title = fields.title.trim(),
    summary = fields.summary.trim(),
    url = fields.url.trim();
  if (!title || !summary) {
    error.value = "请填写标题和正文。";
    return;
  }
  if (url && !safeUrl(url)) {
    error.value = "请使用有效的 http 或 https 链接。";
    return;
  }
  saving.value = true;
  try {
    const base = {
      title,
      summary,
      url,
      source: fields.source.trim() || "个人分享",
    };
    const file = form.value.elements.caseImage.files[0];
    const image = file ? await readImage(file) : "";
    const ids = new Set(props.state.cases.map(c => c.id));
    const state = await repository.createCase({ ...base, body: summary, category: fields.category, image });
    const saved = state.cases.find(c => !ids.has(c.id));
    dialog.value.close();
    emit("saved", state);
    if (saved)
      await router.push(detailUrl(
        "case",
        saved.id,
      ));
  } catch (e) {
    error.value =
      e.name === "QuotaExceededError"
        ? "本地存储空间不足，请使用更小的图片。"
        : e.message || "保存失败，请重试。";
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <dialog
    id="editor"
    ref="dialog"
    @click="outside"
    aria-labelledby="editor-title"
  >
    <UButton
      class="dialog-close"
      color="neutral"
      variant="ghost"
      aria-label="关闭编辑"
      @click="dialog.close()"
      ><Icon name="x"
    /></UButton>
    <div class="editor-top">
      <UBadge variant="soft" color="neutral" size="sm">本机预览</UBadge>
      <h2 id="editor-title" class="mt-4">
        分享实践
      </h2>
    </div>
    <form id="editor-form" ref="form" @submit.prevent="save">
      <label
        >标题<UInput
          class="w-full"
          size="lg"
          name="title" v-model="fields.title"
          required
          maxlength="48"
          placeholder="用一句话说清楚"
      /></label>
      <label
        >摘要 / 正文<UTextarea
          class="w-full"
          size="lg"
          name="summary" v-model="fields.summary"
          required
          :rows="4"
          maxlength="3000"
          placeholder="适用场景、具体做法与成果"
      /></label>
      <div class="form-row">
        <label
          >来源 / 团队<UInput
            class="w-full"
            size="lg"
            name="source" v-model="fields.source"
            maxlength="40"
            placeholder="你的团队" /></label
        ><label id="category-field"
          >工作场景<select name="category" v-model="fields.category">
            <option>产品设计</option>
            <option>内容运营</option>
            <option>通用办公</option>
            <option>研发提效</option>
          </select></label
        >
      </div>
      <label
        >原文或活动链接（选填）<UInput
          class="w-full"
          size="lg"
          name="url" v-model="fields.url"
          type="url"
          placeholder="https://"
      /></label>
      <div id="case-image-field">
        <label
          >案例封面（选填）<input
            name="caseImage"
            type="file"
            accept="image/png,image/jpeg,image/webp"
        /></label>
        <p class="field-help">PNG、JPEG 或 WebP，大小不超过 2 MB。</p>
      </div>
      <UAlert v-if="error" color="error" variant="soft" :title="error" />
      <div class="form-actions">
        <span>仅保存到本机浏览器。</span
        ><UButton size="lg" type="submit" :loading="saving"
          >保存并预览 <TripleArrow compact
         /></UButton>
      </div>
    </form>
  </dialog>
</template>
