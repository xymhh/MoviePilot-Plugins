import { createApp } from "vue";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import "./styles/tokens.css";
import AppPage from "./components/AppPage.vue";

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: "dark",
  },
});

// 本地自测 Mock 数据
// v1.13.3 瘦页：本地自测用扁平失败记录（重试索引行形状）
const mockFailures = [
  {
    record_id: "mock-failed-1",
    time: "2026-11-17 10:20:00",
    share_url: "https://115.com/s/mock1",
    file_name: "凡人修仙传 S01E121 4K.mkv",
    source_file_name: "凡人修仙传 第121集 4K.mkv",
    title: "凡人修仙传",
    type: "动漫",
    year: "2020",
    tmdb_id: 106342,
    season: 1,
    episode: 121,
    file_size: 1250000000,
    status: "失败",
    stage: "transfer",
    failure_kind: "network",
    failure_reason: "网络超时",
    failure_retryable: true,
    can_retry: true,
    retry_title: "重试此记录",
    transfer_mode: "cross",
    source_drive_key: "quark",
    target_drive_key: "115",
    cloud_dir: "/Alist/影视/moviepilot/媒体库/剧集/国漫/凡人修仙传",
    landed: null,
    resource_type: "quark",
    source: "pansou",
  },
  {
    record_id: "mock-repair-1",
    time: "2026-11-17 09:05:00",
    share_url: "https://115.com/s/mock2",
    file_name: "庆余年 第二季 S02E01 4K.mkv",
    source_file_name: "庆余年 第二季 S02E01 4K.mkv",
    title: "庆余年 第二季",
    type: "电视剧",
    year: "2024",
    tmdb_id: 111110,
    season: 2,
    episode: 1,
    file_size: 4500000000,
    status: "成功",
    stage: "finalize",
    failure_kind: "",
    failure_reason: "",
    failure_retryable: true,
    can_retry: true,
    retry_title: "重新转存此记录",
    transfer_mode: "cross",
    source_drive_key: "quark",
    target_drive_key: "115",
    cloud_dir: "/Alist/影视/moviepilot/媒体库/剧集/国产剧/庆余年 第二季",
    landed: false,
    resource_type: "quark",
    source: "pansou",
  },
];

const mockApi = {
  get(url) {
    if (url.includes("/page_data")) {
      return Promise.resolve({
        success: true,
        data: {
          failures: mockFailures,
          history_page: { page: 1, page_size: 10, total: 2, total_pages: 1, filter_options: { resource_types: [], sources: [] }, enable_cloud_upgrade: true },
          runtime: { status: "idle", tasks: [] },
        },
      });
    }
    if (url.includes("/runtime/status") || url.includes("/runtime")) {
      return Promise.resolve({
        success: true,
        data: { status: "idle", tasks: [], sync_in_progress: false },
      });
    }
    if (url.includes("/tg/status")) {
      return Promise.resolve({
        success: true,
        data: { enabled: true, configured: true, channel_count: 8, item_count: 24, new_count: 3 },
      });
    }
    if (url.includes("/tg/items")) {
      return Promise.resolve({
        success: true,
        data: {
          items: [
            {
              id: 1,
              channel_title: "阿里云盘盘友圈",
              published_at: "2024-11-01 10:20:00",
              title: "【国创动画】凡人修仙传 第121集 4K 60帧 高码版",
              types: ["alipan"],
              links: ["https://www.alipan.com/s/sample123"],
              message_url: "https://t.me/sample/1",
              match_reason: "命中订阅《凡人修仙传》缺集 S01E121",
              status: "transferred",
            },
            {
              id: 2,
              channel_title: "夸克网盘精选",
              published_at: "2024-11-01 11:30:00",
              title: "庆余年 第二季 01-36集 全集 4K HDR",
              types: ["quark"],
              links: ["https://pan.quark.cn/s/sample456"],
              message_url: "https://t.me/sample/2",
              match_reason: "命中订阅《庆余年 第二季》洗版更优画质",
              status: "queued",
            },
            {
              id: 3,
              channel_title: "115分享大本营",
              published_at: "2024-11-01 12:00:00",
              title: "沙丘2 Dune.Part.Two.2024.2160p.HDR.Remux",
              types: ["115"],
              links: ["https://115.com/s/sample789"],
              auto_transfer_error: "分享包含提取码但未提供",
              status: "skipped",
            },
          ],
        },
      });
    }
    return Promise.resolve({ success: true, data: {} });
  },
  post(url, body) {
    console.log("Mock POST:", url, body);
    return Promise.resolve({ success: true, message: "操作成功（Mock）" });
  },
};

const app = createApp(AppPage, {
  api: mockApi,
  pluginId: "CloudSubscribeX",
});
app.use(vuetify);
app.mount("#app");
