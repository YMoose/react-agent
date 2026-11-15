# 外部多 Agent 研究启示录（external-harness-learnings）

> 配套理论与指标规范见：[react-mode-eval.md](react-mode-eval.md)
> 配套实测基准见：[react-mode-eval-benchmarks.md](react-mode-eval-benchmarks.md)
> 历史学习对象（dsh-superagent-prototype）见：[dsh-superagent-learnings.md](dsh-superagent-learnings.md)
>
> 定位：集中收录来自外部研究与社区实践的多 Agent Harness 启示。每条只记录为**候选研究方向与假设**，不改动现有提示词（`prompts/` 不随本文变化）；引用的外部数据均标注采集日期与样本量，防止把快照当定律。

**一句话总结**：截至 2026-11 的两份高质量外部证据——Google Antigravity `/teamwork` 的公开设计复盘、OpenCode 作者发起的子代理使用分布众包——共同指向：**多 Agent 编排的有效切分维度是"上下文生命周期与验证独立性"，而非"静态工具权限"**。前者我们已在 `Reasoner ↔ Actor` 中实践（有状态单 `actor` + 锚点打包 + 冷热轮转）；后者（干净上下文审计、按任务原型的细粒度路由）是值得受控实验验证的下一步。

---

## 1. 来源一：Google Antigravity `/teamwork`（2026-11-13）

原文地址：[Advancing science & engineering with Antigravity's `/teamwork`](https://research.google/blog/advancing-science-engineering-with-antigravitys-teamwork-feature/)（Google Research Blog，Vahab Mirrokni 等；Gemini 4 Argon/Carbon/Hydrogen）。

### 1.1 与我们框架的交叉印证（不新增行动项）

文章公开的设计动机与我们的假设逐条对上：

| Antigravity 的表述 | 我们的对应概念 |
|---|---|
| 长周期任务注意力分散 → **按构造切分上下文**（每个子 Agent 只拿到该步骤精确需要的文件与工具） | 出站锚点打包（goal/why + premises + `path:line`）与子会话上下文隔离（Cut A） |
| Coordinator 移交 **fresh successors**（干净上下文的新继任者） | 子会话饱和/空返回时的冷轮转（Cold Rotation） |
| 并行探索 + **专用证伪器**（Lean 检查器、未修改的原生测试集、硬件计数器），不信任自我汇报 | Cut B 验收证据（v3.8：`Status: Completed` 必须附验收证据） |
| 用过往轨迹做 Token 效率分析，"一个 harness 优化另一个 harness"（`/boost` 即其产物） | `react-eval` 闭环（Phoenix 遥测 → 分析器 → changelog） |
| 迁移场景 reward hacking（打桩依赖/篡改测试）→ 依赖编译门控 + 锁定原始测试集 | §6 审计护栏（可见性与任务条件对称、裁判校准） |

规模参考：Weil 猜想形式化约 180 万行 Lean / 28,800 次子代理调用；Courtade–Kumar 猜想 5,000 万行 Lean 验证；Zircon 微内核两周约 80 万行安全 Rust；机群内存节省超 1 PiB。说明"上下文经济学 + 硬证伪器"范式在极端长程任务上可扩展。

### 1.2 启示 A：验证阶段的上下文选择——Warm Reuse vs Clean-Context Critic（候选研究方向）

**事实**：Antigravity 的 critic/auditor 在"不带原生成者推理历史的干净上下文"（clean contexts without the original producer's reasoning）中挑战与验证结果。

**张力**：我们当前默认在验证/小修阶段热复用刚完成实现的同一 `actor` `sessionID`（降低 $\mathrm{RAR}$ / $C_{\text{reload}}$）；但当任务存在确认偏误或"打桩绕过依赖、弱化测试迁就坏实现"的 reward hacking 风险时，携带生成者推理链的上下文容易顺着既有盲区自圆其说（推高 $p_{\text{loss}} \cdot C_{\text{risk}}$）。

**候选路由规则（待验证，未进提示词）**：

- **客观机器门禁**（既有编译器、未修改的原生测试集、确定性脚本检查）：热复用 Warm `sessionID`，$\Delta C_{\text{reload}} \approx 0$；
- **逻辑/事实审计与防篡改验收**（代码/证明审查、核对事实验证、检查测试是否被弱化）：派发 Clean-Context Cold `actor`（只传产物路径、原始需求与锁定验收标准，不传生成过程），用可控读盘开销换独立证伪能力。

**验证方式**：在受控实验样本库（benchmarks `## todo` 第 3 条）中加"审计型任务"一组：同一缺陷集分别由 warm 会话与 cold 干净会话验收，比较缺陷检出率与 Token 成本。

### 1.3 启示 B：委派子模式的细粒度原型路由（Pattern Routing）（候选研究方向）

**事实**：`/teamwork` 把工作流表达为声明式自然语言模式并动态路由；其轨迹分析得出——紧耦合代码用串行"实现 $\to$ 验证 $\to$ 修订"循环（`/boost`）在 Token 效率上显著优于并行拆分，并行的价值在"多路独立候选 + 硬证伪器择优"。

**张力**：我们当前路由只有 Inline（单文件/零过滤产出）vs Delegate（搜索/多文件/构建测试）两档，Delegate 内部不区分串行精修与并行探索。

**候选子模式（待验证，未进提示词）**：

1. **串行精修（Sequential Solve-and-Verify）**：紧耦合模块修改，单泳道迭代 + 客观门禁，规避跨泳道同步税 $C_{\text{sync}}$ 与冲突返工 $C_{\text{rework}}$；
2. **并行探索 + 专用证伪器（Parallel Exploration + Dedicated Falsifier）**：高不确定性问题，多路冷会话并行，仅凭锁定测试集/检查器结果择优晋级；
3. **生成-审计解耦（Producer + Clean-Context Critic）**：高风险变更由生成会话与独立干净审计会话配对终态把关。

**验证方式**：受控实验中按"模块间耦合度"分组对照串行 vs 并行的净收益（$\mathrm{NetSaved}$ 口径）。

---

## 2. 来源二：OpenCode 子代理使用分布众包（2026-11-14）

来源：OpenCode 作者 thdxr 的 Gist 众包投票（[gist.github.com/thdxr/bd8a2ae3…](https://gist.github.com/thdxr/bd8a2ae3169e5fa92648f7eedef84c10)，标题 "opencode subagent usage: general vs explore"）。方法本身即启示：一段 441 字节的提示词，让用户的 Agent 只读查询本地 `~/.local/share/opencode/opencode.db`（`session_v2` 表，`parent_id IS NOT NULL`，近 30 天），标准化回帖 `general: X% / explore: Y%`。

### 2.1 数据快照（n=20，2026-11-14 采集）

- 中位数：`general` **82.6%** / `explore` 17.4%；均值 70.3% / 29.7%；
- 11/20 用户 `general` $\ge 80\%$；仅 3/20 以 `explore` 为主（其中 2 人 100% `explore`）；
- 评论区提醒的混淆变量：比例高度依赖主模型家族（有的模型几乎只调权限全开的 `general`，Claude 系更倾向 `explore`），单用户数字不能直接当偏好结论；样本为自愿回帖，存在自选择偏差。

### 2.2 启示 C：静态权限角色切分在实践中失效 → 单一有状态 `actor` 的再确认

**解读**：OpenCode 原生把子代理按静态工具权限拆为只读 `explore` 与全能 `general`。中位数 82.6% 说明模型与用户绝大多数绕过 `explore`。按我们的经济账这符合预期：

- "先 `explore` 侦察、再 `general` 修改"意味着两次跨边界通信税 $C_{\text{tx}}$ + 冷启动断代重读（`general` 进场重读 `explore` 已读文件，$\mathrm{RAR}$ 飙升）；
- 直接调 `general`（能看、能改、能验证）反而净收益更高——模型的"绕行"在经济上是对的。

**对照我们的设计**：`react` 模式收敛为单一有状态 `actor`——只读需求由 Reasoner 在派单提示里声明写边界（read-only），侦察 $\to$ 实现 $\to$ 验证在同一 `sessionID` 内热复用（$\Delta C_{\text{reload}} = 0$）；v3.7 起单已知文件读写干脆 Inline。该众包数据构成对这套设计的独立外部定性印证。

**候选研究方向**：在受控实验中量化"`explore` $\to$ `general` 链式派单 vs 单 `actor` 热复用"的 $\mathrm{RAR}$ 与 $\mathrm{NetSaved}$ 差异，把定性印证变成定量结论。

### 2.3 启示 D：Agent 可执行的众包遥测采集（方法论启示）

**解读**：该 Gist 展示了一种低成本遥测采集范式——把指标定义编译成一段 Agent 可执行的只读查询提示词，社区用本地数据即可产出可横向对比的标准化数字。这与我们"分析器 stdlib-only、SQLite/wire JSONL 直读、不绑定单一后端"的立场同源。

**候选研究方向**：把 react 模式的关键指标（派发触发率、$\mathrm{NCR}$ 分布、`net_saved < 0` 占比）做成同款"一段提示词 + 只读本地查询"的采集器，便于未来跨用户/跨模型收集对照数据。前置条件：口径先在受控实验（benchmarks `## todo` 第 3 条）里稳定下来，避免采集到不可比的数字。

### 2.4 工程备忘：`opencode.db` 是现成的会话血缘真源

`session_v2`（缺表时回退 `session`）含 `(id, parent_id, agent, time_created)`：当 Phoenix Span 缺父子关联时，本地 `opencode.db` 可作为 `opencode.session.parent_id` 的只读替补真源（与 `raw-requests/*.jsonl` 互补）。候选：给 `scripts/phoenix_subagent_usage.py` 增加可选的 `--lineage-db` 回退（低优先级）。

**本机数据点（2026-11-15 采集，`session_v2`，近 30 天）**：子会话共 129 个（父会话 43 个）——`actor` 111（**86.0%**）；旧 omo 多工种残留 explorer 5 / fixer 5 / designer 2 / observer 2 / oracle 2（合计 12.4%，早期预设时期）；原生 `explore` / `general` 各 1。即本机已基本收敛到单 `actor` 形态，与启示 C 的设计判断一致。

---

## 3. 汇总：候选研究方向清单

| # | 方向 | 来源 | 经济学映射 | 状态 |
|---|---|---|---|---|
| A | 验证上下文路由：机器门禁走 warm / 逻辑审计走 cold | Antigravity | $p_{\text{loss}} \cdot C_{\text{risk}}$ vs $C_{\text{reload}}$ | 候选，待受控实验 |
| B | 委派子模式路由：串行精修 / 并行+证伪 / 生成-审计 | Antigravity | $C_{\text{sync}}$、$C_{\text{rework}}$、$G_{\text{time}}$ | 候选，待受控实验 |
| C | 单 `actor` 热复用 vs 静态权限角色链式派单 | Gist 众包 | $C_{\text{tx}}$、$\mathrm{RAR}$ | 已是现设计，待量化自证 |
| D | "一段提示词"众包遥测采集器 | Gist 众包 | 方法论（§6 护栏） | 候选，前置=口径稳定 |
| E | `opencode.db` 血缘回退源 | Gist 众包 | 分析器健壮性 | 候选，低优先级 |

以上方向均不改动现有提示词；进入实施前须先通过 benchmarks `## todo` 第 3 条的可重复评估样本建立基线。
