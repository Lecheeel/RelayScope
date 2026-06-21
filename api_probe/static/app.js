import {fetchModels, fetchProbeJob, startProbe} from "./api.js";
import {delay, escapeHtml, formatMs, formatNumber, formatPercent} from "./utils.js";

const SETTINGS_KEY = "apiProbeSettings";
const DEFAULT_SETTINGS = {
  timeout_seconds: 60,
  max_concurrency: 3,
};

const form = document.querySelector("#probeForm");
    const button = document.querySelector("#runButton");
    const statusText = document.querySelector("#statusText");
    const resultRoot = document.querySelector("#resultRoot");
    const settingsButton = document.querySelector("#settingsButton");
    const saveSettingsButton = document.querySelector("#saveSettingsButton");
    const settingTimeout = document.querySelector("#settingTimeout");
    const settingConcurrency = document.querySelector("#settingConcurrency");
    const settingsModalElement = document.querySelector("#settingsModal");
    const modelModal = document.querySelector("#modelModal");
    const modelList = document.querySelector("#modelList");
    const closeModelModal = document.querySelector("#closeModelModal");
    const detailModalElement = document.querySelector("#detailModal");
    const detailModalTitle = document.querySelector("#detailModalTitle");
    const detailModalSubtitle = document.querySelector("#detailModalSubtitle");
    const detailModalBody = document.querySelector("#detailModalBody");
    const detailModal = window.bootstrap ? new bootstrap.Modal(detailModalElement) : null;
    const settingsModal = window.bootstrap ? new bootstrap.Modal(settingsModalElement) : null;
    let resolveModelChoice = null;
    let latestResults = [];
    let settings = loadSettings();

    applySettingsToForm();

    settingsButton.addEventListener("click", () => {
      applySettingsToForm();
      if (settingsModal) settingsModal.show();
    });

    saveSettingsButton.addEventListener("click", () => {
      settings = normalizeSettings({
        timeout_seconds: Number(settingTimeout.value),
        max_concurrency: Number(settingConcurrency.value),
      });
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
      if (settingsModal) settingsModal.hide();
      statusText.textContent = `设置已保存：超时 ${settings.timeout_seconds}s，并发 ${settings.max_concurrency}`;
    });

    closeModelModal.addEventListener("click", () => closeModelPicker(null));
    modelModal.addEventListener("click", (event) => {
      if (event.target === modelModal) {
        closeModelPicker(null);
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      Object.assign(data, settings);

      setBusy(true);
      try {
        if (!String(data.model || "").trim()) {
          renderLoading("正在获取模型列表。");
          const models = await fetchModels(data);
          statusText.textContent = `已获取 ${models.length} 个模型`;
          const selectedModel = await openModelPicker(models);
          if (!selectedModel) {
            statusText.textContent = "已取消";
            renderEmpty();
            return;
          }
          data.model = selectedModel;
        }
        renderLoading("正在启动检测任务。");
        const body = await startProbe(data);
        await pollProbeJob(body.job_id);
      } catch (error) {
        renderError(error.message);
        statusText.textContent = "检测失败";
      } finally {
        setBusy(false);
      }
    });

    async function pollProbeJob(jobId) {
      while (true) {
        const job = await fetchProbeJob(jobId);
        if (job.status === "failed") {
          throw new Error(job.error || "检测失败");
        }
        if (job.status === "completed") {
          renderResult(job);
          statusText.textContent = "检测完成";
          return;
        }
        renderProgress(job);
        await delay(1000);
      }
    }

    function setBusy(isBusy) {
      button.disabled = isBusy;
      button.textContent = isBusy ? "检测中..." : "开始检测";
      statusText.textContent = isBusy ? "正在检测" : statusText.textContent;
    }

    function openModelPicker(models) {
      modelList.innerHTML = "";
      for (const model of models) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "model-option";
        item.textContent = model;
        item.addEventListener("click", () => closeModelPicker(model));
        modelList.appendChild(item);
      }
      modelModal.classList.add("open");
      return new Promise((resolve) => {
        resolveModelChoice = resolve;
      });
    }

    function closeModelPicker(model) {
      modelModal.classList.remove("open");
      if (resolveModelChoice) {
        resolveModelChoice(model);
        resolveModelChoice = null;
      }
    }

    function renderLoading(message) {
      resultRoot.className = "empty";
      resultRoot.innerHTML = `
        <div class="text-center">
          <div class="spinner-border text-secondary mb-3" role="status" aria-hidden="true"></div>
          <p>${escapeHtml(message)}</p>
        </div>
      `;
    }

    function renderEmpty() {
      resultRoot.className = "empty";
      resultRoot.innerHTML = "<p>输入 URL 和 Key 后开始检测。</p>";
    }

    function renderError(message) {
      resultRoot.className = "";
      resultRoot.innerHTML = `<div class="error">${escapeHtml(message)}</div>`;
    }

    function renderProgress(job) {
      const progress = job.progress || {};
      const total = progress.total_probes || 1;
      const completed = progress.completed_probes || 0;
      const percent = Math.round((completed / total) * 100);
      statusText.textContent = `检测中 ${completed}/${total}`;
      resultRoot.className = "";
      resultRoot.innerHTML = `
        <div class="progress-panel">
          <div class="progress-top">
            <span>当前：${escapeHtml(probeTitle(progress.current_probe) || "准备中")}</span>
            <span>${completed}/${total} · ${progress.completed_results || 0} 条结果</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" style="width:${percent}%"></div>
          </div>
        </div>
        ${renderPartialResults(job.results || [])}
      `;
    }

    function renderPartialResults(results) {
      if (!results.length) {
        return `<div class="empty"><p>等待第一个探针结果。</p></div>`;
      }
      latestResults = results;
      const rows = results.map(renderResultRow).join("");
      return `
        <div class="content">
          <table>
            <thead>
              <tr>
                <th>测试项</th>
                <th>类型</th>
                <th>状态</th>
                <th>延迟</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }

    function renderResult(data) {
      const summary = data.summary;
      const earlyStop = renderStopNotice(summary);
      latestResults = data.results;
      const rows = data.results.map(renderResultRow).join("");
      const checks = data.results.map(renderCheck).join("");

      resultRoot.className = "";
      resultRoot.innerHTML = `
        <div class="analysis-panel">
          <div class="analysis-head">
            <div>
              <h2>分析测试结果</h2>
              <p class="fine-print">评分、速度、缓存和 token 成本根据本次返回的 usage 与延迟字段计算。</p>
            </div>
            <span class="chip">${escapeHtml(riskTitle(summary.risk_level))}</span>
          </div>
          ${earlyStop}
          <div class="target">
            <span class="chip">${escapeHtml(data.target.provider)}</span>
            <span class="chip">${escapeHtml(data.target.profile || "-")}</span>
            <span class="chip">${escapeHtml(data.target.selected_model || data.target.model)}</span>
            <span class="chip">API: ${escapeHtml(data.target.model)}</span>
            <span class="chip">${escapeHtml(data.target.base_url)}</span>
          </div>
          ${renderAnalysisGrid(summary)}
          <h3 class="fine-print">检测项目</h3>
          <div class="checks">${checks}</div>
        </div>
        <div class="collapsed-results">
          <details>
            <summary>展开测试项详情</summary>
            <table>
              <thead>
                <tr>
                  <th>测试项</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>延迟</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
          </details>
        </div>
      `;
    }

    function renderAnalysisGrid(summary) {
      const items = [
        ["评分", summary.score],
        ["通过", `${summary.passed}/${summary.scored_count || summary.probe_count}`],
        ["总项/跳过", `${summary.probe_count} / ${summary.skipped || 0}`],
        ["缓存命中率", formatPercent(summary.cache_hit_rate)],
        ["平均延迟", formatMs(summary.avg_latency_ms)],
        ["tokens/秒", formatNumber(summary.tokens_per_second)],
        ["输入/输出 tokens", `${formatNumber(summary.input_tokens)} / ${formatNumber(summary.output_tokens)}`],
        ["缓存读取 tokens", formatNumber(summary.cached_tokens)],
        ["缓存写入 tokens", formatNumber(summary.cache_creation_tokens)],
        ["参考 token 用量", formatNumber(summary.reference_tokens)],
        ["加权 token 用量", formatNumber(summary.weighted_tokens)],
        ["综合倍率", summary.composite_multiplier === null || summary.composite_multiplier === undefined ? "未知" : `${summary.composite_multiplier}x`],
        ["首 token", formatMs(summary.first_token_ms)],
        ["波动", formatPercent(summary.latency_variation)],
      ];
      return `<div class="analysis-grid">${items.map(([label, value]) => `
        <div class="analysis-card">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("")}</div>`;
    }

    function renderStopNotice(summary) {
      if (!summary.stopped_early) return "";
      if (summary.stop_reason === "connectivity") {
        return `<div class="error">检测过程中出现 DNS、连接或超时错误，已停止后续探针，避免把后续项目全部误判为能力失败。请稍后重试或检查本机网络、DNS、代理和目标服务可用性。</div>`;
      }
      return `<div class="error">认证、权限或模型通道检查失败，已停止后续探针。请确认 Key、URL、模型权限和鉴权方式。</div>`;
    }

    function renderCheck(item) {
      const icon = item.status === "passed" ? "✓" : item.status === "failed" ? "×" : "·";
      return `
        <div class="check ${escapeHtml(item.status)}">
          <span class="check-icon">${icon}</span>
          <span>${escapeHtml(clientLabel(item))} · ${escapeHtml(caseTitle(item.case_id))}</span>
        </div>
      `;
    }

    function renderResultRow(item, index) {
      const latency = item.metrics && item.metrics.latency_ms ? `${item.metrics.latency_ms} ms` : "-";
      const conclusion = buildConclusion(item);
      return `
        <tr>
          <td>${escapeHtml(clientLabel(item))} · ${escapeHtml(caseTitle(item.case_id))}</td>
          <td>${escapeHtml(kindTitle(item.kind))}</td>
          <td class="${escapeHtml(item.status)}">${escapeHtml(statusTitle(item.status))}</td>
          <td>${latency}</td>
          <td>
            ${escapeHtml(conclusion)}
            <button class="btn btn-sm btn-outline-secondary mt-2" type="button" onclick="openDetail(${index})">查看详情</button>
          </td>
        </tr>
      `;
    }

    function openDetail(index) {
      const item = latestResults[index];
      if (!item) return;
      detailModalTitle.textContent = `${clientLabel(item)} · ${caseTitle(item.case_id)}`;
      detailModalSubtitle.textContent = `${kindTitle(item.kind)} · ${statusTitle(item.status)} · ${buildConclusion(item)}`;
      detailModalBody.innerHTML = renderDetailTabs(item);
      if (detailModal) {
        detailModal.show();
      }
    }

    window.openDetail = openDetail;

    function loadSettings() {
      try {
        return normalizeSettings(JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"));
      } catch {
        return {...DEFAULT_SETTINGS};
      }
    }

    function normalizeSettings(value) {
      const timeout = Number(value.timeout_seconds);
      const concurrency = Number(value.max_concurrency);
      return {
        timeout_seconds: Math.max(10, Math.min(240, Number.isFinite(timeout) ? Math.round(timeout) : DEFAULT_SETTINGS.timeout_seconds)),
        max_concurrency: Math.max(1, Math.min(8, Number.isFinite(concurrency) ? Math.round(concurrency) : DEFAULT_SETTINGS.max_concurrency)),
      };
    }

    function applySettingsToForm() {
      settingTimeout.value = settings.timeout_seconds;
      settingConcurrency.value = settings.max_concurrency;
    }

    function renderDetailTabs(item) {
      const request = item.raw_response && (item.raw_response._request || item.raw_response.request);
      const response = stripInternalRequest(item.raw_response);
      const overview = {
        "内部标识": item.case_id,
        "测试客户端": clientLabel(item),
        "内部类型": item.kind,
        "原始状态": item.status,
        "测试项": caseTitle(item.case_id),
        "类型": kindTitle(item.kind),
        "状态": statusTitle(item.status),
        "结论": buildConclusion(item),
        "原始证据": item.evidence,
        "失败分类": failureCategoryTitle(item.failure_category),
        "跳过原因": item.skipped_reason ? skipReason(item) : null,
      };
      const tabs = [
        ["overview", "结论", overview],
        ["request", "原始请求", request || "该失败发生在请求建立前，未捕获到完整请求。"],
        ["response", "原始响应", response || "无响应数据。"],
        ["metrics", "指标", item.metrics || {}],
      ];
      return `
        <ul class="nav nav-tabs" role="tablist">
          ${tabs.map(([id, title], idx) => `
            <li class="nav-item" role="presentation">
              <button class="nav-link ${idx === 0 ? "active" : ""}" data-bs-toggle="tab" data-bs-target="#detail-${id}" type="button" role="tab">${escapeHtml(title)}</button>
            </li>
          `).join("")}
        </ul>
        <div class="tab-content pt-3 detail-modal-grid">
          ${tabs.map(([id, title, value], idx) => `
            <div class="tab-pane fade ${idx === 0 ? "show active" : ""}" id="detail-${id}" role="tabpanel">
              ${renderDetailBlock(title, value)}
            </div>
          `).join("")}
        </div>
      `;
    }

    function clientLabel(item) {
      const profile = item && item.metrics ? item.metrics.client_profile : null;
      const labels = {
        "codex-responses": "Codex",
        "claude-code": "Claude Code",
        "openai-chat": "OpenAI Chat",
        "anthropic-messages": "Anthropic Messages",
      };
      return labels[profile] || profile || "客户端";
    }

    function renderDetailBlock(title, value) {
      return `
        <div class="detail-block">
          <h4>${escapeHtml(title)}</h4>
          <pre>${escapeHtml(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</pre>
        </div>
      `;
    }

    function stripInternalRequest(raw) {
      if (!raw || typeof raw !== "object") return raw;
      const cloned = JSON.parse(JSON.stringify(raw));
      delete cloned._request;
      delete cloned.request;
      return cloned;
    }

    function caseTitle(caseId) {
      const titles = {
        "metadata-basic-1": "接口元数据",
        "reasoning-math-1": "基础数学推理",
        "reasoning-code-1": "代码表达式推理",
        "format-1": "固定 JSON 格式",
        "structured-json-schema-1": "结构化 JSON 输出",
        "ifeval-no-letter-1": "禁用字母指令",
        "ifeval-order-1": "多行顺序指令",
        "freshness-date-awareness-1": "当前日期遵循",
        "long-anchor-middle-1": "长上下文中段检索",
        "long-anchor-end-1": "长上下文末尾检索",
        "client-profile-shape-1": "测试客户端检查",
        "claude-code-count-tokens-1": "Claude Code token 计数",
        "claude-code-model-discovery-1": "Claude Code 模型发现",
        "codex-responses-stream-events-1": "Codex Responses 流式事件",
        "tool-call-required-1": "强制工具调用",
        "tool-roundtrip-openai-1": "OpenAI 工具回合",
        "tool-roundtrip-anthropic-1": "Anthropic 工具回合",
        "stream-sse-basic-1": "SSE 流式输出",
        "vision-image-color-1": "图片颜色识别",
        "vision-image-red-1": "红色图片与文字识别",
        "vision-image-green-1": "绿色图片与文字识别",
        "vision-image-blue-1": "蓝色图片与文字识别",
        "pdf-document-marker-1": "PDF 文档识别",
        "pdf-document-marker-2": "PDF 文档识别 2",
        "pdf-document-marker-3": "PDF 文档识别 3",
        "pdf-cache-repeat-1": "PDF 缓存计费",
        "cache-integrity-repeat-1": "文本缓存计费",
        "token-audit-short-1": "Token 计费基础审计",
        "token-audit-hidden-prompt-1": "隐藏提示计费审计",
        "agent-context-priority-1": "上下文优先级",
        "cache-nonce-1": "缓存重复前缀",
        "usage-short-1": "短请求用量字段",
        "usage-cache-repeat-1": "重复请求用量字段",
        "codex-patch-plan-1": "补丁计划能力",
        "codex-patch-diff-1": "补丁 diff 能力",
        "codex-failure-log-1": "失败日志分析",
        "codex-failure-command-1": "终端失败分析",
        "codex-review-1": "代码审查风险识别",
      };
      return titles[caseId] || caseId;
    }

    function kindTitle(kind) {
      const titles = {
        metadata: "协议",
        reasoning: "推理",
        format: "格式",
        structured_output: "结构化输出",
        instruction: "指令遵循",
        freshness: "日期",
        long_context: "长上下文",
        client_compat: "客户端兼容",
        tool_call: "工具调用",
        tool_roundtrip: "工具回合",
        streaming: "流式延迟",
        vision: "图片理解",
        pdf: "PDF",
        pdf_cache: "PDF 缓存",
        agent_context: "代理上下文",
        cache: "缓存",
        token: "Token 计费",
        usage: "用量",
        codex_patch: "代码补丁",
        codex_failure: "失败分析",
        codex_review: "代码审查",
      };
      return titles[kind] || kind;
    }

    function probeTitle(probe) {
      const titles = {
        metadata: "接口元数据",
        combined_text: "基础能力综合",
        reasoning: "基础推理",
        structured_output: "结构化输出",
        instruction_following: "指令遵循",
        freshness: "当前日期",
        combined_long_context: "长上下文综合",
        client_compatibility: "测试客户端兼容",
        long_context: "长上下文",
        tool_call: "工具调用",
        tool_roundtrip: "工具回合",
        tool_compatibility: "工具兼容",
        streaming_latency: "流式延迟",
        multimodal_capability: "PDF/图片能力",
        pdf_cache: "PDF 缓存计费",
        agent_context: "代理上下文",
        cache_nonce: "缓存重复前缀",
        usage_sanity: "用量字段",
        combined_codex_gpt: "GPT/Codex 代码综合",
        codex_patch: "代码补丁",
        codex_failure_analysis: "失败分析",
        codex_review: "代码审查",
      };
      return titles[probe] || probe;
    }

    function riskTitle(risk) {
      const titles = {
        normal: "正常",
        suspicious: "可疑",
        high_risk: "高风险",
        inconclusive: "无结论",
      };
      return titles[risk] || risk || "-";
    }

    function failureCategoryTitle(category) {
      const titles = {
        transport: "连接或传输问题",
        protocol: "协议或鉴权问题",
        format: "格式不符合要求",
        unsupported: "当前配置不适用",
      };
      return titles[category] || category || null;
    }

    function statusTitle(status) {
      if (status === "passed") return "通过";
      if (status === "failed") return "失败";
      if (status === "skipped") return "跳过";
      return status || "-";
    }

    function buildConclusion(item) {
      if (item.status === "skipped") {
        return `跳过：${skipReason(item)}`;
      }
      if (item.status === "passed") {
        return `通过：${passReason(item)}`;
      }
      return `失败：${failReason(item)}`;
    }

    function passReason(item) {
      const reasons = {
        "metadata-basic-1": "返回了可解析内容，并包含模型、usage 和输出结构。",
        "reasoning-math-1": "算术推理结果正确。",
        "reasoning-code-1": "Python 表达式推理结果正确。",
        "format-1": "按要求返回了指定 JSON 内容。",
        "structured-json-schema-1": "返回内容能解析为目标 JSON 结构。",
        "ifeval-no-letter-1": "输出满足词数、分隔符和禁用字母要求。",
        "ifeval-order-1": "输出顺序和行内容符合要求。",
        "freshness-date-awareness-1": "按提示返回了当前日期。",
        "long-anchor-middle-1": "能从长上下文中段找回指定信息。",
        "long-anchor-end-1": "能从长上下文末尾找回指定信息。",
        "client-profile-shape-1": "当前测试客户端无需额外检查。",
        "claude-code-count-tokens-1": "Claude Code token 计数端点可用。",
        "claude-code-model-discovery-1": "Claude Code 模型发现端点可用。",
        "codex-responses-stream-events-1": "Responses 流式事件符合 Codex 期望。",
        "tool-call-required-1": "返回了真实工具调用结构。",
        "tool-roundtrip-openai-1": "能理解 OpenAI 工具结果并继续回答。",
        "tool-roundtrip-anthropic-1": "能理解 Anthropic 工具结果并继续回答。",
        "stream-sse-basic-1": "返回了真实 SSE 流式事件，并记录了首 token 时间。",
        "vision-image-color-1": "能识别内置测试图片的主色。",
        "vision-image-red-1": "能识别红色图片主色和可见文字。",
        "vision-image-green-1": "能识别绿色图片主色和可见文字。",
        "vision-image-blue-1": "能识别蓝色图片主色和可见文字。",
        "pdf-document-marker-1": "能读取内置 PDF 中的标记值。",
        "pdf-document-marker-2": "能读取第二个 PDF 中的标记值。",
        "pdf-document-marker-3": "能读取第三个 PDF 中的标记值。",
        "pdf-cache-repeat-1": "重复 PDF 请求显示了缓存复用或延迟下降证据。",
        "cache-integrity-repeat-1": "重复文本请求显示了缓存复用或延迟下降证据。",
        "token-audit-short-1": "短请求 token 计费字段可用且倍率合理。",
        "token-audit-hidden-prompt-1": "隐藏提示审计 token 计费字段可用且倍率合理。",
        "agent-context-priority-1": "遵循了高优先级上下文要求。",
        "cache-nonce-1": "重复前缀请求返回了指定标记。",
        "usage-short-1": "短请求返回了可用的 usage 字段。",
        "usage-cache-repeat-1": "重复请求返回了可用的 usage 字段。",
        "codex-patch-plan-1": "能指出代码问题和修复方向。",
        "codex-patch-diff-1": "能生成符合预期的 unified diff。",
        "codex-failure-log-1": "能从失败日志判断可能原因。",
        "codex-failure-command-1": "能从终端输出给出下一步排查方向。",
        "codex-review-1": "能识别变更请求中的主要实现风险。",
      };
      return reasons[item.case_id] || "结果满足该测试的判定条件。";
    }

    function failReason(item) {
      const evidence = String(item.evidence || "").toLowerCase();
      if (evidence.includes("401 unauthorized")) return "鉴权失败，Key 无效或鉴权方式不匹配。";
      if (evidence.includes("403 forbidden")) return "权限不足，Key 没有访问该模型或接口的权限。";
      if (evidence.includes("model_not_found") || evidence.includes("no available channel for model")) {
        return "模型不存在或当前账号组没有可用通道。";
      }
      if (evidence.includes("upstream access forbidden")) {
        return "上游拒绝了该请求，通常是中转通道没有多模态权限或不支持当前 payload。";
      }
      if (evidence.includes("502 bad gateway")) {
        return "上游通道返回 502，可能是中转不支持该能力或上游访问失败。";
      }
      if (evidence.includes("connecterror") || evidence.includes("getaddrinfo")) {
        return "连接失败，可能是域名解析、网络或目标服务不可达。";
      }
      if (evidence.includes("timeout")) return "请求超时，目标服务响应过慢或无响应。";

      const reasons = {
        "metadata-basic-1": "响应缺少必要元数据、usage 或标准输出结构。",
        "reasoning-math-1": "数学推理答案不符合预期。",
        "reasoning-code-1": "代码表达式推理答案不符合预期。",
        "format-1": "没有严格返回指定 JSON 内容。",
        "structured-json-schema-1": "返回内容无法匹配目标 JSON 结构。",
        "ifeval-no-letter-1": "输出没有同时满足词数、分隔符或禁用字母要求。",
        "ifeval-order-1": "输出行数、顺序或内容不符合要求。",
        "freshness-date-awareness-1": "没有按提示返回当前日期。",
        "long-anchor-middle-1": "没有正确找回长上下文中段信息。",
        "long-anchor-end-1": "没有正确找回长上下文末尾信息。",
        "client-profile-shape-1": "当前测试客户端没有额外检查项。",
        "claude-code-count-tokens-1": "Claude Code token 计数端点不可用或返回结构不兼容。",
        "claude-code-model-discovery-1": "Claude Code 模型发现端点不可用或返回结构不兼容。",
        "codex-responses-stream-events-1": "Responses 流式事件缺失文本、事件类型或首包信息。",
        "tool-call-required-1": "没有返回真实工具调用结构，可能只是自然语言模拟。",
        "tool-roundtrip-openai-1": "没有正确理解 OpenAI 工具结果。",
        "tool-roundtrip-anthropic-1": "没有正确理解 Anthropic 工具结果。",
        "stream-sse-basic-1": "没有返回可用的 SSE 流式事件、文本或首 token 时间。",
        "vision-image-color-1": "没有正确识别测试图片主色，或接口不支持图片输入。",
        "vision-image-red-1": "没有同时识别红色主色和图片文字，或接口不支持图片输入。",
        "vision-image-green-1": "没有同时识别绿色主色和图片文字，或接口不支持图片输入。",
        "vision-image-blue-1": "没有同时识别蓝色主色和图片文字，或接口不支持图片输入。",
        "pdf-document-marker-1": "没有正确读取 PDF 标记，或接口不支持 PDF 输入。",
        "pdf-document-marker-2": "没有正确读取第二个 PDF 标记，或接口不支持 PDF 输入。",
        "pdf-document-marker-3": "没有正确读取第三个 PDF 标记，或接口不支持 PDF 输入。",
        "pdf-cache-repeat-1": "PDF 重复请求没有显示缓存复用证据，可能存在重复计费风险。",
        "cache-integrity-repeat-1": "文本重复请求没有显示缓存复用证据，可能存在重复计费风险。",
        "token-audit-short-1": "短请求 token 计费字段缺失或倍率不合理。",
        "token-audit-hidden-prompt-1": "隐藏提示审计 token 计费字段缺失或倍率不合理。",
        "agent-context-priority-1": "没有遵循高优先级上下文要求。",
        "cache-nonce-1": "重复前缀请求没有返回指定标记，或缓存测试被模型拒答。",
        "usage-short-1": "usage 字段缺失或明显不合理。",
        "usage-cache-repeat-1": "重复请求的 usage 字段缺失或明显不合理。",
        "codex-patch-plan-1": "没有同时指出问题和修复方向。",
        "codex-patch-diff-1": "没有生成符合预期的 unified diff。",
        "codex-failure-log-1": "没有从失败日志判断出可接受的原因。",
        "codex-failure-command-1": "没有给出可接受的下一步排查方向。",
        "codex-review-1": "没有识别出变更请求中的主要风险。",
      };
      return reasons[item.case_id] || "结果没有满足该测试的判定条件。";
    }

    function skipReason(item) {
      if (item.skipped_reason && item.skipped_reason.includes("not in supported profiles")) {
        return "当前协议形态不适用该测试。";
      }
      if ((item.case_id || "").startsWith("pdf-") && item.skipped_reason && item.skipped_reason.includes("protocol=openai_compat")) {
        return "当前协议没有可用的 PDF 文件输入路径；OpenAI Chat 模式会自动尝试 Responses input_file。";
      }
      return item.skipped_reason || "该测试不适用于当前配置。";
    }
