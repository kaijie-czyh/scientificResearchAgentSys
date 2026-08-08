/* 科研 Agent 系统前端逻辑 —— 单页应用，原生 JS 实现 */
(function () {
    "use strict";

    // ===== 全局状态 =====
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
        pendingPaperId: null,   // 待定位论文（证据溯源跳转：gaps/claims → papers 定位高亮）
        pendingExperimentId: null, // 待定位实验（Claim 证据 → experiments 定位高亮）
        papersView: "all",      // 论文浏览页视图：all / unlinked（未入库候选）
        dashboardCache: null,   // /dashboard 聚合数据
        discoveryDetailCache: null,  // /discovery-detail 详细数据
        researchReportCache: null,   // /research-report 调研报告
        materialsCvCache: null,      // /materials-cross-validation
        methodAlignmentCache: null,  // /method-alignment
        pendingPaperId: null,        // 证据跳转目标：点击证据中的 paper_id 后置位，renderPapers 自动展开滚动
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
            const d = new Date(iso);
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
    }

    async function api(method, path, body) {
        const opts = { method, headers: {} };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
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

    function statusBanner(status, summary, error, recommendation) {
        let cls = "info";
        if (["completed", "success"].includes(status)) cls = "success";
        else if (["pending_human", "pending_review", "experiment_failed", "anomaly_detected"].includes(status)) cls = "warning";
        else if (["failed", "aborted", "blocked"].includes(status)) cls = "danger";

        const parts = [];
        parts.push(`<span class="status-dot"></span><strong>${escapeHtml(status)}</strong>`);
        if (summary) parts.push(`<span>${escapeHtml(summary)}</span>`);
        if (error) parts.push(`<span class="mono small">错误：${escapeHtml(error)}</span>`);
        if (recommendation) parts.push(`<span class="small">建议：${escapeHtml(recommendation)}</span>`);
        return `<div class="status-banner ${cls}">${parts.join(" · ")}</div>`;
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
        setBadge("badge-claims", counts.claims);
        setBadge("badge-experiments", counts.experiments);
        setBadge("badge-notes", (data.notes_count != null) ? data.notes_count : null);
        setBadge("badge-human", data.pending_human ? "!" : null);
    }

    function setBadge(id, value) {
        const node = document.getElementById(id);
        if (!node) return;
        if (value == null || value === 0) {
            node.style.display = "none";
        } else {
            node.style.display = "inline-block";
            node.textContent = value;
        }
    }

    // ===== 页面渲染入口 =====

    function renderPage() {
        const content = document.getElementById("content");
        clear(content);

        if (state.currentPage !== "create" && !state.currentProjectId) {
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
                statusBanner(data.status, data.summary, null, null));

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

        const card = el("div", { class: "card" });
        card.appendChild(el("div", { class: "card-title" }, "赛题对齐进度"));
        const grid = el("div", { class: "alignment-grid" });

        // 基本任务
        grid.appendChild(el("div", { class: `alignment-item ${basicDone ? "done" : "pending"}` }, [
            el("div", { class: "alignment-head" }, [
                el("span", { class: "alignment-tag" }, "基本任务"),
                el("span", { class: `alignment-status ${basicDone ? "ok" : "pending"}` },
                    basicDone ? "已完成" : "未开始"),
            ]),
            el("div", { class: "alignment-title" }, "文献调研 Agent"),
            el("div", { class: "alignment-meta" },
                `论文 ${counts.papers || 0} 篇 · Research Gaps ${rr.gaps_count || 0} · 共识 ${rr.consensus_count || 0} · 冲突 ${rr.conflicts_count || 0} · 置信度 ${(rr.overall_confidence || 0).toFixed(2)}`),
        ]));

        // 路线 A
        grid.appendChild(el("div", { class: `alignment-item ${routeADone ? "done" : "pending"}` }, [
            el("div", { class: "alignment-head" }, [
                el("span", { class: "alignment-tag tag-route-a" }, "路线 A"),
                el("span", { class: `alignment-status ${routeADone ? "ok" : "pending"}` },
                    routeADone ? "已完成" : "未开始"),
            ]),
            el("div", { class: "alignment-title" }, "构效关系发现"),
            el("div", { class: "alignment-meta" },
                `假设 ${ds.hypotheses || 0} · 候选 ${ds.candidates || 0} · 发现 ${ds.relationships || 0} · Novel ${ds.novel || 0}`),
        ]));

        // Materials Project 交叉验证
        const cvDone = (mc.total_discoveries || 0) > 0;
        grid.appendChild(el("div", { class: `alignment-item ${cvDone ? "done" : "pending"}` }, [
            el("div", { class: "alignment-head" }, [
                el("span", { class: "alignment-tag tag-cv" }, "交叉验证"),
                el("span", { class: `alignment-status ${cvDone ? "ok" : "pending"}` },
                    cvDone ? "已完成" : "未开始"),
            ]),
            el("div", { class: "alignment-title" }, "Materials Project + 规则双路验证"),
            el("div", { class: "alignment-meta" },
                `验证 ${mc.total_discoveries || 0} 条 · MP 命中 ${mc.mp_validated || 0} · 规则通过 ${mc.rule_validated || 0} · 置信度 ${(mc.overall_confidence || 0).toFixed(2)} · 来源 ${mc.source || "—"}`),
        ]));

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
                placeholder: "示例：联邦学习场景下的公平激励机制设计——如何在不暴露客户端私有数据的前提下，避免激励分配被少数强势节点操纵。",
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
            ]),
        ]);
        card.appendChild(resumeRow);

        ]);
        card.appendChild(btnRow);

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
                setActivePage("papers");
            } catch (e) {
                showToast("项目不存在：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "继续";
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

        const actions = el("div", { class: "btn-row mt-16" }, [
            el("button", { class: "btn btn-success", onclick: () => startPipeline() }, "启动 Pipeline"),
            el("button", { class: "btn btn-accent", onclick: () => startDiscovery() }, "启动构效关系发现"),
            el("button", { class: "btn btn-secondary", onclick: () => setActivePage("progress") }, "查看进度"),
        ]);

        container.appendChild(pid);
        container.appendChild(actions);
    }

    async function startPipeline() {
        if (!state.currentProjectId) return;
        try {
            await api("POST", `/api/projects/${state.currentProjectId}/run`);
            showToast("Pipeline 已启动", "success");
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
        const fmtGrowth = g => (g == null ? "N/A" : `${g > 0 ? "+" : ""}${Math.round(g * 100)}%`);

        const head = el("div", { class: "rec-compare-row rec-compare-head" }, [
            el("span", { class: "c-idx" }, "#"),
            el("span", { class: "c-topic" }, "推荐主题"),
            el("span", { class: "c-dim" }, "热门度"),
            el("span", { class: "c-dim" }, "难度"),
            el("span", { class: "c-dim" }, "创新度"),
            el("span", { class: "c-dim" }, "关联度"),
            el("span", { class: "c-dim" }, "增长率"),
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
                html: statusBanner(status.status, status.summary, status.error, status.recommendation),
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
                            el("span", { class: "badge badge-neutral", text: `难度: ${rec.difficulty || "N/A"}` }),
                            el("span", { class: "badge badge-info", text: `创新度: ${rec.novelty || "N/A"}` }),
                            el("span", { class: "badge badge-warning", text: `热门度: ${rec.popularity_score != null ? rec.popularity_score : "N/A"}` }),
                            el("span", { class: "badge badge-success", text: `关联度: ${rec.relevance || "N/A"}` }),
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
            pollStatus();
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
            statusBanner(data.status, data.summary, data.error, data.recommendation));

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
        ]);
        const actionRow = el("div", { class: "btn-row" }, [
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
        ]);
        actionCard.appendChild(actionRow);

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

        content.appendChild(actionCard);

        // 节点历史时间线
        content.appendChild(renderTimeline(data.node_history || []));
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
            // 未入库论文入口卡片
            if (unlinked.length) {
                content.appendChild(el("div", {
                    class: "card unlinked-entry",
                    id: "unlinked-entry",
                    onclick: () => { state.papersView = "unlinked"; renderPage(); },
                }, [
                    el("div", { class: "unlinked-entry-main" }, [
                        el("span", { class: "unlinked-entry-title" }, "未入库论文"),
                        el("span", { class: "unlinked-entry-desc",
                            text: `${unlinked.length} 篇检索命中但未入库的候选（被相关性筛选/去重剔除），可手动补录入库` }),
                    ]),
                    el("span", { class: "unlinked-entry-arrow" }, "查看 →"),
                ]));
            }
            if (!papers.length) {
                content.appendChild(el("div", { class: "list-empty" }, "暂无论文，请先启动 research 阶段"));
                return;
            }
            const list = el("div", { class: "list" });
            papers.forEach(p => list.appendChild(renderPaperItem(p)));
            content.appendChild(list);

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
            const data = await api("GET", `/api/projects/${state.currentProjectId}/papers`);
            clear(content);
            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "论文浏览"),
                el("p", { class: "page-desc" }, `共 ${data.papers.length} 篇入库论文，点击条目展开详情。`),
            ]));

            // 上传文献入口（与新建页共用 renderUploadCard）
            if (state.currentProjectId) {
                content.appendChild(renderUploadCard());
            }

            if (!data.papers.length) {
                content.appendChild(el("div", { class: "list-empty" }, "暂无论文，可使用上方表单上传，或启动 research 阶段自动检索"));
                return;
            }
            const list = el("div", { class: "list" });
            data.papers.forEach(p => list.appendChild(renderPaperItem(p)));
            content.appendChild(list);

            // 若从证据跳转过来，自动展开匹配的论文并滚动到视口
            if (state.pendingPaperId) {
                const targetId = state.pendingPaperId;
                state.pendingPaperId = null;  // 消费一次
                setTimeout(() => {
                    const items = list.querySelectorAll(".list-item");
                    for (const it of items) {
                        const body = it.querySelector(".list-item-body");
                        if (body && body.textContent.includes(targetId)) {
                            it.scrollIntoView({ behavior: "smooth", block: "center" });
                            return;
                        }
                        // 未展开则先展开再判断
                        if (!it.classList.contains("expanded")) {
                            it.click();
                            const b = it.querySelector(".list-item-body");
                            if (b && b.textContent.includes(targetId)) {
                                it.scrollIntoView({ behavior: "smooth", block: "center" });
                                return;
                            } else {
                                it.classList.remove("expanded");
                                const rm = it.querySelector(".list-item-body");
                                if (rm) rm.remove();
                            }
                        }
                    }
                }, 100);
            }
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 未入库论文候选视图 =====

    async function renderUnlinkedPapers(content, unlinked, filterNote) {
        content.appendChild(el("div", { class: "page-header" }, [
            el("h2", { class: "page-title" }, "未入库论文"),
            el("p", { class: "page-desc" },
                `共 ${unlinked.length} 篇检索命中但未入库的候选论文，可手动补录入库。`),
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
    function renderPaperItem(p) {
        const item = el("div", { class: "list-item" });
        const head = el("div", { class: "list-item-head" }, [
            el("span", { class: "list-item-title", text: p.title || "(无标题)" }),
            p.year ? el("span", { class: "badge badge-info badge-stage", text: String(p.year) }) : null,
            p.venue ? el("span", { class: "badge badge-neutral", text: p.venue }) : null,
            el("span", { class: "list-item-meta", text: (p.authors || []).slice(0, 2).join(", ") + ((p.authors || []).length > 2 ? " et al." : "") }),
        ]);
        item.appendChild(head);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                const body = el("div", { class: "list-item-body" });
                // 构建链接区
                let linksHtml = "";
                if (p.url) {
                    linksHtml += `<a href="${escapeHtml(p.url)}" target="_blank" class="link-external">原文链接 ↗</a>`;
                }
                if (p.doi_url) {
                    linksHtml += ` <a href="${escapeHtml(p.doi_url)}" target="_blank" class="link-external">DOI ↗</a>`;
                }
                if (p.pdf_path) {
                    linksHtml += ` <span class="badge badge-success">本地 PDF 已上传</span>`;
                }
                if (!linksHtml) linksHtml = '<span class="muted">无可用链接</span>';

                body.innerHTML = `
                    <dl>
                        <dt>Paper ID</dt><dd>${escapeHtml(p.paper_id)}</dd>
                        <dt>作者</dt><dd>${escapeHtml((p.authors || []).join(", ") || "—")}</dd>
                        <dt>年份</dt><dd>${p.year || "—"}</dd>
                        <dt>会议/期刊</dt><dd>${escapeHtml(p.venue || "—")}</dd>
                        <dt>arXiv ID</dt><dd>${escapeHtml(p.arxiv_id || "—")}</dd>
                        <dt>DOI</dt><dd>${escapeHtml(p.doi || "—")}</dd>
                        <dt>链接</dt><dd>${linksHtml}</dd>
                        <dt>来源</dt><dd>${escapeHtml(p.source_stage || "—")}</dd>
                        <dt>入库时间</dt><dd>${escapeHtml(formatTime(p.created_at))}</dd>
                    </dl>
                    ${p.abstract ? `<div class="mt-8 abstract-box"><strong>摘要</strong><br>${escapeHtml(p.abstract)}</div>` : ""}
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
        const srcBadges = Object.keys(bySource).map(src =>
            el("span", { class: `badge ev-src-badge ev-src-${src}`, text: `${src} ${bySource[src]}` }));

        const card = el("div", { class: "card ev-card" }, [
            el("div", { class: "ev-head" }, [
                el("span", { class: "ev-title" }, "检索证据链 · 审计轨迹"),
                el("span", { class: "badge badge-info", text: `共 ${stats.total} 条` }),
                el("span", { class: "badge badge-neutral", text: `已关联论文 ${stats.linked} 条` }),
                ...srcBadges,
            ]),
            el("div", { class: "ev-desc" },
                "审计轨迹：子问题 → 数据源 → 证据 → 是否入库。每条子问题按固定配额抓取" +
                "（Sciverse 10 + arXiv 3 + S2 2），因此各子问题条数相近；" +
                "「关联入库」数才是该子问题真正采纳的论文数。Sciverse 证据含 doc_id/offset，" +
                "可回读原文核验；关联依据见各条目的 match_type。"),
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
        const row = el("div", { class: "ev-entry" }, [
            el("span", { class: `badge ev-src-badge ev-src-${src}`, text: src }),
            el("span", { class: "ev-entry-title", text: e.title || "(无标题)" }),
        ]);
        if (src === "sciverse") {
            row.appendChild(el("span", { class: "ev-entry-score",
                text: `证据分 ${Number(e.evidence_score || 0).toFixed(2)}` }));
            row.appendChild(el("span", { class: "mono ev-entry-id",
                text: `doc:${e.external_id || "-"}` }));
            if (Number(e.offset || 0) > 0) {
                row.appendChild(el("span", { class: "ev-entry-offset",
                    text: `@偏移${e.offset}` }));
            }
        } else {
            const eid = e.external_id || "";
            if (eid) row.appendChild(el("span", { class: "mono ev-entry-id", text: eid }));
        }
        // 关联依据（量化可审计）：match_type 说明为何关联该论文；
        // paper_id 为空 → 检索命中但未关联（被 filter 相关性筛选/去重剔除）。
        if (e.paper_id) {
            const reason = e.match_type || "证据来源关联";
            row.appendChild(el("span", { class: "ev-entry-linked",
                text: `已入库 · ${reason}` }));
            if (Number(e.paper_relevance || 0) > 0) {
                row.appendChild(el("span", { class: "ev-entry-rel",
                    text: `相关度 ${Number(e.paper_relevance).toFixed(2)}` }));
            }
        } else {
            row.appendChild(el("span", { class: "ev-entry-unlinked",
                text: "未入库 · 被筛选/去重剔除" }));
        }
        return row;
    }

    function renderPaperItem(p) {
        const item = el("div", { class: "list-item" });
        item.setAttribute("data-paper-id", p.paper_id || "");
        const headChildren = [
            el("span", { class: "list-item-title", text: p.title || "(无标题)" }),
            p.year ? el("span", { class: "badge badge-info badge-stage", text: String(p.year) }) : null,
            p.venue ? el("span", { class: "badge badge-neutral", text: p.venue }) : null,
        ];
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
        const head = el("div", { class: "list-item-head" }, headChildren);
        item.appendChild(head);
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
                const link = el("a", {
                    class: "evidence-link",
                    text: refId,
                    title: "点击跳转到论文浏览页",
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
            el("span", { class: "badge badge-neutral", text: `证据 ${c.evidence_count}` }),
        ]));
        // 状态徽章单独一行（便于颜色识别）
        item.appendChild(el("div", { class: "mt-8" }));
        item.insertAdjacentHTML("beforeend", `<div class="mt-8">${statusBadge(c.status)}</div>`);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                item.appendChild(buildPaperBody(p));
            }
        });
        return item;
    }

    function buildPaperBody(p) {
        const body = el("div", { class: "list-item-body" });
        body.innerHTML = `
                    <dl>
                        <dt>Paper ID</dt><dd class="mono">${escapeHtml(p.paper_id)}</dd>
                        <dt>作者</dt><dd>${escapeHtml((p.authors || []).join(", ") || "—")}</dd>
                        <dt>年份</dt><dd>${p.year || "—"}</dd>
                        <dt>会议/期刊</dt><dd>${escapeHtml(p.venue || "—")}</dd>
                        <dt>arXiv ID</dt><dd class="mono">${escapeHtml(p.arxiv_id || "—")}</dd>
                        <dt>URL</dt><dd>${p.url ? `<a href="${escapeHtml(p.url)}" target="_blank">${escapeHtml(p.url)}</a>` : "—"}</dd>
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
        const item = el("div", { class: "mat-card" });
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

        item.insertAdjacentHTML("beforeend",
            `<div class="mat-src">来源论文：${escapeHtml(m.paper_title || "—")}</div>`);
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
    }

    function renderGapCard(g) {
        const typeMeta = GAP_TYPE_META[g.gap_type] || { label: g.gap_type || "未知", cls: "gap-tag-neutral" };
        const actMeta = GAP_ACTION_META[g.actionability] || { label: g.actionability || "中", cls: "gap-act-medium" };
        const sourceLabel = g.source === "data_driven" ? "数据驱动"
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

        return card;
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

    function renderClaimItem(c) {
        const item = el("div", { class: "list-item" });
        const conflicts = c.conflicts || [];
        const hasConflict = conflicts.length > 0;
        const headChildren = [
            el("span", { class: "list-item-title", text: c.statement || "(无陈述)" }),
            el("span", { class: "badge badge-info", text: c.role || "contribution" }),
            el("span", { class: "badge badge-neutral", text: `证据 ${c.evidence_count}` }),
        ];
        // 争议徽章：相关文献冲突非空 → 争议中（红色）；否则按状态
        if (hasConflict) {
            headChildren.push(el("span", {
                class: "badge badge-danger claim-conflict-badge",
                text: `争议中 ${conflicts.length}`,
                title: "该 Claim 的证据来源存在文献冲突，点击展开查看争议双方",
            }));
        }
        item.appendChild(el("div", { class: "list-item-head" }, headChildren));
        // 状态徽章单独一行（便于颜色识别）
        item.appendChild(el("div", { class: "mt-8" }));
        item.insertAdjacentHTML("beforeend", `<div class="mt-8">${statusBadge(c.status)}</div>`);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                const body = el("div", { class: "list-item-body" });
                body.innerHTML = `
                    <dl>
                        <dt>Claim ID</dt><dd class="mono">${escapeHtml(c.claim_id)}</dd>
                        <dt>陈述</dt><dd>${escapeHtml(c.statement)}</dd>
                        <dt>角色</dt><dd>${escapeHtml(c.role || "—")}</dd>
                        <dt>状态</dt><dd>${escapeHtml(c.status)}</dd>
                        <dt>证据数</dt><dd>${c.evidence_count}</dd>
                        <dt>来源 Idea</dt><dd class="mono">${escapeHtml(c.source_idea_id || "—")}</dd>
                        <dt>创建时间</dt><dd class="mono">${escapeHtml(formatTime(c.created_at))}</dd>
                        <dt>验证时间</dt><dd class="mono">${escapeHtml(formatTime(c.verified_at))}</dd>
                    </dl>
                    ${(c.evidence_refs && c.evidence_refs.length)
                        ? `<div class="mt-8"><strong>证据引用（可点击溯源）：</strong></div><div class="claim-ev-refs">${c.evidence_refs.map(r => renderClaimEvidenceRef(r)).join("")}</div>`
                        : ""}
                    ${hasConflict
                        ? `<div class="mt-8"><strong class="claim-conflict-title">文献冲突（争议中）：</strong></div>
                           <div class="claim-conflicts">${conflicts.map(cf => renderConflictCard(cf)).join("")}</div>`
                        : ""}
                `;
                item.appendChild(body);
            }
        });
        return item;
    }

    // 渲染 Claim 的一条证据引用（type=paper → 跳转论文页定位；type=experiment → 跳转实验页）
    function renderClaimEvidenceRef(r) {
        const id = r.id || "";
        const label = r.type === "paper" ? "论文" : (r.type === "experiment" ? "实验" : "证据");
        const chunk = r.chunk_id ? ` · chunk ${escapeHtml(String(r.chunk_id).slice(-8))}` : "";
        if (r.type === "paper" && id) {
            return `<span class="claim-ev-ref">
                <span class="badge badge-neutral">${label}</span>
                <a href="#" class="claim-ev-link" data-ev-type="paper" data-ev-id="${escapeHtml(id)}"
                   onclick="return window.__sraGoPaper && window.__sraGoPaper('${escapeHtml(id)}')">
                   ${escapeHtml(id.slice(-12))}</a>${chunk}</span>`;
        }
        if (r.type === "experiment" && id) {
            return `<span class="claim-ev-ref">
                <span class="badge badge-neutral">${label}</span>
                <a href="#" class="claim-ev-link" data-ev-type="experiment" data-ev-id="${escapeHtml(id)}"
                   onclick="return window.__sraGoExperiment && window.__sraGoExperiment('${escapeHtml(id)}')">
                   ${escapeHtml(id.slice(-12))}</a>${chunk}</span>`;
        }
        return `<span class="claim-ev-ref"><span class="badge badge-neutral">${label}</span> ${escapeHtml(id.slice(-12))}${chunk}</span>`;
    }

    // 渲染一条文献冲突（争议双方来源 + 处置建议）
    function renderConflictCard(cf) {
        const sources = (cf.sources || []).map(s => {
            const stance = s.stance === "refute" ? "反对" : "支持";
            const stanceCls = s.stance === "refute" ? "conflict-stance-refute" : "conflict-stance-support";
            const title = s.title || s.paper_id || "来源论文";
            const link = s.paper_id
                ? `<a href="#" class="claim-ev-link" onclick="return window.__sraGoPaper && window.__sraGoPaper('${escapeHtml(s.paper_id)}')">查看论文 →</a>`
                : "";
            return `<div class="conflict-src ${stanceCls}">
                <span class="conflict-stance-tag">${stance}</span>
                <span class="conflict-src-title">${escapeHtml(title)}</span>
                ${link}</div>`;
        }).join("");
        const conf = Number(cf.confidence || 0);
        return `<div class="conflict-card">
            <div class="conflict-head">
                <span class="gap-tag gap-tag-danger">文献冲突</span>
                <span class="gap-priority">置信度 ${conf.toFixed(2)}</span>
                ${cf.subquery ? `<span class="gap-source">子问题：${escapeHtml(cf.subquery)}</span>` : ""}
            </div>
            <div class="conflict-claim">${escapeHtml(cf.claim || "(无冲突陈述)")}</div>
            <div class="conflict-srcs">${sources}</div>
            ${cf.resolution ? `<div class="conflict-resolution"><strong>处置建议：</strong>${escapeHtml(cf.resolution)}</div>` : ""}
        </div>`;
    }

    // ===== 5. 实验页 =====

    async function renderExperiments(content) {
        content.appendChild(el("div", { class: "loading" }, "加载中…"));
                const body = el("div", { class: "list-item-body" });
                body.innerHTML = `
                    <dl>
                        <dt>Claim ID</dt><dd>${escapeHtml(c.claim_id)}</dd>
                        <dt>陈述</dt><dd>${escapeHtml(c.statement)}</dd>
                        <dt>角色</dt><dd>${escapeHtml(c.role || "—")}</dd>
                        <dt>状态</dt><dd>${escapeHtml(c.status)}</dd>
                        <dt>证据数</dt><dd>${c.evidence_count}</dd>
                        <dt>来源 Idea</dt><dd>${escapeHtml(c.source_idea_id || "—")}</dd>
                        <dt>创建时间</dt><dd>${escapeHtml(formatTime(c.created_at))}</dd>
                        <dt>验证时间</dt><dd>${escapeHtml(formatTime(c.verified_at))}</dd>
                    </dl>
                `;
                // 证据引用：可点击跳转
                if (c.evidence_refs && c.evidence_refs.length) {
                    const evBox = el("div", { class: "mt-8" });
                    evBox.appendChild(el("strong", {}, "证据引用："));
                    evBox.appendChild(buildEvidenceList(c.evidence_refs));
                    body.appendChild(evBox);
                }
                item.appendChild(body);
            }
        });
        return item;
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
                body.innerHTML = `
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
                item.appendChild(body);
            }
        });
        return item;
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
                    "路线 A：调研 → 假设生成 → 搜索空间 → LLM 引导搜索 → 验证 → 汇报。下方展示发现工作流的产出与节点执行状态。"),
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

            // 更新侧边栏 badge
            setBadge("badge-gaps", (report.gaps || []).length || null);

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

    function renderHuman(content) {
        const data = state.statusCache;
        const pending = data && data.pending_human;

        if (!pending) {
            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "人工节点交互"),
                el("p", { class: "page-desc" },
                    "当 Pipeline 遇到人工节点时，请求会显示在此页面。提交响应后 Pipeline 将继续执行。"),
            ]));
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

        // 渲染请求
        const reqCard = el("div", { class: "human-request" });
        reqCard.appendChild(el("div", { class: "request-label", text: "需要人工输入" }));
        reqCard.appendChild(el("div", { class: "request-prompt", text: pending.prompt || "(无提示)" }));
        if (pending.options && pending.options.length) {
        // ===== 有待处理请求：醒目提醒 =====
        const alertBanner = el("div", { class: "human-alert-banner" }, [
            el("span", { class: "human-alert-icon" }, "⚠"),
            el("div", { class: "human-alert-text" }, [
                el("strong", {}, "需要您的人工决策"),
                el("span", { class: "small" },
                    ` · 出现于 ${formatTime(pending.appeared_at)} · Pipeline 已暂停等待您的响应`),
            ]),
        ]);
        content.appendChild(alertBanner);

        // ===== 决策辅助：当前上下文 =====
        const ctxCard = el("div", { class: "card" });
        ctxCard.appendChild(el("div", { class: "card-title" }, "决策上下文 · 辅助判断"));

        // 当前阶段与状态
        const ctxRow = el("div", { class: "ctx-info-row" });
        const counts = (data && data.counts) || {};
        const ctxItems = [
            ["当前阶段", data ? (data.current_stage || "—") : "—"],
            ["Pipeline 状态", data ? data.status : "—"],
            ["已入库论文", String(counts.papers || 0)],
            ["Claim 数", String(counts.claims || 0)],
            ["实验数", String(counts.experiments || 0)],
        ];
        ctxItems.forEach(([k, v]) => {
            ctxRow.appendChild(el("div", { class: "ctx-info-item" }, [
                el("div", { class: "ctx-info-label", text: k }),
                el("div", { class: "ctx-info-value", text: v }),
            ]));
        });
        ctxCard.appendChild(ctxRow);

        // 节点历史摘要（最近 3 个节点）
        const history = (data && data.node_history) || [];
        if (history.length) {
            const recent = history.slice(-3).reverse();
            const histList = el("div", { class: "ctx-history" }, [
                el("div", { class: "ctx-history-title small muted" }, "最近节点："),
            ]);
            recent.forEach(h => {
                histList.appendChild(el("div", { class: "ctx-history-item small" },
                    `${h.node_id || "?"} — ${h.status || "?"}：${(h.summary || "").slice(0, 80)}`));
            });
            ctxCard.appendChild(histList);
        }

        // 人工节点携带的 context 数据
        const ctxData = pending.context || {};
        const ctxKeys = Object.keys(ctxData).filter(k =>
            ctxData[k] != null && ctxData[k] !== "" && typeof ctxData[k] !== "object"
        );
        if (ctxKeys.length) {
            const ctxDataList = el("div", { class: "ctx-data-list" }, [
                el("div", { class: "ctx-history-title small muted" }, "节点上下文数据："),
            ]);
            ctxKeys.forEach(k => {
                ctxDataList.appendChild(el("div", { class: "ctx-data-item small mono" },
                    `${k} = ${ctxData[k]}`));
            });
            ctxCard.appendChild(ctxDataList);
        }

        // 决策建议
        const hints = el("div", { class: "ctx-hints" }, [
            el("div", { class: "ctx-hints-title small" }, "决策建议："),
            el("ul", { class: "ctx-hints-list" }, [
                el("li", { class: "small muted" },
                    "「确认」：同意当前 Agent 产出，Pipeline 继续下一节点"),
                el("li", { class: "small muted" },
                    "「提交修改」：在文本框输入修改意见，Agent 将根据意见调整"),
                el("li", { class: "small muted" },
                    "「回滚」：回退到上一个检查点，重新执行当前阶段（适用于产出质量不佳）"),
                el("li", { class: "small muted" },
                    "「中止」：终止本次 Pipeline 运行（不可恢复）"),
            ]),
        ]);
        ctxCard.appendChild(hints);
        content.appendChild(ctxCard);

        // ===== 请求详情 =====
        const reqCard = el("div", { class: "card human-request-card" });
        reqCard.appendChild(el("div", { class: "request-label" }, "Agent 请求内容"));
        // 结构化渲染 prompt：识别编号列表/项目符号/标题行，避免一整块纯文本
        reqCard.appendChild(renderPromptStructured(pending.prompt || "(无提示)"));

        if (pending.options && pending.options.length) {
            const optsLabel = el("div", { class: "small muted mt-8" }, "可选项（点击填入文本框）：");
            reqCard.appendChild(optsLabel);
            const opts = el("div", { class: "request-options" });
            pending.options.forEach(o => {
                opts.appendChild(el("span", { class: "badge badge-info", text: o }));
            });
            reqCard.appendChild(opts);
        }
        reqCard.appendChild(el("div", { class: "small muted" },
            `出现时间：${formatTime(pending.appeared_at)} · 允许自由文本：${pending.allow_free_text ? "是" : "否"}`));

        // 响应表单
        reqCard.appendChild(el("div", { class: "small muted mt-8" },
            `允许自由文本：${pending.allow_free_text ? "是" : "否"}`));

        // ===== 响应表单 =====
        const form = el("div", { class: "mt-16" });
        const textarea = el("textarea", {
            class: "textarea",
            id: "human-text",
            placeholder: "在此输入修改意见或确认说明（确认时也可留空，提交时将使用 'ok'）",
        });
        // 恢复草稿（轮询/切页导致重建时保留用户已输入的内容）
        if (state.humanDraft) textarea.value = state.humanDraft;
        textarea.addEventListener("input", () => { state.humanDraft = textarea.value; });
        form.appendChild(el("label", { class: "field-label" }, "响应文本"));
        form.appendChild(textarea);

        // 选项快捷按钮（如果有 options）
            rows: "4",
            placeholder: "在此输入修改意见或确认说明。\n• 直接点「确认」可留空（将使用 'ok'）\n• 或输入具体修改建议\n• Ctrl+Enter 快速提交修改",
        });
        form.appendChild(el("label", { class: "field-label" }, "响应文本"));
        form.appendChild(textarea);

        // Ctrl+Enter 快捷提交
        textarea.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                submitHuman("continue", textarea.value.trim().length > 0);
            }
        });

        // 选项快捷按钮
        if (pending.options && pending.options.length) {
            const optRow = el("div", { class: "btn-row mt-8" });
            pending.options.forEach(o => {
                optRow.appendChild(el("button", {
                    class: "btn btn-secondary btn-sm",
                    text: o,
                    onclick: () => {
                        textarea.value = o;
                        state.humanDraft = o; // 同步保存草稿
                    },
                }));
            });
            form.appendChild(optRow);
        }

        // 操作按钮
        const actions = el("div", { class: "btn-row mt-16" }, [
            el("button", {
                class: "btn btn-success",
                onclick: () => submitHuman("continue", false),
            }, "确认（continue）"),
            el("button", {
                class: "btn",
                onclick: () => submitHuman("continue", true),
            }, "提交修改"),
            el("button", {
                class: "btn btn-secondary",
                onclick: () => submitHuman("rollback", false),
            }, "回滚"),
            el("button", {
                class: "btn btn-danger",
                onclick: () => submitHuman("abort", false),
            }, "中止"),
        // 操作按钮（带说明）
        const actions = el("div", { class: "human-actions" }, [
            el("button", {
                class: "btn btn-success",
                onclick: () => submitHuman("continue", false),
            }, "确认 · 同意并继续"),
            el("button", {
                class: "btn",
                onclick: () => submitHuman("continue", true),
            }, "提交修改 · 带意见继续"),
            el("button", {
                class: "btn btn-secondary",
                onclick: () => {
                    if (confirm("确认回滚？Pipeline 将回退到上一个检查点重新执行当前阶段。")) {
                        submitHuman("rollback", false);
                    }
                },
            }, "回滚 · 重新执行"),
            el("button", {
                class: "btn btn-danger",
                onclick: () => {
                    if (confirm("确认中止？将终止本次 Pipeline 运行，不可恢复。")) {
                        submitHuman("abort", false);
                    }
                },
            }, "中止 · 终止运行"),
        ]);
        form.appendChild(actions);

        async function submitHuman(action, useText) {
            const payload = { action };
            if (action === "continue") {
                if (useText) {
                    const text = textarea.value.trim();
                    if (!text) {
                        showToast("请输入修改内容", "error");
                        return;
                    }
                    payload.text = text;
                } else {
                    payload.text = textarea.value.trim() || "ok";
                }
            }
            try {
                const r = await api("POST",
                    `/api/projects/${state.currentProjectId}/human-response`, payload);
                if (r.submitted) {
                    state.humanDraft = "";
                    state.humanFingerprint = null; // 强制下一轮重建为空状态
                    showToast("响应已提交", "success");
                    const msgs = {
                        continue: "已确认，Pipeline 继续执行",
                        rollback: "已回滚，Pipeline 将从上一检查点重新执行",
                        abort: "已中止，Pipeline 运行终止",
                    };
                    showToast(msgs[action] || "响应已提交", "success");
                } else {
                    showToast("未找到等待中的请求", "error");
                }
                pollStatus();
                renderPage();
            } catch (e) {
                // 后端 409/404 的 detail 已由 api() 透传到 e.message
                showToast("提交失败：" + e.message, "error");
                state.humanFingerprint = null; // 让下一轮轮询重建，避免陈旧表单误导
                showToast("提交失败：" + e.message, "error");
            }
        }

        reqCard.appendChild(form);
        content.appendChild(reqCard);
    }

    // ===== 初始化 =====

    function init() {
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

        // 默认页
        setActivePage("dashboard");
    }

    document.addEventListener("DOMContentLoaded", init);
})();
