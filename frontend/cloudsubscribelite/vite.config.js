import {defineConfig} from "vite";
import vue from "@vitejs/plugin-vue";
import federation from "@originjs/vite-plugin-federation";
import {resolve} from "node:path";
import {fileURLToPath} from "node:url";
import fs from "node:fs";

const currentDir = fileURLToPath(new URL(".", import.meta.url));
const outputDir = resolve(currentDir, "../../plugins.v2/cloudsubscribelite/dist/assets");
let pluginVersion = "1.9.36";
try {
    const pkgV2 = JSON.parse(fs.readFileSync(resolve(currentDir, "../../package.v2.json"), "utf-8"));
    pluginVersion = pkgV2?.CloudSubscribeLite?.version || pluginVersion;
} catch {}

export default defineConfig({
    base: "./",
    define: {
        __PLUGIN_VERSION__: JSON.stringify(pluginVersion),
    },
    plugins: [
        vue(),
        federation({
            name: "cloudsubscribelite",
            filename: "remoteEntry.js",
            exposes: {
                "./Page": "./src/components/Page.vue",
                "./Config": "./src/components/Config.vue",
                "./Dashboard": "./src/components/Dashboard.vue",
                "./AppPage": "./src/components/AppPage.vue",
                // MP 按 nav_key 拼组件名（nav_key=main → ./AppPageMain），
                // 上游只暴露 AppPage，补一个别名避免「打开报 not found」。
                "./AppPageMain": "./src/components/AppPage.vue",
                "./AppPageResource": "./src/components/AppPageResource.vue",
                "./AppPageTgMonitor": "./src/components/AppPageTgMonitor.vue",
            },
            shared: {
                vue: {requiredVersion: false, generate: false},
                vuetify: {requiredVersion: false, generate: false, singleton: true},
                "vuetify/styles": {
                    requiredVersion: false,
                    generate: false,
                    singleton: true,
                },
            },
            format: "esm",
        }),
    ],
    build: {
        target: "esnext",
        minify: "esbuild",
        cssCodeSplit: true,
        emptyOutDir: true,
        outDir: outputDir,
        assetsDir: "",
    },
});
