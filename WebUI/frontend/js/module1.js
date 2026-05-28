/**
 * module1.js — Module-1 智能车库交互逻辑
 */

/* ========== DOM 引用 ========== */
const module1Btn = document.getElementById("module1Btn");
const module2Btn = document.getElementById("module2Btn");
const module3Btn = document.getElementById("module3Btn");
const module4Btn = document.getElementById("module4Btn");
const module5Btn = document.getElementById("module5Btn");
const module1Panel = document.getElementById("module1Panel");
const module2Panel = document.getElementById("module2Panel");
const module3Panel = document.getElementById("module3Panel");
const module4Panel = document.getElementById("module4Panel");
const module5Panel = document.getElementById("module5Panel");
const module1WorkbenchBtn = document.getElementById("module1WorkbenchBtn");
const module1TextSearchBtn = document.getElementById("module1TextSearchBtn");
const module1ImageSearchBtn = document.getElementById("module1ImageSearchBtn");
const module1WorkbenchView = document.getElementById("module1WorkbenchView");
const module1TextSearchView = document.getElementById("module1TextSearchView");
const module1ImageSearchView = document.getElementById("module1ImageSearchView");

const imgFiles = document.getElementById("imgFiles");
const imgFolderFiles = document.getElementById("imgFolderFiles");
const imgUploadBtn = document.getElementById("imgUploadBtn");
const imgClearStateBtn = document.getElementById("imgClearStateBtn");
const imgDropZone = document.getElementById("imgDropZone");
const imgUploadPreview = document.getElementById("imgUploadPreview");
const imgUploadMsg = document.getElementById("imgUploadMsg");
const imgAnalyzeMsg = document.getElementById("imgAnalyzeMsg");
const imgAnalyzeRetryBtn = document.getElementById("imgAnalyzeRetryBtn");
const imgAnalyzeFailedList = document.getElementById("imgAnalyzeFailedList");

const imgAnalyzeProgressWrap = document.getElementById("imgAnalyzeProgressWrap");
const imgAnalyzeProgressBar = document.getElementById("imgAnalyzeProgressBar");
const imgAnalyzeProgressText = document.getElementById("imgAnalyzeProgressText");

const imgLibraryRefreshBtn = document.getElementById("imgLibraryRefreshBtn");
const imgLibraryBulkModeBtn = document.getElementById("imgLibraryBulkModeBtn");
const imgLibrarySelectAllBtn = document.getElementById("imgLibrarySelectAllBtn");
const imgLibraryBulkDeleteBtn = document.getElementById("imgLibraryBulkDeleteBtn");
const imgLibraryMsg = document.getElementById("imgLibraryMsg");
const imgLibraryGrid = document.getElementById("imgLibraryGrid");

const vehicleInfoModal = document.getElementById("vehicleInfoModal");
const vehicleInfoCloseBtn = document.getElementById("vehicleInfoCloseBtn");
const vehicleReanalyzeBtn = document.getElementById("vehicleReanalyzeBtn");
const vehicleDeleteBtn = document.getElementById("vehicleDeleteBtn");
const vehicleInfoImage = document.getElementById("vehicleInfoImage");
const vehicleInfoImageMeta = document.getElementById("vehicleInfoImageMeta");
const vehicleInfoTitle = document.getElementById("vehicleInfoTitle");
const vehicleInfoBody = document.getElementById("vehicleInfoBody");

const imgRetrieveAllModal = document.getElementById("imgRetrieveAllModal");
const imgRetrieveAllModalTitle = document.getElementById("imgRetrieveAllModalTitle");
const imgRetrieveAllModalCloseBtn = document.getElementById("imgRetrieveAllModalCloseBtn");
const imgRetrieveAllModalBody = document.getElementById("imgRetrieveAllModalBody");
const imgRetrieveAllBulkModeBtn = document.getElementById("imgRetrieveAllBulkModeBtn");
const imgRetrieveAllBulkDeleteBtn = document.getElementById("imgRetrieveAllBulkDeleteBtn");

/* ========== 状态变量 ========== */
let module1PendingFiles = [];
let module1LibraryLoaded = false;
let module1AnalyzeRunning = false;
let module1LibraryAutoRefreshTimer = null;
let module1LibraryRefreshInFlight = false;
let module1LibraryItems = [];
let module1BulkDeleteMode = false;
let module1SelectedVehicleIds = new Set();
let module1CurrentDetailVehicleId = null;
let module1ImageUrlNonce = Date.now();

const MODULE1_LIBRARY_MAX_ROWS = 3;
const MODULE1_LIBRARY_COLS = 7;
const MODULE1_LIBRARY_CARD_MIN_WIDTH = 160;
const MODULE1_LIBRARY_GRID_GAP = 10;
const MODULE1_ALL_MODAL_BASE_CLASS = "retrieve-grid m1-all-result-grid";
const MODULE1_DROPZONE_IDLE_TEXT = `<div style="display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center;">点击上传/拖拽上传<span style="font-size: 12px; color: var(--muted); font-weight: normal;">（支持单张/多张图片及文件夹，格式：.jpg/.png/.bmp/.webp）</span></div>`;
const MODULE1_ANALYZE_PROGRESS_POLL_MS = 700;

let module1AnalyzeProgressTimer = null;

/* ========== 通用工具 ========== */
function withModule1ImageNonce(url) {
  const base = String(url || "").trim();
  if (!base) return "";
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}_m1_nonce=${module1ImageUrlNonce}`;
}

function parseVehicleIdFromImageName(imageName) {
  const text = String(imageName || "").trim();
  const m = text.match(/^vehicle_(\d+)\.[A-Za-z0-9]+$/);
  if (!m) return null;
  const vid = Number(m[1]);
  return Number.isInteger(vid) && vid > 0 ? vid : null;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeVehicleListData(data) {
  if (Array.isArray(data?.vehicles)) return data.vehicles;
  if (Array.isArray(data?.items)) return data.items;
  return [];
}

function getLoadedCountText(count) {
  const safeCount = Number.isFinite(Number(count)) ? Math.max(0, Number(count)) : 0;
  return `已加载${safeCount}辆车`;
}

function updateLibraryLoadedPill(count) {
  setPill(imgLibraryMsg, getLoadedCountText(count), "");
}

function dedupePendingFiles(files) {
  const seen = new Set(module1PendingFiles.map((f) => `${f.name}__${f.size}__${f.lastModified}`));
  const merged = [...module1PendingFiles];
  files.forEach((file) => {
    const key = `${file.name}__${file.size}__${file.lastModified}`;
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(file);
    }
  });
  return merged;
}

function renderModule1UploadPreview() {
  if (imgUploadPreview) imgUploadPreview.innerHTML = "";
}

function setModule1DropzoneText(text) {
  if (!imgDropZone) return;
  imgDropZone.innerHTML = text;
}

function setModule1PendingFiles(files) {
  module1PendingFiles = dedupePendingFiles(files.filter((f) => isImageFile(f)));
  renderModule1UploadPreview();
  if (!module1PendingFiles.length) {
    setModule1DropzoneText(MODULE1_DROPZONE_IDLE_TEXT);
    setPill(imgUploadMsg, "", "");
    return;
  }
  setModule1DropzoneText(`已选择 ${module1PendingFiles.length} 张图片（点击可继续添加，拖拽可追加）`);
  setPill(imgUploadMsg, `待上传：${module1PendingFiles.length} 张`, "");
}

function clearModule1UploadState() {
  module1PendingFiles = [];
  if (imgFiles) imgFiles.value = "";
  if (imgFolderFiles) imgFolderFiles.value = "";
  setModule1DropzoneText(MODULE1_DROPZONE_IDLE_TEXT);
  if (imgUploadPreview) imgUploadPreview.innerHTML = "";
  setPill(imgUploadMsg, "", "");
  setPill(imgAnalyzeMsg, "", "");
  if (imgAnalyzeProgressWrap) imgAnalyzeProgressWrap.hidden = true;
  if (imgAnalyzeFailedList) imgAnalyzeFailedList.style.display = "none";
}

function stopModule1AnalyzeProgressPolling() {
  if (module1AnalyzeProgressTimer !== null) {
    window.clearInterval(module1AnalyzeProgressTimer);
    module1AnalyzeProgressTimer = null;
  }
}

function setModule1AnalyzeProgress(current, total, prefix = "大模型分析中") {
  const safeTotal = Math.max(0, Number(total) || 0);
  const safeCurrent = Math.max(0, Math.min(Number(current) || 0, safeTotal || Number(current) || 0));
  const percent = safeTotal > 0 ? Math.round((safeCurrent / safeTotal) * 100) : 0;
  if (imgAnalyzeProgressWrap) imgAnalyzeProgressWrap.hidden = false;
  if (imgAnalyzeProgressBar) imgAnalyzeProgressBar.style.width = `${percent}%`;
  if (imgAnalyzeProgressText) imgAnalyzeProgressText.textContent = `${prefix}（${safeCurrent}/${safeTotal || 0}）`;
}

function startModule1AnalyzeProgressPolling(totalHint = 0) {
  stopModule1AnalyzeProgressPolling();
  setModule1AnalyzeProgress(0, totalHint, "大模型分析中");

  module1AnalyzeProgressTimer = window.setInterval(async () => {
    try {
      const p = await api("/api/module1/analyze-vlm/progress");
      const total = Number(p?.total || totalHint || 0);
      const processed = Number(p?.processed || 0);
      const running = Boolean(p?.running);
      setModule1AnalyzeProgress(processed, total, running ? "大模型分析中" : "大模型分析完成");
      if (!running && total > 0) {
        stopModule1AnalyzeProgressPolling();
      }
    } catch (_) {
      // 忽略短暂轮询失败，避免打断分析流程
    }
  }, MODULE1_ANALYZE_PROGRESS_POLL_MS);
}

async function uploadPendingModule1Files() {
  if (!module1PendingFiles.length) {
    setPill(imgUploadMsg, "请先选择图片", "error");
    return;
  }
  if (module1AnalyzeRunning) return;
  module1AnalyzeRunning = true;
  if (imgUploadBtn) imgUploadBtn.disabled = true;
  let uploadCompleted = false;

  try {
    setPill(imgUploadMsg, `上传中：${module1PendingFiles.length} 张`, "");
    const form = new FormData();
    module1PendingFiles.forEach((file) => form.append("files", file));
    const result = await api("/api/module1/upload-images", {
      method: "POST",
      body: form,
    });
    const uploadedCount = Number(result?.uploaded_count || 0);
    const uploadedNames = Array.isArray(result?.items)
      ? result.items.map((it) => String(it?.image_name || "").trim()).filter(Boolean)
      : [];

    module1PendingFiles = [];
    uploadCompleted = true;
    if (imgFiles) imgFiles.value = "";
    if (imgFolderFiles) imgFolderFiles.value = "";
    renderModule1UploadPreview();
    setModule1DropzoneText(`已上传${uploadedCount}张图片`);
    setPill(imgUploadMsg, `已上传${uploadedCount}张图片`, "ok");

    if (uploadedNames.length > 0) {
      if (imgAnalyzeFailedList) imgAnalyzeFailedList.style.display = "none";
      setPill(imgAnalyzeMsg, "大模型分析中...", "status-running");
      startModule1AnalyzeProgressPolling(uploadedNames.length);

      const analyzeResult = await api("/api/module1/analyze-vlm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_names: uploadedNames }),
      });

      const processed = Number(analyzeResult?.processed || uploadedNames.length);
      const success = Number(analyzeResult?.success || analyzeResult?.success_vlm || 0);
      const failed = Number(analyzeResult?.failed_count || 0);
      setModule1AnalyzeProgress(processed, processed || uploadedNames.length, "大模型分析完成");
      stopModule1AnalyzeProgressPolling();
      if (failed > 0) {
        setPill(imgAnalyzeMsg, `分析完成：成功${success}，失败${failed}`, "error");
      } else {
        setPill(imgAnalyzeMsg, `分析完成：成功${success}张`, "ok");
      }
    } else {
      if (imgAnalyzeProgressWrap) imgAnalyzeProgressWrap.hidden = true;
      setPill(imgAnalyzeMsg, "未检测到可分析图片", "error");
    }

    await loadModule1Vehicles(true);
  } catch (err) {
    stopModule1AnalyzeProgressPolling();
    if (imgAnalyzeProgressWrap) imgAnalyzeProgressWrap.hidden = true;
    if (uploadCompleted) {
      setPill(imgAnalyzeMsg, `大模型分析失败：${err.message || "未知错误"}`, "error");
    } else {
      setPill(imgUploadMsg, `上传失败：${err.message || "未知错误"}`, "error");
      setPill(imgAnalyzeMsg, "", "");
    }
  } finally {
    module1AnalyzeRunning = false;
    if (imgUploadBtn) imgUploadBtn.disabled = false;
  }
}

/* ========== 全局模块切换 ========== */
function switchModule(moduleName) {
  const m1Active = moduleName === "module1";
  const m2Active = moduleName === "module2";
  const m3Active = moduleName === "module3";
  const m4Active = moduleName === "module4";
  const m5Active = moduleName === "module5";

  if (module1Btn) module1Btn.classList.toggle("active", m1Active);
  if (module2Btn) module2Btn.classList.toggle("active", m2Active);
  if (module3Btn) module3Btn.classList.toggle("active", m3Active);
  if (module4Btn) module4Btn.classList.toggle("active", m4Active);
  if (module5Btn) module5Btn.classList.toggle("active", m5Active);

  if (module1Panel) module1Panel.classList.toggle("active", m1Active);
  if (module2Panel) module2Panel.classList.toggle("active", m2Active);
  if (module3Panel) module3Panel.classList.toggle("active", m3Active);
  if (module4Panel) module4Panel.classList.toggle("active", m4Active);
  if (module5Panel) module5Panel.classList.toggle("active", m5Active);

  if (m5Active && typeof window.module5OnPanelActivated === "function") {
    window.module5OnPanelActivated().catch(() => {});
  }

  // 模块1激活逻辑
  if (m1Active) {
    if (!module1LibraryLoaded) loadModule1Vehicles().catch(() => {});
    startModule1LibraryAutoRefresh();
  } else {
    stopModule1LibraryAutoRefresh();
  }
}

function switchModule1View(viewName) {
  const wbActive = viewName === "workbench";
  const textActive = viewName === "text";
  const imageActive = viewName === "image";
  if (module1WorkbenchBtn) module1WorkbenchBtn.classList.toggle("active", wbActive);
  if (module1TextSearchBtn) module1TextSearchBtn.classList.toggle("active", textActive);
  if (module1ImageSearchBtn) module1ImageSearchBtn.classList.toggle("active", imageActive);
  if (module1WorkbenchView) module1WorkbenchView.classList.toggle("active", wbActive);
  if (module1TextSearchView) module1TextSearchView.classList.toggle("active", textActive);
  if (module1ImageSearchView) module1ImageSearchView.classList.toggle("active", imageActive);
}

/* ========== 库定时刷新 ========== */
async function refreshModule1LibrarySilently() {
  if (module1LibraryRefreshInFlight) return;
  module1LibraryRefreshInFlight = true;
  try {
    const data = await api("/api/module1/vehicles");
    module1LibraryItems = normalizeVehicleListData(data);
    module1LibraryLoaded = true;
    refreshCurrentGrids();
  } catch (_) {
  } finally {
    module1LibraryRefreshInFlight = false;
  }
}

function startModule1LibraryAutoRefresh() {
  stopModule1LibraryAutoRefresh();
  module1LibraryAutoRefreshTimer = window.setInterval(refreshModule1LibrarySilently, 2000);
}

function stopModule1LibraryAutoRefresh() {
  if (module1LibraryAutoRefreshTimer !== null) {
    window.clearInterval(module1LibraryAutoRefreshTimer);
    module1LibraryAutoRefreshTimer = null;
  }
}

/* ========== 批量操作核心 ========== */
function updateLibraryBulkControls() {
  const ids = module1LibraryItems.map(r => r.vehicle_id).filter(id => id);
  const allSelected = ids.length > 0 && ids.every(id => module1SelectedVehicleIds.has(id));
  
  const setBtnText = (btn, text, show) => {
    if (!btn) return;
    btn.textContent = text;
    btn.style.display = show ? "inline-block" : "none";
  };

  const modeText = module1BulkDeleteMode ? "取消多选" : "多选";
  setBtnText(imgLibraryBulkModeBtn, modeText, true);
  setBtnText(imgRetrieveAllBulkModeBtn, modeText, true);

  if (imgLibrarySelectAllBtn) {
    imgLibrarySelectAllBtn.style.display = module1BulkDeleteMode ? "inline-block" : "none";
    imgLibrarySelectAllBtn.textContent = allSelected ? "取消全选" : "全选";
  }

  const delText = module1SelectedVehicleIds.size > 0 ? `删除选中(${module1SelectedVehicleIds.size})` : "删除选中";
  setBtnText(imgLibraryBulkDeleteBtn, delText, module1BulkDeleteMode);
  setBtnText(imgRetrieveAllBulkDeleteBtn, delText, module1BulkDeleteMode);
}

function setLibraryBulkMode(enabled) {
  module1BulkDeleteMode = Boolean(enabled);
  if (!module1BulkDeleteMode) module1SelectedVehicleIds.clear();
  updateLibraryBulkControls();
  refreshCurrentGrids();
}

function toggleLibraryVehicleSelection(vehicleId) {
  if (module1SelectedVehicleIds.has(vehicleId)) {
    module1SelectedVehicleIds.delete(vehicleId);
  } else {
    module1SelectedVehicleIds.add(vehicleId);
  }
  updateLibraryBulkControls();
  refreshCurrentGrids();
}

async function deleteSelectedModule1Vehicles() {
  const ids = Array.from(module1SelectedVehicleIds);
  if (!ids.length) return;
  if (!window.confirm(`确认批量删除 ${ids.length} 辆车？`)) return;

  try {
    await api("/api/module1/vehicles/batch-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vehicle_ids: ids })
    });
    module1SelectedVehicleIds.clear();
    setLibraryBulkMode(false);
    await loadModule1Vehicles(true);
    setPill(imgLibraryMsg, "删除成功", "ok");
  } catch (err) {
    setPill(imgLibraryMsg, "删除失败", "error");
  }
}

/* ========== 渲染引擎 ========== */
function normalizeVehicleItem(item) {
  const vid = item?.vehicle_id || null;
  const fallbackUrl = vid ? `/api/module1/image-by-id/${vid}` : "";
  // 核心修复点：优先使用后端 image_url，并使用 withModule1ImageNonce 安全拼接
  const rawUrl = item?.image_url || fallbackUrl;
  return {
    vehicle_id: vid,
    image_name: item?.image_name || `vehicle_${vid || 'unknown'}`,
    image_url: withModule1ImageNonce(rawUrl),
    upload_date: item?.upload_date || item?.updated_at || "",
    updated_at: item?.updated_at || "",
    plate: item?.plate || "无车牌",
    has_plate: typeof item?.has_plate === "boolean" ? item.has_plate : null,
    color: item?.color || "未知",
    brand: item?.brand || "未知",
    type: item?.type || "未知",
    type_info: item?.type_info || "",
    material: item?.material || "",
    sign: item?.sign || "",
    structure: item?.structure || "",
    exception: item?.exception || "",
    other_info: item?.other_info || ""
  };
}

function createVehicleCard(item) {
  const v = normalizeVehicleItem(item);
  const vId = v.vehicle_id;
  const isSelected = module1BulkDeleteMode && vId && module1SelectedVehicleIds.has(vId);

  const figure = document.createElement("figure");
  figure.className = "retrieve-item" + (isSelected ? " retrieve-item-selected" : "");
  figure.tabIndex = 0;

  if (module1BulkDeleteMode && vId) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "retrieve-select-box";
    box.checked = isSelected;
    box.onclick = (e) => {
      e.stopPropagation();
      toggleLibraryVehicleSelection(vId);
    };
    figure.appendChild(box);
  }

  if (v.image_url) {
    const img = document.createElement("img");
    img.src = v.image_url;
    img.loading = "lazy";
    img.onerror = () => { img.src = "/frontend/icons/icon.png"; }; 
    figure.appendChild(img);
  }

  const hint = document.createElement("div");
  hint.className = "retrieve-hint";
  hint.textContent = module1BulkDeleteMode ? "勾选删除" : "详情";
  figure.appendChild(hint);

  figure.onclick = () => {
    if (!module1BulkDeleteMode) openVehicleInfoModal(v);
  };
  return figure;
}

function renderVehicleGrid(container, items, emptyText) {
  container.innerHTML = "";
  container.classList.remove("module1-grid-empty");
  if (!items || !items.length) {
    if (container === imgLibraryGrid) {
      container.classList.add("module1-grid-empty");
      container.innerHTML = `<div class="empty-vehicle-msg">${emptyText}</div>`;
    } else {
      container.innerHTML = `<div class="retrieve-empty">${emptyText}</div>`;
    }
    return;
  }

  const isMainGrid = (container === imgLibraryGrid);
  let displayItems = items;
  let hiddenCount = 0;

  if (isMainGrid) {
    // 主网格固定为 3 行 * 7 列，超出时最后一格显示“更多车辆”
    const maxVisible = MODULE1_LIBRARY_MAX_ROWS * MODULE1_LIBRARY_COLS;
    if (items.length > maxVisible) {
      displayItems = items.slice(0, maxVisible - 1);
      hiddenCount = items.length - displayItems.length;
    }
  }

  displayItems.forEach(item => {
    container.appendChild(createVehicleCard(item));
  });

  if (isMainGrid && hiddenCount > 0) {
    const more = document.createElement("article");
    more.className = "retrieve-item module1-more-card";
    more.innerHTML = `<div class="module1-more-title">更多车辆</div><div class="module1-more-sub">+${hiddenCount} 辆</div>`;
    more.onclick = () => openImageRetrieveAllModal("全部车辆", items);
    container.appendChild(more);
  }
}

function refreshCurrentGrids() {
  if (imgLibraryGrid) renderVehicleGrid(imgLibraryGrid, module1LibraryItems, "暂无车辆信息");
  if (imgRetrieveAllModal && !imgRetrieveAllModal.hidden) {
    renderVehicleGrid(imgRetrieveAllModalBody, module1LibraryItems, "暂无车辆数据");
  }
}

async function loadModule1Vehicles(silent = false) {
  if (!silent) setPill(imgLibraryMsg, "正在加载...", "");
  try {
    const data = await api("/api/module1/vehicles");
    module1LibraryItems = normalizeVehicleListData(data);
    module1LibraryLoaded = true;
    refreshCurrentGrids();
    updateLibraryLoadedPill(module1LibraryItems.length);
  } catch (err) {
    module1LibraryItems = [];
    refreshCurrentGrids();
    setPill(imgLibraryMsg, "已加载0辆车", "error");
  }
}

/* ========== 弹窗交互 ========== */
function openImageRetrieveAllModal(title, items) {
  imgRetrieveAllModalTitle.textContent = title;
  renderVehicleGrid(imgRetrieveAllModalBody, items, "暂无车辆");
  imgRetrieveAllModal.hidden = false;
  updateLibraryBulkControls();
}

function closeImageRetrieveAllModal() {
  imgRetrieveAllModal.hidden = true;
}

function openVehicleInfoModal(v) {
  vehicleInfoTitle.textContent = "详细信息";
  vehicleInfoImage.src = v.image_url;
  vehicleInfoImageMeta.textContent = `入库时间：${String(v.upload_date || "").trim() || "未知"}`;

  const printable = (value, fallback = "未知") => {
    const text = String(value ?? "").trim();
    return text || fallback;
  };
  const rows = [
    ["车辆类型", printable(v.type_info)],
    ["车牌号码", printable(v.plate, "无车牌")],
    ["颜色材质", `${printable(v.color)} / ${printable(v.material)}`],
    ["显著标识", printable(v.sign)],
    ["结构特征", printable(v.structure)],
    ["异常情况", printable(v.exception, "无")],
    ["其他信息", printable(v.other_info, "无")],
  ];
  vehicleInfoBody.innerHTML = rows.map(([label, value]) => `
    <div class="vehicle-info-row">
      <div class="vehicle-info-label">${escapeHtml(label)}</div>
      <div class="vehicle-info-value">${escapeHtml(value)}</div>
    </div>
  `).join("");
  module1CurrentDetailVehicleId = v.vehicle_id;
  vehicleInfoModal.hidden = false;
}

function closeVehicleInfoModal() {
  vehicleInfoModal.hidden = true;
  module1CurrentDetailVehicleId = null;
}

/* ========== 事件监听初始化 ========== */
function initModule1() {
  // 模块菜单监听
  [module1Btn, module2Btn, module3Btn, module4Btn, module5Btn].forEach((btn, i) => {
    if (btn) btn.addEventListener("click", () => switchModule(`module${i + 1}`));
  });

  // 子视图导航
  if (module1WorkbenchBtn) module1WorkbenchBtn.onclick = () => switchModule1View("workbench");
  if (module1TextSearchBtn) module1TextSearchBtn.onclick = () => switchModule1View("text");
  if (module1ImageSearchBtn) module1ImageSearchBtn.onclick = () => switchModule1View("image");

  // 批量操作与刷新
  if (imgLibraryRefreshBtn) imgLibraryRefreshBtn.onclick = () => loadModule1Vehicles();
  if (imgLibraryBulkModeBtn) imgLibraryBulkModeBtn.onclick = () => setLibraryBulkMode(!module1BulkDeleteMode);
  if (imgRetrieveAllBulkModeBtn) imgRetrieveAllBulkModeBtn.onclick = () => setLibraryBulkMode(!module1BulkDeleteMode);
  if (imgLibraryBulkDeleteBtn) imgLibraryBulkDeleteBtn.onclick = deleteSelectedModule1Vehicles;
  if (imgRetrieveAllBulkDeleteBtn) imgRetrieveAllBulkDeleteBtn.onclick = deleteSelectedModule1Vehicles;

  if (imgLibrarySelectAllBtn) {
    imgLibrarySelectAllBtn.onclick = () => {
      const ids = module1LibraryItems.map(r => r.vehicle_id).filter(id => id);
      const allSel = ids.every(id => module1SelectedVehicleIds.has(id));
      if (allSel) module1SelectedVehicleIds.clear();
      else ids.forEach(id => module1SelectedVehicleIds.add(id));
      updateLibraryBulkControls();
      refreshCurrentGrids();
    };
  }

  // 弹窗关闭
  if (imgRetrieveAllModalCloseBtn) imgRetrieveAllModalCloseBtn.onclick = closeImageRetrieveAllModal;
  if (vehicleInfoCloseBtn) vehicleInfoCloseBtn.onclick = closeVehicleInfoModal;

  // 上传区交互
  if (imgDropZone && imgFiles) {
    const openImagePicker = () => imgFiles.click();
    imgDropZone.addEventListener("click", openImagePicker);
    imgDropZone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openImagePicker();
      }
    });
    ["dragenter", "dragover"].forEach((eventName) => {
      imgDropZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        imgDropZone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend", "drop"].forEach((eventName) => {
      imgDropZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        imgDropZone.classList.remove("dragover");
      });
    });
    imgDropZone.addEventListener("drop", (event) => {
      const files = (event.dataTransfer?.files ? Array.from(event.dataTransfer.files) : []);
      if (!files.length) return;
      setModule1PendingFiles(files);
    });
  }

  if (imgFiles) {
    imgFiles.addEventListener("change", () => {
      const files = Array.from(imgFiles.files || []);
      if (!files.length) return;
      setModule1PendingFiles(files);
    });
  }

  if (imgFolderFiles) {
    imgFolderFiles.addEventListener("change", () => {
      const files = Array.from(imgFolderFiles.files || []);
      if (!files.length) return;
      setModule1PendingFiles(files);
    });
  }

  if (imgUploadBtn) imgUploadBtn.onclick = uploadPendingModule1Files;
  if (imgClearStateBtn) imgClearStateBtn.onclick = clearModule1UploadState;

  setModule1DropzoneText(MODULE1_DROPZONE_IDLE_TEXT);
  updateLibraryLoadedPill(0);
  renderModule1UploadPreview();

  // 初始加载
  loadModule1Vehicles().catch(console.error);
}

// 自动启动
initModule1();
