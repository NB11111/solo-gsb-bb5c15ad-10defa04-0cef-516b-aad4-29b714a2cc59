# Timeline Audit Service（跨系统事件时间线核对）

用「差分约束」核对一组来自不同系统的事件记录能否**同时成立**：

- 每个事件给出最早 / 最晚可能时间；
- 事件之间可声明「A 比 B **至少**早多久」「A 比 B **至多**早多久」；
- 服务把它们统一为 `x[target] - x[source] <= w` 形式的差分不等式，
  用 Bellman-Ford 判断是否存在负权环：
  - **可行**：返回每个事件被所有约束收紧后的最早 / 最晚时间范围；
  - **不可行**：返回一条由**原始约束**组成的矛盾闭环（负权环），
    直接定位证据冲突。

时间一律使用 **RFC 3339 UTC**（如 `2026-09-21T08:00:00Z`），间隔使用
**ISO 8601 duration**（如 `PT30M`、`P1D`、`PT0.5S`，不支持年/月）。

## 单条命令启动（8000 端口）

```bash
docker compose up --build
```

启动后：

- 服务地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI 描述：<http://localhost:8000/openapi.json>

停止：`Ctrl+C`；后台运行：`docker compose up --build -d`；
清理：`docker compose down`。

> 不用 Docker 时（需 Python 3.11+）：
> `pip install -r requirements.txt && uvicorn app.main:app --port 8000`

## 配置

配置项很少，均通过环境变量提供，均有默认值：

| 环境变量    | 默认值 | 说明                                         |
| ----------- | ------ | -------------------------------------------- |
| `LOG_LEVEL` | `info` | uvicorn 日志级别（`critical/error/warning/info/debug/trace`） |

端口固定在容器内 `8000`，并在 `docker-compose.yml` 中映射到宿主机 `8000`。
如需改用宿主机其他端口，修改 compose 文件 `ports` 的左侧即可（如 `18000:8000`）。
服务无数据库或外部服务依赖；幂等记录保存在进程内存中，故容器以单 worker 运行
（幂等语义详见下文 [POST /audits](#post-audits)）。

## API

### GET /health

存活探针。返回 `200 {"status": "ok"}`。

### POST /audits

提交事件与约束进行核对。

请求体：

```json
{
  "events": [
    {"id": "login", "earliest": "2026-09-21T08:00:00Z", "latest": "2026-09-21T09:00:00Z"},
    {"id": "trade", "earliest": "2026-09-21T08:00:00Z", "latest": "2026-09-21T09:00:00Z"}
  ],
  "constraints": [
    {"kind": "at_least", "id": "min-gap", "before": "login", "after": "trade", "gap": "PT10M"},
    {"kind": "at_most", "before": "login", "after": "trade", "gap": "PT30M"}
  ]
}
```

- `events[].id`：必填、唯一，不能以保留前缀 `@` 开头。
- `earliest` / `latest`：RFC 3339 UTC，均可省略；支持纳秒小数秒。
- `constraints[].kind`：
  - `at_least`：`after` 比 `before` **晚至少** `gap`；
  - `at_most`：`after` 比 `before` **晚至多** `gap`。
- `gap`：ISO 8601 duration（周/天/时/分/秒，可带小数），非负。
- `before` / `after` 必须引用已声明的事件；约束 `id` 可选，用于在矛盾报告中定位。

#### 可行响应 `200`

`feasible: true`，`ranges` 给出每个事件收紧后的范围（无约束方向为 `null`）：

```json
{
  "feasible": true,
  "ranges": {
    "login": {"earliest": "2026-09-21T08:00:00Z", "latest": "2026-09-21T08:50:00Z"},
    "trade": {"earliest": "2026-09-21T08:10:00Z", "latest": "2026-09-21T09:00:00Z"}
  }
}
```

#### 不可行响应 `200`（语义上的核对结果，非请求错误）

`feasible: false`，`contradiction.steps` 是首尾相接的闭环，每一步都是一条
原始约束（`ref` 为用户提供的约束 id，或按序号生成的 `@constraint-N`；
事件自身的时间界显示为 `@earliest:<事件id>` / `@latest:<事件id>`）：

```json
{
  "feasible": false,
  "contradiction": {
    "steps": [
      {"kind": "at_least", "ref": "min-gap", "source": "trade", "target": "login",
       "detail": "事件 'trade' 比 'login' 晚至少 PT2H", "weight_ns": -7200000000000},
      {"kind": "latest", "ref": "@latest:login", "source": "@epoch", "target": "login",
       "detail": "事件 'login' 不晚于 2026-09-21T08:00:00Z", "weight_ns": 1772006400000000000},
      {"kind": "earliest", "ref": "@earliest:trade", "source": "trade", "target": "@epoch",
       "detail": "事件 'trade' 不早于 2026-09-21T09:00:00Z", "weight_ns": -1772010000000000000}
    ],
    "total_weight_ns": -3600000000000,
    "explanation": "闭环包含 3 条约束，沿环相加得到 0 <= -3600000000000 ns；右端为负数，没有任何时间赋值能同时满足，因此该组记录不可能同时成立。"
  }
}
```

每个 step 的 `target` 等于下一步的 `source`，把所有不等式沿环相加即得
`0 <= total_weight_ns`，而该值为负，故无可行赋值。`@epoch` 是内部参考点
（Unix 纪元，时间 0）。

#### 请求错误 `422`

以下情况返回 422（FastAPI 标准校验错误或纯文本 `detail`）：

- 事件 `id` 重复；
- 约束引用了未声明的事件；
- 时间不是 RFC 3339 UTC（非 UTC 时区、非法日期等）；
- `gap` 不是合法 ISO 8601 duration（含年/月、负数等）；
- `kind` 未知，或使用了保留前缀 `@` 的 id。

#### 幂等提交（`X-Idempotency-Key`）

对可能超时重试的客户端，可在请求头携带 `X-Idempotency-Key` 标识一次审计请求。
服务端先把请求体交给 Pydantic 校验，再对**规范化后的请求**计算 SHA-256 指纹：

- 指纹基于校验后的值（时间、间隔均已折算为纳秒整数），并按 JSON 键名排序序列化。
  因此下列写法差异视为同一次请求，重试互相命中：
  - JSON 键的先后顺序、空白；
  - `2026-09-21T08:00:00Z` 与 `2026-09-21T08:00:00+00:00`；
  - 等价值的时长写法，如 `PT1H` 与 `PT60M`。
- `events` / `constraints` 的**列表顺序参与指纹**（顺序不同即为不同请求）；
  多余的未知字段在 Pydantic 校验阶段即按 422 拒绝。

规则：

| 情形 | 响应 |
| --- | --- |
| 首次提交（key 不存在） | `200`，响应头 `X-Idempotency-Replayed: false`，正常求解 |
| 同 key + 同指纹重试（含并发） | `200`，`X-Idempotency-Replayed: true`，返回**字节级相同**的状态与结果；并发下只有一个请求真正求解，矛盾闭环只生成一次，后续全部重放 |
| 同 key + 不同指纹 | `409`，原结果不变；原请求仍可用相同请求体重放 |
| 求解过程中请求体触发业务 `422` | key 立即释放；客户端修正请求体后可用**同一个 key** 重新提交 |
| 未提供 key | 完全保持无幂等的原有行为，响应不带任何幂等头 |

`X-Idempotency-Key` 必须是 1–255 个可见 ASCII 字符（不含空格与控制字符），
非法 key 与非法请求体一样返回 `422`。成功响应会回显
`X-Idempotency-Key` 与 `X-Idempotency-Replayed` 两个响应头。

并发语义：同一 key 的并发请求在服务端串行化——首个请求成为 LEADER 进入求解，
其余请求等待；LEADER 完成后，同指纹者拿到同一结果并标记 `replayed: true`，
不同指纹者收到 `409`。幂等状态保存在进程内存中（无数据库依赖），
因此容器固定以**单 uvicorn worker** 运行（见 `Dockerfile` /
`docker-compose.yml`）；服务重启后幂等记录清空。水平扩展多实例时需要把存储
替换为 Redis 等共享介质。

快速验证：

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: audit-20260922-001' \
  -d '{"events":[{"id":"a","earliest":"2026-09-21T09:00:00Z"},{"id":"b","latest":"2026-09-21T08:00:00Z"}],"constraints":[{"kind":"at_least","before":"a","after":"b","gap":"PT2H"}]}'
```

## 原理

所有约束统一写成差分不等式（时间以纪元起纳秒整数表示，避免浮点误差）：

| 原始约束                       | 差分不等式                       | 有向边（权 w）              |
| ------------------------------ | -------------------------------- | --------------------------- |
| 事件 i 不早于 E                | `x[0] - x[i] <= -E`              | `i → @epoch`，权 `-E`       |
| 事件 i 不晚于 L                | `x[i] - x[0] <= L`               | `@epoch → i`，权 `L`        |
| after 比 before 晚至少 G       | `x[before] - x[after] <= -G`     | `after → before`，权 `-G`   |
| after 比 before 晚至多 G       | `x[after] - x[before] <= G`      | `before → after`，权 `G`    |

- 系统可行 ⇔ 约束图无负权环（Bellman-Ford，全部节点初值 0）；
- 可行时：`latest[v] = dist(@epoch, v)`、
  `earliest[v] = -dist(v, @epoch)`，不可达即该方向无界；
- 不可行时：沿前驱链还原负权环，环上的边逐条映射回用户输入的原约束。

## 项目结构与测试

```
app/
  main.py        FastAPI 入口（/health、/audits，幂等头与编排）
  idempotency.py 幂等键校验、规范化指纹（SHA-256）、进程内并发去重存储
  schemas.py     请求/响应模型与时间、时长校验
  service.py     引用/重复校验、求解编排、矛盾闭环渲染
  solver.py      差分约束图、Bellman-Ford、负权环还原、收紧界
  timeutils.py   RFC 3339 UTC 与 ISO 8601 duration 的整数解析/格式化
tests/           单元、接口与随机穷举对照测试
Dockerfile       依赖随镜像安装，非 root 运行，单 worker，内置 HEALTHCHECK
docker-compose.yml
```

运行测试（需安装开发依赖）：

```bash
pip install -r requirements-dev.txt
pytest -q
```
