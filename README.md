# 组合电路冒险验证台 (Combinational Hazard Verification Bench)

本地运行的 Web 应用：导入**无环二值门级 JSON 网表**，为每个门设置整数
**最小/最大惯性延迟**，定义输入的初态、终态与一次跃迁窗口；在离散时间上
用 **z3 位向量 SMT 求解**判断指定输出能否违反**脉冲宽度**或**跃迁次数**约束。

- 存在风险 → 返回**可复现的最早反例**：各输入跃迁时刻、每个门实际选用的
  点火延迟、全部节点的 0/1 波形，以及从违规沿回溯到主输入的**竞争路径**。
  当同一离散时刻两个或更多扇入**共同触发**门输出沿（单独撤回任一扇入都
  不能恢复旧值，如 OR 同刻 0→1、AND 同刻 1→0）时，这些同刻扇入都会保留、
  分别递归到各自的主输入沿；窄脉冲的起止两条沿都给出完整、稳定去重的链
  （联合扇入在 hop 的 `joint` 字段标出），API 反例、存档记录与前端波形
  详情共用同一份链路数据。
- 安全 → 给出已覆盖时间、跃迁窗口、最长静态路径与**完全稳定界**，
  说明瞬态是否被完整覆盖。
- **修订行为等价审计** → 同名组合每次保存都会形成不可变修订快照；选择两个
  修订与一份输入跃迁/输出参数后，系统对两版展开电路同步枚举共享的输入跃迁
  时刻与各自允许的门惯性延迟，比较所有可行轨迹的输出波形。不等价时返回最早
  分歧时刻、两版波形、触发输入与两侧延迟选择；等价时返回覆盖边界；超规模或
  超时返回 `unknown`。记录按修订对与参数哈希去重，可查看、重放、导出。
- **反例驱动修复方案搜索（⑦）** → 从已保存的 **unsafe** 分析记录出发，
  在用户给定的门惯性延迟 / 输入跃迁窗口 / 输出脉冲约束**可调范围与改动预算**
  内，用 **Python + z3 做约束优化**枚举“对所有输入时刻和门延迟选择都安全”
  的候选，并用**既有验证器逐个独立复核**；返回至多 10 条按
  （改动数、总偏差、最早风险消除时间）排序的 **Pareto 方案**，附预算内
  最小改动证据、被消除的反例路径、未覆盖边界与 unknown 原因；方案可预览为
  **不覆盖原记录的新参数版本**，全程可审计 / 重放 / 导出（见第 11 节）。
- 超规模/超时 → 明确返回 **unknown（无法判定）**，不做含糊承诺。

分析记录按 **网表哈希 + 参数哈希** 去重保存在 SQLite，可随时回看。
前端为原生 JavaScript + Canvas：网表层级图可缩放/平移，波形可缩放并标注
违规时刻与门点火点。

## 层次化模块库与组合验证（⑤ 标签页）

在扁平门级网表之外，还可以登记**带唯一名称和版本的无环模块**：声明有序
输入/输出端口，网表由门与**模块实例**组成；再创建仅含模块实例的**组合
网表**，为每个实例绑定**已存在的模块版本**与逐端口映射。保存时服务强制
校验并拒绝：

- 端口缺失、未知端口、同一端口重复映射；
- 一个信号被多个源驱动、输入连到未声明信号、wire/输出无驱动源；
- 实例递归（模块引用图成环，含同包互引）与组合环（门+实例混合 DAG 检测）。

**模块版本一经登记不可修改**（只能登记新版本）；验证前将层次网表**确定性
展开**（稳定拓扑序 + 并查集合并端口别名，门命名为 `实例路径/局部门id`，
保留局部门延迟），再走同一套脉冲宽度/跃迁约束的 z3 流水线生成反例或安全
覆盖。`race_paths`、波形与记录节点同时显示**实例路径与端口映射**（如
`o0/y ← o0.y`、主输入叶子 `a ← i0.a ← o0.a`，门 hop 带 `模块@版本`）。
层次分析按“组合 + 展开网表哈希 + 参数哈希”去重存档，快照展开网表与各模块
版本；每次组合内容变化还会写入不可变的**修订快照**（原文、展开网表、模块
版本映射），供 ⑥ 页选择两版做行为等价审计。组合后续修订也可用 **重放**
复现旧分析。支持模块/实例/展开结果的 API、页面查看与整库 JSON 导入导出。
详见第 9、10 节。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后浏览器访问：

```
http://localhost:8000
```

- 端口可在 `docker-compose.yml` 的 `ports` 修改。
- 分析记录保存在命名卷 `hazard-data`（容器内 `/data/hazard.db`）。
- 规模上限与求解超时通过环境变量调整（见文末）。

停止：

```bash
docker compose down
```

## 2. 本地直接运行（无 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问 http://127.0.0.1:8000 。

运行测试：

```bash
pip install pytest httpx
pytest -q
```

测试套件包含一个**独立的枚举式 DFS 模拟器**（`app/verifier/sim.py`），
它与 z3 编码在 60+ 随机小电路上逐电路比对“是否违规 / 最早违规时刻”，
用于交叉验证求解器语义的正确性；另有面向**重汇聚、同刻多扇入共同触发
与零延迟门**的专项回归（`tests/test_race_paths.py`），检查竞争路径完整
到达主输入、窄脉冲起止双沿链路齐全、联合触发标记正确及重复运行稳定去重，
并对 80 个含零延迟/重汇聚的随机 DAG 做 z3↔事件模拟交叉测试。
`tests/test_hierarchy.py` 覆盖层次模块登记/版本不可变、端口缺失/重复/未知、
驱动冲突、组合环、实例递归、确定性展开（实例路径与局部门延迟、多次实例化、
三层嵌套）、反例节点的路径+端口映射标注、记录重放与整包导入导出（15 项）。

---

## 3. 使用流程

1. **① 导入与参数**：点“载入示例”、导入 `.json` 文件，或直接在两个文本框
   编辑网表与参数；也可用右侧表单勾选初/终态、填写窗口与输出约束后
   “生成参数 JSON”。
2. “校验网表”可先检查无环、信号引用与自动时间上界。
3. “运行验证”调用 z3 求解，自动跳到 **③ 波形与反例**：
   - 红色虚线标出最早违规时刻，红点是门的实际点火时刻；
   - 下方列出输入跃迁时刻、各门点火延迟、竞争路径；
   - 可“导出反例 JSON”。
4. **② 网表视图**：门为矩形（标注类型与 `[lo,hi]`），主输入为圆，
   被约束输出描金边；滚轮缩放、拖拽平移。
5. **④ 分析记录**：按哈希去重的历史结论，点“载入”恢复网表、参数与结果。
6. **⑦ 反例修复搜索**：在④或⑤里产生 **unsafe** 记录后，打开⑦页选择该源，
   勾选要调的门延迟 / 输入窗口 / 输出约束并填入**包含原值**的允许范围，
   设置最大改动数与最大总偏差预算，点“z3 约束优化搜索”；右侧查看至多
   10 条 Pareto 方案、预算内最小改动证据、已消除反例路径与未覆盖边界，
   可把任一方案“预览为新参数版本”（另存 safe 分析记录，不覆盖原记录），
   底部支持查看 / 重放 / 导出搜索记录（见第 11 节）。
7. **⑤ 层次模块库**：登记带版本的层次模块、创建模块实例组合、确定性展开
   并运行组合验证（详见第 9 节）。
8. **⑥ 修订等价审计**：同名组合保存出两个修订后，选择修订 A/B、填写输入
   跃迁与输出参数，同步枚举并查看/重放/导出行为审计（详见第 10 节）。

---

## 4. JSON 网表格式

```json
{
  "name": "可选的网表名",
  "inputs": ["a", "b"],
  "gates": [
    {
      "id": "z",
      "type": "and",
      "inputs": ["a", "b"],
      "delay": [1, 3]
    }
  ],
  "outputs": ["z"]
}
```

| 字段 | 说明 |
| --- | --- |
| `inputs` | 主输入信号名（字符串数组，至少 1 个，不可重复） |
| `gates` | 门数组；`id` 全局唯一且不能与输入重名 |
| `gates[].type` | `buf` `not`（一元）/ `and` `or` `xor` `nand` `nor` `xnor`（二元） |
| `gates[].inputs` | 扇入信号名；一元门 1 个、二元门 2 个，必须已声明 |
| `gates[].delay` | 整数惯性延迟区间 `[min,max]`；可写单整数或 `[d]`，缺省 `[1,3]`，要求 `0≤min≤max` |
| `outputs` | 要观测/约束的信号（输入或门 id），至少 1 个 |

网表必须**无组合环路**（拓扑校验，自环也算）。

### 分析参数格式

```json
{
  "initial": { "a": 1, "b": 0 },
  "final":   { "a": 0, "b": 0 },
  "windows": { "a": [2, 5] },
  "horizon": 12,
  "outputs": [
    { "name": "z", "min_pulse_width": 3, "max_transitions": 2 }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `initial` / `final` | 每个主输入的初态/终态值（0/1）；缺省初态为 0、终态同初态 |
| `windows` | 每个发生跃迁的输入给出 `[tmin,tmax]`：跃迁时刻 τ 在该闭区间内**任选整数**；`tmin==tmax` 表示时刻固定 |
| — | 初态≠终态的输入**必须**给窗口；初态=终态的输入（含未列出者）保持不变 |
| `outputs[].min_pulse_width` | 相邻两次输出跃迁的最小可接受间隔（格子数）；出现更窄脉冲即违规；省略则不检查 |
| `outputs[].max_transitions` | `t=0..H` 内输出跃迁次数上限；超过即违规（`0`=不允许任何毛刺）；省略则不检查 |
| `horizon` | 离散时间上界 H（检查 `t=0..H` 共 H+1 个格子）；省略时自动取 `最大窗口右端 + 各门最长延迟路径` |

`outputs` 条目也可只写信号名字符串（此时必须至少给一个约束字段才合法）。
每个被约束输出至少需要 `min_pulse_width`、`max_transitions` 之一。

---

## 5. 语义约定（重要）

时间离散为半开格子 `[t, t+1)`，`t = 0,1,…,H`：

- 输入在所选时刻 τ 取终态：`C(t)=after` 当 `t≥τ`。
- 每个门带整数惯性延迟 `[lo,hi]`。门的计算值 `V(t)=f(C_pred(t))`，
  实际输出 `C(t)` 仅在门“点火”时更新。
- 失配（`V≠C`）连续保持 d 个格子后，门可在延迟 d 点火，其中 `lo≤d≤hi`；
  失配在 `d<lo` 时消失则候选变化被**取消**（毛刺被惯性滤除）；
  到达 `d=hi` 仍失配则**必须点火**。
- `lo=0` 的门可在失配出现的同一格子点火，并沿拓扑序在该格级联。
- 每个候选输出变化的延迟独立在区间内取值；这正是“竞争”的来源。
- **脉冲宽度**按相邻跃迁的格子差计：宽度 `< min_pulse_width` 判违规
  （即宽度恰好等于阈值是安全的）。

求解策略（`app/verifier/z3verifier.py`）：信号用 1 位位向量、门直接映射
位运算；① 普通 Solver 判违规存在性（unsat 即安全）→ ② 增量 push/pop
二分最早违规时刻 → ③ 固定最早时刻后用 Optimize 最小化输入跃迁时刻，
得到稳定可复现的反例。

### 竞争因果链（`counterexample.race_paths`）

从违规输出沿按门实际点火延迟定位**失配起点**，枚举让门输出恢复旧值的
最小变化扇入子集：

- 单子集即恢复（如重汇聚的某一条支路）→ 该扇入单独因果必需；
- 只有多个扇入联合才能恢复（同刻/同一失配窗口共同触发）→ 全部共同扇入
  保留并分别递归，hop 带 `"joint": [扇入...]` 标记；
- 递归到主输入时落到该输入**实际发生沿的时刻**，保证每条链都完整到达
  主输入，不会只显示门节点或出现空链。

窄脉冲按起止**两条输出沿**分别回溯；路径按沿时刻与链签名排序去重，
`race_path_count` 给出去重后总数，超过展示上限（200 条）时
`race_paths_truncated=true`。前端“③ 波形与反例”中：红色虚线为违规沿，
橙色虚线为窄脉冲另一条沿，下方“波形因果链详情”与 API、分析记录使用
同一份链路数据。

---

## 6. 规模上限与“无法判定”

默认上限（可用环境变量覆盖）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `HAZARD_MAX_GATES` | 200 | 门数上限 |
| `HAZARD_MAX_NODES` | 300 | 输入+门节点数上限 |
| `HAZARD_MAX_HORIZON` | 250 | 时间上界 H 上限 |
| `HAZARD_MAX_CELLS` | 40000 | 节点数 × (H+1) 上限 |
| `HAZARD_TIMEOUT_MS` | 25000 | 单次求解超时（毫秒） |
| `HAZARD_HIER_MAX_GATES` | 2000 | 层次网表展开后的门数上限 |
| `HAZARD_HIER_MAX_DEPTH` | 100 | 实例嵌套深度上限 |
| `HAZARD_AUDIT_MAX_GATES` | 40 | 每个修订展开后的门数上限（审计枚举器） |
| `HAZARD_AUDIT_MAX_HORIZON` | 120 | 等价审计时间上界 H 上限 |
| `HAZARD_AUDIT_MAX_INPUT_ASSIGNMENTS` | 10000 | 共享输入跃迁时刻组合上限 |
| `HAZARD_AUDIT_MAX_TRAJECTORIES` | 200000 | 每个修订允许枚举的门延迟轨迹上限 |
| `HAZARD_AUDIT_TIMEOUT_S` | 5 | 单次审计枚举超时（秒） |
| `HAZARD_REPAIR_MAX_GATES` | 60 | 修复搜索支持的门数上限 |
| `HAZARD_REPAIR_MAX_HORIZON` | 250 | 修复搜索时间上界 H 上限 |
| `HAZARD_REPAIR_SOLVER_TIMEOUT_MS` | 20000 | 修复搜索单次 z3 求解超时（毫秒） |
| `HAZARD_REPAIR_MAX_BAD_KEYS` | 200 | 每层最多剔除的不安全候选键数 |
| `HAZARD_REPAIR_MAX_SAFE_PER_LEVEL` | 30 | 每层保留的安全候选上限 |
| `HAZARD_REPAIR_MAX_VERIFICATIONS` | 30 | 交给既有验证器复核的候选数上限 |
| `HAZARD_DB` | `/data/hazard.db` | SQLite 路径 |

超过任一规模上限返回 `status="unknown", reason="scale_limit"`；
求解超时返回 `reason="timeout"`。安全结论中的 `coverage` 字段报告：
`covered_time`、`window_end_max`、`longest_static_path`、`settling_bound`
（= 最大窗口右端 + 最长静态路径）以及 `transient_fully_covered`；
当 `H < settling_bound` 时会明确提示 `t>H` 的行为未覆盖。

---

## 7. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/limits` | 当前规模上限与超时 |
| GET | `/api/samples` | 内置示例（网表+参数） |
| POST | `/api/validate` | 校验网表/参数，返回拓扑序、初态门值、自动 H |
| POST | `/api/analyze` | 执行验证并按哈希存档，返回结果与记录 id |
| GET | `/api/records` | 历史记录列表 |
| GET | `/api/records/{id}` | 单条记录（含网表、参数、完整结果） |
| GET | `/api/repairs/sources` | 可驱动修复的 unsafe 源记录（扁平+层次） |
| POST | `/api/repairs/search/{flat\|hier}/{id}` | 对某条 unsafe 记录发起修复方案搜索 |
| GET | `/api/repairs/records` | 修复搜索记录列表 |
| GET | `/api/repairs/records/{id}` | 单条修复搜索记录（含快照/方案/审计日志） |
| POST | `/api/repairs/records/{id}/replay` | 用记录快照与规范请求重放搜索 |
| POST | `/api/repairs/records/{id}/materialize` | 把某方案另存为新分析记录（不覆盖原记录） |
| GET | `/api/repairs/export` | 导出全部修复搜索记录 JSON |
| GET | `/api/repairs/limits` | 修复搜索规模/时间预算 |

`/api/analyze` 请求体：`{"netlist": {...}, "params": {...}}`。

### 层次模块库接口（前缀 `/api/hierarchy`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/modules` | 登记一个模块版本（name+version 唯一；不可修改，冲突返回 409） |
| POST | `/modules/validate` | 不落库校验单个模块（含递归检测） |
| GET | `/modules` | 模块版本列表（含端口/门/实例计数） |
| GET | `/modules/{name}/{version}` | 模块原文 JSON |
| POST | `/import` | 整包导入 `{modules:[...], compositions:{...}}`（同版本内容相同跳过，不同整包 409） |
| GET | `/export` | 导出整库 `{modules, compositions}` |
| PUT | `/compositions/{name}` | 创建组合；内容变化时替换并自增修订号，未变则保留 |
| POST | `/compositions/validate` | 校验组合文档（不落库） |
| POST | `/compositions/validate-flatten` | 保存前干跑：校验并返回展开结果 |
| GET | `/compositions` | 组合列表（含修订版本） |
| GET | `/compositions/{name}` | 组合原文与元数据 |
| DELETE | `/compositions/{name}` | 删除组合（分析记录保留） |
| GET | `/compositions/{name}/flatten` | 确定性展开：扁平网表 + 实例/节点/端口映射 |
| GET | `/compositions/{name}/revisions` | 列出该组合全部不可变修订快照 |
| GET | `/compositions/{name}/revisions/{revision}` | 查看单个修订（原文/展开/模块版本） |
| POST | `/compositions/{name}/audits` | 选择两个修订和参数，发起行为等价审计 |
| GET | `/audits` | 行为审计记录列表 |
| GET | `/audits/{id}` | 单条行为审计记录（含双修订快照与结果） |
| POST | `/audits/{id}/replay` | 用记录中的快照重放等价审计 |
| GET | `/audits/export` | 导出行为审计记录 JSON（含完整快照） |
| POST | `/compositions/{name}/analyze` | 对已保存组合执行验证并存层次分析记录 |
| GET | `/analyses` | 层次分析记录列表 |
| GET | `/analyses/{id}` | 单条层次记录（展开网表/参数/版本快照/结果） |
| POST | `/analyses/{id}/replay` | 用固定版本快照重放旧分析 |
| GET | `/samples` | 层次示例（`data/hier_sample_*.json`） |

## 8. 目录结构

```
app/
  models.py             # 网表模型、门求值、无环/拓扑校验
  params.py             # 初态/终态/窗口/输出约束解析
  verifier/z3verifier.py# z3 BV 编码与三阶段求解
  verifier/sim.py       # 枚举 DFS 参考模拟器（测试用）
  hierarchy/
    models.py           # 模块/组合解析：端口绑定、驱动唯一、组合环/递归校验
    flatten.py          # 确定性展开（实例路径、端口别名并查集、局部门延迟）
    store.py            # 模块/组合修订/层次分析/行为审计 SQLite 表
    service.py          # 登记/导入/保存/展开/验证/标注/重放/审计业务
    audit.py            # 修订行为等价审计：输入时刻与门延迟同步枚举
    routes.py           # /api/hierarchy 路由
  storage.py            # 扁平分析 SQLite 哈希去重存档
  repair/
    engine.py           # 反例驱动 z3 约束优化：枚举安全候选、Pareto、证据
    store.py            # 修复搜索记录 SQLite（源快照/请求/结果/审计日志）
    service.py          # 加载源、搜索、重放、导出、预览为新参数版本
    routes.py           # /api/repairs 路由
  main.py               # FastAPI 路由 + 静态托管
static/                 # 原生 JS + Canvas 前端（repair.js 为⑦页）
data/                   # 内置示例（hier_sample_* 为层次示例）与数据库
tests/                  # pytest（test_hierarchy.py 为层次模块回归）
docker-compose.yml / Dockerfile
```

---

## 9. 层次化模块库与组合验证

### 9.1 模块 JSON 格式

```json
{
  "name": "inv",
  "version": "1",
  "inputs": ["a"],
  "outputs": ["y"],
  "wires": [],
  "gates": [
    {"id": "y", "type": "not", "inputs": ["a"], "delay": [1, 3]}
  ],
  "instances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `name` / `version` | 唯一模块坐标；版本可写字符串/整数（`"1"`、`"2024r1"`），**登记后不可修改** |
| `inputs` / `outputs` | **有序**端口名数组，不可重复；输出端口引用一个已驱动信号（通常是同名门） |
| `wires` | 可选的内部信号声明；实例输出连到新名字时可隐式创建 |
| `gates` | 与扁平网表相同的门（含整数 `[lo,hi]` 延迟） |
| `instances` | 模块实例数组（见下） |

实例格式（`connections` 可用对象，或 `[["端口","信号"], ...]` 数组以便检测
重复映射）：

```json
{
  "id": "u1",
  "module": "inv",
  "version": "1",
  "connections": {"a": "a", "y": "n"}
}
```

- `module+version` 或 `module_ref: "inv@1"` 指定**已存在**的模块版本；
- 被引用模块的**每个**输入/输出端口必须恰好出现一次映射；
- 实例输出连到的信号由该实例独占驱动（多驱动、连到门/输入端口均拒绝）；
- 模块不能（传递）实例化自身——保存时做模块引用图 DFS 递归检测；
- 门与实例的混合信号依赖图必须无环（稳定 Kahn 序，检测组合环）。

### 9.2 组合 JSON 格式与展开

组合**只含模块实例**（不能直接放门），字段与模块体相同：

```json
{
  "name": "h_haz",
  "inputs": ["a"],
  "outputs": ["z"],
  "instances": [
    {"id": "i0", "module": "inv", "version": "1",
     "connections": {"a": "a", "y": "w"}},
    {"id": "o0", "module": "or2", "version": "1",
     "connections": {"a": "a", "b": "w", "y": "z"}}
  ]
}
```

确定性展开规则：

- 局部门全局名 = `实例路径/门id`（如 `i0/y`、`u/u1/y`），顶层输入保持原名；
- 端口与外部连接的 wire 是同一物理信号的别名，用**并查集**跨层合并，
  代表名优先取顶层输入/驱动门，结果完全确定、可复现；
- 局部门的 `[lo,hi]` 延迟原样保留；同一模块多次实例化互不干扰；
- 展开后的门级网表直接交给既有 `parse_netlist` + z3 流水线；
- 展开上限：`HAZARD_HIER_MAX_GATES`（默认 2000）、`HAZARD_HIER_MAX_DEPTH`
  （默认 100），超过拒绝展开。

`/flatten` 同时返回 `instances`（实例路径、`模块@版本`、完整端口映射）、
`nodes`（每个节点的实例来源与穿越边界 `crossings`）、`alias_index` 与
`output_mapping`（组合端口 → 扁平节点）。

### 9.3 层次验证参数与结果展示

参数与第 4 节一致，但 `outputs[].name` 写**组合输出端口名**（内部自动映射
到展开节点）；`initial/final/windows` 用顶层输入名。

反例/安全结论沿用第 5 节全部语义，并额外携带：

- `result.hierarchy.module_versions`：本次展开实际使用的模块版本快照；
- `result.hierarchy.instances`：实例路径与端口映射；
- `waves[].origin / crossings / route`：波形行的实例路径、`模块@版本`、
  局部门延迟与穿越的实例边界端口；
- `counterexample.race_paths[].hops[]`：每个门 hop 增加 `instance_path`、
  `instance_id`、`module`、`module_version` 与 `route`（从扁平节点到
  主输入沿途的 `实例.端口` 映射）；叶子节点给出主输入穿越的输入端口链；
  `joint` 共同触发扇入同样带 `joint_paths`；
- `chosen_delays_paths` / `node_paths`：门点火延迟表与输入跃迁的路径明细；
- `bad_output_port`：组合输出端口名（`bad_output` 为展开后的门 id）。

前端 **⑤ 层次模块库**页：登记模块、整包导入/导出、创建组合、干跑展开、
查看“实例树 / 展平网表 / 节点来源”表并运行验证；结果跳到 **③ 波形与反例**
（波形标签显示实例路径短名与模块版本，竞争链显示完整端口映射，页底另有
“层次信息”节点目录）。层次分析记录在 ⑤ 页底部“查看 / 重放”。

### 9.4 版本固定与旧分析重放

- 模块表只增不改：登记过的 `name@version` 永久指向同一份网表；
- 组合同名保存时，**内容真正变化**才让修订号 `version` 自增，内容未变
  则保留当前修订号（响应 `unchanged=true`）；每次新修订在同一事务中保存
  **组合原文 + 确定性展开网表 + 模块版本映射**，因此后续审计不依赖会变化的
  当前组合；
- 层次分析记录保存**展开网表 + 参数 + 各模块版本映射**的快照并按
  `(组合, 展开哈希, 参数哈希)` 去重；
- 即使后续组合已修订、只要快照模块版本仍在，`replay` 会重建等价展开并
  重新求解（带层次标注）；若组合已变化导致重展开与快照不一致，则回退到
  记录中存储的层次标注与结果，保证旧分析始终可复现。

### 9.5 页面操作流程

1. 打开 ⑤ 页，点“载入示例①”，把文本框内容“导入整包”（登记 inv@1、or2@1
   与组合 h_haz）；
2. 在右侧组合区“载入” h_haz，可先“校验+干跑展开”查看实例路径与节点来源，
   或直接“查看展开结果”；
3. 参数框已含示例参数，点“运行层次验证”→ 自动跳到 ③ 查看带实例路径/端口
   映射的波形、反例与竞争链；页底“层次信息”列出版本与节点目录；
4. ⑤ 页底部表可“查看”存档结果或“重放”固定版本的旧分析；

---

## 10. 组合修订行为等价审计（⑥ 页）

### 10.1 审计语义

同名组合保存为修订 1、2、3… 后，可任选两个**不同修订**与一份共享参数：

- 两个修订的输入/输出端口序列必须相同；输入跃迁时刻由两版**同步选择**
  （同一个 τ 组合同时送给两版）；
- 每个修订展开后的门仍独立遵守各自的 `[min,max]` 惯性延迟；枚举器遍历两版
  各自所有合法的门延迟选择；
- 对每个可行轨迹记录同名输出端口在 `t=0..H` 的完整 0/1 波形；
- 两版“输出波形集合”完全相同即 `equivalent`；否则为 `inequivalent`，返回
  最早分歧时刻、对应输出、两版波形、共享输入跃迁时刻以及两侧各门的
  `(点火时刻, 延迟)` 选择；
- 超过门数/时间/输入时刻组合/轨迹数预算或枚举超时返回
  `status="unknown"`，并给出原因和已枚举统计。

`params.outputs` 可写同名输出端口名数组，或带 `min_pulse_width` /
`max_transitions` 的对象；这些约束会随参数哈希保存并用于重放，但审计阶段
不会把违反约束的轨迹提前过滤掉——否则修订新引入的毛刺可能被排除，造成虚假
等价。`horizon` 省略时取两版自动稳定界中的较大值，保证默认覆盖完整瞬态。

### 10.2 API 发起审计

```http
POST /api/hierarchy/compositions/h_haz/audits
Content-Type: application/json

{
  "left_revision": 1,
  "right_revision": 2,
  "params": {
    "initial": {"a": 1},
    "final": {"a": 0},
    "windows": {"a": [2, 2]},
    "outputs": ["z"],
    "horizon": 12
  }
}
```

修订号顺序不影响结果：服务端会把较小修订号规范化为 `left`。同一
`(组合, 左修订, 右修订, 参数哈希)` 只保留一条审计记录，重复发起会更新该
记录并返回 `reused=true`。

结果要点：

```json
{
  "record": {"id": 1, "left_revision": 1, "right_revision": 2,
             "params_hash": "…", "reused": false},
  "result": {
    "status": "inequivalent",
    "earliest_divergence": {
      "time": 3,
      "left_output": "z",
      "right_output": "z",
      "left_value": 0,
      "right_value": 1,
      "trigger": {"input_transition_times": {"a": 2}},
      "delay_choices": {
        "revision_1": {"o/y": [{"t": 3, "delay": 1}]},
        "revision_2": {"i1/y": [{"t": 3, "delay": 1}],
                       "i2/y": [{"t": 4, "delay": 1}]}
      },
      "waveforms": {"revision_1": [{"output": "z", "edges": []}]}
    }
  }
}
```

等价时不返回 `earliest_divergence`，而返回：

```json
{"status": "equivalent",
 "coverage": {"covered_time": 12, "window_end_max": 2,
              "longest_static_path": 4, "settling_bound": 6,
              "transient_fully_covered": true}}
```

### 10.3 查看、重放与导出

| 操作 | 接口 |
| --- | --- |
| 列表 | `GET /api/hierarchy/audits` |
| 单条（含双修订原文、展开网表、模块版本、参数、结果） | `GET /api/hierarchy/audits/{id}` |
| 重放 | `POST /api/hierarchy/audits/{id}/replay` |
| 导出全部 | `GET /api/hierarchy/audits/export` |

单条 JSON 可直接用页面上的“导出”或浏览器/脚本保存；整表导出格式为
`{"format":"behavior-audit-records/v1","records":[...]}`。记录内已保存双修订
快照，后续组合继续修订也能查看与重放原审计。

### 10.4 页面操作

1. 在 ⑤ 页保存/导入同名组合的多个内容修订；
2. 打开 **⑥ 修订等价审计**，选择组合并点“载入修订”；
3. 在“修订 A / 修订 B”两个下拉框选择不同版本，确认或编辑参数；
4. 点“同步枚举并审计”：等价时查看覆盖边界，不等价时查看分歧竖线、两版
   波形、触发输入和门延迟；
5. 在底部表中“查看 / 重放 / 导出”单条记录，或“导出记录 JSON”批量导出。


---

## 11. 反例驱动修复方案搜索（⑦ 页）

### 11.1 思路与改动计量

从一条**已保存的 unsafe 分析记录**（扁平 `/api/records` 或层次
`/api/hierarchy/analyses`）出发，不改动原记录，只在用户给定的可调范围与预算内
寻找“修复后参数版本”。

一个**改动（change）**是一个被修改的**实体端点组**：

| 实体 | 计为 1 处改动 | 计入总偏差的端点 |
| --- | --- | --- |
| 门惯性延迟 | `[lo,hi]` 任一端点变化 | `|lo-lo0|+|hi-hi0|` |
| 输入跃迁窗口 | `[tmin,tmax]` 变化 | `|tmin-t0|+|tmax-t1|` |
| 输出约束字段 | `min_pulse_width` 或 `max_transitions` 之一变化 | 该字段的绝对差 |

**总偏差 deviation** 为各端点绝对差之和。可调范围必须**包含原值**（否则 400）；
搜索只允许缩小/放宽门延迟、移动已存在的窗口、放宽已有的输出约束。

### 11.2 安全语义与 z3 约束优化

候选参数被**钉死**后，输入跃迁时刻 τ 与每个门的点火选择仍都是自由变量
（即被全称量化）：复用第 5 节惯性延迟编码（`_encode` 的 `dyn` 模式把门延迟
端点、窗口端点、脉宽/跃迁阈值做成 z3 符号），当“存在违规轨迹”不可满足
（`unsat`）时，该候选才是**对所有 τ/门延迟都安全**。

搜索按改动数 k 从 1 逐层枚举：

1. **坏键剔除**：求解“存在违规”的候选赋值并逐条阻塞（k=0 即基线，
   源记录已确认 unsafe，直接判不可行，作为最小改动证据的下界）；
2. **安全候选**：对剩余赋值钉死后确认违规不可满足，得到 z3 证明安全的候选；
3. **既有验证器复核**：对 Pareto 前沿与“最小改动见证”调用原有
   `verify()` 独立复核——只有复核 `safe` 的方案才返回；
4. 取三维（改动数、总偏差、最早风险消除时间）互不支配的 **Pareto 前沿**，
   按 `(改动数, 总偏差, 最早风险消除时间)` 字典序返回**至多 10 条**。

“最早风险消除时间”取复核 safe 结论的完全稳定界
`settling_bound = 最大窗口右端 + 最长静态路径(hi)`，越小表示越早必然无风险。

### 11.3 最小改动证据 / 已消除路径 / 未覆盖边界 / unknown

- **预算内最小改动证据** `min_change_evidence`：给出最小可行改动数、被 z3
  严格证明 unsat 的更低改动层、最小改动层的最小总偏差与预算；低层穷尽
  时 `proof=complete`，搜索预算截断时为 `partial`。
- **已消除的反例路径** `eliminated_race_paths`：原 unsafe 结果的每条竞争路径
  都标注被哪类改动覆盖（链上被改门→`gate_delay`；叶输入窗口→`input_window`；
  否则放宽的违规输出约束→`output_constraint`）。
- **未覆盖边界** `uncovered`：某方案稳定界大于搜索 H 时列出
  （`t>H` 行为未覆盖）。
- **无法证明安全时**返回 `status=unknown` 并给原因：`solver_timeout`
  （z3 超时）、`verifier_unknown`（验证器对候选返回超时/规模 unknown）、
  `search_budget`（枚举/复核预算用尽）、`recheck_mismatch`（z3 判安全但
  验证器仍判违规）、`scale_limit`（超搜索规模）；预算内确无解时为
  `no_solution/budget_exceeded`。

### 11.4 API

```http
POST /api/repairs/search/flat/12
Content-Type: application/json

{
  "gates":   [{"id": "w1", "range": [0, 8]}],
  "windows": [{"name": "a", "range": [0, 6]}],
  "outputs": [{"name": "z", "min_pulse_width_range": [1, 8],
               "max_transitions_range": [0, 6]}],
  "budget": {"max_changes": 3, "max_total_deviation": 20}
}
```

- `source_kind` 为 `flat`（扁平记录）或 `hier`（层次分析记录，候选网表为其
  展开快照）；至少指定一个可调范围，预算为非负整数。
- 结果含 `status`、`plans[]`（每条带 `changes`/`total_deviation`/
  `risk_eliminated_at`/`change_detail`/`coverage`/`eliminated_race_paths`/
  复核后的安全网表与参数 `netlist`/`params`）、`min_change_evidence`、
  `unknown_reasons`、`uncovered`、`statistics` 与逐步 `audit_log`。
- 搜索记录按 `(源类型, 源 id, 规范请求哈希)` 去重 upsert；记录内嵌源
  网表/参数/原反例快照，组合后续变化也能**查看与确定性重放**
  （`replay_consistent` 比较两次方案签名）。
- `POST /records/{id}/materialize` 带 `plan_key` 把方案交给既有 `/api/analyze`
  流水线另存为一条**新的 safe 分析记录**，原违规记录保持不变（“预览为
  新参数版本”）。
- `GET /export` 返回 `{"format":"repair-search-records/v1","records":[...]}`。

### 11.5 页面操作

1. 先在①（或⑤）得到一条 **unsafe** 分析记录；
2. 打开 **⑦ 反例修复搜索**，选择“扁平/层次”源并点“载入反例”，页面列出
   全部门、已跃迁窗口与输出约束的原值；
3. 勾选可调项并填允许范围（默认范围已包含原值），设置最大改动数 / 最大
   总偏差，点“z3 约束优化搜索”（也可用“查看/编辑请求 JSON”直接粘贴）；
4. 右侧查看横幅结论、最小改动证据、按序排列的 Pareto 方案卡片（改动数、
   偏差、风险消除时间、具体端点变化、已消除路径归因、覆盖边界）；
5. 点卡片上“预览为新参数版本”另存新记录；在底部表中查看 / 重放 /
   导出单条，或导出全部搜索记录。
