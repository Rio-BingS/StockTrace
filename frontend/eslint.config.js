import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  // essential 档：只抓真实错误（如 v-for 缺 key、v-if 与 v-for 同级），
  // 不含模板风格化规则，避免对既有模板产生大面积格式 diff。
  ...pluginVue.configs['flat/essential'],
  {
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        __APP_VERSION__: 'readonly',
      },
    },
    rules: {
      'no-console': 'off',
      'no-case-declarations': 'off',
    },
  },
  // views 目录是路由页面组件，允许单词名（Dashboard/Market）
  {
    files: ['src/views/**'],
    rules: {
      'vue/multi-word-component-names': 'off',
    },
  },
  // vite.config.js 运行在 Node 环境
  {
    files: ['vite.config.js'],
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
]
