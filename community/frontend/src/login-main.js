import { createApp } from "vue";
import Login from "./pages/Login.vue";
import Icon from "./components/Icon.vue";
import ui from "@nuxt/ui/vue-plugin";
import "./styles/nuxt-login.css";
import "./styles/background.css";
createApp(Login).use(ui).component("Icon", Icon).mount("#app");
