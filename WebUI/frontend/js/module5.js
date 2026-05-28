/**
 * module5.js — Module-5 轨迹重建（文档列表 + 交互有向图 + 高德地图）
 * 依赖：common.js 中的 api()、setPill()，以及 /frontend/lib/cytoscape.min.js
 */

(() => {
  const panel = document.getElementById("module5Panel");
  if (!panel) return;

  const trajFile = document.getElementById("m5TrajFile");
  const trajDropZone = document.getElementById("m5TrajDropZone");
  const trajUploadBtn = document.getElementById("m5TrajUploadBtn");
  const trajResetBtn = document.getElementById("m5TrajResetBtn");

  const docList = document.getElementById("m5DocList");
  const m5DocCountMsg = document.getElementById("m5DocCountMsg");

  const plateKeyword = document.getElementById("m5PlateKeyword");
  const plateSearchBtn = document.getElementById("m5PlateSearchBtn");
  const plateSelect = document.getElementById("m5PlateSelect");

  const graphCy = document.getElementById("m5GraphCy");
  const mapContainer = document.getElementById("m5MapContainer");
  const graphFitBtn = document.getElementById("m5GraphFitBtn");

  const TRAJ_DROP_IDLE_TEXT = `<div style="display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center;">点击上传/拖拽上传<span style="font-size: 12px; color: var(--muted); font-weight: normal;">（支持单文件上传，格式：.xlsx）</span></div>`;
  const M5_DOC_MAX_VISIBLE = 4;
  const M5_AMAP_JS_KEY = "82dcf3c3abe2a4d2448387b2bf92ad88";
  const M5_AMAP_SECURITY_JS_CODE = "5c047343d839002f920fb393b5e16f07";

  let selectedFile = null;
  let selectedDocId = "";
  let selectedPlateNo = "";
  let latestTrajectoryData = null;
  let uploadedDocs = [];
  let cy = null;
  let activePopover = null;

  let amap = null;
  let amapSdkPromise = null;
  let mapGestureProxyBound = false;
  let isMapDragging = false;
  let lastMapDragPointer = null;
  
  let m5DocMultiSelectMode = false;
  let m5DocSelectedIds = new Set();
  
  const m5DocMultiSelectBtn = document.getElementById("m5DocMultiSelectBtn");
  const m5DocSelectAllBtn = document.getElementById("m5DocSelectAllBtn");
  const m5DocDeleteSelectedBtn = document.getElementById("m5DocDeleteSelectedBtn");
  const m5DocRefreshBtn = document.getElementById("m5DocRefreshBtn");

  function ensureAmapSecurityConfig() {
    let securityJsCode = "";
    try {
      securityJsCode = String(localStorage.getItem("m5_amap_security_js_code") || "").trim();
    } catch (_) {
      securityJsCode = "";
    }
    if (!securityJsCode) securityJsCode = String(M5_AMAP_SECURITY_JS_CODE || "").trim();
    if (!securityJsCode) return;

    const prev = (window._AMapSecurityConfig && typeof window._AMapSecurityConfig === "object")
      ? window._AMapSecurityConfig
      : {};
    window._AMapSecurityConfig = {
      ...prev,
      securityJsCode,
    };

    try {
      if (!localStorage.getItem("m5_amap_security_js_code")) {
        localStorage.setItem("m5_amap_security_js_code", securityJsCode);
      }
    } catch (_) {
      // ignore localStorage write failures
    }
  }

  function ensureMapStatusBar() {
    if (!mapContainer) return null;
    let el = mapContainer.querySelector(".m5-map-status");
    if (!el) {
      el = document.createElement("div");
      el.className = "m5-map-status";
      el.hidden = true;
      mapContainer.appendChild(el);
    }
    return el;
  }

  function setMapStatusBar(text, tone = "info") {
    const el = ensureMapStatusBar();
    if (!el) return;
    const msg = String(text || "").trim();
    if (!msg) {
      el.hidden = true;
      el.textContent = "";
      el.removeAttribute("data-tone");
      return;
    }
    el.hidden = false;
    el.textContent = msg;
    el.dataset.tone = tone;
  }

  function buildMapCenterFromPlate(plateNo) {
    const baseLng = 116.397428;
    const baseLat = 39.90923;
    const plate = String(plateNo || "").trim();
    if (!plate) return [baseLng, baseLat];

    let hash = 0;
    for (const ch of plate) hash = ((hash * 131) + ch.charCodeAt(0)) >>> 0;
    const lngOffset = ((hash % 60) - 30) * 0.002;
    const latOffset = (((Math.floor(hash / 60)) % 60) - 30) * 0.0015;
    return [baseLng + lngOffset, baseLat + latOffset];
  }

  function renderCurrentPlaceholder(text) {
    renderGraphPlaceholder(text);
    renderMapBackground("").catch(() => {
      // ignore map background failures in placeholder mode
    });
  }

  function renderMapPlaceholder(text) {
    if (!mapContainer) return;
    if (amap) {
      try { amap.destroy(); } catch (_) {}
    }
    amap = null;
    mapContainer.innerHTML = `<div class="empty-vehicle-msg">${escapeHtml(text || "地图背景加载失败")}</div>`;
  }

  function loadAmapSdk() {
    ensureAmapSecurityConfig();
    if (window.AMap) return Promise.resolve(window.AMap);
    if (amapSdkPromise) return amapSdkPromise;

    amapSdkPromise = new Promise((resolve, reject) => {
      if (!M5_AMAP_JS_KEY) {
        reject(new Error("缺少高德 JS Key。"));
        return;
      }

      const script = document.createElement("script");
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(M5_AMAP_JS_KEY)}&plugin=AMap.Scale,AMap.ToolBar`;
      script.async = true;
      script.defer = true;
      script.dataset.m5AmapSdk = "1";
      script.onload = () => {
        if (window.AMap) resolve(window.AMap);
        else reject(new Error("高德地图 SDK 加载失败。"));
      };
      script.onerror = () => reject(new Error("高德地图脚本加载失败。"));
      document.head.appendChild(script);
    });

    return amapSdkPromise;
  }

  async function ensureAmapPlugins(AMap) {
    if (!AMap) return;

    const needed = [];
    if (typeof AMap.Scale !== "function") needed.push("AMap.Scale");
    if (typeof AMap.ToolBar !== "function") needed.push("AMap.ToolBar");

    if (!needed.length || typeof AMap.plugin !== "function") return;

    await new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        resolve();
      };

      try {
        AMap.plugin(needed, done);
      } catch (_) {
        done();
        return;
      }

      setTimeout(done, 1800);
    });
  }

  async function ensureAmapMap(plateNo = "") {
    const AMap = await loadAmapSdk();
    await ensureAmapPlugins(AMap);
    if (!mapContainer) throw new Error("地图容器不存在。");

    const center = buildMapCenterFromPlate(plateNo);

    if (!amap) {
      mapContainer.innerHTML = "";
      amap = new AMap.Map(mapContainer, {
        zoom: 11,
        center,
        resizeEnable: true,
        mapStyle: "amap://styles/normal",
        dragEnable: true,
        zoomEnable: true,
        doubleClickZoom: true,
        keyboardEnable: true,
        jogEnable: true,
        scrollWheel: true,
        touchZoom: true,
      });
      try {
        amap.addControl(new AMap.Scale());
        amap.addControl(new AMap.ToolBar({ position: "RB" }));
      } catch (_) {
        // ignore optional control errors
      }
    } else {
      try {
        amap.setCenter(center);
      } catch (_) {
        // ignore center update errors
      }
    }

    try {
      amap.setZoom(11);
    } catch (_) {
      // ignore zoom update errors
    }

    return AMap;
  }

  function bindMapInteractionProxy() {
    if (mapGestureProxyBound || !graphCy) return;
    mapGestureProxyBound = true;

    // 在叠层模式下，右键拖拽用于移动地图；Alt+左键拖拽也可移动地图。
    graphCy.addEventListener("contextmenu", (evt) => {
      evt.preventDefault();
    });

    graphCy.addEventListener("wheel", (evt) => {
      // 普通滚轮给结构图；仅 Alt+滚轮转发给地图缩放
      if (!evt.altKey || !amap) return;
      evt.preventDefault();
      evt.stopPropagation();
      if (typeof evt.stopImmediatePropagation === "function") {
        evt.stopImmediatePropagation();
      }

      const currentZoom = Number(amap.getZoom ? amap.getZoom() : 11);
      const delta = evt.deltaY < 0 ? 0.6 : -0.6;
      const nextZoom = Math.max(3, Math.min(20, currentZoom + delta));
      try {
        amap.setZoom(nextZoom);
      } catch (_) {
        // ignore zoom proxy failures
      }
    }, { passive: false, capture: true });

    graphCy.addEventListener("mousedown", (evt) => {
      const canDragMap = evt.button === 2 || (evt.button === 0 && evt.altKey);
      if (!canDragMap || !amap) return;

      isMapDragging = true;
      lastMapDragPointer = { x: evt.clientX, y: evt.clientY };
      evt.preventDefault();
      evt.stopPropagation();
      if (typeof evt.stopImmediatePropagation === "function") {
        evt.stopImmediatePropagation();
      }
    }, { capture: true });

    window.addEventListener("mousemove", (evt) => {
      if (!isMapDragging || !amap || !lastMapDragPointer) return;

      const dx = evt.clientX - lastMapDragPointer.x;
      const dy = evt.clientY - lastMapDragPointer.y;
      lastMapDragPointer = { x: evt.clientX, y: evt.clientY };

      try {
        amap.panBy(-dx, -dy);
      } catch (_) {
        // ignore pan proxy failures
      }
    });

    const endMapDrag = () => {
      isMapDragging = false;
      lastMapDragPointer = null;
    };

    window.addEventListener("mouseup", endMapDrag);
    window.addEventListener("blur", endMapDrag);
  }

  async function renderMapBackground(plateNo = "") {
    setMapStatusBar("");
    try {
      await ensureAmapMap(plateNo);
      try { amap.resize(); } catch (_) {}
    } catch (err) {
      console.warn("[Module5-Map] background load failed:", err);
      renderMapPlaceholder("地图背景加载失败，已使用纯色背景");
    }
  }

  function updateM5DocCountBadge(count) {
    if (!m5DocCountMsg) return;
    const safeCount = Number.isFinite(Number(count)) ? Math.max(0, Number(count)) : 0;
    m5DocCountMsg.textContent = `已加载${safeCount}份文档`;
  }

  function updateM5DocBulkControls() {
    const ids = uploadedDocs.map((d) => String(d.doc_id || "")).filter(Boolean);
    const allSelected = ids.length > 0 && ids.every((id) => m5DocSelectedIds.has(id));

    if (m5DocSelectAllBtn) {
      m5DocSelectAllBtn.style.display = m5DocMultiSelectMode ? "inline-block" : "none";
      m5DocSelectAllBtn.textContent = allSelected ? "取消全选" : "全选";
      m5DocSelectAllBtn.disabled = ids.length === 0;
    }

    if (m5DocDeleteSelectedBtn) {
      m5DocDeleteSelectedBtn.style.display = m5DocMultiSelectMode ? "inline-block" : "none";
      m5DocDeleteSelectedBtn.textContent = m5DocSelectedIds.size > 0
        ? `删除选中项(${m5DocSelectedIds.size})`
        : "删除选中项";
      m5DocDeleteSelectedBtn.disabled = m5DocSelectedIds.size === 0;
    }
  }

  if (m5DocMultiSelectBtn) {
    m5DocMultiSelectBtn.addEventListener("click", () => {
      m5DocMultiSelectMode = !m5DocMultiSelectMode;
      m5DocMultiSelectBtn.textContent = m5DocMultiSelectMode ? "取消多选" : "多选";
      if (!m5DocMultiSelectMode) {
        m5DocSelectedIds.clear();
      }
      updateM5DocBulkControls();
      renderDocList();
    });
  }

  if (m5DocSelectAllBtn) {
    m5DocSelectAllBtn.addEventListener("click", () => {
      const ids = uploadedDocs.map((d) => String(d.doc_id || "")).filter(Boolean);
      const allSelected = ids.length > 0 && ids.every((id) => m5DocSelectedIds.has(id));
      if (allSelected) {
        m5DocSelectedIds.clear();
      } else {
        ids.forEach((id) => m5DocSelectedIds.add(id));
      }
      updateM5DocBulkControls();
      renderDocList();
    });
  }

  // 刷新与删除逻辑统一在后面定义，此处移除错误的重复声明和调用
  if (m5DocDeleteSelectedBtn) {
    m5DocDeleteSelectedBtn.addEventListener("click", async () => {
      if (m5DocSelectedIds.size === 0) return;
      if (!confirm(`确认要删除选中的 ${m5DocSelectedIds.size} 份文档吗？相关的轨迹数据也将被一并删除。`)) return;

      m5DocDeleteSelectedBtn.disabled = true;
      try {
        for (const docId of m5DocSelectedIds) {
          await api(`/api/module5/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
        }
        m5DocMultiSelectMode = false;
        m5DocSelectedIds.clear();
        if (m5DocMultiSelectBtn) m5DocMultiSelectBtn.textContent = "多选";
        updateM5DocBulkControls();
        await loadDocuments();
      } catch (err) {
        alert("删除失败：" + err.message);
      } finally {
        m5DocDeleteSelectedBtn.disabled = false;
      }
    });
  }

  function escapeHtml(text) {
    return String(text || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatDateTime(text) {
    const raw = String(text || "").trim();
    if (!raw) return "-";
    const d = new Date(raw.replace(" ", "T"));
    if (Number.isNaN(d.getTime())) return raw;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${y}-${m}-${day} ${hh}:${mm}`;
  }

  function isXlsxFile(file) {
    return Boolean(file && typeof file.name === "string" && /\.xlsx$/i.test(file.name));
  }

  function setSelectedFile(file) {
    selectedFile = isXlsxFile(file) ? file : null;
    if (trajDropZone) {
      trajDropZone.innerHTML = selectedFile
        ? `已选择：${selectedFile.name}（点击或拖拽可更换）`
        : TRAJ_DROP_IDLE_TEXT;
    }
  }

  function fillPlateOptions(items) {
    if (!plateSelect) return;
    plateSelect.innerHTML = "";
    const list = Array.isArray(items) ? items : [];
    const first = document.createElement("option");
    first.value = "";
    first.textContent = list.length ? "请选择车牌" : "暂无车牌数据";
    plateSelect.appendChild(first);
    for (const plate of list) {
      const opt = document.createElement("option");
      opt.value = String(plate || "");
      opt.textContent = String(plate || "");
      plateSelect.appendChild(opt);
    }
  }

  /* ========== 文档列表（卡片化，与 M3 统一风格） ========== */

  function renderDocList() {
    if (!docList) return;
    docList.innerHTML = "";

    if (!uploadedDocs.length) {
      docList.innerHTML = '<div class="empty-vehicle-msg" style="height:100%;font-size:16px;color:var(--muted);font-weight:600;">暂无已上传文档</div>';
      return;
    }

    const grid = document.createElement("div");
    grid.className = "m5-doc-grid";

    const visible = uploadedDocs.slice(0, M5_DOC_MAX_VISIBLE);
    const hiddenCount = uploadedDocs.length - visible.length;

    for (const doc of visible) {
      const docId = String(doc.doc_id || "");
      const active = docId === selectedDocId;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "m5-doc-card" + (active ? " active" : "");
      btn.innerHTML = `
        <div class="m5-doc-card-icon">表</div>
        <div class="m5-doc-card-text">
          <div class="m5-doc-card-name">${escapeHtml(doc.source_file || docId || "未知文档")}</div>
          <div class="m5-doc-card-meta">${Number(doc.clean_rows || 0)}行 · ${Number(doc.unique_plates || 0)}车牌</div>
        </div>
      `;
      if (m5DocMultiSelectMode) {
        if (m5DocSelectedIds.has(docId)) btn.classList.add("selected");
        const chk = document.createElement("div");
        chk.className = "m3-doc-checkbox"; // We can reuse the same CSS class as M3
        if (m5DocSelectedIds.has(docId)) chk.classList.add("checked");
        btn.appendChild(chk);
      }

      btn.addEventListener("click", async () => {
        if (m5DocMultiSelectMode) {
          if (m5DocSelectedIds.has(docId)) {
            m5DocSelectedIds.delete(docId);
            btn.classList.remove("selected");
            const c = btn.querySelector(".m3-doc-checkbox");
            if (c) c.classList.remove("checked");
          } else {
            m5DocSelectedIds.add(docId);
            btn.classList.add("selected");
            const c = btn.querySelector(".m3-doc-checkbox");
            if (c) c.classList.add("checked");
          }
          updateM5DocBulkControls();
          return;
        }
        await selectDocument(docId, { showStatus: true });
      });
      grid.appendChild(btn);
    }

    if (hiddenCount > 0) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "m5-doc-more-card";
      more.textContent = `更多文档 (+${hiddenCount})`;
      more.addEventListener("click", () => {
        // 展开全部
        renderDocListFull();
      });
      grid.appendChild(more);
    }

    docList.appendChild(grid);
  }

  function renderDocListFull() {
    if (!docList) return;
    docList.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "m5-doc-grid";

    for (const doc of uploadedDocs) {
      const docId = String(doc.doc_id || "");
      const active = docId === selectedDocId;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "m5-doc-card" + (active ? " active" : "");
      btn.innerHTML = `
        <div class="m5-doc-card-icon">表</div>
        <div class="m5-doc-card-text">
          <div class="m5-doc-card-name">${escapeHtml(doc.source_file || docId || "未知文档")}</div>
          <div class="m5-doc-card-meta">${Number(doc.clean_rows || 0)}行 · ${Number(doc.unique_plates || 0)}车牌</div>
        </div>
      `;
      if (m5DocMultiSelectMode) {
        if (m5DocSelectedIds.has(docId)) btn.classList.add("selected");
        const chk = document.createElement("div");
        chk.className = "m3-doc-checkbox";
        if (m5DocSelectedIds.has(docId)) chk.classList.add("checked");
        btn.appendChild(chk);
      }

      btn.addEventListener("click", async () => {
        if (m5DocMultiSelectMode) {
          if (m5DocSelectedIds.has(docId)) {
            m5DocSelectedIds.delete(docId);
            btn.classList.remove("selected");
            const c = btn.querySelector(".m3-doc-checkbox");
            if (c) c.classList.remove("checked");
          } else {
            m5DocSelectedIds.add(docId);
            btn.classList.add("selected");
            const c = btn.querySelector(".m3-doc-checkbox");
            if (c) c.classList.add("checked");
          }
          updateM5DocBulkControls();
          return;
        }
        await selectDocument(docId, { showStatus: true });
      });
      grid.appendChild(btn);
    }
    docList.appendChild(grid);
  }

  /* ========== 图谱 ========== */

  function destroyGraph() {
    removePopover();
    if (cy) { cy.destroy(); cy = null; }
  }

  function renderGraphPlaceholder(text) {
    destroyGraph();
    if (!graphCy) return;
    graphCy.innerHTML = `<div class="empty-vehicle-msg">${escapeHtml(text || "请选择车牌查看轨迹图")}</div>`;
  }

  /* ── 泳道布局：X 轴按 seq 排序，Y 轴按 point_name 分 lane ── */

  function buildSwimLanePositions(nodes) {
    const width = Math.max(600, Number(graphCy?.clientWidth || 0));
    const height = Math.max(320, Number(graphCy?.clientHeight || 0));
    const paddingX = 60;
    const paddingY = 50;
    const usableW = Math.max(200, width - paddingX * 2);
    const usableH = Math.max(100, height - paddingY * 2);

    const sorted = [...nodes].sort((a, b) => Number(a?.seq || 0) - Number(b?.seq || 0));

    // 获取各 point_name 的唯一列表（按首次出现排序）
    const laneMap = new Map();
    let laneIdx = 0;
    for (const n of sorted) {
      const name = String(n.point_name || n.point_code || "unknown");
      if (!laneMap.has(name)) {
        laneMap.set(name, laneIdx++);
      }
    }

    const laneCount = Math.max(1, laneMap.size);
    const laneGap = laneCount > 1 ? usableH / (laneCount - 1) : 0;
    const count = Math.max(1, sorted.length);
    const positions = {};
    const placedPoints = []; // [{x, y}]
    const placedSegments = []; // [{p1: {x, y}, p2: {x, y}}]
    
    const MIN_DIST_PP = 42; // 点与点最小间距 (直径 34 + 8)
    const MIN_DIST_PS = 22; // 点到线最小间距 (半径 17 + 5)
    const MIN_X_STEP = 45;  // 强制水平步长，杜绝交叉

    // 辅助函数：点到线段的最小距离
    function getDistToSegment(px, py, x1, y1, x2, y2) {
      const dx = x2 - x1;
      const dy = y2 - y1;
      const l2 = dx * dx + dy * dy;
      if (l2 === 0) return Math.sqrt((px - x1) ** 2 + (py - y1) ** 2);
      let t = ((px - x1) * dx + (py - y1) * dy) / l2;
      t = Math.max(0, Math.min(1, t));
      return Math.sqrt((px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2);
    }

    let lastX = paddingX - MIN_X_STEP;

    sorted.forEach((node, idx) => {
      const lane = laneMap.get(String(node.point_name || node.point_code || "unknown")) || 0;
      const centerY = paddingY + lane * laneGap;
      const idealX = paddingX + (usableW / count) * idx;

      let bestX = Math.max(lastX + MIN_X_STEP, idealX);
      let bestY = centerY;
      let prevPoint = placedPoints[idx - 1] || null;

      // 尝试查找最优位置
      for (let attempt = 0; attempt < 40; attempt++) {
        const rx = (Math.random() - 0.5) * 120;
        const ry = (Math.random() - 0.5) * 260;
        const staggerY = (idx % 2 === 0 ? 35 : -35);

        const tx = Math.max(lastX + MIN_X_STEP, idealX + rx);
        const ty = centerY + ry + staggerY;

        let ok = true;
        // 1. 点-点避碰
        for (const p of placedPoints) {
          if (Math.sqrt((tx - p.x) ** 2 + (ty - p.y) ** 2) < MIN_DIST_PP) {
            ok = false; break;
          }
        }
        if (!ok) continue;

        // 2. 新点 vs 旧线避碰 (点不要挡住之前的线)
        for (const seg of placedSegments) {
          if (getDistToSegment(tx, ty, seg.p1.x, seg.p1.y, seg.p2.x, seg.p2.y) < MIN_DIST_PS) {
            ok = false; break;
          }
        }
        if (!ok) continue;

        // 3. 新线 vs 旧点避碰 (线不要穿过之前的点)
        if (prevPoint) {
          for (let k = 0; k < idx - 1; k++) {
            const p = placedPoints[k];
            if (getDistToSegment(p.x, p.y, prevPoint.x, prevPoint.y, tx, ty) < MIN_DIST_PS) {
              ok = false; break;
            }
          }
        }

        if (ok) {
          bestX = tx;
          bestY = ty;
          break;
        }
      }

      // 记录位置
      const pos = { x: bestX, y: bestY };
      if (prevPoint) {
        placedSegments.push({ p1: prevPoint, p2: pos });
      }
      placedPoints.push(pos);
      lastX = bestX;
      positions[String(node.id || "")] = pos;
    });
    return positions;
  }

  function renderGraph(graph, plateNo) {
    const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
    const edges = Array.isArray(graph?.edges) ? graph.edges : [];

    if (!nodes.length) {
      renderGraphPlaceholder("该车牌暂无可构建轨迹图的数据");
      return;
    }

    if (typeof window.cytoscape !== "function") {
      renderGraphPlaceholder("未加载 cytoscape，无法渲染交互图谱");
      return;
    }

    if (!graphCy) return;
    graphCy.innerHTML = "";
    destroyGraph();

    const elements = [];
    for (const node of nodes) {
      elements.push({
        data: {
          id: String(node.id || ""),
          seq: Number(node.seq || 0),
          label: String(Number(node.seq || 0)),
          point_name: String(node.point_name || "-"),
          point_code: String(node.point_code || ""),
          pass_time: String(node.pass_time || "-"),
          end_time: String(node.end_time || "-"),
          record_count: Number(node.record_count || 1),
        },
      });
    }

    for (const edge of edges) {
      elements.push({
        data: {
          id: String(edge.id || `${edge.source}->${edge.target}`),
          source: String(edge.source || ""),
          target: String(edge.target || ""),
          gap_minutes: Number(edge.gap_minutes || 0),
        },
      });
    }

    const positions = buildSwimLanePositions(nodes);

    cy = window.cytoscape({
      container: graphCy,
      elements,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#1e3264",
            "border-color": "#dbeafe",
            "border-width": 2,
            "label": "data(label)",
            "color": "#ffffff",
            "font-size": 12,
            "font-weight": 700,
            "text-valign": "center",
            "text-halign": "center",
            width: 34,
            height: 34,
            "transition-property": "width, height, background-color",
            "transition-duration": "0.2s",
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.1,
            "line-color": "#475569",
            "target-arrow-color": "#475569",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.75,
            "curve-style": "bezier",
          },
        },
        {
          selector: "node:selected",
          style: {
            "background-color": "#0f766e",
            width: 44,
            height: 44,
          },
        },
      ],
      layout: {
        name: "preset",
        positions,
        fit: true,
        padding: 40,
        animate: false,
      },
      wheelSensitivity: 0.9,
      minZoom: 0.2,
      maxZoom: 3.0,
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      autoungrabify: false,
      autolock: false,
    });

    /* ── 节点点击：局部放大 + 信息卡片弹窗 ── */
    cy.on("tap", "node", (evt) => {
      const node = evt.target;
      const d = node.data();

      // 动画聚焦
      cy.animate({
        center: { eles: node },
        zoom: Math.min(2.2, cy.zoom() * 1.5),
      }, { duration: 300 });

      // 显示弹窗
      showNodePopover(node, d);
    });

    cy.on("tap", (evt) => {
      if (evt.target === cy) removePopover();
    });

    cy.on("pan zoom", () => {
      if (activePopover) updatePopoverPosition();
    });

    setTimeout(() => {
      if (cy) cy.fit(undefined, 36);
    }, 0);
  }

  /* ── 节点信息弹窗 ── */

  let popoverNodeRef = null;

  function showNodePopover(node, d) {
    removePopover();
    popoverNodeRef = node;

    const pop = document.createElement("div");
    pop.className = "m5-node-popover";
    pop.innerHTML = `
      <div class="m5-node-popover-title">
        <span class="m5-node-seq">${Number(d.seq || 0)}</span>
        ${escapeHtml(d.point_name || "-")}
      </div>
      <div class="m5-node-popover-row"><span class="m5-node-popover-label">点位编码：</span><span class="m5-node-popover-value">${escapeHtml(d.point_code || "-")}</span></div>
      <div class="m5-node-popover-row"><span class="m5-node-popover-label">过车时间：</span><span class="m5-node-popover-value">${escapeHtml(d.pass_time || "-")}</span></div>
    `;

    const wrap = graphCy?.parentElement || graphCy;
    if (wrap) {
      wrap.style.position = "relative";
      wrap.appendChild(pop);
    }
    activePopover = pop;
    updatePopoverPosition();
  }

  function updatePopoverPosition() {
    if (!activePopover || !popoverNodeRef || !cy || !graphCy) return;
    const pos = popoverNodeRef.renderedPosition();
    const rect = graphCy.getBoundingClientRect();
    const wrapRect = (graphCy.parentElement || graphCy).getBoundingClientRect();
    const left = pos.x + rect.left - wrapRect.left + 20;
    const top = pos.y + rect.top - wrapRect.top - 20;
    activePopover.style.left = `${Math.max(0, Math.min(left, wrapRect.width - 220))}px`;
    activePopover.style.top = `${Math.max(0, Math.min(top, wrapRect.height - 120))}px`;
  }

  function removePopover() {
    if (activePopover) {
      activePopover.remove();
      activePopover = null;
    }
    popoverNodeRef = null;
  }

  /* ========== 文档 / 车牌 加载 ========== */

  async function loadDocuments() {
    const data = await api("/api/module5/documents?limit=500");
    uploadedDocs = Array.isArray(data.items) ? data.items : [];
    updateM5DocCountBadge(uploadedDocs.length);
    const docIdSet = new Set(uploadedDocs.map((x) => String(x.doc_id || "")).filter(Boolean));
    m5DocSelectedIds = new Set(Array.from(m5DocSelectedIds).filter((id) => docIdSet.has(id)));
    updateM5DocBulkControls();

    if (!uploadedDocs.length) {
      selectedDocId = "";
      latestTrajectoryData = null;
      renderDocList();
      fillPlateOptions([]);
      renderCurrentPlaceholder("请先上传并解析轨迹文档");
      return;
    }

    const exists = uploadedDocs.some((x) => String(x.doc_id || "") === selectedDocId);
    if (!selectedDocId || !exists) {
      const latest = String(data.latest_doc_id || "").trim();
      selectedDocId = latest || String(uploadedDocs[0].doc_id || "");
    }
    renderDocList();
  }

  async function loadPlates(keyword = "") {
    if (!selectedDocId) {
      fillPlateOptions([]);
      return null;
    }
    const q = String(keyword || "").trim();
    const params = new URLSearchParams();
    params.set("doc_id", selectedDocId);
    params.set("limit", "5000");
    if (q) params.set("keyword", q);
    const data = await api(`/api/module5/plates?${params.toString()}`);
    fillPlateOptions(data.items || []);
    return data;
  }

  async function selectDocument(docId, { showStatus = false } = {}) {
    const nextDocId = String(docId || "").trim();
    if (!nextDocId) return;
    selectedDocId = nextDocId;
    selectedPlateNo = "";
    latestTrajectoryData = null;
    if (plateSelect) plateSelect.value = "";
    renderDocList();
    renderCurrentPlaceholder("请选择车牌查看轨迹图");
    try {
      await loadPlates(plateKeyword?.value || "");
    } catch (err) {
      // silently handle
    }
  }

  async function loadTrajectory(plateNo) {
    const plate = String(plateNo || "").trim();
    selectedPlateNo = plate;

    if (!selectedDocId) {
      renderCurrentPlaceholder("请先选择文档");
      return;
    }
    if (!plate) {
      renderCurrentPlaceholder("请选择车牌查看轨迹图");
      return;
    }

    const params = new URLSearchParams();
    params.set("doc_id", selectedDocId);
    params.set("merge_seconds", "0");

    try {
      const data = await api(`/api/module5/trajectory/${encodeURIComponent(plate)}?${params.toString()}`);
      latestTrajectoryData = data;
      renderGraph(data.graph || {}, data.plate_no || plate);
      await renderMapBackground(data.plate_no || plate);
    } catch (err) {
      latestTrajectoryData = null;
      renderCurrentPlaceholder("轨迹加载失败");
    }
  }

  async function resetModuleState() {
    setSelectedFile(null);
    if (trajFile) trajFile.value = "";
    selectedPlateNo = "";
    latestTrajectoryData = null;
    if (plateKeyword) plateKeyword.value = "";
    if (plateSelect) plateSelect.value = "";
    renderCurrentPlaceholder("请选择车牌查看轨迹图");
    try {
      await loadPlates("");
    } catch (_) {}
  }

  /* ========== 交互绑定 ========== */

  trajDropZone.addEventListener("click", () => trajFile.click());
  trajDropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); trajFile.click(); }
  });

  ["dragenter", "dragover"].forEach((ev) => {
    trajDropZone.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); trajDropZone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach((ev) => {
    trajDropZone.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); trajDropZone.classList.remove("dragover"); });
  });

  trajDropZone.addEventListener("drop", (e) => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []).filter(isXlsxFile);
    if (!files.length) { setSelectedFile(null); trajFile.value = ""; return; }
    if (files.length > 1) window.alert("轨迹重建仅支持 1 个 .xlsx 文件，已自动取第一个。");
    setSelectedFile(files[0]);
    trajFile.value = "";
  });

  trajFile.addEventListener("change", () => {
    setSelectedFile((trajFile.files || [])[0] || null);
  });

  trajUploadBtn.addEventListener("click", async () => {
    if (!selectedFile || !isXlsxFile(selectedFile)) {
      window.alert("请先选择 .xlsx 文件");
      return;
    }
    trajUploadBtn.disabled = true;
    try {
      const form = new FormData();
      form.append("file", selectedFile);
      const data = await api("/api/module5/upload-xlsx", { method: "POST", body: form });
      await loadDocuments();
      if (data?.summary?.doc_id) {
        await selectDocument(String(data.summary.doc_id), { showStatus: false });
      } else if (selectedDocId) {
        await selectDocument(selectedDocId, { showStatus: false });
      }
      setSelectedFile(null);
      if (trajFile) trajFile.value = "";
    } catch (err) {
      window.alert("上传失败: " + err.message);
    } finally {
      trajUploadBtn.disabled = false;
    }
  });

  trajResetBtn.addEventListener("click", async () => {
    await resetModuleState();
  });

  plateSearchBtn.addEventListener("click", async () => {
    try { await loadPlates(plateKeyword?.value || ""); } catch (_) {}
  });

  plateSelect.addEventListener("change", async () => {
    await loadTrajectory(plateSelect.value || "");
  });

  graphFitBtn.addEventListener("click", () => {
    if (cy) cy.fit(undefined, 36);
    if (amap) {
      try {
        amap.resize();
        amap.setCenter(buildMapCenterFromPlate(selectedPlateNo || ""));
        amap.setZoom(11);
      } catch (_) {
        // ignore map view reset errors
      }
    }
  });

  /* ---- 刷新按钮 ---- */
  if (m5DocRefreshBtn) {
    m5DocRefreshBtn.addEventListener("click", async () => {
      m5DocRefreshBtn.disabled = true;
      try { await loadDocuments(); } finally { m5DocRefreshBtn.disabled = false; }
    });
  }

  /* ========== 面板激活钩子 ========== */

  window.module5OnPanelActivated = async () => {
    try {
      await loadDocuments();
      if (selectedDocId) {
        await selectDocument(selectedDocId, { showStatus: false });
      }
      await renderMapBackground(selectedPlateNo || "");
    } catch (_) {
      renderCurrentPlaceholder("加载文档失败");
    }
  };

  /* ========== 初始化 ========== */
  setSelectedFile(null);
  bindMapInteractionProxy();
  renderCurrentPlaceholder("请选择车牌查看轨迹图");
  renderMapBackground("").catch(() => {
    // ignore initial background load failure
  });
})();
