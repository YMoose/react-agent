# 多 Agent 协作实证基准报告（`ocg-obs` / `opencode` / `default`）

> 配套理论与指标规范见：[react-mode-eval.md](react-mode-eval.md)  
> 分析脚本：`obsystem/react-agent/scripts/phoenix_subagent_usage.py`  
> 数据源：`obsystem/phoenix-runtime/data/phoenix.db` + `~/.local/share/opencode/raw-requests/ses_*.jsonl`

---

## 一、基准一：同步热复用深度协作（`ocg-obs` 项目 `agent-eval` vs 纯内联基线）

父会话 `ses_e730e619…`（Reasoner）与子会话 `ses_e730e0ee…`（Actor）在 2026-10-30 13:13–13:41 UTC 完成“升级 Phoenix 到 v21.0.0 并核查源码与配置”，共触发 **5 次同步派发**（`CALL_SYNC`，$k=1$ 冷启动，$k=2\dots 5$ 全部复用同一热 `sessionID`）。

### 1.1 逐派发三刀切边界记分表（M1–M3 实测）

| $k$ | 时间窗 (UTC) | 耗时 | 模式 | $T_{\text{dispatch}}$ | Actor LLM/TOOL | 原始工具返回 chars | $\Delta\mathrm{Ctx}_k$ | $T_{\text{report}}$ (精确) | $\mathrm{NCR}_{\text{char}}$ | **$\mathrm{NCR}_{\text{ctx}}$** | $N_{\text{rem}}$ | 复利省下 tok-calls | 协议/语义终态 | 经济判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **1** | 13:13:39→13:15:27 | 108s | 冷启动 | ~622 | 8 / 16 | 14,545 | 16,072 | **1,159** | 5.39 | **13.87** | 9 | 45,270 | `completed` / `Completed` | **`HIGH_GAIN`** |
| **2** | 13:16:04→13:19:05 | 181s | 热复用 | **856** | 13 / 13 | 17,159 | 12,082 | **1,409** | 5.63 | **8.57** | 8 | 47,136 | `completed` / `Completed` | **`HIGH_GAIN`** |
| **3** | 13:21:21→13:23:39 | 139s | 热复用 | **794** | 8 / 10 | 9,286 | 8,802 | **975** | 4.15 | **9.03** | 6 | 17,856 | `completed` / `Completed` | **`HIGH_GAIN`** |
| **4** | 13:32:26→13:33:21 | 55s | 热复用 | **379** | 4 / 4 | 8,493 | 4,995 | **1,101** | **2.90** | **4.54** | 3 | 7,539 | `completed` / `Completed` | **`MODERATE_GAIN`** |
| **5** | 13:35:10→13:40:48 | 338s | 热复用 | **481** | 29 / 43 | **101,077** | **41,449** | **1,964** | **20.30** | **21.10** | 1 | 41,047 | `completed` / `Completed` | **`HIGH_GAIN`** |
| **合计** | 27.8m | 821s | $\gamma_{\text{warm}}=80\%$ | **~3,132** | **62 / 86** | **150,560** | **83,400** | **6,608** | **9.48** | **12.62** | $\sum=27$ | **158,848** | 5/5 `Completed` | **0 返工** |

### 1.2 全量 Wire Body 三刀切边界审计结论

1. **$k=1$（冷启动侦察 + 软性前提纠偏，$\mathrm{NCR}_{\text{ctx}}=13.87, N_{\text{rem}}=9$）**：
   * 用户提问假设 Phoenix 通过 `uvx` 安装；Actor 探盘后在报告 `Issues & Observations` 中证伪（实为 `obsystem/phoenix-runtime/.venv` 下的 `uv pip install`），Reasoner 在切面 C（$R_2$ 的 `opencode.reasoning`）仅凭报告一次修正升级方案，并在 $k=2$ 顺利完成升级与五项验证。
2. **$k=4$（抓到“规则驱动越过经济边界”的直接证据，$\mathrm{NCR}_{\text{char}}=2.90$）**：
   * 在 $R_4$ 的切面 A（`opencode.reasoning`）中，Reasoner 原话写道：
     > *"Actually this is a simple read task — but per my role, discovery should go through an actor. It's a quick one. Let me reuse the warm session."*
   * 因为 Actor 仅执行了 4 次工具调用（3 次 `shell` + 1 次 `read`），却按固定四段式模板写了 1,101 Tokens 报告，导致 $\mathrm{NCR}_{\text{char}}$ 跌至 $2.90:1$。该次调用之所以仍保持正收益（`MODERATE_GAIN`），全靠 $\gamma_{\text{warm}}=1$ 免除了冷启动成本，且顺手校正了技能文档（`.jsonc`）与实机配置（`.json`）的差异。
3. **$k=5$（深度源码追查，$\mathrm{NCR}_{\text{ctx}}=21.10$）**：
   * Actor 连续执行 43 次工具调用（22 次源码分段 `read` + 21 次 `shell`），吞下 101,077 字符原始输出，最终向主窗口仅注入 1,964 Tokens 报告，实现 **21 倍上下文压缩**。

---

### 1.3 全局账单与重复读盘对照：`Reasoner + Actor` vs 纯内联基线（M2 & M4 实测）

| 会话 | 模式与角色 | LLM 轮数 | **Context Peak（峰值窗口）** | 累计总 Prefill (`input_tokens`) | KV Cache 命中率 | 可见输出 (`out_vis`) | 区间感知真重复读盘 (`redundant`) |
|---|---|---|---|---|---|---|---|
| **`ses_e730e619…`（父）** | **Reasoner（双角色主脑）** | 10 | **30,166** | 201,899 | 86.6% | 5,930（含派发 3,132 = 53%） | **0 次（0%）** |
| **`ses_e730e0ee…`（子）** | **Actor（热复用×4）** | 62 | 81,401 | 2,691,154 | **97.0%** | 15,597（含报告 6,608 = 42%） | **1 次 / 27 读（3.7%）** |
| `B1: ses_e7841…` | 纯内联（短任务 9.7m） | 23 | 33,547 | 517,666 | 94.0% | 5,101 | 1 次 / 2 读（50%） |
| `B2: ses_e7860…` | **纯内联（同规模 74 轮）** | 74 | **195,749** *(3×Compaction)* | **6,968,275** | 79.5% | 72,994 | **15 次完全重复 / 24 读（62.5%）** |
| `B3: ses_e799a…` | 纯内联为主（跨两天） | 114 | **175,941** | 14,075,242 | 90.7% | 47,506 | 6 个文件多次全文重读 |

* **主脑峰值窗口压缩 $6.5\times$**：在同等 ~73 轮 LLM 推理规模下，Reasoner 峰值窗口仅 **30,166 Tokens**（内联 `B2` 高达 **195,749 Tokens** 并触发 3 次 Compaction）。
* **累计总 Prefill 节省 $58.5\%$**：主从合计 2.89M Input Tokens，对比 `B2` 的 6.97M 立省 408 万 Input Tokens。
* **交接小作文摩擦占比**：Reasoner 写派发指令共 ~3,132 Tokens（占主可见输出 53%），Actor 写汇报共 6,608 Tokens（占子可见输出 42%），合计约 9,740 Tokens 的生成摩擦，换取了 408 万 Prefill Tokens 的节省与零 Compaction 失忆。
* **自指验证（第 6 样本）**：执行本次指标分析的子会话 `ses_e70bb348…` 自身实测 $\mathrm{NCR}_{\text{ctx}} = 14.06:1$（吞下 103,102 字符原始查询结果，回传 5,276 Tokens 报告），与 `agent-eval` 样本高度一致。

---

## 二、基准二：异步并发扇出与 `task_*` 控制面实测（`opencode` 与 `default` 项目，M5 实测）

| 项目与父会话 | 协作拓扑与原语特征 | 实测指标表现 |
|---|---|---|
| **`opencode`**：`ses_e828c9c15ffe…` | 5×`CALL_ASYNC`（其中 3 路 explorer 并发扇出 + 2 路 fixer） | • 启动句柄返回耗时仅 11.9–40.9s，子会话在后台实际执行 238–581s<br>• 全会话串行子执行总时长 **2,172.3s** vs 并集墙钟 **1,304.2s** $\implies$ **$\Delta t_{\text{parallel}} = 868.0\text{s}$（整体省时 40%；其中 3 路 explorer 并发波段 1,449s→581s，省时 60%）** |
| **`default`**：`ses_e975c9c8effe…` | 1×`CALL_SYNC` + 4×`CALL_ASYNC`（未传 `background` 参数但走默认异步） | • 完整验证两条异步报告注入通道：2 路经 **`MSG_EMIT(notify)`（原生完成通知落入父 Wire Body）** 注入，2 路经 **`MSG_CONSUME(pull)`（调用 `task_status(result=true)`，耗时 0.0–0.4s）** 拉取注入 |

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
2. **豁免口过宽**：检查切面 A 的 `opencode.reasoning` 是否逐字引用了提示词中的宽泛豁免条款（如 *"Handle trivial, self-contained edits directly"*）进行自我豁免。
3. **首步滑坡（First-Step Inertia）**：检查父会话首个 TOOL Span 是否为亲自调用的 `read` / `shell`——一旦主会话为“先看一眼好写规格”而亲自读盘，后续极易顺着上下文惯性一路单干到底。
