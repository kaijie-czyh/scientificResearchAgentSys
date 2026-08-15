/* 科研 Agent 系统前端逻辑 —— 单页应用，原生 JS 实现 */
(function () {
    "use strict";

    // 全局状态
    // API base URL：部署在静态托管（HF Spaces）时由 index.html 注入 window.SRA_API_BASE
    // 指向独立后端（如 Render）；同源部署/本地时不注入则保持相对路径
    const SRA_API_BASE = (typeof window !== "undefined" && window.SRA_API_BASE) || "";
    const state = {
        currentProjectId: null,
        currentPage: "create",
        currentPage: "dashboard",
        statusCache: null,
        pollTimer: null,
        lastPollAt: 0,
        runMode: "",            // pipeline / discovery（由 /discoveries 与启动动作同步）
        discoveryCache: null,   // 最近一次 /discoveries 结果
        humanFingerprint: null, // 最近一次 pending_human 的指纹（用于跳过无变化的整页重建）
        humanDraft: "",         // 人工输入草稿（轮询重建时恢复）
        autoNavDone: false,     // 本次运行是否已自动导航到论文浏览（避免轮询反复打断）
        pendingPaperId: null,   // 待定位论文（证据溯源跳转：gaps/claims → papers 定位高亮）
        papersCache: null,      // 论文列表缓存（供前端筛选重绘）
        searchPrefs: null,      // 最近一次提交的检索偏好（年份/期刊）
        searchPrefsForm: null,  // 检索范围配置表单引用（提交时收集）
        pendingExperimentId: null, // 待定位实验（Claim 证据 → experiments 定位高亮）
        papersView: "all",      // 论文浏览页视图：all / unlinked（未入库候选）
        // 论文浏览筛选状态
        papersFilter: {
            q: "",          // 关键词（标题/作者/摘要/ID）
            yearMin: "",
            yearMax: "",
            venue: "",      // 期刊/venue 关键词
            sort: "newest", // newest / oldest / relevance / title / if_desc / if_asc
            casZone: "",    // 中科院分区筛选："" / "1" / "2" / "3" / "4"
            ifMin: "",      // 最低影响因子
            pdfOnly: false, // 仅显示可下载 PDF 的论文
        },
        dashboardCache: null,   // /dashboard 聚合数据
        discoveryDetailCache: null,  // /discovery-detail 详细数据
        researchReportCache: null,   // /research-report 调研报告
        materialsCvCache: null,      // /materials-cross-validation
        methodAlignmentCache: null,  // /method-alignment
        pendingPaperId: null,        // 证据跳转目标：点击证据中的 paper_id 后置位，renderPapers 自动展开滚动
        profileMaterialId: null,     // 深度分析页（材料画像/合成路线）当前选中的材料
        profileMaterialName: null,   // 选中材料名（用于标题展示）
    };

    const STAGES = ["research", "ideation", "design", "experiment", "writing"];
    const STAGE_LABELS = {
        research: "调研",
        ideation: "思路探讨",
        design: "方案制定",
        experiment: "实验运行",
        writing: "论文写作",
    };

    // ===== 工具函数 =====

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        if (attrs) {
            for (const k in attrs) {
                if (k === "class") node.className = attrs[k];
                else if (k === "text") node.textContent = attrs[k];
                else if (k === "html") node.innerHTML = attrs[k];
                else if (k.startsWith("data-")) node.setAttribute(k, attrs[k]);
                else if (k === "onclick") node.addEventListener("click", attrs[k]);
                else if (k in node) node[k] = attrs[k];
                else node.setAttribute(k, attrs[k]);
            }
        }
        if (children) {
            if (!Array.isArray(children)) children = [children];
            children.forEach(c => {
                if (c == null) return;
                node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
            });
        }
        return node;
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function truncateText(s, max) {
        if (s == null) return "";
        s = String(s);
        return s.length > max ? s.slice(0, max - 1) + "…" : s;
    }

    function escapeHtml(s) {
        if (s == null) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ===== 骨架屏 =====
    function renderSkeleton(type) {
        const wrap = el("div", { class: "skeleton-wrap" });
        if (type === "card") {
            wrap.appendChild(el("div", { class: "skeleton skeleton-line lg" }));
            wrap.appendChild(el("div", { class: "skeleton skeleton-line" }));
            wrap.appendChild(el("div", { class: "skeleton skeleton-line sm" }));
        } else if (type === "list") {
            for (let i = 0; i < 4; i++) {
                wrap.appendChild(el("div", { class: "skeleton skeleton-block" }));
            }
        } else if (type === "cards") {
            for (let i = 0; i < 3; i++) {
                wrap.appendChild(el("div", { class: "skeleton skeleton-card" }));
            }
        } else {
            wrap.appendChild(el("div", { class: "skeleton skeleton-line lg" }));
            wrap.appendChild(el("div", { class: "skeleton skeleton-line" }));
        }
        return wrap;
    }

    // ===== 空状态 =====
    function renderEmptyState(opts) {
        return el("div", { class: "empty-state" }, [
            el("div", { class: "empty-state-icon", html: opts.icon || "📭" }),
            el("div", { class: "empty-state-title", text: opts.title || "暂无数据" }),
            el("div", { class: "empty-state-desc", text: opts.desc || "" }),
            opts.actions ? el("div", { class: "empty-state-actions" }, opts.actions) : null,
        ].filter(Boolean));
    }

    // ===== 错误状态 =====
    function renderErrorState(opts) {
        return el("div", { class: "error-state" }, [
            el("div", { class: "error-state-icon", text: "⚠" }),
            el("div", { class: "error-state-title", text: opts.title || "出错了" }),
            el("div", { class: "error-state-desc", text: opts.desc || opts.message || "" }),
            opts.actions ? el("div", { class: "error-state-actions" }, opts.actions) : null,
        ].filter(Boolean));
    }

    // ===== 全局 Loading 遮罩 =====
    function showLoadingOverlay(text) {
        const overlay = document.getElementById("loading-overlay");
        const txt = document.getElementById("loading-overlay-text");
        if (overlay) {
            if (text && txt) txt.textContent = text;
            overlay.style.display = "flex";
        }
    }

    function hideLoadingOverlay() {
        const overlay = document.getElementById("loading-overlay");
        if (overlay) overlay.style.display = "none";
    }

    // ===== 进度指示器 =====
    function renderProgressIndicator(text) {
        return el("span", { class: "progress-indicator", "data-tooltip": "系统正在后台运行" }, [
            el("span", { class: "progress-dot" }),
            el("span", { text: text || "运行中" }),
        ]);
    }

    function formatTime(iso) {
        if (!iso) return "—";
        try {
            // 后端存的是 UTC 时间（无时区后缀），前端显示为本地时区
            const d = new Date(iso + "Z"); // 加 Z 标记为 UTC
            if (isNaN(d.getTime())) return iso;
            const pad = n => String(n).padStart(2, "0");
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
        } catch (e) {
            return iso;
        }
    }

    function showToast(msg, kind) {
        const t = document.getElementById("toast");
        t.textContent = msg;
        t.className = "toast show" + (kind ? " " + kind : "");
        clearTimeout(t._timer);
        t._timer = setTimeout(() => {
            t.className = "toast" + (kind ? " " + kind : "");
        }, 2400);
    }

    function downloadFile(artifactType, filename, format) {
        if (!state.currentProjectId) return;
        const fmt = format || "md";
        const url = `/api/projects/${state.currentProjectId}/download/${artifactType}?format=${fmt}`;
        fetch(url)
            .then(resp => {
                if (!resp.ok) throw new Error("下载失败");
                return resp.blob();
            })
            .then(blob => {
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                // 根据格式调整扩展名
                const dotIdx = filename.lastIndexOf(".");
                const baseName = dotIdx > 0 ? filename.substring(0, dotIdx) : filename;
                const ext = fmt === "docx" ? ".docx" : fmt === "pdf" ? ".pdf" : (dotIdx > 0 ? filename.substring(dotIdx) : ".md");
                a.download = baseName + ext;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(a.href);
                showToast("已下载 " + a.download, "success");
            })
            .catch(e => showToast("下载失败: " + e.message, "error"));
    }

    function renderDownloadBar(items) {
        const bar = el("div", { class: "download-bar" }, [
            el("span", { class: "download-bar-label" }, "下载产出："),
        ]);

        // 格式选择器
        const fmtSelect = el("select", { class: "download-format-select" });
        [
            { v: "md", t: "Markdown (.md)" },
            { v: "docx", t: "Word (.docx)" },
            { v: "pdf", t: "PDF (.pdf)" },
        ].forEach(o => {
            const opt = el("option", { value: o.v, text: o.t });
            fmtSelect.appendChild(opt);
        });
        bar.appendChild(fmtSelect);

        items.forEach(it => {
            bar.appendChild(el("button", {
                class: "btn btn-secondary btn-sm",
                text: it.label,
                onclick: () => {
                    const fmt = fmtSelect.value;
                    const ext = fmt === "docx" ? ".docx" : fmt === "pdf" ? ".pdf" : ".md";
                    downloadFile(it.type, it.filename, fmt);
                },
            }));
        });
        return bar;
    }

    // ===== 侧边栏全局下载入口（解决下载仅在 Dashboard 可见的问题） =====

    // 项目 ID 切换/创建后刷新侧边栏下载列表
    function renderSidebarDownload() {
        const wrap = document.getElementById("sidebar-download");
        const list = document.getElementById("sidebar-download-list");
        if (!wrap || !list) return;
        clear(list);
        if (!state.currentProjectId) {
            wrap.style.display = "none";
            return;
        }
        wrap.style.display = "block";
        // 与 Dashboard 一致的 9 类产物
        const items = [
            { type: "full-report", label: "全流程报告", icon: "📋" },
            { type: "research-report", label: "调研报告", icon: "📚" },
            { type: "ideas-summary", label: "思路汇总", icon: "💡" },
            { type: "method-doc", label: "方法文档", icon: "🔬" },
            { type: "experiment-code", label: "实验代码", icon: "💻" },
            { type: "experiment-results", label: "实验结果", icon: "📊" },
            { type: "claims-summary", label: "Claim 汇总", icon: "📝" },
            { type: "discovery-report", label: "发现报告", icon: "🔍" },
            { type: "paper-draft", label: "论文稿", icon: "📄" },
        ];
        items.forEach(it => {
            const btn = el("button", {
                class: "sidebar-download-item",
                "data-tooltip": `下载 ${it.label}`,
                onclick: () => {
                    const fmt = document.getElementById("sidebar-download-fmt")?.value || "md";
                    const filename = it.type + (fmt === "docx" ? ".docx" : fmt === "pdf" ? ".pdf" : ".md");
                    downloadFile(it.type, filename, fmt);
                },
            }, [
                el("span", { class: "sidebar-download-icon", text: it.icon }),
                el("span", { text: it.label }),
            ]);
            list.appendChild(btn);
        });

        // ----- LaTeX 报告（前后端对齐版）-----
        // 4 个按钮：调研报告 PDF / 调研报告 .tex / 构效报告 PDF / 构效报告 .tex
        const latexDivider = el("div", { class: "download-divider" });
        list.appendChild(latexDivider);
        list.appendChild(el("div", {
            class: "sidebar-download-section-title",
            text: "LaTeX 报告（编译器）",
        }));
        const latexItems = [
            { kind: "research", ext: "pdf", label: "文献调研（PDF）", icon: "��" },
            { kind: "research", ext: "tex", label: "文献调研（.tex）", icon: "��" },
            { kind: "discovery", ext: "pdf", label: "构效分析（PDF）", icon: "��" },
            { kind: "discovery", ext: "tex", label: "构效分析（.tex）", icon: "��" },
        ];
        latexItems.forEach(li => {
            const btn = el("button", {
                class: "sidebar-download-item",
                "data-tooltip": `${li.label}（点击先编译再下载）`,
                onclick: () => downloadLatexReport(li.kind, li.ext),
            }, [
                el("span", { class: "sidebar-download-icon", text: li.icon }),
                el("span", { text: li.label }),
            ]);
            list.appendChild(btn);
        });
    }

    async function downloadLatexReport(kind, ext) {
        if (!state.currentProjectId) {
            showToast("请先选择项目", "error");
            return;
        }
        const pid = state.currentProjectId;
        showToast(`正在编译 ${kind} 报告（${ext.toUpperCase()}）...`, "info");
        try {
            const gen = await api("POST", `/api/projects/${pid}/latex-report/${kind}`);
            if (gen.error) {
                showToast(`编译失败：${gen.error}`, "error");
                return;
            }
            // 触发浏览器下载
            const url = `/api/projects/${pid}/download/${kind}-latex-report.${ext}`;
            const resp = await fetch(url);
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            const blob = await resp.blob();
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = `${kind}-latex-report.${ext}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);
            showToast(`已下载 ${kind} 报告 ${ext.toUpperCase()}`, "success");
        } catch (e) {
            showToast(`下载失败：${e.message || e}`, "error");
        }
    }

    async function api(method, path, body) {
        const opts = { method, headers: {} };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        // 静态托管场景：为相对路径补上远程后端地址；绝对路径/已带 base 的不重复拼
        const url = path.startsWith("http") ? path : SRA_API_BASE + path;
        const resp = await fetch(url, opts);
        if (resp.status === 204) return null;
        let data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        if (!resp.ok) {
            const msg = (data && data.detail) || resp.statusText || `HTTP ${resp.status}`;
            throw new Error(msg);
        }
        return data;
    }

    // 节点 ID → 中文描述（进度页「正在执行 / 下一步」展示）
    const NODE_LABELS = {
        // research 阶段
        topic_refine: "主题解析与检索策略制定",
        subquery_decompose: "研究问题分解（拆分子查询）",
        cp_before_confirm: "检查点：准备人工确认",
        topic_confirm: "研究主题确认（等待确认）",
        paper_fetch: "文献抓取（Sciverse/arxiv 多源检索）",
        paper_filter: "论文相关性筛选与打分",
        paper_ingest: "论文入库与 URL 补全",
        material_extraction: "材料知识抽取（材料-性能-合成三元组）",
        cross_validate: "多源信息交叉验证",
        research_gap: "研究缺口识别（Research Gap 清单）",
        // ideation 阶段
        brainstorm: "候选思路生成（与用户探讨）",
        idea_discuss: "思路讨论（等待人工输入）",
        cp_before_validate: "检查点：思路验证前",
        idea_validate: "思路可行性/新颖性验证",
        claim_draft: "核心 Claim 起草",
        // design 阶段
        atom_decompose: "方法原子概念分解",
        method_formalize: "方法形式化（公式/伪代码）",
        cp_before_review: "检查点：方案审查前",
        method_review: "方案人工审查（等待确认）",
        claim_evidence_link: "Claim 证据关联",
        method_artifact: "方法文档产出",
        // experiment 阶段
        experiment_config: "实验配置生成",
        code_generate: "实验代码生成",
        code_review: "代码审查（导师-学生迭代）",
        cp_before_run: "检查点：实验运行前",
        anomaly_check: "实验异常检测",
        claim_verify: "Claim 实验验证",
        experiment_outcome_assess: "实验成败评估",
        // writing 阶段
        style_learn: "写作风格学习",
        outline: "论文大纲生成",
        section_draft: "论文章节撰写",
        review: "论文审稿",
        // discovery 阶段（路线 A）
        hypothesis_seed: "构效关系假设生成",
        search_space: "搜索空间定义",
        llm_guided_search: "LLM 引导搜索",
        discovery_validate: "发现验证（文献交叉+新颖性）",
        discovery_report: "发现报告生成",
        // topic_discovery 阶段
        trend_fetch: "趋势数据获取",
        trend_analysis: "趋势分析（新兴/稳定/饱和）",
        topic_recommend: "推荐研究主题生成",
        topic_select: "主题选择（等待用户选择）",
    };

    function nodeLabel(nodeId) {
        return NODE_LABELS[nodeId] || (nodeId || "未知节点");
    }

    function statusBadge(status) {
        const map = {
            success: "badge-success",
            completed: "badge-success",
            verified: "badge-success",
            done: "badge-success",
            running: "badge-info",
            in_progress: "badge-info",
            pending_human: "badge-warning",
            pending_review: "badge-warning",
            draft: "badge-neutral",
            planned: "badge-neutral",
            not_started: "badge-neutral",
            evidence_linked: "badge-info",
            failed: "badge-danger",
            aborted: "badge-danger",
            refuted: "badge-danger",
            blocked: "badge-danger",
            anomaly_detected: "badge-warning",
            superseded: "badge-neutral",
            experiment_failed: "badge-warning",
            stopped: "badge-neutral",
            created: "badge-neutral",
            rejected: "badge-danger",
            validated: "badge-info",
            adopted: "badge-success",
        };
        const cls = map[status] || "badge-neutral";
        return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
    }

    function statusBanner(status, summary, error, recommendation, advice) {
        let cls = "info";
        if (["completed", "success"].includes(status)) cls = "success";
        else if (["pending_human", "pending_review", "experiment_failed", "anomaly_detected"].includes(status)) cls = "warning";
        else if (["failed", "aborted", "blocked"].includes(status)) cls = "danger";

        const parts = [];
        parts.push(`<span class="status-dot"></span><strong>${escapeHtml(status)}</strong>`);
        if (summary) parts.push(`<span>${escapeHtml(summary)}</span>`);
        if (error) parts.push(`<span class="mono small">错误：${escapeHtml(error)}</span>`);
        if (recommendation) parts.push(`<span class="small">建议：${escapeHtml(recommendation)}</span>`);
        let adviceHtml = "";
        if (Array.isArray(advice) && advice.length) {
            const rows = advice.map(a => {
                const target = a.target ? `<span class="badge badge-info">${escapeHtml(a.target)}</span>` : "";
                return `<div class="advice-row">
                    ${target}
                    <strong>${escapeHtml(a.action || "")}</strong>
                    <div class="small muted">依据：${escapeHtml(a.reason || "")}</div>
                    <div class="small muted">预期：${escapeHtml(a.expected || "")}</div>
                </div>`;
            }).join("");
            adviceHtml = `<div class="advice-block mt-8"><div class="small advice-title">方法改进建议：</div>${rows}</div>`;
        }
        return `<div class="status-banner ${cls}">${parts.join(" · ")}</div>${adviceHtml}`;
    }

    // ===== 导航 =====

    function setActivePage(page) {
        state.currentPage = page;
        document.querySelectorAll(".nav-item").forEach(n => {
            n.classList.toggle("active", n.getAttribute("data-page") === page);
        });
        const titles = {
            create: "新建项目",
            "topic-discovery": "方向推荐",
            progress: "研究进度",
            papers: "论文浏览",
            materials: "材料知识",
            gaps: "研究缺口",
            claims: "Claim 列表",
            experiments: "实验列表",
            discovery: "构效关系发现",
            dashboard: "总览 Dashboard",
            create: "新建项目",
            progress: "研究进度",
            "research-report": "文献调研报告",
            papers: "论文浏览",
            discovery: "构效关系发现概览",
            "discovery-detail": "发现详情与可视化",
            "materials-cv": "Materials Project 交叉验证",
            claims: "Claim 列表",
            experiments: "实验列表",
            "method-alignment": "方法↔代码对齐",
            "material-profile": "材料深度画像",
            "synthesis-routes": "合成路线设计",
            notes: "灵感笔记",
            human: "人工节点交互",
        };
        const title = titles[page] || "科研 Agent 系统";
        const topbarTitle = document.getElementById("topbar-title");
        if (topbarTitle) topbarTitle.textContent = title;
        renderPage();
    }

    // 证据溯源全局跳转：Claim 证据/冲突 → 论文页定位 / 实验页
    window.__sraGoPaper = function (paperId) {
        state.pendingPaperId = paperId;
        setActivePage("papers");
        return false;
    };
    window.__sraGoExperiment = function (expId) {
        state.pendingExperimentId = expId;
        setActivePage("experiments");
        return false;
    };

    // ===== 顶部状态徽章更新 =====
    function updateTopbarStatus(status) {
        const node = document.getElementById("topbar-status");
        const txt = document.getElementById("topbar-status-text");
        if (!node || !txt) return;
        if (!status || status === "idle") {
            node.style.display = "none";
            return;
        }
        node.style.display = "flex";
        node.className = "topbar-status";
        let cls = "", text = "";
        if (status === "running" || status === "in_progress") { cls = "running"; text = "运行中"; }
        else if (status === "completed") { cls = "completed"; text = "已完成"; }
        else if (status === "failed" || status === "blocked") { cls = "failed"; text = "失败"; }
        else if (status === "pending_human" || status === "pending_review") { cls = "pending"; text = "等待人工"; }
        else if (status === "experiment_failed") { cls = "failed"; text = "实验失败"; }
        else { cls = ""; text = status; }
        node.classList.add(cls);
        txt.textContent = text;
    }

    // ===== 快捷键 =====
    function setupKeyboardShortcuts() {
        document.addEventListener("keydown", (e) => {
            // 帮助面板：? 或 F1
            if (e.key === "?" || e.key === "F1") {
                e.preventDefault();
                toggleHelpPanel();
                return;
            }
            // Esc：关闭帮助面板
            if (e.key === "Escape") {
                closeHelpPanel();
                return;
            }
            // 数字键 1-8：导航（仅在非输入元素聚焦时）
            const tag = (e.target.tagName || "").toLowerCase();
            if (["input", "textarea"].includes(tag) || e.target.isContentEditable) return;
            if (e.ctrlKey || e.metaKey || e.altKey) return;
            const map = {
                "1": "dashboard", "2": "create", "3": "progress",
                "4": "research-report", "5": "papers", "6": "discovery",
                "7": "claims", "8": "experiments",
            };
            const target = map[e.key];
            if (target) {
                e.preventDefault();
                setActivePage(target);
            }
        });
    }

    function toggleHelpPanel() {
        const panel = document.getElementById("help-panel");
        if (panel) panel.style.display = panel.style.display === "none" ? "flex" : "none";
    }

    function closeHelpPanel() {
        const panel = document.getElementById("help-panel");
        if (panel) panel.style.display = "none";
    }

    function updateProjectIdDisplay() {
        document.getElementById("current-project-id").textContent =
            state.currentProjectId || "未创建";
    }

    // ===== 轮询 =====

    function startPolling() {
        stopPolling();
        state.pollTimer = setInterval(pollStatus, 2000);
        // 立即跑一次
        pollStatus();
    }

    function stopPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    async function pollStatus() {
        if (!state.currentProjectId) return;
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/status`);
            state.statusCache = data;
            updateBadges(data);
            // 导航条状态实时同步：每次轮询都刷新（独立于页面重渲染）
            renderFlowNav();
            // progress / topic-discovery 页面自动重渲染（数据驱动）
            if (state.currentPage === "progress" || state.currentPage === "topic-discovery") {
                renderPage();
            } else if (state.currentPage === "human") {
                // human 页：仅当 pending_human 指纹变化时才重建，避免输入框被 2s 轮询清空。
                // 注意：勿在 pending_human payload 中加入动态字段（如时间戳），否则指纹每轮变化、方案失效。
                const fp = JSON.stringify(data.pending_human);
                if (fp !== state.humanFingerprint) {
                    state.humanFingerprint = fp;
                    state.humanDraft = ""; // 新请求/消失/切换 → 丢弃上一节点草稿，避免串节点
                    renderPage();
                }
            }
            // 自动导航：开始查找文献（paper_fetch/paper_filter 运行）时，
            // 若用户当前在进度页（自动启动场景），自动切到「论文浏览」实时看抓取结果。
            // 每个项目只自动跳一次（autoNavDone），避免轮询反复打断手动浏览。
            if (data.status === "running" && !state.autoNavDone) {
                const cn = (data.current_node || {}).node_id || "";
                if (["paper_fetch", "paper_filter", "paper_ingest"].includes(cn)
                    && state.currentPage === "progress") {
                    state.autoNavDone = true;
                    setActivePage("papers");
                    return;
                }
            }
            const prevPending = state.statusCache && state.statusCache.pending_human;
            state.statusCache = data;
            updateBadges(data);
            updateTopbarStatus(data.status);
            const curPending = data.pending_human;
            // 人工节点出现时弹出 toast 提醒（仅状态从无→有时）
            if (!prevPending && curPending) {
                showToast("收到人工节点请求，请前往「人工节点」页面处理", "warning");
            }
            // 仅在 pending 状态变化时重渲染 human 页，避免 textarea 被清空
            const pendingChanged = !!prevPending !== !!curPending;
            // 仅在 status / counts / stages 等关键字段变化时刷新 progress 页，避免 2s 整页重建导致抖动
            if (state.currentPage === "progress") {
                const statusChanged = !state.statusCachePrevious
                    || state.statusCachePrevious.status !== data.status
                    || JSON.stringify(state.statusCachePrevious.completed_stages || [])
                       !== JSON.stringify(data.completed_stages || [])
                    || JSON.stringify(state.statusCachePrevious.counts || {})
                       !== JSON.stringify(data.counts || {});
                if (statusChanged) renderPage();
                state.statusCachePrevious = data;
            } else if (state.currentPage === "human" && pendingChanged) {
                renderPage();
            }
            // 轮询 status 时若 run_mode=discovery，同步刷新 discoveries
            if (state.currentPage === "discovery" ||
                (state.runMode === "discovery" && data.status === "running")) {
                fetchDiscoveries();
            }
        } catch (e) {
            // 静默
        }
    }

    function updateBadges(data) {
        const counts = data.counts || {};
        setBadge("badge-papers", counts.papers);
        setBadge("badge-materials", counts.materials);
        setBadge("badge-gaps", counts.gaps);
        setBadge("badge-report-gaps", counts.gaps);
        setBadge("badge-claims", counts.claims);
        setBadge("badge-experiments", counts.experiments);
        setBadge("badge-notes", (data.notes_count != null) ? data.notes_count : null);
        setBadge("badge-human", data.pending_human ? "!" : null);
    }

    function setBadge(id, value) {
        const node = document.getElementById(id);
        if (!node) return;
        // 常亮：数字存在即显示（含 0），null/undefined 才隐藏
        if (value == null) {
            node.style.display = "none";
        } else {
            node.style.display = "inline-block";
            node.textContent = value;
        }
    }

    // ===== 页面渲染入口 =====

    // 流程导航条：按项目真实运行顺序（6 个人工确认节点用 👤 标记）
    // 自动节点合并为阶段产出：论文浏览=抓取+筛选+入库，研究缺口=交叉验证+缺口识别
    const FLOW_STEPS = [
        { label: "方向推荐", page: "topic-discovery", human: false, nodes: ["trend_fetch", "trend_analysis", "topic_recommend"], stage: "topic_discovery" },
        { label: "选择主题", page: "human", human: true, nodes: ["topic_select"], stage: "topic_discovery" },
        { label: "确认检索方向", page: "human", human: true, nodes: ["topic_confirm"], stage: "research" },
        { label: "论文浏览", page: "papers", human: false, nodes: ["paper_fetch", "paper_filter", "paper_ingest"], stage: "research" },
        { label: "材料知识", page: "materials", human: false, nodes: ["material_extraction"], stage: "research" },
        { label: "研究缺口", page: "gaps", human: false, nodes: ["cross_validate", "research_gap"], stage: "research" },
        { label: "思路探讨", page: "human", human: true, nodes: ["brainstorm", "idea_discuss"], stage: "ideation" },
        { label: "方法确认", page: "human", human: true, nodes: ["atom_decompose", "method_formalize", "method_review"], stage: "design" },
        { label: "实验审核", page: "human", human: true, nodes: ["experiment_config", "code_generate", "code_review", "experiment_review"], stage: "experiment" },
        { label: "构效发现", page: "discovery-detail", human: false, nodes: ["hypothesis_seed", "search_space", "llm_guided_search", "discovery_validate", "discovery_report"], stage: "discovery" },
        { label: "终稿确认", page: "human", human: true, nodes: ["provenance_check", "style_learn", "outline", "section_draft", "review", "revise"], stage: "writing" },
    ];
    // 页面 → 步骤索引（用于手动跳页时定位高亮）
    const FLOW_STEP_OF_PAGE = {
        dashboard: 0,
        "topic-discovery": 0,
        create: 0,
        progress: 0,
        "research-report": 5,
        papers: 3,
        materials: 4,
        gaps: 5,
        "material-profile": 4,
        "synthesis-routes": 4,
        discovery: 9,
        "discovery-detail": 9,
        "materials-cv": 9,
        "method-alignment": 9,
        claims: 8,
        experiments: 8,
        notes: 10,
        human: 1,
    };

    // 计算当前活动步骤索引（后端 current_node 命中优先；否则取第一个未完成阶段的首步）
    function currentActiveStepIndex() {
        const st = state.statusCache;
        if (!st) return -1;
        const cn = st.current_node;
        if (cn) {
            const idx = FLOW_STEPS.findIndex(s => s.nodes.includes(cn));
            if (idx >= 0) return idx;
        }
        const ss = st.stage_statuses || {};
        const order = ["research", "ideation", "design", "experiment", "writing"];
        let cur = st.current_stage;
        for (const sg of order) {
            if (ss[sg] !== "done") { cur = sg; break; }
        }
        if (cur) {
            // 同一 stage 可能映射到多个 FLOW_STEPS（如 research：确认方向/论文浏览/材料/缺口）。
            // 遍历该 stage 的所有步骤，跳过已完成（node_history 含全部节点）的步骤，
            // 返回第一个"尚未完成"的步骤——避免永远停在第一个（确认检索方向）。
            const nh = st.node_history || [];
            const doneNodes = new Set(nh.map(n => n.node_id));
            const stageSteps = FLOW_STEPS
                .map((s, i) => ({ s, i }))
                .filter(x => x.s.stage === cur);
            for (const { s, i } of stageSteps) {
                const allDone = s.nodes.every(n => doneNodes.has(n));
                if (!allDone) return i;
            }
            // 该阶段所有步骤都完成（阶段刚收尾）：停在最后一步
            if (stageSteps.length) return stageSteps[stageSteps.length - 1].i;
        }
        return -1;
    }

    // 根据后端状态判断步骤：skipped（跳过）/ done / active / pending
    function flowStepState(i) {
        const st = state.statusCache;
        if (!st) return "pending";
        const ss = st.stage_statuses || {};
        const td = st.topic_discovery || {};
        const nh = st.node_history || [];

        // 0) 是否走了方向推荐：直接启动科研 → 前 2 步跳过
        const usedDiscovery = !!(td.recommendations && td.recommendations.length) || !!td.selected_topic;
        const inRealRun = ["running", "completed", "blocked", "pending_human"].includes(st.status);
        if (inRealRun && !usedDiscovery && (i === 0 || i === 1)) return "skipped";

        // 0.5) 构效发现（discovery 是独立子图，不走标准 lifecycle）：
        //      - 有 discovery 节点完成过 → done
        //      - 当前正在 discovery（run_mode=discovery 或节点命中）→ active
        const s = FLOW_STEPS[i];
        if (s.stage === "discovery") {
            const discNodesDone = nh.some(n => s.nodes.includes(n.node_id));
            const discRunning = st.run_mode === "discovery" ||
                (st.current_node && s.nodes.includes(st.current_node));
            if (discNodesDone || (st.status === "completed" && !discRunning)) return "done";
            if (discRunning) return "active";
            // discovery 未跑过：experiment 已完成则视为待办（active 引导），否则 pending
            if (ss.experiment === "done") return "active";
            return "pending";
        }

        // 1) 推荐已出未选：第 1 步 done，第 2 步 active（等待人工选择）
        if (i === 0 && td.recommendations && td.recommendations.length) return "done";
        if (i === 1 && td.recommendations && td.recommendations.length && !td.selected_topic) return "active";

        // 2) 相对当前活动步骤：之前 done，等于 active，之后 pending
        const activeIdx = currentActiveStepIndex();
        if (activeIdx < 0) return "pending";
        if (i === activeIdx) return "active";
        if (i < activeIdx) return "done";
        return "pending";
    }

    function renderFlowNav() {
        // 独立容器渲染（灵感笔记页隐藏）
        const host = document.getElementById("flow-nav-host");
        if (!host) return null;
        // 保存用户当前的滚动位置（重建 DOM 后恢复，避免轮询把进度条弹回原位）
        const prevScrollLeft = host.querySelector(".flow-steps")?.scrollLeft || 0;
        clear(host);
        if (state.currentPage === "notes") {
            host.style.display = "none";
            return null;
        }
        host.style.display = "";

        const stepsEl = el("div", { class: "flow-steps", id: "flow-steps" });
        FLOW_STEPS.forEach((s, i) => {
            const cls = ["flow-step"];
            const stt = flowStepState(i);
            cls.push(stt);
            if (stt === "done") cls.push("done");
            if (s.human) cls.push("human");
            const numEl = el("span", { class: "flow-step-num" },
                stt === "done" ? "✓" : (stt === "skipped" ? "–" : (s.human ? "👤" : String(i + 1))));
            stepsEl.appendChild(el("button", {
                class: cls.join(" "),
                "data-step": i,
                onclick: () => setActivePage(s.page),
                title: (stt === "skipped" ? "已跳过（未走方向推荐）" :
                        s.human ? "人工确认：需你参与 → " : "") + `前往：${s.label}`,
            }, [
                numEl,
                el("span", { class: "flow-step-label", text: s.label }),
            ]));
        });

        const wrap = el("div", { class: "flow-nav" }, [
            el("span", { class: "flow-nav-title" }, "项目流程"),
            stepsEl,
        ]);
        host.appendChild(wrap);

        // 恢复用户滚动位置（必须在元素插入 DOM 后执行，临时禁用平滑避免缓慢归位动画）
        if (prevScrollLeft > 0) {
            stepsEl.style.scrollBehavior = "auto";
            stepsEl.scrollLeft = prevScrollLeft;
            stepsEl.style.scrollBehavior = "";
        }

        // 当前步骤自动滚动到可见区域（新的进来，旧的往前消失）。
        // 记录上次 active 步骤，只有 active 变化时才滚动（避免 2s 轮询频繁打扰）
        const activeStep = stepsEl.querySelector(".flow-step.active");
        if (activeStep) {
            const curActiveIdx = activeStep.getAttribute("data-step");
            if (String(state.lastFlowActiveIdx) !== String(curActiveIdx)) {
                state.lastFlowActiveIdx = curActiveIdx;
                setTimeout(() => {
                    activeStep.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
                }, 60);
            }
        } else {
            state.lastFlowActiveIdx = null;
        }
        return wrap;
    }

    function renderPage() {
        const content = document.getElementById("content");
        clear(content);

        renderFlowNav();

        // 侧边栏小组件跟随每次页面渲染实时刷新（灵感笔记 + 快速下载），
        // 保证导航切换后仍常驻显示（不在 notes 页时也保持可见）
        renderSidebarNotes();
        renderSidebarDownload();

        if (state.currentPage !== "create" && state.currentPage !== "dashboard" && !state.currentProjectId) {
            content.appendChild(renderNoProject());
            return;
        }

        switch (state.currentPage) {
            case "create": renderCreate(content); break;
            case "topic-discovery": renderTopicDiscovery(content); break;
            case "progress": renderProgress(content); break;
            case "papers": renderPapers(content); break;
            case "materials": renderMaterials(content); break;
            case "gaps": renderGaps(content); break;
            case "claims": renderClaims(content); break;
            case "experiments": renderExperiments(content); break;
            case "discovery": renderDiscovery(content); break;
            case "dashboard": renderDashboard(content); break;
            case "create": renderCreate(content); break;
            case "progress": renderProgress(content); break;
            case "research-report": renderResearchReport(content); break;
            case "papers": renderPapers(content); break;
            case "claims": renderClaims(content); break;
            case "experiments": renderExperiments(content); break;
            case "discovery": renderDiscovery(content); break;
            case "discovery-detail": renderDiscoveryDetail(content); break;
            case "materials-cv": renderMaterialsCv(content); break;
            case "method-alignment": renderMethodAlignment(content); break;
            case "material-profile": renderMaterialProfile(content); break;
            case "synthesis-routes": renderSynthesis(content); break;
            case "notes": renderNotes(content); break;
            case "human": renderHuman(content); break;
        }
    }

    function renderNoProject() {
        return el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "尚未创建或选择项目"),
            el("p", { class: "muted" }, "请先在「新建项目」页面创建一个科研项目。"),
            el("div", { class: "btn-row mt-16" }, [
                el("button", { class: "btn", onclick: () => setActivePage("create") }, "去创建项目"),
            ]),
        ]);
    }

    // ===== 0. Dashboard 总览页（赛题对齐展示）=====

    async function renderDashboard(content) {
        if (!state.currentProjectId) {
            // 无项目时显示系统介绍
            content.appendChild(renderDashboardIntro());
            return;
        }

        // 骨架屏占位
        content.appendChild(renderSkeleton("cards"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/dashboard`);
            state.dashboardCache = data;
            clear(content);

            // 顶部状态横幅
            content.insertAdjacentHTML("beforeend",
                statusBanner(data.status, data.summary, null, data.recommendation, data.advice));

            // 赛题对齐卡片
            content.appendChild(renderCompetitionAlignment(data));

            // 阶段进度条
            content.appendChild(renderStageProgress(data));

            // 研究链路可视化（想法→公式→代码→实验→论文）
            content.appendChild(renderResearchChain(data));

            // 计数卡片（含路线 A 发现数）
            content.appendChild(renderDashboardCounts(data.counts || {}));

            // 调研报告摘要 + 发现摘要 + 交叉验证摘要（三栏）
            content.appendChild(renderDashboardSummaries(data));

            // 数据源合规性卡片（赛题 §5.3 支撑）
            renderDashboardDataSources(content);

            // 系统级指标卡片（赛题 §4.2 阶段性结果 + 效果分析 + 指标可视化）
            renderDashboardSystemMetrics(content);

            // 快速操作
            content.appendChild(renderDashboardActions(data));
        } catch (e) {
            clear(content);
            content.appendChild(renderErrorState({
                title: "Dashboard 加载失败",
                desc: e.message || "请检查网络或项目状态",
                actions: [
                    el("button", {
                        class: "btn",
                        onclick: () => setActivePage("dashboard"),
                    }, "重试"),
                ],
            }));
        }
    }

    function renderDashboardIntro() {
        return el("div", { class: "dashboard-intro" }, [
            el("div", { class: "card hero-card" }, [
                el("div", { class: "hero-eyebrow" }, "GOAI 世界人工智能开源大赛 · 赛道三 · 方向三"),
                el("h1", { class: "hero-title" }, "材料科学文献驱动的科学发现智能体"),
                el("p", { class: "hero-desc" },
                    "本系统是一个覆盖科研全生命周期的多 Agent 系统：从文献调研（基本任务）到构效关系发现（路线 A），" +
                    "通过 LLM 深度参与搜索过程、文献代理模型、Materials Project 交叉验证，" +
                    "产出可溯源、可验证的科学发现。"),
                el("div", { class: "btn-row mt-16" }, [
                    el("button", { class: "btn btn-success", onclick: () => setActivePage("create") }, "启动新项目"),
                ]),
            ]),
            el("div", { class: "counts-grid" }, [
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "基本任务"),
                    el("div", { class: "count-value" }, "✓"),
                    el("div", { class: "count-extra" }, "文献调研 Agent · 三源融合（arxiv + S2 + Sciverse）"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "路线 A"),
                    el("div", { class: "count-value" }, "✓"),
                    el("div", { class: "count-extra" }, "构效关系发现 · MCTS + LLM 深度融合"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "交叉验证"),
                    el("div", { class: "count-value" }, "MP+规则"),
                    el("div", { class: "count-extra" }, "Materials Project API + 物理规则双路验证"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "证据链"),
                    el("div", { class: "count-value" }, "硬关联"),
                    el("div", { class: "count-extra" }, "每条发现可追溯到 paper_id + chunk_id"),
                ]),
            ]),
            el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "核心创新点"),
                el("ul", { class: "feature-list" }, [
                    el("li", {}, "LLM 深度参与搜索过程（评估中间结果科学合理性、引导剪枝、给物理机制），区别于 LLM4Mat/ChemCrow 的「仅生成搜索代码」"),
                    el("li", {}, "文献代理模型：从论文 chunk 抽取 (结构, 性能) 数据点构建加权最近邻插值模型，证据可追溯到 paper_id"),
                    el("li", {}, "新颖性显式评估：novel / partially_known / known 三级，known 的发现置信度低"),
                    el("li", {}, "Materials Project API + 规则双路交叉验证，满足赛题路线 A 公开数据库交叉验证硬要求"),
                    el("li", {}, "溯源链硬校验：writing 阶段强制校验 Claim 已 VERIFIED + Experiment 已 COMPLETED"),
                ]),
            ]),
        ]);
    }

    function renderCompetitionAlignment(data) {
        const counts = data.counts || {};
        const rr = data.research_report_summary || {};
        const ds = data.discovery_summary || {};
        const mc = data.materials_cv_summary || {};

        // 基本任务完成度
        const basicDone = (counts.papers || 0) > 0 && (rr.gaps_count || 0) > 0;
        // 路线 A 完成度
        const routeADone = (ds.relationships || 0) > 0;
        // 交叉验证完成度
        const cvDone = (mc.total_discoveries || 0) > 0;

        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "赛题对齐进度"));
        const grid = el("div", { class: "alignment-grid" });

        // 构建对齐卡片项的辅助函数：结构化展示（标签 + 标题 + 状态徽章 + 关键数字指标）
        const buildItem = (tagLabel, tagClass, title, status, statusLabel, metrics) => {
            const meta = el("div", { class: "alignment-meta" });
            metrics.forEach(m => {
                const row = el("div", { class: "alignment-metric-row" }, [
                    el("span", { class: "alignment-metric-label", text: m.label }),
                    el("span", { class: "alignment-metric-value", text: m.value }),
                ]);
                meta.appendChild(row);
            });
            return el("div", { class: `alignment-item ${status}` }, [
                el("div", { class: "alignment-head" }, [
                    el("span", { class: `alignment-tag ${tagClass || ""}`, text: tagLabel }),
                    el("span", { class: `alignment-status ${status === "done" ? "ok" : "pending"}` },
                        statusLabel),
                ]),
                el("div", { class: "alignment-title" }, title),
                meta,
            ]);
        };

        // 基本任务
        grid.appendChild(buildItem(
            "基本任务", "",
            "文献调研 Agent（arxiv + S2 + Sciverse 三源融合）",
            basicDone ? "done" : "pending",
            basicDone ? "已完成" : "未开始",
            [
                { label: "论文数", value: String(counts.papers || 0) },
                { label: "Research Gaps", value: String(rr.gaps_count || 0) },
                { label: "共识", value: String(rr.consensus_count || 0) },
                { label: "冲突", value: String(rr.conflicts_count || 0) },
                { label: "置信度", value: (rr.overall_confidence || 0).toFixed(2) },
            ],
        ));

        // 路线 A
        grid.appendChild(buildItem(
            "路线 A", "tag-route-a",
            "构效关系发现（MCTS + LLM 深度融合）",
            routeADone ? "done" : "pending",
            routeADone ? "已完成" : "未开始",
            [
                { label: "假设", value: String(ds.hypotheses || 0) },
                { label: "候选", value: String(ds.candidates || 0) },
                { label: "发现关系", value: String(ds.relationships || 0) },
                { label: "Novel", value: String(ds.novel || 0) },
            ],
        ));

        // 交叉验证
        grid.appendChild(buildItem(
            "交叉验证", "tag-cv",
            "Materials Project + 规则双路验证",
            cvDone ? "done" : "pending",
            cvDone ? "已完成" : "未开始",
            [
                { label: "验证条数", value: String(mc.total_discoveries || 0) },
                { label: "MP 命中", value: String(mc.mp_validated || 0) },
                { label: "规则通过", value: String(mc.rule_validated || 0) },
                { label: "置信度", value: (mc.overall_confidence || 0).toFixed(2) },
                { label: "来源", value: mc.source || "—" },
            ],
        ));

        card.appendChild(grid);
        return card;
    }

    function renderResearchChain(data) {
        const counts = data.counts || {};
        const stages = data.stage_statuses || {};
        const currentStage = data.current_stage || "";

        // 研究链路各节点
        const chainSteps = [
            {
                id: "research",
                icon: "01",
                name: "文献调研",
                desc: "检索·筛选·知识抽取",
                page: "research-report",
                outputs: [
                    { label: "论文", value: counts.papers || 0 },
                    { label: "Gaps", value: (data.research_report_summary?.gaps_count) || 0 },
                ],
                status: stages.research || "",
            },
            {
                id: "ideation",
                icon: "02",
                name: "思路探讨",
                desc: "Idea 生成·筛选",
                page: "notes",
                outputs: [
                    { label: "思路", value: counts.ideas || 0 },
                ],
                status: stages.ideation || "",
            },
            {
                id: "design",
                icon: "03",
                name: "方法设计",
                desc: "公式形式化·代码映射",
                page: "method-alignment",
                outputs: [
                    { label: "Claim", value: counts.claims || 0 },
                    { label: "公式", value: (data.method_alignment_summary?.total_formulas) || 0 },
                ],
                status: stages.design || "",
            },
            {
                id: "experiment",
                icon: "04",
                name: "实验运行",
                desc: "代码生成·审查·执行",
                page: "experiments",
                outputs: [
                    { label: "实验", value: counts.experiments || 0 },
                ],
                status: stages.experiment || "",
            },
            {
                id: "writing",
                icon: "05",
                name: "论文写作",
                desc: "大纲·初稿·审稿·修订",
                page: "progress",
                outputs: [
                    { label: "产出", value: (data.writing_summary?.artifact_count) || 0 },
                ],
                status: stages.writing || "",
            },
        ];

        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "研究链路 · Idea → 公式 → 代码 → 实验 → 论文"),
            el("p", { class: "muted small mb-12" },
                "完整的科研工作流可视化，点击各节点跳转详情页。代码逻辑关系可在「方法↔代码对齐」页查看。"),
        ]);

        const chainEl = el("div", { class: "research-chain" });
        chainSteps.forEach((step, idx) => {
            const isCurrent = currentStage === step.id;
            const isDone = step.status === "completed" || step.status === "success";
            const isRunning = step.status === "running" || isCurrent;
            const isFailed = step.status === "failed";

            const statusIcon = isDone ? "✓" : isRunning ? "●" : isFailed ? "✗" : "○";
            const statusClass = isDone ? "done" : isRunning ? "running" : isFailed ? "failed" : "pending";

            const stepEl = el("div", {
                class: `chain-step ${statusClass} ${isCurrent ? "current" : ""}`,
                onclick: () => setActivePage(step.page),
            });

            stepEl.appendChild(el("div", { class: "chain-step-header" }, [
                el("div", { class: `chain-step-icon ${statusClass}`, text: step.icon }),
                el("div", { class: "chain-step-status", text: statusIcon }),
            ]));

            stepEl.appendChild(el("div", { class: "chain-step-name", text: step.name }));
            stepEl.appendChild(el("div", { class: "chain-step-desc small muted", text: step.desc }));

            // 产出计数
            const outputsEl = el("div", { class: "chain-step-outputs" });
            step.outputs.forEach(o => {
                outputsEl.appendChild(el("div", { class: "chain-output-item" }, [
                    el("span", { class: "chain-output-value", text: String(o.value) }),
                    el("span", { class: "chain-output-label", text: o.label }),
                ]));
            });
            stepEl.appendChild(outputsEl);

            // 状态标签
            stepEl.appendChild(el("div", { class: `chain-step-badge badge-${statusClass}` },
                isDone ? "已完成" : isRunning ? "进行中" : isFailed ? "失败" : "未开始"));

            chainEl.appendChild(stepEl);

            // 箭头连接（除最后一个）
            if (idx < chainSteps.length - 1) {
                chainEl.appendChild(el("div", { class: `chain-arrow ${statusClass}` }, "→"));
            }
        });

        card.appendChild(chainEl);
        return card;
    }

    function renderDashboardCounts(counts) {
        const wrap = el("div", { class: "counts-grid" });
        const items = [
            { label: "论文", value: counts.papers, extra: "research 阶段产出", highlight: true },
            { label: "思路", value: counts.ideas, extra: "ideation 阶段产出" },
            { label: "Claim", value: counts.claims, extra: "design + discovery 阶段" },
            { label: "实验", value: counts.experiments, extra: "experiment 阶段产出" },
            { label: "发现", value: counts.discovery_claims, extra: "路线 A 构效关系发现", highlight: true },
        ];
        items.forEach(it => {
            wrap.appendChild(el("div", { class: `count-card ${it.highlight ? "highlight" : ""}` }, [
                el("div", { class: "count-label", text: it.label }),
                el("div", { class: "count-value", text: String(it.value != null ? it.value : 0) }),
                el("div", { class: "count-extra", text: it.extra }),
            ]));
        });
        return wrap;
    }

    function renderDashboardSummaries(data) {
        const wrap = el("div", { class: "summary-grid" });

        // 调研报告摘要
        const rr = data.research_report_summary || {};
        wrap.appendChild(el("div", { class: "card summary-card" }, [
            el("div", { class: "card-title" }, "文献调研报告"),
            el("div", { class: "summary-stats" }, [
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(rr.gaps_count || 0) }),
                    el("div", { class: "summary-stat-label" }, "Research Gaps"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(rr.consensus_count || 0) }),
                    el("div", { class: "summary-stat-label" }, "共识"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(rr.conflicts_count || 0) }),
                    el("div", { class: "summary-stat-label" }, "冲突"),
                ]),
            ]),
            el("div", { class: "mt-8" },
                `整体置信度：${(rr.overall_confidence || 0).toFixed(2)}`),
            el("div", { class: "btn-row mt-8" }, [
                el("button", {
                    class: "btn btn-secondary btn-sm",
                    onclick: () => setActivePage("research-report"),
                }, "查看完整报告 →"),
            ]),
        ]));

        // 发现摘要
        const ds = data.discovery_summary || {};
        wrap.appendChild(el("div", { class: "card summary-card" }, [
            el("div", { class: "card-title" }, "构效关系发现"),
            el("div", { class: "summary-stats" }, [
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(ds.hypotheses || 0) }),
                    el("div", { class: "summary-stat-label" }, "假设"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(ds.relationships || 0) }),
                    el("div", { class: "summary-stat-label" }, "发现"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value novel", text: String(ds.novel || 0) }),
                    el("div", { class: "summary-stat-label" }, "Novel"),
                ]),
            ]),
            el("div", { class: "btn-row mt-8" }, [
                el("button", {
                    class: "btn btn-secondary btn-sm",
                    onclick: () => setActivePage("discovery-detail"),
                }, "查看可视化 →"),
            ]),
        ]));

        // 交叉验证摘要
        const mc = data.materials_cv_summary || {};
        wrap.appendChild(el("div", { class: "card summary-card" }, [
            el("div", { class: "card-title" }, "Materials 交叉验证"),
            el("div", { class: "summary-stats" }, [
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(mc.mp_validated || 0) }),
                    el("div", { class: "summary-stat-label" }, "MP 命中"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: String(mc.rule_validated || 0) }),
                    el("div", { class: "summary-stat-label" }, "规则通过"),
                ]),
                el("div", { class: "summary-stat" }, [
                    el("div", { class: "summary-stat-value", text: (mc.overall_confidence || 0).toFixed(2) }),
                    el("div", { class: "summary-stat-label" }, "置信度"),
                ]),
            ]),
            el("div", { class: "mt-8 small muted" },
                `来源：${mc.source === "mp" ? "Materials Project API + 规则" : "规则交叉验证（未配置 MP API key）"}`),
            el("div", { class: "btn-row mt-8" }, [
                el("button", {
                    class: "btn btn-secondary btn-sm",
                    onclick: () => setActivePage("materials-cv"),
                }, "查看报告 →"),
            ]),
        ]));

        return wrap;
    }

    // ===== 数据源合规性卡片（赛题 §5.3 支撑） =====
    function renderDashboardDataSources(container) {
        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, [
                "数据源合规性（赛题 §5.3）",
            ]),
            el("div", { class: "card-sub muted" }, [
                "系统使用的外部数据源 / API 与许可证，集中登记见 ",
                el("a", {
                    href: "/api/data-sources",
                    target: "_blank",
                    class: "link",
                }, "/api/data-sources"),
                " 与源码 ",
                el("code", {}, "core/tools/data_provenance.py"),
                "。",
            ]),
            el("div", { id: "data-sources-summary" }, [el("div", { class: "muted small" }, "加载中...")]),
            el("div", { id: "data-sources-list" }, []),
        ]);
        container.appendChild(card);
        // 异步拉取 + 渲染（失败不阻塞 dashboard）
        fetch(SRA_API_BASE + "/api/data-sources")
            .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
            .then(payload => {
                const list = (payload.sources || []);
                const sum = payload.summary || {};
                // 顶部统计
                const summary = document.getElementById("data-sources-summary");
                if (summary) {
                    clear(summary);
                    summary.appendChild(el("div", { class: "summary-stats" }, [
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(sum.total || 0) }),
                            el("div", { class: "summary-stat-label" }, "数据源"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(sum.required_count || 0) }),
                            el("div", { class: "summary-stat-label" }, "必需"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(sum.token_required_count || 0) }),
                            el("div", { class: "summary-stat-label" }, "需 Token"),
                        ]),
                    ]));
                }
                // 列表
                const listEl = document.getElementById("data-sources-list");
                if (!listEl) return;
                clear(listEl);
                const table = el("table", { class: "data-sources-table" }, [
                    el("thead", {}, el("tr", {}, [
                        el("th", {}, "名称"),
                        el("th", {}, "类别"),
                        el("th", {}, "许可证"),
                        el("th", {}, "接入"),
                        el("th", {}, "必选"),
                    ])),
                ]);
                const tbody = el("tbody");
                list.forEach(src => {
                    tbody.appendChild(el("tr", {}, [
                        el("td", {}, [
                            el("strong", {}, src.name),
                            el("div", { class: "small muted" }, src.usage || ""),
                        ]),
                        el("td", {}, src.category || ""),
                        el("td", {}, src.license || ""),
                        el("td", {}, src.access || ""),
                        el("td", {}, src.required ? "必选" : "可选"),
                    ]));
                });
                table.appendChild(tbody);
                listEl.appendChild(table);
            })
            .catch(e => {
                const summary = document.getElementById("data-sources-summary");
                if (summary) {
                    clear(summary);
                    summary.appendChild(el("div", { class: "error small" },
                        "数据源列表加载失败：" + (e.message || e)));
                }
            });
    }

    // ===== 系统级指标卡片（赛题 §4.2 阶段性结果 + 效果分析 + 指标可视化） =====
    function renderDashboardSystemMetrics(container) {
        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, [
                "系统级指标（赛题 §4.2）",
                el("span", { class: "card-sub muted small" }, "9 类指标聚合 / 跨项目 / 可下载"),
            ]),
            el("div", { class: "card-sub muted" }, [
                "数据来自 ",
                el("a", { href: "/api/metrics/system", target: "_blank", class: "link" }, "/api/metrics/system"),
                "；Markdown 导出见 ",
                el("a", { href: "/api/metrics/system/markdown", target: "_blank", class: "link" }, "/api/metrics/system/markdown"),
                "。Golden Set 8 个固定查询回归测试见 ",
                el("code", {}, "tests/test_golden_set.py"),
                "。",
            ]),
            el("div", { id: "sys-metrics-summary" }, [el("div", { class: "muted small" }, "加载中...")]),
            el("div", { id: "sys-metrics-detail" }, []),
        ]);
        container.appendChild(card);

        fetch("/api/metrics/system")
            .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
            .then(payload => {
                const sumEl = document.getElementById("sys-metrics-summary");
                const detailEl = document.getElementById("sys-metrics-detail");
                if (sumEl) {
                    clear(sumEl);
                    sumEl.appendChild(el("div", { class: "summary-stats" }, [
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.project_count || 0) }),
                            el("div", { class: "summary-stat-label" }, "项目总数"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.completed_count || 0) }),
                            el("div", { class: "summary-stat-label" }, "已完成"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.failed_count || 0) }),
                            el("div", { class: "summary-stat-label" }, "失败"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.total_papers || 0) }),
                            el("div", { class: "summary-stat-label" }, "论文"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.total_ideas || 0) }),
                            el("div", { class: "summary-stat-label" }, "Ideas"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.total_claims || 0) }),
                            el("div", { class: "summary-stat-label" }, "Claims"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.total_gaps || 0) }),
                            el("div", { class: "summary-stat-label" }, "Gaps"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value", text: String(payload.total_evidence_entries || 0) }),
                            el("div", { class: "summary-stat-label" }, "证据条目"),
                        ]),
                        el("div", { class: "summary-stat" }, [
                            el("div", { class: "summary-stat-value small", text:
                                (payload.efficiency && payload.efficiency.avg_duration_seconds != null)
                                    ? payload.efficiency.avg_duration_seconds.toFixed(1) + "s"
                                    : "—" }),
                            el("div", { class: "summary-stat-label" }, "平均耗时"),
                        ]),
                    ]));
                }
                if (!detailEl) return;
                clear(detailEl);

                // 9 类指标概览
                const sections = [];

                // 1. 节点完成率（Top 5 + Bottom 5）
                if (payload.node_completion && Object.keys(payload.node_completion).length) {
                    const entries = Object.entries(payload.node_completion).sort((a, b) => b[1] - a[1]);
                    const top = entries.slice(0, 5);
                    const bottom = entries.slice(-5).reverse();
                    sections.push(el("div", { class: "sys-metric-section" }, [
                        el("div", { class: "sys-metric-title" }, "① 节点完成率"),
                        el("div", { class: "small muted" }, `共 ${entries.length} 个节点参与统计`),
                        renderBarGroup("Top 5", top),
                        renderBarGroup("Bottom 5", bottom),
                    ]));
                }

                // 4. 5 维度评分分布
                if (payload.reliability_dims && Object.keys(payload.reliability_dims).length) {
                    const dimEntries = Object.entries(payload.reliability_dims);
                    sections.push(el("div", { class: "sys-metric-section" }, [
                        el("div", { class: "sys-metric-title" }, "④ 5 维度可信度评分（中位数）"),
                        renderBarGroup(
                            "5 维度",
                            dimEntries.map(([k, v]) => [k, v.median || 0]),
                        ),
                    ]));
                }

                // 7. 证据链（按阶段）
                if (payload.evidence_chain && Object.keys(payload.evidence_chain).length) {
                    const ev = Object.entries(payload.evidence_chain).sort((a, b) => b[1] - a[1]);
                    sections.push(el("div", { class: "sys-metric-section" }, [
                        el("div", { class: "sys-metric-title" }, "⑦ 证据链（按阶段落库条目数）"),
                        renderBarGroup("阶段", ev),
                    ]));
                }

                // 9. 效率
                if (payload.efficiency && Object.keys(payload.efficiency).length) {
                    const ef = Object.entries(payload.efficiency).map(([k, v]) => [k, v]);
                    sections.push(el("div", { class: "sys-metric-section" }, [
                        el("div", { class: "sys-metric-title" }, "⑨ 流水线效率"),
                        el("ul", { class: "small" },
                            ef.map(([k, v]) =>
                                el("li", {}, k + ": " + (typeof v === "number" ? v.toFixed(1) : v))
                            )
                        ),
                    ]));
                }

                sections.forEach(s => detailEl.appendChild(s));
            })
            .catch(e => {
                const sumEl = document.getElementById("sys-metrics-summary");
                if (sumEl) {
                    clear(sumEl);
                    sumEl.appendChild(el("div", { class: "error small" },
                        "系统指标加载失败：" + (e.message || e)));
                }
            });
    }

    function renderBarGroup(title, entries) {
        const wrap = el("div", { class: "sys-bar-group" });
        entries.forEach(([label, value]) => {
            const row = el("div", { class: "sys-bar-row" });
            const v = Math.max(0, Math.min(1, Number(value) || 0));
            row.appendChild(el("div", { class: "sys-bar-label small" }, label));
            row.appendChild(el("div", { class: "sys-bar-track" }, [
                el("div", {
                    class: "sys-bar-fill",
                    style: `width: ${(v * 100).toFixed(1)}%`,
                }),
            ]));
            row.appendChild(el("div", { class: "sys-bar-value small" }, (v * 100).toFixed(1) + "%"));
            wrap.appendChild(row);
        });
        return wrap;
    }

    function renderDashboardActions(data) {
        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "快速操作"),
            el("div", { class: "btn-row" }, [
                el("button", {
                    class: "btn btn-success",
                    onclick: () => startPipeline(),
                }, data.status === "created" ? "启动 Pipeline" : "继续 Pipeline"),
                el("button", {
                    class: "btn btn-accent",
                    onclick: () => startDiscovery(),
                }, "启动构效关系发现"),
                el("button", {
                    class: "btn btn-secondary",
                    onclick: () => setActivePage("progress"),
                }, "查看节点历史"),
                el("button", {
                    class: "btn btn-secondary",
                    onclick: () => setActivePage("method-alignment"),
                }, "方法↔代码对齐"),
            ]),
        ]);
        // 下载区
        card.appendChild(el("div", { class: "download-divider" }));
        card.appendChild(renderDownloadBar([
            { type: "full-report", label: "全流程报告", filename: "full_report.md" },
            { type: "research-report", label: "调研报告", filename: "research_report.md" },
            { type: "ideas-summary", label: "思路汇总", filename: "ideas_summary.md" },
            { type: "method-doc", label: "方法文档", filename: "method_doc.md" },
            { type: "experiment-code", label: "实验代码", filename: "run_exp.py" },
            { type: "experiment-results", label: "实验结果", filename: "experiment_results.md" },
            { type: "claims-summary", label: "Claim 汇总", filename: "claims_summary.md" },
            { type: "discovery-report", label: "发现报告", filename: "discovery_report.md" },
            { type: "paper-draft", label: "论文稿", filename: "paper_draft.md" },
        ]));
        return card;
    }

    // ===== 1. 项目创建页 =====

    function renderCreate(content) {
        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "启动新的科研项目"),
            el("p", { class: "muted small mb-0" },
                "输入研究主题，系统将依次执行：调研 → 思路探讨 → 方案制定 → 实验运行 → 论文写作。"),
        ]);

        const topicField = el("div", { class: "field" }, [
            el("label", { class: "field-label", for: "topic-input" }, "研究主题"),
            el("textarea", {
                class: "textarea",
                id: "topic-input",
                placeholder: "示例：液冷材料",
            }),
        ]);
        card.appendChild(topicField);

        const btnRow = el("div", { class: "btn-row" }, [
            el("button", { class: "btn", id: "create-btn" }, "启动科研"),
            el("button", { class: "btn btn-outline", id: "topic-discovery-create-btn" }, "方向推荐"),
        ]);
        card.appendChild(btnRow);

        // 继续已有项目（配合服务端启动恢复机制：重启后旧项目自动恢复）
        const resumeRow = el("div", { class: "field mt-16" }, [
            el("label", { class: "field-label", for: "resume-project-input" }, "继续已有项目"),
            el("div", { class: "btn-row" }, [
                el("input", {
                    class: "input",
                    id: "resume-project-input",
                    placeholder: "粘贴已有 Project ID（服务重启后项目自动恢复，可直接查看数据）",
                    style: "flex:1;min-width:0;",
                }),
                el("button", { class: "btn btn-outline", id: "resume-project-btn" }, "继续"),
                el("button", { class: "btn btn-danger btn-outline", id: "delete-project-btn" }, "删除"),
            ]),
        ]);
        card.appendChild(resumeRow);

        // 会话重置：刷新项目 → 回到无项目状态（不清磁盘数据，仅清前端记忆）
        const sessionRow = el("div", { class: "field mt-16" }, [
            el("label", { class: "field-label" }, "会话重置"),
            el("p", { class: "muted small mb-8" },
                "刷新项目 = 清除页面上的项目记忆（localStorage），回到未创建项目的初始状态。磁盘上的项目数据保留，仍可用上方 Project ID 继续访问。"),
            el("div", { class: "btn-row" }, [
                el("button", { class: "btn btn-outline", id: "reset-session-btn" }, "刷新项目（回到无项目状态）"),
            ]),
        ]);
        card.appendChild(sessionRow);

        const resultArea = el("div", { id: "create-result" });
        card.appendChild(resultArea);

        content.appendChild(card);

        // 绑定「继续已有项目」
        document.getElementById("resume-project-btn").addEventListener("click", async () => {
            const pid = document.getElementById("resume-project-input").value.trim();
            if (!pid) {
                showToast("请输入项目 ID", "error");
                return;
            }
            const btn = document.getElementById("resume-project-btn");
            btn.disabled = true;
            btn.textContent = "恢复中…";
            try {
                const st = await api("GET", `/api/projects/${pid}/status`);
                state.currentProjectId = pid;
                updateProjectIdDisplay();
                startPolling();
                renderSidebarNotes();
                renderCreateResult(resultArea, { project_id: pid, topic: st.topic || "" });
                showToast("已恢复项目，可查看数据", "success");
                // 留在新建页展示恢复结果（主题/操作按钮），由用户决定下一步
                // 不再强制跳转 papers，让主题展示可见
            } catch (e) {
                showToast("项目不存在：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "继续";
            }
        });

        // 绑定「删除项目」
        document.getElementById("delete-project-btn").addEventListener("click", async () => {
            const pid = document.getElementById("resume-project-input").value.trim();
            if (!pid) {
                showToast("请输入要删除的 Project ID", "error");
                return;
            }
            if (!confirm(`确认删除项目 ${pid}？\n\n此操作不可恢复：内存中的运行状态将被清理，磁盘上的项目数据（论文/材料/Claim/实验等）也将一并删除。`)) {
                return;
            }
            const btn = document.getElementById("delete-project-btn");
            btn.disabled = true;
            btn.textContent = "删除中…";
            try {
                await api("DELETE", `/api/projects/${pid}`);
                // 若删的就是当前项目，重置前端状态并回到「新建项目」页
                if (state.currentProjectId === pid) {
                    state.currentProjectId = null;
                    state.statusCache = null;
                    state.discoveryCache = null;
                    state.dashboardCache = null;
                    state.researchReportCache = null;
                    state.discoveryDetailCache = null;
                    state.materialsCvCache = null;
                    state.methodAlignmentCache = null;
                    clearProjectFromStorage();
                    stopPolling();
                    updateProjectIdDisplay();
                    renderSidebarNotes();
                    setActivePage("create");
                }
                showToast("项目已删除", "success");
                document.getElementById("resume-project-input").value = "";
            } catch (e) {
                showToast("删除失败：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "删除";
            }
        });

        // 绑定「刷新项目」：清除前端项目记忆，回到无项目状态（磁盘数据保留）
        document.getElementById("reset-session-btn").addEventListener("click", async () => {
            const pid = state.currentProjectId;
            if (!pid && !getProjectFromStorage()) {
                showToast("当前已是无项目状态", "info");
                return;
            }
            if (!confirm("确认刷新项目？\n\n将清除页面上的项目记忆并回到未创建项目状态。磁盘上的项目数据会保留，仍可通过 Project ID 继续访问。")) {
                return;
            }
            const btn = document.getElementById("reset-session-btn");
            btn.disabled = true;
            btn.textContent = "刷新中…";
            try {
                // 清空前端状态 + localStorage + 停止轮询
                state.currentProjectId = null;
                state.statusCache = null;
                state.statusCachePrevious = null;
                state.discoveryCache = null;
                state.dashboardCache = null;
                state.researchReportCache = null;
                state.discoveryDetailCache = null;
                state.materialsCvCache = null;
                state.methodAlignmentCache = null;
                state.papersCache = null;
                state.papersFilter = { q: "", yearMin: "", yearMax: "", venue: "", sort: "newest", casZone: "", ifMin: "", pdfOnly: false };
                state.searchPrefs = null;
                state.searchPrefsForm = null;
                state.runMode = "";
                clearProjectFromStorage();
                stopPolling();
                updateProjectIdDisplay();
                renderSidebarNotes();
                renderSidebarDownload();
                setActivePage("create");
                showToast("已刷新，回到无项目状态", "success");
            } catch (e) {
                showToast("刷新失败：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "刷新项目（回到无项目状态）";
            }
        });

        // 绑定按钮
        document.getElementById("create-btn").addEventListener("click", async () => {
            const topic = document.getElementById("topic-input").value.trim();
            if (!topic) {
                showToast("请输入研究主题", "error");
                return;
            }
            const btn = document.getElementById("create-btn");
            btn.disabled = true;
            btn.textContent = "创建中…";
            showLoadingOverlay("正在创建项目…");
            try {
                const data = await api("POST", "/api/projects", { topic });
                state.currentProjectId = data.project_id;
                saveProjectToStorage(data.project_id);
                updateProjectIdDisplay();
                startPolling();
                renderSidebarNotes();
                renderSidebarDownload();
                renderCreateResult(resultArea, data);
                showToast("项目已创建", "success");
            } catch (e) {
                showToast("创建失败：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "启动科研";
                hideLoadingOverlay();
            }
        });

        // 方向推荐按钮：创建项目后启动方向推荐
        document.getElementById("topic-discovery-create-btn").addEventListener("click", async () => {
            const topic = document.getElementById("topic-input").value.trim();
            if (!topic) {
                showToast("请输入研究兴趣", "error");
                return;
            }
            const btn = document.getElementById("topic-discovery-create-btn");
            btn.disabled = true;
            btn.textContent = "创建中…";
            try {
                // 创建项目（topic 字段暂存研究兴趣）
                const data = await api("POST", "/api/projects", { topic });
                state.currentProjectId = data.project_id;
                updateProjectIdDisplay();
                startPolling();
                renderSidebarNotes();
                // 启动方向推荐
                await api("POST", `/api/projects/${data.project_id}/run-topic-discovery`, { interest: topic });
                state.runMode = "topic_discovery";
                setActivePage("topic-discovery");
                showToast("方向推荐已启动", "success");
            } catch (e) {
                showToast("启动失败：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "方向推荐";
            }
        });

        // 若已有项目，显示当前项目并允许切换
        if (state.currentProjectId) {
            renderCreateResult(resultArea, { project_id: state.currentProjectId, topic: state.statusCache?.topic || "" });
        }
        // 若已有项目，显示当前项目并允许切换
        if (state.currentProjectId) {
            renderCreateResult(resultArea, { project_id: state.currentProjectId, topic: state.statusCache?.topic || "" });
        }

        // 文件上传区（上传文献 PDF/文本 或 主题描述文件）
        if (state.currentProjectId) {
            content.appendChild(renderUploadCard());
        }

        // 项目列表（刷新后可恢复/切换）
        renderProjectList(content);
    }

    async function renderProjectList(content) {
        try {
            const data = await api("GET", "/api/projects");
            const projects = data.projects || [];
            if (!projects.length) return;
            const card = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "已有项目（点击切换）"),
            ]);
            const list = el("div", { class: "list" });
            projects.forEach(p => {
                const isCurrent = p.project_id === state.currentProjectId;
                const item = el("div", {
                    class: `list-item project-switch-item ${isCurrent ? "current" : ""}`,
                    onclick: async () => {
                        state.currentProjectId = p.project_id;
                        state.autoNavDone = false; // 切换项目后允许重新自动导航
                        saveProjectToStorage(p.project_id);
                        updateProjectIdDisplay();
                        startPolling();
                        renderSidebarDownload();
                        showToast(`已切换到项目：${p.topic.slice(0, 30)}`, "success");
                        setActivePage("dashboard");
                    },
                });
                item.appendChild(el("div", { class: "list-item-head" }, [
                    el("span", { class: "list-item-title", text: p.topic || "(无主题)" }),
                    isCurrent ? el("span", { class: "badge badge-success", text: "当前" }) : null,
                    el("span", { class: `badge ${statusBadge(p.status).match(/badge-(\w+)/)?.[1] || "badge-neutral"}`, text: p.status }),
                ]));
                item.appendChild(el("div", { class: "small muted" },
                    `${p.project_id} · ${formatTime(p.created_at)}`));
                if (p.summary) {
                    item.appendChild(el("div", { class: "small muted" },
                        (p.summary || "").slice(0, 80)));
                }
                list.appendChild(item);
            });
            card.appendChild(list);
            content.appendChild(card);
        } catch (e) {
            // 静默
        }
    }

    function renderUploadCard() {
        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "上传文献 / 主题文件"),
            el("p", { class: "muted small mb-12" },
                "支持上传 PDF/TXT/MD 格式的文献（入库为 Paper 实体），或上传主题描述文件覆盖当前研究主题。"),
        ]);

        // 上传文献
        const paperSection = el("div", { class: "upload-section" }, [
            el("div", { class: "field-label" }, "上传文献"),
        ]);
        const paperFile = el("input", { type: "file", accept: ".pdf,.txt,.md", class: "upload-input" });
        const paperTitle = el("input", { type: "text", class: "input", placeholder: "文献标题（可选，默认用文件名）" });
        const paperBtn = el("button", { class: "btn btn-secondary btn-sm", text: "上传文献" });
        paperSection.appendChild(paperFile);
        paperSection.appendChild(paperTitle);
        paperSection.appendChild(paperBtn);

        paperBtn.addEventListener("click", async () => {
            if (!paperFile.files.length) {
                showToast("请选择文件", "error");
                return;
            }
            const fd = new FormData();
            fd.append("file", paperFile.files[0]);
            if (paperTitle.value.trim()) {
                fd.append("title", paperTitle.value.trim());
            }
            paperBtn.disabled = true;
            paperBtn.textContent = "上传中…";
            try {
                const resp = await fetch(
                    `/api/projects/${state.currentProjectId}/upload-paper`,
                    { method: "POST", body: fd }
                );
                const data = await resp.json();
                if (resp.ok) {
                    showToast(`文献上传成功：${data.title}（${data.chunks} 个 chunk）`, "success");
                    paperFile.value = "";
                    paperTitle.value = "";
                    // 刷新当前页（论文页会重新拉取列表，新建页无副作用）
                    renderPage();
                } else {
                    showToast("上传失败：" + (data.detail || "未知错误"), "error");
                }
            } catch (e) {
                showToast("上传失败：" + e.message, "error");
            } finally {
                paperBtn.disabled = false;
                paperBtn.textContent = "上传文献";
            }
        });
        card.appendChild(paperSection);

        // 分隔
        card.appendChild(el("div", { class: "download-divider" }));

        // 上传主题文件
        const topicSection = el("div", { class: "upload-section" }, [
            el("div", { class: "field-label" }, "上传主题描述文件"),
        ]);
        const topicFile = el("input", { type: "file", accept: ".txt,.md", class: "upload-input" });
        const topicBtn = el("button", { class: "btn btn-secondary btn-sm", text: "上传并覆盖主题" });
        topicSection.appendChild(topicFile);
        topicSection.appendChild(topicBtn);

        topicBtn.addEventListener("click", async () => {
            if (!topicFile.files.length) {
                showToast("请选择文件", "error");
                return;
            }
            const fd = new FormData();
            fd.append("file", topicFile.files[0]);
            topicBtn.disabled = true;
            topicBtn.textContent = "上传中…";
            try {
                const resp = await fetch(
                    `/api/projects/${state.currentProjectId}/upload-topic`,
                    { method: "POST", body: fd }
                );
                const data = await resp.json();
                if (resp.ok) {
                    showToast("主题已更新", "success");
                    // 同步到 topic 输入框
                    const ti = document.getElementById("topic-input");
                    if (ti) ti.value = data.topic;
                    if (state.statusCache) state.statusCache.topic = data.topic;
                    updateProjectIdDisplay();
                } else {
                    showToast("上传失败：" + (data.detail || "未知错误"), "error");
                }
            } catch (e) {
                showToast("上传失败：" + e.message, "error");
            } finally {
                topicBtn.disabled = false;
                topicBtn.textContent = "上传并覆盖主题";
            }
        });
        card.appendChild(topicSection);

        return card;
    }

    function renderCreateResult(container, data) {
        clear(container);
        const pid = el("div", { class: "project-id-display" }, [
            el("span", { text: "Project ID：" }),
            el("strong", { text: data.project_id }),
            el("button", {
                class: "copy-btn",
                text: "复制",
                onclick: async () => {
                    try {
                        await navigator.clipboard.writeText(data.project_id);
                        showToast("已复制", "success");
                    } catch (e) {
                        showToast("复制失败", "error");
                    }
                },
            }),
        ]);
        container.appendChild(pid);

        // 主题展示（含恢复的主题）
        if (data.topic) {
            const topicBox = el("div", { class: "project-topic-display mt-12" }, [
                el("span", { class: "project-topic-label", text: "研究主题：" }),
                el("span", { class: "project-topic-value", text: data.topic }),
            ]);
            container.appendChild(topicBox);
        }

        const actions = el("div", { class: "btn-row mt-16" }, [
            el("button", { class: "btn btn-success", onclick: () => startPipeline() }, "启动 Pipeline"),
            el("button", { class: "btn btn-accent", onclick: () => startDiscovery() }, "启动构效关系发现"),
            el("button", { class: "btn btn-secondary", onclick: () => setActivePage("progress") }, "查看进度"),
        ]);

        container.appendChild(actions);
    }

    async function startPipeline(forceWriting) {
        if (!state.currentProjectId) return;
        try {
            const body = forceWriting ? { force_writing: true } : undefined;
            await api("POST", `/api/projects/${state.currentProjectId}/run`, body);
            showToast(forceWriting ? "已启动强制写作模式" : "Pipeline 已启动", "success");
            setActivePage("progress");
        } catch (e) {
            showToast("启动失败：" + e.message, "error");
        }
    }

    async function startDiscovery() {
        if (!state.currentProjectId) return;
        try {
            const r = await api("POST", `/api/projects/${state.currentProjectId}/run-discovery`);
            state.runMode = "discovery";
            showToast(r && r.resumed ? "已继续构效关系发现" : "构效关系发现已启动", "success");
            setActivePage("discovery");
        } catch (e) {
            showToast("启动失败：" + e.message, "error");
        }
    }

    // ===== 方向推荐页面 =====

    // ===== 方向推荐：维度对比总览表（纯展示，不参与排序/选择） =====
    // 注意：此表仅按服务端已排好的顺序展示，绝不在前端重排，
    // 保证卡片数字下标 = 服务端 recommendations 下标 = 提交文本 String(i+1)。

    // 增长率格式化：模块级函数，对比表与卡片共用
    const fmtGrowth = g => (g == null ? "N/A" : `${g > 0 ? "+" : ""}${Math.round(g * 100)}%`);

    function buildRecCompareTable(recommendations, selectedTopic) {
        const dimClass = v => {
            if (!v) return "";
            const s = String(v).toLowerCase();
            if (s === "high" || s === "hard") return "dim-high";
            if (s === "medium" || s === "med" || s === "moderate") return "dim-med";
            if (s === "low" || s === "easy") return "dim-low";
            return "";
        };
        const dimText = v => {
            if (!v) return "N/A";
            const map = {
                high: "高", hard: "高", medium: "中", med: "中", moderate: "中",
                low: "低", easy: "低",
            };
            return map[String(v).toLowerCase()] || v;
        };

        const head = el("div", { class: "rec-compare-row rec-compare-head" }, [
            el("span", { class: "c-idx" }, "#"),
            el("span", { class: "c-topic" }, "推荐主题"),
            el("span", { class: "c-dim", title: "该方向在领域内的受关注程度（0~100 评分）" }, "热门度"),
            el("span", { class: "c-dim", title: "实现该方向的难易程度：高=需大量实验/算力，低=可行性更高" }, "难度"),
            el("span", { class: "c-dim", title: "创新度：该方向相对已有工作的新颖程度，高=填补空白或提出新范式" }, "创新度"),
            el("span", { class: "c-dim", title: "关联度：该方向与你的研究兴趣的匹配程度，高=贴合输入兴趣" }, "关联度"),
            el("span", { class: "c-dim", title: "增长率：该方向关键词在近年的年度论文增长幅度，高=领域正在升温" }, "增长率"),
        ]);
        const rows = [head];

        recommendations.forEach((rec, i) => {
            const isSelected = selectedTopic && rec.topic === selectedTopic;
            const pop = rec.popularity_score;
            const row = el("div", { class: "rec-compare-row" }, [
                el("span", { class: "c-idx" }, `[${i + 1}]`),
                el("span", { class: "c-topic", title: rec.topic || "" },
                    isSelected ? `✓ ${rec.topic || "N/A"}` : (rec.topic || "N/A")),
                el("span", { class: "c-dim pop-cell" }, [
                    el("span", { class: "pop-bar" }, [
                        el("span", { class: "pop-fill", style: `width:${Math.min(100, Math.max(0, pop || 0))}%` }),
                    ]),
                    el("span", { class: "pop-val" }, `${pop ?? "N/A"}`),
                ]),
                el("span", { class: `c-dim ${dimClass(rec.difficulty)}` }, [
                    el("span", { class: "dim-dot" }),
                    dimText(rec.difficulty),
                ]),
                el("span", { class: `c-dim ${dimClass(rec.novelty)}` }, [
                    el("span", { class: "dim-dot" }),
                    dimText(rec.novelty),
                ]),
                el("span", { class: `c-dim ${dimClass(rec.relevance)}` }, [
                    el("span", { class: "dim-dot" }),
                    dimText(rec.relevance),
                ]),
                el("span", { class: "c-dim c-dim-col" }, fmtGrowth(rec.growth_rate)),
            ]);
            rows.push(row);
        });

        // 表尾图例：说明每个维度的含义，避免用户看不懂
        rows.push(el("div", { class: "rec-compare-row rec-compare-legend" }, [
            el("span", { class: "c-idx" }, "ℹ"),
            el("span", { class: "c-topic", text: "怎么看：创新度=填补空白或新范式的程度；关联度=与你的兴趣匹配程度；增长率=近年论文增长幅度（+% 为上升）" }),
            el("span", { class: "c-dim" }, ""),
            el("span", { class: "c-dim" }, ""),
            el("span", { class: "c-dim" }, ""),
            el("span", { class: "c-dim" }, ""),
            el("span", { class: "c-dim" }, ""),
        ]));

        return el("div", { class: "rec-compare mt-12" }, rows);
    }

    function renderTopicDiscovery(content) {
        const status = state.statusCache;
        const td = status?.topic_discovery || {};
        const recommendations = td.recommendations || [];
        const selectedTopic = td.selected_topic || "";
        const interest = td.interest || status?.topic || "";

        const card = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "方向推荐"),
            el("p", { class: "muted small" },
                "输入研究兴趣，系统将分析领域趋势（关键词演化、增长率）并推荐值得研究的方向。"),
        ]);

        // 研究兴趣显示
        if (interest) {
            card.appendChild(el("div", { class: "field" }, [
                el("span", { class: "field-label" }, "研究兴趣："),
                el("span", { text: interest }),
            ]));
        }

        // 状态横幅
        if (status && status.status) {
            card.appendChild(el("div", {
                html: statusBanner(status.status, status.summary, status.error, status.recommendation, status.advice),
            }));
        }

        // 等待人工选择
        if (status && status.status === "pending_human") {
            card.appendChild(el("div", { class: "alert alert-warning" }, [
                el("p", {}, "系统已生成推荐主题，等待你选择。"),
                el("button", { class: "btn", onclick: () => setActivePage("human") }, "去选择主题"),
            ]));
        }

        // 推荐结果列表（服务端已按热门度 popularity_score 降序返回，前端不要二次排序，
        // 否则数字下标与 TopicSelectHuman 的服务端解析不一致会导致选错主题）
        if (recommendations.length > 0) {
            const recSection = el("div", { class: "mt-16" }, [
                el("h3", { class: "section-title" }, `推荐研究主题（${recommendations.length} 个，按热门度排序）`),
            ]);

            // 维度对比总览表（纯展示，点击选择仍走下方卡片；排序与选择契约不受影响）
            recSection.appendChild(buildRecCompareTable(recommendations, selectedTopic));

            recommendations.forEach((rec, i) => {
                const isSelected = selectedTopic && rec.topic === selectedTopic;
                const recCard = el("div", {
                    class: "card rec-card" + (isSelected ? " rec-selected" : ""),
                    onclick: () => selectTopic(i, rec),
                }, [
                    el("div", { class: "rec-header" }, [
                        el("span", { class: "rec-index" }, `[${i + 1}]`),
                        el("span", { class: "rec-topic", text: rec.topic || "N/A" }),
                        isSelected ? el("span", { class: "badge badge-success" }, "已选择") : null,
                    ]),
                    // 热门度条形（0-100）
                    el("div", { class: "rec-popbar" }, [
                        el("span", { class: "rec-popbar-label" }, "热门度"),
                        el("span", { class: "rec-popbar-track" }, [
                            el("span", { class: "rec-popbar-fill", style: `width:${Math.min(100, Math.max(0, rec.popularity_score || 0))}%` }),
                        ]),
                        el("span", { class: "rec-popbar-val" }, `${rec.popularity_score ?? "N/A"}`),
                    ]),
                    el("div", { class: "rec-body" }, [
                        el("div", { class: "rec-field" }, [
                            el("span", { class: "rec-label", text: "理由：" }),
                            el("span", { text: rec.rationale || "N/A" }),
                        ]),
                        el("div", { class: "rec-field" }, [
                            el("span", { class: "rec-label", text: "创新切入点：" }),
                            el("span", { text: rec.innovation_point || "N/A" }),
                        ]),
                        el("div", { class: "rec-field" }, [
                            el("span", { class: "rec-label", text: "推荐材料：" }),
                            el("span", { text: (rec.recommended_materials || []).join(", ") || "N/A" }),
                        ]),
                        el("div", { class: "rec-field" }, [
                            el("span", { class: "rec-label", text: "趋势摘要：" }),
                            el("span", { text: rec.trend_summary || "N/A" }),
                        ]),
                        el("div", { class: "rec-tags" }, [
                            el("span", { class: "badge badge-neutral", title: "实现该方向的难易程度：高=需大量实验/算力，低=可行性更高", text: `难度: ${rec.difficulty || "N/A"}` }),
                            el("span", { class: "badge badge-info", title: "创新度：该方向相对已有工作的新颖程度，高=填补空白或提出新范式", text: `创新度: ${rec.novelty || "N/A"}` }),
                            el("span", { class: "badge badge-warning", title: "热门度：该方向在领域内的受关注程度（0~100 评分）", text: `热门度: ${rec.popularity_score != null ? rec.popularity_score : "N/A"}` }),
                            el("span", { class: "badge badge-success", title: "关联度：该方向与你的研究兴趣的匹配程度，高=贴合输入兴趣", text: `关联度: ${rec.relevance || "N/A"}` }),
                            el("span", { class: "badge badge-accent", title: "增长率：该方向关键词在近年的年度论文增长幅度，高=领域正在升温", text: `增长率: ${fmtGrowth(rec.growth_rate)}` }),
                        ]),
                        el("div", { class: "rec-hint small muted mt-8" },
                            "点击卡片即可选择该主题"),
                    ]),
                ]);
                recSection.appendChild(recCard);
            });

            card.appendChild(recSection);
        }

        // 已完成且选择了主题 → 提供开始文献调研按钮
        if (status && status.status === "completed" && selectedTopic) {
            card.appendChild(el("div", { class: "alert alert-success mt-16" }, [
                el("p", {}, `已选择主题：${selectedTopic}`),
            ]));
            card.appendChild(el("div", { class: "btn-row mt-16" }, [
                el("button", {
                    class: "btn btn-success",
                    onclick: () => startPipeline(),
                }, "使用该主题开始文献调研"),
                el("button", {
                    class: "btn btn-secondary",
                    onclick: () => setActivePage("progress"),
                }, "查看进度"),
            ]));
        }

        // 运行中提示
        if (status && status.status === "running" && recommendations.length === 0) {
            card.appendChild(el("div", { class: "alert alert-info" }, [
                el("p", {}, "正在分析领域趋势，获取论文数据并生成推荐…"),
                el("p", { class: "muted small" }, "此过程可能需要 1-3 分钟（真实模式）或几秒（dry_run 模式）"),
            ]));
        }

        content.appendChild(card);
    }

    // 点击推荐卡片选择主题（数字下标 = 服务端 ctx 中 recommendations 下标，与服务端 TopicSelectHuman 解析一致）
    async function selectTopic(i, rec) {
        if (!state.currentProjectId) return;
        try {
            await api("POST", `/api/projects/${state.currentProjectId}/human-response`, {
                action: "continue",
                text: String(i + 1),
            });
            showToast("已选择：" + (rec.topic || ""), "success");
            state.humanFingerprint = null; // 强制下一轮刷新页面状态
            // 后端在方向推荐完成后会自动启动 research pipeline。
            // 这里轮询几次确认状态从 completed 切到 running 即跳到进度页。
            setActivePage("progress");
            for (let k = 0; k < 8; k++) {
                await pollStatus();
                if (state.statusCache && state.statusCache.status === "running") {
                    renderPage();
                    return;
                }
                await new Promise(r => setTimeout(r, 500));
            }
            renderPage();
        } catch (e) {
            showToast("选择失败：" + e.message, "error");
        }
    }

    async function fetchDiscoveries() {
        if (!state.currentProjectId) return;
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/discoveries`);
            state.discoveryCache = data;
            if (data.run_mode) state.runMode = data.run_mode;
            setBadge("badge-discovery", (data.relationships || []).length || null);
            // 仅在数据真有变化时增量更新（避免每 2s 整页重建导致抖动）
            if (state.currentPage === "discovery") {
                const rels = data.relationships || [];
                const prevRels = state.discoveryCachePrevious?.relationships || [];
                const summary = data.discovery_summary || {};
                const prevSummary = state.discoveryCachePrevious?.discovery_summary || {};
                const relsChanged = JSON.stringify(rels) !== JSON.stringify(prevRels);
                const summaryChanged = JSON.stringify(summary) !== JSON.stringify(prevSummary);
                if (relsChanged || summaryChanged) {
                    // 仅关系或汇总变化时整页重建（用户感知有意义的变化）
                    renderPage();
                }
                // 否则不重渲染，避免滚动位置丢失、勾选状态丢失、闪屏
                state.discoveryCachePrevious = data;
            }
        } catch (e) {
            // 静默
        }
    }

    // ===== 2. 研究进度页 =====

    function renderProgress(content) {
        const data = state.statusCache;
        if (!data) {
            content.appendChild(el("div", { class: "loading" }, "加载中…"));
            pollStatus();
            return;
        }

        // 顶部状态
        content.insertAdjacentHTML("beforeend",
            statusBanner(data.status, data.summary, data.error, data.recommendation, data.advice));

        // 阶段进度条
        content.appendChild(renderStageProgress(data));

        // 当前执行提示卡（长任务不干等：正在做什么 + 下一步）
        if (data.status === "running" || data.status === "pending_human") {
            content.appendChild(renderCurrentNode(data));
        }

        // 计数卡片
        content.appendChild(renderCounts(data.counts || {}));

        // 操作区
        const actionCard = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "Pipeline 操作"),
            el("div", { class: "btn-row" }, [
                el("button", {
                    class: "btn btn-success",
                    onclick: () => startPipeline(),
                }, data.status === "created" ? "启动 Pipeline" : "继续 / 重启"),
                el("button", {
                    class: "btn btn-accent",
                    onclick: () => startDiscovery(),
                }, "启动构效关系发现"),
                el("button", {
                    class: "btn btn-secondary",
                    onclick: () => { pollStatus(); showToast("已刷新", "success"); },
                }, "刷新状态"),
            ]),
        ]);
        content.appendChild(actionCard);

        // 实验失败时的特殊操作区
        if (data.status === "experiment_failed") {
            const failCard = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "实验未通过 — 后续选择"),
                el("p", { class: "muted small" },
                    "实验未能验证核心 Claim 是科研常态。你可以选择：强制进入论文写作（撰写负面结果），" +
                    "或重新启动 Pipeline 从思路探讨阶段改进方案。"),
            ]);
            const failRow = el("div", { class: "btn-row" }, [
                el("button", {
                    class: "btn btn-warning",
                    onclick: () => startPipeline(true),
                }, "强制生成论文（撰写负面结果）"),
                el("button", {
                    class: "btn btn-secondary",
                    onclick: () => {
                        startPipeline();
                        showToast("已重新启动 Pipeline，将从未完成阶段继续", "success");
                    },
                }, "重新启动 Pipeline"),
            ]);
            failCard.appendChild(failRow);
            content.appendChild(failCard);
        }

        // 中止/失败时的操作
        if (data.status === "aborted" || data.status === "failed") {
            const errCard = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "异常恢复"),
                el("p", { class: "muted small" },
                    `Pipeline 状态为 ${data.status}。可以重新启动继续执行，或查看错误信息。`),
                el("div", { class: "btn-row" }, [
                    el("button", {
                        class: "btn btn-success",
                        onclick: () => startPipeline(),
                    }, "重新启动"),
                ]),
            ]);
            content.appendChild(errCard);
        }

        // 节点历史时间线
        content.appendChild(renderTimeline(data.node_history || []));
    }

    function renderCurrentNode(data) {
        const card = el("div", { class: "card current-node-card" });
        const cur = data.current_node;
        const next = data.next_nodes || [];

        // 当前执行
        if (cur && cur.node_id) {
            const head = el("div", { class: "current-node-head" }, [
                el("span", { class: "current-node-dot" }),
                el("div", {}, [
                    el("div", { class: "current-node-title" }, [
                        el("strong", { text: "正在执行：" }),
                        el("span", { text: nodeLabel(cur.node_id) }),
                        el("span", { class: "badge badge-info mono small", text: cur.node_id }),
                    ]),
                    el("div", { class: "current-node-hint muted small" },
                        "长任务通常需要几分钟，页面会自动刷新进度，请耐心等待…"),
                ]),
            ]);
            card.appendChild(head);

            // 下一步预告
            if (next && next.length) {
                const steps = next.map(n =>
                    el("span", { class: "next-node-chip", text: nodeLabel(n) }));
                card.appendChild(el("div", { class: "next-node-row" }, [
                    el("span", { class: "muted small", text: "下一步：" }),
                    ...steps,
                ]));
            }
        } else if (data.status === "pending_human") {
            card.appendChild(el("div", { class: "current-node-head" }, [
                el("span", { class: "current-node-dot warn" }),
                el("div", {}, [
                    el("div", { class: "current-node-title" },
                        el("strong", { text: "等待人工确认：" })),
                    el("div", { class: "current-node-hint muted small" },
                        "请在下方操作区或相应页面完成确认后继续。"),
                ]),
            ]));
        } else {
            card.appendChild(el("div", { class: "current-node-hint muted small" },
                "任务已启动，正在初始化…"));
        }
        return card;
    }

    function renderStageProgress(data) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "生命周期阶段"));
        const stageStatuses = data.stage_statuses || {};
        const currentStage = data.current_stage;

        const progress = el("div", { class: "stage-progress" });
        STAGES.forEach((s, i) => {
            const status = stageStatuses[s] || "not_started";
            let cls = "";
            if (status === "done") cls = "done";
            else if (s === currentStage && status !== "not_started") cls = "active";
            else if (status === "in_progress" || status === "pending_review" || status === "blocked") cls = "active";

            const step = el("div", { class: `stage-step ${cls}` }, [
                el("div", { class: "stage-name", text: STAGE_LABELS[s] || s }),
                el("div", { class: "stage-status", text: status.replace(/_/g, " ") }),
            ]);
            progress.appendChild(step);
        });
        card.appendChild(progress);
        return card;
    }

    function renderCounts(counts) {
        const wrap = el("div", { class: "counts-grid" });
        const items = [
            { label: "论文", value: counts.papers, extra: "research 阶段产出" },
            { label: "思路", value: counts.ideas, extra: "ideation 阶段产出" },
            { label: "Claim", value: counts.claims, extra: "design 阶段产出" },
            { label: "实验", value: counts.experiments, extra: "experiment 阶段产出" },
        ];
        items.forEach(it => {
            wrap.appendChild(el("div", { class: "count-card" }, [
                el("div", { class: "count-label", text: it.label }),
                el("div", { class: "count-value", text: String(it.value != null ? it.value : 0) }),
                el("div", { class: "count-extra", text: it.extra }),
            ]));
        });
        return wrap;
    }

    function renderTimeline(history) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "节点执行历史"));
        if (!history || history.length === 0) {
            card.appendChild(el("div", { class: "list-empty" }, "暂无节点执行记录"));
            content.appendChild(card);
            return card;
        }
        const tl = el("div", { class: "timeline" });
        history.forEach(h => {
            const status = h.status || "unknown";
            let cls = "";
            if (status === "success") cls = "success";
            else if (status === "failed") cls = "failed";
            else if (status === "pending_human") cls = "pending";
            else if (status === "skipped") cls = "skipped";

            const item = el("div", { class: `timeline-item ${cls}` });
            item.appendChild(el("div", { class: "timeline-head" }, [
                el("span", { class: "timeline-node-id", text: nodeLabel(h.node_id) }),
                el("span", { class: "timeline-node-type mono", text: h.node_id || "" }),
                el("span", { class: "timeline-node-id", text: h.node_id || "?" }),
                el("span", { class: "timeline-node-type", text: h.node_type || "" }),
                el("span", { class: "timeline-time", text: formatTime(h.timestamp) }),
            ]));
            item.appendChild(el("div", { class: "timeline-summary" },
                h.summary || `（无摘要，状态=${status}）`));
            tl.appendChild(item);
        });
        card.appendChild(tl);
        return card;
    }

    // ===== 3. 论文浏览页 =====

    async function renderPapers(content) {
        content.appendChild(el("div", { class: "loading" }, "加载中…"));
        try {
            // 并行拉取论文列表 + 检索证据链（审计轨迹）+ 未入库候选
            const evData = await api("GET", `/api/projects/${state.currentProjectId}/evidence`)
                .catch(() => null);
            const paperData = await api("GET", `/api/projects/${state.currentProjectId}/papers`);
            const unlinkedData = await api("GET", `/api/projects/${state.currentProjectId}/unlinked-papers`)
                .catch(() => null);
            const papers = paperData.papers || [];
            const unlinked = (unlinkedData && unlinkedData.papers) || [];
            state.papersCache = papers; // 供 rerenderPaperList 前端过滤复用
            clear(content);

            // ===== 未入库候选视图 =====
            if (state.papersView === "unlinked") {
                renderUnlinkedPapers(content, unlinked, (unlinkedData && unlinkedData.filter_note) || "");
                return;
            }

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "论文浏览"),
                el("p", { class: "page-desc" }, `共 ${papers.length} 篇入库论文，点击条目展开详情。`),
            ]));
            // 检索证据链卡片（真实模式下由 Sciverse/arXiv/S2 检索命中记录生成）
            content.appendChild(renderEvidenceCard(evData));
            // 未入库论文入口卡片：始终显示（0 篇时提示暂无候选），标注筛选/去重数量
            {
                const rejected = unlinked.filter(p => p.reason === "score_rejected").length;
                const merged = unlinked.filter(p => p.reason === "dedup_merged").length;
                const desc = unlinked.length
                    ? `${unlinked.length} 篇检索命中但未入库的候选（相关度<0.5 被筛选 ${rejected} 篇 · 与已入库重复去重 ${merged} 篇），可手动补录入库`
                    : "暂无未入库候选：当前检索命中的证据均已关联入库或被去重剔除（运行 research 阶段后自动生成候选列表）";
                content.appendChild(el("div", {
                    class: "card unlinked-entry",
                    id: "unlinked-entry",
                    onclick: () => { state.papersView = "unlinked"; renderPage(); },
                }, [
                    el("div", { class: "unlinked-entry-main" }, [
                        el("span", { class: "unlinked-entry-title" }, "未入库论文"),
                        el("span", { class: "unlinked-entry-desc", text: desc }),
                    ]),
                    el("span", { class: "unlinked-entry-arrow" }, "查看 →"),
                ]));
            }
            if (!papers.length) {
                content.appendChild(el("div", { class: "list-empty" }, "暂无论文，请先启动 research 阶段"));
                return;
            }
            // 筛选工具栏（搜索/年份/期刊/排序），前端过滤
            content.appendChild(buildPaperFilterBar(papers));

            // 应用筛选 + 排序
            const filtered = filterPapers(papers, state.papersFilter);

            const list = el("div", { class: "list" });
            filtered.forEach(p => list.appendChild(renderPaperItem(p)));
            content.appendChild(list);
            // 空结果提示由 rerenderPaperList 统一维护，这里不重复添加

            // 证据溯源定位：跳转本页后定位到指定论文（滚动 + 高亮 + 展开）
            const targetId = state.pendingPaperId;
            if (targetId) {
                state.pendingPaperId = null;
                const cards = list.querySelectorAll(".list-item");
                for (const card of cards) {
                    if (card.getAttribute("data-paper-id") === targetId) {
                        card.scrollIntoView({ behavior: "smooth", block: "center" });
                        card.classList.add("paper-flash");
                        setTimeout(() => card.classList.remove("paper-flash"), 2600);
                        // 自动展开详情
                        if (!card.classList.contains("expanded")) {
                            card.classList.add("expanded");
                            if (!card.querySelector(".list-item-body")) {
                                const p = papers.find(x => x.paper_id === targetId);
                                if (p) card.appendChild(buildPaperBody(p));
                            }
                        }
                        break;
                    }
                }
            }
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 论文筛选工具栏（搜索/年份/期刊/排序，前端过滤）=====

    function buildPaperFilterBar(allPapers) {
        const F = state.papersFilter;
        const wrap = el("div", { class: "card paper-filter-bar" });

        // 关键词搜索
        const qInput = el("input", {
            class: "input paper-filter-input paper-filter-q",
            type: "text",
            placeholder: "搜索标题 / 作者 / 摘要 / Paper ID…",
            value: F.q,
        });
        qInput.addEventListener("input", () => { F.q = qInput.value.trim(); rerenderPaperList(); });

        // 年份范围
        const yMin = el("input", {
            class: "input paper-filter-year",
            type: "number", min: "1000", max: "2100",
            placeholder: "起始年",
            value: F.yearMin,
        });
        yMin.addEventListener("input", () => { F.yearMin = yMin.value; rerenderPaperList(); });
        const yMax = el("input", {
            class: "input paper-filter-year",
            type: "number", min: "1000", max: "2100",
            placeholder: "结束年",
            value: F.yearMax,
        });
        yMax.addEventListener("input", () => { F.yearMax = yMax.value; rerenderPaperList(); });

        // 期刊筛选（下拉列出已有的 venue 供选择）
        const venueOpts = [];
        const venueSet = new Set();
        allPapers.forEach(p => {
            const v = (p.venue || "").trim();
            if (v) venueSet.add(v);
        });
        venueOpts.push(el("option", { value: "", text: "全部期刊" }));
        [...venueSet].sort().forEach(v => {
            venueOpts.push(el("option", { value: v, text: truncateText(v, 40) }));
        });
        const venueSel = el("select", { class: "input paper-filter-select" });
        venueOpts.forEach(o => venueSel.appendChild(o));
        venueSel.value = F.venue || "";
        venueSel.addEventListener("change", () => { F.venue = venueSel.value; rerenderPaperList(); });

        // 中科院分区筛选
        const casSel = el("select", { class: "input paper-filter-select paper-filter-cas" });
        [
            ["", "全部分区"],
            ["1", "1区（顶刊）"],
            ["2", "2区"],
            ["3", "3区"],
            ["4", "4区"],
        ].forEach(([val, label]) => {
            casSel.appendChild(el("option", { value: val, text: label }));
        });
        casSel.value = F.casZone || "";
        casSel.addEventListener("change", () => { F.casZone = casSel.value; rerenderPaperList(); });

        // 最低影响因子
        const ifMin = el("input", {
            class: "input paper-filter-year paper-filter-if",
            type: "number", min: "0", max: "100", step: "0.1",
            placeholder: "最低IF",
            value: F.ifMin,
        });
        ifMin.addEventListener("input", () => { F.ifMin = ifMin.value; rerenderPaperList(); });

        // 排序
        const sortSel = el("select", { class: "input paper-filter-select" });
        [
            ["newest", "年份 ↓（最新优先）"],
            ["oldest", "年份 ↑（最早优先）"],
            ["relevance", "相关度 ↓"],
            ["if_desc", "影响因子 ↓"],
            ["if_asc", "影响因子 ↑"],
            ["title", "标题 A→Z"],
        ].forEach(([val, label]) => {
            sortSel.appendChild(el("option", { value: val, text: label }));
        });
        sortSel.value = F.sort || "newest";
        sortSel.addEventListener("change", () => { F.sort = sortSel.value; rerenderPaperList(); });

        // 仅可下载 PDF 复选框
        const pdfOnlyCb = el("input", {
            type: "checkbox",
            id: "pdf-only-toggle",
            class: "paper-filter-checkbox",
        });
        pdfOnlyCb.checked = !!F.pdfOnly;
        pdfOnlyCb.addEventListener("change", () => { F.pdfOnly = pdfOnlyCb.checked; rerenderPaperList(); });
        const pdfOnlyLabel = el("label", {
            class: "paper-filter-check-label",
            for: "pdf-only-toggle",
            text: "仅可下载",
        });

        // 结果计数 + 重置
        const countLabel = el("span", { class: "paper-filter-count", text: `${allPapers.length} 篇` });
        const resetBtn = el("button", {
            class: "btn btn-outline btn-sm",
            text: "重置",
            onclick: () => {
                F.q = ""; F.yearMin = ""; F.yearMax = ""; F.venue = ""; F.sort = "newest";
                F.casZone = ""; F.ifMin = ""; F.pdfOnly = false;
                qInput.value = ""; yMin.value = ""; yMax.value = "";
                venueSel.value = ""; casSel.value = ""; ifMin.value = ""; sortSel.value = "newest";
                pdfOnlyCb.checked = false;
                rerenderPaperList();
            },
        });

        wrap.appendChild(el("div", { class: "paper-filter-row" }, [
            qInput,
            el("span", { class: "paper-filter-label", text: "年份" }),
            yMin, el("span", { class: "paper-filter-sep", text: "–" }), yMax,
            el("span", { class: "paper-filter-label", text: "期刊" }),
            venueSel,
            el("span", { class: "paper-filter-label", text: "分区" }),
            casSel,
            el("span", { class: "paper-filter-label", text: "最低IF" }),
            ifMin,
            el("span", { class: "paper-filter-label", text: "排序" }),
            sortSel,
            el("span", { class: "paper-filter-check-wrap" }, [pdfOnlyCb, pdfOnlyLabel]),
            countLabel,
            resetBtn,
        ]));
        return wrap;
    }

    function filterPapers(allPapers, F) {
        const q = (F.q || "").toLowerCase().trim();
        const yMin = parseInt(F.yearMin, 10);
        const yMax = parseInt(F.yearMax, 10);
        const venueQ = (F.venue || "").toLowerCase().trim();
        const casZone = F.casZone || "";
        const ifMinVal = parseFloat(F.ifMin);
        const pdfOnly = !!F.pdfOnly;

        let out = allPapers.filter(p => {
            // 仅可下载 PDF（真实可下载：arxiv 直链 或 非 doi.org 兜底的 pdf_url）
            if (pdfOnly) {
                const pdfUrl = p.pdf_url || "";
                const arxivOk = !!p.arxiv_id;
                const directOk = pdfUrl && !pdfUrl.includes("doi.org");
                if (!arxivOk && !directOk) return false;
            }
            // 关键词：标题/作者/摘要/ID/来源
            if (q) {
                const hay = [
                    p.title || "", (p.authors || []).join(" "),
                    p.abstract || "", p.paper_id || "",
                    p.venue || "", p.source || "", p.doi || "",
                ].join(" ").toLowerCase();
                if (!hay.includes(q)) return false;
            }
            // 年份范围
            const y = p.year;
            if (y && !isNaN(yMin)) { if (y < yMin) return false; }
            if (y && !isNaN(yMax)) { if (y > yMax) return false; }
            // 期刊关键词
            if (venueQ && !((p.venue || "").toLowerCase().includes(venueQ))) return false;
            // 中科院分区筛选
            if (casZone && (p.cas_zone || "") !== casZone) return false;
            // 最低影响因子筛选
            if (!isNaN(ifMinVal) && (Number(p.impact_factor) || 0) < ifMinVal) return false;
            return true;
        });

        // 排序
        const sort = F.sort || "newest";
        out.sort((a, b) => {
            if (sort === "oldest") return (a.year || 0) - (b.year || 0);
            if (sort === "relevance") return (Number(b.relevance_score) || 0) - (Number(a.relevance_score) || 0);
            if (sort === "if_desc") return (Number(b.impact_factor) || 0) - (Number(a.impact_factor) || 0);
            if (sort === "if_asc") return (Number(a.impact_factor) || 0) - (Number(b.impact_factor) || 0);
            if (sort === "title") return String(a.title || "").localeCompare(String(b.title || ""));
            // newest 默认：年份新→旧，同年前靠 relevance
            const d = (b.year || 0) - (a.year || 0);
            return d !== 0 ? d : ((Number(b.relevance_score) || 0) - (Number(a.relevance_score) || 0));
        });
        return out;
    }

    // 仅重绘论文列表区（不重新拉接口），保持工具栏状态
    function rerenderPaperList() {
        const content = document.getElementById("content");
        if (!content) return;
        const listEl = content.querySelector(".list");
        const emptyEl = content.querySelector(".paper-filter-empty");
        const countEl = content.querySelector(".paper-filter-count");
        if (!listEl) return;
        // 重新读取当前缓存的 papers（从 DOM 之外保留一份更稳妥：这里直接用接口缓存）
        // renderPapers 内部维护的最新数据在 state.papersCache
        const papers = state.papersCache || [];
        const filtered = filterPapers(papers, state.papersFilter);
        listEl.innerHTML = "";
        filtered.forEach(p => listEl.appendChild(renderPaperItem(p)));
        if (countEl) countEl.textContent = `${filtered.length} / ${papers.length} 篇`;
        // 空结果提示挪到 toolbar 下方统一插入/移除
        const bar = content.querySelector(".paper-filter-bar");
        if (emptyEl) emptyEl.remove();
        if (!filtered.length && bar) {
            const tip = el("div", { class: "list-empty paper-filter-empty",
                text: "没有符合筛选条件的论文，试试放宽年份/期刊范围或清空搜索词。" });
            bar.after(tip);
        }
    }

    // ===== 未入库论文候选视图 =====

    async function renderUnlinkedPapers(content, unlinked, filterNote) {
        const rejected = unlinked.filter(p => p.reason === "score_rejected").length;
        const merged = unlinked.filter(p => p.reason === "dedup_merged").length;
        content.appendChild(el("div", { class: "page-header" }, [
            el("h2", { class: "page-title" }, "未入库论文"),
            el("p", { class: "page-desc" },
                `共 ${unlinked.length} 篇检索命中但未入库的候选论文，可手动补录入库。`),
            el("div", { class: "filter-stat-row" }, [
                el("span", { class: "badge badge-warning", text: `相关度<0.5 被筛选 ${rejected} 篇` }),
                el("span", { class: "badge badge-info", text: `与已入库重复去重 ${merged} 篇` }),
                el("span", { class: "badge badge-neutral", text: `总计未入库 ${unlinked.length} 篇` }),
            ]),
        ]));
        // 返回按钮
        content.appendChild(el("div", { class: "btn-row mt-0 mb-12" }, [
            el("button", {
                class: "btn btn-secondary btn-sm",
                onclick: () => { state.papersView = "all"; renderPage(); },
            }, "← 返回论文浏览"),
        ]));
        // 量化标准说明卡片（为什么这些命中没入库）
        if (filterNote) {
            content.appendChild(el("div", { class: "card filter-note-card" }, [
                el("div", { class: "filter-note-title" }, "为什么命中子问题却没入库？"),
                el("div", { class: "filter-note-body", text: filterNote }),
                el("div", { class: "filter-note-body" },
                    "下方每篇候选标注了具体原因：相关度不足（被打分剔除）或与已入库论文重复（去重合并）。"),
            ]));
        }
        if (!unlinked.length) {
            content.appendChild(el("div", { class: "list-empty" },
                "暂无未入库候选：当前检索命中的证据均已关联入库或被去重剔除"));
            return;
        }
        const list = el("div", { class: "list" });
        unlinked.forEach(p => list.appendChild(renderUnlinkedItem(p)));
        content.appendChild(list);
    }

    function renderUnlinkedItem(p) {
        const item = el("div", { class: "list-item unlinked-item" });
        const headChildren = [
            el("span", { class: "list-item-title", text: p.title || "(无标题)" }),
            el("span", { class: "badge badge-neutral", text: p.source || "?" }),
            el("span", { class: "badge badge-info",
                text: `命中 ${p.hit_count} 个子问题` }),
        ];
        // 未入库原因徽章
        if (p.reason === "dedup_merged") {
            headChildren.push(el("span", { class: "badge reason-badge reason-dedup",
                title: p.reason_detail || "", text: "未入库 · 与已入库重复" }));
        } else {
            headChildren.push(el("span", { class: "badge reason-badge reason-score",
                title: p.reason_detail || "", text: "未入库 · 相关度 < 0.5" }));
        }
        if (Number(p.evidence_score || 0) > 0) {
            headChildren.push(el("span", {
                class: "badge ev-src-badge ev-src-sciverse",
                text: `证据分 ${Number(p.evidence_score).toFixed(2)}`,
            }));
        }
        const head = el("div", { class: "list-item-head" }, headChildren);
        item.appendChild(head);
        if (p.snippet) {
            item.appendChild(el("div", { class: "unlinked-snippet" },
                (p.snippet || "").slice(0, 220)));
        }
        // 操作：查看证据片段详情（折叠展开）+ 入库按钮
        const actions = el("div", { class: "unlinked-actions" }, [
            el("span", { class: "unlinked-subq",
                text: `命中子问题：${p.subquery || "—"}` }),
            el("button", {
                class: "btn btn-primary btn-sm",
                onclick: (ev) => {
                    ev.stopPropagation();
                    importUnlinkedPaper(p);
                },
            }, "入库"),
        ]);
        item.appendChild(actions);
        // 点击展开详情
        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".unlinked-body")) {
                const body = el("div", { class: "unlinked-body" });
                body.innerHTML = `
                    <dl>
                        <dt>来源</dt><dd>${escapeHtml(p.source || "—")}</dd>
                        <dt>外部 ID</dt><dd class="mono">${escapeHtml(p.external_id || "—")}</dd>
                        <dt>未入库原因</dt><dd>${escapeHtml(p.reason_detail || p.reason || "—")}</dd>
                        <dt>证据片段</dt><dd>${escapeHtml(p.snippet || "（无片段）")}</dd>
                        <dt>命中子问题</dt><dd>${escapeHtml(p.subquery || "—")}</dd>
                    </dl>
                `;
                item.appendChild(body);
            }
        });
        return item;
    }

    async function importUnlinkedPaper(p) {
        try {
            const res = await api("POST", `/api/projects/${state.currentProjectId}/papers/import`, {
                external_id: p.external_id,
                title: p.title,
                snippet: p.snippet,
            });
            // 入库成功后重新拉取未入库列表刷新
            state.papersView = "unlinked";
            const fresh = await api("GET", `/api/projects/${state.currentProjectId}/unlinked-papers`)
                .catch(() => null);
            const unlinked = (fresh && fresh.papers) || [];
            const note = (fresh && fresh.filter_note) || "";
            const content = document.getElementById("content");
            clear(content);
            await renderUnlinkedPapers(content, unlinked, note);
            // 顶部提示
            const banner = el("div", { class: "status-banner success" },
                `${res.message || "入库成功"}（已返回未入库列表）`);
            content.insertBefore(banner, content.firstChild);
        } catch (e) {
            alert(`入库失败：${e.message || e}`);
        }
    }

    function renderEvidenceCard(evData) {
        const stats = (evData && evData.stats) || { total: 0, by_source: {}, linked: 0 };
        const entries = (evData && evData.entries) || [];
        const bySource = stats.by_source || {};
        // 区分检索 vs 手动补录：检索抓取 = total - manual；检索已入库 = linked - manual（近似，
        // manual 条目 paper_id 均已回填）；手动补录 = manual
        const manual = Number(stats.manual || 0);
        const retrieved = Number(stats.retrieved || Math.max((stats.total || 0) - manual, 0));
        const srcBadges = Object.keys(bySource)
            .filter(src => src !== "manual")
            .map(src =>
                el("span", { class: `badge ev-src-badge ev-src-${src}`, text: `${src} ${bySource[src]}` }));
        if (manual > 0) {
            srcBadges.push(el("span", { class: "badge ev-src-badge ev-src-manual", text: `手动补录 ${manual}` }));
        }

        const card = el("div", { class: "card ev-card" }, [
            el("div", { class: "ev-head" }, [
                el("span", { class: "ev-title" }, "检索证据链 · 审计轨迹"),
                el("span", { class: "badge badge-info", text: `检索抓取 ${retrieved} 条` }),
                el("span", { class: "badge badge-success", text: `检索已入库 ${Math.max(Number(stats.linked || 0) - manual, 0)} 条` }),
                el("span", { class: "badge badge-warning", text: `未入库 ${stats.unlinked || 0} 条` }),
                ...srcBadges,
            ]),
            el("div", { class: "ev-desc" },
                "审计轨迹：子问题 → 数据源 → 证据 → 是否入库。每条子问题按固定配额抓取" +
                "（Sciverse 10 + arXiv 3 + S2 2），因此各子问题条数相近；" +
                "「检索已入库」才是该子问题真正采纳的论文数，「未入库」为相关度<0.5 被筛选或与已入库重复" +
                "被去重剔除的候选（可在下方「未入库论文」中手动补录）。Sciverse 证据含 doc_id/offset，" +
                "可回读原文核验；关联依据见各条目的 match_type。" +
                (manual > 0
                    ? "另有 " + manual + " 条为手动补录/上传文献（不属检索，单独计数）。"
                    : "")),
        ]);

        if (!entries.length) {
            card.appendChild(el("div", { class: "ev-empty" },
                "暂无证据链记录：真实模式下运行 research 阶段后，检索调用自动落库"));
            return card;
        }

        // 按子问题分组展示
        const groups = {};
        entries.forEach(e => {
            const key = e.subquery || "(未标注子问题)";
            (groups[key] = groups[key] || []).push(e);
        });
        const body = el("div", { class: "ev-groups" });
        Object.keys(groups).forEach(sq => {
            const items = groups[sq];
            // 配额构成：同一子问题按数据源统计（Sciverse N + arXiv N），
            // 说明「命中条数」来自每条子问题的固定抓取配额，而非相关性筛剩下的数量。
            const bySrc = {};
            items.forEach(e => { bySrc[e.source] = (bySrc[e.source] || 0) + 1; });
            const srcParts = Object.keys(bySrc).map(s =>
                `${s === "sciverse" ? "Sciverse" : s} ${bySrc[s]}`);
            const linkedInSq = items.filter(e => e.paper_id).length;
            const group = el("div", { class: "ev-group" }, [
                el("div", { class: "ev-group-head" }, [
                    el("span", { class: "ev-group-q", text: sq }),
                    el("span", { class: "ev-group-count", text:
                        `抓取 ${items.length} 条（${srcParts.join(" + ")}）` }),
                    el("span", { class: "ev-group-linked", text:
                        `关联入库 ${linkedInSq} 条` }),
                ]),
            ]);
            const listEl = el("div", { class: "ev-group-body" });
            items.slice(0, 60).forEach(e => listEl.appendChild(renderEvidenceEntry(e)));
            group.appendChild(listEl);
            body.appendChild(group);
        });
        card.appendChild(body);
        return card;
    }

    function renderEvidenceEntry(e) {
        const src = e.source || "?";
        // 主行：来源徽章 + 标题（标题独立占满，允许完整换行，不再被元信息挤压/截断）
        const main = el("div", { class: "ev-entry-main" }, [
            el("span", { class: `badge ev-src-badge ev-src-${src}`, text: src }),
            el("span", { class: "ev-entry-title", text: e.title || "(无标题)" }),
        ]);
        // 元信息行：证据分 / doc_id / 偏移 / 关联依据 整齐排一行
        const meta = el("div", { class: "ev-entry-meta" });
        if (src === "sciverse") {
            meta.appendChild(el("span", { class: "ev-entry-score",
                text: `证据分 ${Number(e.evidence_score || 0).toFixed(2)}` }));
            meta.appendChild(el("span", { class: "mono ev-entry-id",
                text: `doc:${e.external_id || "-"}` }));
            if (Number(e.offset || 0) > 0) {
                meta.appendChild(el("span", { class: "ev-entry-offset",
                    text: `@偏移${e.offset}` }));
            }
        } else {
            const eid = e.external_id || "";
            if (eid) meta.appendChild(el("span", { class: "mono ev-entry-id", text: eid }));
        }
        // 关联依据（量化可审计）：match_type 说明为何关联该论文；
        // paper_id 为空 → 检索命中但未关联（被 filter 相关性筛选/去重剔除）。
        if (e.paper_id) {
            const reason = e.match_type || "证据来源关联";
            meta.appendChild(el("span", { class: "ev-entry-linked",
                text: `已入库 · ${reason}` }));
            if (Number(e.paper_relevance || 0) > 0) {
                meta.appendChild(el("span", { class: "ev-entry-rel",
                    text: `相关度 ${Number(e.paper_relevance).toFixed(2)}` }));
            }
        } else {
            meta.appendChild(el("span", { class: "ev-entry-unlinked",
                text: "未入库 · 被筛选/去重剔除" }));
        }
        return el("div", { class: "ev-entry" }, [main, meta]);
    }

    function renderPaperItem(p) {
        const item = el("div", { class: "list-item" });
        item.setAttribute("data-paper-id", p.paper_id || "");
        const headChildren = [
            el("span", { class: "list-item-title", text: p.title || "(无标题)" }),
            p.year ? el("span", { class: "badge badge-info badge-stage", text: String(p.year) }) : null,
            p.venue ? el("span", { class: "badge badge-neutral", text: p.venue }) : null,
        ];

        // 期刊影响因子徽章（IF > 0 才显示）
        const ifVal = Number(p.impact_factor) || 0;
        if (ifVal > 0) {
            const ifCls = ifVal >= 15 ? "badge-if-high" : (ifVal >= 8 ? "badge-if-mid" : "badge-if-low");
            headChildren.push(el("span", {
                class: `badge ${ifCls}`,
                title: p.cas_subcategory || "",
                text: `IF ${ifVal.toFixed(1)}`,
            }));
        }
        // 中科院分区标签
        if (p.cas_zone) {
            const zoneCls = p.cas_zone === "1" ? "badge-cas-1" : (p.cas_zone === "2" ? "badge-cas-2" : "badge-cas-low");
            const zoneText = p.is_top_journal ? `${p.cas_zone}区 Top` : `${p.cas_zone}区`;
            headChildren.push(el("span", {
                class: `badge ${zoneCls}`,
                title: p.cas_subcategory || "",
                text: zoneText,
            }));
        }

        // 数据源徽章（Sciverse 为赛题推荐主源，标注证据分）
        if (p.source === "sciverse") {
            headChildren.push(el("span", {
                class: "badge ev-src-badge ev-src-sciverse",
                text: `SC ${Number(p.evidence_score || 0).toFixed(2)}`,
            }));
        } else if (p.source) {
            headChildren.push(el("span", {
                class: `badge ev-src-badge ev-src-${p.source}`,
                text: p.source,
            }));
        }
        // 主题相关度徽章（filter 阶段量化打分 0~1，可点击展开查看依据）
        if (Number(p.relevance_score || 0) > 0) {
            const rs = Number(p.relevance_score);
            const cls = rs >= 0.7 ? "ev-entry-rel" : (rs >= 0.5 ? "ev-entry-rel-mid" : "ev-entry-rel-low");
            headChildren.push(el("span", {
                class: `badge ${cls}`,
                title: p.relevance_reason || "",
                text: `相关度 ${rs.toFixed(2)}`,
            }));
        }
        headChildren.push(el("span", { class: "list-item-meta", text:
            (p.authors || []).slice(0, 2).join(", ") + ((p.authors || []).length > 2 ? " et al." : "") }));

        // PDF 一键下载按钮（真实可下载才显示：arxiv 直链 或 非 doi.org 兜底的 pdf_url）
        const pdfUrl = p.pdf_url || (p.arxiv_id ? `https://arxiv.org/pdf/${p.arxiv_id}.pdf` : "");
        const isDoiFallback = pdfUrl && pdfUrl.includes("doi.org") && !p.arxiv_id;
        if (pdfUrl && !isDoiFallback) {
            const pdfBtn = el("button", {
                class: "btn btn-sm btn-pdf-download",
                title: "下载 PDF（后端代理保存到本地）",
                text: "PDF ↓",
            });
            pdfBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                pdfBtn.disabled = true;
                pdfBtn.textContent = "下载中…";
                try {
                    await downloadPaperPdf(state.currentProjectId, p.paper_id);
                } catch (err) {
                    alert(`PDF 下载失败：${err.message || err}`);
                } finally {
                    pdfBtn.disabled = false;
                    pdfBtn.textContent = "PDF ↓";
                }
            });
            headChildren.push(pdfBtn);
        } else if (pdfUrl && isDoiFallback && p.doi_url) {
            // 仅 doi.org 兜底：不显示下载按钮（大概率付费墙），提示查看原文
            headChildren.push(el("span", {
                class: "badge badge-warning badge-pdf-locked",
                title: "该论文仅能访问出版商文章页（可能付费墙），请点击原文链接查看",
                text: "原文可读",
            }));
        }

        const head = el("div", { class: "list-item-head" }, headChildren);
        item.appendChild(head);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                const body = buildPaperBody(p);
                // 展开详情里的「下载 PDF」链接绑定异步下载
                const dlLink = body.querySelector(".js-pdf-download");
                if (dlLink) {
                    dlLink.addEventListener("click", async (ev) => {
                        ev.preventDefault();
                        ev.stopPropagation();
                        try {
                            await downloadPaperPdf(state.currentProjectId, p.paper_id);
                        } catch (err) {
                            alert(`PDF 下载失败：${err.message || err}`);
                        }
                    });
                }
                item.appendChild(body);
            }
        });
        return item;
    }

    // 异步下载论文 PDF（fetch 二进制 → blob → 触发浏览器下载）
    async function downloadPaperPdf(projectId, paperId) {
        const url = `/api/projects/${projectId}/papers/${paperId}/pdf`;
        const resp = await fetch(url);
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try {
                const data = await resp.json();
                if (data && data.detail) msg = data.detail;
            } catch (e) { /* ignore */ }
            throw new Error(msg);
        }
        const blob = await resp.blob();
        // 触发浏览器下载（用后端返回的文件名，兜底用 paperId）
        const cd = resp.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
        const fname = m ? decodeURIComponent(m[1]) : `${paperId}.pdf`;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            URL.revokeObjectURL(a.href);
            a.remove();
        }, 1000);
    }

    // ===== 4. Claim 页 =====

    let claimsFilter = "all";

    async function renderClaims(content) {
        content.appendChild(el("div", { class: "loading" }, "加载中…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/claims`);
            clear(content);
            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "Claim 列表"),
                el("p", { class: "page-desc" }, `共 ${data.claims.length} 条 Claim。`),
            ]));

            // 筛选条
            const statuses = ["all", "draft", "evidence_linked", "verified", "refuted", "superseded"];
            const bar = el("div", { class: "filter-bar" });
            bar.appendChild(el("span", { class: "filter-label", text: "状态筛选：" }));
            statuses.forEach(s => {
                const chip = el("span", {
                    class: "filter-chip" + (claimsFilter === s ? " active" : ""),
                    text: s === "all" ? "全部" : s,
                    onclick: () => {
                        claimsFilter = s;
                        renderPage();
                    },
                });
                bar.appendChild(chip);
            });
            content.appendChild(bar);

            const filtered = claimsFilter === "all"
                ? data.claims
                : data.claims.filter(c => c.status === claimsFilter);

            if (!filtered.length) {
                content.appendChild(el("div", { class: "list-empty" }, "无符合条件的 Claim"));
                return;
            }
            const list = el("div", { class: "list" });
            filtered.forEach(c => list.appendChild(renderClaimItem(c)));
            content.appendChild(list);
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // 可复用的证据列表构建器：返回 DOM 节点
    // refs: [{type:"paper"/"experiment", id:"...", chunk_id?:"..."}, ...] 或 [string, ...]
    function buildEvidenceList(refs) {
        const evList = el("div", { class: "evidence-list" });
        (refs || []).forEach(ref => {
            const refObj = typeof ref === "string" ? { id: ref, type: "paper" } : ref;
            const refType = refObj.type || "paper";
            const refId = refObj.id || refObj.paper_id || "?";
            const refChunk = refObj.chunk_id || "";
            const typeIcon = refType === "paper" ? "📄" : refType === "experiment" ? "🔬" : "📌";
            const evItem = el("div", { class: "evidence-item" });
            evItem.appendChild(el("span", { class: "evidence-icon", text: typeIcon }));
            evItem.appendChild(el("span", { class: "evidence-type", text: refType }));
            if (refType === "paper") {
                // 优先展示论文标题（可读），id 作为副信息
                const refTitle = refObj.title || refId;
                const link = el("a", {
                    class: "evidence-link",
                    text: refTitle,
                    title: `点击跳转到论文浏览页 (${refId})`,
                    onclick: (e) => {
                        e.stopPropagation();
                        state.pendingPaperId = refId;
                        setActivePage("papers");
                    },
                });
                link.style.cursor = "pointer";
                link.style.color = "var(--color-primary)";
                link.style.textDecoration = "underline";
                evItem.appendChild(link);
                if (refObj.title && refTitle !== refId) {
                    evItem.appendChild(el("span", {
                        class: "evidence-id small muted mono",
                        text: refId,
                    }));
                }
                // 溯源片段（可悬停查看）
                if (refObj.snippet) {
                    const snip = el("span", {
                        class: "evidence-snippet small muted",
                        text: `「${refObj.snippet.slice(0, 120)}${refObj.snippet.length > 120 ? "…" : ""}」`,
                    });
                    evItem.appendChild(snip);
                }
            } else if (refType === "experiment") {
                const link = el("a", {
                    class: "evidence-link",
                    text: refId,
                    title: "点击跳转到实验列表",
                    onclick: (e) => {
                        e.stopPropagation();
                        setActivePage("experiments");
                    },
                });
                link.style.cursor = "pointer";
                link.style.color = "var(--color-primary)";
                link.style.textDecoration = "underline";
                evItem.appendChild(link);
            } else {
                evItem.appendChild(el("span", { class: "evidence-id", text: refId }));
            }
            if (refChunk) {
                evItem.appendChild(el("span", { class: "evidence-chunk small muted", text: `chunk: ${refChunk}` }));
            }
            evList.appendChild(evItem);
        });
        return evList;
    }

    function renderClaimItem(c) {
        const item = el("div", { class: "list-item" });
        item.appendChild(el("div", { class: "list-item-head" }, [
            el("span", { class: "list-item-title", text: c.statement || "(无陈述)" }),
            el("span", { class: "badge badge-info", text: c.role || "contribution" }),
            c.evidence_count > 0
                ? el("span", { class: "badge badge-neutral", text: `证据 ${c.evidence_count}` })
                : el("span", { class: "badge badge-warning", text: "证据 0 · 待验证" }),
        ]));
        // 状态徽章单独一行（便于颜色识别）
        item.appendChild(el("div", { class: "mt-8" }));
        item.insertAdjacentHTML("beforeend", `<div class="mt-8">${statusBadge(c.status)}</div>`);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                item.appendChild(buildClaimBody(c));
            }
        });
        return item;
    }

    // Claim 详情正文：陈述 + 角色 + 证据链 + 状态时间（可点击溯源）
    function buildClaimBody(c) {
        const body = el("div", { class: "list-item-body" });
        const refs = c.evidence_refs || [];
        // 关联冲突提示（Claim 处于争议中）
        if (c.conflicts && c.conflicts.length) {
            body.appendChild(el("div", { class: "status-banner warning mt-8" },
                `该 Claim 引用文献存在 ${c.conflicts.length} 处冲突结论（争议中）`));
        }
        body.innerHTML = `
            <dl>
                <dt>Claim ID</dt><dd class="mono">${escapeHtml(c.claim_id || "—")}</dd>
                <dt>角色</dt><dd>${escapeHtml((c.role || "contribution").replace("_", " "))}</dd>
                <dt>状态</dt><dd>${escapeHtml((c.status || "draft").replace("_", " "))}</dd>
                <dt>来源阶段</dt><dd>${escapeHtml(c.source_stage || "—")}</dd>
                <dt>创建时间</dt><dd class="mono">${escapeHtml(formatTime(c.created_at) || "—")}</dd>
                ${c.verified_at ? `<dt>验证时间</dt><dd class="mono">${escapeHtml(formatTime(c.verified_at))}</dd>` : ""}
            </dl>
        `;
        // 证据链（可点击跳转论文/实验）
        if (refs.length) {
            const evDiv = el("div", { class: "evidence-chain mt-8" });
            evDiv.appendChild(el("div", { class: "small evidence-title" }, "证据溯源链："));
            evDiv.appendChild(buildEvidenceList(refs));
            body.appendChild(evDiv);
        } else {
            body.appendChild(el("div", { class: "small muted mt-8" },
                "暂无证据：该 Claim 为 draft 状态，等待实验验证后回填证据。"));
        }
        return body;
    }

    function buildPaperBody(p) {
        const body = el("div", { class: "list-item-body" });
        // PDF 下载链接（真实可下载才生成下载链接；仅 doi.org 兜底则提示原文）
        const pdfUrl = p.pdf_url || (p.arxiv_id ? `https://arxiv.org/pdf/${p.arxiv_id}.pdf` : "");
        const isDoiFallback = pdfUrl && pdfUrl.includes("doi.org") && !p.arxiv_id;
        let linksHtml = "";
        if (p.url) {
            linksHtml += `<a href="${escapeHtml(p.url)}" target="_blank" class="link-external">原文链接 ↗</a>`;
        }
        if (p.doi_url) {
            linksHtml += ` <a href="${escapeHtml(p.doi_url)}" target="_blank" class="link-external">DOI ↗</a>`;
        }
        if (pdfUrl && !isDoiFallback) {
            linksHtml += ` <a href="javascript:void(0)" class="link-external js-pdf-download" data-paper-id="${escapeHtml(p.paper_id)}">下载 PDF ↗</a>`;
        }
        if (p.pdf_path) {
            linksHtml += ` <span class="badge badge-success">本地 PDF 已上传</span>`;
        }
        if (!linksHtml) linksHtml = '<span class="muted">无可用链接</span>';
        // 期刊质量信息行
        let qualityHtml = "";
        const ifVal = Number(p.impact_factor) || 0;
        if (ifVal > 0 || p.cas_zone) {
            qualityHtml = `<dt>期刊质量</dt><dd>`;
            if (ifVal > 0) qualityHtml += `影响因子 <strong>${ifVal.toFixed(1)}</strong>`;
            if (p.cas_zone) qualityHtml += ` · 中科院 ${p.cas_zone}区${p.is_top_journal ? " Top" : ""}`;
            if (p.cas_subcategory) qualityHtml += `（${escapeHtml(p.cas_subcategory)}）`;
            qualityHtml += `</dd>`;
        }
        body.innerHTML = `
                    <dl>
                        <dt>Paper ID</dt><dd class="mono">${escapeHtml(p.paper_id)}</dd>
                        <dt>作者</dt><dd>${escapeHtml((p.authors || []).join(", ") || "—")}</dd>
                        <dt>年份</dt><dd>${p.year || "—"}</dd>
                        <dt>会议/期刊</dt><dd>${escapeHtml(p.venue || "—")}</dd>
                        ${qualityHtml}
                        <dt>arXiv ID</dt><dd class="mono">${escapeHtml(p.arxiv_id || "—")}</dd>
                        <dt>DOI</dt><dd class="mono">${escapeHtml(p.doi || "—")}</dd>
                        <dt>链接</dt><dd>${linksHtml}</dd>
                        <dt>来源阶段</dt><dd>${escapeHtml(p.source_stage || "—")}</dd>
                        <dt>数据源</dt><dd>${escapeHtml(p.source || "—")}${p.source_subquery ? `（命中子问题：${escapeHtml(p.source_subquery)}）` : ""}</dd>
                        ${p.doc_id ? `<dt>Sciverse doc_id</dt><dd class="mono">${escapeHtml(p.doc_id)}${p.offset ? ` @偏移${p.offset}` : ""}</dd>` : ""}
                        ${p.evidence_score ? `<dt>Sciverse 证据分</dt><dd>${escapeHtml(String(Number(p.evidence_score).toFixed(3)))}</dd>` : ""}
                        ${p.relevance_score ? `<dt>主题相关度</dt><dd>${escapeHtml(String(Number(p.relevance_score).toFixed(2)))}${p.relevance_reason ? `（${escapeHtml(p.relevance_reason)}）` : ""}</dd>` : ""}
                        <dt>入库时间</dt><dd class="mono">${escapeHtml(formatTime(p.created_at))}</dd>
                    </dl>
                    ${p.abstract ? `<div class="mt-8"><strong>摘要：</strong><br>${escapeHtml(p.abstract)}</div>` : ""}
                `;
        return body;
    }

    // ===== 3.5 材料知识页（Task 2：材料-性能-合成三元组；Task 3：搜索跳转）=====

    // 材料页过滤状态（搜索词 + 体系筛选）
    let materialsState = { query: "", catFilter: "", lastQuery: "" };
    const MAT_CAT_CLASSES = ["mat-cat-c0", "mat-cat-c1", "mat-cat-c2", "mat-cat-c3",
        "mat-cat-c4", "mat-cat-c5", "mat-cat-c6", "mat-cat-c7"];
    function matCatClass(cat) {
        let h = 0;
        for (const ch of String(cat)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
        return MAT_CAT_CLASSES[h % MAT_CAT_CLASSES.length];
    }
    // 性能类别展示顺序（与后端 normalize.PROPERTY_CATEGORIES 一致）+ 配色
    const PROP_CAT_ORDER = ["热电优值", "电输运", "热输运", "载流子", "能带结构", "稳定性", "器件性能", "其他"];
    const PROP_CAT_COLORS = {
        "热电优值": "#4a9eff", "电输运": "#f5a623", "热输运": "#e8643a",
        "载流子": "#7a3eb1", "能带结构": "#1e8e6e", "稳定性": "#b3511a",
        "器件性能": "#33518f", "其他": "#8a8f98",
    };

    function groupByCat(list, getCat) {
        const groups = {};
        list.forEach(x => {
            const c = getCat(x) || "其他";
            (groups[c] = groups[c] || []).push(x);
        });
        return groups;
    }

    // 材料是否命中搜索词（材料名/化学式/体系/性能/合成方法/条件）
    function materialMatches(m, q) {
        if (!q) return true;
        q = q.toLowerCase();
        const parts = [m.name, m.formula, m.category];
        (m.properties || []).forEach(p => {
            parts.push(p.property_name, p.property_name_cn, p.norm_key, p.norm_cn, p.symbol, p.value, p.condition);
        });
        (m.synthesis || []).forEach(s => {
            parts.push(s.method, s.method_category, s.method_label, s.temperature, s.pressure, s.atmosphere, s.duration);
            if (s.precursors && s.precursors.length) parts.push(s.precursors.join(" "));
        });
        return parts.filter(Boolean).join(" ").toLowerCase().includes(q);
    }

    // 命中高亮：把搜索词在文本中加 <mark>
    function highlightMatch(text, q) {
        if (!q || !text) return escapeHtml(text);
        const esc = escapeHtml(text);
        const escQ = escapeHtml(q);
        try {
            const re = new RegExp(`(${escQ.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
            return esc.replace(re, "<mark class='mat-hl'>$1</mark>");
        } catch (e) {
            return esc;
        }
    }

    function propEntryHtml(p, q) {
        const sym = p.symbol ? `<span class="mat-sym">${escapeHtml(p.symbol)}</span>` : "";
        const stdName = p.norm_cn && p.norm_cn !== "其他" ? p.norm_cn : "";
        const orig = p.property_name || "";
        const nameBits = [];
        if (stdName) {
            nameBits.push(highlightMatch(stdName, q));
            if (orig && orig.toLowerCase() !== stdName.toLowerCase()) {
                nameBits.push(`<span class="muted small">原文 ${highlightMatch(orig, q)}</span>`);
            }
        } else {
            nameBits.push(highlightMatch(orig, q) || "未命名指标");
        }
        const val = (p.value ? highlightMatch(p.value, q) : "<span class='muted'>—</span>") + (p.unit ? ` ${escapeHtml(p.unit)}` : "");
        const cond = p.condition ? `<span class="mat-entry-cond">@ ${highlightMatch(p.condition, q)}</span>` : "";
        const src = p.paper_title
            ? `<span class="mat-src-tag" title="${escapeHtml(p.paper_title)}">${escapeHtml(String(p.paper_title).slice(0, 42))}${String(p.paper_title).length > 42 ? "…" : ""}</span>`
            : "";
        return `<div class="mat-entry"><span class="mat-entry-name">${sym}${nameBits.join(" ")}</span><span class="mat-entry-val">${val}</span>${cond}${src}</div>`;
    }

    function synEntryHtml(s, q) {
        const chip = s.method_label && s.method_label !== "其他工艺"
            ? `<span class="mat-method-chip">${highlightMatch(s.method_label, q)}</span>` : "";
        const orig = s.method && s.method !== s.method_label
            ? `<span class="muted small">${highlightMatch(s.method, q)}</span>` : "";
        const conds = [];
        if (s.temperature) conds.push(`温度 ${escapeHtml(s.temperature)}`);
        if (s.pressure) conds.push(`压力 ${escapeHtml(s.pressure)}`);
        if (s.atmosphere) conds.push(`气氛 ${escapeHtml(s.atmosphere)}`);
        if (s.duration) conds.push(`时长 ${escapeHtml(s.duration)}`);
        if (s.precursors && s.precursors.length) conds.push(`前驱体 ${escapeHtml(s.precursors.join(", "))}`);
        const condHtml = conds.length ? `<span class="mat-entry-cond">${conds.join(" · ")}</span>` : "";
        const src = s.paper_title
            ? `<span class="mat-src-tag" title="${escapeHtml(s.paper_title)}">${escapeHtml(String(s.paper_title).slice(0, 42))}${String(s.paper_title).length > 42 ? "…" : ""}</span>`
            : "";
        return `<div class="mat-entry">${chip}${orig}${condHtml}${src}</div>`;
    }

    function buildMaterialCard(m, idx, q) {
        const item = el("div", { class: "mat-card", "data-mat-id": m.material_id || "" });
        const head = el("div", { class: "mat-card-head" }, [
            el("span", { class: "mat-idx", text: String(idx + 1) }),
            el("span", { class: "mat-name", html: highlightMatch(m.name || "未命名材料", q) }),
            m.formula ? el("span", { class: "badge badge-formula", text: m.formula }) : null,
            m.category && m.category !== "其他" ?
                el("span", { class: `badge mat-cat-badge ${matCatClass(m.category)}`, text: m.category }) : null,
            el("span", { class: "badge badge-neutral", text: `置信度 ${Number(m.confidence || 0).toFixed(2)}` }),
            (m.source_paper_ids && m.source_paper_ids.length) ?
                el("span", { class: "badge badge-s2", text: `跨文献 ${m.source_paper_ids.length + 1} 篇` }) : null,
            el("span", { class: "mat-counts" }, [
                el("span", { class: "mat-count", text: `性能 ${(m.properties || []).length}` }),
                el("span", { class: "mat-count", text: `合成 ${(m.synthesis || []).length}` }),
            ]),
            el("span", { class: "mat-card-actions" }, [
                el("button", {
                    class: "btn btn-accent btn-sm mat-profile-btn",
                    onclick: () => { state.profileMaterialId = m.material_id; state.profileMaterialName = m.name; setActivePage("material-profile"); },
                }, "深度画像"),
                el("button", {
                    class: "btn btn-secondary btn-sm mat-profile-btn",
                    onclick: () => { state.profileMaterialId = m.material_id; state.profileMaterialName = m.name; setActivePage("synthesis-routes"); },
                }, "合成路线"),
            ]),
        ]);
        item.appendChild(head);

        // 结构/组成
        const structBits = [];
        if (m.crystal_structure) structBits.push(`结构 ${m.crystal_structure}`);
        if (m.space_group) structBits.push(`空间群 ${m.space_group}`);
        if (m.lattice_parameters) structBits.push(`晶格 ${m.lattice_parameters}`);
        if (m.symmetry) structBits.push(`对称 ${m.symmetry}`);
        if (m.composition) structBits.push(`组成 ${m.composition}`);
        if (structBits.length) {
            item.insertAdjacentHTML("beforeend",
                `<div class="mat-struct">${structBits.map(escapeHtml).join(" · ")}</div>`);
        }

        // 性能指标（按类别分组展示；未知类别兜底追加）
        const props = m.properties || [];
        if (props.length) {
            const groups = groupByCat(props, p => p.category || "其他");
            const known = PROP_CAT_ORDER.filter(c => groups[c]);
            const extraCats = Object.keys(groups).filter(c => !PROP_CAT_ORDER.includes(c)).sort();
            const ordered = known.concat(extraCats.map(c => groups[c] ? c : null).filter(Boolean));
            const gHtml = ordered.map(c => {
                const color = PROP_CAT_COLORS[c] || "#8a8f98";
                return `
                    <div class="mat-group" style="--cat-color:${color}">
                        <div class="mat-group-head">
                            <span class="mat-group-title"><span class="mat-cat-dot"></span>${escapeHtml(c)}</span>
                            <span class="mat-group-count">${groups[c].length} 项</span>
                        </div>
                        <div class="mat-group-body">${groups[c].map(p => propEntryHtml(p, q)).join("")}</div>
                    </div>`;
            }).join("");
            item.insertAdjacentHTML("beforeend", `
                <div class="mat-section">
                    <div class="mat-section-title">性能指标 <span class="mat-section-sub">（${props.length} 条 · 已归一化为标准指标）</span></div>
                    <div class="mat-group-list">${gHtml}</div>
                </div>`);
        } else {
            item.insertAdjacentHTML("beforeend",
                `<div class="mat-empty">暂无性能记录（该论文未抽取到量化指标）</div>`);
        }

        // 合成方法（按工艺类别分组展示）
        const syns = m.synthesis || [];
        if (syns.length) {
            const groups = groupByCat(syns, s => s.method_category || "其他");
            const gHtml = Object.keys(groups).sort().map(c => `
                <div class="mat-group" style="--cat-color:#33518f">
                    <div class="mat-group-head">
                        <span class="mat-group-title"><span class="mat-cat-dot"></span>${escapeHtml(c)}</span>
                        <span class="mat-group-count">${groups[c].length} 项</span>
                    </div>
                    <div class="mat-group-body">${groups[c].map(s => synEntryHtml(s, q)).join("")}</div>
                </div>`).join("");
            item.insertAdjacentHTML("beforeend", `
                <div class="mat-section">
                    <div class="mat-section-title">合成方法 <span class="mat-section-sub">（${syns.length} 条 · 已按工艺类别归类）</span></div>
                    <div class="mat-group-list">${gHtml}</div>
                </div>`);
        } else {
            item.insertAdjacentHTML("beforeend",
                `<div class="mat-empty">暂无合成记录（论文未提供工艺条件）</div>`);
        }

        // 关联材料（会议纪要：给材料"牵线搭桥"——同体系/共性能/共用方法/同论文）
        const rels = m.related_materials || [];
        if (rels.length) {
            const relMeta = {
                same_system: { label: "同体系", cls: "mat-rel-system" },
                same_property: { label: "共性能", cls: "mat-rel-property" },
                same_method: { label: "同方法", cls: "mat-rel-method" },
                same_paper: { label: "同论文", cls: "mat-rel-paper" },
            };
            const relHtml = rels.map(r => {
                const meta = relMeta[r.relation] || { label: r.relation, cls: "mat-rel-other" };
                return `
                    <span class="mat-rel-chip" data-mat-rel-id="${escapeHtml(r.material_id)}" title="${escapeHtml(r.reason || "")}">
                        <span class="mat-rel-tag ${meta.cls}">${meta.label}</span>
                        <span class="mat-rel-name">${escapeHtml(r.name || r.formula || "材料")}</span>
                    </span>`;
            }).join("");
            item.insertAdjacentHTML("beforeend", `
                <div class="mat-section mat-rel-section">
                    <div class="mat-section-title">关联材料 <span class="mat-section-sub">（${rels.length} 条 · 同体系 / 共性能指标 / 共用合成方法 / 同来源论文）</span></div>
                    <div class="mat-rel-list">${relHtml}</div>
                </div>`);
        }

        item.insertAdjacentHTML("beforeend",
            `<div class="mat-src">来源论文：${escapeHtml(m.paper_title || "—")}</div>`);

        // 关联材料 chip 点击 → 滚动定位并高亮目标材料卡片
        item.querySelectorAll(".mat-rel-chip").forEach(chip => {
            chip.addEventListener("click", (e) => {
                e.stopPropagation();
                const targetId = chip.getAttribute("data-mat-rel-id");
                const targetCard = document.querySelector(`.mat-card[data-mat-id="${targetId}"]`);
                if (targetCard) {
                    targetCard.classList.add("mat-flash");
                    targetCard.scrollIntoView({ behavior: "smooth", block: "start" });
                    setTimeout(() => targetCard.classList.remove("mat-flash"), 3400);
                } else {
                    showToast("目标材料不在当前筛选结果中，请调整筛选后查看", "info");
                }
            });
        });
        return item;
    }

    async function renderMaterials(content) {
        content.appendChild(el("div", { class: "loading" }, "加载材料知识库…"));
        let data;
        try {
            data = await api("GET", `/api/projects/${state.currentProjectId}/materials`);
        } catch (e) {
            content.appendChild(el("div", { class: "status-banner danger" },
                "加载失败：" + (e.message || e)));
            return;
        }
        clear(content);
        const mats = data.materials || [];
        const stats = data.stats || {};
        const agg = data.aggregation || {};

        content.appendChild(el("h2", { class: "page-title" }, "材料知识库"));
        content.appendChild(el("p", { class: "page-desc" },
            `共 ${mats.length} 种材料 · ${stats.properties || 0} 条性能 · ${stats.synthesis || 0} 条合成方法 · 完整三元组 ${stats.complete_triples || 0} 条。` +
            `由 research 阶段「材料知识抽取」节点从论文摘要中自动抽取，指标 / 工艺 / 体系均已按材料科学标准归一化。`));

        // 搜索条：输入框 + 命中计数 + 定位 / 重置
        const searchBar = el("div", { class: "mat-search-bar" });
        const input = el("input", {
            class: "mat-search-input",
            type: "text",
            placeholder: "搜索材料名 / 化学式 / 体系 / 性能（如 ZT、热导率、功率因子）/ 合成方法…",
            value: materialsState.query,
            oninput: (e) => {
                materialsState.query = e.target.value;
                renderMaterialList();
            },
            onkeydown: (e) => {
                if (e.key === "Enter") { e.preventDefault(); jumpToMatch(); }
                if (e.key === "Escape") { materialsState.query = ""; input.value = ""; renderMaterialList(); }
            },
        });
        const searchIcon = el("span", { class: "mat-search-icon" }, "⌕");
        searchBar.appendChild(searchIcon);
        searchBar.appendChild(input);
        const hitBadge = el("span", { class: "mat-hit-badge" });
        searchBar.appendChild(hitBadge);
        searchBar.appendChild(el("button", {
            class: "btn btn-accent btn-sm mat-search-btn",
            onclick: jumpToMatch,
        }, "定位"));
        searchBar.appendChild(el("button", {
            class: "btn btn-secondary btn-sm mat-search-btn",
            onclick: () => {
                materialsState.query = "";
                materialsState.catFilter = "";
                renderMaterials(content);
            },
        }, "重置"));
        content.appendChild(searchBar);

        // 统计卡片
        const statCard = el("div", { class: "card" });
        const grid = el("div", { class: "counts-grid" });
        [
            { label: "材料", value: mats.length, extra: "去重合并后" },
            { label: "性能指标", value: stats.properties || 0, extra: "ZT/热导率等" },
            { label: "合成方法", value: stats.synthesis || 0, extra: "工艺+条件" },
            { label: "完整三元组", value: stats.complete_triples || 0, extra: "材料+性能+合成" },
        ].forEach(c => grid.appendChild(el("div", { class: "count-card" }, [
            el("div", { class: "count-num", text: String(c.value) }),
            el("div", { class: "count-label", text: c.label }),
            el("div", { class: "count-extra", text: c.extra }),
        ])));
        statCard.appendChild(grid);
        content.appendChild(statCard);

        // 知识结构总览（材料体系 chips 可点击筛选；性能/工艺为统计展示，超出折叠）
        const aggCard = el("div", { class: "card" });
        aggCard.appendChild(el("div", { class: "card-title" }, "知识结构总览"));
        const rows = [
            { label: "材料体系", counters: agg.material_categories || {}, clickable: true },
            { label: "性能类别", counters: agg.property_categories || {}, clickable: false },
            { label: "合成工艺", counters: agg.method_categories || {}, clickable: false },
        ];
        rows.forEach(r => {
            const row = el("div", { class: "mat-agg-row" }, [el("span", { class: "mat-agg-label", text: r.label })]);
            const entries = Object.entries(r.counters).sort((a, b) => b[1] - a[1]);
            if (!entries.length) {
                row.appendChild(el("span", { class: "muted small" }, "暂无数据"));
            } else {
                const MAX_CHIPS = 10;
                const shown = entries.slice(0, MAX_CHIPS);
                shown.forEach(([cat, n]) => {
                    const active = r.clickable && materialsState.catFilter === cat;
                    const chip = el("span", {
                        class: "mat-agg-chip" + (active ? " active" : "") +
                            (r.clickable ? ` mat-cat-badge ${matCatClass(cat)}` : ""),
                    });
                    if (r.clickable) {
                        chip.setAttribute("data-clickable", "1");
                        chip.setAttribute("data-cat", cat);
                        chip.addEventListener("click", () => {
                            materialsState.catFilter = materialsState.catFilter === cat ? "" : cat;
                            // 同步更新所有体系 chip 的 active 状态
                            aggCard.querySelectorAll('.mat-agg-chip[data-clickable="1"]').forEach(c => {
                                c.classList.toggle("active", c.getAttribute("data-cat") === materialsState.catFilter);
                            });
                            renderMaterialList();
                        });
                    }
                    chip.appendChild(document.createTextNode(cat));
                    chip.appendChild(el("span", { class: "mat-agg-num", text: String(n) }));
                    row.appendChild(chip);
                });
                if (entries.length > MAX_CHIPS) {
                    row.appendChild(el("span", { class: "mat-agg-more" },
                        `… 等 ${entries.length - MAX_CHIPS} 类`));
                }
            }
            aggCard.appendChild(row);
        });
        content.appendChild(aggCard);

        if (!mats.length) {
            content.appendChild(el("div", { class: "list-empty" },
                "暂无材料知识：运行 research 阶段后，材料抽取节点自动从入库论文中抽取材料-性能-合成三元组。"));
            return;
        }

        // 列表容器 + 渲染
        const listWrap = el("div", { id: "mat-list-wrap" });
        content.appendChild(listWrap);

        function renderMaterialList() {
            const q = materialsState.query.trim();
            const cf = materialsState.catFilter;
            const filtered = mats.filter(m => materialMatches(m, q) && (!cf || m.category === cf));
            clear(listWrap);
            // 命中计数
            hitBadge.textContent = q || cf
                ? `命中 ${filtered.length} / ${mats.length} 种材料`
                : `共 ${mats.length} 种材料`;
            if (!filtered.length) {
                listWrap.appendChild(el("div", { class: "list-empty" },
                    "没有匹配的材料，换个关键词试试（支持材料名 / 化学式 / 体系 / 性能 / 合成方法）。"));
                return;
            }
            listWrap.appendChild(el("div", { class: "mat-list-summary" },
                `共 ${filtered.length} 种材料${q ? ` · 匹配「${q}」` : ""}${cf ? ` · 体系「${cf}」` : ""}` +
                ` · 其中 ${filtered.filter(m => (m.properties || []).length).length} 种含性能、${filtered.filter(m => (m.synthesis || []).length).length} 种含合成`));
            filtered.forEach((m, i) => listWrap.appendChild(buildMaterialCard(m, i, q)));
        }

        function jumpToMatch() {
            const q = materialsState.query.trim();
            materialsState.lastQuery = q;
            if (!q) {
                showToast("请先输入要搜索的关键词", "error");
                input.focus();
                return;
            }
            renderMaterialList();
            const card = listWrap.querySelector(".mat-card");
            if (card) {
                card.classList.add("mat-flash");
                card.scrollIntoView({ behavior: "smooth", block: "start" });
                setTimeout(() => card.classList.remove("mat-flash"), 3400);
                showToast(`已定位到「${q}」的首个匹配`, "success");
            } else {
                showToast("未找到匹配材料", "error");
            }
        }

        renderMaterialList();
    }

    // ===== 3.6 深度分析页（材料画像 + 合成路线；P6）=====

    // 证据等级徽章（A/B/C/D/E）
    const EVIDENCE_LEVEL_META = {
        A: { label: "多篇实验", desc: "多篇实验论文直接验证", cls: "ev-badge-A" },
        B: { label: "单篇实验", desc: "单篇实验论文直接验证", cls: "ev-badge-B" },
        C: { label: "多篇间接", desc: "多个文献间接支持", cls: "ev-badge-C" },
        D: { label: "理论/库", desc: "理论/数据库预测（MP/DFT）", cls: "ev-badge-D" },
        E: { label: "LLM推断", desc: "LLM 推断（非文献原始数据，仅供实验设计参考）", cls: "ev-badge-E" },
    };
    // 六大性质维度展示元数据
    const DIMENSION_ORDER = ["structure", "electronic", "thermal", "optical", "mechanical", "chemical_stability", "performance", "other"];
    const DIMENSION_META = {
        structure: { label: "基础结构", color: "#4a9eff" },
        electronic: { label: "电子性质", color: "#f5a623" },
        thermal: { label: "热学性质", color: "#e8643a" },
        optical: { label: "光学性质", color: "#1e8e6e" },
        mechanical: { label: "力学性质", color: "#7a3eb1" },
        chemical_stability: { label: "化学稳定性", color: "#b3511a" },
        performance: { label: "目标性能", color: "#33518f" },
        other: { label: "其他", color: "#8a8f98" },
    };

    function evBadge(level) {
        const meta = EVIDENCE_LEVEL_META[level] || EVIDENCE_LEVEL_META.E;
        return `<span class="ev-badge ${meta.cls}" title="证据等级 ${level || "E"}：${meta.desc}">${level || "E"} · ${meta.label}</span>`;
    }
    function dataTypeLabel(dt) {
        return { experimental: "实验值", theoretical: "理论值", database: "数据库", inferred: "推断值" }[dt] || dt || "";
    }

    // 深度分析页顶部材料选择器（下拉切换分析对象；mats 为已获取的材料列表）
    function renderProfilePicker(content, mats, onSelect) {
        const picker = el("div", { class: "card profile-picker" });
        picker.appendChild(el("div", { class: "card-title" }, "选择分析对象"));
        const sel = el("select", { class: "profile-select" });
        sel.appendChild(el("option", { value: "", text: mats.length ? "— 请选择材料 —" : "暂无材料数据（先运行 research 阶段）" }));
        mats.forEach(m => {
            sel.appendChild(el("option", {
                value: m.material_id,
                text: `${m.name || "未命名材料"}${m.formula ? " · " + m.formula : ""}`,
            }));
        });
        sel.value = state.profileMaterialId || "";
        sel.addEventListener("change", () => {
            const m = mats.find(x => x.material_id === sel.value);
            state.profileMaterialId = sel.value || null;
            state.profileMaterialName = m ? m.name : null;
            if (onSelect) onSelect();
        });
        picker.appendChild(sel);
        content.appendChild(picker);
    }

    // 材料画像页头部：名称/化学式/结构徽章 + 证据汇总
    function profileHeader(p) {
        const badges = [];
        const s = p.structure || {};
        if (s.crystal_structure) badges.push(`<span class="badge badge-formula">${escapeHtml(s.crystal_structure)}</span>`);
        if (s.crystal_system) badges.push(`<span class="badge badge-neutral">晶系 ${escapeHtml(s.crystal_system)}</span>`);
        if (s.space_group) badges.push(`<span class="badge badge-neutral">空间群 ${escapeHtml(s.space_group)}</span>`);
        if (s.morphology) badges.push(`<span class="badge badge-neutral">形貌 ${escapeHtml(s.morphology)}</span>`);
        if (s.is_multiphase) badges.push(`<span class="badge badge-s2">多相</span>`);
        if (p.category && p.category !== "其他") badges.push(`<span class="badge mat-cat-badge ${matCatClass(p.category)}">${escapeHtml(p.category)}</span>`);
        const targetBadge = p.target ? `<span class="badge badge-accent">研究目标 ${escapeHtml(p.target)}</span>` : "";
        return `<div class="profile-head">
            <div class="profile-head-row">
                <span class="profile-name">${escapeHtml(p.name || "未命名材料")}</span>
                ${p.formula ? `<span class="profile-formula">${escapeHtml(p.formula)}</span>` : ""}
                ${targetBadge}
            </div>
            ${badges.length ? `<div class="profile-badges">${badges.join("")}</div>` : ""}
        </div>`;
    }

    // 材料画像页主体
    async function renderMaterialProfile(content) {
        content.appendChild(el("div", { class: "loading" }, "正在加载材料知识库…"));
        // 并行获取材料列表 + 目标材料画像
        const matsPromise = api("GET", `/api/projects/${state.currentProjectId}/materials`)
            .catch(() => ({ materials: [] }));
        let p = null;
        let loadErr = "";
        if (state.profileMaterialId) {
            try {
                p = await api("GET", `/api/projects/${state.currentProjectId}/materials/${state.profileMaterialId}/profile`);
            } catch (e) { loadErr = e.message || String(e); }
        }
        const matsData = await matsPromise;
        const mats = matsData.materials || [];

        clear(content);
        content.appendChild(el("h2", { class: "page-title" }, "材料深度画像"));
        content.appendChild(el("p", { class: "page-desc" },
            "多维性质画像 → 性质机制 → 目标性能因果链 → 横向对比 → 候选排序。" +
            "所有数值均来自已入库文献，缺失数据明确标记「暂无可靠文献数据」，绝不编造；证据等级 A/B/C/D/E 标注来源可信度。"));

        renderProfilePicker(content, mats, () => renderPage());

        if (!state.profileMaterialId) {
            content.appendChild(el("div", { class: "list-empty" },
                "尚未选择材料：请从上方下拉框选择，或到「材料知识」页点击材料卡片的「深度画像」按钮。"));
            return;
        }
        if (loadErr || !p) {
            content.appendChild(el("div", { class: "status-banner danger" }, "加载失败：" + loadErr));
            return;
        }
        content.insertAdjacentHTML("beforeend", profileHeader(p));

        // ① 结构信息
        const s = p.structure || {};
        const structRows = [];
        if (s.composition) structRows.push(["组成", s.composition]);
        if (s.element_composition) structRows.push(["元素组成", s.element_composition]);
        if (s.element_ratio) structRows.push(["元素比例", s.element_ratio]);
        if (s.lattice_parameters) structRows.push(["晶格参数", s.lattice_parameters]);
        if (s.symmetry) structRows.push(["对称性", s.symmetry]);
        if (s.phase_composition) structRows.push(["相组成", s.phase_composition]);
        if (s.material_type) structRows.push(["材料类型", s.material_type]);
        if (structRows.length) {
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">基础结构</div>
                <div class="kv-grid">${structRows.map(([k, v]) =>
                    `<div class="kv-item"><span class="kv-key">${escapeHtml(k)}</span><span class="kv-val">${escapeHtml(v)}</span></div>`).join("")}</div>
            </div>`);
        }

        // ② 多维性质仪表盘（按六大维度分组）
        const grouped = p.properties || {};
        const dims = DIMENSION_ORDER.filter(d => (grouped[d] || []).length);
        if (dims.length) {
            const dash = el("div", { class: "card" });
            dash.appendChild(el("div", { class: "card-title" }, "多维性质画像"));
            dims.forEach(d => {
                const meta = DIMENSION_META[d] || DIMENSION_META.other;
                const list = grouped[d];
                const body = list.map(pr => `
                    <div class="prof-prop">
                        <div class="prof-prop-head">
                            <span class="prof-prop-name">${pr.symbol ? `<span class="mat-sym">${escapeHtml(pr.symbol)}</span>` : ""}${escapeHtml(pr.norm_cn || pr.property_name_cn || pr.property_name || "未命名")}</span>
                            <span class="prof-prop-val">${escapeHtml(pr.value != null && pr.value !== "" ? pr.value : (pr.value_num != null ? String(pr.value_num) : "暂无数据"))}${pr.unit ? ` <span class="muted">${escapeHtml(pr.unit)}</span>` : ""}</span>
                        </div>
                        <div class="prof-prop-meta">
                            ${evBadge(pr.evidence_level)}
                            ${pr.data_type ? `<span class="prof-dt">${escapeHtml(dataTypeLabel(pr.data_type))}</span>` : ""}
                            ${pr.test_temperature ? `<span class="prof-cond">@ ${escapeHtml(pr.test_temperature)}</span>` : ""}
                            ${pr.condition ? `<span class="prof-cond">@ ${escapeHtml(pr.condition)}</span>` : ""}
                        </div>
                    </div>`).join("");
                const col = el("div", { class: "prof-dim" });
                col.appendChild(el("div", { class: "prof-dim-head" },
                    `<span class="prof-dim-dot" style="background:${meta.color}"></span>${meta.label} <span class="muted small">${list.length} 项</span>`));
                col.insertAdjacentHTML("beforeend", body);
                dash.appendChild(col);
            });
            content.appendChild(dash);
        }

        // ③ 性质 → 机制 → 目标性能 因果链
        const mechs = p.mechanisms || [];
        if (mechs.length) {
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">性质 → 机制 → 目标性能</div>
                <div class="mech-list">${mechs.map(m => `
                    <div class="mech-item">
                        <div class="mech-prop">${escapeHtml(m.property_cn || m.property)}${m.value ? ` <span class="mech-val">${escapeHtml(m.value)}${m.unit ? " " + escapeHtml(m.unit) : ""}</span>` : ""} ${evBadge(m.evidence_level)}</div>
                        <div class="mech-line"><span class="mech-arrow">→ 机制</span><span class="mech-text">${escapeHtml(m.mechanism || "")}</span></div>
                        <div class="mech-line"><span class="mech-arrow">→ 目标影响</span><span class="mech-text">${escapeHtml(m.impact_on_target || "")}</span></div>
                    </div>`).join("")}</div>
            </div>`);
        }

        // ④ 目标性能因果拆解
        const td = p.target_decomposition || {};
        if (td.formula) {
            const factorRows = (td.factors || []).map(f => `
                <div class="td-factor${f.has_data ? "" : " td-factor-missing"}">
                    <span class="td-factor-name">${escapeHtml(f.factor_cn || f.factor)}</span>
                    <span class="td-factor-role muted">${escapeHtml(f.role || "")}</span>
                    <span class="td-factor-val">${f.has_data ? (escapeHtml(f.value || "") + (f.unit ? " " + escapeHtml(f.unit) : "")) : "暂无数据"}</span>
                </div>`).join("");
            const priorityRows = (td.optimization_priority || []).map(o => `
                <div class="td-priority"><span class="td-priority-idx">${o.priority}</span><span class="td-priority-var">${escapeHtml(o.variable)}</span><span class="muted small">${escapeHtml(o.reason || "")}</span></div>`).join("");
            const strengthHtml = (td.strengths || []).map(x => `<span class="td-tag td-tag-ok">${escapeHtml(x)}</span>`).join("");
            const bottleHtml = (td.bottlenecks || []).map(x => `<span class="td-tag td-tag-warn">${escapeHtml(x)}</span>`).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">目标性能因果拆解 <span class="muted small">${escapeHtml(td.target || "")}</span></div>
                <div class="td-formula">${escapeHtml(td.formula)}</div>
                <div class="td-factors">${factorRows}</div>
                ${strengthHtml ? `<div class="td-tags">${strengthHtml}</div>` : ""}
                ${bottleHtml ? `<div class="td-tags">${bottleHtml}</div>` : ""}
                ${priorityRows ? `<div class="td-priorities"><div class="td-subtitle">优化优先级</div>${priorityRows}</div>` : ""}
            </div>`);
        }

        // ⑤ 对比矩阵（限定列数：当前材料 + 排名靠前的候选，避免 249 列卡死）
        const cmp = p.comparison || {};
        if (cmp.properties && cmp.properties.length && cmp.matrix) {
            const allIds = Object.keys(cmp.matrix);
            // 构建 name → material_id 映射（用于把 ranking 的 name 映射回 matrix）
            const nameToId = {};
            allIds.forEach(id => {
                const e = cmp.matrix[id];
                const key = (e.name || "") + "|" + (e.formula || "");
                if (!nameToId[key]) nameToId[key] = id;
            });
            const orderedIds = [];
            const pushId = (id) => { if (id && orderedIds.indexOf(id) < 0) orderedIds.push(id); };
            // 1) 当前材料置首
            pushId(state.profileMaterialId);
            // 2) 按 ranking 顺序补充（通过 name+formula 匹配）
            (p.ranking || []).forEach(r => {
                const id = nameToId[(r.material || "") + "|" + (r.formula || "")];
                pushId(id);
            });
            // 3) 剩余按 matrix 原始顺序补齐
            allIds.forEach(pushId);
            const MAX_COLS = 8;
            const matIds = orderedIds.slice(0, MAX_COLS);
            const propsMeta = cmp.properties;
            // 只展示「在这些列中至少有一个非缺失值」的性质行（减少空列噪音）
            const nonEmptyKeys = propsMeta.filter(pm => matIds.some(id => {
                const cell = cmp.matrix[id] && cmp.matrix[id].cells[pm.norm_key];
                return cell && !cell.missing;
            }));
            const showProps = nonEmptyKeys.length ? nonEmptyKeys : propsMeta.slice(0, 6);
            const omitted = allIds.length - matIds.length;
            const headerCells = matIds.map(id => {
                const m = cmp.matrix[id];
                return `<th>${escapeHtml(m.name || "材料")}${m.formula ? `<div class="muted small">${escapeHtml(m.formula)}</div>` : ""}</th>`;
            }).join("");
            const bodyRows = showProps.map(pm => {
                const cells = matIds.map(id => {
                    const cell = cmp.matrix[id] && cmp.matrix[id].cells[pm.norm_key];
                    if (!cell || cell.missing) {
                        return `<td class="cmp-missing">暂无数据</td>`;
                    }
                    const dt = cell.data_type ? `<span class="prof-dt">${escapeHtml(dataTypeLabel(cell.data_type))}</span>` : "";
                    const temp = cell.test_temperature ? `<span class="prof-cond">@${escapeHtml(cell.test_temperature)}</span>` : "";
                    return `<td><span class="cmp-val">${escapeHtml(cell.value)}${cell.unit ? " " + escapeHtml(cell.unit) : ""}</span>
                        <div class="cmp-meta">${evBadge(cell.evidence_level)}${dt}${temp}</div></td>`;
                }).join("");
                return `<tr><th class="cmp-prop">${pm.symbol ? `<span class="mat-sym">${escapeHtml(pm.symbol)}</span>` : ""}${escapeHtml(pm.norm_cn || pm.norm_key)}</th>${cells}</tr>`;
            }).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">材料横向对比矩阵 <span class="muted small">（单位已统一 · 范围显示 min–max · 证据等级标注）</span></div>
                ${omitted > 0 ? `<div class="muted small" style="margin-bottom:8px">已聚焦当前材料与排名前 ${matIds.length} 的候选（共 ${allIds.length} 种，其余 ${omitted} 种折叠隐藏）。</div>` : ""}
                <div class="cmp-table-wrap"><table class="cmp-table">
                    <thead><tr><th>性质</th>${headerCells}</tr></thead>
                    <tbody>${bodyRows}</tbody>
                </table></div>
            </div>`);
        }

        // ⑥ 候选排序（仅展示 Top 8，其余折叠）
        const ranking = p.ranking || [];
        if (ranking.length) {
            const shown = ranking.slice(0, 8);
            const omittedRank = ranking.length - shown.length;
            const cards = shown.map((r, i) => {
                const dims = r.dimensions || {};
                const dimBars = ["target_potential", "evidence_strength", "structure_match", "synthesis_feasibility", "stability", "novelty"].map(k => {
                    const label = { target_potential: "性能潜力", evidence_strength: "证据", structure_match: "结构匹配", synthesis_feasibility: "合成可行", stability: "稳定性", novelty: "创新性" }[k];
                    const v = dims[k] || 0;
                    return `<div class="rank-dim"><span class="rank-dim-label">${label}</span><span class="rank-dim-bar"><span class="rank-dim-fill" style="width:${Math.min(100, v)}%"></span></span><span class="rank-dim-val">${Math.round(v)}</span></div>`;
                }).join("");
                const strengths = (r.strengths || []).map(x => `<li class="rank-strength">${escapeHtml(x)}</li>`).join("");
                const risks = (r.risks || []).map(x => `<li class="rank-risk">${escapeHtml(x)}</li>`).join("");
                const ev = (r.evidence || []).slice(0, 3).map(t => `<span class="prof-src" title="${escapeHtml(t)}">${escapeHtml(String(t).slice(0, 40))}</span>`).join("");
                return `<div class="rank-card${i === 0 ? " rank-top" : ""}">
                    <div class="rank-head">
                        <span class="rank-idx">#${i + 1}</span>
                        <span class="rank-name">${escapeHtml(r.material || "")}</span>
                        ${r.formula ? `<span class="badge badge-formula">${escapeHtml(r.formula)}</span>` : ""}
                        <span class="rank-score">${r.composite_score}</span>
                    </div>
                    <div class="rank-reason muted small">${escapeHtml(r.reason || "")}</div>
                    <div class="rank-dims">${dimBars}</div>
                    ${strengths ? `<ul class="rank-list rank-list-ok">${strengths}</ul>` : ""}
                    ${risks ? `<ul class="rank-list rank-list-warn">${risks}</ul>` : ""}
                    ${ev ? `<div class="rank-ev">${ev}</div>` : ""}
                </div>`;
            }).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">候选材料排序 <span class="muted small">（六维加权评分 · 可溯源）</span></div>
                ${omittedRank > 0 ? `<div class="muted small" style="margin-bottom:8px">共 ${ranking.length} 种候选，展示 Top ${shown.length}。</div>` : ""}
                <div class="rank-list">${cards}</div>
            </div>`);
        }
    }

    // 合成路线页主体
    async function renderSynthesis(content) {
        content.appendChild(el("div", { class: "loading" }, "正在加载材料知识库…"));
        const matsPromise = api("GET", `/api/projects/${state.currentProjectId}/materials`)
            .catch(() => ({ materials: [] }));
        let p = null;
        let loadErr = "";
        if (state.profileMaterialId) {
            try {
                p = await api("GET", `/api/projects/${state.currentProjectId}/materials/${state.profileMaterialId}/profile`);
            } catch (e) { loadErr = e.message || String(e); }
        }
        const matsData = await matsPromise;
        const mats = matsData.materials || [];

        clear(content);
        content.appendChild(el("h2", { class: "page-title" }, "合成路线设计"));
        content.appendChild(el("p", { class: "page-desc" },
            "路线对比 → 目标驱动推荐 → 分步实验流程 → 参数敏感性 → 风险与可复现性。" +
            "文献直引参数标记「文献」，AI 归纳的通用步骤标记「AI 归纳」，绝不编造参数。"));

        renderProfilePicker(content, mats, () => renderPage());

        if (!state.profileMaterialId) {
            content.appendChild(el("div", { class: "list-empty" },
                "尚未选择材料：请从上方下拉框选择，或到「材料知识」页点击材料卡片的「合成路线」按钮。"));
            return;
        }
        if (loadErr || !p) {
            content.appendChild(el("div", { class: "status-banner danger" }, "加载失败：" + loadErr));
            return;
        }
        content.insertAdjacentHTML("beforeend", profileHeader(p));

        const syn = p.synthesis || {};
        const routes = (syn.routes && syn.routes.routes) || [];

        // ① 路线对比表
        if (routes.length) {
            const rows = routes.map(r => {
                const risks = (r.risks || []).map(x => `<span class="risk-chip risk-${(x.level || "low").toLowerCase()}" title="${escapeHtml(x.reason || "")}">${escapeHtml(x.risk || "")}</span>`).join("");
                const adv = (r.advantages || []).map(x => `<span class="route-adv">${escapeHtml(x)}</span>`).join("");
                return `<tr>
                    <td><strong>${escapeHtml(r.method || "")}</strong></td>
                    <td>${escapeHtml(r.temperature || "—")}</td>
                    <td>${escapeHtml(r.cost || "—")}</td>
                    <td>${escapeHtml(r.phase_purity || "—")}</td>
                    <td>${escapeHtml(r.particle_control || "—")}</td>
                    <td>${escapeHtml(r.scale_difficulty || "—")}</td>
                    <td>${adv || "—"}</td>
                    <td>${risks || "—"}</td>
                    <td><span class="rank-score">${r.recommendation_score}</span></td>
                    <td>${evBadge(r.evidence_level)}</td>
                </tr>`;
            }).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">合成路线对比</div>
                <div class="cmp-table-wrap"><table class="cmp-table route-table">
                    <thead><tr><th>工艺</th><th>温度</th><th>成本</th><th>相纯度</th><th>粒径控制</th><th>放大</th><th>优势</th><th>风险</th><th>推荐度</th><th>证据</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>
            </div>`);
        } else {
            content.appendChild(el("div", { class: "card mat-empty" },
                "暂无合成路线记录：该材料的入库论文未提供工艺条件。"));
        }

        // ② 目标驱动路线推荐
        const reco = syn.route_recommendation || {};
        if (reco.ranking && reco.ranking.length) {
            const recCards = reco.ranking.map((r, i) => {
                const reasons = (r.goal_reasons || []).map(x => `<span class="route-goal-reason">${escapeHtml(x)}</span>`).join("");
                return `<div class="route-reco-item${i === 0 ? " route-reco-top" : ""}">
                    <div class="route-reco-head"><span class="rank-idx">#${i + 1}</span><strong>${escapeHtml(r.method)}</strong><span class="rank-score">${r.score}</span></div>
                    <div class="route-reco-reasons">${reasons}</div>
                </div>`;
            }).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">目标驱动路线推荐 <span class="muted small">（目标：${escapeHtml((reco.goals || []).join("、") || "—")}）</span></div>
                <div class="route-reco-list">${recCards}</div>
            </div>`);
        }

        // ③ 分步实验工作流（每条合成）
        const workflows = syn.workflows || [];
        if (workflows.length) {
            const wfBlocks = workflows.map(w => {
                const steps = (w.workflow_steps || []).map(st => {
                    const literal = st.is_literal;
                    return `<div class="wf-step${literal ? " wf-literal" : " wf-inferred"}">
                        <span class="wf-step-idx">${st.step}</span>
                        <span class="wf-step-op">${escapeHtml(st.operation || "")}</span>
                        <span class="wf-step-param">${st.parameter ? escapeHtml(st.parameter) : "（通用步骤）"}</span>
                        <span class="wf-step-src ${literal ? "" : "wf-src-ai"}">${literal ? "文献" : "AI 归纳"}</span>
                    </div>`;
                }).join("");
                const rep = w.reproducibility || {};
                const repFactors = rep.factors || {};
                const repLabels = { param_completeness: "参数完整度", precursor_completeness: "前驱体信息", equipment_completeness: "设备信息", key_param_clarity: "关键参数明确", independent_sources: "独立文献支持", result_consistency: "结果一致性" };
                const repBars = Object.keys(repLabels).map(k => {
                    const v = repFactors[k] != null ? repFactors[k] : 0;
                    return `<div class="rank-dim"><span class="rank-dim-label">${repLabels[k]}</span><span class="rank-dim-bar"><span class="rank-dim-fill" style="width:${v}%"></span></span><span class="rank-dim-val">${v}</span></div>`;
                }).join("");
                const risks = (w.risks || []).map(x => `<div class="risk-line"><span class="risk-chip risk-${(x.level || "low").toLowerCase()}">${escapeHtml(x.risk || "")}</span><span class="muted small">${escapeHtml(x.reason || "")}${x.source ? ` · ${escapeHtml(x.source)}` : ""}</span></div>`).join("");
                return `<div class="wf-card">
                    <div class="wf-card-head">
                        <strong>${escapeHtml(w.method || "合成方法")}</strong>
                        <span>${evBadge(w.evidence_level)}</span>
                        ${w.paper_title ? `<span class="prof-src" title="${escapeHtml(w.paper_title)}">${escapeHtml(String(w.paper_title).slice(0, 40))}</span>` : ""}
                        <span class="rank-score">可复现性 ${rep.score != null ? rep.score : "—"}</span>
                    </div>
                    <div class="wf-steps">${steps}</div>
                    <div class="wf-repro"><div class="td-subtitle">可复现性评分因素</div>${repBars}</div>
                    ${risks ? `<div class="wf-risks"><div class="td-subtitle">风险分析</div>${risks}</div>` : ""}
                </div>`;
            }).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">分步实验工作流 <span class="muted small">（文献直引 vs AI 归纳）</span></div>
                <div class="wf-list">${wfBlocks}</div>
            </div>`);
        }

        // ④ 参数敏感性
        const sens = syn.sensitivity || {};
        if ((sens.high_impact && sens.high_impact.length) || (sens.low_impact && sens.low_impact.length)) {
            const chip = (x, cls) => `<span class="sens-chip ${cls}" title="${escapeHtml(x.reason || "")}">${escapeHtml(x.parameter)}</span>`;
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">参数敏感性分析 <span class="muted small">（按文献出现频率推导）</span></div>
                <div class="sens-row"><span class="sens-label">高影响参数</span>${(sens.high_impact || []).map(x => chip(x, "sens-high")).join("") || "—"}</div>
                <div class="sens-row"><span class="sens-label">低影响参数</span>${(sens.low_impact || []).map(x => chip(x, "sens-low")).join("") || "—"}</div>
            </div>`);
        }

        // ⑤ 性质-合成联合分析（工艺→结构→性质→性能链路）
        const ja = p.joint_analysis || {};
        const links = ja.process_property_links || [];
        if (links.length) {
            const linkHtml = links.map(l => `
                <div class="ppl-item">
                    <div class="ppl-head"><span class="ppl-process">${escapeHtml(l.process)}${l.direction ? ` ${escapeHtml(l.direction)}` : ""}</span></div>
                    <div class="ppl-flow">
                        <span class="ppl-node">结构：${escapeHtml(l.structure_effect || "")}</span>
                        <span class="ppl-arrow">→</span>
                        <span class="ppl-node">性质：${escapeHtml(l.property_effect || "")}</span>
                        <span class="ppl-arrow">→</span>
                        <span class="ppl-node ppl-target">性能：${escapeHtml(l.target_effect || "")}</span>
                    </div>
                </div>`).join("");
            content.insertAdjacentHTML("beforeend", `<div class="card">
                <div class="card-title">工艺 → 结构 → 性质 → 性能 链路 <span class="muted small">（基于文献中实际给出的参数）</span></div>
                <div class="ppl-list">${linkHtml}</div>
            </div>`);
        }
    }

    // ===== 3.5 研究缺口页（Task 3：Research Gap 识别） =====

    let gapsFilter = "all";

    const GAP_TYPE_META = {
        contradiction: { label: "矛盾结论", cls: "gap-tag-danger" },
        unexplored: { label: "未被探索方向", cls: "gap-tag-info" },
        missing_link: { label: "缺失知识连接", cls: "gap-tag-warning" },
    };
    const GAP_ACTION_META = {
        high: { label: "高可操作性", cls: "gap-act-high" },
        medium: { label: "中", cls: "gap-act-medium" },
        low: { label: "低", cls: "gap-act-low" },
    };

    async function renderGaps(content) {
        content.appendChild(el("div", { class: "loading" }, "加载研究缺口…"));
        let data;
        try {
            data = await api("GET", `/api/projects/${state.currentProjectId}/gaps`);
        } catch (e) {
            content.appendChild(el("div", { class: "status-banner danger" },
                "加载失败：" + (e.message || e)));
            return;
        }
        clear(content);
        const gaps = data.gaps || [];
        const stats = data.stats || {};

        content.appendChild(el("h2", { class: "page-title" }, "研究缺口（Research Gap）"));
        const byType = stats.by_type || {};
        const typeBrief = Object.entries(byType)
            .map(([k, v]) => `${GAP_TYPE_META[k] ? GAP_TYPE_META[k].label : k} ${v} 个`)
            .join(" · ") || "暂无";
        content.appendChild(el("p", { class: "page-desc" },
            `共 ${gaps.length} 个研究缺口（${typeBrief}）。` +
            `由 research 阶段「研究缺口识别」节点在交叉验证后生成：` +
            `双通道（LLM 语义分析 + 数据驱动断链检测）输出带证据链的结构化 Gap 清单，` +
            `供思路生成（ideation）与构效关系发现（discovery）消费。`));

        if (!gaps.length) {
            content.appendChild(el("div", { class: "card mat-empty" },
                "暂无研究缺口。完成 research 阶段的「交叉验证 → 研究缺口识别」节点后自动生成。"));
            return;
        }

        // 类型筛选条
        const bar = el("div", { class: "filter-bar" });
        bar.appendChild(el("span", { class: "filter-label", text: "类型筛选：" }));
        ["all", "contradiction", "unexplored", "missing_link"].forEach(t => {
            const label = t === "all" ? "全部" : (GAP_TYPE_META[t] ? GAP_TYPE_META[t].label : t);
            bar.appendChild(el("span", {
                class: "filter-chip" + (gapsFilter === t ? " active" : ""),
                text: label,
                onclick: () => {
                    gapsFilter = t;
                    renderPage();
                },
            }));
        });
        content.appendChild(bar);

        // Gap 卡片列表（按优先级 1 最高在前，接口已排序）
        const sorted = gapsFilter === "all"
            ? gaps
            : gaps.filter(g => g.gap_type === gapsFilter);
        const list = el("div", { class: "gap-list" });
        for (const g of sorted) {
            list.appendChild(renderGapCard(g));
        }
        content.appendChild(list);

        // 文献冲突裁决区块（会议纪要：同一结论冲突时按期刊等级/文献新旧二次验证，用户可裁决）
        content.appendChild(await renderConflictAdjudication());
    }

    // ===== 文献冲突裁决（Task 2：冲突二次验证） =====
    // 每条冲突：support/refute 双方证据 → 自动裁决建议（期刊等级+文献新度打分）
    // → 用户可一键采纳 / 存疑，裁决结果写回 DB（metadata.adjudication）
    const CONFLICT_VERDICT_META = {
        adopt_support: { label: "采纳支持方", cls: "verdict-support" },
        adopt_refute: { label: "采纳反对方", cls: "verdict-refute" },
        suspect: { label: "双方存疑", cls: "verdict-suspect" },
        unknown: { label: "证据不足", cls: "verdict-unknown" },
    };

    async function renderConflictAdjudication() {
        const card = el("div", { class: "card conflict-adjudication" });
        let data;
        try {
            data = await api("GET", `/api/projects/${state.currentProjectId}/conflicts`);
        } catch (e) {
            return card; // 静默：冲突区块失败不影响缺口页
        }
        const conflicts = data.conflicts || [];
        card.appendChild(el("div", { class: "card-title" },
            `文献冲突裁决（${conflicts.length} 处）· 自动建议 + 人工裁决`));
        card.appendChild(el("p", { class: "muted small mb-12" },
            "同一结论存在冲突的文献自动按「期刊等级（IF/分区）> 文献新度」加权打分给出采纳建议，你可一键裁决覆盖（结果用于构效分析前剔除低可信来源）。"));
        if (!conflicts.length) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无文献冲突（交叉验证未发现矛盾结论）"));
            return card;
        }

        const list = el("div", { class: "list" });
        conflicts.forEach((c, i) => {
            const item = el("div", { class: "list-item conflict-item" });
            // 头部：编号 + 陈述 + 裁决状态徽章
            const vm = CONFLICT_VERDICT_META[c.auto_verdict] || CONFLICT_VERDICT_META.suspect;
            const head = el("div", { class: "list-item-head" }, [
                el("span", { class: "conflict-number", text: `#${i + 1}` }),
                el("span", { class: "list-item-title", text: c.claim || "(无陈述)" }),
            ]);
            if (c.adjudicated) {
                head.appendChild(el("span", {
                    class: `conflict-verdict-badge ${vm.cls}`,
                    title: "已人工裁决（可重新裁决覆盖）",
                    text: `已裁决：${vm.label}`,
                }));
            }
            item.appendChild(head);

            // 双方证据质量条
            const sides = el("div", { class: "conflict-sides" });
            const mkSide = (label, stance, score, info, sources) => {
                const side = el("div", {
                    class: `conflict-side ${stance}${score >= 0.3 ? " stronger" : ""}`,
                });
                side.appendChild(el("div", { class: "conflict-side-head" }, [
                    el("span", { class: `conflict-stance-tag ${stance}`, text: label }),
                    el("span", { class: "conflict-side-score", text: `质量 ${(score * 100).toFixed(0)}` }),
                    el("span", { class: "muted small", text: info || "" }),
                ]));
                const srcs = sources || [];
                if (srcs.length) {
                    const ul = el("ul", { class: "conflict-src-list" });
                    srcs.slice(0, 3).forEach(s => {
                        const li = el("li", { class: "small" });
                        const pid = s.paper_id || "";
                        li.appendChild(el("span", { text: `[${pid.slice(-8)}] ${s.title || "来源论文"}` }));
                        if (pid) {
                            li.appendChild(el("a", {
                                class: "gap-ev-link",
                                text: "查看 →",
                                href: "#",
                                onclick: (e) => {
                                    e.preventDefault(); e.stopPropagation();
                                    state.pendingPaperId = pid;
                                    setActivePage("papers");
                                },
                            }));
                        }
                        ul.appendChild(li);
                    });
                    side.appendChild(ul);
                }
                return side;
            };
            sides.appendChild(mkSide("支持方", "support", c.support_score || 0, c.support_info, c.sources.filter(s => s.stance === "support")));
            sides.appendChild(mkSide("反对方", "refute", c.refute_score || 0, c.refute_info, c.sources.filter(s => s.stance === "refute")));
            item.appendChild(sides);

            // 自动裁决建议
            item.appendChild(el("div", { class: "conflict-auto-verdict" }, [
                el("span", { class: "conflict-auto-label" }, "裁决建议："),
                el("span", { class: `conflict-verdict-badge ${vm.cls}`, text: vm.label }),
                el("span", { class: "muted small", text: ` — ${c.auto_reason || ""}` }),
            ]));

            // 裁决按钮（人工覆盖）
            const btnRow = el("div", { class: "btn-row conflict-adjudicate-btns" }, [
                el("button", {
                    class: "btn btn-sm btn-success",
                    onclick: () => adjudicateConflict(c.conflict_id, "adopt_support", item),
                }, "采纳支持方"),
                el("button", {
                    class: "btn btn-sm btn-warning",
                    onclick: () => adjudicateConflict(c.conflict_id, "adopt_refute", item),
                }, "采纳反对方"),
                el("button", {
                    class: "btn btn-sm btn-secondary",
                    onclick: () => adjudicateConflict(c.conflict_id, "suspect", item),
                }, "双方存疑"),
            ]);
            item.appendChild(btnRow);
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    // 提交冲突裁决并就地刷新该卡片
    async function adjudicateConflict(conflictId, verdict, item) {
        try {
            const res = await api("POST",
                `/api/projects/${state.currentProjectId}/conflicts/${conflictId}/adjudicate`,
                { verdict, note: "" });
            showToast(res.message || "裁决已保存", "success");
            // 就地更新：重新拉冲突列表，替换当前冲突卡片
            const data = await api("GET", `/api/projects/${state.currentProjectId}/conflicts`);
            const conf = (data.conflicts || []).find(x => x.conflict_id === conflictId);
            if (conf && item && item.parentNode) {
                const vm = CONFLICT_VERDICT_META[conf.auto_verdict] || CONFLICT_VERDICT_META.suspect;
                const badge = item.querySelector(".conflict-verdict-badge");
                if (badge) {
                    badge.className = `conflict-verdict-badge ${vm.cls}`;
                    badge.textContent = `已裁决：${vm.label}`;
                    badge.title = "已人工裁决（可重新裁决覆盖）";
                }
                const auto = item.querySelector(".conflict-auto-verdict");
                if (auto) {
                    auto.querySelector(".conflict-verdict-badge").className = `conflict-verdict-badge ${vm.cls}`;
                    auto.querySelector(".conflict-verdict-badge").textContent = vm.label;
                    auto.querySelector(".muted").textContent = " — 人工裁决已生效";
                }
            }
        } catch (e) {
            showToast("裁决失败：" + (e.message || e), "error");
        }
    }

    function renderGapCard(g) {
        const typeMeta = GAP_TYPE_META[g.gap_type] || { label: g.gap_type || "未知", cls: "gap-tag-neutral" };
        const actMeta = GAP_ACTION_META[g.actionability] || { label: g.actionability || "中", cls: "gap-act-medium" };
        const sourceLabel = g.source === "data_driven" ? "数据驱动"
            : g.source === "db_driven" ? "数据库驱动"
            : g.source === "hybrid" ? "LLM + 数据"
            : g.source === "placeholder" ? "占位"
            : "LLM 分析";

        const card = el("div", { class: "gap-card" });

        // 头部：类型徽章 + 优先级 + 来源 + statement
        const head = el("div", { class: "gap-head" });
        head.appendChild(el("span", { class: `gap-tag ${typeMeta.cls}`, text: typeMeta.label }));
        head.appendChild(el("span", { class: "gap-priority", text: `优先级 P${g.priority || 3}` }));
        head.appendChild(el("span", { class: "gap-source", text: sourceLabel }));
        head.appendChild(el("span", { class: `gap-act ${actMeta.cls}`, text: `可操作性：${actMeta.label}` }));
        card.appendChild(head);

        card.appendChild(el("div", { class: "gap-statement", text: g.statement || "（无陈述）" }));

        if (g.detail) {
            card.appendChild(el("div", { class: "gap-detail", text: g.detail }));
        }

        // 关联材料
        const mats = g.related_materials || [];
        if (mats.length) {
            const row = el("div", { class: "gap-mats" });
            row.appendChild(el("span", { class: "gap-row-label", text: "关联材料：" }));
            mats.forEach(m => row.appendChild(el("span", { class: "mat-chip", text: m })));
            card.appendChild(row);
        }

        // 证据链（可溯源）
        const evs = g.evidence || [];
        if (evs.length) {
            const evBlock = el("div", { class: "gap-evidence" });
            evBlock.appendChild(el("div", { class: "gap-row-label", text: `证据链（${evs.length} 条，可溯源）：` }));
            evs.slice(0, 3).forEach(ev => {
                const title = ev.title || ev.paper_id || "来源论文";
                const snippet = ev.snippet ? ` — ${ev.snippet.slice(0, 120)}${ev.snippet.length > 120 ? "…" : ""}` : "";
                const row = el("div", { class: "gap-ev-item" });
                row.appendChild(el("span", { text: `[${ev.paper_id ? ev.paper_id.slice(-8) : "?"}] ${title}${snippet}` }));
                if (ev.paper_id) {
                    row.appendChild(el("a", {
                        class: "gap-ev-link",
                        text: "查看论文 →",
                        href: "#",
                        onclick: (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            state.pendingPaperId = ev.paper_id;
                            setActivePage("papers");
                        },
                    }));
                }
                evBlock.appendChild(row);
            });
            if (evs.length > 3) {
                evBlock.appendChild(el("div", { class: "gap-ev-more", text: `…另有 ${evs.length - 3} 条证据` }));
            }
            card.appendChild(evBlock);
        }

        // 建议行动
        const acts = g.suggested_actions || [];
        if (acts.length) {
            const actRow = el("div", { class: "gap-actions" });
            actRow.appendChild(el("span", { class: "gap-row-label", text: "建议行动：" }));
            acts.forEach(a => actRow.appendChild(el("span", { class: "gap-act-chip", text: a })));
            card.appendChild(actRow);
        }

        // 数据库证据链（Materials Project / OQMD / NOMAD，与文献证据构成双证据链）
        const dbEvs = g.db_evidence || [];
        if (dbEvs.length) {
            const dbBlock = el("div", { class: "gap-dbevidence" });
            dbBlock.appendChild(el("div", { class: "gap-row-label", text: `数据库证据（${dbEvs.length} 条，MP/OQMD/NOMAD）：` }));
            dbEvs.slice(0, 3).forEach(ev => {
                const formula = ev.formula || ev.name || "?";
                const mp = ev.mp || {};
                const oqmd = ev.oqmd || {};
                const nomad = ev.nomad || {};
                const chips = [];
                chips.push(`MP ${mp.matched ? `命中 ${mp.entry_count ?? 0} 条` : "未命中"}`);
                if (mp.band_gap != null) chips.push(`带隙 ${mp.band_gap} eV`);
                chips.push(`OQMD ${oqmd.matched ? `命中 ${oqmd.entry_count ?? 0} 条` : "未命中"}`);
                if (oqmd.stability) chips.push(oqmd.stability);
                chips.push(`NOMAD ${nomad.matched ? `命中 ${nomad.entry_count ?? 0} 条` : "未命中"}`);
                const row = el("div", { class: "gap-dbev-item" });
                row.appendChild(el("span", { text: `[${formula}] ${chips.join(" · ")}` }));
                dbBlock.appendChild(row);
            });
            if (dbEvs.length > 3) {
                dbBlock.appendChild(el("div", { class: "gap-ev-more", text: `…另有 ${dbEvs.length - 3} 条数据库证据` }));
            }
            card.appendChild(dbBlock);
        }

        return card;
    }

    // ===== 5. 实验页 =====

    async function renderExperiments(content) {
        content.appendChild(el("div", { class: "loading" }, "加载中…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/experiments`);
            clear(content);
            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "实验列表"),
                el("p", { class: "page-desc" }, `共 ${data.experiments.length} 个实验。`),
            ]));
            if (!data.experiments.length) {
                content.appendChild(el("div", { class: "list-empty" }, "暂无实验，请先启动 experiment 阶段"));
                return;
            }
            const list = el("div", { class: "list" });
            data.experiments.forEach(e => list.appendChild(renderExperimentItem(e)));
            content.appendChild(list);

            // 实验定位（Claim 证据溯源跳转）
            const targetId = state.pendingExperimentId;
            if (targetId) {
                state.pendingExperimentId = null;
                const cards = list.querySelectorAll(".list-item");
                for (const card of cards) {
                    if (card.getAttribute("data-exp-id") === targetId) {
                        card.scrollIntoView({ behavior: "smooth", block: "center" });
                        card.classList.add("paper-flash");
                        setTimeout(() => card.classList.remove("paper-flash"), 2600);
                        if (!card.classList.contains("expanded")) {
                            card.classList.add("expanded");
                            const exp = data.experiments.find(x => x.experiment_id === targetId);
                            if (exp && !card.querySelector(".list-item-body")) {
                                // 复用 renderExperimentItem 的展开逻辑：触发一次展开
                                const body = el("div", { class: "list-item-body" });
                                body.innerHTML = buildExperimentBody(exp);
                                card.appendChild(body);
                            }
                        }
                        break;
                    }
                }
            }
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    function renderExperimentItem(e) {
        const item = el("div", { class: "list-item" });
        item.setAttribute("data-exp-id", e.experiment_id || "");
        item.appendChild(el("div", { class: "list-item-head" }, [
            el("span", { class: "list-item-title", text: e.name || "(无名称)" }),
        ]));
        item.insertAdjacentHTML("beforeend",
            `<div class="mt-8">${statusBadge(e.status)} <span class="badge badge-neutral">验证 ${e.verifies_claim_ids?.length || 0} 个 Claim</span></div>`);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                const body = el("div", { class: "list-item-body" });
                body.innerHTML = buildExperimentBody(e);
                item.appendChild(body);
            }
        });
        return item;
    }

    function buildExperimentBody(e) {
        return `
                    <dl>
                        <dt>Experiment ID</dt><dd class="mono">${escapeHtml(e.experiment_id)}</dd>
                        <dt>名称</dt><dd>${escapeHtml(e.name)}</dd>
                        <dt>状态</dt><dd>${escapeHtml(e.status)}</dd>
                        <dt>验证 Claim</dt><dd class="mono">${escapeHtml((e.verifies_claim_ids || []).join(", ") || "—")}</dd>
                        <dt>开始时间</dt><dd class="mono">${escapeHtml(formatTime(e.started_at))}</dd>
                        <dt>完成时间</dt><dd class="mono">${escapeHtml(formatTime(e.completed_at))}</dd>
                        <dt>创建时间</dt><dd class="mono">${escapeHtml(formatTime(e.created_at))}</dd>
                    </dl>
                    ${e.result_summary ? `<div class="mt-8"><strong>结果摘要：</strong><br>${escapeHtml(e.result_summary)}</div>` : ""}
                    ${e.anomaly_notes ? `<div class="mt-8" style="color:var(--color-danger)"><strong>异常记录：</strong><br>${escapeHtml(e.anomaly_notes)}</div>` : ""}
                    ${e.config && Object.keys(e.config).length
                        ? `<div class="mt-8"><strong>实验配置：</strong></div><pre class="code-block">${escapeHtml(JSON.stringify(e.config, null, 2))}</pre>`
                        : ""}
                `;
    }

    // ===== 5b. 构效关系发现页（路线 A）=====

    async function renderDiscovery(content) {
        content.appendChild(renderSkeleton("cards"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/discoveries`);
            state.discoveryCache = data;
            if (data.run_mode) state.runMode = data.run_mode;
            setBadge("badge-discovery", (data.relationships || []).length || null);
            clear(content);

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "构效关系发现"),
                el("p", { class: "page-desc" },
                    "路线 A：调研 → 假设生成 → 搜索空间 → LLM 引导搜索 → 验证 → 汇报。下方展示发现工作流的产出与节点执行状态。" +
                    "面向材料科学领域的文献驱动科学发现：从调研文献中提取构效关系假设，用 MCTS + LLM 融合搜索验证，产出含证据链的新颖发现。"),
            ]));

            // 功能说明卡片
            content.appendChild(el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "功能说明"),
                el("div", { class: "info-list" }, [
                    el("p", { class: "small mb-0" }, [
                        el("strong", {}, "适用场景："),
                        "材料科学、化学、药物设计等领域，需从文献中发现「结构-性能」关系（如热电材料 ZT 值优化、合金强度预测等）。",
                    ]),
                    el("p", { class: "small mb-0" }, [
                        el("strong", {}, "与主 Pipeline 的关系："),
                        "发现工作流复用调研阶段的文献产出（论文 + 交叉验证报告），独立执行 5 步发现流程，产出构效关系发现报告。不依赖 ideation/design/experiment 阶段。",
                    ]),
                    el("p", { class: "small mb-0" }, [
                        el("strong", {}, "发现流程："),
                        "① 假设生成（从 Research Gap 提取构效关系种子）→ ② 搜索空间定义（材料变量 + 性能目标 + 文献数据点）→ ③ MCTS + LLM 引导搜索（核心创新）→ ④ 验证（文献交叉验证 + 新颖性评估）→ ⑤ 报告生成（含物理机制解释）。",
                    ]),
                    el("p", { class: "small mb-0" }, [
                        el("strong", {}, "产出："),
                        "验证通过的构效关系发现（含 novel/partially_known/known 新颖性标签）、文献证据链、交叉验证报告、发现报告（可下载）。",
                    ]),
                ]),
            ]));

            // 运行模式提示
            if (data.run_mode === "discovery") {
                content.insertAdjacentHTML("beforeend",
                    `<div class="status-banner info"><span class="status-dot"></span><strong>discovery 模式</strong><span>当前项目已启用构效关系发现工作流，结果随轮询自动刷新。</span></div>`);
            } else if (!data.run_mode) {
                // 空状态：未启动时引导用户启动
                content.appendChild(renderEmptyState({
                    icon: "🔬",
                    title: "尚未运行构效关系发现",
                    desc: "点击下方按钮启动发现工作流。系统将从调研文献中自动提取构效关系假设，用 MCTS + LLM 融合搜索验证，产出新颖的科学发现。",
                    actions: [
                        el("button", {
                            class: "btn btn-accent",
                            onclick: () => startDiscovery(),
                        }, "启动构效关系发现"),
                    ],
                }));
            }

            // 操作区
            const actionCard = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "发现工作流操作"),
                el("div", { class: "btn-row" }, [
                    el("button", {
                        class: "btn btn-accent",
                        "data-tooltip": "启动 5 步发现流程：假设 → 搜索空间 → MCTS → 验证 → 报告",
                        onclick: () => startDiscovery(),
                    }, "启动构效关系发现"),
                    el("button", {
                        class: "btn btn-secondary",
                        onclick: () => { fetchDiscoveries(); showToast("已刷新", "success"); },
                    }, "刷新结果"),
                    el("button", {
                        class: "btn btn-secondary",
                        onclick: () => setActivePage("progress"),
                    }, "查看 Pipeline 进度"),
                ]),
            ]);
            content.appendChild(actionCard);

            // discovery_summary 计数卡片
            const summary = data.discovery_summary || {};
            content.appendChild(renderDiscoverySummary(summary));

            // 假设可验证性评分（三维评分 + 排序）
            if (summary.hypothesis_list && summary.hypothesis_list.length) {
                content.appendChild(renderHypothesisScores(summary.hypothesis_list));
            }

            // discovery 节点执行状态
            if (summary.nodes && summary.nodes.length) {
                content.appendChild(renderDiscoveryNodes(summary.nodes));
            }

            // relationships 列表
            const rels = data.relationships || [];
            if (rels.length > 0) {
                content.appendChild(renderRelationships(rels));
            } else if (data.run_mode === "discovery") {
                // 运行中但暂无结果
                content.appendChild(renderEmptyState({
                    icon: "🧪",
                    title: "发现流程进行中中",
                    desc: "MCTS + LLM 引导搜索正在执行。完成后将显示构效关系发现列表。",
                }));
            }

            // 客观指标层（材料方向评审关键）：让评委/专家一眼可见「数据支持的质量」

            // 1) Research Gap 质量评分（上游入口）
            const gapScores = data.gap_scores || [];
            if (gapScores.length) {
                content.appendChild(renderGapQualityScores(gapScores, data.gap_summary || {}));
            }

            // 2) Discovery 可信度评分（核心：5 维度 + 风险标签）
            const relScores = data.reliability_scores || [];
            if (relScores.length) {
                content.appendChild(renderReliabilityScores(relScores, data.reliability_summary || {}));
            }

            // 3) 专家辅助包（让材料专家感到「对我有用」）
            const expertAssists = data.expert_assistances || [];
            if (expertAssists.length) {
                content.appendChild(renderExpertAssistances(expertAssists));
            }
        } catch (e) {
            clear(content);
            content.appendChild(renderErrorState({
                title: "发现数据加载失败",
                desc: e.message || "请稍后重试",
                actions: [
                    el("button", {
                        class: "btn",
                        onclick: () => setActivePage("discovery"),
                    }, "重试"),
                ],
            }));
        }
    }

    // ===== 客观指标层渲染（材料方向评审关键） =====

    function renderGapQualityScores(scores, summary) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `Research Gap 质量评分（4 维度 + 综合分，共 ${scores.length} 条）`));
        card.appendChild(el("div", { class: "card-desc" },
            "客观评分依据知识库真实数据：文献稀缺度 · 可填补性 · 行动清晰度 · 关联强度。" +
            "综合分 = 0.30×稀缺度 + 0.30×可填补性 + 0.20×行动清晰度 + 0.20×关联强度。" +
            "评分对应「调研报告」中识别的研究缺口，点击标题可展开关联论文。"));

        // 顶部汇总
        if (summary && (summary.avg_quality_score != null || summary.top_gap_id)) {
            const topId = summary.top_gap_id || (scores[0] || {}).gap_id;
            const topScore = (scores.find(s => s.gap_id === topId) || {}).quality_score;
            const summaryBox = el("div", { class: "metric-summary" }, [
                el("div", { class: "metric-summary-cell" }, [
                    el("div", { class: "metric-summary-label" }, "平均综合分"),
                    el("div", { class: "metric-summary-val", text:
                        summary.avg_quality_score != null ? summary.avg_quality_score.toFixed(2) : "—" }),
                ]),
                el("div", { class: "metric-summary-cell" }, [
                    el("div", { class: "metric-summary-label" }, "Top Gap 综合分"),
                    el("div", { class: "metric-summary-val", text:
                        topScore != null ? topScore.toFixed(2) : "—" }),
                    el("div", { class: "metric-summary-sub mono small", text:
                        topId ? (scores.find(s => s.gap_id === topId) || {}).statement || "" : "" }),
                ]),
                el("div", { class: "metric-summary-cell" }, [
                    el("div", { class: "metric-summary-label" }, "评分版本"),
                    el("div", { class: "metric-summary-val mono small", text:
                        (summary.score_version || "v1.0") }),
                ]),
            ]);
            card.appendChild(summaryBox);
        }

        const list = el("div", { class: "metric-list" });
        scores.forEach((s, idx) => {
            const item = el("div", { class: "list-item metric-item gap-score-item" });
            const dims = s.dimensions || {};
            const overall = s.quality_score != null ? s.quality_score.toFixed(2) : "0.00";
            // 显示标题：优先 statement（一句话陈述），否则回退论文标题，再回退 gap_id
            const statement = s.statement || "";
            const paperTitle = (s.paper_titles && s.paper_titles[0]) || "";
            const title = statement || paperTitle || "（无标题的缺口）";
            const paperIds = s.paper_ids || [];

            const head = el("div", { class: "list-item-head gap-score-head" }, [
                el("span", { class: "metric-rank", text: `#${idx + 1}` }),
                el("span", {
                    class: "list-item-title gap-score-title",
                    text: title,
                    title: statement || title,
                }),
                el("span", { class: "badge badge-accent", text: `综合分 ${overall}` }),
            ]);
            item.appendChild(head);

            // 副标题：gap_id 保留但弱化（等宽小字，用户可辨认追溯用）
            item.appendChild(el("div", { class: "gap-score-sub mono small muted", text: `gap_id: ${s.gap_id || ""}` }));

            // 关联论文徽章（可点击跳转论文页）
            if (paperIds.length) {
                const papersRow = el("div", { class: "gap-score-papers" });
                papersRow.appendChild(el("span", { class: "gap-row-label", text: "关联论文：" }));
                paperIds.forEach((pid, pi) => {
                    const pTitle = (s.paper_titles && s.paper_titles[pi]) || pid;
                    papersRow.appendChild(el("button", {
                        class: "btn btn-outline btn-sm gap-ev-link",
                        onclick: () => { state.pendingPaperId = pid; setActivePage("papers"); },
                        title: `跳转到论文：${pTitle}`,
                    }, `📄 ${truncateText(pTitle, 28)}`));
                });
                item.appendChild(papersRow);
            }

            if (s.reasoning) {
                item.appendChild(el("div", { class: "metric-reasoning small", text: s.reasoning }));
            }

            // 4 维度条
            const bars = [
                { label: "文献稀缺度", val: dims.literature_scarcity || 0, weight: 0.30 },
                { label: "可填补性", val: dims.fillability || 0, weight: 0.30 },
                { label: "行动清晰度", val: dims.action_clarity || 0, weight: 0.20 },
                { label: "关联强度", val: dims.related_strength || 0, weight: 0.20 },
            ];
            item.appendChild(buildMetricBars(bars));

            // 权重说明
            const weightNote = bars.map(b => `${b.label} ${(b.weight * 100).toFixed(0)}%`).join(" · ");
            item.appendChild(el("div", { class: "metric-weights small muted", text: `权重：${weightNote}` }));

            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderReliabilityScores(scores, summary) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `Discovery 可信度评分（5 维度 + 风险标签，共 ${scores.length} 条）`));
        card.appendChild(el("div", { class: "card-desc" },
            "让评委一眼可见「该发现是否值得实验验证」。综合分 = 0.25×外推安全性 + 0.20×文献密度 + 0.20×机制论证 + 0.20×交叉验证 + 0.15×CI 合理性。"));

        // 顶部汇总（按风险标签聚合）
        if (summary && (summary.avg_reliability != null || summary.by_risk_label)) {
            const byRisk = summary.by_risk_label || {};
            const cells = [
                el("div", { class: "metric-summary-cell" }, [
                    el("div", { class: "metric-summary-label" }, "平均可信度"),
                    el("div", { class: "metric-summary-val", text:
                        summary.avg_reliability != null ? summary.avg_reliability.toFixed(2) : "—" }),
                ]),
            ];
            // 推荐级别计数
            ["✓ 强烈推荐实验验证", "△ 谨慎推荐（建议先小规模验证）", "○ 高风险高回报（需 DFT 计算支撑）", "✗ 不建议直接实验（建议改进机制论证）"].forEach(label => {
                const n = byRisk[label] || 0;
                cells.push(el("div", { class: "metric-summary-cell" }, [
                    el("div", { class: "metric-summary-label" }, label),
                    el("div", { class: "metric-summary-val", text: String(n) }),
                ]));
            });
            card.appendChild(el("div", { class: "metric-summary" }, cells));
        }

        const list = el("div", { class: "metric-list" });
        scores.forEach((s, idx) => {
            const item = el("div", { class: "list-item metric-item" });
            const dims = s.dimensions || {};
            const overall = s.reliability_score != null ? s.reliability_score.toFixed(2) : "0.00";
            const riskCls = (s.risk_label || "").startsWith("✓")
                ? "badge-success"
                : (s.risk_label || "").startsWith("△")
                    ? "badge-warning"
                    : (s.risk_label || "").startsWith("○")
                        ? "badge-info"
                        : "badge-danger";

            const head = el("div", { class: "list-item-head" }, [
                el("span", { class: "metric-rank", text: `#${idx + 1}` }),
                el("span", { class: "list-item-title mono small", text: s.claim_id || "" }),
                el("span", { class: `badge ${riskCls}`, text: s.risk_label || "未评级" }),
                el("span", { class: "badge badge-accent", text: `综合分 ${overall}` }),
            ]);
            item.appendChild(head);

            // 元信息：novelty + LLM confidence
            const meta = el("div", { class: "metric-meta small muted" });
            meta.innerHTML =
                `新颖性：<strong>${escapeHtml(s.novelty || "unknown")}</strong>` +
                (s.llm_confidence != null
                    ? ` · LLM 置信度：<strong>${s.llm_confidence.toFixed(2)}</strong>`
                    : "") +
                (s.score_version ? ` · 评分版本：${escapeHtml(s.score_version)}` : "");
            item.appendChild(meta);

            // 5 维度条
            const bars = [
                { label: "外推安全性", val: dims.extrapolation_safety || 0, weight: 0.25, sub: `风险 ${dims.extrapolation_risk != null ? dims.extrapolation_risk.toFixed(2) : "—"}` },
                { label: "文献密度", val: dims.literature_density || 0, weight: 0.20 },
                { label: "机制论证", val: dims.mechanism_evidence || 0, weight: 0.20 },
                { label: "交叉验证", val: dims.cross_validation_consistency || 0, weight: 0.20 },
                { label: "CI 合理性", val: dims.interval_reasonability || 0, weight: 0.15 },
            ];
            item.appendChild(buildMetricBars(bars));

            const weightNote = bars.map(b => `${b.label} ${(b.weight * 100).toFixed(0)}%`).join(" · ");
            item.appendChild(el("div", { class: "metric-weights small muted", text: `权重：${weightNote}` }));

            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderExpertAssistances(assistances) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `专家辅助包（最近邻工艺 + 性能对照 + DFT 协议 + 实验 protocol，共 ${assistances.length} 条）`));
        card.appendChild(el("div", { class: "card-desc" },
            "针对每条构效关系发现，自动从材料库拉取：① 相似材料合成工艺 ② 性能基准对照表 ③ DFT 计算任务清单 ④ 实验 protocol（温区 / 表征 / 统计设计）。"));

        const list = el("div", { class: "metric-list" });
        assistances.forEach((pkg, idx) => {
            const item = el("div", { class: "list-item metric-item collapsible" });
            const head = el("div", { class: "list-item-head" }, [
                el("span", { class: "metric-rank", text: `#${idx + 1}` }),
                el("span", { class: "list-item-title mono small", text: pkg.claim_id || "" }),
                el("span", { class: "badge badge-neutral", text: pkg.material || "—" }),
                el("span", { class: "badge badge-accent", text:
                    `${(pkg.nearest_neighbor_synthesis || []).length} 工艺` }),
                el("span", { class: "badge badge-neutral", text:
                    `${(pkg.similar_materials_table || []).length} 对照` }),
                el("span", { class: "badge badge-neutral", text:
                    `${(pkg.dft_verification_protocol || {}).tasks ? pkg.dft_verification_protocol.tasks.length : 0} DFT` }),
                el("button", {
                    class: "btn btn-secondary btn-small",
                    onclick: (ev) => {
                        ev.stopPropagation();
                        item.classList.toggle("expanded");
                    },
                }, "展开/收起"),
            ]);
            item.appendChild(head);

            // 默认收起，仅展示摘要
            const summary = el("div", { class: "small muted mt-8" });
            const nn = (pkg.nearest_neighbor_synthesis || [])[0];
            summary.innerHTML = nn
                ? `推荐合成工艺：<strong>${escapeHtml(nn.method || "—")}</strong>` +
                  (nn.temperature ? ` · 温度：${escapeHtml(String(nn.temperature))}` : "") +
                  (nn.source_material ? ` · 借鉴自 ${escapeHtml(nn.source_material)}` : "")
                : "（暂无最近邻工艺数据）";
            item.appendChild(summary);

            // 展开后的详细内容
            const body = el("div", { class: "metric-expert-body" });

            // 1) 最近邻工艺
            const nnSection = el("div", { class: "metric-section" });
            nnSection.appendChild(el("div", { class: "metric-section-title" }, "① 最近邻材料合成工艺"));
            const nnList = pkg.nearest_neighbor_synthesis || [];
            if (nnList.length) {
                const tbl = el("table", { class: "metric-table" });
                tbl.appendChild(el("thead", {}, el("tr", {}, [
                    el("th", {}, "材料"),
                    el("th", {}, "相似度"),
                    el("th", {}, "方法"),
                    el("th", {}, "前驱体"),
                    el("th", {}, "温度"),
                    el("th", {}, "气氛"),
                    el("th", {}, "时长"),
                    el("th", {}, "来源"),
                ])));
                const tbody = el("tbody");
                nnList.forEach(r => {
                    tbody.appendChild(el("tr", {}, [
                        el("td", { class: "mono small" }, escapeHtml(r.source_material || "")),
                        el("td", { text: r.similarity != null ? r.similarity.toFixed(2) : "—" }),
                        el("td", {}, escapeHtml(r.method || "—")),
                        el("td", { class: "small" }, escapeHtml((r.precursors || []).join(", ") || "—")),
                        el("td", { class: "small" }, escapeHtml(String(r.temperature || "—"))),
                        el("td", { class: "small" }, escapeHtml(r.atmosphere || "—")),
                        el("td", { class: "small" }, escapeHtml(r.duration || "—")),
                        el("td", { class: "mono small" }, r.source_paper_id ? `[${escapeHtml(r.source_paper_id)}]` : "—"),
                    ]));
                });
                tbl.appendChild(tbody);
                nnSection.appendChild(tbl);
            } else {
                nnSection.appendChild(el("div", { class: "list-empty" }, "暂无"));
            }
            body.appendChild(nnSection);

            // 2) 类似材料性能对照
            const simSection = el("div", { class: "metric-section" });
            simSection.appendChild(el("div", { class: "metric-section-title" }, "② 类似材料性能对照"));
            const sims = pkg.similar_materials_table || [];
            if (sims.length) {
                const tbl = el("table", { class: "metric-table" });
                tbl.appendChild(el("thead", {}, el("tr", {}, [
                    el("th", {}, "材料"),
                    el("th", {}, "相似度"),
                    el("th", {}, "目标性能"),
                    el("th", {}, "数值"),
                    el("th", {}, "单位"),
                    el("th", {}, "测试条件"),
                    el("th", {}, "来源"),
                ])));
                const tbody = el("tbody");
                sims.forEach(r => {
                    tbody.appendChild(el("tr", {}, [
                        el("td", { class: "mono small" }, escapeHtml(r.material || "")),
                        el("td", { text: r.similarity != null ? r.similarity.toFixed(2) : "—" }),
                        el("td", { class: "small" }, escapeHtml(r.target_property || "—")),
                        el("td", { class: "mono small", text: r.value != null ? String(r.value) : "—" }),
                        el("td", { class: "small" }, escapeHtml(r.unit || "")),
                        el("td", { class: "small" }, escapeHtml(r.condition || "—")),
                        el("td", { class: "mono small" }, r.source_paper_id ? `[${escapeHtml(r.source_paper_id)}]` : "—"),
                    ]));
                });
                tbl.appendChild(tbody);
                simSection.appendChild(tbl);
            } else {
                simSection.appendChild(el("div", { class: "list-empty" }, "暂无"));
            }
            body.appendChild(simSection);

            // 3) DFT protocol
            const dft = pkg.dft_verification_protocol || {};
            const dftSection = el("div", { class: "metric-section" });
            dftSection.appendChild(el("div", { class: "metric-section-title" }, "③ DFT 计算验证协议"));
            if (dft.tasks && dft.tasks.length) {
                dftSection.appendChild(el("div", { class: "small" }, [
                    el("strong", {}, "计算任务："),
                    " ",
                    escapeHtml(dft.tasks.join(" / ")),
                ]));
                if (dft.expected_outputs && dft.expected_outputs.length) {
                    dftSection.appendChild(el("div", { class: "small mt-4" }, [
                        el("strong", {}, "预期产物："),
                        " ",
                        escapeHtml(dft.expected_outputs.join(" / ")),
                    ]));
                }
                const refRows = [
                    dft.reference_space_group ? `参考空间群：${dft.reference_space_group}` : null,
                    dft.reference_lattice_parameters ? `参考晶格参数：${dft.reference_lattice_parameters}` : null,
                    dft.software_recommendations ? `推荐软件：${(dft.software_recommendations || []).join(" / ")}` : null,
                    dft.estimated_cpu_hours ? `预计 CPU·h：${dft.estimated_cpu_hours}` : null,
                ].filter(Boolean);
                if (refRows.length) {
                    dftSection.appendChild(el("div", { class: "small mt-4 muted", text: refRows.join(" · ") }));
                }
                if (dft.notes) {
                    dftSection.appendChild(el("div", { class: "small mt-4 muted", text: dft.notes }));
                }
            } else if (dft.warning) {
                dftSection.appendChild(el("div", { class: "list-empty" }, dft.warning));
            } else {
                dftSection.appendChild(el("div", { class: "list-empty" }, "暂无"));
            }
            body.appendChild(dftSection);

            // 4) 实验 protocol
            const exp = pkg.experiment_protocol || {};
            const expSection = el("div", { class: "metric-section" });
            expSection.appendChild(el("div", { class: "metric-section-title" }, "④ 实验 Protocol"));
            if (exp.synthesis) {
                const syn = exp.synthesis;
                expSection.appendChild(el("div", { class: "small mt-4" }, [
                    el("strong", {}, "合成："),
                    " 方法：",
                    escapeHtml(syn.method_recommendation || "—"),
                    syn.atmosphere ? ` · 气氛：${escapeHtml(syn.atmosphere)}` : "",
                    syn.post_treatment ? ` · 后处理：${escapeHtml(syn.post_treatment)}` : "",
                    syn.form ? ` · 形态：${escapeHtml(syn.form)}` : "",
                ]));
            }
            if (exp.characterization && exp.characterization.length) {
                expSection.appendChild(el("div", { class: "small mt-4" }, [
                    el("strong", {}, "表征："),
                    " " + escapeHtml(exp.characterization.join(" · ")),
                ]));
            }
            if (exp.performance_test) {
                const pt = exp.performance_test;
                const rng = (pt.temperature_range_K || []).length === 2
                    ? `${pt.temperature_range_K[0]}–${pt.temperature_range_K[1]} K（步长 ${pt.temperature_step_K} K）`
                    : "—";
                expSection.appendChild(el("div", { class: "small mt-4" }, [
                    el("strong", {}, "性能测试："),
                    ` 目标=${escapeHtml(pt.target_property || "—")}` +
                    (pt.target_unit ? `（${escapeHtml(pt.target_unit)}）` : "") +
                    ` · 温区=${rng}` +
                    (pt.instruments && pt.instruments.length
                        ? ` · 仪器=${escapeHtml(pt.instruments.join(" / "))}`
                        : "") +
                    (pt.estimated_time_per_sample
                        ? ` · 单样时长=${escapeHtml(pt.estimated_time_per_sample)}`
                        : ""),
                ]));
            }
            if (exp.controls && exp.controls.length) {
                expSection.appendChild(el("div", { class: "small mt-4" }, [
                    el("strong", {}, "对照："),
                    " " + escapeHtml(exp.controls.join(" · ")),
                ]));
            }
            if (exp.statistical_design) {
                const sd = exp.statistical_design;
                expSection.appendChild(el("div", { class: "small mt-4 muted", text:
                    `统计设计：${sd.sample_count || ""} · ${sd.uncertainty_quantification || ""} · ${sd.publication_threshold || ""}` }));
            }
            if (exp.duration_estimate_weeks) {
                expSection.appendChild(el("div", { class: "small mt-4 muted", text:
                    `预计周期：${escapeHtml(exp.duration_estimate_weeks)}` }));
            }
            body.appendChild(expSection);

            // 默认折叠（仅 head + summary 显示），点击 button 才展开
            body.style.display = "none";
            head.querySelector("button").addEventListener("click", (ev) => {
                ev.stopPropagation();
                const shown = body.style.display !== "none";
                body.style.display = shown ? "none" : "block";
            });
            item.appendChild(body);

            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function buildMetricBars(bars) {
        const wrap = el("div", { class: "metric-bars" });
        bars.forEach(b => {
            const row = el("div", { class: "metric-bar-row" });
            row.appendChild(el("span", { class: "metric-bar-label", text: b.label }));
            const track = el("div", { class: "metric-bar-track" });
            const pct = Math.max(0, Math.min(100, Math.round((b.val || 0) * 100)));
            const fill = el("span", { class: "metric-bar-fill" });
            fill.style.width = `${pct}%`;
            // 颜色：≥0.7 绿 / 0.4-0.7 蓝 / <0.4 橙红
            if (b.val >= 0.7) fill.style.background = "linear-gradient(90deg, #27ae60, #5dd39e)";
            else if (b.val >= 0.4) fill.style.background = "linear-gradient(90deg, #2f80ed, #6ea8f5)";
            else fill.style.background = "linear-gradient(90deg, #e67e22, #f0a35e)";
            track.appendChild(fill);
            row.appendChild(track);
            row.appendChild(el("span", { class: "metric-bar-val mono", text: b.val.toFixed(2) }));
            if (b.sub) {
                row.appendChild(el("span", { class: "metric-bar-sub small muted", text: b.sub }));
            }
            wrap.appendChild(row);
        });
        return wrap;
    }

    function renderDiscoverySummary(summary) {
        const wrap = el("div", { class: "counts-grid" });
        const items = [
            { label: "假设", value: summary.hypotheses, extra: "hypothesis_seed 生成" },
            { label: "候选", value: summary.candidates, extra: "search_space 候选" },
            { label: "发现", value: summary.relationships, extra: "验证通过的构效关系" },
            { label: "Novel", value: summary.novel, extra: "新颖发现数" },
        ];
        items.forEach(it => {
            wrap.appendChild(el("div", { class: "count-card" }, [
                el("div", { class: "count-label", text: it.label }),
                el("div", { class: "count-value", text: String(it.value != null ? it.value : 0) }),
                el("div", { class: "count-extra", text: it.extra }),
            ]));
        });
        return wrap;
    }

    function renderHypothesisScores(hypList) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `假设可验证性评分（按综合分排序，共 ${hypList.length} 个）`));
        card.appendChild(el("div", { class: "card-desc" },
            "三维评分：新颖性（与已有文献差异）· 可行性（变量可量化/可搜索验证）· 缺口关联度（与 Research Gap 匹配）。综合分 = 0.4×新颖性 + 0.3×可行性 + 0.3×缺口关联度。"));

        const list = el("div", { class: "list" });
        hypList.forEach((h, idx) => {
            const item = el("div", { class: "list-item hy-item" });
            // 头部：排名 + 假设 + 综合分
            const head = el("div", { class: "hy-head" });
            head.appendChild(el("span", { class: "hy-rank", text: `#${idx + 1}` }));
            head.appendChild(el("div", { class: "hy-main" }, [
                el("div", { class: "hy-text", text: h.hypothesis || "" }),
                el("div", { class: "hy-meta" },
                    `目标性能：${escapeHtml(h.target_property || "—")}` +
                    (h.variables && h.variables.length ? ` · 变量：${escapeHtml(h.variables.join(" / "))}` : "") +
                    (h.gap_ref ? ` · Gap：${escapeHtml(String(h.gap_ref).slice(0, 24))}` : "")),
                h.rationale ? el("div", { class: "hy-rationale", text: h.rationale }) : null,
            ]));
            head.appendChild(el("div", { class: "hy-overall" }, [
                el("div", { class: "hy-overall-val", text: h.overall_score != null ? h.overall_score.toFixed(2) : "0.00" }),
                el("div", { class: "hy-overall-label" }, "综合分"),
            ]));
            item.appendChild(head);
            // 三维评分条
            const bars = [
                { label: "新颖性", val: h.novelty_score || 0 },
                { label: "可行性", val: h.feasibility_score || 0 },
                { label: "缺口关联度", val: h.gap_relevance_score || 0 },
            ];
            const barWrap = el("div", { class: "hy-bars" });
            bars.forEach(b => {
                const row = el("div", { class: "rec-popbar" });
                row.appendChild(el("span", { class: "rec-popbar-label", text: b.label }));
                const track = el("div", { class: "rec-popbar-track" });
                const pct = Math.max(0, Math.min(100, Math.round((b.val || 0) * 100)));
                const fill = el("span", { class: "rec-popbar-fill" });
                fill.style.width = `${pct}%`;
                // 高分绿、中分蓝、低分橙红
                if (b.val >= 0.7) fill.style.background = "linear-gradient(90deg, #27ae60, #5dd39e)";
                else if (b.val >= 0.4) fill.style.background = "linear-gradient(90deg, #2f80ed, #6ea8f5)";
                else fill.style.background = "linear-gradient(90deg, #e67e22, #f0a35e)";
                track.appendChild(fill);
                row.appendChild(track);
                row.appendChild(el("span", { class: "rec-popbar-val", text: b.val.toFixed(2) }));
                barWrap.appendChild(row);
            });
            item.appendChild(barWrap);
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderDiscoveryNodes(nodes) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "Discovery 节点执行状态"));
        const tl = el("div", { class: "timeline" });
        nodes.forEach(n => {
            const status = n.status || "unknown";
            let cls = "";
            if (status === "success" || status === "completed") cls = "success";
            else if (status === "failed") cls = "failed";
            else if (status === "pending_human" || status === "pending_review") cls = "pending";
            else if (status === "skipped") cls = "skipped";
            const item = el("div", { class: `timeline-item ${cls}` });
            item.appendChild(el("div", { class: "timeline-head" }, [
                el("span", { class: "timeline-node-id", text: n.node_id || "?" }),
                el("span", { class: "timeline-node-type", text: status }),
            ]));
            item.appendChild(el("div", { class: "timeline-summary" },
                n.summary || "（无摘要）"));
            tl.appendChild(item);
        });
        card.appendChild(tl);
        return card;
    }

    function renderRelationships(relationships) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `构效关系发现（${relationships.length} 条）`));
        if (!relationships.length) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无构效关系发现，请先启动发现工作流"));
            return card;
        }
        const list = el("div", { class: "list" });
        relationships.forEach(r => {
            const item = el("div", { class: "list-item" });
            item.appendChild(el("div", { class: "list-item-head" }, [
                el("span", { class: "list-item-title", text: r.statement || "(无陈述)" }),
                el("span", { class: "badge badge-neutral",
                    text: `证据 ${(r.evidence_refs || []).length}` }),
            ]));
            item.insertAdjacentHTML("beforeend",
                `<div class="mt-8">${statusBadge(r.status)}</div>`);
            item.appendChild(el("div", { class: "list-item-meta mt-8",
                text: formatTime(r.created_at) }));

            item.addEventListener("click", () => {
                const expanded = item.classList.toggle("expanded");
                if (expanded && !item.querySelector(".list-item-body")) {
                    const body = el("div", { class: "list-item-body" });
                    body.innerHTML = `
                        <dl>
                            <dt>Claim ID</dt><dd class="mono">${escapeHtml(r.claim_id)}</dd>
                            <dt>陈述</dt><dd>${escapeHtml(r.statement)}</dd>
                            <dt>状态</dt><dd>${escapeHtml(r.status)}</dd>
                            <dt>创建时间</dt><dd class="mono">${escapeHtml(formatTime(r.created_at))}</dd>
                        </dl>
                        ${r.evidence_refs && r.evidence_refs.length
                            ? `<div class="mt-8"><strong>证据引用：</strong></div><pre class="code-block">${escapeHtml(JSON.stringify(r.evidence_refs, null, 2))}</pre>`
                            : ""}
                    `;
                    // 证据引用：可点击跳转（替换原 JSON <pre> 块，提升可读性）
                    if (r.evidence_refs && r.evidence_refs.length) {
                        const evBox = el("div", { class: "mt-8" });
                        evBox.appendChild(el("strong", {}, "证据引用："));
                        evBox.appendChild(buildEvidenceList(r.evidence_refs));
                        body.appendChild(evBox);
                    }
                    item.appendChild(body);
                }
            });
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    // ===== 6. 灵感笔记页 =====

    async function renderNotes(content) {
        clear(content);
        content.appendChild(el("div", { class: "page-header" }, [
            el("h2", { class: "page-title" }, "灵感笔记"),
            el("p", { class: "page-desc" }, "记录科研过程中的灵感、启发与思考。笔记与项目关联，不参与 Pipeline 自动流转。"),
        ]));

        // 输入卡片
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "新增笔记"));
        card.appendChild(el("label", { class: "field-label", for: "note-input" }, "科研灵感记录"));
        const textarea = el("textarea", {
            class: "textarea",
            id: "note-input",
            placeholder: "记录此刻的想法、对某个 Claim 的质疑、或下一步要尝试的方向…（支持多行，Ctrl+Enter 提交）",
        });
        card.appendChild(textarea);
        card.appendChild(el("div", { class: "btn-row mt-16" }, [
            el("button", {
                class: "btn",
                onclick: async () => {
                    const text = document.getElementById("note-input").value.trim();
                    if (!text) {
                        showToast("笔记内容不能为空", "error");
                        return;
                    }
                    try {
                        await api("POST", `/api/projects/${state.currentProjectId}/notes`, { text });
                        document.getElementById("note-input").value = "";
                        showToast("已保存", "success");
                        renderSidebarNotes();
                        renderPage();
                    } catch (e) {
                        showToast("保存失败：" + e.message, "error");
                    }
                },
            }, "保存笔记"),
            el("button", {
                class: "btn btn-secondary",
                onclick: () => { document.getElementById("note-input").value = ""; },
            }, "清空"),
        ]));
        content.appendChild(card);

        // 笔记列表
        const listCard = el("div", { class: "card" });
        listCard.appendChild(el("div", { class: "card-title" }, "历史笔记"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/notes`);
            const notes = data.notes || [];
            if (!notes.length) {
                listCard.appendChild(el("div", { class: "list-empty" }, "暂无笔记"));
            } else {
                notes.slice().reverse().forEach(n => {
                    listCard.appendChild(el("div", { class: "note-item" }, [
                        el("p", { class: "note-text", text: n.text }),
                        el("div", { class: "note-time", text: formatTime(n.created_at) }),
                    ]));
                });
            }
        } catch (e) {
            listCard.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
        content.appendChild(listCard);
    }

    // ===== 6b. 侧边栏灵感笔记小组件 =====

    async function renderSidebarNotes() {
        const widget = document.getElementById("sidebar-notes");
        if (!widget) return;
        if (!state.currentProjectId) {
            widget.style.display = "none";
            return;
        }
        widget.style.display = "";
        const listEl = document.getElementById("sidebar-notes-list");
        if (listEl) clear(listEl);
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/notes`);
            const notes = (data.notes || []).slice().reverse().slice(0, 3);
            setBadge("badge-notes", (data.notes || []).length || null);
            if (listEl) {
                if (!notes.length) {
                    listEl.appendChild(el("div", { class: "sidebar-notes-empty" }, "暂无灵感记录"));
                } else {
                    notes.forEach(n => {
                        listEl.appendChild(el("div", { class: "sidebar-note-item" }, [
                            el("div", { class: "sidebar-note-text", text: n.text }),
                            el("div", { class: "sidebar-note-time", text: formatTime(n.created_at) }),
                        ]));
                    });
                }
            }
        } catch (e) {
            // 静默
        }
    }

    async function saveSidebarNote() {
        if (!state.currentProjectId) return;
        const input = document.getElementById("sidebar-note-input");
        const text = (input && input.value || "").trim();
        if (!text) {
            showToast("笔记内容不能为空", "error");
            return;
        }
        try {
            await api("POST", `/api/projects/${state.currentProjectId}/notes`, { text });
            if (input) input.value = "";
            showToast("灵感已记录", "success");
            renderSidebarNotes();
        } catch (e) {
            showToast("保存失败：" + e.message, "error");
        }
    }

    // ===== 7. 人工节点交互页 =====

    function renderHuman(content) {
        const data = state.statusCache;
        const pending = data && data.pending_human;

        content.appendChild(el("div", { class: "page-header" }, [
            el("h2", { class: "page-title" }, "人工节点交互"),
            el("p", { class: "page-desc" },
                "当 Pipeline 遇到人工节点时，请求会显示在此页面。提交响应后 Pipeline 将继续执行。"),
        ]));

        if (!pending) {
            state.humanDraft = ""; // 无等待请求，清空草稿
            content.appendChild(el("div", { class: "human-empty" }, [
                el("div", { text: "当前无等待中的人工节点请求。", class: "mb-0" }),
                el("div", { class: "small muted mt-8" },
                    `Pipeline 状态：${data ? data.status : "未知"}`),
            ]));
            const card = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "提示"),
                el("p", { class: "muted small" },
                    "此页面会自动每 2 秒检查一次是否有人工节点请求，无需手动刷新。"),
                el("div", { class: "btn-row mt-16" }, [
                    el("button", { class: "btn btn-secondary", onclick: () => pollStatus() }, "立即刷新"),
                ]),
            ]);
            content.appendChild(card);
            return;
        }

        // 有 pending 请求：渲染 prompt / options / textarea / 提交按钮
        const prompt = pending.prompt || "(无 prompt)";
        const options = Array.isArray(pending.options) ? pending.options : [];
        const allowFreeText = pending.allow_free_text !== false;
        const ctxDict = pending.context || {};
        // 检索范围配置（确认检索方向节点会下发 search_prefs）
        const searchPrefs = ctxDict.search_prefs || null;
        const savedPrefs = (data && data.search_prefs) || state.searchPrefs || {};

        const card = el("div", { class: "card human-card" });

        // Prompt 文本
        card.appendChild(el("div", { class: "card-title" }, "Pipeline 等待你的输入"));
        const promptBox = el("pre", { class: "human-prompt", text: prompt });
        card.appendChild(promptBox);

        // 检索范围配置表单（年份区间/期刊关键词）——确认检索方向节点
        if (searchPrefs) {
            const prefsBox = el("div", { class: "human-context mt-12 search-prefs-box" });
            prefsBox.appendChild(el("div", { class: "field-label mb-8" }, "📌 检索范围配置（抓取时按此过滤，可留空不限）"));

            const yMin = el("input", {
                class: "input paper-filter-year",
                type: "number", min: "1000", max: "2100",
                placeholder: "起始年（如 2018）",
                value: searchPrefs.year_min != null ? String(searchPrefs.year_min) : (savedPrefs.year_min || ""),
            });
            const yMax = el("input", {
                class: "input paper-filter-year",
                type: "number", min: "1000", max: "2100",
                placeholder: "结束年（如 2025）",
                value: searchPrefs.year_max != null ? String(searchPrefs.year_max) : (savedPrefs.year_max || ""),
            });
            const vHint = el("input", {
                class: "input paper-filter-input",
                type: "text",
                placeholder: "期刊/venue 关键词（如 Nature、ACS，多个用空格）",
                value: searchPrefs.venue_hint != null ? searchPrefs.venue_hint : (savedPrefs.venue_hint || ""),
            });

            const row1 = el("div", { class: "paper-filter-row" });
            row1.appendChild(el("span", { class: "paper-filter-label", text: "年份" }));
            row1.appendChild(yMin);
            row1.appendChild(el("span", { class: "paper-filter-sep", text: "–" }));
            row1.appendChild(yMax);
            row1.appendChild(el("span", { class: "paper-filter-label", text: "期刊" }));
            row1.appendChild(vHint);
            prefsBox.appendChild(row1);
            prefsBox.appendChild(el("div", { class: "small muted mt-8" },
                "抓取阶段会按此范围向 arXiv / Semantic Scholar 下发年份过滤；期刊关键词在拿到元数据后过滤。留空则不限。"));

            // 保存到模块级，提交时随 human-response 携带
            state.searchPrefsForm = { yMin, yMax, vHint };
            card.appendChild(prefsBox);
        }

        // 上下文信息（如推荐主题、节点 ID 等）
        const ctxKeys = Object.keys(ctxDict);
        if (ctxKeys.length) {
            const ctxBox = el("div", { class: "human-context mt-12" });
            ctxBox.appendChild(el("div", { class: "small muted mb-8" }, "上下文信息："));
            ctxKeys.forEach(k => {
                ctxBox.appendChild(el("div", { class: "small" },
                    `${k}: ${typeof ctxDict[k] === "object" ? JSON.stringify(ctxDict[k]) : String(ctxDict[k])}`));
            });
            card.appendChild(ctxBox);
        }

        // 选项按钮（若有）
        let selectedOption = null; // 当前选中的 option
        if (options.length) {
            const optsBox = el("div", { class: "human-options mt-16" });
            optsBox.appendChild(el("div", { class: "field-label mb-8" }, "选择一项："));
            options.forEach((opt, i) => {
                const optLabel = typeof opt === "string" ? opt : (opt.label || opt.value || JSON.stringify(opt));
                const optValue = typeof opt === "string" ? opt : (opt.value || optLabel);
                const btn = el("button", {
                    class: "btn btn-outline human-option-btn",
                    text: `[${i + 1}] ${optLabel}`,
                    onclick: () => {
                        selectedOption = optValue;
                        // 视觉选中态
                        optsBox.querySelectorAll(".human-option-btn").forEach(b => b.classList.remove("btn-success"));
                        btn.classList.add("btn-success");
                    },
                });
                optsBox.appendChild(btn);
            });
            card.appendChild(optsBox);
        }

        // 自由文本输入
        let textarea = null;
        if (allowFreeText) {
            const taField = el("div", { class: "field mt-16" }, [
                el("label", { class: "field-label", for: "human-response-input" },
                    options.length ? "或输入自定义响应：" : "输入响应："),
            ]);
            textarea = el("textarea", {
                class: "textarea",
                id: "human-response-input",
                placeholder: "请输入你的响应...",
                rows: 4,
            });
            // 恢复草稿（避免轮询重建时丢失输入）
            textarea.value = state.humanDraft || "";
            textarea.addEventListener("input", () => {
                state.humanDraft = textarea.value;
            });
            taField.appendChild(textarea);
            card.appendChild(taField);
        }

        // 提交按钮
        const actionRow = el("div", { class: "btn-row mt-16" }, [
            el("button", {
                class: "btn btn-success",
                id: "human-submit-continue",
                onclick: () => submitHumanResponse("continue", selectedOption, textarea),
            }, "提交并继续"),
            el("button", {
                class: "btn btn-secondary",
                id: "human-submit-abort",
                onclick: () => submitHumanResponse("abort", null, null),
            }, "中止 Pipeline"),
        ]);
        card.appendChild(actionRow);

        content.appendChild(card);
    }

    async function submitHumanResponse(action, selectedOption, textarea) {
        if (!state.currentProjectId) return;
        const text = textarea ? textarea.value.trim() : "";
        if (action === "continue" && !text && !selectedOption) {
            showToast("请填写响应内容或选择一个选项", "error");
            return;
        }
        // 检索范围配置：收集表单值（如有）
        let searchPrefs = null;
        const form = state.searchPrefsForm;
        if (form) {
            const yMinRaw = (form.yMin.value || "").trim();
            const yMaxRaw = (form.yMax.value || "").trim();
            const vHint = (form.vHint.value || "").trim();
            searchPrefs = {
                year_min: yMinRaw ? Number(yMinRaw) : null,
                year_max: yMaxRaw ? Number(yMaxRaw) : null,
                venue_hint: vHint || "",
            };
            state.searchPrefs = searchPrefs;
        }
        const payload = { action, text, selected_option: selectedOption };
        if (searchPrefs) payload.search_prefs = searchPrefs;
        try {
            await api("POST", `/api/projects/${state.currentProjectId}/human-response`, payload);
            state.humanDraft = "";
            state.humanFingerprint = null;
            showToast(action === "abort" ? "Pipeline 已中止" : "已提交响应，Pipeline 继续执行", "success");
            await pollStatus();
            renderPage();
        } catch (e) {
            showToast("提交失败：" + e.message, "error");
        }
    }

    // ===== 6c. 文献调研报告页（赛题基本任务核心产出）=====

    async function renderResearchReport(content) {
        content.appendChild(el("div", { class: "loading" }, "加载调研报告…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/research-report`);
            state.researchReportCache = data;
            clear(content);

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "文献调研报告"),
                el("p", { class: "page-desc" },
                    "赛题基本任务核心产出：基于多源文献（arxiv + S2 + Sciverse）的交叉验证报告，含 Research Gaps、共识、冲突。"),
            ]));

            const report = data.report;
            if (!report) {
                content.appendChild(el("div", { class: "card" }, [
                    el("div", { class: "list-empty" },
                        data.message || "尚未生成调研报告，请先启动 Pipeline 或构效关系发现"),
                    el("div", { class: "btn-row mt-16" }, [
                        el("button", {
                            class: "btn btn-accent",
                            onclick: () => startDiscovery(),
                        }, "启动构效关系发现"),
                        el("button", {
                            class: "btn btn-success",
                            onclick: () => startPipeline(),
                        }, "启动 Pipeline"),
                    ]),
                ]));
                return;
            }

            // 顶部统计
            content.appendChild(el("div", { class: "counts-grid" }, [
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "Research Gaps"),
                    el("div", { class: "count-value", text: String((report.gaps || []).length) }),
                    el("div", { class: "count-extra" }, "未被充分探索的方向"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "共识"),
                    el("div", { class: "count-value", text: String((report.consensus || []).length) }),
                    el("div", { class: "count-extra" }, "多方一致认同的陈述"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "冲突"),
                    el("div", { class: "count-value", text: String((report.conflicts || []).length) }),
                    el("div", { class: "count-extra" }, "陈述相反的论断"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "整体置信度"),
                    el("div", { class: "count-value", text: (report.overall_confidence || 0).toFixed(2) }),
                    el("div", { class: "count-extra" }, "0~1，低于 0.6 建议补充检索"),
                ]),
            ]));

            // 更新侧边栏 badge（调研报告徽章独立 id，避免与研究缺口冲突）
            setBadge("badge-report-gaps", (report.gaps || []).length || null);

            // Research Gaps（最重要，赛题核心要求）
            const gapsCard = el("div", { class: "card" });
            gapsCard.appendChild(el("div", { class: "card-title" },
                `Research Gaps（${(report.gaps || []).length} 个）· 可证伪识别`));
            const gaps = report.gaps || [];
            if (!gaps.length) {
                gapsCard.appendChild(el("div", { class: "list-empty" }, "未识别到 Research Gap"));
            } else {
                const list = el("div", { class: "list" });
                gaps.forEach((g, i) => {
                    // ===== 结构化 Gap 字段解析（兼容旧版字符串）=====
                    const isStructured = g && typeof g === "object";
                    const gapText = isStructured
                        ? (g.gap || g.description || JSON.stringify(g))
                        : (g || "");
                    const gapType = (isStructured && g.type) ? g.type : "underexplored";
                    const importance = (isStructured && typeof g.importance === "number")
                        ? g.importance : null;
                    const actionability = (isStructured && g.actionability)
                        ? g.actionability : "medium";
                    const citedPids = (isStructured && Array.isArray(g.cited_paper_ids))
                        ? g.cited_paper_ids : [];
                    const citedChunks = (isStructured && Array.isArray(g.cited_chunk_ids))
                        ? g.cited_chunk_ids : [];
                    const rationale = (isStructured && g.rationale) ? g.rationale : "";

                    const item = el("div", { class: "list-item gap-item" });

                    // 头部：序号 + 标题 + type 彩色徽章
                    const headChildren = [
                        el("span", { class: "gap-number", text: `#${i + 1}` }),
                        el("span", { class: "list-item-title", text: gapText }),
                    ];
                    // type 徽章
                    headChildren.push(el("span", {
                        class: `gap-type-badge gap-type-${gapType}`,
                        text: gapType,
                        title: `Research Gap 类型：${gapType}`,
                    }));
                    // actionability 徽章
                    headChildren.push(el("span", {
                        class: `gap-actionability gap-action-${actionability}`,
                        text: `可操作 ${actionability}`,
                        title: `可操作性：${actionability}（高/中/低）`,
                    }));
                    // importance 显示
                    if (importance !== null) {
                        headChildren.push(el("span", {
                            class: "gap-importance mono small muted",
                            text: `重要性 ${importance.toFixed(2)}`,
                            title: "重要性 0~1",
                        }));
                    }
                    item.appendChild(el("div", { class: "list-item-head" }, headChildren));

                    // rationale
                    if (rationale) {
                        item.appendChild(el("div", { class: "gap-rationale small" },
                            `依据：${rationale}`));
                    }

                    // evidence（结构化：可点击 paper_id）
                    if (citedPids.length) {
                        const evBox = el("div", { class: "gap-evidence mt-8" });
                        evBox.appendChild(el("span", { class: "gap-evidence-label small muted", text: "证据链 paper_id：" }));
                        citedPids.slice(0, 5).forEach(pid => {
                            const link = el("span", {
                                class: "gap-paper-badge badge badge-info",
                                text: String(pid).length > 16 ? String(pid).slice(0, 14) + "…" : String(pid),
                                title: `点击跳转到论文浏览页：${pid}\n${citedChunks.length ? `含 chunk: ${citedChunks.length}` : ""}`,
                                onclick: (ev) => {
                                    ev.stopPropagation();
                                    state.pendingPaperId = pid;
                                    setActivePage("papers");
                                },
                            });
                            link.style.cursor = "pointer";
                            evBox.appendChild(link);
                        });
                        if (citedPids.length > 5) {
                            evBox.appendChild(el("span", {
                                class: "small muted",
                                text: ` 等 ${citedPids.length} 篇`,
                            }));
                        }
                        item.appendChild(evBox);
                    } else {
                        // 旧版兼容：从 evidence/source_papers 字段提取
                        const legacy = isStructured
                            ? (g.evidence || g.source_papers || [])
                            : [];
                        if (legacy.length) {
                            const evBox = el("div", { class: "gap-evidence mt-8" });
                            evBox.appendChild(el("span", {
                                class: "small muted",
                                text: `证据来源（旧版）：${legacy.slice(0, 3).join("、")}${legacy.length > 3 ? ` 等 ${legacy.length} 篇` : ""}`,
                            }));
                            item.appendChild(evBox);
                        }
                    }

                    // 可展开：展示完整字段
                    item.addEventListener("click", () => {
                        if (item.classList.contains("expanded")) {
                            item.classList.remove("expanded");
                            const body = item.querySelector(".list-item-body");
                            if (body) body.remove();
                            return;
                        }
                        item.classList.add("expanded");
                        const body = el("div", { class: "list-item-body" });
                        body.innerHTML = `
                            <dl>
                                <dt>Gap</dt><dd>${escapeHtml(gapText)}</dd>
                                <dt>类型</dt><dd><span class="gap-type-badge gap-type-${gapType}">${escapeHtml(gapType)}</span></dd>
                                <dt>重要性</dt><dd>${importance !== null ? importance.toFixed(2) : "—"}</dd>
                                <dt>可操作性</dt><dd><span class="gap-actionability gap-action-${actionability}">${escapeHtml(actionability)}</span></dd>
                                <dt>关联 paper_id</dt><dd class="mono">${citedPids.length ? escapeHtml(citedPids.join(", ")) : "—"}</dd>
                                <dt>关联 chunk_id</dt><dd class="mono">${citedChunks.length ? escapeHtml(citedChunks.join(", ")) : "—"}</dd>
                                <dt>依据</dt><dd>${rationale ? escapeHtml(rationale) : "—"}</dd>
                            </dl>
                        `;
                        item.appendChild(body);
                    });
                    list.appendChild(item);
                });
                gapsCard.appendChild(list);
            }
            content.appendChild(gapsCard);

            // 共识
            const consCard = el("div", { class: "card" });
            consCard.appendChild(el("div", { class: "card-title" },
                `共识（${(report.consensus || []).length} 条）`));
            const cons = report.consensus || [];
            if (!cons.length) {
                consCard.appendChild(el("div", { class: "list-empty" }, "暂无共识"));
            } else {
                const ul = el("ul", { class: "feature-list" });
                cons.forEach(c => {
                    const text = typeof c === "string" ? c : (c.statement || c.consensus || JSON.stringify(c));
                    ul.appendChild(el("li", {}, text));
                });
                consCard.appendChild(ul);
            }
            content.appendChild(consCard);

            // 冲突
            const confCard = el("div", { class: "card" });
            confCard.appendChild(el("div", { class: "card-title" },
                `冲突结论（${(report.conflicts || []).length} 处）· 处置建议`));
            const confs = report.conflicts || [];
            if (!confs.length) {
                confCard.appendChild(el("div", { class: "list-empty" }, "暂无冲突"));
            } else {
                const list = el("div", { class: "list" });
                confs.forEach((c, i) => {
                    const item = el("div", { class: "list-item conflict-item" });
                    let summary, suggestion, sources, positions;
                    if (typeof c === "string") {
                        summary = c; suggestion = ""; sources = []; positions = [];
                    } else {
                        summary = c.summary || c.conflict || c.description || c.topic || c.claim || "";
                        suggestion = c.suggestion || c.resolution || c.disposition || "";
                        sources = c.sources || c.papers || [];
                        positions = c.positions || c.sides || [];
                        // 若仍无 summary，把对象的 key-value 拼成可读文本（避免 JSON.stringify）
                        if (!summary) {
                            const parts = [];
                            for (const [k, v] of Object.entries(c)) {
                                if (typeof v === "string" && v.length < 200) {
                                    parts.push(`${k}: ${v}`);
                                }
                            }
                            summary = parts.join("；") || "(冲突详情无法解析)";
                        }
                    }
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "conflict-number", text: `#${i + 1}` }),
                        el("span", { class: "list-item-title", text: summary }),
                    ]));
                    // 冲突双方立场列表（若存在）
                    if (positions && positions.length) {
                        const posList = el("ul", { class: "conflict-positions" });
                        positions.forEach(p => {
                            posList.appendChild(el("li", { class: "small", text: typeof p === "string" ? p : (p.position || p.side || JSON.stringify(p)) }));
                        });
                        item.appendChild(posList);
                    }
                    if (suggestion) {
                        item.appendChild(el("div", { class: "conflict-suggestion" },
                            `处置：${suggestion}`));
                    }
                    if (sources && sources.length) {
                        item.appendChild(el("div", { class: "gap-evidence" },
                            `涉及文献：${sources.slice(0, 3).join("、")}`));
                    }
                    // 结构化 source_paper_ids（赛题证据链增强）
                    const confPids = (typeof c === "object" && Array.isArray(c.source_paper_ids))
                        ? c.source_paper_ids : [];
                    if (confPids.length) {
                        const evBox = el("div", { class: "gap-evidence mt-8" });
                        evBox.appendChild(el("span", { class: "small muted", text: "证据 paper_id： " }));
                        confPids.slice(0, 5).forEach(pid => {
                            const link = el("span", {
                                class: "gap-paper-badge badge badge-info",
                                text: String(pid).length > 16 ? String(pid).slice(0, 14) + "…" : String(pid),
                                title: `点击跳转到论文浏览页：${pid}`,
                                onclick: (ev) => {
                                    ev.stopPropagation();
                                    state.pendingPaperId = pid;
                                    setActivePage("papers");
                                },
                            });
                            link.style.cursor = "pointer";
                            evBox.appendChild(link);
                        });
                        item.appendChild(evBox);
                    }
                    list.appendChild(item);
                });
                confCard.appendChild(list);
            }
            content.appendChild(confCard);

            // 操作区
            content.appendChild(el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "后续操作"),
                el("div", { class: "btn-row" }, [
                    el("button", {
                        class: "btn btn-accent",
                        onclick: () => startDiscovery(),
                    }, "基于 Gap 启动构效关系发现"),
                    el("button", {
                        class: "btn btn-secondary",
                        onclick: () => setActivePage("papers"),
                    }, "查看论文详情"),
                    el("button", {
                        class: "btn btn-secondary",
                        onclick: () => renderResearchReport(content),
                    }, "刷新报告"),
                ]),
            ]));
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 6d. 构效关系发现详情页（MCTS 可视化 + 散点图 + Novel 高亮 + 证据溯源链）=====

    async function renderDiscoveryDetail(content) {
        content.appendChild(el("div", { class: "loading" }, "加载发现详情…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/discovery-detail`);
            state.discoveryDetailCache = data;
            clear(content);

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "构效关系发现详情"),
                el("p", { class: "page-desc" },
                    "路线 A 完整产出：MCTS 搜索过程可视化 · 文献数据点散点图 · Novel 发现高亮 · 证据溯源链 · Materials 交叉验证。"),
            ]));

            const summary = data.discovery_summary || {};
            const trace = data.discovery_search_trace || {};
            const litPoints = data.discovery_literature_points || [];
            const relationships = data.discovery_relationships || [];
            const hypotheses = data.discovery_hypotheses || [];
            const searchSpace = data.discovery_search_space || {};
            const symbolicFit = data.discovery_symbolic_regression || {};
            const calibration = data.discovery_surrogate_calibration || {};

            // 1. 计数卡片
            content.appendChild(el("div", { class: "counts-grid" }, [
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "假设"),
                    el("div", { class: "count-value", text: String(summary.hypotheses || 0) }),
                    el("div", { class: "count-extra" }, "hypothesis_seed 生成"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "MCTS 候选"),
                    el("div", { class: "count-value", text: String(summary.candidates || 0) }),
                    el("div", { class: "count-extra" }, "通过评估的候选"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "验证发现"),
                    el("div", { class: "count-value", text: String(summary.relationships || 0) }),
                    el("div", { class: "count-extra" }, "验证通过的构效关系"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "Novel"),
                    el("div", { class: "count-value novel", text: String(summary.novel || 0) }),
                    el("div", { class: "count-extra" }, "新颖发现数"),
                ]),
            ]));

            // 2. MCTS 搜索过程可视化
            content.appendChild(renderMctsTrace(trace));

            // 2b. 符号回归（第二搜索算法）
            content.appendChild(renderSymbolicRegression(symbolicFit));

            // 2c. 代理模型-数据库校准（性能评估闭环）
            content.appendChild(renderSurrogateCalibration(calibration));

            // 3. 文献数据点散点图（SVG）
            content.appendChild(renderLiteratureScatter(litPoints, searchSpace));

            // 4. 验证发现列表（含 Novel 高亮 + 证据溯源链 + 交叉验证）
            content.appendChild(renderRelationshipsDetail(relationships));

            // 5. 候选假设列表
            if (hypotheses.length) {
                content.appendChild(renderHypothesesList(hypotheses));
            }

            // 6. 搜索空间定义
            content.appendChild(renderSearchSpace(searchSpace));

            // 7. 发现报告 Markdown 预览
            if (data.discovery_report_content) {
                content.appendChild(renderReportPreview(data.discovery_report_content));
            }
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    function renderSymbolicRegression(fit) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            "符号回归（Symbolic Regression）· 第二搜索算法"));

        if (!fit || Object.keys(fit).length === 0) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无符号回归数据（运行 discovery 流程后自动生成）"));
            return card;
        }

        const fitted = fit.fitted;
        const desc = el("div", { class: "small muted mb-8" },
            "从文献数据点直接拟合解析表达式（如 ZT = f(组成, 温度)），" +
            "与 MCTS 互补：MCTS 在配置空间搜索，符号回归给出可解释公式。");
        card.appendChild(desc);

        if (!fitted) {
            card.appendChild(el("div", { class: "status-banner warning" },
                fit.note || "符号回归未成功拟合"));
            return card;
        }

        // 拟合结果
        const body = el("div", { class: "list-item-body" });
        body.innerHTML = `
            <dl>
                <dt>拟合表达式</dt>
                <dd class="mono" style="font-size:14px;line-height:1.6">${escapeHtml(fit.expr_str || "—")}</dd>
                <dt>LaTeX</dt>
                <dd class="mono">${escapeHtml(fit.expr_latex || "—")}</dd>
                <dt>R²（决定系数）</dt>
                <dd><strong>${Number(fit.r2 || 0).toFixed(4)}</strong></dd>
                <dt>MAE（平均绝对误差）</dt>
                <dd>${Number(fit.mae || 0).toFixed(4)}</dd>
                <dt>数据点数</dt>
                <dd>${fit.n_points || 0}</dd>
                <dt>变量</dt>
                <dd>${escapeHtml((fit.variable_names || []).join(", ") || "—")}</dd>
            </dl>
        `;

        // 质量徽章
        const r2 = Number(fit.r2 || 0);
        let badgeCls = "badge-warning";
        let badgeText = "中等拟合";
        if (r2 >= 0.9) { badgeCls = "badge-success"; badgeText = "高质量拟合"; }
        else if (r2 < 0.5) { badgeCls = "badge-danger"; badgeText = "拟合不足"; }
        body.insertAdjacentHTML("afterbegin",
            `<div class="mb-8"><span class="badge ${badgeCls}">${badgeText}</span></div>`);

        card.appendChild(body);
        return card;
    }

    function renderSurrogateCalibration(cal) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            "代理模型-数据库校准（性能评估闭环）"));

        if (!cal || Object.keys(cal).length === 0) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无校准数据（运行 discovery 流程后自动生成）"));
            return card;
        }

        const desc = el("div", { class: "small muted mb-8" },
            "将代理模型（基于文献数据点的加权 KNN）的预测值与 " +
            "Materials Project / OQMD / NOMAD 的 DFT 计算值对比，" +
            "量化系统偏差，使搜索空间有数据库证据支持。");
        card.appendChild(desc);

        if (!cal.calibrated) {
            card.appendChild(el("div", { class: "status-banner warning" },
                cal.note || "代理模型未完成校准"));
            return card;
        }

        // 校准指标概览
        const mae = Number(cal.mae || 0);
        const bias = Number(cal.bias || 0);
        let badgeCls = "badge-success";
        let badgeText = "偏差可接受";
        if (mae > 0.5) { badgeCls = "badge-warning"; badgeText = "偏差较大"; }
        if (mae > 1.0) { badgeCls = "badge-danger"; badgeText = "偏差显著"; }

        const body = el("div", { class: "list-item-body" });
        body.innerHTML = `
            <div class="mb-8">
                <span class="badge ${badgeCls}">${badgeText}</span>
                <span class="badge badge-neutral ml-4">数据源：${escapeHtml((cal.sources_used || []).join(" / ") || "—")}</span>
            </div>
            <dl>
                <dt>校准材料数</dt>
                <dd>${cal.n_matched || 0} / ${cal.n_checked || 0} 匹配到数据库 DFT 值</dd>
                <dt>MAE（平均绝对误差）</dt>
                <dd><strong>${mae.toFixed(4)}</strong></dd>
                <dt>系统偏差（预测 - DFT）</dt>
                <dd>${bias >= 0 ? "+" : ""}${bias.toFixed(4)} ${bias > 0 ? "（代理偏高）" : bias < 0 ? "（代理偏低）" : ""}</dd>
            </dl>
        `;

        // 逐材料对比表
        const perMat = cal.per_material || [];
        if (perMat.length) {
            const tbl = el("div", { class: "mt-8" });
            tbl.appendChild(el("div", { class: "small evidence-title" }, "逐材料对比："));
            const rows = el("div", { class: "list" });
            perMat.forEach(m => {
                const dev = Number(m.deviation || 0);
                const devCls = Math.abs(dev) > 0.5 ? "badge-warning" : "badge-success";
                const row = el("div", { class: "list-item ev-entry" }, [
                    el("span", { class: "mono", text: m.formula || "?" }),
                    el("span", { class: "small muted", text: m.db_source || "" }),
                    el("span", { class: "small", text: `DFT: ${m.db_value}` }),
                    el("span", { class: "small", text: `代理: ${m.surrogate_prediction}` }),
                    el("span", { class: `badge ${devCls}`, text: `偏差 ${dev >= 0 ? "+" : ""}${dev.toFixed(3)}` }),
                ]);
                rows.appendChild(row);
            });
            tbl.appendChild(rows);
            body.appendChild(tbl);
        }

        card.appendChild(body);
        return card;
    }

    function renderMctsTrace(trace) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "MCTS 搜索过程可视化"));

        if (!trace || !trace.trace || !trace.trace.length) {
            card.appendChild(el("div", { class: "list-empty" }, "暂无搜索轨迹数据"));
            return card;
        }

        // 顶部统计
        const stats = el("div", { class: "summary-stats" }, [
            el("div", { class: "summary-stat" }, [
                el("div", { class: "summary-stat-value", text: String(trace.iterations || 0) }),
                el("div", { class: "summary-stat-label" }, "总迭代数"),
            ]),
            el("div", { class: "summary-stat" }, [
                el("div", { class: "summary-stat-value", text: String(trace.evaluated || 0) }),
                el("div", { class: "summary-stat-label" }, "通过评估"),
            ]),
            el("div", { class: "summary-stat" }, [
                el("div", { class: "summary-stat-value", text: String(trace.pruned || 0) }),
                el("div", { class: "summary-stat-label" }, "被剪枝"),
            ]),
        ]);
        card.appendChild(stats);

        // 每轮迭代轨迹
        card.appendChild(el("div", { class: "card-subtitle mt-16" }, "每轮迭代详情"));
        const list = el("div", { class: "mcts-trace-list" });
        trace.trace.forEach(t => {
            const item = el("div", {
                class: `mcts-trace-item ${t.pruned ? "pruned" : "kept"}`,
            });
            item.appendChild(el("div", { class: "mcts-trace-head" }, [
                el("span", { class: "mcts-iter", text: `#${t.iter}` }),
                el("span", {
                    class: `mcts-status ${t.pruned ? "pruned" : "kept"}`,
                    text: t.pruned ? "✗ 剪枝" : "✓ 保留",
                }),
                el("span", { class: "mcts-pred" },
                    `预测 ZT=${format3g(t.predicted_target || 0)}`),
                el("span", { class: "mcts-plaus" },
                    `合理性 ${(t.plausibility || 0).toFixed(2)}`),
            ]));
            // 配置
            const configStr = Object.entries(t.config || {})
                .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : v}`)
                .join(", ");
            item.appendChild(el("div", { class: "mcts-config mono small", text: configStr }));
            if (t.mechanism) {
                item.appendChild(el("div", { class: "mcts-mechanism small" },
                    `机制：${t.mechanism}`));
            }
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function format3g(v) {
        // 兼容老浏览器
        try { return Number(v).toFixed(3); } catch (e) { return "?"; }
    }

    function renderLiteratureScatter(litPoints, searchSpace) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            "文献数据点散点图（代理模型训练样本）"));

        if (!litPoints || !litPoints.length) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无文献数据点。代理模型需要 (结构, 性能) 数据点作为训练样本。"));
            return card;
        }

        // 提取温度与 ZT 用于散点图
        const points = litPoints.map(p => {
            const cfg = p.config || {};
            const temp = cfg.temperature || cfg.T || cfg.temp_K || cfg["Temperature (K)"];
            const target = p.target || p.predicted_target || 0;
            return { temp, target, paper_id: p.paper_id || "", note: p.note || "" };
        }).filter(p => typeof p.temp === "number" && typeof p.target === "number");

        card.appendChild(el("div", { class: "small muted mb-8" },
            `共 ${litPoints.length} 个文献数据点，其中 ${points.length} 个含温度+目标性能（可绘散点图）。每点可追溯到 paper_id。`));

        if (points.length < 2) {
            card.appendChild(el("div", { class: "list-empty" },
                "可绘图数据点不足 2 个，无法生成散点图"));
            // 仍展示原始数据
            const pre = el("pre", { class: "code-block" });
            pre.textContent = JSON.stringify(litPoints, null, 2);
            card.appendChild(pre);
            return card;
        }

        // SVG 散点图
        const W = 560, H = 320, PAD = 50;
        const temps = points.map(p => p.temp);
        const targets = points.map(p => p.target);
        const tMin = Math.min(...temps), tMax = Math.max(...temps);
        const zMin = Math.min(...targets), zMax = Math.max(...targets);
        const tRange = Math.max(1, tMax - tMin);
        const zRange = Math.max(0.01, zMax - zMin);

        const svg = [`<svg viewBox="0 0 ${W} ${H}" class="scatter-svg" xmlns="http://www.w3.org/2000/svg">`];
        // 坐标轴
        svg.push(`<line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="#333" stroke-width="1.5"/>`);
        svg.push(`<line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H - PAD}" stroke="#333" stroke-width="1.5"/>`);
        // 轴标签
        svg.push(`<text x="${W / 2}" y="${H - 12}" text-anchor="middle" font-size="12" fill="#333">温度 (K)</text>`);
        svg.push(`<text x="14" y="${H / 2}" text-anchor="middle" font-size="12" fill="#333" transform="rotate(-90 14 ${H / 2})">目标性能 (ZT)</text>`);
        // 刻度
        for (let i = 0; i <= 4; i++) {
            const x = PAD + (W - 2 * PAD) * i / 4;
            const tVal = tMin + tRange * i / 4;
            svg.push(`<line x1="${x}" y1="${H - PAD}" x2="${x}" y2="${H - PAD + 4}" stroke="#333"/>`);
            svg.push(`<text x="${x}" y="${H - PAD + 16}" text-anchor="middle" font-size="10" fill="#666">${tVal.toFixed(0)}</text>`);
        }
        for (let i = 0; i <= 4; i++) {
            const y = H - PAD - (H - 2 * PAD) * i / 4;
            const zVal = zMin + zRange * i / 4;
            svg.push(`<line x1="${PAD - 4}" y1="${y}" x2="${PAD}" y2="${y}" stroke="#333"/>`);
            svg.push(`<text x="${PAD - 6}" y="${y + 3}" text-anchor="end" font-size="10" fill="#666">${zVal.toFixed(2)}</text>`);
        }
        // 散点
        points.forEach((p, i) => {
            const x = PAD + (W - 2 * PAD) * (p.temp - tMin) / tRange;
            const y = H - PAD - (H - 2 * PAD) * (p.target - zMin) / zRange;
            const title = `温度 ${p.temp}K, ZT=${p.target}\npaper: ${p.paper_id || "?"}\n${p.note || ""}`;
            svg.push(`<circle cx="${x}" cy="${y}" r="6" fill="#1976d2" stroke="#fff" stroke-width="1.5" opacity="0.85"><title>${escapeHtml(title)}</title></circle>`);
        });
        svg.push(`</svg>`);
        card.insertAdjacentHTML("beforeend", svg.join(""));

        // 数据点列表
        card.appendChild(el("div", { class: "card-subtitle mt-16" }, "数据点详情（可追溯到 paper_id，点击徽章跳转）"));
        const list = el("div", { class: "list" });
        points.forEach((p, i) => {
            const item = el("div", { class: "list-item" });
            // paper_id 徽章可点击跳转到论文页
            const paperBadge = p.paper_id
                ? el("span", {
                    class: "badge badge-info",
                    text: `paper ${p.paper_id.slice(0, 8)}`,
                    title: "点击跳转到论文浏览页",
                    onclick: (e) => {
                        e.stopPropagation();
                        state.pendingPaperId = p.paper_id;
                        setActivePage("papers");
                    },
                })
                : el("span", { class: "badge badge-neutral", text: "paper ?" });
            if (p.paper_id) {
                paperBadge.style.cursor = "pointer";
            }
            item.appendChild(el("div", { class: "list-item-head" }, [
                el("span", { class: "list-item-title", text: `数据点 #${i + 1}: ZT=${p.target.toFixed(3)} @ ${p.temp}K` }),
                paperBadge,
            ]));
            if (p.note) {
                item.appendChild(el("div", { class: "small muted mt-8", text: p.note }));
            }
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderRelationshipsDetail(relationships) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `验证后的构效关系发现（${relationships.length} 条）· Novel 高亮 · 证据溯源链`));

        if (!relationships.length) {
            card.appendChild(el("div", { class: "list-empty" },
                "暂无构效关系发现，请先启动发现工作流"));
            return card;
        }

        const list = el("div", { class: "list" });
        relationships.forEach((r, i) => {
            const novelty = r.novelty || "unknown";
            const isNovel = novelty === "novel";
            const cv = r.cross_validation || {};
            const evidenceRefs = r.evidence_refs || r.evidence_paper_ids || [];

            const item = el("div", {
                class: `list-item ${isNovel ? "novel-item" : ""}`,
            });
            item.appendChild(el("div", { class: "list-item-head" }, [
                el("span", { class: "gap-number", text: `#${i + 1}` }),
                el("span", { class: "list-item-title", text: r.relationship || r.statement || "(无陈述)" }),
                el("span", {
                    class: `badge ${isNovel ? "badge-success" : (novelty === "known" ? "badge-neutral" : "badge-info")}`,
                    text: novelty,
                }),
                el("span", { class: "badge badge-neutral", text: `证据 ${evidenceRefs.length}` }),
            ]));

            // 配置与预测
            const config = r.config || {};
            const predTarget = r.predicted_target || r.predicted_ZT || 0;
            const configStr = Object.entries(config)
                .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : v}`)
                .join(", ");
            item.appendChild(el("div", { class: "small mt-8 mono" },
                `配置：${configStr || "—"}`));
            item.appendChild(el("div", { class: "small mt-8" },
                `预测 ZT=${predTarget.toFixed(3)} · 置信度 ${(r.confidence || 0).toFixed(2)}`));

            // 物理机制
            if (r.mechanism) {
                item.appendChild(el("div", { class: "small mt-8 mechanism-text" },
                    `机制：${r.mechanism}`));
            }

            // 新颖性说明
            if (r.novelty_reason) {
                item.appendChild(el("div", { class: "small mt-8 novelty-text" },
                    `新颖性：${r.novelty_reason}`));
            }

            // 新知 vs 已知：量化相似度 Top-N（与已入库文献对比）
            const nctx = r.novelty_context || {};
            if (nctx.top_similar_papers && nctx.top_similar_papers.length) {
                const nctxDiv = el("div", { class: "novelty-context mt-8" });
                nctxDiv.appendChild(el("div", { class: "small evidence-title" },
                    `新知对比（最大相似度 ${nctx.max_similarity} · 判定 ${nctx.assessment || "—"}）：`));
                nctx.top_similar_papers.forEach(sp => {
                    const row = el("div", { class: "small novelty-context-row" });
                    row.appendChild(el("span", {
                        class: "mono muted",
                        text: `sim ${sp.similarity.toFixed(2)}`,
                    }));
                    row.appendChild(el("span", {
                        class: "novelty-context-title",
                        title: `匹配词：${(sp.matched_terms || []).join(", ")}`,
                        text: sp.title,
                    }));
                    nctxDiv.appendChild(row);
                });
                item.appendChild(nctxDiv);
            }

            // 交叉验证结果
            if (Object.keys(cv).length) {
                const cvDiv = el("div", { class: "cv-block mt-8" });
                cvDiv.appendChild(el("div", { class: "small cv-title" },
                    `Materials 交叉验证（${cv.cross_validation_source || "—"}）`));
                cvDiv.appendChild(el("div", { class: "small cv-line" },
                    `MP 命中：${cv.mp_match ? "✓" : "✗"} · 规则检查：${cv.rule_check_passed ? "✓ 通过" : "✗ 未通过"} · 文献一致：${cv.literature_consistent ? "✓" : "✗"}`));
                if (cv.rule_check_notes) {
                    cvDiv.appendChild(el("div", { class: "small cv-notes" }, cv.rule_check_notes));
                }
                if (cv.mp_band_gap !== null && cv.mp_band_gap !== undefined) {
                    cvDiv.appendChild(el("div", { class: "small cv-line" },
                        `MP 带隙：${cv.mp_band_gap} eV`));
                }
                item.appendChild(cvDiv);
            }

            // 证据溯源链（可点击跳转到论文/实验页）
            if (evidenceRefs.length) {
                const evDiv = el("div", { class: "evidence-chain mt-8" });
                evDiv.appendChild(el("div", { class: "small evidence-title" }, "证据溯源链："));
                evDiv.appendChild(buildEvidenceList(evidenceRefs));
                item.appendChild(evDiv);
            }

            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderHypothesesList(hypotheses) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" },
            `候选构效关系假设（${hypotheses.length} 个）· 搜索种子`));
        const list = el("div", { class: "list" });
        hypotheses.forEach((h, i) => {
            const item = el("div", { class: "list-item" });
            item.appendChild(el("div", { class: "list-item-head" }, [
                el("span", { class: "gap-number", text: `#${i + 1}` }),
                el("span", { class: "list-item-title", text: h.hypothesis || "(无假设陈述)" }),
                el("span", { class: "badge badge-info", text: h.target_property || "?" }),
            ]));
            if (h.variables && h.variables.length) {
                item.appendChild(el("div", { class: "small mt-8" },
                    `变量：${h.variables.join(", ")}`));
            }
            if (h.rationale) {
                item.appendChild(el("div", { class: "small mt-8 muted" },
                    `依据：${h.rationale}`));
            }
            if (h.gap_ref) {
                item.appendChild(el("div", { class: "small mt-8 gap-text" },
                    `关联 Research Gap：${h.gap_ref}`));
            }
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    function renderSearchSpace(searchSpace) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "搜索空间定义"));
        const variables = searchSpace.variables || [];
        if (!variables.length) {
            card.appendChild(el("div", { class: "list-empty" }, "暂无搜索空间定义"));
            return card;
        }
        card.appendChild(el("div", { class: "small muted mb-8" },
            `目标性能：${searchSpace.target_property || "?"} ${searchSpace.target_unit || ""}`));
        const list = el("div", { class: "list" });
        variables.forEach(v => {
            const item = el("div", { class: "list-item" });
            item.appendChild(el("div", { class: "list-item-head" }, [
                el("span", { class: "list-item-title", text: v.name || "?" }),
                el("span", { class: "badge badge-neutral", text: v.type || "continuous" }),
            ]));
            const range = v.type === "categorical"
                ? (v.categories || []).join(" / ")
                : `${v.low} ~ ${v.high} ${v.unit || ""}`;
            item.appendChild(el("div", { class: "small mt-8" }, `定义域：${range}`));
            list.appendChild(item);
        });
        card.appendChild(list);

        // 物理约束
        const constraints = searchSpace.constraints || [];
        if (constraints.length) {
            card.appendChild(el("div", { class: "card-subtitle mt-16" }, "物理约束（LLM 剪枝依据）"));
            const ul = el("ul", { class: "feature-list" });
            constraints.forEach(c => ul.appendChild(el("li", {}, c)));
            card.appendChild(ul);
        }
        return card;
    }

    function renderReportPreview(content) {
        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "构效关系发现报告（Markdown）"));
        const pre = el("pre", { class: "code-block report-preview" });
        pre.textContent = content;
        card.appendChild(pre);
        return card;
    }

    // ===== 6e. Materials Project 交叉验证页 =====

    async function renderMaterialsCv(content) {
        content.appendChild(el("div", { class: "loading" }, "加载交叉验证报告…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/materials-cross-validation`);
            state.materialsCvCache = data;
            clear(content);

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "Materials Project 交叉验证"),
                el("p", { class: "page-desc" },
                    "赛题路线 A 硬要求：与公开数据库交叉验证。无 MP API key 时降级为规则交叉验证（基于已知热电材料体系物理范围）。"),
            ]));

            const report = data.report;
            if (!report) {
                content.appendChild(el("div", { class: "card" }, [
                    el("div", { class: "list-empty" },
                        data.message || "尚未生成交叉验证报告，请先运行构效关系发现"),
                    el("div", { class: "btn-row mt-16" }, [
                        el("button", {
                            class: "btn btn-accent",
                            onclick: () => startDiscovery(),
                        }, "启动构效关系发现"),
                    ]),
                ]));
                return;
            }

            // ===== 顶部状态徽章（MP 已连接 / 规则降级 / 混合）=====
            const src = report.source || "";
            const badgeInfo = {
                mp: { cls: "badge badge-success", text: "✓ 已连接 Materials Project API" },
                rule: { cls: "badge badge-warn", text: "⚠ 规则降级（未配置 MP API key）" },
                hybrid: { cls: "badge badge-success", text: "✓ MP API + 规则 双路" },
            }[src] || { cls: "badge badge-muted", text: "未知来源" };
            content.appendChild(el("div", { class: "badge-row mt-8" }, [
                el("span", { class: badgeInfo.cls }, badgeInfo.text),
                el("a", {
                    href: `/api/projects/${state.currentProjectId}/download/cross-validation`,
                    class: "btn btn-secondary btn-sm",
                    style: "margin-left:auto",
                }, "下载报告 (.md)"),
            ]));

            // 顶部统计
            content.appendChild(el("div", { class: "counts-grid" }, [
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "验证发现数"),
                    el("div", { class: "count-value", text: String(report.total_discoveries || 0) }),
                    el("div", { class: "count-extra" }, "总验证条数"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "MP 命中"),
                    el("div", { class: "count-value", text: String(report.mp_validated || 0) }),
                    el("div", { class: "count-extra" }, "Materials Project 数据库匹配"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "规则通过"),
                    el("div", { class: "count-value", text: String(report.rule_validated || 0) }),
                    el("div", { class: "count-extra" }, "物理范围规则检查通过"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "整体置信度"),
                    el("div", { class: "count-value", text: (report.overall_confidence || 0).toFixed(2) }),
                    el("div", { class: "count-extra" }, "0~1"),
                ]),
            ]));

            // 来源说明
            content.appendChild(el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "验证来源说明"),
                el("p", { class: "small" }, report.notes || ""),
                el("p", { class: "small muted mt-8" },
                    `来源：${report.source === "mp" ? "Materials Project API + 规则双路" : "规则交叉验证（未配置 MP API key）"}`),
                el("p", { class: "small muted mt-8" },
                    "配置 MATERIALS_PROJECT_API_KEY 环境变量可启用 Materials Project 数据库交叉验证（获取地址：https://next-gen.materialsproject.org/api）"),
            ]));

            // 每条发现的验证详情
            const results = report.results || [];
            const card = el("div", { class: "card" });
            card.appendChild(el("div", { class: "card-title" },
                `逐条验证详情（${results.length} 条）`));
            if (!results.length) {
                card.appendChild(el("div", { class: "list-empty" }, "暂无验证详情"));
            } else {
                const list = el("div", { class: "list" });
                results.forEach((r, i) => {
                    const item = el("div", {
                        class: `list-item ${r.rule_check_passed ? "" : "conflict-item"}`,
                    });
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "gap-number", text: `#${i + 1}` }),
                        el("span", { class: "list-item-title", text: `材料：${r.material || "未识别"}` }),
                        el("span", {
                            class: `badge ${r.novelty === "novel" ? "badge-success" : "badge-neutral"}`,
                            text: r.novelty || "?",
                        }),
                        el("span", {
                            class: `badge ${r.rule_check_passed ? "badge-success" : "badge-danger"}`,
                            text: r.rule_check_passed ? "规则通过" : "规则未通过",
                        }),
                        el("span", {
                            class: `badge ${r.mp_match ? "badge-success" : "badge-neutral"}`,
                            text: r.mp_match ? "MP 命中" : "MP 未命中",
                        }),
                    ]));
                    item.appendChild(el("div", { class: "small mt-8" },
                        `综合置信度：${(r.confidence || 0).toFixed(2)} · 验证来源：${r.cross_validation_source || "—"}`));
                    if (r.rule_check_notes) {
                        item.appendChild(el("div", { class: "small mt-8 cv-notes" },
                            r.rule_check_notes));
                    }
                    if (r.mp_band_gap !== null && r.mp_band_gap !== undefined) {
                        item.appendChild(el("div", { class: "small mt-8" },
                            `Materials Project 带隙：${r.mp_band_gap} eV`));
                    }
                    if (r.claim_id) {
                        item.appendChild(el("div", { class: "small mt-8 mono muted" },
                            `Claim ID: ${r.claim_id}`));
                    }
                    list.appendChild(item);
                });
                card.appendChild(list);
            }
            content.appendChild(card);
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 6f. 方法↔代码对齐页 =====

    async function renderMethodAlignment(content) {
        content.appendChild(el("div", { class: "loading" }, "加载方法↔代码对齐数据…"));
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/method-alignment`);
            state.methodAlignmentCache = data;
            clear(content);

            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "方法↔代码对齐"),
                el("p", { class: "page-desc" },
                    "从方法 Artifact 抽取 LaTeX 公式，与实验代码做关键词匹配，标注 mapped / partial / missing。"),
            ]));

            if (data.error) {
                content.appendChild(el("div", { class: "status-banner danger" },
                    `加载失败：${escapeHtml(data.error)}`));
                return;
            }

            // 对齐摘要
            const summary = data.alignment_summary || {};
            content.appendChild(el("div", { class: "counts-grid" }, [
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "总公式数"),
                    el("div", { class: "count-value", text: String(summary.total_formulas || 0) }),
                    el("div", { class: "count-extra" }, "从方法 Artifact 抽取"),
                ]),
                el("div", { class: "count-card highlight" }, [
                    el("div", { class: "count-label" }, "已映射"),
                    el("div", { class: "count-value", text: String(summary.mapped || 0) }),
                    el("div", { class: "count-extra" }, "实验代码中找到对应变量"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "部分映射"),
                    el("div", { class: "count-value", text: String(summary.partial || 0) }),
                    el("div", { class: "count-extra" }, "公式存在但变量未完全匹配"),
                ]),
                el("div", { class: "count-card" }, [
                    el("div", { class: "count-label" }, "缺失"),
                    el("div", { class: "count-value", text: String(summary.missing || 0) }),
                    el("div", { class: "count-extra" }, "代码中未找到对应实现"),
                ]),
            ]));

            // 方法 Artifact 列表
            const artifacts = data.method_artifacts || [];
            if (artifacts.length) {
                const artCard = el("div", { class: "card" });
                artCard.appendChild(el("div", { class: "card-title" },
                    `方法 Artifact（${artifacts.length} 个）`));
                const list = el("div", { class: "list" });
                artifacts.forEach(a => {
                    const item = el("div", { class: "list-item" });
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "list-item-title", text: a.artifact_id || "?" }),
                    ]));
                    if (a.content) {
                        const pre = el("pre", { class: "code-block small" });
                        pre.textContent = (a.content || "").slice(0, 500) + "…";
                        item.appendChild(pre);
                    }
                    list.appendChild(item);
                });
                artCard.appendChild(list);
                content.appendChild(artCard);
            }

            // 公式列表
            const formulas = data.formulas || [];
            const fCard = el("div", { class: "card" });
            fCard.appendChild(el("div", { class: "card-title" },
                `LaTeX 公式对齐详情（${formulas.length} 条）`));
            if (!formulas.length) {
                fCard.appendChild(el("div", { class: "list-empty" },
                    "未从方法 Artifact 中抽取到 LaTeX 公式"));
            } else {
                const list = el("div", { class: "list" });
                formulas.forEach((f, i) => {
                    const item = el("div", { class: "list-item" });
                    const statusCls = f.alignment_status === "mapped" ? "badge-success"
                        : (f.alignment_status === "partial" ? "badge-warning" : "badge-danger");
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "gap-number", text: `#${i + 1}` }),
                        el("span", { class: `badge ${statusCls}`, text: f.alignment_status }),
                        el("span", { class: "badge badge-neutral", text: f.kind }),
                    ]));
                    const pre = el("pre", { class: "code-block formula-block" });
                    pre.textContent = f.formula;
                    item.appendChild(pre);
                    if (f.keywords && f.keywords.length) {
                        item.appendChild(el("div", { class: "small mt-8" },
                            `公式变量：${f.keywords.join(", ")}`));
                    }
                    if (f.matched_keywords && f.matched_keywords.length) {
                        item.appendChild(el("div", { class: "small mt-8 mapped-text" },
                            `代码中匹配：${f.matched_keywords.join(", ")}`));
                    }
                    list.appendChild(item);
                });
                fCard.appendChild(list);
            }
            content.appendChild(fCard);

            // 实验代码片段
            const exps = data.experiment_code_snippets || [];
            if (exps.length) {
                const expCard = el("div", { class: "card" });
                expCard.appendChild(el("div", { class: "card-title" },
                    `实验代码片段（${exps.length} 个）`));
                const list = el("div", { class: "list" });
                exps.forEach(e => {
                    const item = el("div", { class: "list-item" });
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "list-item-title", text: e.name || "(无名称)" }),
                        el("span", {
                            class: `badge ${e.status === "completed" ? "badge-success" : "badge-warning"}`,
                            text: e.status,
                        }),
                    ]));
                    if (e.config_keys && e.config_keys.length) {
                        item.appendChild(el("div", { class: "small mt-8" },
                            `配置键：${e.config_keys.join(", ")}`));
                    }
                    if (e.result_summary) {
                        item.appendChild(el("div", { class: "small mt-8 muted" },
                            `结果：${e.result_summary}`));
                    }
                    list.appendChild(item);
                });
                expCard.appendChild(list);
                content.appendChild(expCard);
            }
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 7. 人工节点交互页 =====

    // 结构化渲染 prompt 文本：识别编号列表、项目符号、标题行
    // 把一整块纯文本拆成有视觉层次的 DOM 节点，而非 <pre> 或单 div
    function renderPromptStructured(text) {
        const wrap = el("div", { class: "request-prompt-structured" });
        const lines = (text || "").split("\n");
        let currentList = null;  // 当前正在累积的 <ol> 或 <ul>
        let listType = null;     // "ol" / "ul"

        const flushList = () => {
            if (currentList) {
                wrap.appendChild(currentList);
                currentList = null;
                listType = null;
            }
        };

        lines.forEach(rawLine => {
            const line = rawLine.replace(/\s+$/, "");  // 去行尾空格
            const trimmed = line.trim();

            // 空行：结束当前列表
            if (!trimmed) {
                flushList();
                return;
            }

            // 编号列表项：  1. xxx  或  1) xxx  或  1、xxx
            const olMatch = trimmed.match(/^(\d+)[.、)]\s*(.+)/);
            if (olMatch) {
                if (listType !== "ol") { flushList(); currentList = el("ol", { class: "prompt-ol" }); listType = "ol"; }
                currentList.appendChild(el("li", { class: "prompt-li", text: olMatch[2] }));
                return;
            }

            // 项目符号：  - xxx  或  • xxx
            const ulMatch = trimmed.match(/^[-•·]\s*(.+)/);
            if (ulMatch) {
                if (listType !== "ul") { flushList(); currentList = el("ul", { class: "prompt-ul" }); listType = "ul"; }
                currentList.appendChild(el("li", { class: "prompt-li", text: ulMatch[1] }));
                return;
            }

            // 非列表行：先 flush，再判断是否是标题行（以冒号结尾且较短）
            flushList();
            if (trimmed.length <= 40 && /[:：]$/.test(trimmed)) {
                wrap.appendChild(el("div", { class: "prompt-heading", text: trimmed }));
            } else {
                wrap.appendChild(el("div", { class: "prompt-text", text: trimmed }));
            }
        });
        flushList();
        return wrap;
    }

    // ===== localStorage 持久化 =====

    function saveProjectToStorage(projectId) {
        try {
            localStorage.setItem("sra_project_id", projectId);
        } catch (e) { /* 隐私模式忽略 */ }
    }

    function clearProjectFromStorage() {
        try {
            localStorage.removeItem("sra_project_id");
        } catch (e) { /* 忽略 */ }
        // 同步清空侧边栏/顶栏徽章（论文数/材料/缺口/Claim/实验等）
        ["badge-papers", "badge-materials", "badge-gaps", "badge-report-gaps",
         "badge-claims", "badge-experiments", "badge-notes", "badge-human",
         "badge-topic-discovery", "badge-progress"].forEach(id => {
            const node = document.getElementById(id);
            if (node) { node.style.display = "none"; node.textContent = ""; }
        });
        // 重置导航条 → 全部 pending（无状态时 flowStepState 返回 pending）
        state.lastFlowActiveIdx = null;
        state.autoNavDone = false; // 新项目/清空后重置自动导航标记
        const host = document.getElementById("flow-nav-host");
        if (host) renderFlowNav();
        // 顶栏状态徽章也清空
        const topStatus = document.getElementById("topbar-status");
        if (topStatus) topStatus.style.display = "none";
    }

    function getProjectFromStorage() {
        try {
            return localStorage.getItem("sra_project_id");
        } catch (e) { return null; }
    }

    // ===== 初始化 =====

    async function init() {
        // 绑定导航
        document.querySelectorAll(".nav-item").forEach(n => {
            n.addEventListener("click", () => {
                const page = n.getAttribute("data-page");
                if (page) setActivePage(page);
            });
        });

        // 顶部帮助按钮 + 帮助面板关闭
        const helpBtn = document.getElementById("topbar-help");
        if (helpBtn) helpBtn.addEventListener("click", toggleHelpPanel);
        const helpClose = document.getElementById("help-panel-close");
        if (helpClose) helpClose.addEventListener("click", closeHelpPanel);
        // 点击帮助面板背景关闭
        const helpPanel = document.getElementById("help-panel");
        if (helpPanel) {
            helpPanel.addEventListener("click", (e) => {
                if (e.target === helpPanel) closeHelpPanel();
            });
        }

        // 键盘快捷键
        setupKeyboardShortcuts();

        // 侧边栏灵感笔记小组件
        const snSave = document.getElementById("sidebar-note-save");
        if (snSave) snSave.addEventListener("click", saveSidebarNote);
        const snLink = document.getElementById("sidebar-note-link");
        if (snLink) snLink.addEventListener("click", () => setActivePage("notes"));
        const snInput = document.getElementById("sidebar-note-input");
        if (snInput) snInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                saveSidebarNote();
            }
        });
        // 默认页
        setActivePage("create");

        // URL 深链支持：?project=<project_id>&page=<page>&paper=<paper_id>&exp=<exp_id>
        //（page 直达页面；paper/exp 用于证据溯源定位到具体论文/实验）
        const params = new URLSearchParams(window.location.search);
        const urlProject = (params.get("project") || "").trim();
        const urlPage = (params.get("page") || "").trim();
        const urlPaper = (params.get("paper") || "").trim();
        const urlExp = (params.get("exp") || "").trim();
        if (urlProject) {
            state.currentProjectId = urlProject;
            updateProjectIdDisplay();
            if (urlPaper) state.pendingPaperId = urlPaper;
            if (urlExp) state.pendingExperimentId = urlExp;
            startPolling();
            if (urlPage && ["progress", "papers", "materials", "gaps", "claims", "experiments", "discovery", "notes", "human"].includes(urlPage)) {
                setActivePage(urlPage);
            } else {
                setActivePage("progress");
            }
        }

        // 从 localStorage 恢复上次项目（解决刷新丢失问题）
        const savedPid = getProjectFromStorage();
        if (savedPid) {
            try {
                const data = await api("GET", `/api/projects/${savedPid}/status`);
                if (data && data.project_id) {
                    state.currentProjectId = savedPid;
                    state.statusCache = data;
                    state.runMode = data.run_mode || "";
                    updateProjectIdDisplay();
                    updateBadges(data);
                    startPolling();
                    renderSidebarNotes();
                    renderSidebarDownload();
                }
            } catch (e) {
                // 项目已不存在（服务器重启等），清除存储
                clearProjectFromStorage();
            }
        }

        // 默认页（仅当 URL 深链/localStorage 恢复都未设置项目时进入 dashboard，
        // 避免覆盖上方 urlProject 深链指定的 page）
        if (!state.currentProjectId) {
            setActivePage("dashboard");
        }
    }

    document.addEventListener("DOMContentLoaded", init);
})();
