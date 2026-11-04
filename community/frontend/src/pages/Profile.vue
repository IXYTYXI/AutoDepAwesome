<script setup>
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import { ref } from "vue";
import { auth } from "../services/auth.js";
const props = defineProps({ state: Object, user: Object });
const emit = defineEmits(["compose", "retry"]);
const exiting = ref(false);
async function logout() {
  exiting.value = true;
  try {
    await auth.logout();
  } catch {
    exiting.value = false;
    alert("退出失败，请重试。");
  }
}
</script>
<template>
  <main class="profile-main wrap" id="main-content">
    <PageBreadcrumb current="个人空间" />
    <div class="profile-heading">
      <h1>个人空间</h1>
    </div>
    <div class="profile-layout">
      <section
        class="identity-panel profile-glass"
        aria-labelledby="identity-title"
      >
        <div class="identity-cover" aria-hidden="true"><i></i></div>
        <div class="identity-content">
          <div class="identity-avatar" aria-hidden="true">
            <svg
              class="ui-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.75"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
              focusable="false"
            >
              <use href="/assets/icons/lucide.svg#user-round"></use>
            </svg>
          </div>
          <span class="account-status">{{
            user.identity_source === "feishu" ? "飞书已登录" : "开发账号"
          }}</span>
          <h2 id="identity-title">个人信息</h2>
          <dl class="identity-fields">
            <div>
              <dt>姓名</dt>
              <dd data-user-name>{{ user.display_name }}</dd>
            </div>
            <div>
              <dt>所属部门</dt>
              <dd data-user-departments>
                {{
                  user.departments
                    ?.map((d) => d.name)
                    .filter(Boolean)
                    .join(" / ") || "未提供"
                }}
              </dd>
            </div>
            <div>
              <dt>工作邮箱</dt>
              <dd data-user-email>{{ user.email || "未提供" }}</dd>
            </div>
          </dl>
          <div class="login-placeholder">
            <UButton
              color="neutral"
              variant="outline"
              type="button"
              @click="logout"
              :loading="exiting"
            >
              退出登录
            </UButton>
          </div>
        </div>
      </section>
      <section
        class="incentive-planning profile-glass"
        id="points"
        aria-labelledby="incentive-title"
      >
        <span class="planning-icon" aria-hidden="true"
          ><Icon name="gift"
        /></span>
        <h2 id="incentive-title">AI 激励政策</h2>
        <p>AI激励政策正在积极规划中...</p>
      </section>
    </div>
    <div class="profile-bottom">
      <AppLink href="index.html"
        >返回首页 <span><TripleArrow compact /></span
      ></AppLink>
    </div>
  </main>
</template>

<style scoped>
.incentive-planning {
  position: relative;
  overflow: hidden;
  min-height: 320px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  background:
    radial-gradient(ellipse at 80% 0%, #dee6f4, transparent 70%), #f8fbff;
}
.incentive-planning::after {
  content: "";
  position: absolute;
  width: 240px;
  height: 240px;
  right: -120px;
  bottom: -120px;
  border: 1px solid #becfe666;
  border-radius: 50%;
  box-shadow:
    0 0 0 30px #c9d9ec20,
    0 0 0 60px #c9d9ec16;
  pointer-events: none;
}
.planning-icon {
  display: grid;
  place-items: center;
  width: 68px;
  height: 68px;
  border-radius: 22px;
  border: 1px solid white;
  background: #ffffffb0;
  color: #728eaf;
  box-shadow: 0 8px 24px #3452780d;
}
.planning-icon .ui-icon {
  width: 30px;
  height: 30px;
}
.incentive-planning h2 {
  font-size: 23px;
  font-weight: 600;
  color: #344e6e;
  margin: 24px 0 12px;
}
.incentive-planning p {
  font-size: 15px;
  color: #75869b;
  line-height: 1.8;
  text-align: center;
}
</style>
