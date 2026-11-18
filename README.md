# 防汛雨量数据平台

把雨量站主、备翻斗计的**累计脉冲计数**转换为可信的 **UTC 小时降雨量**，供预警模型取数。
Flask + SQLite，网页上传/查询 + 等效 JSON API，Docker Compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库文件保存在命名卷 `rainfall-data`（容器内 `/data/rainfall.db`），重启后数据仍可查询。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
python -m app.app                 # 开发服务器，监听 8000
# 或生产形态（与 Docker CMD 相同）：
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 "app.app:create_app()"
```

运行自测（36 个用例）：

```bash
python -m unittest discover -s tests -v
```

## CSV 格式

列顺序：`站点, 主备标记, UTC整分钟, 计数(0~9999), 修订号`；表头可选（支持中英文列名），无表头时按此顺序解析。

```csv
station,device,minute,counter,revision
ST01,primary,2026-11-17T10:00:00Z,9980,1
ST01,backup,2026-11-17T10:00:00Z,120,1
```

- **站点**：非空字符串（≤64 字符）。
- **主备标记**：`primary`/`backup`，也接受 `主`/`备` 等别名。
- **UTC 整分钟**：如 `2026-11-17T10:00:00Z`、`2026-11-17 10:00`；未带时区按 UTC 处理，带偏移会换算成 UTC；秒必须为 00。
- **计数**：整数 0~9999。
- **修订号**：非负整数。同一（站点, 设备, 分钟）**只接受更高修订号**，否则该行被拒绝并记入批次报告。

示例文件：`examples/sample.csv`（含完整小时、备计、回绕、缺口四种情形）。

## 计算规则

- 每小时 = 60 个相邻分钟间隔（整点 H:00 至下一整点，共 61 个端点）。
- 每个间隔：**主计两端齐全取主计差值，否则备计两端齐全取备计差值，绝不跨设备相减**；两端都无可用数据则该间隔缺失。
- 计数下降按一次 10000 回绕处理：差值 = (末 − 初) mod 10000。
- 每脉冲 0.2 mm；**任一间隔缺失则整小时不可供数**（`status=incomplete`，`rainfall_mm=null`），网页会标出缺口与每段来源。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 首页：上传、查询、站点与批次列表 |
| `POST /upload` | 网页上传（文件或粘贴文本），返回批次报告页 |
| `POST /api/upload` | 同上，JSON 返回 |
| `GET /report?station=..&hour=..` | 网页小时报告（雨量、缺口、每段来源） |
| `GET /api/hourly?station=..&hour=..` | 等效 JSON（与网页同一计算入口，结果一致） |
| `GET /history` | 网页历史回溯：按站点、UTC 整点小时、批次号查询 |
| `POST /history/enable` | 网页启用历史回溯（以当前有效读数建立起点） |
| `GET /api/history?station=..&hour=..&batch=..` | 历史回溯 JSON（与网页同一入口，结果一致） |
| `GET /api/history/status` | 历史回溯启用状态与起点批次号 |
| `POST /api/history/enable` | 启用历史回溯（幂等），JSON 返回 |
| `GET /api/stations` | 站点数据范围 |
| `GET /api/batches` | 上传批次列表 |
| `GET /batches/<id>` | 批次行级明细（接受/拒绝及原因） |
| `GET /api/health` | 健康检查 |

`hour` 需整点对齐，如 `2026-11-17T10:00Z`。预警模型取数时请检查 `status == "ok"` 再使用 `rainfall_mm`。

## 历史回溯（按批次复盘取数）

防汛数据员复盘预警模型取数时，需要回答“某次上传完成后，当时实际可用的小时雨量是多少”。历史回溯按
**站点 + UTC 整点小时 + 批次号**查询该批提交完成时的结果。

### 启用与起点

- 功能默认关闭。在 `/history` 页（或 `POST /api/history/enable`）启用：系统以**当前全部有效读数**
  在单个事务中建立起点快照，分配一个起点批次号 `baseline_batch_id`（本身可查询）。
- **启用前的旧批次明确无法回溯**：网页与 API 对 `batch < baseline_batch_id` 的查询一致返回
  409 与提示文案；未启用时任何历史查询返回 409。
- 启用是幂等的：重复启用不会重建起点，也不产生新批次。

### 快照与不可变性

- 启用后，每个被接受的修订批次在同一事务内向 `reading_history` 写入快照；**同批多次修订同键，
  以该批最终接受值为准**（每键每批仅一条）。
- 查询某批次时，对每个读数键取该批次及之前最近的一条快照值，还原“该批提交完成时”的 61 个端点，
  再走与实时查询**完全相同的 `compute_hour`**：主备取值、10000 回绕、缺口规则、每段来源展示一致。
- 快照行随批次提交后**永不更新或删除**：后续上传、失败回滚、进程重启都不会改写已提交批次的查询结果
  （重启持久化由挂载卷上的 SQLite 保证）。
- 全部行被拒的批次不产生快照键，对其查询等价于复现上一批的状态。

### 响应

`GET /api/history` 的数据体与 `/api/hourly` 同构（同样的 `status` / `rainfall_mm` /
`intervals` / `source_counts`），并额外带 `batch_id` 与 `history`（启用时间、起点批次、不可变说明）；
网页与 API 共用同一计算入口，返回一致结果。错误情形：

| 情形 | HTTP 状态 |
|---|---|
| 未启用 / 查询启用前旧批次 | 409 |
| 批次号不存在 | 404 |
| 缺少参数、小时非整点、批次号非正整数 | 400 |


## 一致性与可靠性设计

- **两小时一致**：小时雨量由 `readings` 表现算现算（无缓存），补传或修订整点读数提交后，前后两小时立即同时反映新值；网页与 API 共用同一计算函数。
- **历史不可变**：历史回溯启用后，被接受的修订按批写入不可变快照表 `reading_history`；已提交批次的查询结果不受后续上传、失败或重启影响。
- **原子性**：每批上传的读数、批次记录、行级明细、历史快照在**单个事务**中写入，任何失败整体回滚，不留半批新结果；启用起点同样是单事务（批次记录 + 全量快照 + 元数据）。
- **持久化**：SQLite（WAL 模式）落盘于挂载卷，重启后仍可查询。
- **并发**：单 gunicorn worker 多线程 + SQLite `BEGIN IMMEDIATE` 与 `busy_timeout`，写入串行化。

## 目录结构

```
app/
  core.py        # 纯逻辑：CSV 解析校验、小时雨量计算（不依赖 Flask/SQLite）
  db.py          # SQLite：模式、事务性批量入库、查询、历史快照与回溯
  app.py         # Flask 路由（网页 + API，含历史回溯）
  templates/     # 页面模板（含 history.html 历史回溯页）
tests/test_app.py  # 自测用例（核心计算、修订、事务、网页/API 一致、重启、历史回溯）
examples/sample.csv
Dockerfile / docker-compose.yml / requirements.txt
```
