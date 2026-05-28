/**
 * module3.js — Module-3 文档管理与 GraphRAG 问答交互
 * 依赖：common.js 中的 api()、setPill()
 */

/* ========== DOM 引用 ========== */
const uploadBtn = document.getElementById("uploadBtn");
const clearTxtSelectionBtn = document.getElementById("clearTxtSelectionBtn");
const txtFiles = document.getElementById("txtFiles");
const txtDropZone = document.getElementById("txtDropZone");
const graphragStageList = document.getElementById("graphragStageList");
const queryMethod = document.getElementById("queryMethod");
const questionInput = document.getElementById("questionInput");
const askBtn = document.getElementById("askBtn");
const askMsg = document.getElementById("askMsg");
const queryProcessBox = document.getElementById("queryProcessBox");
const queryProcessList = document.getElementById("queryProcessList");
const answerBox = document.getElementById("answerBox");
const docGalleryMsg = document.getElementById("docGalleryMsg");
const docGalleryGrid = document.getElementById("docGalleryGrid");
const docPreviewModal = document.getElementById("docPreviewModal");
const docPreviewTitle = document.getElementById("docPreviewTitle");
const docPreviewBody = document.getElementById("docPreviewBody");
const docPreviewCloseBtn = document.getElementById("docPreviewCloseBtn");

/* ========== 状态变量 ========== */
let currentTaskId = null;
let timer = null;
let selectedTxtFiles = [];
let stageStatusMemory = new Map();
let lastWorkflowTaskId = null;
let module3HideDocGalleryUntilDone = false;
let module3LastRenderedDocNames = new Set();
let module3PendingRevealDocNames = new Set();

/* ========== 刷新文档库 DOM ========== */
const m3DocRefreshBtn = document.getElementById("m3DocRefreshBtn");

if (m3DocRefreshBtn) {
  m3DocRefreshBtn.addEventListener("click", () => {
    loadDocuments();
  });
}

/* ========== 重置知识库 ========== */
const m3ResetKnowledgeBtn = document.getElementById("m3ResetKnowledgeBtn");
if (m3ResetKnowledgeBtn) {
  m3ResetKnowledgeBtn.addEventListener("click", async () => {
    if (!confirm("警告：确定要重置知识库吗？所有输入文件、生成图谱及查询状态均将被清空且无法恢复。")) {
      return;
    }
    m3ResetKnowledgeBtn.disabled = true;
    try {
      const res = await api("/api/rules/reset", { method: "POST" });
      if (res.success) {
        alert("知识库已重置完成，清空了 " + (res.removed ? res.removed.length : 0) + " 个核心目录。");
        loadDocuments();
        renderWorkflowProgress(null);
        if (answerBox) {
          answerBox.hidden = true;
          answerBox.textContent = "";
        }
      } else {
        alert("重置失败或异常");
      }
    } catch (err) {
      alert("重置失败：" + err.message);
    } finally {
      m3ResetKnowledgeBtn.disabled = false;
      m3ResetKnowledgeBtn.textContent = "重置知识库";
    }
  });
}

const WORKFLOW_ACTIVE_STATUSES = new Set(["queued", "running"]);
const WORKFLOW_MAX_COMPLETED_ADVANCE_PER_TICK = 1;

const WORKFLOW_STAGE_ORDER = [
  "load_input_documents",
  "create_base_text_units",
  "create_final_documents",
  "extract_graph",
  "finalize_graph",
  "extract_covariates",
  "create_communities",
  "create_final_text_units",
  "create_community_reports",
  "generate_text_embeddings",
];

const WORKFLOW_STAGE_LABELS = {
  load_input_documents: "加载输入文档",
  create_base_text_units: "生成基础文本单元",
  create_final_documents: "生成最终文档集",
  extract_graph: "抽取图谱结构",
  finalize_graph: "图谱整理收敛",
  extract_covariates: "抽取协变量",
  create_communities: "构建社区",
  create_final_text_units: "生成最终文本单元",
  create_community_reports: "生成社区报告",
  generate_text_embeddings: "生成文本向量",
};

const TXT_DROP_ZONE_IDLE_TEXT = `<div style="display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center;">点击上传/拖拽上传<span style="font-size: 12px; color: var(--muted); font-weight: normal;">（支持单个文件，格式：.txt/.doc/.docx/.pdf）</span></div>`;
const TXT_DROP_ZONE_TIP_TEXT = "（支持单个文件，格式：.txt/.doc/.docx/.pdf）";
const DOC_GALLERY_PENDING_HINT = "入库处理中，完成后显示文档列表";
const QUERY_STATUS_TEXT = {
  running: "大模型思考中",
  completed: "思考完毕",
  failed: "思考失败",
};

/* ========== 工具函数 ========== */
function formatBytes(size) {
  const n = Number(size || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDateTime(isoText) {
  const t = String(isoText || "").trim();
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return "未知时间";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${day} ${hh}:${mm}`;
}

function isTxtFile(file) {
  return Boolean(file && typeof file.name === "string" && /\.(txt|doc|docx|pdf)$/i.test(file.name));
}

function clearTaskPolling() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function clearSelectedTxtQueue() {
  selectedTxtFiles = [];
  if (txtFiles) txtFiles.value = "";
  renderTxtDropZone();
}

function setAskStatus(text, cls = "") {
  if (!askMsg) return;
  if (!text) {
    askMsg.style.display = "none";
    askMsg.textContent = "";
    askMsg.className = "pill";
    return;
  }
  askMsg.style.display = "inline-block";
  setPill(askMsg, text, cls);
}

function getDocDisplayName(fileName) {
  const raw = String(fileName || "").trim();
  if (!raw) return "未命名文档";
  const noSuffix = raw.replace(/\.txt$/i, "").trim();
  return noSuffix || raw;
}

function setDocumentGalleryDeferred(deferred) {
  module3HideDocGalleryUntilDone = Boolean(deferred);
}

function resetQueryProcess() {
  if (queryProcessList) queryProcessList.innerHTML = "";
  if (queryProcessBox) queryProcessBox.hidden = true;
}

function showSingleQueryProcess(state, text) {
  if (!queryProcessList) return;
  queryProcessList.innerHTML = "";
  const li = document.createElement("li");
  li.className = `query-process-item ${state}`;
  li.textContent = text;
  queryProcessList.appendChild(li);
}

function startQueryProcess() {
  resetQueryProcess();
  if (queryProcessBox) queryProcessBox.hidden = false;
  showSingleQueryProcess("running", QUERY_STATUS_TEXT.running);
}

function finishQueryProcess(ok) {
  if (queryProcessBox) queryProcessBox.hidden = false;
  showSingleQueryProcess(ok ? "completed" : "failed", ok ? QUERY_STATUS_TEXT.completed : QUERY_STATUS_TEXT.failed);
}

/* ========== 左侧拖拽预览 ========== */
function renderTxtDropZone() {
  txtDropZone.innerHTML = "";

  if (!selectedTxtFiles.length) {
    const title = document.createElement("div");
    title.className = "module3-drop-empty-title";
    title.innerHTML = TXT_DROP_ZONE_IDLE_TEXT;

    txtDropZone.appendChild(title);
    return;
  }

  const title = document.createElement("div");
  title.className = "module3-drop-empty-title";
  title.textContent = `已上传文件：${selectedTxtFiles[0].name}`;
  txtDropZone.appendChild(title);
}

async function setSelectedTxtFiles(inputFiles) {
  const all = Array.from(inputFiles || []);
  const txtOnly = all.filter(isTxtFile);
  if (!txtOnly.length) {
    selectedTxtFiles = [];
    renderTxtDropZone();
    return;
  }

  selectedTxtFiles = txtOnly.slice(0, 1);
  renderTxtDropZone();
}

txtFiles.addEventListener("change", () => {
  setSelectedTxtFiles(txtFiles.files).catch(() => {
    clearSelectedTxtQueue();
  });
});

if (clearTxtSelectionBtn) {
  clearTxtSelectionBtn.addEventListener("click", () => {
    clearSelectedTxtQueue();
  });
}

txtDropZone.addEventListener("click", () => txtFiles.click());
txtDropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    txtFiles.click();
  }
});

["dragenter", "dragover"].forEach((ev) =>
  txtDropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    txtDropZone.classList.add("dragover");
  })
);

["dragleave", "drop"].forEach((ev) =>
  txtDropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    txtDropZone.classList.remove("dragover");
  })
);

txtDropZone.addEventListener("drop", (e) => {
  setSelectedTxtFiles(e.dataTransfer ? e.dataTransfer.files : null).catch(() => {
    clearSelectedTxtQueue();
  });
  txtFiles.value = "";
});

/* ========== 阶段进度渲染 ========== */
function buildDefaultWorkflowProgress() {
  const stages = WORKFLOW_STAGE_ORDER.map((key) => ({
    key,
    label: WORKFLOW_STAGE_LABELS[key] || key,
    status: "pending",
  }));
  return {
    current_stage: null,
    completed_count: 0,
    total_count: stages.length,
    percentage: 0,
    stages,
  };
}

function isWorkflowTaskActive(task) {
  const status = String(task?.status || "").toLowerCase();
  return WORKFLOW_ACTIVE_STATUSES.has(status);
}

function getDisplayedCompletedCount() {
  let count = 0;
  for (const key of WORKFLOW_STAGE_ORDER) {
    if (stageStatusMemory.get(key) === "completed") count += 1;
  }
  return count;
}

function normalizeWorkflowProgress(task) {
  const fallback = buildDefaultWorkflowProgress();
  const src = task && typeof task.workflow_progress === "object" ? task.workflow_progress : fallback;

  const sourceMap = new Map();
  const sourceStages = Array.isArray(src.stages) ? src.stages : [];
  for (const stage of sourceStages) {
    if (stage && typeof stage.key === "string") {
      sourceMap.set(stage.key, stage);
    }
  }

  const stages = WORKFLOW_STAGE_ORDER.map((key) => {
    const raw = sourceMap.get(key) || {};
    const status = ["pending", "running", "completed", "failed"].includes(raw.status) ? raw.status : "pending";
    return {
      key,
      label: String(raw.label || WORKFLOW_STAGE_LABELS[key] || key),
      status,
    };
  });

  if (task && task.status === "success") {
    for (const stage of stages) stage.status = "completed";
  } else if (task && task.status === "failed") {
    const hasFailed = stages.some((s) => s.status === "failed");
    if (!hasFailed) {
      const hasStarted = stages.some((s) => s.status !== "pending");
      if (hasStarted) {
        const running = stages.find((s) => s.status === "running") || stages[stages.length - 1] || null;
        if (running) running.status = "failed";
      }
    }
  }

  if (isWorkflowTaskActive(task) && !stages.some((s) => s.status === "failed")) {
    const sourceCompleted = stages.filter((s) => s.status === "completed").length;
    const previousDisplayedCompleted = getDisplayedCompletedCount();
    const displayCompleted = Math.min(
      stages.length,
      Math.max(
        previousDisplayedCompleted,
        Math.min(sourceCompleted, previousDisplayedCompleted + WORKFLOW_MAX_COMPLETED_ADVANCE_PER_TICK)
      )
    );

    for (let idx = 0; idx < stages.length; idx += 1) {
      stages[idx].status = idx < displayCompleted ? "completed" : "pending";
    }
    if (displayCompleted < stages.length) {
      stages[displayCompleted].status = "running";
    }
  }

  const completed = stages.filter((s) => s.status === "completed").length;
  const hasRunning = stages.some((s) => s.status === "running");
  const total = stages.length;
  let pct = total > 0 ? (completed / total) * 100 : 0;
  if (hasRunning && completed < total) pct += total > 0 ? (50 / total) : 0;

  return {
    completed_count: completed,
    total_count: total,
    percentage: Math.max(0, Math.min(100, Number(src.percentage) || pct)),
    stages,
  };
}

function renderWorkflowProgress(task) {
  const incomingTaskId = task && task.task_id ? String(task.task_id) : null;
  if (!incomingTaskId || incomingTaskId !== lastWorkflowTaskId) {
    stageStatusMemory = new Map();
  }
  lastWorkflowTaskId = incomingTaskId;

  const progress = normalizeWorkflowProgress(task);

  graphragStageList.innerHTML = "";
  for (const stage of progress.stages) {
    const previousStatus = stageStatusMemory.get(stage.key);
    const changed = previousStatus !== undefined && previousStatus !== stage.status;

    const li = document.createElement("li");
    li.className = `module3-stage-item ${stage.status}`;
    if (changed) {
      li.classList.add("stage-changed", `changed-to-${stage.status}`);
    }

    const main = document.createElement("div");
    main.className = "module3-stage-main";

    const label = document.createElement("span");
    label.className = "module3-stage-label";
    label.textContent = stage.label;
    main.appendChild(label);

    li.appendChild(main);
    graphragStageList.appendChild(li);

    stageStatusMemory.set(stage.key, stage.status);
  }
}

/* ========== 文档列表模态框 ========== */
let m3DocsModal = null;
let m3DocsModalGrid = null;
let module3CurrentDocs = []; // 保存当前最新列表

function showM3AllDocsModal(docs, revealSet) {
  if (!m3DocsModal) {
    m3DocsModal = document.createElement("div");
    m3DocsModal.className = "modal";
    m3DocsModal.innerHTML = `
      <div class="modal-content" style="max-width: 900px; height: 80vh;">
        <div class="modal-header">
          <div class="modal-title">全部知识文档（${docs.length}）</div>
          <button type="button" class="modal-close" id="m3DocsModalClose">&times;</button>
        </div>
        <div class="modal-body doc-grid" id="m3ModalDocsGridWrapper" style="align-content: start;"></div>
      </div>
    `;
    document.body.appendChild(m3DocsModal);
    m3DocsModalGrid = m3DocsModal.querySelector("#m3ModalDocsGridWrapper");
    m3DocsModal.querySelector("#m3DocsModalClose").addEventListener("click", () => {
      m3DocsModal.hidden = true;
    });
    m3DocsModal.addEventListener("click", (e) => {
      if (e.target === m3DocsModal) m3DocsModal.hidden = true;
    });
  } else {
    m3DocsModal.querySelector(".modal-title").textContent = `全部知识文档（${docs.length}）`;
  }
  
  m3DocsModalGrid.innerHTML = "";
  
  for (const doc of docs) {
    const rawName = String(doc.file_name || "");
    const rawLower = rawName.toLowerCase();
    const displayName = getDocDisplayName(rawName);
    const card = document.createElement("button");
    card.type = "button";
    card.className = "doc-card";
    card.title = displayName;
    if (revealSet.has(rawLower)) card.classList.add("doc-card-reveal");

    const icon = document.createElement("div");
    icon.className = "doc-card-icon";
    icon.textContent = "文";

    const textWrap = document.createElement("div");
    textWrap.className = "doc-card-text";

    const name = document.createElement("div");
    name.className = "doc-card-name";
    name.textContent = displayName;

    const meta = document.createElement("div");
    meta.className = "doc-card-meta";
    meta.textContent = `${formatBytes(doc.size_bytes)} · ${formatDateTime(doc.modified_at)}`;

    textWrap.appendChild(name);
    textWrap.appendChild(meta);
    card.appendChild(icon);
    card.appendChild(textWrap);

    card.addEventListener("click", async () => {
      await openDocumentPreview(rawName);
    });
    m3DocsModalGrid.appendChild(card);
  }
  
  m3DocsModal.hidden = false;
}

/* ========== 文档列表 ========== */
function renderDocumentCards(docs, revealNames = null) {
  docGalleryGrid.innerHTML = "";
  if (!Array.isArray(docs) || !docs.length) {
    docGalleryGrid.innerHTML = '<div class="empty-vehicle-msg" style="grid-column: 1/-1; height: 300px;">暂无知识文档</div>';
    module3LastRenderedDocNames = new Set();
    return;
  }

  const revealSet = revealNames instanceof Set ? revealNames : new Set();
  const renderedNames = new Set();
  
  const MAX_VISIBLE = 11;
  const visibleDocs = docs.slice(0, MAX_VISIBLE);
  const hiddenCount = docs.length - visibleDocs.length;

  for (const doc of visibleDocs) {
    const rawName = String(doc.file_name || "");
    const rawLower = rawName.toLowerCase();
    const displayName = getDocDisplayName(rawName);
    const card = document.createElement("button");
    card.type = "button";
    card.className = "doc-card";
    card.title = displayName;
    if (revealSet.has(rawLower)) {
      card.classList.add("doc-card-reveal");
    }

    const icon = document.createElement("div");
    icon.className = "doc-card-icon";
    icon.textContent = "文";

    const textWrap = document.createElement("div");
    textWrap.className = "doc-card-text";

    const name = document.createElement("div");
    name.className = "doc-card-name";
    name.textContent = displayName;

    const meta = document.createElement("div");
    meta.className = "doc-card-meta";
    meta.textContent = `${formatBytes(doc.size_bytes)} · ${formatDateTime(doc.modified_at)}`;

    textWrap.appendChild(name);
    textWrap.appendChild(meta);
    card.appendChild(icon);
    card.appendChild(textWrap);

    card.addEventListener("click", async (e) => {
      await openDocumentPreview(rawName);
    });
    docGalleryGrid.appendChild(card);
    renderedNames.add(rawLower);
  }
  
  if (hiddenCount > 0) {
    const moreBtn = document.createElement("button");
    moreBtn.type = "button";
    moreBtn.className = "m5-doc-more-card";
    moreBtn.innerHTML = `<div>更多文档 (+${hiddenCount})</div>`;
    moreBtn.addEventListener("click", () => {
      showM3AllDocsModal(docs, revealSet);
    });
    docGalleryGrid.appendChild(moreBtn);
  }

  module3LastRenderedDocNames = renderedNames;
}

async function loadDocuments(force = false) {
  if (module3HideDocGalleryUntilDone && !force) {
    setDocumentGalleryDeferred(true);
    return;
  }
  setPill(docGalleryMsg, "文档列表加载中...", "");
  const docCountBadge = document.getElementById("docCountBadge");
  try {
    const data = await api("/api/rules/documents");
    const docs = Array.isArray(data.documents) ? data.documents : [];
    const revealSet = force ? module3PendingRevealDocNames : new Set();
    module3CurrentDocs = docs;
    renderDocumentCards(docs, revealSet);

    let revealCount = 0;
    for (const name of revealSet) {
      if (module3LastRenderedDocNames.has(name)) revealCount += 1;
    }
    module3PendingRevealDocNames = new Set();

    if (revealCount > 0) {
      setPill(docGalleryMsg, `已加载 ${docs.length} 份文档（新增 ${revealCount}）`, "ok");
    } else {
      setPill(docGalleryMsg, `已加载 ${docs.length} 份文档`, "ok");
    }
    if (docCountBadge) docCountBadge.textContent = docs.length > 0 ? `(${docs.length}份)` : "";
  } catch (_err) {
    // 静默处理，不在 UI 上显示加载失败
  }
}

async function openDocumentPreview(fileName) {
  const safeName = String(fileName || "").trim();
  if (!safeName) return;
  docPreviewModal.hidden = false;
  docPreviewTitle.textContent = getDocDisplayName(safeName);
  docPreviewBody.textContent = "加载中...";
  try {
    const data = await api(`/api/rules/document/${encodeURIComponent(safeName)}`);
    docPreviewTitle.textContent = getDocDisplayName(String(data.file_name || safeName));
    docPreviewBody.textContent = String(data.content || "");
  } catch (err) {
    docPreviewBody.textContent = "加载失败：" + err.message;
  }
}

function closeDocumentPreview() {
  docPreviewModal.hidden = true;
  docPreviewTitle.textContent = "文档内容";
  docPreviewBody.textContent = "";
}

docPreviewCloseBtn.addEventListener("click", closeDocumentPreview);
docPreviewModal.addEventListener("click", (e) => {
  if (e.target === docPreviewModal) closeDocumentPreview();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !docPreviewModal.hidden) closeDocumentPreview();
});

/* ========== 任务状态轮询 ========== */
async function pollTask(taskId) {
  if (!taskId) return;
  try {
    const task = await api("/api/rules/update-status/" + encodeURIComponent(taskId));
    renderWorkflowProgress(task);
    const status = String(task?.status || "").toLowerCase();

    if (status === "queued" || status === "running") {
      setDocumentGalleryDeferred(true);
    }

    if (status === "success" || status === "failed") {
      clearTaskPolling();
      setDocumentGalleryDeferred(false);
      await loadDocuments(true);
      currentTaskId = null;
    }
  } catch (_) {
    clearTaskPolling();
    setDocumentGalleryDeferred(false);
  }
}

/* ========== 上传 txt ========== */
uploadBtn.addEventListener("click", async () => {
  if (!selectedTxtFiles.length) {
    window.alert("请先选择文档文件（.txt/.doc/.docx/.pdf）");
    return;
  }

  const form = new FormData();
  for (const file of selectedTxtFiles) form.append("files", file);

  uploadBtn.disabled = true;
  uploadBtn.textContent = "载入中...";

  try {
    const data = await api("/api/rules/upload-txt", { method: "POST", body: form });
    const uploadedFiles = Array.isArray(data.uploaded_files) ? data.uploaded_files : [];
    module3PendingRevealDocNames = new Set(
      uploadedFiles
        .map((row) => String(row?.original_name || row?.stored_name || "").trim().toLowerCase())
        .filter(Boolean)
    );
    currentTaskId = data.update_task_id;
    clearTaskPolling();
    setDocumentGalleryDeferred(true);
    await pollTask(currentTaskId);
    timer = setInterval(() => pollTask(currentTaskId), 1200);
    clearSelectedTxtQueue();
  } catch (err) {
    window.alert("上传失败: " + err.message);
  } finally {
    uploadBtn.disabled = false;
    uploadBtn.textContent = "载入知识库";
  }
});

/* ========== GraphRAG 问答 ========== */
function renderAnswer(text) {
  const raw = text || "(无输出)";
  if (!answerBox) return;
  answerBox.hidden = false;
  try {
    const html = window.marked ? window.marked.parse(raw) : raw;
    const safe = window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
    answerBox.innerHTML = safe;
  } catch (_) {
    answerBox.textContent = raw;
  }
}

if (askBtn) askBtn.addEventListener("click", async () => {
  const question = (questionInput?.value || "").trim();
  if (!question) {
    setAskStatus("请先输入问题", "error");
    return;
  }
  const method = queryMethod?.value === "local" ? "local" : "global";

  askBtn.disabled = true;
  setAskStatus("", "");
  startQueryProcess();
  answerBox.hidden = true;
  answerBox.textContent = "";
  try {
    const data = await api("/api/rules/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, method, response_type: "Multiple Paragraphs" }),
    });
    finishQueryProcess(true);
    setAskStatus("", "");
    renderAnswer(data.answer || "(无输出)");
  } catch (err) {
    finishQueryProcess(false);
    setAskStatus("", "");
    renderAnswer(err.message);
  } finally {
    askBtn.disabled = false;
  }
});

/* ========== 面板激活钩子（由 module1.js 切换模块时触发） ========== */
window.module3OnPanelActivated = () => {
  resetQueryProcess();
  if (currentTaskId) {
    pollTask(currentTaskId).catch(() => {});
    return;
  }
  setDocumentGalleryDeferred(false);
  loadDocuments().catch(() => {});
};

/* ========== 初始化 ========== */
resetQueryProcess();
if (answerBox) answerBox.hidden = true;
renderWorkflowProgress(null);
renderTxtDropZone();
setDocumentGalleryDeferred(false);
loadDocuments();
