# 多 Agent 协作模式研究与评估

本规范围绕“多 Agent 协作（如 `Reasoner ↔ Actor`）如何能真正优于单 Agent one loop 执行，以及如何用真实运行数据持续调优协作边界”这一核心问题，按以下**四步主线**组织：

1. **交互原语与单次边界模型（§1）**：先梳理不同 Agent 之间本项目使用的信息交互原语，并刻画单次跨 Agent 交互内部的三切面边界过程；
2. **上下文带宽经济学原理（§2）**：引入协作收益与摩擦成本的盈亏平衡不等式，从原理上回答“什么任务值得拆、什么摩擦会毁掉协作”；
3. **Agent 可观测遥测标准（§3）**：区分 OTel GenAI SemConv、OpenInference 与插件字段，明确观测值、估算值及其限制；
4. **分项展开、实测指标与范式映射（§4）**：将 §2 的经济学分项与 §3 的遥测数据逐项对接，在每个分项下统一给出**理论公式、实测计算指标与反馈优化判据**，并汇总协作范式示例。

> 本文的四族九原语、Three-Cut 和经济学公式是本项目的分析模型，不是 OTel 标准或已证明完备的行业分类。观测字段、推导公式和判据都应随源码与实测证据修订。

## 1. Agent 信息交互原语与边界模型

### 1.1 跨 Agent 信息交互原语

为表达业界 **多 Agent 通信与协作范式**，不同 Agent 之间的信息交互收敛为以下 **分析原语**（各范式与原语组合、经济学特征的逐项对照见 4.3 节）：

- **族 I：控制流调用与移交**
  - `CALL_SYNC`：阻塞等待子执行及结果。当前 `subagent` 的完成 wrapper 可作为同步返回证据；工具 Span 是否覆盖完整子执行窗需核查。
  - `CALL_ASYNC`：启动子执行后返回句柄，完成另行通知。当前宿主的 background 返回文本是识别线索。
  - `HANDOFF_TAIL`：模型中的尾部控制权移交；具体框架是否结束调用方、是否裁剪历史，需由适配层定义。
- **族 II：异步消息与事件**
  - `MSG_EMIT`：单播、广播或追加公共消息；当前例子是子结果通知进入父会话后续请求。
  - `MSG_CONSUME`：消费已就绪消息或结果，例如 `task_status(result=true)`；收到内容不证明模型已正确理解。
- **族 III：共享媒介读写**
  - `MEDIUM_MUTATE`：写共享状态、文件或 Git 产物，可从对应工具事件观测。
  - `MEDIUM_OBSERVE`：读共享状态、文件或差异，例如 `read/grep/git diff`。
- **族 IV：异常、仲裁与轮询**
  - `STATUS_POLL`：只查询未完成任务的状态；本项目希望减少可由通知替代的轮询。
  - `YIELD_OR_ABORT`：中途提问、回合末 Yielded、取消等治理行为；宿主状态与报告语义须分别记录。

### 1.2 单次跨 Agent 交互边界模型（Three-Cut Boundary Model）

聚焦到单次主从交互（第 $k$ 次派发），一个 Agent 从发起委托、子 Agent 内部执行、到结果回传被主 Agent 吸收的全过程，按分析目的划分为 **A / B / C 三个交互切面**：

```mermaid
sequenceDiagram
    autonumber
    participant R as 父会话 (Reasoner)
    participant A as 子会话 (Actor)
    participant M as 共享媒介 (File / Git / State)

    rect rgb(235, 245, 255)
    Note over R: 切面 A：Pre-Dispatch 派发前决策 (父推理步 R_k)<br/>• 派单动机思考 (reasoning)：为何派单而非内联？<br/>• 去程指令生成：产出派发规格 prompt
    end

    R->>A: CALL_SYNC / CALL_ASYNC / HANDOFF_TAIL (arguments.prompt)

    rect rgb(245, 245, 245)
    Note over A,M: 子会话执行窗 [A_k,1 ... A_k,last]
    A->>M: MEDIUM_MUTATE / MEDIUM_OBSERVE (write / edit / read / grep)
    M-->>A: 本地吞下原始工具输出与探索过程噪音
    opt 回合内治理与状态穿插
        R->>A: STATUS_POLL / YIELD_OR_ABORT (waiting_input + task_reply / task_cancel)
    end
    end

    rect rgb(255, 243, 224)
    Note over R,A: 切面 B：Execution & Dual-Layer Return/Yield 边界<br/>• 协议层状态 (Protocol)：completed | error | cancelled | running<br/>• 语义层终态 (Semantic)：Completed | Yielded (Needs Reasoning) | Blocked | Empty
    A-->>R: 返回/通知/拉取报告 (CALL_SYNC result / MSG_EMIT / MSG_CONSUME)
    end

    rect rgb(232, 248, 245)
    Note over R: 切面 C：Post-Return 接收吸收与重规划 (父消费步 R_consume)<br/>• 压缩报告注入父会话窗口<br/>• 验收与重规划思考 (reasoning)：采纳推进 / 因 Yielded 改计划 / 失真返工
    end
```

* **执行态与业务态双层分离（宿主执行成败 Protocol $\neq$ 任务目标达成 Semantic）**：
  * **协议层状态（`protocol`）** 来自宿主执行器（`completed` / `error` / `cancelled` / `running`）；
  * **语义层状态（`status`）** 来自子智能体报告体（`Completed` / `Yielded (Needs Reasoning)` / `Blocked`），以及分析器识别的空返回态 `Empty`。同步空结果且执行窗无子 LLM 活动时，脚本判 `FAILED_DISPATCH`；这不要求宿主已明确报告 completed，也不能从空内容独自确定失败原因。
  * **关键边界观测点**：当子智能体因前提矛盾或跨模块冲突主动刹车，返回 `protocol="completed"` 且 `status="Yielded (Needs Reasoning)"` 时，切面 C 中父智能体能否**不亲自读源码、仅凭文字报告一次改对计划**，可作为边界恢复质量的观测线索；当遭遇 `Empty` 静默空返回（原因需另行核实）时，切面 C 必须触发带锚点换仓冷启动（Session Rotation），禁止回退为父会话连续内联工具流。

## 2. Agent 交互的上下文带宽经济学原理

在明确了交互原语（§1.1）与三切面边界（§1.2）后，需要从 LLM 推理引擎的底层物理机制（Prefill/Decode 成本差异、缓存与多轮输入累积）出发，建立判断“内联自做（Inline）”还是“跨边界派发（Delegate）”的经济学原理。

### 2.1 第一层：顶层盈亏平衡判据（Top-Level Equilibrium）

当且仅当跨边界协作带来的**总收益 $G_{\text{collab}}$** 大于跨边界引入的**总摩擦成本 $C_{\text{collab}}$** 时，派发决策在经济学上成立：

$$\Delta V_{\text{collab}} = G_{\text{collab}} - C_{\text{collab}} > 0 \quad \Longleftrightarrow \quad G_{\text{collab}} > C_{\text{collab}}$$

### 2.2 第二层：收益与成本的构成分解（Dimension Decomposition）

将不等式两端按物理作用机制拆解为 **2 项互补收益（空间省窗口 + 时间省等待）** 与 **4 项全生命周期摩擦成本**（核算时需统一成本单位并避免重复计费，分项不天然互斥）：

$$\underbrace{G_{\text{ctx}} + G_{\text{time}}}_{G_{\text{collab}}\text{（协作总收益）}} \;>\; \underbrace{C_{\text{tx}} + C_{\text{state}} + C_{\text{gov}} + C_{\text{risk}}}_{C_{\text{collab}}\text{（协作总摩擦成本）}}$$

- **收益端 $G_{\text{collab}}$**：由 **主窗口上下文隔离降噪收益（$G_{\text{ctx}}$）** 与 **并发墙钟时间收益（$G_{\text{time}}$）** 构成；
- **成本端 $C_{\text{collab}}$**：由 **边界通信生成税（$C_{\text{tx}}$）**、**状态同步与冷启动税（$C_{\text{state}}$）**、**控制面轮询与验收税（$C_{\text{gov}}$）** 以及 **信息失真与返工期望成本（$C_{\text{risk}}$）** 构成。

> 遥测能提供 usage、时间和内容长度，但注意力收益、反事实内联成本及返工因果不能直接读取。这些公式是成本模型：时间、Token 和质量项须先换算到共同单位；当前脚本只实现其中部分代理指标。

## 3. Agent 可观测遥测契约

本项目使用 `openinference.span.kind` 的 `AGENT` / `LLM` / `TOOL` 分类；它们不是 OTel 原生 `SpanKind`（`INTERNAL` / `SERVER` / `CLIENT` / `PRODUCER` / `CONSUMER`）。以下区分标准属性与当前插件约定：

- `gen_ai.conversation.id`：GenAI 会话标识。插件也输出 `session.id` 供 OpenInference 使用；二者在本插件中的对应关系不是 OTel 强制等价关系。
- `gen_ai.operation.name`：`chat`、`execute_tool`、`invoke_agent` 是标准值；`compaction` 是插件自定义值。
- `gen_ai.usage.input_tokens`：包含缓存 Token 的总输入 usage。OTel 要求尽力使用供应商计数，必要时汇总或估算；它不是实际新计算的 Prefill 工作量。数值不经过内容字符串截断，但可能缺失或受上游计数口径影响。
- `gen_ai.usage.output_tokens` 与 `gen_ai.usage.reasoning.output_tokens`：后者应包含在前者中。在当前 OpenCode 插件中，输出总量由可见输出加 reasoning 得到，因此 $\mathrm{out}_{\text{vis}} = \text{output_tokens} - \text{reasoning.output_tokens}$；不应据此推断完整上下文占用。
- `gen_ai.usage.cache_read.input_tokens`：供应商报告的缓存输入 Token 数，其占总输入的比例是 usage 比率，不是直接测得的底层 KV Cache 命中率。
- `gen_ai.tool.name`、`gen_ai.tool.call.id`、`gen_ai.tool.call.arguments`、`gen_ai.tool.call.result`：工具身份、参数和结果。插件另有 `input.value` / `output.value` 镜像；内容可能受开关、脱敏和截断影响。Wire 中结果的结构随供应商而异，不能只检查 `role="tool"`。
- `gen_ai.output.messages`：插件输出 assistant 的 reasoning/text parts。它只含上游公开的 reasoning 文本，不是完整内部思考；插件选择将 reasoning 排在 text 前，工具调用另存 TOOL Span。这是实现选择，不是 SemConv 禁止 `tool_call` parts。当前 OpenCode 的 reasoning 事件已有 `ordinal`，插件尚未用它恢复交错顺序。序列化后截断还可能使该属性失去 JSON 结构，解析失败须显式标注。
- `opencode.agent`、`opencode.session.parent_id`：插件的角色与父子会话字段；`TOOL` 上的 `gen_ai.agent.name` 可表示被调目标角色。
- `opencode.wire.seq`、`input.value_original_length`：插件的 Wire 回源辅助字段。`tracing.contentMaxLength` 默认 61,440 字节（60 KiB），由插件执行截断，并非 Phoenix 固定上限。`src/wire.ts` 将同一 recorder 序号写入 JSONL 与 Span，但 recorder 重建会重置序号，不能跨启动周期只凭 `seq` 唯一关联。当前分析器的 `wire_body()` 仍有这个限制；通知扫描另按记录 `ts` 与 wrapper 次数匹配。
- Wire JSONL：只有启用记录、帧未被 `maxFrameBytes` 截断且未因读取/写入错误丢失时，才可用于完整回源。HTTP `ts` 在请求体读取完成时生成，不等于模型请求的精确启动时刻；按时间匹配也只是回退手段。

## 4. 模型的可观测参数展开与范式映射

有了 §1 的交互边界模型、§2 的经济学不等式与 §3 的 OTel 标准字段，现在将 §2 的分项映射为观测量及带假设的代理指标。

### 4.1 单次往返的基础物理量与双向边界效率比（一进一出对偶）

先按一次完整往返，定义基础物理量与两组边界效率比:
**“父派单（切面 A）$\to$ 子执行 $\to$ 子汇报（切面 B）$\to$ 父继续推理（切面 C）”**

**1. 单次跨边界往返的理论基础量**

以下量首先定义模型含义；实际脚本的近似口径另见 §4.2。

* **去程指令量 $T_{\text{dispatch}}$**：父会话在切面 A 生成派发指令（`prompt`）消耗的 Token 数；
* **子会话重取税 $T_{\text{reload}}$**：因去程信息传递不足，子会话进场后被迫重新读取/搜索主会话（或前序子会话）已知信息所浪费的 Token 数；
* **子会话原始工作量 $\Delta T_{\text{work}}$**：子会话在本地执行窗吞下的全部工具输出与中间推理上下文增量 Token 数；
* **回程汇报量 $T_{\text{report}}$**：子会话将执行过程压缩后回传、在切面 C 注入父会话窗口的报告 Token 数（在通用消息范式中即接收端载荷）；
* **计价与复利基准**：输入 Prefill 单价 $c_{\text{in}}$、输出 Decode 单价 $c_{\text{out}}$（取决于供应商定价与缓存计费，不能由物理机制直接推出固定倍率），以及父会话在当前任务片段（Episode）内还需继续推理的**剩余轮次 $N_{\text{rem}}^{\text{ep}}$**。

**2. 由基础量导出的双向边界效率比**

* **回程压缩效率（Child $\to$ Parent，用 $\Delta T_{\text{work}}$ 与 $T_{\text{report}}$ 相除，详见 4.2.1 `[M1]`）**：
  $$\mathrm{NCR} = \frac{\Delta T_{\text{work}}}{T_{\text{report}}} \quad\text{（上下文信噪压缩比）}, \qquad \sigma_{\text{leak}} = \frac{T_{\text{report}}}{\Delta T_{\text{work}}} = \frac{1}{\mathrm{NCR}} \quad\text{（上下文泄漏系数）}$$
  由此直接得到**单次替父窗口挡住的过程噪音净额**：$\Delta T_{\text{noise}} = \Delta T_{\text{work}} - T_{\text{report}} = \Delta T_{\text{work}}(1 - \sigma_{\text{leak}})$。
* **去程自足效率（Parent $\to$ Child，用 $T_{\text{reload}}$ 与 $T_{\text{dispatch}}$ 相除，详见 4.2.4 `[M2]`）**：
  $$\mathrm{RAR} = \frac{T_{\text{reload}}}{T_{\text{dispatch}}} \quad\text{（二次重取放大比）}, \qquad \mathrm{DSR} = \frac{T_{\text{dispatch}}}{T_{\text{dispatch}} + T_{\text{reload}}} = \frac{1}{1+\mathrm{RAR}} \quad\text{（派发信息自足率）}$$
  衡量父会话传给子会话的信息是否够用（$\mathrm{RAR}=0$ 即 $\mathrm{DSR}=100\%$，在理论定义中代表零重取；$\mathrm{RAR} \ge 1$ 代表子会话重取已知信息的代价已超过派发指令本身）。

### 4.2 分项展开

#### 4.2.1 主窗口上下文隔离降噪收益（$G_{\text{ctx}}$）

* **分项展开式与物理机制**：
  $$G_{\text{ctx}} = N_{\text{rem}}^{\text{ep}} \cdot \Delta T_{\text{noise}} \cdot c_{\text{in}} + \Delta A_{\text{attn}}, \qquad \Delta T_{\text{noise}} = \Delta T_{\text{work}} - T_{\text{report}} = \Delta T_{\text{work}}(1 - \sigma_{\text{leak}})$$
  * **线性累积乘子 $N_{\text{rem}}^{\text{ep}}$（片段内父会话剩余轮次）**：被挡在子会话内的过程噪音 $\Delta T_{\text{noise}}$ 省下的不是单次成本，而是避免在父会话后续 $N_{\text{rem}}^{\text{ep}}$ 轮推理中被反复作为输入；缓存可能降低实际计算和费用，不能按未缓存输入单价直接推算账单。乘子以**当前任务片段（Episode）**为界——终止于父会话下一次 `compaction`、轮间空闲间隔 $\Delta t_{\text{idle}} > \tau_{\text{ep}}$（默认 30 min）或会话结束（`compaction` / `idle_gap` / `session_end` 三种终止因），防止把数小时后无关任务的轮次虚记为本次隔离收益（实测 $k{=}1$ 全会话原始 $N_{\text{rem}}{=}46$，片段内 $N_{\text{rem}}^{\text{ep}}{=}8$，虚增 ~5.75×）。
  * **注意力保真收益 $\Delta A_{\text{attn}}$**：保持父窗口短小所免除的长上下文注意力稀释与 Compaction 摘要失忆损耗。
* **实测计量公式（`[M1]` 信噪压缩比与复利节省量 + `[M4]` 主脑峰值水位）**：
  * **报告注入量的上下文差分候选值**（$R_{\text{consume}, k}$ 为收到报告后的首个父 LLM Span，$R_{\text{prev}, k}$ 为其紧邻上一轮父 LLM Span）：
    $$T_{\text{report}, k} = \mathrm{in}(R_{\text{consume}, k}) - \mathrm{in}(R_{\text{prev}, k}) - \mathrm{out}_{\text{vis}}(R_{\text{prev}, k})$$
  * **子会话执行窗的 usage 差分代理量**（记子会话第 $j$ 轮末个 LLM Span 的脚本代理量为 $\mathrm{ctx}_j = \mathrm{in}(A_{j,\text{last}}) + \mathrm{out}_{\text{total}}(A_{j,\text{last}})$，其中 total 包含 reasoning，不能视作下一请求实际保留的上下文）：
    $$\Delta \mathrm{Ctx}_k = \max(\mathrm{ctx}_k - \mathrm{ctx}_{\text{before launch}}, 0)$$
    冷启动时基线为 0，故该值还包含系统提示与初始输入；compaction、历史裁剪和 reasoning 保留策略也会影响它。它不等于纯工具输出或净吸收的信息量。
  * 差分字段虽名为 `t_report_exact`，也可能混入其他工具结果、用户消息和宿主注入；负差分按分支丢弃或置零。当前污染门限为差分大于字符估计的 2 倍，且多出 1,500 Token，此时回退到 `t_report_est`；通过门限不证明归因精确。
  * **双口径 $\mathrm{NCR}$、累积节省代理量与主脑峰值**：
    $$\mathrm{NCR}_{\text{ctx}, k} = \frac{\Delta \mathrm{Ctx}_k}{T_{\text{report}, k}}, \qquad \mathrm{NCR}_{\text{char}, k} = \frac{\sum_{t \in \text{Actor TOOL}_k} \mathrm{len}(\text{result}_t)}{\mathrm{len}(\text{report}_k)}, \qquad \widehat{\mathrm{Saved}}_k = N_{\text{rem}, k}^{\text{ep}} \cdot \max(\widehat{T}_{\text{tool results}, k} - T_{\text{report,effective}, k}, 0)$$
    $$\mathrm{Peak}(R) = \max_{m \in \text{LLM}(R)} \mathrm{in}(m)$$
    当前 `Saved` 的实现与 `NCR_ctx` 分子不同：`raw_tok = int(actor_raw_result_chars / chars_per_token)`，`dt_noise_tokens = max(raw_tok - t_report_effective, 0)`，再乘片段内剩余轮次。默认 `chars_per_token=4.0`，2.35 是历史样本校准值；工具结果截断、重复文本和反事实执行差异都会影响估算。
* **反馈判据与演进方向**：
  * `HIGH_GAIN`（$\mathrm{NCR}_{\text{ctx}} \ge 8:1$）：脚本的高压缩比分档，不代表已证实总体经济收益；
  * `MODERATE_GAIN`（$2:1 \le \mathrm{NCR}_{\text{ctx}} < 8:1$）：中等压缩比分档；是否净获益仍取决于费用、剩余轮次和任务质量；
  * `MARGINAL_OVERHEAD`（$\mathrm{NCR}_{\text{ctx}} < 2:1$）：低压缩比分档，不能仅凭此证明派发净亏损；
  * **主脑输入告警目标**：如选用 40k 阈值，须同时报告首轮输入和后续增长；用户初始大输入不能归因于内联行为。
  * **演进指标 `[M8]`（注意力稀释与 Compaction 失忆代理指标 $\Delta A_{\text{attn}}$ Proxy）**：计算会话内 `input_tokens` 跨过不同水位线（如 50k / 100k / 150k）或发生 `compaction` 后，`redundant` 重复读盘率与工具调用失败率的斜率跃升，将 $\Delta A_{\text{attn}}$ 转化为可观测退化曲线。

#### 4.2.2 并发墙钟时间收益（$G_{\text{time}}$）

* **分项展开式与物理机制**：
  $$\Delta t_{\text{parallel}} = \sum_{k} \lvert W_k \rvert - \Bigl\lvert \bigcup_{k} W_k \Bigr\rvert, \qquad \mathrm{Overlap\%} = \frac{\Delta t_{\text{parallel}}}{\sum_k \lvert W_k \rvert}$$
  * **非阻塞多路扇出**：各子任务耗时的算术和减去并集实际墙钟跨度，是观测到的执行区间重叠量；只有串行执行耗时不变等假设成立时，才能作为省时估算 $\Delta t_{\text{parallel}}$。该值不单由同步/异步标签决定：异步任务可串行执行，多个并发的同步调用也可重叠；单父串行同步调用之间没有重叠。区间还可能含排队和通知等待，不是端到端加速的直接测量。
* **实测计量公式与判据（`[M5]` 并行时间收益）**：
  * 对 `CALL_ASYNC` 派发，以子任务观测窗 $W_k = [t_{\text{launch}, k}, t_{\text{delivery/end}, k}]$（而非父会话获取启动句柄的瞬时耗时）计算区间重叠量及占比。

#### 4.2.3 边界通信生成税（$C_{\text{tx}}$）

* **分项展开式与物理机制**：
  $$C_{\text{tx}} = T_{\text{dispatch}} \cdot c_{\text{out}} + T_{\text{report}} \cdot c_{\text{out}}$$
  * **输出定价假设（需按实际供应商与缓存价格校准）**：父写派发指令 $T_{\text{dispatch}}$ 与子写汇报摘要 $T_{\text{report}}$ 都包含模型生成成本，结构化工具参数也不会免除该成本；轻任务若套用重型汇报模板，$C_{\text{tx}}$ 可能超过降噪收益 $G_{\text{ctx}}$（倒挂）。
* **实测计量公式（`[M4]` 交接小作文摩擦占比与单次派发净收益）**：
  * **热复用派发的输入差分候选值（存在此前子会话 usage 时）**：$T_{\text{dispatch}, k} = \max(\mathrm{in}(A_{k,1}) - \mathrm{ctx}_{\text{before launch}}, 0)$（冷启动或缺少差分时按 `len(prompt) / chars_per_token` 估算；差分可能包含宿主注入，不等于精确指令长度）；
  * **交接小作文摩擦占比（Handoff Essay Friction Share）**：
    $$\mathrm{Friction}_{\text{parent}} = \frac{\sum_k T_{\text{dispatch}, k}}{\sum_{m \in R} \mathrm{out}_{\text{vis}}(m)}, \qquad \mathrm{Friction}_{\text{child}} = \frac{\sum_k T_{\text{report}, k}}{\sum_{n \in A} \mathrm{out}_{\text{vis}}(n)}$$
  * **单次派发的简化折算净收益与最小摊销轮次**（记输出/输入单价倍率 $\beta = c_{\text{out}} / c_{\text{in}}$；下式 Saved 与噪音量按上文脚本估算口径）：
    $$\mathrm{NetSaved}_k = \mathrm{Saved}_k - \beta \cdot (T_{\text{dispatch}, k} + T_{\text{report}, k}) > 0 \quad \Longleftrightarrow \quad N_{\text{rem}, k}^{\text{ep}} > N_{\text{rem}, k}^* = \frac{\beta \cdot (T_{\text{dispatch}, k} + T_{\text{report}, k})}{\Delta T_{\text{noise}, k}}$$
    该等价式仅在 $\Delta T_{\text{noise},k}>0$ 时成立；为 0 时脚本令 `n_rem_star=None`。它忽略缓存差价、冷启动、治理和质量损失，不能代表总账单节省。模型含义是父会话在当前片段内的剩余轮次 $N_{\text{rem}, k}^{\text{ep}}$ 必须跨过摊销下界 $N_{\text{rem}, k}^*$（或单次挡住的噪音 $\Delta T_{\text{noise}, k}$ 足够大），省下的输入 Prefill 才足以抵消写 `prompt` 与 `report` 的高价输出成本。
* **反馈判据与演进方向**：
  * **演进指标 `[M9]`（Stigmergy 载荷分流比 Artifact Offload Ratio）**：$\frac{\sum \mathrm{len}(\text{MEDIUM\_MUTATE 写入盘面的代码/文档字节})}{\mathrm{len}(\text{CALL\_SYNC/MSG\_CONSUME 跨边界报告字节})}$，量化文件系统/Worktree 替对话通道分流了多少倍重型载荷；
  * **演进指标 `[M10]`（自适应汇报压缩比 Adaptive Report Compactness）**：按子回合工具调用数分桶（无异常 Completed 轻任务 $\le 4$ 次工具 vs 其余回合），监控引入“轻任务最多六行汇报规则”前后的 $T_{\text{report}}$、$\mathrm{Friction}_{\text{child}}$ 与 $\mathrm{NCR}$ 变化，解决轻任务（如实测 $k=4$）因固定汇报模板过重导致 $\mathrm{NCR}$ 降低的问题。

#### 4.2.4 状态同步与冷启动税（$C_{\text{state}}$）

* **分项展开式与物理机制**：
  $$C_{\text{state}} = (1 - \gamma_{\text{warm}}) \cdot C_{\text{reload}} + T_{\text{reload}} \cdot c_{\text{in}}, \qquad T_{\text{reload}} = \mathrm{RAR} \cdot T_{\text{dispatch}}$$
  * **热会话复用率 $\gamma_{\text{warm}} \in [0, 1]$ 与冷启动成本 $C_{\text{reload}}$**：复用同一 `sessionID` 持续推进“侦察 $\to$ 实现 $\to$ 验证”时 $\gamma_{\text{warm}} \to 1$，使冷启动建仓读盘成本 $(1 - \gamma_{\text{warm}}) \cdot C_{\text{reload}} \to 0$；
  * **去程欠传递重取税 $T_{\text{reload}}$**：派单漏传锚点是重读的一种可能原因；脚本的 $\mathrm{RAR}>0$ 本身不能证明原因；
  * **子会话告警水线 $\tau_{\text{rotate}} = 120\text{k}$ tok**：这是可配置的轮换策略阈值，不是模型上下文上限；超线不能证明将崩溃，低于水线也不能证明热复用最优。
* **实测计量公式（`[M2]` 去程自足率/热会话命中率/重复读盘率 + `[M4]` 子脑饱和水线）**：
  * **子会话观测 Token 分解**（第 $k$ 次派发中子会话全部 `MEDIUM_OBSERVE` 观测 Token 记为 $T_{\text{obs}, k}$）：
    $$T_{\text{obs}, k} = \underbrace{T_{\text{fresh}, k}}_{\text{首次下钻探索（主会话未知的有益委托）}} + \underbrace{T_{\text{reload}, k}}_{\text{二次重取税（主会话/前序已知，因传递不足被迫再获取一次）}}$$
  * **去程比率与热会话命中率**：
    $$\mathrm{RAR}_k = \frac{T_{\text{reload}, k}}{T_{\text{dispatch}, k}}, \qquad \mathrm{DSR}_k = \frac{1}{1 + \mathrm{RAR}_k}, \qquad \eta_{\text{fresh}, k} = 1 - \frac{T_{\text{reload}, k}}{T_{\text{obs}, k}}, \qquad \gamma_{\text{warm}} = \frac{\lvert \{k \mid \text{sessionID}_k \neq \varnothing\} \rvert}{K}$$
  * **$T_{\text{reload}, k}$ 的三类待诊断来源**：
    1. **主子跨会话重读（Parent $\to$ Child Overlap）**：父会话派单前已读取/搜索过该目标，但 `prompt` 未传递具体结论或 `path:line`，导致子会话再次读取同一目标；
    2. **冷启动断代重读（Cold-Spawn Re-acquisition）**：未复用 `sessionID`（或新开并发子会话）且未注入前序子会话已探明的锚点，导致重复读取前序子会话已读区间；
    3. **缺锚点盲搜税（Anchorless Search Prefix）**：父会话在切面 A 已持有目标路径/符号锚点却未写入 `prompt`，导致子会话进场后耗费多步 `glob`/`grep` 重新定位该已知锚点。
  * **区间感知读盘分类的设计模型**（提取 `MEDIUM_OBSERVE(read)` 的 `(path, [offset, offset+limit))`，对比同一 `path` 已读区间并集 $\mathcal{I}_{\text{prev}}$）：
    * `disjoint`（不相交，$\lvert I_{\text{cur}} \cap \mathcal{I}_{\text{prev}} \rvert = 0$）：正常大文件分段翻页，**不计入冗余**；
    * `partial`（部分重叠）：跨页边界衔接；
    * `redundant`（完全包含，$I_{\text{cur}} \subseteq \mathcal{I}_{\text{prev}}$）：重复读取候选；文件修改后的复核也会命中，不能直接归因为浪费或失忆。
* **当前脚本覆盖范围**：`T_obs` 统计 `read/grep/glob` 输出字符估计；`T_reload` 只统计同路径、被任一既有读取区间完整包含的 `read` 输出。它不合并区间、不检查文件版本、不理解 prompt 是否漏传锚点，也不识别 grep/glob 的盲搜原因；因此 RAR/DSR 是重读代理指标，RAR=0 只表示该规则未识别到重读。
* **反馈判据与待验证假设**：
  * $\mathrm{RAR}_k = 0$（$\mathrm{DSR}_k = 100\%$）：当前规则未发现重读，不能证明派单完全自足；
  * $\mathrm{RAR}_k \ge 1$（$\mathrm{DSR}_k \le 50\%$）：脚本识别的重读 Token 估计已达派单量的一倍以上，需检查重读是否必要；
  * 跨轮次重读候选率 `redundant` 应 $< 10\%$；若同一文件被主/子或不同子会话反复重读 $>2$ 次，需检查文件是否已变更、验证需求、`sessionID` 复用，以及并发拆单时是否存在**模块边界重叠（多路子会话交叉读写同一范围）**；
  * **子会话饱和水线判据**：任何子会话 $\mathrm{Peak}(A) = \max_{n \in \text{LLM}(A)} \mathrm{in}(n) \ge 120{,}000$ tok 判 **`CHILD_OVERLOADED (Rotate Session)`**（历史记录中高峰值后出现 0s 空返回，但缺少宿主原因码，未证明由窗口溢出导致），须**主动换仓**——携带紧凑 `path:line` 锚点摘要开启新子会话；
  * **去程 $\mathrm{DSR}/\mathrm{RAR}$ 与回程 $\mathrm{NCR}$ 的跨轮次关联假设**：第 $k-1$ 轮报告保留 `path:line` 锚点（回程仅多花可忽略的少量 Token），有望减少第 $k$ 轮重读，仍需验证——回程压缩不应以剥离锚点为代价换取 $\mathrm{NCR}$ 虚高。

#### 4.2.5 控制面治理与验收税（$C_{\text{gov}}$）

* **分项展开式与物理机制**：
  $$C_{\text{gov}} = C_{\text{poll}} + C_{\text{verify}}$$
  * **纯轮询浪费成本 $C_{\text{poll}}$**：父会话对未完成后台任务调用 `STATUS_POLL` 窥探状态所白白消耗的 Prefill/Decode 成本；
  * **验收与中途仲裁成本 $C_{\text{verify}}$**：父会话在切面 C 吸收报告，或中途通过 `task_reply` 响应子会话挂起提问所消耗的成本。
* **实测计量公式与演进方向（`[M5]` 轮询浪费 + `[M7]` 中途挂起恢复比）**：
  * **轮询浪费 $C_{\text{poll}}$**：统计所有 `STATUS_POLL` 事件次数及其注入父窗口的 Token 总和（目标：$C_{\text{poll}} = 0$）；
  * **演进指标 `[M7]`（中途挂起恢复比 Mid-Flight Escalation Efficiency）**：对比走 `waiting_input` + `task_reply`（回合内恢复）与走回合末 `Status: Yielded`（重开下一轮）的平均耗时和额外 Token 开销，优化双档位 Yield 机制的边界阈值。

#### 4.2.6 信息失真与返工期望成本（$C_{\text{risk}}$）

* **分项展开式与物理机制**：
  $$C_{\text{risk}} = p_{\text{loss}} \cdot C_{\text{rework}} + \mathbb{E}\bigl[C_{\text{escape}, k}\bigr]$$
  * **跨边界传话失真率 $p_{\text{loss}}$ 与返工/沉没成本 $C_{\text{rework}}$**：因上下文裁剪导致前提误判的概率 $p_{\text{loss}}$，乘以作废重跑、`task_cancel` 止损或经核实无效的子会话消耗，须按费用折算后计入 $C_{\text{rework}}$；
  * **派发失败后的父内联逃逸成本 $\Delta\mathrm{Ctx}_{\text{escape}, k}$**：派发 $k$ 异常终止后，父会话放弃派发改为亲自调用工具内联兜底所引发的父窗口膨胀成本。
* **实测计量公式（`[M3]` 双层边界终态/返工/止损与内联逃逸）**：
  * **双层边界终态与异常集合**：每次边界记录二元组 $(\text{protocol}_k, \text{status}_k)$，异常/返工边界集合为
    $$K_{\text{rework}} = \{k \mid \text{protocol}_k \in \{\text{error}, \text{cancelled}\} \lor \text{status}_k \in \{\text{Yielded}, \text{Blocked}, \text{Empty}\} \lor \text{YIELD\_OR\_ABORT}_k\}$$
  * **显式取消的消耗代理量**：脚本统计被 `task_cancel(reason)` 中止的子会话在该轮已消耗的 Token；取消不等于全部成果无效，折算返工成本还需检查产物；
  * **失败后父会话内联逃逸成本（$\Delta\mathrm{Ctx}_{\text{escape}, k}$）**：当边界 $k \in K_{\text{rework}}$（含 0s 空返回的失败派发）且父会话随后**内联**执行工具（`read`/`edit`/`write`/`shell`/`grep`/`glob`）而非再次委托时，将该逃逸窗（终止于下一次 `subagent` 派发、下一次 `compaction`、轮间空闲 $> \tau_{\text{ep}}$ 或会话结束）内的父会话上下文增长、内联工具数与逃逸时长归属到边界 $k$：
    $$C_{\text{escape}, k} = \Delta\mathrm{Ctx}_{\text{escape}, k} \cdot c_{\text{in}}, \qquad \Delta\mathrm{Ctx}_{\text{escape}, k} = \max\bigl(\mathrm{in}(L_{\text{esc,last}}) - \mathrm{in}(L_{\text{esc,first}}),\ 0\bigr)$$
  * **未覆盖子会话分类**：脚本先用派发记录、工具参数或父工具时间窗判断覆盖，再查父 Wire。分类是观测启发式，不是运行时事实：
    * `delivered_missing_tool_span`：父 Wire 中找到子会话 wrapper，排除出浪费账；这不能单独证明父模型已理解或使用报告。
    * `killed_by_host_restart`：结构化错误结果含中断通知，且未找到 cancelled wrapper。当前名称仍可能误归因用户中断，须等待上游原因码；它不进入 `C_rework`。
    * `in_flight`：末次活动距 trace 水位不超过 `--child-live-window-sec`（默认 300s）。
    * `in_flight_pending_parent`：不满足上一项，但在 `--in-flight-window-sec`（默认 600s）内且父会话在子启动后无更新 Span。它提示可能尚在途，不证明父会话确实阻塞。
    * `orphan_aborted_or_dropped`：不满足以上条件的未覆盖子会话。脚本将其累计输入与输出计入 rework 账，但这只是待复核的保守归类；缺 Span 不证明任务失败或成果无用。
* **反馈判据与演进方向**：
  * 区分“有益刹车（`Yielded` 后切面 C 单轮改对计划）”与“失真返工（因指令不清或汇报遗漏导致同一目标重复派单）”；$C_{\text{escape}}$ 揭示“派发失败 $\to$ 父会话亲自兜底”的隐性成本（实测 $k{=}5$ 0s 失败后父会话内联逃逸 59 min、$+148{,}729$ tok），它标记边界之后的上下文增长；与失败的因果关系及相对正常执行的增量成本仍需核查；
  * **演进指标 `[M6]`（`Yielded` 一击修复率 One-Shot Re-Plan Rate）**：取所有 `status_k == "Yielded (Needs Reasoning)"` 的边界，检验切面 C（$R_{\text{consume}, k}$）是否在**无需父会话亲自 `read` 源码**的前提下，于 $k+1$ 次派发一次通过（`status_{k+1} == "Completed"`），作为 `Yielded` 边界上的成功恢复代理指标，不能直接等同传话失真概率。

### 4.3 协作范式与模型适用范围

以下是通道组合示例，不是各框架必然具有的成本或隔离保证：

- **RPC / Subagent**：常见 `CALL_SYNC` + `YIELD_OR_ABORT`。只返回摘要时可隔离过程文本；报告长度与后续轮次决定收益。
- **Handoff**：以 `HANDOFF_TAIL` 描述一次控制权移交。历史可以保留或过滤；调用方可能在后续流程再次获得控制，不能将具体框架一概等同永久退栈。
- **Mailbox / Actor**：组合 `CALL_ASYNC`、`MSG_EMIT`、`MSG_CONSUME`，可能附带 `STATUS_POLL`。异步启动提供并发机会，但不保证实际重叠。
- **Pub/Sub**：以 `MSG_EMIT(multicast)` 和 `MSG_CONSUME` 描述广播及消费。订阅关系与共享状态仍有耦合，多订阅者唤醒也可能增加输入成本。
- **Blackboard**：以 `MEDIUM_MUTATE(state)`、`MEDIUM_OBSERVE(state)` 读写共享状态。Schema 约束结构，若包含长文本仍有生成和读取成本，也不保证语义正确。
- **Group Chat**：共享 transcript 可增加多方输入；过滤、路由与裁剪决定实际暴露范围，不能统一断言所有实现均无隔离或必然负收益。
- **Stigmergy / 文件协作**：以 `MEDIUM_MUTATE(file/git)`、`MEDIUM_OBSERVE(file/git)` 传递产物。对话载荷可减少，但读盘、冲突协调与任务发现仍有成本；文件共享不等于零通信或最高并行度。

本项目的 `σ_leak = T_report / ΔT_work` 是单边界载荷比，不能同时用作全群广播倍率。`OpenCode + omo-slim` 的对话指令与文件产物分流有利于隔离，但是否达到 `NCR > 10:1` 仍须测量。

## 5. 架构预留扩展槽（编排底座与可观测平台适配）

当前工程实现固定为 **OpenCode + `oh-my-opencode-slim`（前端编排）** 与 **Arize Phoenix（后端可观测平台）**。未来若需横向迁移，可从以下映射开始适配，并验证事件生命周期、计数口径及内容恢复能力：

1. **预留扩展槽 1 — 前端 Agent 编排底座（当前：`OpenCode + omo-slim`，未来可扩展：`dsh` / `Claude Code` 等）**：
   * 首先核对 **5 项映射**，它们不是充分兼容性保证：① 会话角色与父子血缘字段（当前为 `opencode.agent` + `opencode.session.parent_id`）；② 九原语对应的工具名与同步/异步返回特征；③ 公开 reasoning 文本字段（当前为标准 `gen_ai.output.messages` 内的 `{"type":"reasoning"}` part，旧 `opencode.reasoning` 扁平镜像仅作回退）；④ 文件读取工具的 `(path, offset, limit)` 提取规则；⑤ Span 属性截断时的内容回源路径及其完整性限制（当前为 `opencode.wire.seq` $\leftrightarrow$ `raw-requests/ses_*.jsonl`）。
2. **预留扩展槽 2 — 后端可观测平台（当前固定：`Arize Phoenix`）**：
   * 指标计算层只消费归一化后的 Span 列表与 Wire 帧，不绑定存储引擎。当前脚本支持 SQLite `mode=ro` 直读和 GraphQL API。Phoenix MCP 可用于人工辅助查询，但不是该脚本内置的第三种后端。

## 6. 基准对比与自动化爬山（Hillclimbing）的审计护栏

以下为本项目的评测护栏。旧版对《MemoryTax》、具体模型及错判案例的归因未提供可复核来源，本次移除该归因，不将其作为实证依据。

1. **可见性与任务条件对称**：比较内联与派发或不同提示词版本时，控制任务、模型、初始上下文、工具权限、缓存条件和完成质量；请求轮数相近不等于任务等价。
2. **裁判校准与复核**：用已知答案、工具验证与人工抽检评估裁判可靠性，特别复核执行者纠正任务前提的案例。不能仅凭模型名称或笼统的能力排名保证判定正确。
3. **区分已实现与规划指标**：脚本主要实现 M1–M5 的部分计量；M6–M10 在本文中是演进指标，不能当成已自动产出的完整评分。NCR 分档、β=4.0 与 120k 告警线是可调参数，不是通用定律。

## 7. 参考资料

1. 配套实测数据与基准对照见：[react-mode-eval-benchmarks.md](react-mode-eval-benchmarks.md)
