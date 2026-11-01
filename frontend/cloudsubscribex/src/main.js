import { createApp } from "vue";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import AppPage from "./components/AppPage.vue";

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: "dark",
  },
});

// 本地自测 Mock 数据
const mockHistoryGroups = [
  {
    group_key: "庆余年2",
    title: "庆余年 第二季",
    year: "2024",
    type: "电视剧",
    total_size: 156829104820,
    poster_path: "https://image.tmdb.org/t/p/w500/r0L74qCq5v2tM7K0i5W5Z3nB0bA.jpg",
    resource_types: ["115", "quark"],
    tmdb_id: 111110,
    records: [
      { season: 2, episode: 1, file_name: "Joy.of.Life.S02E01.4K.mkv", size: 4500000000, time: "2024-05-16 19:30", status: "成功", resource_type: "115" },
      { season: 2, episode: 2, file_name: "Joy.of.Life.S02E02.4K.mkv", size: 4300000000, time: "2024-05-16 20:00", status: "成功", resource_type: "115" },
      { season: 2, episode: 3, file_name: "Joy.of.Life.S02E03.4K.mkv", size: 4200000000, time: "2024-05-16 20:30", status: "跳过", reason: "标题包含违规水印", resource_type: "quark" },
      { season: 2, episode: 4, file_name: "Joy.of.Life.S02E04.4K.mkv", size: 4600000000, time: "2024-05-16 21:00", status: "成功", resource_type: "115" },
    ],
  },
  {
    group_key: "凡人修仙传",
    title: "凡人修仙传",
    year: "2020",
    type: "动漫",
    total_size: 89000000000,
    poster_path: "https://image.tmdb.org/t/p/w500/6MKbUe0U3gNcfY2H6hV8A7o8N.jpg",
    resource_types: ["115"],
    tmdb_id: 106342,
    records: [
      { season: 1, episode: 120, file_name: "A.Record.of.Mortal.Journey.E120.1080p.mkv", size: 1200000000, time: "2024-10-27 11:00", status: "成功", resource_type: "115" },
      { season: 1, episode: 121, file_name: "A.Record.of.Mortal.Journey.E121.1080p.mkv", size: 1250000000, time: "2024-11-01 11:00", status: "成功", resource_type: "115" },
    ],
  },
  {
    group_key: "沙丘2",
    title: "沙丘2",
    year: "2024",
    type: "电影",
    total_size: 28000000000,
    poster_path: "https://image.tmdb.org/t/p/w500/8b8R8l88Qje9dn9OE8PY05Nxl1X.jpg",
    resource_types: ["115"],
    tmdb_id: 693134,
    records: [
      { season: 0, episode: 0, file_name: "Dune.Part.Two.2024.2160p.UHD.Remux.mkv", size: 28000000000, time: "2024-04-15 14:00", status: "成功", resource_type: "115" },
    ],
  },
];

const mockApi = {
  get(url) {
    if (url.includes("/history/summary")) {
      return Promise.resolve({
        success: true,
        data: { total: 128, today: 5, success: 120, failed: 8 },
      });
    }
    if (url.includes("/page_data")) {
      return Promise.resolve({
        success: true,
        data: {
          history_groups: mockHistoryGroups,
          history_page: { page: 1, page_size: 10, total: 3, total_pages: 1 },
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
