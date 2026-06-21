# RelayScope

Stop guessing whether your LLM relay is actually compatible.

RelayScope is a local probe suite for auditing LLM API relays before you trust them in a real agent workflow. It checks protocol compatibility, model identity, structured outputs, tool calls, streaming, multimodal behavior, prompt cache signals, usage accounting, and client-specific behavior for Codex and Claude Code style clients.

## Why It Exists

Many LLM relay services look OpenAI-compatible or Anthropic-compatible on the surface, but break when an actual coding agent uses tool calls, structured output, streaming, cache controls, usage metadata, or provider-specific message formats.

RelayScope gives you a fast local way to find those problems before they become production bugs or billing surprises.

## What It Checks

- Codex, Claude Code, OpenAI Chat, and Anthropic Messages client profiles
- OpenAI-compatible and Anthropic-native protocol behavior
- Structured JSON output and schema handling
- Tool call creation and tool result round trips
- Streaming latency and response timing
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
