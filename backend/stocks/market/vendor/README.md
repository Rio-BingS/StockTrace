# 第三方脚本（vendored）

## ths.js

- 用途：生成同花顺 `hexin-v` 反爬 token（`sectors._ths_token_headers` 调用其中的 `v()`）。
- 来源：akshare 1.18.64 `akshare/stock_feature/ths.js`（MIT License，© akfamily），原样复制，未改动。
- 为什么复制进仓库（DAT-17）：原先通过 akshare 私有函数 `_get_file_content_ths` 读取，
  akshare 升级改名/挪位置就会整条同花顺链路失效。
- 更新方式：同花顺 token 算法变化导致 403/空页面时，从新版 akshare 同路径复制覆盖，
  并在此处更新来源版本号。
