/**
 * common.js — WebUI 公共工具函数
 * 提供 api()、setPill()、isImageFile() 等全局工具
 */

/**
 * 统一 API 请求封装：自动处理 JSON / 非 JSON 响应，抛出带详细信息的 Error
 * @param {string} path - 请求路径
 * @param {RequestInit} [options] - fetch 选项
 */
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const contentType = resp.headers.get("Content-Type") || "";
  const isJson = contentType.includes("application/json");

  if (!resp.ok) {
    let detail = resp.statusText;
    if (isJson) {
      try {
        const err = await resp.json();
        detail = err.detail
          ? typeof err.detail === "string"
            ? err.detail
            : JSON.stringify(err.detail)
          : JSON.stringify(err);
      } catch (_) { /* ignore */ }
    } else {
      detail = await resp.text().catch(() => resp.statusText);
    }
    throw new Error(`HTTP ${resp.status}: ${detail}`);
  }
  if (!isJson) return resp;
  return resp.json();
}

/**
 * 设置 pill 元素的文本和样式
 * @param {HTMLElement} el
 * @param {string} text
 * @param {string} [cls] - "ok" | "error" | ""
 */
function setPill(el, text, cls = "") {
  if (!el) return;
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

/**
 * 判断文件是否为允许的图片格式
 * @param {File} file
 */
function isImageFile(file) {
  if (!file) return false;
  const allowed = new Set(["image/jpeg", "image/png", "image/bmp", "image/webp", "image/gif"]);
  if (allowed.has(file.type)) return true;
  const ext = (file.name || "").split(".").pop().toLowerCase();
  return ["jpg", "jpeg", "png", "bmp", "webp"].includes(ext);
}
