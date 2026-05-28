/**
 * module2.js — Module-2 智能交通监控分析助手
 * 依赖：common.js 中的 api()、setPill()
 */

const vid2File = document.getElementById("vid2File");
const vid2DropZone = document.getElementById("vid2DropZone");
const vid2FileState = document.getElementById("vid2FileState");
const vid2YoloEnhance = document.getElementById("vid2YoloEnhance");
const vid2AnalyzeBtn = document.getElementById("vid2AnalyzeBtn");
const vid2Msg = document.getElementById("vid2Msg");
const vid2Player = document.getElementById("vid2Player");
const vid2Placeholder = document.getElementById("vid2Placeholder");
const vid2AnswerBox = document.getElementById("vid2AnswerBox");
const vid2ReportActions = document.getElementById("vid2ReportActions");
const vid2ReportFormat = document.getElementById("vid2ReportFormat");
const vid2DownloadReportBtn = document.getElementById("vid2DownloadReportBtn");
const vid2ReportHint = document.getElementById("vid2ReportHint");

let vid2DownloadUrls = null;
let vid2SelectedFile = null;

function formatFileSize(sizeBytes) {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return "未知大小";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  const sizeKB = sizeBytes / 1024;
  if (sizeKB < 1024) return `${sizeKB.toFixed(1)} KB`;
  const sizeMB = sizeKB / 1024;
  return `${sizeMB.toFixed(1)} MB`;
}

function pickFirstVideoFile(fileList) {
  if (!fileList || !fileList.length) return null;
  const allowedExt = [".mp4", ".avi", ".mov", ".mkv"];
  for (const file of fileList) {
    const lowerName = (file.name || "").toLowerCase();
    if ((file.type && file.type.startsWith("video/")) || allowedExt.some((ext) => lowerName.endsWith(ext))) {
      return file;
    }
  }
  return null;
}

function setModule2SelectedFile(file) {
  vid2SelectedFile = file || null;

  const MODULE2_DROPZONE_IDLE_TEXT = `<div style="display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center;">点击上传/拖拽上传<span style="font-size: 12px; color: var(--muted); font-weight: normal;">（支持单个视频文件，格式：.mp4/.avi/.mov/.mkv）</span></div>`;
  if (vid2DropZone) {
    vid2DropZone.innerHTML = file
      ? `已选择视频：${file.name}（点击可重新选择，或拖拽替换）`
      : MODULE2_DROPZONE_IDLE_TEXT;
  }

  if (vid2FileState) {
    vid2FileState.textContent = file
      ? `已选择：${file.name}（${formatFileSize(file.size)}）`
      : "未选择视频";
  }
}

function syncNativeFileInput(file) {
  if (!vid2File || !file) return;
  try {
    const dt = new DataTransfer();
    dt.items.add(file);
    vid2File.files = dt.files;
  } catch (_err) {
    // 某些浏览器环境不允许直接写入 FileList，此时使用 vid2SelectedFile 作为上传来源。
  }
}

if (vid2File) {
  vid2File.addEventListener("change", () => {
    const file = pickFirstVideoFile(vid2File.files);
    if (!file) {
      setModule2SelectedFile(null);
      if (vid2File.files && vid2File.files.length > 0) {
        setPill(vid2Msg, "请选择视频文件（支持.mp4/.avi/.mov/.mkv）", "error");
      }
      return;
    }
    setModule2SelectedFile(file);
  });
}

if (vid2DropZone && vid2File) {
  const openPicker = () => vid2File.click();

  vid2DropZone.addEventListener("click", openPicker);
  vid2DropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPicker();
    }
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    vid2DropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      vid2DropZone.classList.add("dragover");
    });
  });

  ["dragleave", "dragend"].forEach((eventName) => {
    vid2DropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      vid2DropZone.classList.remove("dragover");
    });
  });

  vid2DropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    vid2DropZone.classList.remove("dragover");

    const files = (event.dataTransfer && event.dataTransfer.files)
      ? Array.from(event.dataTransfer.files)
      : [];
    if (!files.length) return;

    const file = pickFirstVideoFile(files);
    if (!file) {
      setPill(vid2Msg, "请拖入视频文件（支持.mp4/.avi/.mov/.mkv）", "error");
      return;
    }

    if (files.length > 1) {
      setPill(vid2Msg, `检测到 ${files.length} 个文件，已选择第 1 个视频：${file.name}`, "");
    }

    setModule2SelectedFile(file);
    syncNativeFileInput(file);
  });
}

setModule2SelectedFile(null);

function resetModule2ReportDownload() {
  vid2DownloadUrls = null;
  if (vid2ReportActions) vid2ReportActions.style.display = "none";
  if (vid2ReportHint) vid2ReportHint.textContent = "";
}

function setVideoPlaceholder(text) {
  vid2Placeholder.style.display = "flex";
  vid2Player.style.display = "none";
  vid2Placeholder.innerHTML = `<strong>${text}</strong>`;
}

function renderMarkdownToBox(markdownText) {
  const md = markdownText || "（模型未返回内容）";
  if (window.DOMPurify && window.marked) {
    vid2AnswerBox.innerHTML = window.DOMPurify.sanitize(window.marked.parse(md));
  } else {
    vid2AnswerBox.textContent = md;
  }
}

function showM2Thinking(text) {
  vid2AnswerBox.innerHTML = `<div class="m2-thinking-box">
    <div class="m2-thinking-text">${text || "大模型分析中..."}</div>
    <div class="m2-thinking-dots"><span></span><span></span><span></span></div>
  </div>`;
}


const vid2ClearBtn = document.getElementById("vid2ClearBtn");
if (vid2ClearBtn) {
  vid2ClearBtn.addEventListener("click", () => {
    setModule2SelectedFile(null);
    if (vid2File) vid2File.value = "";
    setPill(vid2Msg, "", "");
    // 重置视频播放器
    vid2Player.style.display = "none";
    vid2Player.removeAttribute("src");
    vid2Player.load();
    vid2Placeholder.style.display = "flex";
    vid2Placeholder.innerHTML = `<strong>暂无视频流</strong>`;
    // 重置报告区域
    vid2AnswerBox.innerHTML = '<div class="empty-vehicle-msg" style="flex: 1; color: #000; font-size: 24px; font-weight: 900; letter-spacing: 2px; display: flex; align-items: center; justify-content: center;">暂无分析报告</div>';
    resetModule2ReportDownload();
    // 移除视频下方状态栏
    const bar = document.getElementById("vid2StatusBar");
    if (bar) bar.textContent = "";
  });
}

vid2AnalyzeBtn.addEventListener("click", async () => {
  const file = vid2SelectedFile || (vid2File && vid2File.files[0]);
  const yoloEnhance = Boolean(vid2YoloEnhance && vid2YoloEnhance.checked);

  if (!file) {
    setPill(vid2Msg, "请先选择一个视频文件", "error");
    return;
  }

  vid2AnalyzeBtn.disabled = true;
  resetModule2ReportDownload();
  const statusBar = document.getElementById("vid2StatusBar");

  /* ── 阶段 1：上传视频 ── */
  setPill(vid2Msg, "正在上传视频...", "status-running");
  setVideoPlaceholder("视频上传中...");
  showM2Thinking("视频上传中");

  let videoName = "";
  try {
    const form = new FormData();
    form.append("file", file);
    const upRes = await api("/api/module2/upload-video", {
      method: "POST",
      body: form
    });
    videoName = upRes.video_name;
  } catch (err) {
    setPill(vid2Msg, "视频上传失败：" + err.message, "error");
    vid2AnswerBox.innerHTML = '<div class="empty-vehicle-msg" style="flex: 1;">暂无分析报告</div>';
    vid2AnalyzeBtn.disabled = false;
    return;
  }

  /* ── 阶段 2：YOLO 处理（如开启）── */
  let analyzedVideoName = videoName;
  let analyzedSrcDir = "raw";

  if (yoloEnhance) {
    setPill(vid2Msg, "YOLO 增强检测处理中", "status-running");
    // 左边 YOLO 处理中带呼吸条
    vid2Placeholder.style.display = "flex";
    vid2Player.style.display = "none";
    vid2Placeholder.innerHTML = `<div class="m2-thinking-box"><div class="m2-thinking-text">YOLO 增强处理中</div><div class="m2-thinking-dots"><span></span><span></span><span></span></div></div>`;
    // 右边显示"大模型等待中"（无呼吸条）
    vid2AnswerBox.innerHTML = `<div class="m2-thinking-box" style="flex: 1; display: flex; align-items: center; justify-content: center;"><div class="m2-thinking-text">大模型等待中</div></div>`;

    try {
      const yoloRes = await api("/api/module2/yolo-enhance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_name: videoName, yolo_enhance: true }),
      });
      analyzedVideoName = yoloRes.processed_video_name || videoName;
      analyzedSrcDir = yoloRes.video_src_dir || "raw";
    } catch (err) {
      setPill(vid2Msg, "YOLO 处理失败：" + err.message, "status-error");
      vid2AnswerBox.innerHTML = '<span style="color: var(--error)">YOLO处理失败：<br>' + err.message + '</span>';
      vid2AnalyzeBtn.disabled = false;
      return;
    }

    /* YOLO完成后立即展示视频 */
    vid2Placeholder.style.display = "none";
    vid2Player.style.display = "block";
    vid2Player.src = `/api/module2/video/${analyzedSrcDir}/` + encodeURIComponent(analyzedVideoName) + `?t=${Date.now()}`;
    vid2Player.load();
    setPill(vid2Msg, "YOLO 增强完成，正在进行大模型视频分析", "status-running");
  } else {
    /* 非 YOLO：直接展示原始上传视频 */
    vid2Placeholder.style.display = "none";
    vid2Player.style.display = "block";
    vid2Player.src = "/api/module2/video/raw/" + encodeURIComponent(videoName) + `?t=${Date.now()}`;
    vid2Player.load();
    setPill(vid2Msg, "正在进行大模型视频分析", "status-running");
  }

  /* ── 阶段 3：大模型分析 ── */
  // 右边居中显示"大模型分析中"（带呼吸条），字号同占位符
  vid2AnswerBox.innerHTML = `
    <div class="m2-thinking-box" style="flex:1;display:flex;align-items:center;justify-content:center;">
      <div style="text-align:center;">
        <div class="m2-thinking-text">大模型分析中</div>
        <div class="m2-thinking-dots" style="justify-content:center;"><span></span><span></span><span></span></div>
      </div>
    </div>`;

  try {
    const analysis = await api("/api/module2/analyze-report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_name: videoName,
        yolo_enhance: yoloEnhance,
        processed_video_name: yoloEnhance ? analyzedVideoName : null,
      }),
    });

    if (!analysis.success) {
      throw new Error(analysis.error || "分析失败");
    }

    /* 渲染报告 */
    renderMarkdownToBox(analysis.report_markdown || "（模型未返回内容）");

    vid2DownloadUrls = analysis.download_urls || null;
    const appliedText = analysis.yolo_applied ? "已启用 YOLO 增强" : "未启用 YOLO 增强";
    if (statusBar) {
      statusBar.textContent = `${appliedText} · 报告ID: ${analysis.report_id || "-"}`;
      statusBar.style.display = "block";
    }
    if (vid2DownloadUrls) {
      vid2ReportActions.style.display = "flex";
      vid2ReportHint.textContent = "";
    }

    setPill(
      vid2Msg,
      `分析完成：${analysis.yolo_applied ? "YOLO增强+" : ""}报告已生成（${analysis.frames_used || "-"}帧）`,
      "status-ok"
    );
  } catch (err) {
    setPill(vid2Msg, "报告生成失败：" + err.message, "status-error");
    vid2AnswerBox.innerHTML = '<span style="color: var(--error)">网络异常：<br>' + err.message + '</span>';
  } finally {
    vid2AnalyzeBtn.disabled = false;
  }
});

vid2DownloadReportBtn.addEventListener("click", () => {
  const fmt = (vid2ReportFormat.value || "md").toLowerCase();
  const url = vid2DownloadUrls && vid2DownloadUrls[fmt];
  if (!url) {
    setPill(vid2Msg, "当前报告不可下载，请先完成一次分析", "error");
    return;
  }
  const a = document.createElement("a");
  a.href = url;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
});
