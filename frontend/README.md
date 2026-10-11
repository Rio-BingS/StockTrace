# StockTrace 前端

Vue 3 + Vite + ECharts。开发命令、运行时版本与目录说明见仓库根目录 [`README.md`](../README.md)
的「开发」一节，部署见 [`deploy/DEPLOY.md`](../deploy/DEPLOY.md)。

```bash
npm ci
npm run dev        # http://localhost:5173，/api 代理到 localhost:8000（VITE_API_TARGET 可覆盖）
npm run lint && npm test && npm run build
```

Node.js 版本见 `.nvmrc`（22 LTS，≥ 22.13）。
