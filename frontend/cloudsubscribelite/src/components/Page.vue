<script>
// 1.7.90：单入口收敛。MoviePilot 的 nav_key=main 只加载 ./AppPage，
// ./Page 仅作老版本 fallback，这里直接转发，避免两份实现继续漂移。
import AppPage from "./AppPage.vue"

export default AppPage
</script>
