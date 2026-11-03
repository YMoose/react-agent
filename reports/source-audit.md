# 文档事实核查记录

核查日期：2026-11-03。仅修正文档、技能说明和技能评估预期，没有修改分析器行为、角色提示词或部署运行配置。历史遥测数值保留原记录，本次未重跑历史快照。

## 来源与路径约定

- `react:` 路径相对本仓库；核查前版本 `db9af00`。
- `obs:` 路径相对本地 `opencode-genai-obs`；版本 `6736179`。
- `opencode:` 路径相对本地 OpenCode；版本 `c5fea455b5af7765c021b808afd294937a99f9b8`。
- `omo:` 路径相对本地 `oh-my-opencode-slim`；版本 `c1d2d67b7d6460625e62c2838afd1903f6108039`。
- `harness:` 路径相对本地 `deepseek-harness`；版本 `ce7073d51254c26cd63b22a60835b8f948deb447`。
- OTel 依据为本地 `@opentelemetry/api@1.9.1` 与 `@opentelemetry/semantic-conventions@1.43.0`。后者的 GenAI 属性标为 experimental，并注明迁移至独立 GenAI 约定；不能将本次核对当作永久稳定标准。
- 本地源码存在与本次核查无关的改动；这里只读取上述相关源码，没有修改上游工作树。

## 已核实并修正

- **Span 分类与操作名**：`obs:src/spans.ts` 写入 `openinference.span.kind=AGENT/LLM/TOOL`。OTel 原生 SpanKind 是 INTERNAL/SERVER/CLIENT/PRODUCER/CONSUMER。`compaction` 是插件自定义 operation 值。
- **Token 口径**：`opencode:packages/core/src/session/usage.ts` 将可见输出和 reasoning 分开；`obs:src/spans.ts` 相加后导出 output usage，输入加上 cache read/write。总输入包含缓存，不等于实际新计算的 Prefill 工作量；缓存 Token 比率不是直接测得的 KV Cache 命中率。
- **消息顺序与完整性**：`opencode:packages/schema/src/session-event.ts` 的 Reasoning.Started/Delta/Ended 已含 `ordinal`。`obs:src/spans.ts` 仍先拼 reasoning 再拼 text；这不是上游缺序号的必然限制。插件对序列化后的消息字符串截断，可能破坏 JSON；公开 reasoning 文本也不代表完整内部思考。
- **截断与 Wire 回源**：`obs:src/config.ts`、`obs:src/truncate.ts`、`obs:src/wire.ts` 和 `obs:src/spans.ts` 证明默认 61,440 字节限制来自插件配置。JSONL 与 Span 使用同一 recorder 序号，重建 recorder 会重置；Wire 还可受帧长度设置、读取超时及写入失败影响。HTTP 时间戳在读取完成时生成。
- **指标的实际实现**：`react:scripts/phoenix_subagent_usage.py` 的 `usage_of()`、`build_economics()` 和 `classify_reads()`：`NCR_ctx` 使用包含 reasoning 的 input+output 差分代理量；`Saved` 使用工具结果字符估算再减报告量并截为非负值。`t_report_exact` 是差分候选值，污染门限为大于字符估计的 2 倍且多出 1,500 Token。`T_reload` 只识别单个既有读取区间完整覆盖的同路径 `read`，不合并区间，也不验证文件是否变化或派单是否缺锚点。
- **告警与在途分类**：同一脚本的默认值是 `child_peak_warn=120000`、`episode_gap_min=30`、`child_live_window_sec=300`、`in_flight_window_sec=600`、`beta=4.0`、`chars_per_token=4.0`。2.35 是历史样本校准值。120k 不是模型硬限制；近期活动不是存活证明。`HIGH_GAIN` 等标签按 NCR 分档，不是净经济收益的判决。
- **提示词加载**：`omo:src/config/loader.ts` 的 `loadAgentPrompt()` 会按项目、用户及 preset 路径优先级读文件；`omo:src/index.ts` 的 profile refresh 会重新构建 agent 定义。文件部署一致不证明当前请求已使用新提示词，compaction 也不是通用刷新保证，需查实际请求。技能中不存在的参考文件已改为本仓库可用资料。
- **Actor 汇报约束**：`react:prompts/fixer.md` 的六行规则只适用于不超过四次工具调用、Completed 且无异常的回合；Yielded/Blocked 仍使用完整契约。此次只修正说明，没有修改该提示词。
- **Harness 消息投递**：`harness:packages/core/agent-loop/src/agent.ts` 的 `followup()` 是 next-turn + wakeup，`steer()` 是 next-step + wakeup，`inject()` 是 next-step 且不主动唤醒。inject 不等于直接追加已消费历史，也不保证后续没有输入成本。
- **Harness subagent 与 usage**：`harness:packages/subagent/tool-subagent/src/index.ts` 启动独立子代理并立即返回 ID，完成另行通知，不能仅凭 spawn 推断同步调用。`harness:packages/llm/llm/src/types.ts` 提供 TokenUsage、ReasoningBlock 和 reasoning-delta；不能从原型未展示观测接入推出底层没有这些数据。

## 保留限制与后续事项

- `react:scripts/phoenix_subagent_usage.py` 的 `wire_body()` 仍按单独 seq 查找；跨 recorder 重建可能误配。通知匹配另用时间和 wrapper，但时间戳也不是唯一请求身份。本次如实记录限制，没有以文档修正冒充分析器修复。
- `killed_by_host_restart` 标签仍依赖结构化中断文本及 cancelled wrapper 排除法。相同文本可来自用户中断，缺少宿主原因码时不能证明 restart；未覆盖子会话也不能直接证明浪费。
- `react:reports/react-mode-eval-benchmarks.md` 是不同会话的描述性对照，未控制任务、初始上下文、缓存和完成质量。原始差值保留，但不再称为协作模式造成的账单节省。M6–M10 是规划或人工检查项，并非均已自动实现。
- `dsh-superagent-prototype` 的历史版本 `6337fc8` 未在本地找到。`react:reports/dsh-superagent-learnings.md` 中原型独有的实现细节、行号与常量统一标为待复核；当前 Harness 只支持核对通用接口，不能替代该历史版本。
- 原规范对《MemoryTax》及具体模型错判案例缺少可核查来源，本次移除归因，保留可见性对称与裁判校准的评测建议。
