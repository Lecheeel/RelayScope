# RelayScope

别再猜你的 LLM 中转站到底能不能真跑 Agent。

RelayScope 是一个本地 LLM API 中转检测工具，用来在你真正把中转站接入 Codex、Claude Code 或其他 Agent 工作流之前，快速审计它的协议兼容性、模型身份、结构化输出、工具调用、流式响应、多模态、PDF、缓存、usage 计费和客户端适配风险。

## 为什么需要它

很多 LLM 中转服务表面上看起来兼容 OpenAI 或 Anthropic，但一旦真实编码 Agent 开始使用工具调用、结构化输出、流式响应、缓存控制、usage 元数据或特定客户端消息格式，问题就会暴露出来。

RelayScope 给你一个本地、直接、可复现的检测入口，在上线前找出兼容性问题、模型伪装风险和可疑计费信号。

## 检测能力

- 模拟 Codex、Claude Code、OpenAI Chat 和 Anthropic Messages 客户端
- 检测 OpenAI-compatible 与 Anthropic-native 协议行为
- 检测结构化 JSON 输出与 schema 处理能力
- 检测工具调用生成和工具结果回传
- 检测流式延迟、首 token 延迟和响应耗时
- 检测多模态和 PDF 请求处理能力
- 检测 prompt cache 行为和缓存 usage 指标
- 检测 usage、token 统计和可疑计费信号
- 检测模型身份与响应元数据一致性
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

如果 RelayScope 帮你发现了中转兼容性、模型伪装或可疑计费问题，欢迎点个 Star。它能让更多开发者在信任一个 LLM API 之前，先用更安全的方式测一遍。

---

# RelayScope

Stop guessing whether your LLM relay is actually compatible.

RelayScope is a local probe suite for auditing LLM API relays before you trust them in a real agent workflow. It checks protocol compatibility, model identity, structured outputs, tool calls, streaming, multimodal behavior, PDF handling, prompt cache signals, usage accounting, and client-specific behavior for Codex and Claude Code style clients.

## Why It Exists

Many LLM relay services look OpenAI-compatible or Anthropic-compatible on the surface, but break when an actual coding agent uses tool calls, structured output, streaming, cache controls, usage metadata, or provider-specific message formats.

RelayScope gives you a fast local way to find those problems before they become production bugs or billing surprises.

## What It Checks

- Codex, Claude Code, OpenAI Chat, and Anthropic Messages client profiles
- OpenAI-compatible and Anthropic-native protocol behavior
- Structured JSON output and schema handling
- Tool call creation and tool result round trips
- Streaming latency, first-token latency, and response timing
- Multimodal and PDF request handling
- Prompt cache behavior and cache usage metrics
- Usage, token accounting, and suspicious billing signals
- Model identity and response metadata consistency
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

If RelayScope helped you catch a broken relay, fake compatibility, or suspicious billing behavior, consider giving it a star. It helps other developers find a safer way to test LLM APIs before trusting them in production.

## License

MIT
