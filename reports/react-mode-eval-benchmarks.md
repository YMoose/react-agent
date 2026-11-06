# 多 Agent 协作实证基准报告（`ocg-obs` / `opencode` / `default`）

> 配套理论与指标规范见：[react-mode-eval.md](react-mode-eval.md)
> 分析脚本：`obsystem/react-agent/scripts/phoenix_subagent_usage.py`
> 数据源：`obsystem/phoenix-runtime/data/phoenix.db` + `~/.local/share/opencode/raw-requests/ses_*.jsonl`

> 2026-11-03 事实核查说明：以下数值保留原评估记录，本次未重跑历史快照。`精确` 列名改为 usage 差分候选值；`Saved` 是字符估算与剩余轮次相乘的代理量。不同会话的任务、输入和完成质量未受控，差值只能作描述性对照，不能归因为协作模式的因果收益。当前公式、分类及已知局限见 [react-mode-eval.md](react-mode-eval.md) 与 [tuning-casebook.md](../skills/react-eval/references/tuning-casebook.md)。

---

## 一、基准一：同步热复用深度协作（`ocg-obs` 项目 `agent-eval` vs 纯内联基线）

父会话 `ses_e730e619…`（Reasoner）与子会话 `ses_e730e0ee…`（Actor）在 2026-10-30 13:13–13:41 UTC 完成“升级 Phoenix 到 v21.0.0 并核查源码与配置”，共触发 **5 次同步派发**（`CALL_SYNC`，$k=1$ 冷启动，$k=2\dots 5$ 全部复用同一热 `sessionID`）。

### 1.1 逐派发三刀切边界记分表（M1–M3 实测）

- $k$：**1**
  - 时间窗 (UTC)：13:13:39→13:15:27
  - 耗时：108s
  - 模式：冷启动
  - $T_{\text{dispatch}}$：~622
  - Actor LLM/TOOL：8 / 16
  - 原始工具返回 chars：14,545
  - $\Delta\mathrm{Ctx}_k$：16,072
  - $T_{\text{report}}$ (usage 差分候选)：**1,159**
  - $\mathrm{NCR}_{\text{char}}$：5.39
  - **$\mathrm{NCR}_{\text{ctx}}$**：**13.87**
  - $N_{\text{rem}}$：9
  - 复利省下 tok-calls：45,270
  - 协议/语义终态：`completed` / `Completed`
  - 经济判定：**`HIGH_GAIN`**

- $k$：**2**
  - 时间窗 (UTC)：13:16:04→13:19:05
  - 耗时：181s
  - 模式：热复用
  - $T_{\text{dispatch}}$：**856**
  - Actor LLM/TOOL：13 / 13
  - 原始工具返回 chars：17,159
  - $\Delta\mathrm{Ctx}_k$：12,082
  - $T_{\text{report}}$ (usage 差分候选)：**1,409**
  - $\mathrm{NCR}_{\text{char}}$：5.63
  - **$\mathrm{NCR}_{\text{ctx}}$**：**8.57**
  - $N_{\text{rem}}$：8
  - 复利省下 tok-calls：47,136
  - 协议/语义终态：`completed` / `Completed`
  - 经济判定：**`HIGH_GAIN`**

- $k$：**3**
  - 时间窗 (UTC)：13:21:21→13:23:39
  - 耗时：139s
  - 模式：热复用
  - $T_{\text{dispatch}}$：**794**
  - Actor LLM/TOOL：8 / 10
  - 原始工具返回 chars：9,286
  - $\Delta\mathrm{Ctx}_k$：8,802
  - $T_{\text{report}}$ (usage 差分候选)：**975**
  - $\mathrm{NCR}_{\text{char}}$：4.15
  - **$\mathrm{NCR}_{\text{ctx}}$**：**9.03**
  - $N_{\text{rem}}$：6
  - 复利省下 tok-calls：17,856
  - 协议/语义终态：`completed` / `Completed`
  - 经济判定：**`HIGH_GAIN`**

- $k$：**4**
  - 时间窗 (UTC)：13:32:26→13:33:21
  - 耗时：55s
  - 模式：热复用
  - $T_{\text{dispatch}}$：**379**
  - Actor LLM/TOOL：4 / 4
  - 原始工具返回 chars：8,493
  - $\Delta\mathrm{Ctx}_k$：4,995
  - $T_{\text{report}}$ (usage 差分候选)：**1,101**
  - $\mathrm{NCR}_{\text{char}}$：**2.90**
  - **$\mathrm{NCR}_{\text{ctx}}$**：**4.54**
  - $N_{\text{rem}}$：3
  - 复利省下 tok-calls：7,539
  - 协议/语义终态：`completed` / `Completed`
  - 经济判定：**`MODERATE_GAIN`**

- $k$：**5**
  - 时间窗 (UTC)：13:35:10→13:40:48
  - 耗时：338s
  - 模式：热复用
  - $T_{\text{dispatch}}$：**481**
  - Actor LLM/TOOL：29 / 43
  - 原始工具返回 chars：**101,077**
  - $\Delta\mathrm{Ctx}_k$：**41,449**
  - $T_{\text{report}}$ (usage 差分候选)：**1,964**
  - $\mathrm{NCR}_{\text{char}}$：**20.30**
  - **$\mathrm{NCR}_{\text{ctx}}$**：**21.10**
  - $N_{\text{rem}}$：1
  - 复利省下 tok-calls：41,047
  - 协议/语义终态：`completed` / `Completed`
  - 经济判定：**`HIGH_GAIN`**

- $k$：**合计**
  - 时间窗 (UTC)：27.8m
  - 耗时：821s
  - 模式：$\gamma_{\text{warm}}=80\%$
  - $T_{\text{dispatch}}$：**~3,132**
  - Actor LLM/TOOL：**62 / 86**
  - 原始工具返回 chars：**150,560**
  - $\Delta\mathrm{Ctx}_k$：**83,400**
  - $T_{\text{report}}$ (usage 差分候选)：**6,608**
  - $\mathrm{NCR}_{\text{char}}$：**9.48**
  - **$\mathrm{NCR}_{\text{ctx}}$**：**12.62**
  - $N_{\text{rem}}$：$\sum=27$
  - 复利省下 tok-calls：**158,848**
  - 协议/语义终态：5/5 `Completed`
  - 经济判定：**0 返工**

### 1.2 全量 Wire Body 三刀切边界审计结论

1. **$k=1$（冷启动侦察 + 软性前提纠偏，$\mathrm{NCR}_{\text{ctx}}=13.87, N_{\text{rem}}=9$）**：
   * 用户提问假设 Phoenix 通过 `uvx` 安装；Actor 探盘后在报告 `Issues & Observations` 中证伪（实为 `obsystem/phoenix-runtime/.venv` 下的 `uv pip install`），Reasoner 在切面 C（$R_2$ 的 `opencode.reasoning`）仅凭报告一次修正升级方案，并在 $k=2$ 顺利完成升级与五项验证。
2. **$k=4$（抓到“规则驱动越过经济边界”的直接证据，$\mathrm{NCR}_{\text{char}}=2.90$）**：
   * 在 $R_4$ 的切面 A（`opencode.reasoning`）中，Reasoner 原话写道：
     > *"Actually this is a simple read task — but per my role, discovery should go through an actor. It's a quick one. Let me reuse the warm session."*
   * 因为 Actor 仅执行了 4 次工具调用（3 次 `shell` + 1 次 `read`），却按固定四段式模板写了 1,101 Tokens 报告，导致 $\mathrm{NCR}_{\text{char}}$ 跌至 $2.90:1$。该次被脚本归入 `MODERATE_GAIN`，依据是 NCR 分档，不能据此证明净收益或归因为热复用；它也校正了技能文档（`.jsonc`）与实机配置（`.json`）的差异。
3. **$k=5$（深度源码追查，$\mathrm{NCR}_{\text{ctx}}=21.10$）**：
   * Actor 连续执行 43 次工具调用（22 次源码分段 `read` + 21 次 `shell`），吞下 101,077 字符原始输出，最终向主窗口仅注入 1,964 Tokens 报告，实现 **21 倍上下文压缩**。

---

### 1.3 全局账单与重复读盘对照：`Reasoner + Actor` vs 纯内联基线（M2 & M4 实测）

- 会话：**`ses_e730e619…`（父）**
  - 模式与角色：**Reasoner（双角色主脑）**
  - LLM 轮数：10
  - **Context Peak（峰值窗口）**：**30,166**
  - 累计总输入 usage (`input_tokens`)：201,899
  - 缓存输入 Token 占比：86.6%
  - 可见输出 (`out_vis`)：5,930（含派发 3,132 = 53%）
  - 区间覆盖重读候选 (`redundant`)：**0 次（0%）**

- 会话：**`ses_e730e0ee…`（子）**
  - 模式与角色：**Actor（热复用×4）**
  - LLM 轮数：62
  - **Context Peak（峰值窗口）**：81,401
  - 累计总输入 usage (`input_tokens`)：2,691,154
  - 缓存输入 Token 占比：**97.0%**
  - 可见输出 (`out_vis`)：15,597（含报告 6,608 = 42%）
  - 区间覆盖重读候选 (`redundant`)：**1 次 / 27 读（3.7%）**

- 会话：`B1: ses_e7841…`
  - 模式与角色：纯内联（短任务 9.7m）
  - LLM 轮数：23
  - **Context Peak（峰值窗口）**：33,547
  - 累计总输入 usage (`input_tokens`)：517,666
  - 缓存输入 Token 占比：94.0%
  - 可见输出 (`out_vis`)：5,101
  - 区间覆盖重读候选 (`redundant`)：1 次 / 2 读（50%）

- 会话：`B2: ses_e7860…`
  - 模式与角色：**纯内联（同规模 74 轮）**
  - LLM 轮数：74
  - **Context Peak（峰值窗口）**：**195,749** *(3×Compaction)*
  - 累计总输入 usage (`input_tokens`)：**6,968,275**
  - 缓存输入 Token 占比：79.5%
  - 可见输出 (`out_vis`)：72,994
  - 区间覆盖重读候选 (`redundant`)：**15 次完全重复 / 24 读（62.5%）**

- 会话：`B3: ses_e799a…`
  - 模式与角色：纯内联为主（跨两天）
  - LLM 轮数：114
  - **Context Peak（峰值窗口）**：**175,941**
  - 累计总输入 usage (`input_tokens`)：14,075,242
  - 缓存输入 Token 占比：90.7%
  - 可见输出 (`out_vis`)：47,506
  - 区间覆盖重读候选 (`redundant`)：6 个文件多次全文重读

* **峰值输入相差约 $6.5\times$**：两组累计 LLM 请求数接近（72 与 74），但任务条件未受控，Reasoner 峰值窗口仅 **30,166 Tokens**（内联 `B2` 高达 **195,749 Tokens** 并触发 3 次 Compaction）。
* **累计总输入相差 $58.5\%$**：主从合计 2.89M Input Tokens，对比 `B2` 的 6.97M 少约 408 万；含缓存输入，不等于账单或 Prefill 计算减少同一比例。
* **交接小作文摩擦占比**：Reasoner 写派发指令共 ~3,132 Tokens（占主可见输出 53%），Actor 写汇报共 6,608 Tokens（占子可见输出 42%），合计约 9,740 Tokens 的生成摩擦，该组记录的总输入更低且未记录 Compaction；这些观测不能证明模式带来的净节省或不存在信息丢失。
* **自指验证（第 6 样本）**：执行本次指标分析的子会话 `ses_e70bb348…` 自身实测 $\mathrm{NCR}_{\text{ctx}} = 14.06:1$（吞下 103,102 字符原始查询结果，回传 5,276 Tokens 报告），同属较高 NCR 区间；单个额外样本不构成独立效果验证。

---

## 二、基准二：异步并发扇出与 `task_*` 控制面实测（`opencode` 与 `default` 项目，M5 实测）

- **`opencode`**：`ses_e828c9c15ffe…`：协作拓扑与原语特征：5×`CALL_ASYNC`（其中 3 路 explorer 并发扇出 + 2 路 fixer）；实测指标表现：• 启动句柄返回耗时仅 11.9–40.9s，子会话在后台实际执行 238–581s<br>• 全会话串行子执行总时长 **2,172.3s** vs 并集墙钟 **1,304.2s** $\implies$ **$\Delta t_{\text{parallel}} = 868.0\text{s}$（区间重叠占比 40%；其中 3 路 explorer 波段为 60%，不是受控串行基线下测得的端到端加速）**

- **`default`**：`ses_e975c9c8effe…`：协作拓扑与原语特征：1×`CALL_SYNC` + 4×`CALL_ASYNC`（未传 `background` 参数但走默认异步）；实测指标表现：• 完整验证两条异步报告注入通道：2 路经 **`MSG_EMIT(notify)`（原生完成通知落入父 Wire Body）** 注入，2 路经 **`MSG_CONSUME(pull)`（调用 `task_status(result=true)`，耗时 0.0–0.4s）** 拉取注入

---

## 三、分析脚本运行与排障手册

### 3.1 常用命令

```bash
# 1. 分析 ocg-obs 项目（传入中英混排实测校准系数 2.35 chars/token）
python3 obsystem/react-agent/scripts/phoenix_subagent_usage.py --project ocg-obs --chars-per-token 2.35

# 2. 分析异步并发与 task_* 控制面项目
python3 obsystem/react-agent/scripts/phoenix_subagent_usage.py --project opencode
python3 obsystem/react-agent/scripts/phoenix_subagent_usage.py --project default

# 3. 离线直读 SQLite（Phoenix 停机可用）+ 导出结构化 JSON
python3 obsystem/react-agent/scripts/phoenix_subagent_usage.py \
  --db obsystem/phoenix-runtime/data/phoenix.db \
  --project ocg-obs \
  --chars-per-token 2.35 \
  --json-out /tmp/opencode/subagent-report.json
```

### 3.2 派发触发率为 0 时的三步排障清单

1. **工具注册与符号对齐**：检查首条 LLM Span `input.value` 的 `tools` 数组是否包含 `subagent`（脚本缺失时打 `⚠` 警告），并确认提示词中的工具/参数名（`subagent` / `agent` / `sessionID`）与宿主 Schema 一致（如 v2 宿主不存在 v1 的 `task_message`）。
2. **豁免口过宽**：检查切面 A 的 `gen_ai.output.messages` reasoning parts（旧 `opencode.reasoning` 作回退）是否逐字引用了提示词中的宽泛豁免条款（如 *"Handle trivial, self-contained edits directly"*）进行自我豁免。
3. **首步滑坡（First-Step Inertia）**：检查父会话首个 TOOL Span 是否为亲自调用的 `read` / `shell`——一旦主会话为“先看一眼好写规格”而亲自读盘，后续极易顺着上下文惯性一路单干到底。

---

## 四、2026-10-31 附录：盲区 D/E/F 分析器扩展回归（`ocg-obs`）

对应规范 `react-mode-eval.md` 同日修订：M1 片段内乘子 $N_{\text{rem}}^{\text{ep}}$、M2/M4 子会话饱和水线、M3 内联逃逸 $C_{\text{escape}}$ 与孤儿子会话沉没成本。新增 CLI：`--child-peak-warn 120000` / `--episode-gap-min 30` / `--in-flight-window-sec 600`；JSON 载荷新增 `child_saturation` / `orphan_children` / `params`。回归对照会话 `ses_e799a9c6…`（k1–k6）与 `ses_e7063a72…`：

- **D 子会话饱和水线**：回归判定：`ses_e70bb348…`（`A_old`，`ses_e799a9c6…` 之子）Peak(A) 判 `CHILD_OVERLOADED (Rotate Session)`；实测结果：**274,198 tok ≥ 120k ⚠rotate**（M4 账单行打标；水线表同时捕获 219,348 / 207,989 两个越线子会话）；k=5 热派发 0s `FAILED_DISPATCH`；缺少宿主原因码，未证明是上下文溢出

- **E-① 片段内乘子**：回归判定：`ses_e799a9c6…` k=1 的 `N_rem ep/raw` 与终止因；实测结果：**8/46，`end=idle_gap`**（原始跨片段计数虚增 ~5.75×）；全会话复利节省改按 $\sum N_{\text{rem}}^{\text{ep}} \cdot \Delta T_{\text{noise}}$ 口径输出

- **E-② 内联逃逸**：回归判定：k=5（0s 失败）后的父会话内联逃逸窗归属；实测结果：**12 次内联工具、59 min、ΔCtx=+148,729 tok** 全部归属 k=5（`inline_escape_ctx_delta/tools/duration_s`），汇总行 `Σ C_escape ≈ +148,729 tok`

- **F 孤儿/在途**：回归判定：`ses_e706375e7…`（父 `ses_e7063a72…` 重启丢 Span）与在途分类；实测结果：`ses_e706375e7…` 判 **`orphan_aborted_or_dropped`**（age 829 min，沉没 57,217 tok 计入 `C_rework`）；`in_flight_pending_parent` 分支经合成夹具验证（近期活跃 + 父静默 → 在途；老化 → 孤儿；父 `subagent` Span 窗口覆盖 → 无孤儿），当前快照无真实在途样本

> 此附录记录的是 2026-10-31 分类。当前脚本还优先检查 Wire 送达证据、中断通知和默认 300s 的 `in_flight` 窗口；`in_flight_pending_parent` 默认 600s 且要求父无更新 Span。年龄以 trace 最大 Span 终点为水位，缺少时才用当前时间；近期性不能证明真实存活。旧 `ses_e706375e7…` 孤儿归类后来已被修订，见 casebook Bug H；其 `killed_by_host_restart` 标签本身仍有中断原因歧义。

## todo

对 react-agent 的方法论：

1. 评分前先判断数据是否足够。
   参数缺失、卸载补发、共享回传、用户取消要分别标记；只有证据充分的边界进入对应指标。completed、报告声称完成、验收通过，也应该分别记录。
2. 拆开压缩效果、实际成本和估算收益。
   这次 27 个边界被标为 HIGH_GAIN，但独立完成边界中有 14 个估算净收益为负。建议把这个标签改成“高压缩比”，避免暗示经济收益。
   还有一个具体问题：当前计算 (react-agent/scripts/phoenix_subagent_usage.py:2074)把 原始输出量 − 报告量 截成非负数。报告更大时，应该保留负值，才能体现后续父窗口反复携带这些额外内容的成本。
3. 建立可重复的评估样本，减少事后解释。
   固定几类任务：已知文件读取、小修改、多文件调查、长构建、异步并行；控制模型、起始上下文和验收标准，再比较 inline、v3.6、v3.7。与此同时，把晚到通知、共享报告、序号重置、Code Mode 调用等案例做成脱敏回归样本。
