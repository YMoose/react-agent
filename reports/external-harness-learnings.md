# 外部多 Agent 研究启示录（external-harness-learnings）

> 配套理论与指标规范见：[react-mode-eval.md](react-mode-eval.md)
> 配套实测基准见：[react-mode-eval-benchmarks.md](react-mode-eval-benchmarks.md)
>
> 定位：集中收录来自外部原型仓库、公开研究复盘与社区众包数据的多 Agent Harness 启示（按时间分为三部分：dsh-superagent-prototype、Google Antigravity `/teamwork`、OpenCode 子代理使用分布众包）。各部分只记录为**候选研究方向与假设**（dsh 部分的"可落地反哺清单"同属此类），不改动现有提示词（`prompts/` 不随本文变化）；引用的外部数据均标注采集日期与样本量，防止把快照当定律。

**一句话总结**：截至 2026-11 的三份外部证据共同指向：**多 Agent 编排的有效切分维度是"上下文生命周期与验证独立性"，而非"静态工具权限"或"更多角色"**——dsh 原型证明可枚举的调度/重试/授权决策应迁移给宿主代码、语义判断留给模型并用 Schema 收口（第一部分）；Antigravity `/teamwork` 证明长程任务靠按构造切分上下文 + 独立干净上下文证伪器（第二部分）；OpenCode 众包证明静态权限双角色（`explore`/`general`）在实践中被系统性绕行（第三部分）。我们 `Reasoner ↔ Actor` 的单一有状态 `actor` + 锚点打包 + 冷热轮转 + 客观验收证据设计与三者一致；其余启示（干净上下文审计、原型路由、反哺清单）列为待受控实验验证的候选方向。

---

# 第一部分：dsh-superagent-prototype —— 确定性控制面、原语细化与上下文经济学工程化（2026-10-31 记录）

> 学习对象：`dsh-superagent-prototype`，本地 checkout `~/yzh/engineering/dsh-superagent-prototype`（HEAD `6337fc8`，2026-11-03 复核确认存在）。
> 引用约定：`dsh:` 前缀路径均相对该原型仓库根目录，**不是** `deepseek-harness` 路径；`harness:` 表示本地 `deepseek-harness` 仓库。
>
> 2026-11-03 核查范围：原型特有的类名、常量、Schema、工具守卫和行号是历史笔记，尚未重新核实，下文“证据”均指原记录引用。本次只修正可由本地 Harness 或逻辑核实的错误，不能以当前 Harness 行为反证原型当年的实现。

**一句话总结**：按原记录，dsh-superagent 把调度、持久化和生命周期控制从 LLM 上下文移到宿主代码——入库、批处理、授权、调度、重试、生命周期全部收敛进一个**单写者确定性状态机**（Engine + Effects + Passes），LLM 仍承担准入、路由等语义决策与工人执行，并用**物理级工具防火墙**取代提示词自律。它为我们稳定内核 A（四族九原语）贡献了 **6 个原语子模式**，为稳定内核 B（三层经济学不等式）贡献了 **5 项工程深化**，但原笔记未展示原型自身的 Token 级指标接入；这不能推出其依赖的 Harness 没有 usage 数据——恰是我们框架的用武之地。

---

## 1. 核心范式跃迁：从"提示词自律编排"到"确定性控制面 + 物理级能力防火墙"

### 1.1 我们的问题：提示词自律的两个实测失效面

我们的 `Reasoner ↔ Actor` 模式中，"Reasoner 不亲自侦察、按经济边界派发"目前是**提示词层软约束**，实测暴露了两类失效：

- **边界派发失准（$k=4$）**：Reasoner 原话 *"Actually this is a simple read task — but per my role, discovery should go through an actor."* ——规则驱动的派单是否划算需进一步核实，4 次工具调用的轻任务套用四段式汇报模板后 $\mathrm{NCR}_{\text{char}}$ 跌至 2.90，该次热复用，但仅凭 NCR 不能判定净收益（`react-mode-eval-benchmarks.md`，对应 `react-mode-eval.md` M10 槽位）。
- **首步滑坡（First-Step Inertia）**：父会话首个 TOOL Span 一旦是亲自 `read`/`shell`，后续极易沿上下文惯性单干到底（`react-mode-eval-benchmarks.md`）。

两者同根：**调度决策与调度执行都在同一个 LLM 上下文里**，靠模型自我克制，无物理强制。

### 1.2 原型的设计：确定性调度进状态机，语义判断仍由模型参与

- 任务准入：我们（提示词自律）：Reasoner 读卡片自行判断；dsh-superagent（确定性控制面）：两层：**Gate**（确定性 Policy 校验）+ **Assessment**（主 Session 仅对主动候选做模型研判），强度/依据/规则匹配结构化落库（`dsh:docs/001-domain.md:33`）

- 批处理与去重：我们（提示词自律）：依赖 Reasoner 记忆；dsh-superagent（确定性控制面）：Intake 按 Thread 批处理、触发器幂等（"Accepting a Trigger twice creates one Ticket"，`dsh:docs/001-domain.md:28`）

- 调度与并发：我们（提示词自律）：Actor 心智 + 宿主默认；dsh-superagent（确定性控制面）：`WorkerSlots` 显式槽位预算（`dsh:src/executor/dsh.ts:171-199`）

- 重试与退避：我们（提示词自律）：模型自行重试；dsh-superagent（确定性控制面）：Effects 持久队列指数退避（`dsh:src/app/effects.ts:46`，`dsh:docs/007-architecture.md:172-181`）

- 生命周期状态：我们（提示词自律）：报告体文字（`Status:` 行）；dsh-superagent（确定性控制面）：Ticket/Assignment 独立状态机，"Ticket 定义自己的状态而非跟随 Executor Session"（`dsh:docs/001-domain.md:56`）

- 越界防护：我们（提示词自律）：提示词禁止；dsh-superagent（确定性控制面）：工具白名单 + 参数守卫（见 1.3）

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
Main Router Session（orchestrator）        ≈ Reasoner（含路由/评估，工具受限）
  └─ Persistent Executor Session（worker-<ticketId>，1 Ticket ↔ 1 Session ↔ 1 git worktree）
                                            ≈ Actor（热会话 + 专属工作目录）
       └─ Nested subagent（standard preset）         ≈ Actor 的临时分身（同步性待核实）
```

- 工人会话命名 `worker-<ticketId>`，前缀可反解工单关联，不能代替父子会话血缘字段（`dsh:src/executor/dsh.ts:24-26`）；
- 嵌套 subagent 走 `provider: spawn` 的 `subagent` 工具、强制 `standard` preset，并继承工人的工具白名单、Ticket 结束即失能（`dsh:cordis.patch.yml:96-111`，`dsh:src/app/sessions.ts:300-318` 的 `limitSubagent` 向上爬最多 8 层父链找到所属 worker）；
- 工人 shell 前台命令 5 秒自动转后台 job，长命令不阻塞人员消息在下一步注入（`dsh:docs/003-execution.md:27`，`dsh:cordis.patch.yml:38-49`）。

对照我们的体系：主 Session 是工具受限的路由/评估者（1.3 节仍列有 `bash/read`），worker 是"目录级 Actor"；当前 Harness `subagent` 工具立即返回 ID 并异步通知完成，不能仅凭 `provider: spawn` 推断 `CALL_SYNC`（`harness:packages/subagent/tool-subagent/src/index.ts`）——与我们"Reasoner 不持盘、Actor 独占工作区"的分工一致，但 DSH 把**层级间每种通道都做成了显式原语**（见第 2 节）。

---

## 2. 对"四族九原语（稳定内核 A）"的六项新认知与细化

以下六个子模式均不改变九原语分类，而是揭示各原语在该研究原型中的**内部结构**。

### 2.1 `MSG_CONSUME` 的三模态收件箱：解耦"消息送达"与"LLM 唤醒 Prefill"

原记录将 `AgentDelivery.send` 的模式映射到以下 API（`dsh:src/app/delivery.ts:66-85`，原型映射待复核）。当前 Harness 的实际语义见 `harness:packages/core/agent-loop/src/agent.ts`：

- `'queue'` → `agent.followup(msg)`：写入 next-turn inbox 并请求唤醒，不必等外部“自然唤醒”。消息被消费时产生输入成本。
- `'steer'` → `agent.steer(msg)`：写入 next-step inbox 并请求唤醒；运行中通常在后续步骤消费，空闲时也能启动。不是当前模型请求的即时增量 Prefill，具体生效边界取决于执行状态。
- `{inject}` → `agent.inject(msg)`：写入 next-step inbox，**不主动唤醒**；不等于直接提交到已消费历史。正在运行的会话仍可在下一步消费，空闲会话等待后续唤醒；不主动启动新调用不代表消息未来没有 Token 成本。

配套**请求级幂等**：`messageId(requestId)` 派生稳定消息 ID（`dsh:src/app/delivery.ts:16`），重启后扫描持久化历史 + 双收件箱（`nextTurn`/`nextStep`）去重（`dsh:src/app/delivery.ts:40-64, 74-75`）；`relay`/`notice` 两个 effect 种类**保序**投递到主 Session（`dsh:src/app/sessions.ts:104-105`），notice 按 key 合并去重（`dsh:src/app/notices.ts:15-19`）。

**对我们的细化**：`react-mode-eval.md` 中 `MSG_CONSUME` 的落点目前只有"拉取"（`task_status(result=true)`）一档；DSH 证明**消费时机**本身是三维设计空间：

1. `{inject}` 模式实现"送达 ≠ 唤醒"——不需立即决策的消息可先留在 inbox，减少额外唤醒，但后续消费仍有输入成本。这是对 M5 中 $C_{\text{poll}}$ 的对偶优化：不仅消灭轮询，还消灭"被动唤醒税"；
2. `'steer'` 是 `YIELD_OR_ABORT` 之外的中途信息注入第三径：不需要先结束当前回合再排后续回合；成本是否更低仍须测量；
3. 幂等键设计（请求 ID 派生消息 ID）值得 omo-slim 后台任务通道直接抄。

### 2.2 范围外触发器的重新路由（`trigger_handback`）vs `YIELD_OR_ABORT`（受阻求援）

**证据**：DSH 把"工人交回控制权"拆成两条语义正交的通路：

- **范围内受阻 → `YIELD_OR_ABORT`（求援不弃单）**：报告 `blocked`/`failed` 或结构化 `human_action` 单问题，进入 `waitsForPerson` 状态挂起自动输入（`dsh:src/core/model.ts:349`；`dsh:prompts/handoff-worker.md:1-7`——"one concrete question"、"state may remain progress while you continue unambiguous authorized work"）。任务仍归本工单。
- **范围外越界 → 触发器退回评估队列**：`orchestrator_trigger_handback` 把到达本工单线程、但"超出本 Ticket 或属于另一 Ticket"的触发器**退回 Eval 队列**重新评估，每条触发器只退一次、订阅保留（`dsh:src/app/tools/executor.ts:66-67`，`dsh:docs/007-architecture.md:91`）。退单后该消息作为带 `handed_back` 标记的候选卡回到主 Session（`dsh:prompts/dispatch.md`："handed_back: a ticket's worker returned it as beyond that ticket, with why; accept, attach it elsewhere or ignore it, never attach it back"）。

**对我们的细化**：我们的 Actor 报告契约只有 `Status: Completed | Yielded | Blocked` 三态，"这活不归我"目前只能写进 Yielded 小作文让 Reasoner 语义解析。应增设**结构化退单字段**（如 `Handback: <trigger_ref> (out-of-scope)`），让范围仲裁走 Schema 而非自然语言——这正是 `react-mode-eval.md` M3"有益刹车 vs 失真返工"与 M6 $p_{\text{loss}}$ 的减震器：退单可减少无效执行，但重新评估与路由仍有成本；退回一个触发器不等于工人会话尾调用退出。

### 2.3 `HANDOFF_TAIL` 的异构后台资源移交：Subagent 即杀 vs Shell Job 托付

**证据**：`handOff()`（`dsh:src/executor/dsh.ts:588-599`）在工人向人移交时对两类后台资源做**异构处置**：

- **子代理（subagents）立即回收**：`stopSubagents(..., keepInbox)` 释放全部子代理（`dsh:src/executor/background.ts:141-153`）——子代理的唯一消费者是工人的推理上下文，工人退场即价值消亡；
- **Shell Job 留给人类并静默回收结果**：如供人评审的 dev server 继续跑，不占槽位、豁免 30 分钟限制，结束时用 `collectJob` + `{inject}` 模式静默落账，工人下次回合再读（`dsh:src/executor/dsh.ts:590-598`；`dsh:docs/003-execution.md:39`）。

**对我们的细化**：判定规则是"**资源的消费者是否在移交后存活**"——按接手方是否仍需该资源决定终止或保留。omo-slim 的后台任务在 Actor `Yielded` 时目前无显式处置规则，可直接采纳此判定。

### 2.4 `MEDIUM_OBSERVE` 的无授权只读订阅（Context-Only Subscription）

**证据**：`orchestrator_thread_follow` 让工单订阅关联线程"仅作上下文"——消息照常送达，但"**不借予 follow-up 强度、不能唤醒已结束工单**"（`dsh:src/app/tools/executor.ts:51-52`，`dsh:docs/007-architecture.md:90`）；投递卡片显式携带 `context: true` 标记（`dsh:src/executor/dsh.ts:156-159`）。

**对我们的细化**：这是"授权（Mandate）与上下文（Subscription）正交分解"的范本——全量共享群聊记录的多方输入开销（`react-mode-eval.md` §4.3 的 Group Chat 示例）被驯服为**单向、带标记、无行动权**的观察流。我们的多工单场景（如同一仓库多个并行 Actor）中，跨工单上下文共享目前只能靠 Reasoner 转述（吃 $C_{\text{tx}}$）；context-only 订阅提供了零授权泄漏的第三径。

### 2.5 `MEDIUM_MUTATE` 的单写者铁律与无授权便签（`orchestrator_ticket_note`）

**证据**：每类状态一个写者的完整产权表（`dsh:docs/007-architecture.md:259-271`）；主 Session 可向别的工作流写入纯事实便签——`orchestrator_ticket_note`："Tell a ticket's worker a fact from other work... **it grants no authority**"（`dsh:src/app/tools/main.ts:177-179`）。授权只活在不可变、带 Policy 版本、可重校验的 Mandate 里（`dsh:docs/001-domain.md:34`），且"外部文本不能改变策略"（`dsh:prompts/orchestrator.md:11`）。

**对我们的细化**：该设计意图区分背景事实与授权；是否阻止权限提升取决于各执行入口的校验，不能仅凭字段分离宣称对提示注入免疫。对照我们的实践：Reasoner 给 Actor 的派发指令里"授权范围"与"背景事实"混在同一自然语言段落中；分离二者（授权走结构化字段、事实走便签通道）可直接压低 $p_{\text{loss}}$ 与 $C_{\text{risk}}$。

### 2.6 `STATUS_POLL` 的宿主化剥离与合并去重（$C_{\text{poll}} \to 0$）

**证据**：

- 2 秒心跳 pass 驱动一切到期工作（`dsh:src/app/passes.ts:31`）；
- Effects 按 `kind/key` 合并：同一等待键的新请求**替换载荷**而非叠加排队（`dsh:docs/007-architecture.md:166-170`）——轮询风暴在队列层被压缩为单行；
- Eval 超时 → 触发器重排队（上限 `REQUEUES = 2` 次）→ 第三次记 `unavailable`（`dsh:src/core/evals.ts:21, 60-66`）——超时治理也在代码里；
- 上下文内等待被物理禁止：noWait 守卫（1.3 节）+ 提示词封堵（`dsh:prompts/orchestrator.md:16`）；仅存的一次性状态询问 `orchestrator_ticket_request_status` 被严格限界——"只问结束回合却未报告的工人，不唤醒其他工单"（`dsh:src/app/tools/main.ts:168-171`）。

**对我们的细化**：$C_{\text{poll}}$ 的最优解不是"提醒模型别轮询"，而是**让轮询无处发生**——所有"未就绪等待"都由宿主定时器与合并队列代持。omo-slim 的 `task_status` 通道可对照检查：诊断型轮询（`diagnostic=true`）之外的窥探是否已有宿主侧替代。

---

## 3. 对"三层上下文经济学不等式（稳定内核 B）"的五项工程深化

对照 `react-mode-eval.md` §2.2：$G_{\text{ctx}} + G_{\text{time}} > C_{\text{tx}} + C_{\text{state}} + C_{\text{gov}} + C_{\text{risk}}$。

### 3.1 限制可见载荷：有界卡片与渐进披露

**证据链**（四层递进）：

1. **卡片层**：一切跨边界记录投影为紧凑卡片——160 字符预览 + `read: "trigger/<id>"` 资源指针，全文仅按需拉取（`dsh:src/app/cards.ts:1-13`）；输入预览 80 字符（`dsh:src/app/cards.ts:37`）、报告预览 100 字符（`dsh:src/app/cards.ts:50`）；
2. **投递层**：触发器内联 600 字符、任务书内联 1500 字符，超限截断并附资源路径（`dsh:src/executor/dsh.ts:112-116`）；
3. **评估窗口层**：候选评估初始窗口四档上限 4000/1200/400/50（触发器本身/近邻消息/窗口消息/存根预览，`dsh:src/app/assessment.ts:30`）；
4. **宿主层**：DSH 工具结果修剪器 `thresholdChars: 8192 → head 4096 + tail 1024`，两个 preset 均配（`dsh:cordis.patch.yml:79-95, 177-193`）；资源快照走按字符总量有界的 LRU `TextCache`（`dsh:src/app/cache.ts:5`）。

**工程深化**：原记录中的预览截断可约束单张卡片的可见长度；若需推导 $\sigma_{\text{leak}}$ 上界，还要核实消息总数、完整报告与按需回读。资源访问提供审计机会，不等于已经完成 Token 计量。我们实测的 $k=4$ 模板过重问题（`react-mode-eval-benchmarks.md`）可能得到缓解：预览上限不等于生成阶段报告长度上限，也不能防止累计多条卡片或全文回读，M10 的"轻任务汇报分桶"可直接落地为模板常量而非提示词请求。

### 3.2 限制 $C_{\text{tx}}$：类型化字段与显式长度约束

**证据**：

- **原型应用工具的结构化接口**：原笔记记录主会话与工人的编排接口为类型化工具（主 Session 14 个 `orchestrator_*` + 工人 5 个），全部 Zod Schema 校验；派发即结构化字段 `{title, worktree_name, scope, intent_evidence, acknowledgement}`（`dsh:src/app/assessment.ts:19-28`）——Schema 只证明结构约束，必须另查字符串 `max`、数组界限及运行时截断，才能确认长度上限；本次缺少原型源码，不能确认派单已封顶；
- **字数常量硬编码**：便签 30 词（`NOTE_WORDS`）、IM 消息 100 词（`IM_WORDS`）（`dsh:src/infra/text.ts:3-4`），派发路由注 ≤30 词（`dsh:prompts/dispatch.md`，提示词约束本身不是代码硬上限）；
- **回复目的门控**：`orchestrator_reply` 按 purpose 白名单拒绝普通进度外发（`dsh:src/app/tools/main.ts:224-226`）。

**工程深化**：对照 `react-mode-eval.md` M4 的 Friction 占比指标——我们实测交接小作文占 Reasoner 可见输出 53%、Actor 42%（`react-mode-eval-benchmarks.md`）。该设计提示 $C_{\text{tx}}$ 有压缩空间，但结构化字段仍需模型生成，不能据此推出成本趋零：把"派发指令"从自由文本降维成结构化字段 + 有界路由注。我们的派发规格可逐步 Schema 化（目标 → 边界 → 验收标准三字段 + 30 词注）。

### 3.3 降低 $C_{\text{state}}$：会话复用与前缀稳定

**证据**：

- **① 三元组 1:1:1 绑定**：`ticketId ↔ worker-<ticketId> 会话 ↔ git worktree 目录`（`dsh:src/executor/dsh.ts:24-26`；worktree 名与任务书在 `dsh:src/executor/dsh.ts:97-111` 生成）。人类的后续消息在**同一 Mandate 下免 Eval 直接复活**已结束工单（`dsh:docs/001-domain.md:37`）；retry/restart/恢复全部复用同会话同目录（`dsh:docs/003-execution.md:141-152`）——热复用是**默认**，新工单仍需冷启动；实际 $\gamma_{\text{warm}}$ 取决于初次派发与复用派发的比例，不能恒定为 1；
- **② Cache-Aware Prompt Assembly**：15 个 prompt 部件按固定顺序装配（角色 → 团队节 → 订阅/回复 → 派发或工作 → 准入 → 移交 → 环境 → 策略），**动态 Policy 节严格放最后**——"Policy 变更不影响其前的提示文本"，且每次模型请求前重生成（`dsh:docs/004-prompts.md:46-59`，esp. :59）；系统提示按请求绑定版本号戳记并持久化各会话用过的版本（`dsh:src/app/sessions.ts:244` 的 `dsh-superagent-policy` section，`order: 9999` 且 `interpolate: false`）。

**工程深化**：$\gamma_{\text{warm}}$ 与缓存输入占比（历史记录 Actor 97.0%，`react-mode-eval-benchmarks.md`）受多种条件影响，可优化的两项条件是：**会话-目录-任务的三元组绑定**（状态不迁移）与**前缀稳定的提示装配**（高频变更段后置）。会话复用与前缀稳定都不能保证缓存命中，仍受提供商策略、缓存寿命、路由和驱逐影响。omo-slim/Reasoner 的系统提示装配顺序值得按此审计：把每轮变化的内容（时间戳、动态策略、最新状态摘要）后置。

### 3.4 优化 $G_{\text{time}}$：防饥饿的双水位并发槽位

**证据**：`WorkerSlots` 在 `maxWorkers` 之外为**人类消息唤醒**预留 +1 槽位：`reserve(ticketId, 'person')` 可超限占第 `max+1` 槽，且唤醒优先于新发射（`launches()` 把 wake 占用计入基数，`dsh:src/executor/dsh.ts:171-199`，esp. :182-196）；槽位不足时 `Later` 无损延迟 5 秒再试（`dsh:src/executor/dsh.ts:35-36, 192-196`）。

**工程深化**：并发预算对"热路径人机交互"与"冷启动新任务"**分水位**——人类追问已有热工单的延迟与发射队列长度解耦（"New work therefore cannot prevent a person from continuing an existing ticket"，`dsh:docs/003-execution.md:59`）。这同时保护 $G_{\text{time}}$（交互不排队）与 $\gamma_{\text{warm}}$ 的时间维度（热会话不被饿死过久）。omo-slim 后台任务并发上限可借鉴：为热会话 follow-up 保留唤醒优先级。

### 3.5 控制 $C_{\text{risk}}$：单问题原则 + 版本化准入的双向重校验

**证据**：

- **单问题结构化**：一次只问一个具体问题、结构化字段递送（`dsh:docs/000-philosophy.md:13`）；`human_action` 是 Ticket 上一等结构化字段（卡片直接投影 `attention`，`dsh:src/app/cards.ts:31`），且"从自由文本推断等人"被明确列为拒绝概念（`dsh:docs/001-domain.md:60`）——问题经 `question_for_trigger_id` 路由回**原始提问所在的会话**（`dsh:prompts/handoff-worker.md:1`）；
- **Mandate/Policy 双向重校验**：Mandate 不可变、记录 Policy 版本；复活/续期前 `revalidate` 重查（`dsh:docs/001-domain.md:34`，`dsh:docs/007-architecture.md:136-147`）；Policy 变更对在途工单**送达变更说明而非取消**——半成品不被规则变更摧毁（`dsh:docs/001-domain.md:32, 58`）。

**工程深化**：$C_{\text{risk}} = p_{\text{loss}} \cdot C_{\text{rework}}$ 通常只在"消息失真"轴上优化；DSH 展示了**授权轴**——规则可表达的越权可在准入重校验点拦截；模型执行中的语义范围漂移仍需核实（proactive 工单的 mandate 限制指令，`dsh:src/executor/dsh.ts:91-95`），而不是靠工人自觉汇报"我超范围了"。对应我们的 M6/M3：Yielded 一击修复率之外，可增设"授权边界重校验拦截数"作为 $p_{\text{loss}}$ 的前置减震指标。

---

## 4. 双向互补与可落地反哺清单

### 4.1 观测接入差异与待核实项

1. **原型指标接入待核实**：原笔记只展示 `HealthBoard` 组件健康和 Store 审计记录，未展示边界经济学指标，不能因此断言整个 Harness 无 usage 或 tracing 能力。当前 Harness 的 `TokenUsage` 包含 input/output/cache/reasoning 等计数（`harness:packages/llm/llm/src/types.ts`）。
2. **角色与血缘需单独映射**：`worker-<ticketId>` 只是工单关联；会话父子关系、工具控制流与任务归属不能混为一个前缀规则。原型的双层终态和边界指标是否已有实现，留待原型源码复核。
3. **reasoning 与回源需逐层核实**：当前 Harness 定义了 `ReasoningBlock`、`reasoning-delta` 和 usage，不能因未见 `opencode.reasoning` 就判定没有 reasoning 数据。Store 资源原文不等于供应商完整请求，也不覆盖所有工具输出；资源回读不能自动替代截断后的 Wire 回源。接入条件见配套规范 §5。

### 4.2 可落地反哺清单（按优先级）

- 1：反哺项：**类型化退单字段**（`Handback: <ref>`）：范围外任务走结构化退单，不写 Yielded 小作文；来源证据：2.2 节；落点：Actor 报告契约 + `react-mode-eval.md` M3/M6；增加触发器重新路由观测，避免等同会话尾调用

- 2：反哺项：**汇报模板分桶封顶**：按任务轻重分桶的卡片模板 + 截断常量，替代"写短点"提示词；来源证据：3.1 节；落点：omo-slim 汇报模板；M10 落地

- 3：反哺项：**三模态消息消费**：至少实现/模拟 `{inject}`（送达不唤醒）与请求级幂等键；来源证据：2.1 节；落点：omo-slim 后台任务通道；M5 增记"被动唤醒 Prefill 税"

- 4：反哺项：**Cache-aware 提示装配**：动态段（时间/状态/策略）严格后置 + 每请求版本戳；来源证据：3.3 节②；落点：Reasoner/Actor 系统提示装配顺序审计

- 5：反哺项：**双水位并发槽位**：热会话 follow-up 唤醒优先于新发射；来源证据：3.4 节；落点：omo-slim 并发调度

- 6：反哺项：**noWait 守卫**：禁 sleep/长超时/长 job 等待，等待物自达；来源证据：2.6 节；落点：Reasoner 工具守卫 + 提示词；$C_{\text{poll}} \to 0$

- 7：反哺项：**单问题 + 原文路由**：attention 结构化单问题，携 `question_for_trigger_id` 路由回原提问处；来源证据：3.5 节；落点：Actor 报告契约细化

- 8：反哺项：**异构后台资源处置**：移交时上下文绑定型子代理即杀、人类面向型 job 托付并静默回收；来源证据：2.3 节；落点：omo-slim Yield 时后台任务处置规则

- 9：反哺项：**授权/事实通道分离**：派发中的授权范围走结构化字段，背景事实走无授权便签（≤30 词）；来源证据：2.5 / 3.2 节；落点：Reasoner 派发规格 Schema 化

- 10：反哺项：**Context-only 订阅标记**：跨工单转发上下文时携带 `context: true`，接收方不获得行动权；来源证据：2.4 节；落点：多工单工作流的 Reasoner 转发协议

### 4.3 保留我们现状的理由（不必全盘 DSH 化）

按原记录，原型把调度与状态写入交给代码，模型仍参与 Assessment 和路由；Reason 代码字典（`dsh:src/core/reasons.ts`）是结构化审计线索，不能据此断言没有模型语义解释；且五部件单写者引擎的建设成本高。我们的研究目标是**观测与量化** Reasoner↔Actor 边界，LLM-in-the-loop 恰好暴露切面 A（派单动机）与切面 C（验收判断）的模型公开 reasoning 文本的辅助证据（`react-mode-eval.md`）。因此正确姿势是**按决策类迁移**：调度/重试/排序/授权迁移给代码（第 2.6、3.3、3.4、3.5 项工程深化），范围判断与边界语义留在模型并用 Schema 收口（第 4.2 节 1/7/9 项）。

---

*原型细节保留为待复核的历史笔记；本次仅审计文档和可用的 Harness 源码，没有改动上游实现。*

---

# 第二部分：Google Antigravity `/teamwork`（2026-11-13）

原文地址：[Advancing science & engineering with Antigravity's `/teamwork`](https://research.google/blog/advancing-science-engineering-with-antigravitys-teamwork-feature/)（Google Research Blog，Vahab Mirrokni 等；Gemini 4 Argon/Carbon/Hydrogen）。

## 与我们框架的交叉印证（不新增行动项）

文章公开的设计动机与我们的假设逐条对上：

| Antigravity 的表述 | 我们的对应概念 |
|---|---|
| 长周期任务注意力分散 → **按构造切分上下文**（每个子 Agent 只拿到该步骤精确需要的文件与工具） | 出站锚点打包（goal/why + premises + `path:line`）与子会话上下文隔离（Cut A） |
| Coordinator 移交 **fresh successors**（干净上下文的新继任者） | 子会话饱和/空返回时的冷轮转（Cold Rotation） |
| 并行探索 + **专用证伪器**（Lean 检查器、未修改的原生测试集、硬件计数器），不信任自我汇报 | Cut B 验收证据（v3.8：`Status: Completed` 必须附验收证据） |
| 用过往轨迹做 Token 效率分析，"一个 harness 优化另一个 harness"（`/boost` 即其产物） | `react-eval` 闭环（Phoenix 遥测 → 分析器 → changelog） |
| 迁移场景 reward hacking（打桩依赖/篡改测试）→ 依赖编译门控 + 锁定原始测试集 | §6 审计护栏（可见性与任务条件对称、裁判校准） |

规模参考：Weil 猜想形式化约 180 万行 Lean / 28,800 次子代理调用；Courtade–Kumar 猜想 5,000 万行 Lean 验证；Zircon 微内核两周约 80 万行安全 Rust；机群内存节省超 1 PiB。说明"上下文经济学 + 硬证伪器"范式在极端长程任务上可扩展。

## 启示 A：验证阶段的上下文选择——Warm Reuse vs Clean-Context Critic（候选研究方向）

**事实**：Antigravity 的 critic/auditor 在"不带原生成者推理历史的干净上下文"（clean contexts without the original producer's reasoning）中挑战与验证结果。

**张力**：我们当前默认在验证/小修阶段热复用刚完成实现的同一 `actor` `sessionID`（降低 $\mathrm{RAR}$ / $C_{\text{reload}}$）；但当任务存在确认偏误或"打桩绕过依赖、弱化测试迁就坏实现"的 reward hacking 风险时，携带生成者推理链的上下文容易顺着既有盲区自圆其说（推高 $p_{\text{loss}} \cdot C_{\text{risk}}$）。

**候选路由规则（待验证，未进提示词）**：

- **客观机器门禁**（既有编译器、未修改的原生测试集、确定性脚本检查）：热复用 Warm `sessionID`，$\Delta C_{\text{reload}} \approx 0$；
- **逻辑/事实审计与防篡改验收**（代码/证明审查、核对事实验证、检查测试是否被弱化）：派发 Clean-Context Cold `actor`（只传产物路径、原始需求与锁定验收标准，不传生成过程），用可控读盘开销换独立证伪能力。

**验证方式**：在受控实验样本库（benchmarks `## todo` 第 3 条）中加"审计型任务"一组：同一缺陷集分别由 warm 会话与 cold 干净会话验收，比较缺陷检出率与 Token 成本。

## 启示 B：委派子模式的细粒度原型路由（Pattern Routing）（候选研究方向）

**事实**：`/teamwork` 把工作流表达为声明式自然语言模式并动态路由；其轨迹分析得出——紧耦合代码用串行"实现 $\to$ 验证 $\to$ 修订"循环（`/boost`）在 Token 效率上显著优于并行拆分，并行的价值在"多路独立候选 + 硬证伪器择优"。

**张力**：我们当前路由只有 Inline（单文件/零过滤产出）vs Delegate（搜索/多文件/构建测试）两档，Delegate 内部不区分串行精修与并行探索。

**候选子模式（待验证，未进提示词）**：

1. **串行精修（Sequential Solve-and-Verify）**：紧耦合模块修改，单泳道迭代 + 客观门禁，规避跨泳道同步税 $C_{\text{sync}}$ 与冲突返工 $C_{\text{rework}}$；
2. **并行探索 + 专用证伪器（Parallel Exploration + Dedicated Falsifier）**：高不确定性问题，多路冷会话并行，仅凭锁定测试集/检查器结果择优晋级；
3. **生成-审计解耦（Producer + Clean-Context Critic）**：高风险变更由生成会话与独立干净审计会话配对终态把关。

**验证方式**：受控实验中按"模块间耦合度"分组对照串行 vs 并行的净收益（$\mathrm{NetSaved}$ 口径）。

---

# 第三部分：OpenCode 子代理使用分布众包（2026-11-14）

来源：OpenCode 作者 thdxr 的 Gist 众包投票（[gist.github.com/thdxr/bd8a2ae3…](https://gist.github.com/thdxr/bd8a2ae3169e5fa92648f7eedef84c10)，标题 "opencode subagent usage: general vs explore"）。方法本身即启示：一段 441 字节的提示词，让用户的 Agent 只读查询本地 `~/.local/share/opencode/opencode.db`（`session_v2` 表，`parent_id IS NOT NULL`，近 30 天），标准化回帖 `general: X% / explore: Y%`。

## 数据快照（n=20，2026-11-14 采集）

- 中位数：`general` **82.6%** / `explore` 17.4%；均值 70.3% / 29.7%；
- 11/20 用户 `general` $\ge 80\%$；仅 3/20 以 `explore` 为主（其中 2 人 100% `explore`）；
- 评论区提醒的混淆变量：比例高度依赖主模型家族（有的模型几乎只调权限全开的 `general`，Claude 系更倾向 `explore`），单用户数字不能直接当偏好结论；样本为自愿回帖，存在自选择偏差。

## 启示 C：静态权限角色切分在实践中失效 → 单一有状态 `actor` 的再确认

**解读**：OpenCode 原生把子代理按静态工具权限拆为只读 `explore` 与全能 `general`。中位数 82.6% 说明模型与用户绝大多数绕过 `explore`。按我们的经济账这符合预期：

- "先 `explore` 侦察、再 `general` 修改"意味着两次跨边界通信税 $C_{\text{tx}}$ + 冷启动断代重读（`general` 进场重读 `explore` 已读文件，$\mathrm{RAR}$ 飙升）；
- 直接调 `general`（能看、能改、能验证）反而净收益更高——模型的"绕行"在经济上是对的。

**对照我们的设计**：`react` 模式收敛为单一有状态 `actor`——只读需求由 Reasoner 在派单提示里声明写边界（read-only），侦察 $\to$ 实现 $\to$ 验证在同一 `sessionID` 内热复用（$\Delta C_{\text{reload}} = 0$）；v3.7 起单已知文件读写干脆 Inline。该众包数据构成对这套设计的独立外部定性印证。

**候选研究方向**：在受控实验中量化"`explore` $\to$ `general` 链式派单 vs 单 `actor` 热复用"的 $\mathrm{RAR}$ 与 $\mathrm{NetSaved}$ 差异，把定性印证变成定量结论。

## 启示 D：Agent 可执行的众包遥测采集（方法论启示）

**解读**：该 Gist 展示了一种低成本遥测采集范式——把指标定义编译成一段 Agent 可执行的只读查询提示词，社区用本地数据即可产出可横向对比的标准化数字。这与我们"分析器 stdlib-only、SQLite/wire JSONL 直读、不绑定单一后端"的立场同源。

**候选研究方向**：把 react 模式的关键指标（派发触发率、$\mathrm{NCR}$ 分布、`net_saved < 0` 占比）做成同款"一段提示词 + 只读本地查询"的采集器，便于未来跨用户/跨模型收集对照数据。前置条件：口径先在受控实验（benchmarks `## todo` 第 3 条）里稳定下来，避免采集到不可比的数字。

## 工程备忘：`opencode.db` 是现成的会话血缘真源

`session_v2`（缺表时回退 `session`）含 `(id, parent_id, agent, time_created)`：当 Phoenix Span 缺父子关联时，本地 `opencode.db` 可作为 `opencode.session.parent_id` 的只读替补真源（与 `raw-requests/*.jsonl` 互补）。候选：给 `scripts/phoenix_subagent_usage.py` 增加可选的 `--lineage-db` 回退（低优先级）。

**本机数据点（2026-11-15 采集，`session_v2`，近 30 天）**：子会话共 129 个（父会话 43 个）——`actor` 111（**86.0%**）；旧 omo 多工种残留 explorer 5 / fixer 5 / designer 2 / observer 2 / oracle 2（合计 12.4%，早期预设时期）；原生 `explore` / `general` 各 1。即本机已基本收敛到单 `actor` 形态，与启示 C 的设计判断一致。

---

# 汇总：候选研究方向清单

| 来源 | 方向 | 经济学映射 | 状态 |
|---|---|---|---|
| dsh 原型 | 反哺项 1–10（类型化退单字段、汇报模板分桶封顶、三模态消息消费、Cache-aware 提示装配、双水位并发槽位、noWait 守卫、单问题+原文路由、异构后台资源处置、授权/事实通道分离、Context-only 订阅标记；详见第一部分反哺清单） | $C_{\text{tx}}$、$C_{\text{poll}}$、$C_{\text{state}}$、$C_{\text{risk}}$ 等 | 候选，逐项待验证 |
| Antigravity | A：验证上下文路由——机器门禁走 warm / 逻辑审计走 cold | $p_{\text{loss}} \cdot C_{\text{risk}}$ vs $C_{\text{reload}}$ | 候选，待受控实验 |
| Antigravity | B：委派子模式路由——串行精修 / 并行+证伪 / 生成-审计 | $C_{\text{sync}}$、$C_{\text{rework}}$、$G_{\text{time}}$ | 候选，待受控实验 |
| OpenCode 众包 | C：单 `actor` 热复用 vs 静态权限角色链式派单 | $C_{\text{tx}}$、$\mathrm{RAR}$ | 已是现设计，待量化自证 |
| OpenCode 众包 | D："一段提示词"众包遥测采集器 | 方法论（§6 护栏） | 候选，前置=口径稳定 |
| OpenCode 众包 | E：`opencode.db` 血缘回退源 | 分析器健壮性 | 候选，低优先级 |

以上方向均不改动现有提示词；进入实施前须先通过 benchmarks `## todo` 第 3 条的可重复评估样本建立基线。
