# AGENTS.md

本项目的 AI 助手入口。**先读 `CLAUDE.md`**（命令/架构/不变量/红线全在里面），接任务前再看 `PROJECT_PROGRESS.md` 的「待办与已知问题」。

一句话：Django REST + SQLite（backend/）+ Vue 3 + ECharts（frontend/）的个人 A 股监控面板；权威代码只在根目录 backend/ 与 frontend/；发版用 git 标签（当前版本以 `git describe --tags` 为准，文档不写死）；数据只来自真实上游，绝不伪造。
