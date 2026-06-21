# RelayScope

别再为掺水中转站买单。

RelayScope 是一个本地 LLM API 中转检测工具，专门用来发现中转站的假模型、掺水模型、降质模型、暗改缓存倍率、usage 异常和客户端兼容性问题。它的目标很直接：在你把中转站接入 Codex、Claude Code 或其他 Agent 工作流之前，先测清楚这个 API 到底值不值得信任。

## 为什么需要它

很多 LLM 中转站会把问题藏在表面兼容性后面：接口能通，回答也有，但实际体验又贵又难用。常见情况包括用低质模型冒充高端模型、对模型输出做降质处理、缓存命中不透明、缓存倍率被暗改、usage 统计异常，或者在 Codex、Claude Code 这类真实 Agent 客户端里暴露协议缺陷。

RelayScope 的初衷就是解决这个痛点：不要只看中转商怎么宣传，而是用一组可复现的探针去验证模型身份、能力表现、缓存行为和计费信号。它帮你在花更多钱之前，先判断这个中转站有没有掺水。

## 检测能力

- 检测假模型、模型伪装和响应模型元数据异常
- 检测降质模型在推理、结构化输出、工具调用和长上下文任务中的表现
- 检测 prompt cache 行为、缓存命中信号和缓存 usage 指标
- 检测 usage、token 统计、缓存倍率和可疑计费信号
- 模拟 Codex、Claude Code、OpenAI Chat 和 Anthropic Messages 客户端
- 检测 OpenAI-compatible 与 Anthropic-native 协议行为
- 检测结构化 JSON 输出与 schema 处理能力
- 检测工具调用生成和工具结果回传
- 检测流式延迟、首 token 延迟和响应耗时
- 检测多模态和 PDF 请求处理能力
- 输出风险评分，并保留每个探针的详细 evidence

## 安装

```bash
python -m pip install -e .
```

需要 Python 3.11 或更高版本。

## Web 界面

启动本地界面：

```bash
api-probe-web
```

然后打开：

```text
http://127.0.0.1:8765
```

输入中转 API URL、API Key、客户端类型和模型名称即可开始检测。API Key 只会发送到本机后端用于本次检测请求。

## 命令行

检测单个目标：

```bash
api-probe \
  --provider openai \
  --base-url https://relay.example.com/v1 \
  --api-key "$OPENAI_RELAY_API_KEY" \
  --model gpt-5.5 \
  --profile codex-responses \
  --name relay-gpt55
```

使用配置文件检测：

```bash
api-probe --config configs.example.yaml
```

报告会写入配置中的输出目录，默认通常是 `runs/`。

## 配置示例

多目标配置示例见 [configs.example.yaml](configs.example.yaml)。

真实 API Key 建议通过环境变量传入。不要提交本地配置、报告或日志文件。

## 安全说明

RelayScope 面向本地审计场景：

- API Key 不会写入报告文件。
- 本地日志、报告、测试笔记、`.env` 文件和真实配置已被 git 忽略。
- 检测结果可能包含响应片段和服务商元数据，分享前请先检查报告内容。

## 点个 Star

如果 RelayScope 帮你避开了掺水中转、假模型、降质模型或可疑计费，欢迎点个 Star。它能让更多开发者在付费和接入之前，先把 LLM API 测清楚。

---

# RelayScope

Stop paying for watered-down LLM relays.

RelayScope is a local probe suite for exposing fake models, watered-down models, degraded responses, hidden cache multiplier changes, suspicious usage accounting, and client compatibility problems in LLM API relays. Its goal is simple: before you wire a relay into Codex, Claude Code, or any real agent workflow, verify whether that API is actually worth trusting.

## Why It Exists

Many LLM relay services hide serious problems behind surface-level compatibility. The endpoint works and the model replies, but the real experience can still be worse and more expensive than expected. Common issues include low-quality models pretending to be premium models, degraded output quality, opaque cache behavior, altered cache multipliers, abnormal usage accounting, or protocol defects that only appear in real clients like Codex and Claude Code.

RelayScope was built for that pain point. Instead of trusting relay marketing, it runs reproducible probes against model identity, capability behavior, cache signals, usage metadata, and billing risk. It helps you decide whether a relay is clean before you spend more money or depend on it in production.

## What It Checks

- Fake models, model spoofing, and response metadata anomalies
- Degraded model behavior across reasoning, structured output, tools, and long-context tasks
- Prompt cache behavior, cache hit signals, and cache usage metrics
- Usage, token accounting, cache multipliers, and suspicious billing signals
- Codex, Claude Code, OpenAI Chat, and Anthropic Messages client profiles
- OpenAI-compatible and Anthropic-native protocol behavior
- Structured JSON output and schema handling
- Tool call creation and tool result round trips
- Streaming latency, first-token latency, and response timing
- Multimodal and PDF request handling
- Risk scoring with detailed evidence for each probe

## Install

```bash
python -m pip install -e .
```

Python 3.11 or newer is required.

## Web UI

Start the local UI:

```bash
api-probe-web
```

Then open:

```text
http://127.0.0.1:8765
```

Enter a relay base URL, API key, client profile, and model. API keys are sent only to the local backend for the current probe request.

## CLI

Run a single target:

```bash
api-probe \
  --provider openai \
  --base-url https://relay.example.com/v1 \
  --api-key "$OPENAI_RELAY_API_KEY" \
  --model gpt-5.5 \
  --profile codex-responses \
  --name relay-gpt55
```

Run from a config file:

```bash
api-probe --config configs.example.yaml
```

Reports are written to the configured output directory, normally `runs/`.

## Config Example

See [configs.example.yaml](configs.example.yaml) for a multi-target configuration.

Use environment variables for real API keys. Do not commit local config files, reports, or logs.

## Safety Notes

RelayScope is designed for local auditing:

- API keys are not written to report files.
- Local logs, reports, test notes, `.env` files, and real config files are ignored by git.
- Probe results may contain response excerpts and provider metadata, so review reports before sharing them.

## Star

If RelayScope helped you avoid a watered-down relay, fake model, degraded model, or suspicious billing behavior, consider giving it a star. It helps other developers test an LLM API before paying for it or trusting it in production.

## License

MIT
