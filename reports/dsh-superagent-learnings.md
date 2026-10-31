# dsh-superagent-prototype 架构学习报告：确定性控制面、原语细化与上下文经济学工程化

> 配套理论与指标规范见：[react-mode-eval.md](react-mode-eval.md)  
> 配套实测基准见：[react-mode-eval-benchmarks.md](react-mode-eval-benchmarks.md)  
> 学习对象：`~/yzh/engineering/dsh-superagent-prototype`（DeepSeek Harness Team 单提交研究原型 `6337fc8`，2026-10-31）  
> 引用约定：`dsh:` 前缀路径均相对上述仓库根目录；`eval.md` / `benchmarks.md` 指本目录两份配套文档。

**一句话总结**：dsh-superagent 把"编排"从 LLM 上下文里整体搬出——入库、批处理、授权、调度、重试、生命周期全部收敛进一个**单写者确定性状态机**（Engine + Effects + Passes），LLM 退化为一组"被咨询的判断函数"（准入评估 / 路由 / 工人执行），并用**物理级工具防火墙**取代提示词自律。它为我们稳定内核 A（四族九原语）贡献了 **6 个原语子模式**，为稳定内核 B（三层经济学不等式）贡献了 **5 项工程深化**，但自身**完全没有 Token 级遥测与经济学量化**——恰是我们框架的用武之地。

---

## 1. 核心范式跃迁：从"提示词自律编排"到"确定性控制面 + 物理级能力防火墙"

### 1.1 我们的问题：提示词自律的两个实测失效面

我们的 `Reasoner ↔ Actor` 模式中，"Reasoner 不亲自侦察、按经济边界派发"目前是**提示词层软约束**，实测暴露了两类失效：

- **边界派发失准（$k=4$）**：Reasoner 原话 *"Actually this is a simple read task — but per my role, discovery should go through an actor."* ——规则驱动越过了经济边界，4 次工具调用的轻任务套用四段式汇报模板后 $\mathrm{NCR}_{\text{char}}$ 跌至 2.90，仅靠 $\gamma_{\text{warm}}=1$ 免亏（`benchmarks.md:28-31`，对应 `eval.md` M10 槽位）。
- **首步滑坡（First-Step Inertia）**：父会话首个 TOOL Span 一旦是亲自 `read`/`shell`，后续极易沿上下文惯性单干到底（`benchmarks.md:87`）。

两者同根：**调度决策与调度执行都在同一个 LLM 上下文里**，靠模型自我克制，无物理强制。

### 1.2 DSH 的答案：编排决策整体出栈，单写者状态机接管

| 决策类别 | 我们（提示词自律） | dsh-superagent（确定性控制面） |
|---|---|---|
| 任务准入 | Reasoner 读卡片自行判断 | 两层：**Gate**（确定性 Policy 校验）+ **Assessment**（主 Session 仅对主动候选做模型研判），强度/依据/规则匹配结构化落库（`dsh:docs/001-domain.md:33`） |
| 批处理与去重 | 依赖 Reasoner 记忆 | Intake 按 Thread 批处理、触发器幂等（"Accepting a Trigger twice creates one Ticket"，`dsh:docs/001-domain.md:28`） |
| 调度与并发 | Actor 心智 + 宿主默认 | `WorkerSlots` 显式槽位预算（`dsh:src/executor/dsh.ts:171-199`） |
| 重试与退避 | 模型自行重试 | Effects 持久队列指数退避（`dsh:src/app/effects.ts:46`，`dsh:docs/007-architecture.md:172-181`） |
| 生命周期状态 | 报告体文字（`Status:` 行） | Ticket/Assignment 独立状态机，"Ticket 定义自己的状态而非跟随 Executor Session"（`dsh:docs/001-domain.md:56`） |
| 越界防护 | 提示词禁止 | 工具白名单 + 参数守卫（见 1.3） |

核心构件：

- **Engine**（`dsh:src/core/engine.ts:24`）：Trigger/Thread/Decision/Ticket/Assignment 的**唯一写者**，拆为 Intake→Evals→Tickets→Assignments→Replies 五个单向依赖部件（`dsh:docs/007-architecture.md:52-61`）；所有方法同步、每命令一个事务，端口在**事务内**入队副作用、适配器在**提交后**执行（`dsh:docs/007-architecture.md:48`）——"先落库、后执行"的 Stored Work 模式让崩溃恢复成为一等公民。
- **Effects**：SQLite 持久副作用队列，`kind/key` 单行合并去重（重复入队替换载荷并立即到期，不改排队序），`Later` 异常无损延迟（`dsh:docs/007-architecture.md:166-170`）。
- **Passes**：2 秒心跳 pass 驱动到期 Effects 与 `Engine.evaluateDue`（`dsh:src/app/passes.ts:31`）——调度心跳在宿主定时器里，不在任何 LLM 上下文里。

### 1.3 物理级能力防火墙：提示词只是最后的说明书

DSH 同样写了角色防火墙提示词（`dsh:prompts/shared.md:5`："Outside an assessment, the orchestrator must not inspect code, detailed logs, linked documents or technical discussion, or solve worker blockers itself"），但真正的强制在代码层：

- **主 Session 工具白名单**：`restrict({ allow: ['skill','bash','read','read_image','job_output','job_list','job_kill'] })`——无 `subagent`、无写文件、无 web（`dsh:src/app/sessions.ts:262-265`）；
- **noWait 守卫**：拒绝 `sleep`、>30s 的 shell 超时、>5s 的 job 等待——"等待的东西会自己来，阻塞一步就阻塞下一个候选"（`dsh:src/app/sessions.ts:63-86`，`dsh:prompts/orchestrator.md:16`）；
- **Worker 工具约束**：`WORK_TOOLS` 白名单 + Ticket 结束即全部禁用的 guard（`dsh:src/app/sessions.ts:61-62, 282-299`）；而 blocked/failed 的 worker **保留工具**以便人类直接接管（`dsh:docs/001-domain.md:59`）——防火墙拦机器不拦人；
- **单写者铁律**：每类状态只有一个写者，Source 改 Ticket 只能走 `bindThread`/`threadClosed` 两条 Engine 入口（`dsh:docs/007-architecture.md:259-271`）。

**范式判断**：这不是"更好的提示词工程"，而是**决策权重新分配**——可枚举、可重试、必须一致的决策类（调度/排序/授权/重试）进代码；需要语义判断的（这个请求该不该接、这个范围归不归我）留给模型，且给模型的输入输出全部 Schema 化（`dsh:src/app/assessment.ts:19-28`）。

### 1.4 三层 Agent 层级与我们 Reasoner↔Actor 的对位

```
Main Router Session（orchestrator）        ≈ Reasoner（只路由，无侦察工具）
  └─ Persistent Executor Session（worker-<ticketId>，1 Ticket ↔ 1 Session ↔ 1 git worktree）
                                            ≈ Actor（热会话 + 专属工作目录）
       └─ Nested sync subagent（DSH standard preset）  ≈ Actor 的临时分身（CALL_SYNC）
```

- 工人会话命名 `worker-<ticketId>`，前缀反解即血缘（`dsh:src/executor/dsh.ts:24-26`）；
- 嵌套 subagent 走 `provider: spawn` 的 `subagent` 工具、强制 `standard` preset，并继承工人的工具白名单、Ticket 结束即失能（`dsh:cordis.patch.yml:96-111`，`dsh:src/app/sessions.ts:300-318` 的 `limitSubagent` 向上爬最多 8 层父链找到所属 worker）；
- 工人 shell 前台命令 5 秒自动转后台 job，长命令不阻塞人员消息在下一步注入（`dsh:docs/003-execution.md:27`，`dsh:cordis.patch.yml:38-49`）。

对照我们的体系：主 Session 是"无工具 Reasoner"，worker 是"目录级 Actor"，subagent 是 Actor 内同步分身——与我们"Reasoner 不持盘、Actor 独占工作区"的分工一致，但 DSH 把**层级间每种通道都做成了显式原语**（见第 2 节）。

---

## 2. 对"四族九原语（稳定内核 A）"的六项新认知与细化

以下六个子模式均不改变九原语分类，而是揭示各原语在成熟系统中的**内部结构**。

### 2.1 `MSG_CONSUME` 的三模态收件箱：解耦"消息送达"与"LLM 唤醒 Prefill"

**证据**：`AgentDelivery.send` 的三种投递模式（`dsh:src/app/delivery.ts:66-85`）：

| 模式 | API | 语义 | Prefill 代价 |
|---|---|---|---|
| `'queue'` | `agent.followup(msg)` | 排队等**下一回合**消费 | 下一次自然唤醒时一并算 |
| `'steer'` | `agent.steer(msg)` | 注入**正在运行的回合**的下一步，不打断当轮 | 立即（增量） |
| `{inject}` | `agent.inject(msg)` | 追加进历史但**不唤醒**模型 | **0**——直到该会话因别的理由被唤醒 |

配套**请求级幂等**：`messageId(requestId)` 派生稳定消息 ID（`dsh:src/app/delivery.ts:16`），重启后扫描持久化历史 + 双收件箱（`nextTurn`/`nextStep`）去重（`dsh:src/app/delivery.ts:40-64, 74-75`）；`relay`/`notice` 两个 effect 种类**保序**投递到主 Session（`dsh:src/app/sessions.ts:104-105`），notice 按 key 合并去重（`dsh:src/app/notices.ts:15-19`）。

**对我们的细化**：`eval.md` 中 `MSG_CONSUME` 的落点目前只有"拉取"（`task_status(result=true)`）一档；DSH 证明**消费时机**本身是三维设计空间：
1. `{inject}` 模式实现"送达 ≠ 唤醒"——不需决策的消息（通知类）可零 Prefill 沉积在历史里，等下一次自然唤醒顺带可见。这是对 M5 中 $C_{\text{poll}}$ 的对偶优化：不仅消灭轮询，还消灭"被动唤醒税"；
2. `'steer'` 是 `YIELD_OR_ABORT` 之外的中途信息注入第三径：不打断、不退栈、下一步生效——比 `waiting_input`+`task_reply` 仲裁更轻；
3. 幂等键设计（请求 ID 派生消息 ID）值得 omo-slim 后台任务通道直接抄。

### 2.2 `HANDOFF_TAIL` 的逆向越界退单（`trigger_handback`）vs `YIELD_OR_ABORT`（受阻求援）

**证据**：DSH 把"工人交回控制权"拆成两条语义正交的通路：

- **范围内受阻 → `YIELD_OR_ABORT`（求援不弃单）**：报告 `blocked`/`failed` 或结构化 `human_action` 单问题，进入 `waitsForPerson` 状态挂起自动输入（`dsh:src/core/model.ts:349`；`dsh:prompts/handoff-worker.md:1-7`——"one concrete question"、"state may remain progress while you continue unambiguous authorized work"）。任务仍归本工单。
- **范围外越界 → 逆向 `HANDOFF_TAIL`（退单不求助）**：`orchestrator_trigger_handback` 把到达本工单线程、但"超出本 Ticket 或属于另一 Ticket"的触发器**退回 Eval 队列**重新评估，每条触发器只退一次、订阅保留（`dsh:src/app/tools/executor.ts:66-67`，`dsh:docs/007-architecture.md:91`）。退单后该消息作为带 `handed_back` 标记的候选卡回到主 Session（`dsh:prompts/dispatch.md`："handed_back: a ticket's worker returned it as beyond that ticket, with why; accept, attach it elsewhere or ignore it, never attach it back"）。

**对我们的细化**：我们的 Actor 报告契约只有 `Status: Completed | Yielded | Blocked` 三态，"这活不归我"目前只能写进 Yielded 小作文让 Reasoner 语义解析。应增设**结构化退单字段**（如 `Handback: <trigger_ref> (out-of-scope)`），让范围仲裁走 Schema 而非自然语言——这正是 `eval.md` M3"有益刹车 vs 失真返工"与 M6 $p_{\text{loss}}$ 的减震器：退单是零返工的边界重路由。

### 2.3 `HANDOFF_TAIL` 的异构后台资源移交：Subagent 即杀 vs Shell Job 托付

**证据**：`handOff()`（`dsh:src/executor/dsh.ts:588-599`）在工人向人移交时对两类后台资源做**异构处置**：

- **子代理（subagents）立即回收**：`stopSubagents(..., keepInbox)` 释放全部子代理（`dsh:src/executor/background.ts:141-153`）——子代理的唯一消费者是工人的推理上下文，工人退场即价值消亡；
- **Shell Job 留给人类并静默回收结果**：如供人评审的 dev server 继续跑，不占槽位、豁免 30 分钟限制，结束时用 `collectJob` + `{inject}` 模式静默落账，工人下次回合再读（`dsh:src/executor/dsh.ts:590-598`；`dsh:docs/003-execution.md:39`）。

**对我们的细化**：判定规则是"**资源的消费者是否在移交后存活**"——上下文绑定型资源随移交者死，人类面向型资源随接手者活。omo-slim 的后台任务在 Actor `Yielded` 时目前无显式处置规则，可直接采纳此判定。

### 2.4 `MEDIUM_OBSERVE` 的无授权只读订阅（Context-Only Subscription）

**证据**：`orchestrator_thread_follow` 让工单订阅关联线程"仅作上下文"——消息照常送达，但"**不借予 follow-up 强度、不能唤醒已结束工单**"（`dsh:src/app/tools/executor.ts:51-52`，`dsh:docs/007-architecture.md:90`）；投递卡片显式携带 `context: true` 标记（`dsh:src/executor/dsh.ts:156-159`）。

**对我们的细化**：这是"授权（Mandate）与上下文（Subscription）正交分解"的范本——群聊范式的 $\sigma_{\text{leak}}=1\sim M$ 灾难（`eval.md` 第 1.3 节范式 6）被驯服为**单向、带标记、无行动权**的观察流。我们的多工单场景（如同一仓库多个并行 Actor）中，跨工单上下文共享目前只能靠 Reasoner 转述（吃 $C_{\text{tx}}$）；context-only 订阅提供了零授权泄漏的第三径。

### 2.5 `MEDIUM_MUTATE` 的单写者铁律与无授权便签（`orchestrator_ticket_note`）

**证据**：每类状态一个写者的完整产权表（`dsh:docs/007-architecture.md:259-271`）；主 Session 可向别的工作流写入纯事实便签——`orchestrator_ticket_note`："Tell a ticket's worker a fact from other work... **it grants no authority**"（`dsh:src/app/tools/main.ts:177-179`）。授权只活在不可变、带 Policy 版本、可重校验的 Mandate 里（`dsh:docs/001-domain.md:34`），且"外部文本不能改变策略"（`dsh:prompts/orchestrator.md:11`）。

**对我们的细化**：共享媒介可以传**事实**但永远传不了**权利**——这是对提示注入类权限提升的结构性免疫。对照我们的实践：Reasoner 给 Actor 的派发指令里"授权范围"与"背景事实"混在同一自然语言段落中；分离二者（授权走结构化字段、事实走便签通道）可直接压低 $p_{\text{loss}}$ 与 $C_{\text{risk}}$。

### 2.6 `STATUS_POLL` 的宿主化剥离与合并去重（$C_{\text{poll}} \to 0$）

**证据**：
- 2 秒心跳 pass 驱动一切到期工作（`dsh:src/app/passes.ts:31`）；
- Effects 按 `kind/key` 合并：同一等待键的新请求**替换载荷**而非叠加排队（`dsh:docs/007-architecture.md:166-170`）——轮询风暴在队列层被压缩为单行；
- Eval 超时 → 触发器重排队（上限 `REQUEUES = 2` 次）→ 第三次记 `unavailable`（`dsh:src/core/evals.ts:21, 60-66`）——超时治理也在代码里；
- 上下文内等待被物理禁止：noWait 守卫（1.3 节）+ 提示词封堵（`dsh:prompts/orchestrator.md:16`）；仅存的一次性状态询问 `orchestrator_ticket_request_status` 被严格限界——"只问结束回合却未报告的工人，不唤醒其他工单"（`dsh:src/app/tools/main.ts:168-171`）。

**对我们的细化**：$C_{\text{poll}}$ 的最优解不是"提醒模型别轮询"，而是**让轮询无处发生**——所有"未就绪等待"都由宿主定时器与合并队列代持。omo-slim 的 `task_status` 通道可对照检查：诊断型轮询（`diagnostic=true`）之外的窥探是否已有宿主侧替代。

---

## 3. 对"三层上下文经济学不等式（稳定内核 B）"的五项工程深化

对照 `eval.md` 第 2.1 节：$G_{\text{ctx}} + G_{\text{time}} > C_{\text{tx}} + C_{\text{state}} + C_{\text{gov}} + C_{\text{risk}}$。

### 3.1 锁定 $G_{\text{ctx}}$：有界卡片渐进披露——用通道物理上限压死 $\sigma_{\text{leak}}$

**证据链**（四层递进）：
1. **卡片层**：一切跨边界记录投影为紧凑卡片——160 字符预览 + `read: "trigger/<id>"` 资源指针，全文仅按需拉取（`dsh:src/app/cards.ts:1-13`）；输入预览 80 字符（`dsh:src/app/cards.ts:37`）、报告预览 100 字符（`dsh:src/app/cards.ts:50`）；
2. **投递层**：触发器内联 600 字符、任务书内联 1500 字符，超限截断并附资源路径（`dsh:src/executor/dsh.ts:112-116`）；
3. **评估窗口层**：候选评估初始窗口四档上限 4000/1200/400/50（触发器本身/近邻消息/窗口消息/存根预览，`dsh:src/app/assessment.ts:30`）；
4. **宿主层**：DSH 工具结果修剪器 `thresholdChars: 8192 → head 4096 + tail 1024`，两个 preset 均配（`dsh:cordis.patch.yml:79-95, 177-193`）；资源快照走按字符总量有界的 LRU `TextCache`（`dsh:src/app/cache.ts:5`）。

**工程深化**：DSH 的 $\sigma_{\text{leak}}$ 上界不靠"汇报写短点"的自律，而靠**通道物理上限**——每条跨边界消息的载荷是 Schema 化卡片，长内容只能经 `read` 资源按需消费（消费即计量、可审计）。我们实测的 $k=4$ 模板过重问题（`benchmarks.md:28-31`）在此结构下不可能发生：报告长度被卡片模板与截断常量封顶，M10 的"轻任务汇报分桶"可直接落地为模板常量而非提示词请求。

### 3.2 锁定 $C_{\text{tx}}$：类型化工具通道 + 硬编码字数上限——消灭交接小作文税

**证据**：
- **无自由文本 Agent 间聊天通道**：所有跨 Agent 数据流经类型化工具（主 Session 14 个 `orchestrator_*` + 工人 5 个），全部 Zod Schema 校验；派发即结构化字段 `{title, worktree_name, scope, intent_evidence, acknowledgement}`（`dsh:src/app/assessment.ts:19-28`）——$T_{\text{dispatch}}$ 被 Schema 封顶，与模型啰嗦程度无关；
- **字数常量硬编码**：便签 30 词（`NOTE_WORDS`）、IM 消息 100 词（`IM_WORDS`）（`dsh:src/infra/text.ts:3-4`），派发路由注 ≤30 词（`dsh:prompts/dispatch.md`）；
- **回复目的门控**：`orchestrator_reply` 按 purpose 白名单拒绝普通进度外发（`dsh:src/app/tools/main.ts:224-226`）。

**工程深化**：对照 `eval.md` M4 的 Friction 占比指标——我们实测交接小作文占 Reasoner 可见输出 53%、Actor 42%（`benchmarks.md:49`）。DSH 证明 $C_{\text{tx}}$ 可以做到**结构性趋零**：把"派发指令"从自由文本降维成结构化字段 + 有界路由注。我们的派发规格可逐步 Schema 化（目标 → 边界 → 验收标准三字段 + 30 词注）。

### 3.3 锁定 $C_{\text{state}}$：$\gamma_{\text{warm}} \equiv 1$ 与 KV Cache 命中的结构性保障

**证据**：
- **① 三元组 1:1:1 绑定**：`ticketId ↔ worker-<ticketId> 会话 ↔ git worktree 目录`（`dsh:src/executor/dsh.ts:24-26`；worktree 名与任务书在 `dsh:src/executor/dsh.ts:97-111` 生成）。人类的后续消息在**同一 Mandate 下免 Eval 直接复活**已结束工单（`dsh:docs/001-domain.md:37`）；retry/restart/恢复全部复用同会话同目录（`dsh:docs/003-execution.md:141-152`）——热复用是**默认**，冷启动是例外，$\gamma_{\text{warm}}$ 结构性趋 1；
- **② Cache-Aware Prompt Assembly**：15 个 prompt 部件按固定顺序装配（角色 → 团队节 → 订阅/回复 → 派发或工作 → 准入 → 移交 → 环境 → 策略），**动态 Policy 节严格放最后**——"Policy 变更不影响其前的提示文本"，且每次模型请求前重生成（`dsh:docs/004-prompts.md:46-59`，esp. :59）；系统提示按请求绑定版本号戳记并持久化各会话用过的版本（`dsh:src/app/sessions.ts:244` 的 `dsh-superagent-policy` section，`order: 9999` 且 `interpolate: false`）。

**工程深化**：$\gamma_{\text{warm}}$ 与 KV Cache 命中率（我们实测 Actor 97.0%，`benchmarks.md:42`）不只是"复用 sessionID"的运气，而是两处可显式工程化的不变量：**会话-目录-任务的三元组绑定**（状态不迁移）与**前缀稳定的提示装配**（高频变更段后置）。omo-slim/Reasoner 的系统提示装配顺序值得按此审计：把每轮变化的内容（时间戳、动态策略、最新状态摘要）后置。

### 3.4 优化 $G_{\text{time}}$：防饥饿的双水位并发槽位

**证据**：`WorkerSlots` 在 `maxWorkers` 之外为**人类消息唤醒**预留 +1 槽位：`reserve(ticketId, 'person')` 可超限占第 `max+1` 槽，且唤醒优先于新发射（`launches()` 把 wake 占用计入基数，`dsh:src/executor/dsh.ts:171-199`，esp. :182-196）；槽位不足时 `Later` 无损延迟 5 秒再试（`dsh:src/executor/dsh.ts:35-36, 192-196`）。

**工程深化**：并发预算对"热路径人机交互"与"冷启动新任务"**分水位**——人类追问已有热工单的延迟与发射队列长度解耦（"New work therefore cannot prevent a person from continuing an existing ticket"，`dsh:docs/003-execution.md:59`）。这同时保护 $G_{\text{time}}$（交互不排队）与 $\gamma_{\text{warm}}$ 的时间维度（热会话不被饿死过久）。omo-slim 后台任务并发上限可借鉴：为热会话 follow-up 保留唤醒优先级。

### 3.5 控制 $C_{\text{risk}}$：单问题原则 + 版本化准入的双向重校验

**证据**：
- **单问题结构化**：一次只问一个具体问题、结构化字段递送（`dsh:docs/000-philosophy.md:13`）；`human_action` 是 Ticket 上一等结构化字段（卡片直接投影 `attention`，`dsh:src/app/cards.ts:31`），且"从自由文本推断等人"被明确列为拒绝概念（`dsh:docs/001-domain.md:60`）——问题经 `question_for_trigger_id` 路由回**原始提问所在的会话**（`dsh:prompts/handoff-worker.md:1`）；
- **Mandate/Policy 双向重校验**：Mandate 不可变、记录 Policy 版本；复活/续期前 `revalidate` 重查（`dsh:docs/001-domain.md:34`，`dsh:docs/007-architecture.md:136-147`）；Policy 变更对在途工单**送达变更说明而非取消**——半成品不被规则变更摧毁（`dsh:docs/001-domain.md:32, 58`）。

**工程深化**：$C_{\text{risk}} = p_{\text{loss}} \cdot C_{\text{rework}}$ 通常只在"消息失真"轴上优化；DSH 展示了**授权轴**——范围漂移在重校验点被确定性拦截（proactive 工单的 mandate 限制指令，`dsh:src/executor/dsh.ts:91-95`），而不是靠工人自觉汇报"我超范围了"。对应我们的 M6/M3：Yielded 一击修复率之外，可增设"授权边界重校验拦截数"作为 $p_{\text{loss}}$ 的前置减震指标。

---

## 4. 双向互补与可落地反哺清单

### 4.1 我们的方向所拥有、而 dsh-superagent 缺失的

1. **零 Token 级遥测**：全仓库无 OTel/tracing；无 per-request Prefill/Decode 计量暴露；`HealthBoard` 是组件状态健康（`dsh:src/infra/health.ts:16-33`），不是上下文经济学。他们**看不见** $k=4$ 型边界失准、首步滑坡、$\gamma_{\text{warm}}$、KV 命中率、NCR——Store 审计线索（Reason 代码、回执、prompt 版本）齐全，但 LLM Span 维度缺失；
2. **无边界失真量化**：无双层终态（Protocol × Semantic）观测，无 $p_{\text{loss}}$ / 一击修复率（M6）类指标；
3. 因此 `eval.md` 第 4 节扩展槽 1 恰好为其预留——我们已把 `dsh` 列为未来前端编排底座候选（`eval.md:211`）。五项字段映射在此具体化：① 角色与血缘 = `worker-<ticketId>` 前缀约定（`dsh:src/executor/dsh.ts:26`）；② 原语 = `subagent`(spawn) / `send_message` / `interrupt_agent`（`dsh:src/app/sessions.ts:61-62`）；③ 思维链 = DSH `assistant/message` 会话事件（本仓库未见 `opencode.reasoning` 等价物，需桥接层）；④ 读盘参数 = `orchestrator_read`/`read` 工具入参；⑤ 截断回源 = 不需要——原文常驻 Store 资源表（`dsh:src/app/resources.ts`）。

### 4.2 可落地反哺清单（按优先级）

| # | 反哺项 | 来源证据 | 落点 |
|---|---|---|---|
| 1 | **类型化退单字段**（`Handback: <ref>`）：范围外任务走结构化退单，不写 Yielded 小作文 | 2.2 节 | Actor 报告契约 + `eval.md` M3/M6；`HANDOFF_TAIL` 原语行补"逆向退单"子模式 |
| 2 | **汇报模板分桶封顶**：按任务轻重分桶的卡片模板 + 截断常量，替代"写短点"提示词 | 3.1 节 | omo-slim 汇报模板；M10 落地 |
| 3 | **三模态消息消费**：至少实现/模拟 `{inject}`（送达不唤醒）与请求级幂等键 | 2.1 节 | omo-slim 后台任务通道；M5 增记"被动唤醒 Prefill 税" |
| 4 | **Cache-aware 提示装配**：动态段（时间/状态/策略）严格后置 + 每请求版本戳 | 3.3 节② | Reasoner/Actor 系统提示装配顺序审计 |
| 5 | **双水位并发槽位**：热会话 follow-up 唤醒优先于新发射 | 3.4 节 | omo-slim 并发调度 |
| 6 | **noWait 守卫**：禁 sleep/长超时/长 job 等待，等待物自达 | 2.6 节 | Reasoner 工具守卫 + 提示词；$C_{\text{poll}} \to 0$ |
| 7 | **单问题 + 原文路由**：attention 结构化单问题，携 `question_for_trigger_id` 路由回原提问处 | 3.5 节 | Actor 报告契约细化 |
| 8 | **异构后台资源处置**：移交时上下文绑定型子代理即杀、人类面向型 job 托付并静默回收 | 2.3 节 | omo-slim Yield 时后台任务处置规则 |
| 9 | **授权/事实通道分离**：派发中的授权范围走结构化字段，背景事实走无授权便签（≤30 词） | 2.5 / 3.2 节 | Reasoner 派发规格 Schema 化 |
| 10 | **Context-only 订阅标记**：跨工单转发上下文时携带 `context: true`，接收方不获得行动权 | 2.4 节 | 多工单工作流的 Reasoner 转发协议 |

### 4.3 保留我们现状的理由（不必全盘 DSH 化）

DSH 把 LLM 从编排环路中整体移出，代价是：控制面不可语义解释处（为何这个候选被忽略）依赖 Reason 代码字典（`dsh:src/core/reasons.ts`）而非模型自述；且五部件单写者引擎的建设成本高。我们的研究目标是**观测与量化** Reasoner↔Actor 边界，LLM-in-the-loop 恰好暴露切面 A（派单动机）与切面 C（验收判断）的一手思维链证据（`eval.md:111-131`）。因此正确姿势是**按决策类迁移**：调度/重试/排序/授权迁移给代码（第 2.6、3.3、3.4、3.5 项工程深化），范围判断与边界语义留在模型并用 Schema 收口（第 4.2 节 1/7/9 项）。

---

*报告基于只读检视，未改动 dsh-superagent-prototype 任何文件。*
