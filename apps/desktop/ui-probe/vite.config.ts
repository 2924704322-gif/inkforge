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
  },
})
