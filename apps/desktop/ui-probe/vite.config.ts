import { fileURLToPath } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 临时探针构建配置：输出到 apps/desktop/out/ui-probe（out/ 已被 .gitignore 排除）
export default defineConfig({
  root: import.meta.dirname,
  base: './',
  plugins: [vue()],
  build: {
    // 相对本配置所在目录 → apps/desktop/out/ui-probe
    outDir: '../out/ui-probe',
    emptyOutDir: true,
    // 探针不需要压缩，便于肉眼核对产物
    minify: false,
    rollupOptions: {
      input: {
        // 向导灰屏探针（原有）
        index: fileURLToPath(new URL('./index.html', import.meta.url)),
        // 人审关卡布局探针
        review: fileURLToPath(new URL('./review.html', import.meta.url)),
        // 对话框可用性探针
        dialogs: fileURLToPath(new URL('./dialogs.html', import.meta.url)),
        // 互动创作正文预览框（可拉伸）探针
        interactive: fileURLToPath(new URL('./interactive.html', import.meta.url)),
      },
    },
  },
})
