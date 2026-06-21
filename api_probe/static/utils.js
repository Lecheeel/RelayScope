export function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "未知";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  const abs = Math.abs(number);
  if (abs >= 1000000) return `${(number / 1000000).toFixed(2)}M`;
  if (abs >= 1000) return `${(number / 1000).toFixed(2)}K`;
  return String(number);
}

export function formatMs(value) {
  return value === null || value === undefined ? "未知" : `${value} ms`;
}

export function formatPercent(value) {
  return value === null || value === undefined ? "未知" : `${value}%`;
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
