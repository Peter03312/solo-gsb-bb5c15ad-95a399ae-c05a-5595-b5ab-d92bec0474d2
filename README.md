# Alias Router — 邮件投递前别名路由判定服务

基于 **Python + FastAPI + SQLite** 的 HTTP API：维护一份路由草稿（精确地址规则 + 域兜底规则），
发布为不可变版本后，对给定收件地址做逐跳别名展开，返回去重后的终点邮箱及各自到达路径。

## 功能要点

- **规则模型**：每条规则匹配一个完整邮箱地址（`exact`）或整个域（`domain`），动作二选一：
  - `reject`：拒收（不可带目标）；
  - `route`：指向若干目标，目标分为 `alias`（内部别名，继续逐跳解析）与 `mailbox`（终点邮箱，不再递归）。
- **解析语义**：每一跳先查精确规则、再查域兜底；展开所有分支后按终点去重，返回每个终点的全部到达路径。
  任一分支**拒收 / 无路由 / 成环** → 整次解析失败（422），不返回部分结果。
- **草稿编辑**：整体替换，必须携带所见修订号（乐观锁），冲突返回 409。
  无效地址、重复规则、空目标（`route` 无目标 / `reject` 带目标）在编辑期即拒绝（422）。
- **发布**：在单个事务（`BEGIN IMMEDIATE`）内原子核对修订号，并校验**所有规则目标可解析且无环**；
  成功则生成不可变版本，失败则保持旧版本不变。
- **历史版本**：所有发布版本永久可查，按任意历史版本解析可复现当时的结果。
- **发布前预览**：`POST /preview` 在同一只读快照内对比当前草稿与某个已发布版本，
  列出投递结果有变化的入口地址与域兜底类别（附两侧完整结果）；只读，不改动草稿或历史版本。
- **默认投递策略（灰度）**：管理员用 `PUT /policy` 提交**基线版本、候选版本、候选流量比例（整数 0～100）
  与所见策略修订号**；两个版本都必须是已发布版本，在单个 `BEGIN IMMEDIATE` 事务内核验修订号并保存，
  修订号冲突（409）或引用未发布版本（422）都不会覆盖原策略。
  `POST /deliver` 只输入收件地址：按**规范化地址稳定分桶**（SHA-256，仅依赖地址本身），
  同一地址在同一策略下总选同一版本；`0%` / `100%` 分别全选基线 / 候选。
  选中版本中的**拒收 / 无路由 / 成环仍整次失败，不自动改用另一版本**；
  未配置策略时明确报错（409），**不使用草稿或最新版本兜底**。
  策略只决定“默认用哪个已发布版本”，不影响显式版本解析，草稿与历史版本也不会被策略改写。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `0.0.0.0:8000`，SQLite 数据落在命名卷 `router-data`（容器内 `/data/router.db`），
首次启动自动建表，无需手工初始化。

也可以只用 Docker：

```bash
docker build -t alias-router .
docker run -p 8000:8000 -v router-data:/data alias-router
```

## 本地开发启动

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 默认库文件 ./router.db
pytest tests/ -q                                   # 运行测试
```

## 配置（环境变量）

| 变量          | 默认值（容器内）     | 说明                 |
| ------------- | -------------------- | -------------------- |
| `ROUTER_DB`   | `/data/router.db`    | SQLite 数据库文件路径 |
| `ROUTER_HOST` | `0.0.0.0`            | 监听地址             |
| `ROUTER_PORT` | `8000`               | 监听端口             |

地址与端口：`http://localhost:8000`（Compose 已映射 8000:8000），交互式文档见 `http://localhost:8000/docs`。

## API 一览

| 方法 | 路径              | 说明                                   |
| ---- | ----------------- | -------------------------------------- |
| GET  | `/health`         | 健康检查                               |
| GET  | `/draft`          | 查看草稿（含当前修订号）               |
| PUT  | `/draft`          | 整体替换草稿，需携带所见修订号         |
| POST | `/publish`        | 核对修订号 + 全量校验后发布不可变版本  |
| GET  | `/versions`       | 列出全部已发布版本                     |
| GET  | `/versions/{n}`   | 查看某个历史版本的完整规则             |
| POST | `/resolve`        | 按指定版本解析收件地址（显式版本，兼容） |
| POST | `/preview`        | 预览当前草稿相对基线版本的投递变化     |
| GET  | `/policy`         | 查看当前默认投递策略（未配置时 404）   |
| PUT  | `/policy`         | 配置基线/候选版本与候选比例，需策略修订号 |
| POST | `/deliver`        | 默认投递：按策略对收件地址稳定分桶解析 |

## 调用示例

### 1. 编辑草稿（当前修订号 0）

```bash
curl -X PUT http://localhost:8000/draft -H 'Content-Type: application/json' -d '{
  "revision": 0,
  "rules": [
    {"match_type": "exact", "match_key": "boss@corp.example.com", "action": "route", "targets": [
      {"kind": "alias",   "value": "assistant@corp.example.com"},
      {"kind": "mailbox", "value": "boss-backup@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "assistant@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "alias",   "value": "team@corp.example.com"}
    ]},
    {"match_type": "exact", "match_key": "team@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "mailbox", "value": "bob@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject"},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "catchall@mail.example.com"}
    ]}
  ]}'
# => {"revision": 1, "rules": [...]}
```

修订号过期时返回 `409`：`{"detail": {"error": "revision_conflict", "current_revision": 1, ...}}`。

### 2. 发布（原子核对修订号 + 校验目标可解析、无环）

```bash
curl -X POST http://localhost:8000/publish -H 'Content-Type: application/json' -d '{"revision": 1}'
# => {"version": 1, "source_revision": 1, "rule_count": 5}
```

校验失败（如目标别名无路由、成环）返回 `422` 且旧版本不受影响：

```json
{"detail": {"error": "validation_failed", "problems": [{"seed": "...", "error": "cycle", "chain": [...]}]}}
```

### 3. 解析收件地址

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com", "version": 1}'
```

```json
{
  "address": "boss@corp.example.com",
  "version": 1,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "alice@mail.example.com"],
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "alice@mail.example.com"]
    ]},
    {"mailbox": "bob@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "bob@mail.example.com"]
    ]},
    {"mailbox": "boss-backup@mail.example.com", "paths": [
      ["boss@corp.example.com", "boss-backup@mail.example.com"]
    ]}
  ]
}
```

域兜底（无精确规则时）：`nobody@corp.example.com` → `catchall@mail.example.com`。

任一分支失败则整次失败（`422`，无部分结果）：

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com", "version": 1}'
# => {"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": [...]}}
```

`error` 取值：`rejected`（命中拒收规则）、`no_route`（别名无路由）、`cycle`（成环）。

### 4. 历史版本查询与复现

```bash
curl http://localhost:8000/versions        # 全部版本
curl http://localhost:8000/versions/1      # 某版本的完整规则快照
```

草稿后续修改、发布新版本后，仍可用 `"version": 1` 调 `/resolve` 复现当时的判定结果。

### 5. 发布前预览：草稿 vs 基线版本

管理员在发布前用 `POST /preview` 对比**当前草稿**与某个**已发布版本**的投递差异。
请求携带基线版本号与所见草稿修订号：

```bash
curl -X POST http://localhost:8000/preview -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "revision": 2}'
```

服务在同一只读事务快照内取出两份规则集（不改动草稿或历史版本），沿用与 `/resolve`
相同的精确优先、逐跳展开、整次失败语义做全量对比，只列出**结果有变化**的项：

- `changed_addresses`：两侧全部**精确规则地址与别名目标**（并集）中结果有变化的地址。
  别名下游的变化会追溯到受影响的入口——例如 `team@` 改投后，`boss@`、`assistant@`
  这些经由它的入口也会列出。每项附 `base` / `draft` 两侧的完整结果：
  成功为 `{"status": "ok", "endpoints": [...]}`（形状同 `/resolve` 响应），
  失败为 `{"status": "failed", "error": {...}}`（形状同 `/resolve` 的 422 详情）。
- `changed_domain_fallbacks`：两侧任一处出现过域规则的每个域，排除上述具体地址后的
  **兜底地址类别**。每类用一个避开两侧精确地址与别名目标的合成示例地址
  （如 `_preview-sample@corp.example.com`）代表，同样附两侧完整结果。

比较时**路径排列顺序不计差异**（按路径集合比较），仅终点集合、到达路径或
拒收 / 无路由 / 成环结果不同才列出；无变化时两个列表均为空。
草稿尚未通过发布校验（如有环、目标无路由）时预览仍正常返回，对应入口展示失败结果。

```json
{
  "base_version": 1,
  "revision": 2,
  "changed_addresses": [
    {
      "address": "team@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "alice@mail.example.com", "paths": [["team@corp.example.com", "alice@mail.example.com"]]}, {"mailbox": "bob@mail.example.com", "paths": [["team@corp.example.com", "bob@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "carol@mail.example.com", "paths": [["team@corp.example.com", "carol@mail.example.com"]]}]}
    }
  ],
  "changed_domain_fallbacks": [
    {
      "domain": "corp.example.com",
      "sample": "_preview-sample@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "catchall@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "new-catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "new-catchall@mail.example.com"]]}]}
    }
  ]
}
```

错误契约与既有接口一致：修订号过期返回 `409`（`revision_conflict`，附当前修订号），
基线版本不存在返回 `404`（`version_not_found`）。

> 上例为节选：该场景下 `boss@`、`assistant@` 等经由 `team@` 的入口也会一并出现在
> `changed_addresses` 中。

## 默认投递策略：已发布版本间的灰度

管理员可以让某个**已发布候选版本**按流量比例可控地成为默认投递结果，基线版本承接其余流量。

### 6. 配置策略（基线 / 候选 / 比例 / 策略修订号）

```bash
curl -X PUT http://localhost:8000/policy -H 'Content-Type: application/json' -d '{
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "revision": 0
}'
```

- 只接受**已发布版本**：`baseline_version` / `candidate_version` 任一不存在（或指向草稿）→
  `422 {"error": "version_not_published", ...}`，事务回滚，原策略不变。
- `revision` 为客户端**所见策略修订号**；从未配置时修订号为 `0`，每次成功更新 `+1`。
  修订号过期返回 `409`（`revision_conflict`，附 `current_revision`），冲突不覆盖原策略。
- `candidate_percent` 为整数 `0～100`（请求体层强制），越界返回 `422`。

成功响应即当前策略：

```json
{"revision": 1, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 20, "updated_at": "2026-10-04T01:16:03+00:00"}
```

查看当前策略：`GET /policy`（未配置时 `404 {"error": "policy_not_configured", "revision": 0}`）。

策略修订号与草稿修订号相互独立；改策略不会修改草稿、历史版本，也不会让任何已发布版本被改写。

### 7. 默认投递：只给收件地址

```bash
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com"}'
```

服务先把地址规范化（去空白、小写），再对地址本身做 SHA-256 稳定分桶（桶号 0–99）：
桶号 `< candidate_percent` 选候选，否则选基线。**同一地址在同一策略下永远选同一版本**；
调整比例只会移动分桶边界（地址随比例单调地从基线侧进入候选侧），不会来回抖动。
`0%` 时所有地址选基线，`100%` 时所有地址选候选。

```json
{
  "address": "boss@corp.example.com",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ]
}
```

`endpoints` 的形状与 `/resolve` 完全一致（原有终点与到达路径）；`selection` 为
`"baseline"` / `"candidate"`，`version` 为实际选中的版本号。

失败约定：

- **未配置策略** → `409 {"error": "policy_not_configured", ...}`；不会使用草稿或最新发布版本兜底。
- **选中版本**解析出**拒收 / 无路由 / 成环** → 与 `/resolve` 相同的 `422`
  （`error` 仍为 `rejected` / `no_route` / `cycle`，附原有 `address`、`chain`），
  **整次失败，不自动改用另一版本**；错误详情额外附 `version`、`selection`、`policy_revision`。
- 无效地址 → `422 {"error": "invalid_address", ...}`。

```bash
# 100% 候选后，命中候选拒收规则：失败，不会退回基线
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com"}'
# => 422 {"detail": {"error": "rejected", ..., "policy_revision": 2, "version": 2, "selection": "candidate"}}
```

### 8. 显式版本解析保持兼容

`POST /resolve` 的请求（必须带 `version`）、成功响应与错误约定（`404 version_not_found`、
`422` 的 `rejected` / `no_route` / `cycle` / `invalid_address`）完全不变，不受任何策略影响；
草稿与历史版本同样不随策略变更而改写。需要强制指定版本复现结果时继续使用 `/resolve`。

## 项目结构

```
app/
  config.py    # 环境变量配置
  db.py        # SQLite 连接、建表、BEGIN IMMEDIATE 事务、只读快照事务
  routing.py   # 地址校验、规则集、逐跳解析、发布前全量校验
  traffic.py   # 默认投递的稳定分桶：规范化地址 → 0–99 桶 → 基线/候选
  preview.py   # 发布前预览：草稿 vs 基线版本的投递差异对比（纯内存，不落库）
  models.py    # 请求模型与编辑期校验（无效地址/重复规则/空目标）
  store.py     # 草稿、发布版本与默认投递策略的持久化、修订号核对
  main.py      # FastAPI 路由（/resolve、/preview、/policy、/deliver 等）
tests/         # pytest：API 全流程 + 解析单元测试 + 发布前预览 + 灰度策略
Dockerfile / docker-compose.yml
```
