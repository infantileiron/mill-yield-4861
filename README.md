# 青禾 · 磨坊出粉率分布复核

为青禾制粉厂的 3 条生产线提供原粮批次、检验记录、滚动出粉率索引、分布诊断和复核签核，质量分析员每天把原粮检验变成可追溯的交班结论。

## 真实业务场景

青禾质量组原来把供应商批次写进 `原粮到货-1115.xlsx`，检验员录入不同工作簿，分析员手算四分位数，负责人在群里确认。问题是样本与批次容易错位、计算口径随人员变化、签核意见无法回到当时的数据。本项目承接原粮入厂检验，输出带冻结样本和 SHA-256 的复核 CSV，供产线负责人安排复测。

- 批次台账：供应商、产线、吨数、日期、样本数，唯一业务编号。
- 检验录入：出粉率、水分、检验员、实际采样时刻，范围校验和重复编号阻断。
- 滚动出粉率索引：按实际采样时刻维护最近 24 小时窗口，各产线精确分位数与异常样本可随时查询；采样时刻未知的旧记录单列，不混入窗口。
- 分布诊断：两种受理方式并存——滚动索引诊断（冻结索引代与水位）与原全量诊断（冻结全部历史检验）。Redis 排队，独立 Python worker 计算，显示完整样本直方图、分位数、IQR 异常点和分组均值。
- 复核报告：负责人签核意见，分析员与观察员可下载含冻结样本与采样时刻的 CSV。
- JWT 三角色、写操作审计、每分钟请求限流、可恢复的错误提示。

## 账号与权限

密码均为 `Demo123!`。

| 角色 | 账号 | 权限 | 典型动作 |
|---|---|---|---|
| admin | admin@demo.local | 全部业务读写、索引运维与签核、审计读取 | 核对异常点并签核 |
| analyst | analyst@demo.local | 录入、建索引/追平、提交计算、读取和导出；不可签核 | 新增样本并发起复核 |
| viewer | viewer@demo.local | 只读与导出；后端拒绝业务写入、索引运维与任务提交 | 下载交班报告 |

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18200)、[后端 API 文档](http://localhost:19200/docs)。PostgreSQL 映射端口 17700；Redis 不暴露宿主端口。网络固定为 `10.253.101.0/24`，可通过 `NETWORK_SUBNET` 修改以避开本机大量 Compose 栈的默认网段池限制。

```sh
docker compose down -v --remove-orphans
```

此命令会清除此项目演示数据。普通重启使用 `docker compose stop` / `docker compose start` 保留命名卷。

## 技术与环境变量

| 部分 | 技术 |
|---|---|
| 前端 | 原生 ES modules / HTML / CSS，Nginx 1.27 |
| API | Python 3.11、FastAPI 0.115.6、SQLAlchemy 2.0.36 |
| 数据库 | PostgreSQL 16（`percentile_cont` 计算精确分位数） |
| 队列 | Redis 7、独立 Python worker 与心跳健康检查 |
| 认证 | PyJWT 2.10.1、PBKDF2-SHA256 密码哈希 |

`.env.example` 包含项目名、三个端口、数据库密码、JWT 密钥、演示 seed 开关、网络网段，以及滚动索引的窗口/迟到/构建超时/追平间隔。演示密码和 JWT 密钥仅用于本地可复现运行，部署时自行替换。命名卷分别保存数据库、Redis AOF 和 worker 结果文件。恰好五个服务均配置健康检查，worker 等待 API seed 完成。

## 滚动出粉率索引

### 数据与口径

- 检验记录保存**实际采样时刻** `sampled_at`（UTC）与**录入时刻** `recorded_at`。采样时刻可留空，表示从旧工作簿迁入、时间未知的旧记录。
- `yield_index_entries` 是检验记录的物化副本：采样时刻、产线、出粉率、水分、样本号、批次号、检验员。窗口查询、分位数、异常样本与诊断快照**只读索引**，不再回查 `inspections` 全表。
- 窗口固定取最近 `YIELD_INDEX_WINDOW_HOURS`（默认 24）小时。分位数与异常点口径与全量诊断一致：线性插值 `(n−1)p`、IQR 1.5 倍边界、样本标准差、0–100 固定桶。
- 迟到判定：`recorded_at − sampled_at > YIELD_INDEX_LATE_MINUTES`（默认 30 分钟）。迟到但采样时刻仍落在窗口内的检验会立即写入索引并计入 `late_count`；采样时刻已在窗口外的检验只保留原始留痕，接口返回 `indexed=out-of-window`。
- 采样时刻未知的旧记录单独统计（`legacy.count` / `by_line` / `samples`），不进入滚动分位数，也不进入索引诊断快照。

### 增量与过期

- 新检验在提交事务内直接写入当前 active 代，按 `(generation_id, inspection_id)` 唯一约束幂等。
- 后台 worker 每 `YIELD_INDEX_SYNC_SECONDS`（默认 15 秒）追平一次：只读取 `inspections.id > applied_through_id` 的新行，再按 `sampled_at < cutoff` 精确删除过期行。两者都走索引，不扫描全部历史；10 万条检验下追平为毫秒级。
- 索引写入失败不会阻断检验留痕：记录照常提交，接口返回 `indexed=deferred`，状态标为滞后，下一轮追平补齐。

### 构建、切换与故障

- 建立/重建索引会新建一代（generation），先复制快照点之前的窗口记录，再在窗口内做一次反连接补齐，然后**原子切换**为 active。构建期间旧代继续服务，页面显示「构建中」并标注滞后。
- 切换后的宽限期内每次追平都会再做一次窗口反连接，覆盖「提交晚于快照、切换后才可见」的并发录入；唯一约束保证重试不重复计数。
- 构建失败、追平失败或进程中断：旧代保持完整水位继续回答查询，状态变为「滞后」并给出原因；恢复后自动重试。已退休代只保留最近一代用于核对，更早的物化行在切换后清理，避免无限增长。
- 进程启动时把中断的构建标记失败（不删除旧代），并把未完成的复核任务重新排队。

### 诊断受理

- `POST /reviews/indexed` 在受理瞬间冻结：索引代 id、`applied_through_id` 水位、窗口起止、窗口内全部样本、旧记录条数、以及受理时是否滞后。之后样本过期、重建索引、追平都不改写已受理结果。
- 原 `POST /reviews` 保留：仍按提交时全量历史快照计算，`source=legacy`。两种诊断在列表、报告与 CSV 中都标明来源。
- 单次受理上限 `YIELD_INDEX_MAX_SNAPSHOT`（默认 5000 条）；超出时返回 422 并提示缩短窗口或按产线拆分，不做静默截断。

### 索引接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/index/status` | 水位、active/building 代、窗口/旧记录条数、滞后原因、待追平数量 |
| POST | `/index/build` | 建立或重建索引（异步，worker 执行）；构建中再次调用返回 409 |
| POST | `/index/sync` | 立即追平并清理过期；失败返回 503 且保留旧水位 |
| GET | `/index/window?line=&hours=` | 各产线窗口分位数、边界、直方图、异常样本与迟到数；不返回原始检验行 |
| POST | `/reviews/indexed` | 冻结水位与样本并受理索引诊断 |

## 数据与计算口径

固定随机种子 `3101` 生成 40 批原粮、每批 15 条检验，共 600 条样本。首批是 `QH-1115-A`，检验员林洁。每批首个样本作为采样时刻未知的旧记录，并构造一条迟到补录样本。`seed_demo` 按批次和样本自然键检查后插入；重复执行不增加行数，也不覆盖已有用户数据；首次 seed 后自动建立滚动索引。启动 `SEED_DEMO=true` 自动 seed，失败记录日志不阻塞进程。

出粉率范围 `(0,100]`，水分 `[0,30]`，NaN 与 Infinity 被拒绝，采样时刻不能晚于当前时间 5 分钟以上。数据均为模拟数值，没有外部生产数据依赖。复核按受理时快照计算，后续录入、过期或重建不会改变既有报告。

- 至少 5 个有效样本，否则返回 422，不提交任务。
- 将出粉率排序，对 `p=0.25/0.5/0.75` 使用位置 `(n−1)p` 的相邻值线性插值得到 Q1、中位数、Q3。
- `IQR=Q3−Q1`，异常点为严格小于 `Q1−1.5×IQR` 或严格大于 `Q3+1.5×IQR` 的记录。边界上的点保留为非异常。常量样本 IQR=0，等于常量的样本不标异常。
- 标准差使用样本标准差 `n−1`。直方图区间固定为 0–100、步长 5 个百分点，最后一桶包含 100；展示区域自动覆盖全部非空桶并留一个相邻空桶，完整 0–100 桶计数保存在 API 结果中。
- 分组按原粮产线计算 n、均值、最小、最大。异常点不从总体统计中移除。
- 结果带样本快照 SHA-256，同一冻结输入可复现。统计异常只是核对线索，不自动判废。

后台 worker 从 Redis 消费复核 id 与索引构建任务，更新 queued→running→completed/failed，写结果至共享 artifacts 卷。重启时会扫描未完成任务并重新排队。负责人仅能签核 completed 状态，签核后不可重复签核。CSV 导出包含状态、口径值、索引代/水位/窗口（索引诊断）、签核意见和所有冻结样本（含采样时刻与迟到标记）。

## 一天的工作剧本

1. 08:50 观察员登录查看 `QH-1115-A`，核对 15 个已有样本与各自采样时刻。
2. 09:00 分析员登记 `QH-1115-NEW`，供应商丰垦粮业，到货 28 吨。
3. 09:10 检验员林洁新增 `QH-1115-A-S16`，出粉率 73.20%、水分 12.80%，采样时刻设为当前；该样本自动进入滚动索引。
4. 09:20 录入重复编号应报冲突；出粉率 101% 被范围校验拒绝。
5. 09:25 发现一张 20 小时前的纸质检验单补录，采样时刻填过去、录入时刻为现在：写入索引并标记迟到。
6. 09:30 在分布诊断页查看各产线最近 24 小时分位数，建立「一线最近24小时复核」，受理时冻结索引代与水位。
7. 09:40 独立 worker 完成计算，分析员核对 IQR 边界和异常样本；原「11月15日产线出粉率复核」历史诊断仍在列表中。
8. 10:00 负责人切换 admin，记录复测安排并签核。
9. 10:20 观察员下载 `mill-yield-review-<id>.csv`，交给产线班长。
10. 次日 09:30 前一天的样本自然移出 24 小时窗口，索引自动清理；已签核报告的数字不变。

## 目录与本地开发

`backend/src` 按 routes/controllers/services/models/schemas/middlewares/utils/constants/config 分职责；`backend/src/services/rolling_index.py` 是滚动索引的构建、追平、查询与受理逻辑；`backend/src/config/migrations.py` 对既有库做幂等补列。`backend/scripts` 是幂等 seed；`backend/migrations` 记录 schema 约定，实际表结构由 SQLAlchemy `create_all` + 幂等补列应用。`backend/tests` 覆盖窗口口径、迟到、并发切换、故障滞后、恢复重试与 API 行为。`frontend/src` 按 api/stores/types/components/common/hooks/pages/router/utils/constants 分模块。

本地 Python 开发：安装 `backend/requirements.txt`，配置 `DATABASE_URL` / `REDIS_URL` / `JWT_SECRET`，在 backend 目录执行 `uvicorn src.main:app --reload` 与 `python -m src.worker`。运行测试先安装 `backend/requirements-dev.txt`，再执行 `python -m pytest tests`（用临时 SQLite 与 fakeredis，无需外部服务）；`test-artifacts/gb-3101/verify.sh` 另外跑一遍 10 万条检验的建索引/追平/过期耗时与查询计划。静态前端通过 Nginx `/api/` 代理后端无前缀路由，不需要 Node 构建。

共享枚举：`backend/src/constants/enums.py` 与 `frontend/src/constants/enums.js` 中分别定义角色 Role、复核状态 ReviewStatus、产线 LINES；UI 角色标签、后端鉴权及状态迁移遵守这些值。

## 范围与已知差异

小型项目仅支持逐条录入，不提供 Excel 导入和外部检验仪器同步。原始检验记录采用追加留痕模式，没有删除/覆盖接口；错误记录须重新录入并在复核意见说明。采样时刻可由录入人填写，系统只做「不得晚于当前时间」校验，不校验与纸质单据的一致性。索引按采样时刻滚动，不追溯修改已受理诊断。签核是本地数据库留痕，不是电子签章。演示时区在报告中显式标记 UTC。前端 Google Fonts 离线时回退宋体。

测试脚本、1440×900 页面截图与实测证据在源码外的 `test-artifacts/gb-3101/`。License：MIT。
