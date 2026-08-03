/* 科研 Agent 系统前端逻辑 —— 单页应用，原生 JS 实现 */
(function () {
    "use strict";

    // ===== 全局状态 =====
    const state = {
        currentProjectId: null,
        currentPage: "dashboard",
        statusCache: null,
        pollTimer: null,
        lastPollAt: 0,
        runMode: "",            // pipeline / discovery（由 /discoveries 与启动动作同步）
        discoveryCache: null,   // 最近一次 /discoveries 结果
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

    function formatTime(iso) {
        if (!iso) return "—";
        try {
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

    function downloadFile(artifactType, filename) {
        if (!state.currentProjectId) return;
        const url = `/api/projects/${state.currentProjectId}/download/${artifactType}`;
        fetch(url)
            .then(resp => {
                if (!resp.ok) throw new Error("下载失败");
                return resp.blob();
            })
            .then(blob => {
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(a.href);
                showToast("已下载 " + filename, "success");
            })
            .catch(e => showToast("下载失败: " + e.message, "error"));
    }

    function renderDownloadBar(items) {
        const bar = el("div", { class: "download-bar" }, [
            el("span", { class: "download-bar-label" }, "下载产出："),
        ]);
        items.forEach(it => {
            bar.appendChild(el("button", {
                class: "btn btn-secondary btn-sm",
                text: it.label,
                onclick: () => downloadFile(it.type, it.filename),
            }));
        });
        return bar;
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
        document.getElementById("topbar-title").textContent = titles[page] || "科研 Agent 系统";
        renderPage();
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
            const prevPending = state.statusCache && state.statusCache.pending_human;
            state.statusCache = data;
            updateBadges(data);
            const curPending = data.pending_human;
            // 人工节点出现时弹出 toast 提醒（仅状态从无→有时）
            if (!prevPending && curPending) {
                showToast("收到人工节点请求，请前往「人工节点」页面处理", "warning");
            }
            // 仅在 pending 状态变化时重渲染 human 页，避免 textarea 被清空
            const pendingChanged = !!prevPending !== !!curPending;
            if (state.currentPage === "progress") {
                renderPage();
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

        if (state.currentPage !== "create" && state.currentPage !== "dashboard" && !state.currentProjectId) {
            content.appendChild(renderNoProject());
            return;
        }

        switch (state.currentPage) {
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

        content.appendChild(el("div", { class: "loading" }, "加载 Dashboard 数据…"));
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
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
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
            { type: "research-report", label: "调研报告", filename: "research_report.md" },
            { type: "discovery-report", label: "发现报告", filename: "discovery_report.md" },
            { type: "experiment-code", label: "实验代码", filename: "run_exp.py" },
            { type: "method-doc", label: "方法文档", filename: "method_doc.md" },
            { type: "paper-draft", label: "论文稿", filename: "paper_draft.md" },
            { type: "claims-summary", label: "Claim 汇总", filename: "claims_summary.md" },
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
        ]);
        card.appendChild(btnRow);

        const resultArea = el("div", { id: "create-result" });
        card.appendChild(resultArea);

        content.appendChild(card);

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
            try {
                const data = await api("POST", "/api/projects", { topic });
                state.currentProjectId = data.project_id;
                saveProjectToStorage(data.project_id);
                updateProjectIdDisplay();
                startPolling();
                renderSidebarNotes();
                renderCreateResult(resultArea, data);
                showToast("项目已创建", "success");
            } catch (e) {
                showToast("创建失败：" + e.message, "error");
            } finally {
                btn.disabled = false;
                btn.textContent = "启动科研";
            }
        });

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

    async function fetchDiscoveries() {
        if (!state.currentProjectId) return;
        try {
            const data = await api("GET", `/api/projects/${state.currentProjectId}/discoveries`);
            state.discoveryCache = data;
            if (data.run_mode) state.runMode = data.run_mode;
            setBadge("badge-discovery", (data.relationships || []).length || null);
            if (state.currentPage === "discovery") renderPage();
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

        // 计数卡片
        content.appendChild(renderCounts(data.counts || {}));

        // 操作区
        const actionCard = el("div", { class: "card" }, [
            el("div", { class: "card-title" }, "Pipeline 操作"),
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
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    function renderExperimentItem(e) {
        const item = el("div", { class: "list-item" });
        item.appendChild(el("div", { class: "list-item-head" }, [
            el("span", { class: "list-item-title", text: e.name || "(无名称)" }),
        ]));
        item.insertAdjacentHTML("beforeend",
            `<div class="mt-8">${statusBadge(e.status)} <span class="badge badge-neutral">验证 ${e.verifies_claim_ids?.length || 0} 个 Claim</span></div>`);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
                const body = el("div", { class: "list-item-body" });
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
        content.appendChild(el("div", { class: "loading" }, "加载中…"));
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
            ]));

            // 运行模式提示
            if (data.run_mode === "discovery") {
                content.insertAdjacentHTML("beforeend",
                    `<div class="status-banner info"><span class="status-dot"></span><strong>discovery 模式</strong><span>当前项目已启用构效关系发现工作流，结果随轮询自动刷新。</span></div>`);
            } else if (!data.run_mode) {
                content.insertAdjacentHTML("beforeend",
                    `<div class="status-banner warning"><span class="status-dot"></span><strong>未启动</strong><span>尚未运行构效关系发现，点击下方按钮启动。</span></div>`);
            }

            // 操作区
            const actionCard = el("div", { class: "card" }, [
                el("div", { class: "card-title" }, "发现工作流操作"),
                el("div", { class: "btn-row" }, [
                    el("button", {
                        class: "btn btn-accent",
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

            // discovery 节点执行状态
            if (summary.nodes && summary.nodes.length) {
                content.appendChild(renderDiscoveryNodes(summary.nodes));
            }

            // relationships 列表
            content.appendChild(renderRelationships(data.relationships || []));
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
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
                    const gapText = typeof g === "string" ? g : (g.gap || g.description || JSON.stringify(g));
                    const evidence = typeof g === "object" ? (g.evidence || g.source_papers || []) : [];
                    const item = el("div", { class: "list-item gap-item" });
                    item.appendChild(el("div", { class: "list-item-head" }, [
                        el("span", { class: "gap-number", text: `#${i + 1}` }),
                        el("span", { class: "list-item-title", text: gapText }),
                    ]));
                    if (evidence.length) {
                        item.appendChild(el("div", { class: "gap-evidence" },
                            `证据来源：${evidence.slice(0, 3).join("、")}${evidence.length > 3 ? ` 等 ${evidence.length} 篇` : ""}`));
                    }
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
        reqCard.appendChild(el("div", { class: "small muted mt-8" },
            `允许自由文本：${pending.allow_free_text ? "是" : "否"}`));

        // ===== 响应表单 =====
        const form = el("div", { class: "mt-16" });
        const textarea = el("textarea", {
            class: "textarea",
            id: "human-text",
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
                    },
                }));
            });
            form.appendChild(optRow);
        }

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
                showToast("提交失败：" + e.message, "error");
            }
        }

        reqCard.appendChild(form);
        content.appendChild(reqCard);
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
