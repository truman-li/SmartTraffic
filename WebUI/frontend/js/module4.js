/**
 * module4.js — 智能 Agent 对话式 UI
 * 依赖：common.js 中的 api()、isImageFile()；marked + DOMPurify
 */
(() => {
  const panel = document.getElementById("module4Panel");
  if (!panel) return;

  /* ── DOM refs ── */
  const welcome       = document.getElementById("agentWelcome");
  const chatFlow      = document.getElementById("agentChatFlow");
  const agentShell    = panel.querySelector(".agent-shell");
  const messagesEl    = document.getElementById("agentMessages");
  const inputArea     = document.getElementById("agentInputArea");
  const textInput     = document.getElementById("agentTextInput");
  const imageInputWrap= document.getElementById("agentImageInput");
  const imageFileEl   = document.getElementById("agentImageFile");
  const imageDropZone = document.getElementById("agentImageDropZone");
  const imagePreview  = document.getElementById("agentImagePreview");
  const sendBtn       = document.getElementById("agentSendBtn");
  const historyBtn    = document.getElementById("agentHistoryBtn");
  const historySidebar= document.getElementById("agentHistorySidebar");
  const historyList   = document.getElementById("agentHistoryList");
  const historyCloseBtn  = document.getElementById("agentHistoryCloseBtn");
  const historyClearBtn  = document.getElementById("agentHistoryClearBtn");
  const trajSelector  = document.getElementById("agentTrajSelector");
  const trajDocSel    = document.getElementById("agentTrajDocSelect");
  const trajPlateSel  = document.getElementById("agentTrajPlateSelect");
  const trajPlateKw   = document.getElementById("agentTrajPlateKeyword");
  const trajFilterBtn = document.getElementById("agentTrajFilterBtn");
  const newChatBtn    = document.getElementById("agentNewChatBtn");
  const allResultModal     = document.getElementById("m4RetrieveAllModal");
  const allResultModalTitle= document.getElementById("m4RetrieveAllModalTitle");
  const allResultModalClose= document.getElementById("m4RetrieveAllModalCloseBtn");
  const allResultModalBody = document.getElementById("m4RetrieveAllModalBody");

  const tags = panel.querySelectorAll(".agent-tag");

  /* ── 状态 ── */
  let currentMode    = "knowledge";
  let imageFile      = null;
  let imagePreviewUrl= "";
  let sessionId      = null;
  let chatMessages   = [];   // 当前对话 [{role, content, ...}]
  let knowledgeCtx   = [];   // 知识检索/轨迹问答 LLM 上下文
  let trajDocId      = "";
  let trajPlateNo    = "";
  let isSending      = false;
  let isReadonly      = false;

  const MODE_LABELS = { knowledge: "知识检索", text: "文本搜车", image: "以图搜图", trajectory: "轨迹问答" };
  const MULTI_TURN  = new Set(["knowledge", "trajectory"]);

  /* ═══════════ 工具函数 ═══════════ */

  function mdToHtml(text) {
    if (window.marked && window.DOMPurify) {
      return window.DOMPurify.sanitize(window.marked.parse(String(text || ""), { breaks: true }));
    }
    const escaped = String(text || "").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return `<p>${escaped}</p>`;
  }

  function scrollToBottom() {
    if (chatFlow) chatFlow.scrollTop = chatFlow.scrollHeight;
  }

  function normalizeItem(item) {
    const rawVid = item?.vehicle_id;
    const vid = Number.isInteger(rawVid) ? rawVid : Number.parseInt(String(rawVid || ""), 10) || null;
    const fallbackUrl = vid !== null ? "/api/module1/image-by-id/" + encodeURIComponent(vid) : "";
    const imageName = String(item?.image_name || "").trim();
    const uploadDate = String(item?.upload_date || item?.updated_at || "").trim();
    return {
      ...item,
      vehicle_id: vid,
      image_url: item?.image_url || fallbackUrl || "",
      image_name: imageName,
      upload_date: uploadDate,
      type: String(item?.type || "").trim(),
      type_info: String(item?.type_info || "").trim(),
      brand: String(item?.brand || "").trim(),
      color: String(item?.color || "").trim(),
      material: String(item?.material || "").trim(),
      sign: String(item?.sign || "").trim(),
      structure: String(item?.structure || "").trim(),
      exception: String(item?.exception || "").trim(),
      plate: String(item?.plate || "").trim(),
      other_info: String(item?.other_info || "").trim(),
    };
  }

  /* ═══════════ 气泡渲染 ═══════════ */

  function createUserBubble(content, imageUrl) {
    const div = document.createElement("div");
    div.className = "agent-bubble agent-bubble-user";
    if (imageUrl) {
      const img = document.createElement("img");
      img.src = imageUrl;
      img.alt = "查询图片";
      div.appendChild(img);
    } else {
      div.textContent = content;
    }
    return div;
  }

  function createAiBubble(html) {
    const div = document.createElement("div");
    div.className = "agent-bubble agent-bubble-ai";
    div.innerHTML = html;
    return div;
  }

  function createThinkingBubble() {
    const div = document.createElement("div");
    div.className = "agent-bubble agent-bubble-ai";
    div.innerHTML = `<div class="agent-thinking">
      <span>思考中</span>
      <span class="agent-thinking-dot"></span>
      <span class="agent-thinking-dot"></span>
      <span class="agent-thinking-dot"></span>
    </div>`;
    return div;
  }

  function createVehicleCard(item) {
    const row = normalizeItem(item);
    const figure = document.createElement("figure");
    figure.className = "retrieve-item";
    const canOpen = Number.isInteger(row.vehicle_id) && row.vehicle_id > 0;
    if (canOpen) {
      figure.tabIndex = 0;
      figure.setAttribute("role", "button");
      figure.setAttribute("aria-label", `查看 vehicle_${row.vehicle_id} 详情`);
      figure.addEventListener("click", () => {
        if (typeof window.openVehicleInfoModal === "function") {
          window.openVehicleInfoModal(row);
        }
      });
    }
    if (row.image_url) {
      const img = document.createElement("img");
      img.src = row.image_url;
      img.alt = row.image_name || "vehicle";
      figure.appendChild(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "retrieve-placeholder";
      ph.textContent = "无图片";
      figure.appendChild(ph);
    }
    const hint = document.createElement("div");
    hint.className = "retrieve-hint";
    hint.textContent = canOpen ? "查看信息" : "命中结果";
    figure.appendChild(hint);
    return figure;
  }

  function createCardGrid(items, maxVisible, allTitle) {
    const grid = document.createElement("div");
    grid.className = "agent-card-grid";
    const visible = items.slice(0, maxVisible);
    visible.forEach(it => grid.appendChild(createVehicleCard(it)));
    if (items.length > maxVisible) {
      const more = document.createElement("div");
      more.className = "agent-card-more";
      more.textContent = `更多结果 (${items.length})`;
      more.addEventListener("click", () => openAllModal(allTitle, items));
      grid.appendChild(more);
    }
    return grid;
  }

  function addBubble(el) {
    messagesEl.appendChild(el);
    requestAnimationFrame(scrollToBottom);
  }

  function replaceLastAiBubble(newEl) {
    const last = messagesEl.lastElementChild;
    if (last && last.classList.contains("agent-bubble-ai")) {
      messagesEl.replaceChild(newEl, last);
    } else {
      messagesEl.appendChild(newEl);
    }
    requestAnimationFrame(scrollToBottom);
  }

  /* ═══════════ 模态弹窗 ═══════════ */

  function openAllModal(title, items) {
    if (!allResultModal) return;
    allResultModalTitle.textContent = title;
    allResultModalBody.innerHTML = "";
    items.forEach(it => allResultModalBody.appendChild(createVehicleCard(it)));
    allResultModal.hidden = false;
  }

  function closeAllModal() {
    if (!allResultModal) return;
    allResultModal.hidden = true;
    allResultModalBody.innerHTML = "";
  }

  if (allResultModalClose) allResultModalClose.addEventListener("click", closeAllModal);
  if (allResultModal) allResultModal.addEventListener("click", e => {
    if (e.target === allResultModal) closeAllModal();
  });

  /* ═══════════ 切换欢迎/对话态 ═══════════ */

  function showWelcome() {
    welcome.hidden = false;
    chatFlow.hidden = true;
  }

  function showChat() {
    welcome.hidden = true;
    chatFlow.hidden = false;
  }

  /* ═══════════ Tag 切换 ═══════════ */

  function switchMode(mode) {
    if (isSending) return;
    currentMode = mode;
    tags.forEach(t => t.classList.toggle("active", t.dataset.mode === mode));

    // 切换时清空对话
    messagesEl.innerHTML = "";
    chatMessages = [];
    knowledgeCtx = [];
    sessionId = null;
    isReadonly = false;
    showWelcome();
    clearImageFile();

    // 输入类型原位切换
    const isImage = mode === "image";
    if (textInput) textInput.hidden = isImage;
    if (imageInputWrap) imageInputWrap.hidden = !isImage;
    // 恢复显示发送按钮：现在所有模式都统一显示右下角的搜索图标
    if (sendBtn) sendBtn.hidden = false; 
    
    // 给 row 增加状态类，用于 CSS 切换边框
    const inputRow = panel.querySelector(".agent-input-row");
    if (inputRow) inputRow.classList.toggle("image-mode", isImage);

    if (textInput) {
      textInput.placeholder = mode === "knowledge" ? "输入你的问题..."
        : mode === "text" ? "描述你要搜索的车辆..."
        : mode === "trajectory" ? "输入你的轨迹分析问题..."
        : "输入你的问题...";
    }

    // 轨迹选择器
    if (trajSelector) trajSelector.hidden = mode !== "trajectory";
    if (mode === "trajectory") loadTrajDocs();
  }

  tags.forEach(t => t.addEventListener("click", () => switchMode(t.dataset.mode)));

  /* ═══════════ 图片上传 ═══════════ */

  function setImageFile(file) {
    const picked = file && isImageFile(file) ? file : null;
    imageFile = picked;
    if (imagePreviewUrl) { try { URL.revokeObjectURL(imagePreviewUrl); } catch(_){} imagePreviewUrl = ""; }
    if (!picked) {
      imagePreview.hidden = true;
      imagePreview.innerHTML = "";
      imageDropZone.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; gap:8px;">
          <span style="font-size:16px; font-weight:700;">点击上传/拖拽上传一张图片</span>
          <span style="font-size:12px; opacity:0.7;">（支持.jpg/.png/.bmp/.webp）</span>
        </div>
      `;
      return;
    }
    imagePreviewUrl = URL.createObjectURL(picked);
    // 按照用户要求：不再显示大的图片预览，仅更新文字状态，显示文件名
    imagePreview.hidden = true; 
    imageDropZone.innerHTML = `
      <div style="display:flex; flex-direction:column; align-items:center;">
        <span style="color:#22c55e; font-weight:700; font-size:15px;">已上传：${picked.name}</span>
      </div>
    `;
  }

  function clearImageFile() {
    setImageFile(null);
    if (imageFileEl) imageFileEl.value = "";
  }

  imageDropZone.addEventListener("click", () => imageFileEl.click());
  imageFileEl.addEventListener("change", () => setImageFile((imageFileEl.files || [])[0] || null));
  ["dragenter", "dragover"].forEach(ev => imageDropZone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); imageDropZone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev => imageDropZone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); imageDropZone.classList.remove("dragover"); }));
  imageDropZone.addEventListener("drop", e => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []).filter(isImageFile);
    setImageFile(files[0] || null);
    imageFileEl.value = "";
  });

  /* ═══════════ 轨迹问答选择器 ═══════════ */

  async function loadTrajDocs() {
    try {
      const data = await api("/api/module5/documents?limit=500");
      const docs = Array.isArray(data?.items) ? data.items : [];
      trajDocSel.innerHTML = "";
      if (!docs.length) {
        trajDocSel.innerHTML = '<option value="">暂无轨迹文档</option>';
        return;
      }
      const latest = String(data?.latest_doc_id || "").trim();
      docs.forEach(d => {
        const opt = document.createElement("option");
        opt.value = String(d?.doc_id || "").trim();
        // 移除 (XXX行) 标记，让显示更简洁
        opt.textContent = (d?.source_file || d?.doc_id || "").trim();
        if (opt.value === latest) opt.selected = true;
        trajDocSel.appendChild(opt);
      });
      trajDocId = trajDocSel.value;
      await loadTrajPlates();
    } catch (err) {
      trajDocSel.innerHTML = '<option value="">加载失败</option>';
    }
  }

  async function loadTrajPlates() {
    if (!trajDocId) { trajPlateSel.innerHTML = '<option value="">请先选择文档</option>'; return; }
    const kw = (trajPlateKw?.value || "").trim();
    const params = new URLSearchParams({ doc_id: trajDocId, limit: "5000" });
    if (kw) params.set("keyword", kw);
    try {
      const data = await api(`/api/module5/plates?${params.toString()}`);
      const items = Array.isArray(data?.items) ? data.items : [];
      trajPlateSel.innerHTML = "";
      const first = document.createElement("option");
      first.value = "";
      first.textContent = items.length ? "请选择车牌" : "暂无车牌";
      trajPlateSel.appendChild(first);
      items.forEach(p => {
        const opt = document.createElement("option");
        opt.value = String(p);
        // 剥离 (XXX行) 类似的后缀，保持界面清爽
        opt.textContent = String(p).replace(/\(\d+行\)$/, "").trim();
        trajPlateSel.appendChild(opt);
      });
      trajPlateNo = "";
    } catch (_) {
      trajPlateSel.innerHTML = '<option value="">加载车牌失败</option>';
    }
  }

  trajDocSel.addEventListener("change", () => { trajDocId = trajDocSel.value; trajPlateNo = ""; loadTrajPlates(); });
  trajPlateSel.addEventListener("change", () => { trajPlateNo = trajPlateSel.value; });
  trajFilterBtn.addEventListener("click", () => loadTrajPlates());

  /* ═══════════ 发送逻辑 ═══════════ */

  sendBtn.addEventListener("click", handleSend);
  textInput.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); handleSend(); }
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); handleSend(); }
  });

  // 自动调整高度
  textInput.addEventListener("input", () => {
    textInput.style.height = "auto";
    textInput.style.height = Math.min(textInput.scrollHeight, 140) + "px";
  });

  async function handleSend() {
    if (isSending || isReadonly) return;
    if (currentMode === "image") {
      if (!imageFile) return;
      await handleImageSearch();
    } else {
      const q = (textInput.value || "").trim();
      if (!q) return;
      textInput.value = "";
      textInput.style.height = "auto";
      if (currentMode === "knowledge") await handleKnowledge(q);
      else if (currentMode === "text") await handleTextSearch(q);
      else if (currentMode === "trajectory") await handleTrajectory(q);
    }
  }

  function lockSend() { isSending = true; sendBtn.disabled = true; }
  function unlockSend() { isSending = false; sendBtn.disabled = false; }

  /* ═══════════ 1. 知识检索 ═══════════ */

  async function handleKnowledge(query) {
    if (!MULTI_TURN.has(currentMode) && messagesEl.children.length > 0) {
      messagesEl.innerHTML = "";
      chatMessages = [];
      knowledgeCtx = [];
    }
    showChat();
    addBubble(createUserBubble(query));
    const thinking = createThinkingBubble();
    addBubble(thinking);
    lockSend();

    try {
      const data = await api("/api/rules/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: query, method: "local", response_type: "Multiple Paragraphs" }),
      });
      const answer = String(data?.response || data?.answer || "暂无回答").trim();
      const bubble = createAiBubble(mdToHtml(answer));
      replaceLastAiBubble(bubble);
      chatMessages.push({ role: "user", text: query }, { role: "ai", text: answer });
      knowledgeCtx.push({ role: "user", content: query }, { role: "assistant", content: answer });
      await saveSession();
    } catch (err) {
      const errBubble = createAiBubble(`<span style="color:var(--error)">查询失败：${err.message}</span>`);
      replaceLastAiBubble(errBubble);
    } finally {
      unlockSend();
    }
  }

  /* ═══════════ 2. 文本检索 ═══════════ */

  async function handleTextSearch(query) {
    // 单轮：每次清空
    messagesEl.innerHTML = "";
    chatMessages = [];
    showChat();
    addBubble(createUserBubble(query));
    const thinking = createThinkingBubble();
    addBubble(thinking);
    lockSend();

    try {
      const data = await api("/api/module1/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, top_k: 12, history: [] }),
      });
      const rows = Array.isArray(data.items) ? data.items : (Array.isArray(data.results) ? data.results : []);
      const answer = String(data.answer || "").trim();

      let aiHtml = "";
      if (rows.length > 0) {
        aiHtml = mdToHtml(answer || `好的，为您找到了 ${rows.length} 辆相关车辆：`);
      } else {
        aiHtml = mdToHtml(answer || "抱歉，没有找到符合条件的车辆。");
      }
      const bubble = createAiBubble(aiHtml);
      if (rows.length > 0) {
        bubble.appendChild(createCardGrid(rows, 6, "文本搜车：所有结果"));
      }
      replaceLastAiBubble(bubble);
      chatMessages.push({ role: "user", text: query }, { role: "ai", text: answer, cards: rows.length });
      sessionId = null; // 单轮不保留 session
      await saveSession();
    } catch (err) {
      replaceLastAiBubble(createAiBubble(`<span style="color:var(--error)">检索失败：${err.message}</span>`));
    } finally {
      unlockSend();
    }
  }

  /* ═══════════ 3. 以图搜图 ═══════════ */

  async function handleImageSearch() {
    if (!imageFile) return;
    // 单轮清空
    messagesEl.innerHTML = "";
    chatMessages = [];
    showChat();

    // 用户气泡（图片缩略图）
    const userUrl = URL.createObjectURL(imageFile);
    addBubble(createUserBubble("", userUrl));

    lockSend();

    // AI: 开始初步筛选
    const step1 = createAiBubble(`<div class="agent-thinking"><span>正在进行初步筛选</span><span class="agent-thinking-dot"></span><span class="agent-thinking-dot"></span><span class="agent-thinking-dot"></span></div>`);
    addBubble(step1);

    try {
      const form = new FormData();
      form.append("file", imageFile);
      const data = await api("/api/module1/search/image?top_k=6", { method: "POST", body: form });

      const coarseRows = Array.isArray(data.coarse_results) ? data.coarse_results : [];
      const fineRows   = Array.isArray(data.results) ? data.results : [];

      // 替换 step1 为粗筛结果
      const coarseBubble = createAiBubble(
        coarseRows.length
          ? `<div>初步筛选完成，找到 <strong>${coarseRows.length}</strong> 个相似车辆：</div>`
          : "<div>初步筛选完成，未找到相似度足够的车辆。</div>"
      );
      if (coarseRows.length > 0) {
        coarseBubble.appendChild(createCardGrid(coarseRows, 5, "粗粒度筛选：所有结果"));
      }
      replaceLastAiBubble(coarseBubble);

      // 延迟模拟第二阶段
      await new Promise(r => setTimeout(r, 1200 + Math.random() * 800));

      // AI: 细化中
      const step2 = createAiBubble(`<div class="agent-thinking"><span>正在进一步细化结果</span><span class="agent-thinking-dot"></span><span class="agent-thinking-dot"></span><span class="agent-thinking-dot"></span></div>`);
      addBubble(step2);
      await new Promise(r => setTimeout(r, 800 + Math.random() * 600));

      // 替换 step2 为细筛结果
      const fineBubble = createAiBubble(
        fineRows.length
          ? `<div>细化完成，以下是最匹配的 <strong>${fineRows.length}</strong> 辆车辆：</div>`
          : "<div>细化完成，未能进一步锁定匹配车辆。</div>"
      );
      if (fineRows.length > 0) {
        fineBubble.appendChild(createCardGrid(fineRows, 6, "细粒度筛选：所有结果"));
      }
      replaceLastAiBubble(fineBubble);

      chatMessages.push({ role: "user", text: "[图片搜索]" }, { role: "ai", text: `粗筛${coarseRows.length}辆, 细筛${fineRows.length}辆` });
      await saveSession();
    } catch (err) {
      replaceLastAiBubble(createAiBubble(`<span style="color:var(--error)">搜索失败：${err.message}</span>`));
    } finally {
      unlockSend();
      try { URL.revokeObjectURL(userUrl); } catch(_){}
    }
  }

  /* ═══════════ 4. 轨迹问答 ═══════════ */

  async function handleTrajectory(query) {
    const docId = trajDocId;
    const plate = trajPlateNo;
    if (!docId) { window.alert("请先选择轨迹文档"); return; }
    if (!plate) { window.alert("请先选择车牌"); return; }

    showChat();
    addBubble(createUserBubble(query));
    const thinking = createThinkingBubble();
    addBubble(thinking);
    lockSend();

    try {
      const data = await api("/api/module5/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc_id: docId, plate_no: plate, question: query, merge_seconds: 0 }),
      });
      const answer = String(data?.answer || "暂无回答").trim();
      const bubble = createAiBubble(mdToHtml(answer));
      replaceLastAiBubble(bubble);
      chatMessages.push({ role: "user", text: query }, { role: "ai", text: answer });
      knowledgeCtx.push({ role: "user", content: query }, { role: "assistant", content: answer });
      await saveSession();
    } catch (err) {
      replaceLastAiBubble(createAiBubble(`<span style="color:var(--error)">分析失败：${err.message}</span>`));
    } finally {
      unlockSend();
    }
  }

  /* ═══════════ 历史记录 ═══════════ */

  async function saveSession() {
    if (isReadonly) return;
    const title = getSessionTitle();
    if (!title) return;
    try {
      if (!sessionId) {
        const res = await api("/api/agent/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: currentMode, title, messages: chatMessages }),
        });
        sessionId = res?.session_id || null;
      } else {
        await api(`/api/agent/sessions/${sessionId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, messages: chatMessages }),
        });
      }
    } catch (_) { /* 静默 */ }
  }

  function getSessionTitle() {
    for (const m of chatMessages) {
      if (m.role === "user" && m.text) {
        const t = String(m.text).trim();
        return t.length > 24 ? t.slice(0, 24) + "..." : t;
      }
    }
    return "";
  }

  async function loadHistory() {
    try {
      const data = await api("/api/agent/sessions?limit=50");
      const items = Array.isArray(data?.items) ? data.items : [];
      renderHistory(items);
    } catch (_) {
      historyList.innerHTML = '<div class="agent-history-empty">加载失败</div>';
    }
  }

  function renderHistory(items) {
    historyList.innerHTML = "";
    if (!items.length) {
      historyList.innerHTML = '<div class="agent-history-empty">暂无历史记录</div>';
      return;
    }
    items.forEach(s => {
      const div = document.createElement("div");
      div.className = "agent-history-item";
      div.innerHTML = `
        <div class="agent-history-item-title">${String(s.title || "未命名").replace(/</g,"&lt;")}</div>
        <div class="agent-history-item-meta">${MODE_LABELS[s.mode] || s.mode} · ${formatTime(s.updated_at)}</div>
      `;
      div.addEventListener("click", () => restoreSession(s.session_id));
      historyList.appendChild(div);
    });
  }

  function formatTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
    } catch(_) { return ""; }
  }

  async function restoreSession(sid) {
    try {
      const data = await api(`/api/agent/sessions/${sid}`);
      if (!data) return;

      currentMode = data.mode || "knowledge";
      tags.forEach(t => t.classList.toggle("active", t.dataset.mode === currentMode));
      const isImage = currentMode === "image";
      textInput.hidden = isImage;
      imageInputWrap.hidden = !isImage;
      trajSelector.hidden = currentMode !== "trajectory";

      chatMessages = Array.isArray(data.messages) ? data.messages : [];
      sessionId = sid;
      isReadonly = true;

      messagesEl.innerHTML = "";
      showChat();
      chatMessages.forEach(m => {
        if (m.role === "user") {
          addBubble(createUserBubble(m.text || ""));
        } else if (m.role === "ai") {
          addBubble(createAiBubble(mdToHtml(m.text || "")));
        }
      });
      toggleSidebar(false);
    } catch(e) {
      window.alert("加载历史会话失败：" + e.message);
    }
  }

  function toggleSidebar(open) {
    if (!historySidebar) return;
    historySidebar.classList.toggle("open", open);
    if (agentShell) agentShell.classList.toggle("sidebar-open", open);
    if (historyBtn) historyBtn.classList.toggle("hidden", open);
    if (open) loadHistory();
  }

  // 历史侧栏按钮
  historyBtn.addEventListener("click", () => toggleSidebar(true));
  historyCloseBtn.addEventListener("click", () => toggleSidebar(false));

  historyClearBtn.addEventListener("click", async () => {
    if (!window.confirm("确定要清空所有历史记录吗？")) return;
    try {
      await api("/api/agent/sessions", { method: "DELETE" });
      renderHistory([]);
    } catch(_) {}
  });

  // 点击新对话时退出只读
  function startNewConversation() {
    isReadonly = false;
    sessionId = null;
    chatMessages = [];
    knowledgeCtx = [];
    messagesEl.innerHTML = "";
    showWelcome();
    // UI 联动：收起侧栏，恢复历史按钮
    toggleSidebar(false);
  }

  if (newChatBtn) newChatBtn.addEventListener("click", startNewConversation);

  // tag切换会自动调 switchMode 里 startNewConversation 的逻辑

  /* ═══════════ 键盘/Escape ═══════════ */
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && allResultModal && !allResultModal.hidden) closeAllModal();
  });

  /* ═══════════ 随机问候语 ═══════════ */
  const GREETINGS = [
    "有什么能帮到你？",
    "今天想聊点什么？",
    "我能为你提供什么帮助？",
    "随时准备为您效劳。",
    "需要我帮你查询什么？",
    "欢迎，今天有什么新发现吗？"
  ];

  function updateRandomGreeting() {
    const titleEl = document.getElementById("agentWelcomeTitle");
    if (!titleEl) return;
    const randomIdx = Math.floor(Math.random() * GREETINGS.length);
    titleEl.textContent = GREETINGS[randomIdx];
  }

  /* ═══════════ 面板激活钩子 ═══════════ */
  window.module4OnPanelActivated = () => {
    // 1. 点击侧栏菜单时默认重置回欢迎页
    startNewConversation();
    // 2. 每次进入该面板都随机换一个问候语
    if (!chatFlow || chatFlow.hidden) {
      updateRandomGreeting();
    }
  };

  /* ═══════════ 初始化 ═══════════ */
  updateRandomGreeting();
  switchMode("knowledge");
})();
