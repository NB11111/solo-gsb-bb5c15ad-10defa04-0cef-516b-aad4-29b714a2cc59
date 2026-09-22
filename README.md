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
服务不依赖数据库或外部服务；唯一的运行期状态是幂等提交的内存记录
（详见 [POST /audits](#post-audits)），因此容器固定以单 worker 运行。

## API

### GET /health

存活探针。返回 `200 {"status": "ok"}`。

### POST /audits

提交事件与约束进行核对。

#### 幂等提交（可选）

在请求头携带 `X-Idempotency-Key`（任意由客户端生成的非空字符串，去除首尾
空白后不超过 255 个字符、仅含可打印 ASCII），即可安全地重试，无需担心重复
求解或重复生成矛盾闭环：

- 服务端对**规范化后的 JSON 请求体**（递归按字典序排列对象键、去除多余空白，
  UTF-8 编码）计算 SHA-256 指纹；
- 同一 key + 同一指纹的并发请求或重试：**只执行一次求解**，其余请求回放首次
  请求的**完全相同的状态码与响应体**（包括 422 校验失败），并在响应头
  `X-Idempotency-Replayed` 中标记 `true`（首次请求为 `false`）；
- 同一 key 对应**不同指纹**：返回 `409`，且**不改变**已有的原结果；
  如需发起新请求，请更换新的 key；
- 不带 `X-Idempotency-Key` 时保持原有行为：每次请求照常求解，响应不带
  `X-Idempotency-Replayed` 头；
- 非法 key（空、超长、含非可打印 ASCII 字符、重复同名头）仍按现有 `422`
  规则处理；非法请求体同样仍是 `422`，且不会占用该 key。

幂等记录保存在**进程内存**中（默认最多保留 10 000 条，超出后淘汰最旧的已
完成记录），容器重启即清空，因此镜像与 compose 均固定 `--workers 1`，
保证同一容器内同键请求落到同一份记录。

```bash
curl -s -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: audit-20260922-0001' \
  -d '{"events":[{"id":"a","earliest":"2026-09-21T09:00:00Z"}],"constraints":[]}'
# 原样重试：状态码、响应体与首请求逐字节一致，X-Idempotency-Replayed: true
```

#### 请求体

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
- `kind` 未知，或使用了保留前缀 `@` 的 id；
- 携带了非法的 `X-Idempotency-Key`（空、超长、非可打印 ASCII、重复同名头）。

携带幂等键的请求，其 422 也会被记录：同键同内容重试回放原 422，响应头
`X-Idempotency-Replayed: true`。（请求体本身未通过 JSON/Pydantic 解析的
422 不占用 key，换用合法请求体可正常首次执行。）

#### 幂等冲突 `409`

同一 `X-Idempotency-Key` 被用于两份规范化指纹不同的请求体：

```json
{"detail": "幂等键 'audit-20260922-0001' 已用于另一份请求内容（指纹不一致），原结果保持不变；如确需发起新请求，请更换新的幂等键。"}
```

原结果不受任何影响，使用原请求体 + 原 key 仍可回放到首次结果。

快速验证：

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"id":"a","earliest":"2026-09-21T09:00:00Z"},{"id":"b","latest":"2026-09-21T08:00:00Z"}],"constraints":[{"kind":"at_least","before":"a","after":"b","gap":"PT2H"}]}'

# 幂等提交：第二次（重试）返回逐字节相同的结果，且只求解一次
curl -s -i -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: demo-key-1' \
  -d '{"events":[{"id":"a"}],"constraints":[]}'
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
  main.py          FastAPI 入口（/health、/audits）、幂等头与 OpenAPI 元数据
  schemas.py       请求/响应模型与时间、时长校验
  service.py       引用/重复校验、求解编排、矛盾闭环渲染
  solver.py        差分约束图、Bellman-Ford、负权环还原、收紧界
  idempotency.py   幂等键校验、规范化 JSON 指纹、并发下单次执行与结果回放
  timeutils.py     RFC 3339 UTC 与 ISO 8601 duration 的整数解析/格式化
tests/             单元、接口、幂等并发与随机穷举对照测试
Dockerfile         依赖随镜像安装，非 root 运行，内置 HEALTHCHECK，固定单 worker
docker-compose.yml
```

运行测试（需安装开发依赖）：

```bash
pip install -r requirements-dev.txt
pytest -q
```
