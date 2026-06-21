async function postJson(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || "请求失败");
  }
  return body;
}

export async function fetchModels(data) {
  const body = await postJson("/api/models", data);
  if (!Array.isArray(body.models) || body.models.length === 0) {
    throw new Error("没有获取到可用模型。");
  }
  return body.models;
}

export async function startProbe(data) {
  return postJson("/api/probe/start", data);
}

export async function fetchProbeJob(jobId) {
  const response = await fetch(`/api/probe/status?job_id=${encodeURIComponent(jobId)}`);
  const job = await response.json();
  if (!response.ok) {
    throw new Error(job.error || "读取检测进度失败");
  }
  return job;
}
