/* 科研 Agent 系统前端逻辑 —— 单页应用，原生 JS 实现 */
(function () {
    "use strict";

    // ===== 全局状态 =====
    const state = {
        currentProjectId: null,
        currentPage: "create",
        statusCache: null,
        pollTimer: null,
        lastPollAt: 0,
        runMode: "",            // pipeline / discovery（由 /discoveries 与启动动作同步）
        discoveryCache: null,   // 最近一次 /discoveries 结果
        humanFingerprint: null, // 最近一次 pending_human 的指纹（用于跳过无变化的整页重建）
        humanDraft: "",         // 人工输入草稿（轮询重建时恢复）
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
            try {
                const data = await api("POST", "/api/projects", { topic });
                state.currentProjectId = data.project_id;
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
            // 并行拉取论文列表 + 检索证据链（审计轨迹）
            const evData = await api("GET", `/api/projects/${state.currentProjectId}/evidence`)
                .catch(() => null);
            const paperData = await api("GET", `/api/projects/${state.currentProjectId}/papers`);
            const papers = paperData.papers || [];
            clear(content);
            content.appendChild(el("div", { class: "page-header" }, [
                el("h2", { class: "page-title" }, "论文浏览"),
                el("p", { class: "page-desc" }, `共 ${papers.length} 篇入库论文，点击条目展开详情。`),
            ]));
            // 检索证据链卡片（真实模式下由 Sciverse/arXiv/S2 检索命中记录生成）
            content.appendChild(renderEvidenceCard(evData));
            if (!papers.length) {
                content.appendChild(el("div", { class: "list-empty" }, "暂无论文，请先启动 research 阶段"));
                return;
            }
            const list = el("div", { class: "list" });
            papers.forEach(p => list.appendChild(renderPaperItem(p)));
            content.appendChild(list);
        } catch (e) {
            clear(content);
            content.appendChild(el("div", { class: "status-banner danger" },
                `加载失败：${escapeHtml(e.message)}`));
        }
    }

    // ===== 检索证据链卡片（审计轨迹：query → source → 命中 → paper）=====

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
                "每次检索命中的完整记录：子问题 → 数据源 → 证据 → 是否入库。Sciverse 证据含 doc_id/offset，可回读原文核验。"),
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
            const group = el("div", { class: "ev-group" }, [
                el("div", { class: "ev-group-head" }, [
                    el("span", { class: "ev-group-q", text: sq }),
                    el("span", { class: "ev-group-count", text: `${items.length} 条命中` }),
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
        row.appendChild(el("span", {
            class: e.paper_id ? "ev-entry-linked" : "ev-entry-unlinked",
            text: e.paper_id ? "✓ 已入库" : "未入库",
        }));
        return row;
    }

    function renderPaperItem(p) {
        const item = el("div", { class: "list-item" });
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
        headChildren.push(el("span", { class: "list-item-meta", text:
            (p.authors || []).slice(0, 2).join(", ") + ((p.authors || []).length > 2 ? " et al." : "") }));
        const head = el("div", { class: "list-item-head" }, headChildren);
        item.appendChild(head);

        item.addEventListener("click", () => {
            const expanded = item.classList.toggle("expanded");
            if (expanded && !item.querySelector(".list-item-body")) {
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
                item.appendChild(body);
            }
        });
        return item;
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
                evBlock.appendChild(el("div", { class: "gap-ev-item" },
                    `[${ev.paper_id ? ev.paper_id.slice(-8) : "?"}] ${title}${snippet}`));
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
                        <dt>Claim ID</dt><dd class="mono">${escapeHtml(c.claim_id)}</dd>
                        <dt>陈述</dt><dd>${escapeHtml(c.statement)}</dd>
                        <dt>角色</dt><dd>${escapeHtml(c.role || "—")}</dd>
                        <dt>状态</dt><dd>${escapeHtml(c.status)}</dd>
                        <dt>证据数</dt><dd>${c.evidence_count}</dd>
                        <dt>来源 Idea</dt><dd class="mono">${escapeHtml(c.source_idea_id || "—")}</dd>
                        <dt>创建时间</dt><dd class="mono">${escapeHtml(formatTime(c.created_at))}</dd>
                        <dt>验证时间</dt><dd class="mono">${escapeHtml(formatTime(c.verified_at))}</dd>
                    </dl>
                    ${c.evidence_refs && c.evidence_refs.length
                        ? `<div class="mt-8"><strong>证据引用：</strong></div><pre class="code-block">${escapeHtml(JSON.stringify(c.evidence_refs, null, 2))}</pre>`
                        : ""}
                `;
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
                        ${r.evidence_refs && r.evidence_refs.length
                            ? `<div class="mt-8"><strong>证据引用：</strong></div><pre class="code-block">${escapeHtml(JSON.stringify(r.evidence_refs, null, 2))}</pre>`
                            : ""}
                    `;
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

        // 渲染请求
        const reqCard = el("div", { class: "human-request" });
        reqCard.appendChild(el("div", { class: "request-label", text: "需要人工输入" }));
        reqCard.appendChild(el("div", { class: "request-prompt", text: pending.prompt || "(无提示)" }));
        if (pending.options && pending.options.length) {
            const opts = el("div", { class: "request-options" });
            pending.options.forEach(o => {
                opts.appendChild(el("span", { class: "badge badge-info", text: o }));
            });
            reqCard.appendChild(opts);
        }
        reqCard.appendChild(el("div", { class: "small muted" },
            `出现时间：${formatTime(pending.appeared_at)} · 允许自由文本：${pending.allow_free_text ? "是" : "否"}`));

        // 响应表单
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
                } else {
                    showToast("未找到等待中的请求", "error");
                }
                pollStatus();
                renderPage();
            } catch (e) {
                // 后端 409/404 的 detail 已由 api() 透传到 e.message
                showToast("提交失败：" + e.message, "error");
                state.humanFingerprint = null; // 让下一轮轮询重建，避免陈旧表单误导
            }
        }

        reqCard.appendChild(form);
        content.appendChild(reqCard);
    }

    // ===== 初始化 =====

    function init() {
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
        // 默认页
        setActivePage("create");

        // URL 深链支持：?project=<project_id>&page=<page>（如材料页直达，便于分享/调试）
        const params = new URLSearchParams(window.location.search);
        const urlProject = (params.get("project") || "").trim();
        const urlPage = (params.get("page") || "").trim();
        if (urlProject) {
            state.currentProjectId = urlProject;
            updateProjectIdDisplay();
            startPolling();
            if (urlPage && ["progress", "papers", "materials", "gaps", "claims", "experiments", "discovery", "notes", "human"].includes(urlPage)) {
                setActivePage(urlPage);
            } else {
                setActivePage("progress");
            }
        }
    }

    document.addEventListener("DOMContentLoaded", init);
})();
