"""生成系统架构 SVG 图（重写版 v2.2）。

设计目标（针对评审看到 v2.1 时的"重叠"反馈）：
- 不再把 5 个阶段挤在一行：每个阶段独占一个横条，纵向堆叠
- 节点独占一行（name + I/O），节点高度足够容纳 2 行文字
- 层与层之间留 30px 间隔，避免文字出框
- 阶段之间用宽箭头 + 编号标注
- 物理硬筛 / 证据链 / 5 维评分 / 系统级指标 用统一侧边栏集中说明
- 颜色按 5 阶段线性渐变；硬筛/检查点用特殊色

布局（1600 × 1500）：
  y 0    — 标题 (60px)
  y 80   — 应用层 (110px, 含 Web/CLI/HF/Replit 4 个入口)
  y 210  — 编排层主框架（5 阶段堆叠，每阶段 ~110px，含 41 节点）
        y 210-300  Research (10 节点 + cp)
        y 320-410  Ideation (5 节点 + cp)
        y 430-520  Design (6 节点 + cp)
        y 540-660  Experiment (9 节点 + cp)
        y 680-780  Writing (7 节点 + cp)
        y 800-900  Discovery (6 节点含 ★物理硬筛)
  y 920  — 阶段间反馈弧线（Research→Discovery、Discovery→Experiment、Experiment→Writing）
  y 1050 — 工具与 LLM 层（8 模块）
  y 1230 — 数据与知识层（4 模块）
  y 1370 — 外部数据源层（8 个）+ 系统指标栏（9 指标）
  y 1470 — 图例
"""
from __future__ import annotations
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"


def esc(s: str) -> str:
    """转义 SVG 文本中的特殊字符。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_svg() -> str:
    W = 1700  # 加宽

    # ---------- defs ----------
    defs = """<defs>
  <linearGradient id="research" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#4e54c8"/><stop offset="1" stop-color="#8f94fb"/></linearGradient>
  <linearGradient id="ideation" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#11998e"/><stop offset="1" stop-color="#38ef7d"/></linearGradient>
  <linearGradient id="design" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ee0979"/><stop offset="1" stop-color="#ff6a00"/></linearGradient>
  <linearGradient id="experiment" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fc4a1a"/><stop offset="1" stop-color="#f7b733"/></linearGradient>
  <linearGradient id="writing" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#6a11cb"/><stop offset="1" stop-color="#2575fc"/></linearGradient>
  <linearGradient id="discovery" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ff6a00"/><stop offset="1" stop-color="#ee0979"/></linearGradient>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#888"/></marker>
  <marker id="arrow-gold" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#c8a415"/></marker>
  <marker id="arrow-blue" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#2575fc"/></marker>
</defs>"""

    parts = []

    # ---------- 顶部标题 ----------
    parts.append(f'<text x="{W//2}" y="35" text-anchor="middle" font-size="22" font-weight="700" fill="#1a1a2e">科研智能体系统架构图</text>')
    parts.append(f'<text x="{W//2}" y="58" text-anchor="middle" font-size="13" fill="#666">5 阶段流水线 · 41 个节点 · 物理一致性硬筛 · 5 维度可信度评分 · 双路交叉验证</text>')

    # ========== ① 应用层 ==========
    y0 = 90
    parts.append(f'<rect x="40" y="{y0}" width="{W-80}" height="80" rx="8" fill="#f0f4ff" stroke="#aab8d8"/>')
    parts.append(f'<text x="60" y="{y0+18}" font-size="13" font-weight="600" fill="#1a1a2e">① 应用层 · Application Layer</text>')
    apps = [
        ("Web Dashboard", "FastAPI + 原生 JS", "项目管理 / 报告 / 数据源合规 / 系统级指标", 80),
        ("CLI 工具", "python -m runtime.cli", "批量执行 / 离线运行 / Golden Set 回归", 480),
        ("HuggingFace Space", "Static Mirror", "前端镜像（无需本地复现）", 870),
        ("Replit 后端", "FastAPI Deploy", "云端后端 API 持续运行", 1280),
    ]
    for name, sub, desc, x in apps:
        parts.append(f'<rect x="{x}" y="{y0+25}" width="350" height="45" rx="6" fill="white" stroke="#4e54c8"/>')
        parts.append(f'<text x="{x+15}" y="{y0+42}" font-size="12" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        parts.append(f'<text x="{x+15}" y="{y0+57}" font-size="10" fill="#666">{esc(sub)}</text>')
        # 描述文字如果超过容器宽度则缩小
        desc_fs = 10 if len(desc) <= 18 else 9
        parts.append(f'<text x="{x+150}" y="{y0+42}" font-size="{desc_fs}" fill="#888">{esc(desc)}</text>')

    # ========== ② 编排层：5 阶段堆叠 ==========
    layer2_y = 195
    # 计算总高：每阶段 90px（标题 + 节点行 + 间距）
    # 5 阶段 × 90px = 450px
    stage_h = 90
    layer2_total_h = 5 * stage_h + 20
    parts.append(f'<rect x="40" y="{layer2_y}" width="{W-80}" height="{layer2_total_h}" rx="8" fill="#fefefe" stroke="#d0d4e0"/>')
    parts.append(f'<text x="60" y="{layer2_y+18}" font-size="13" font-weight="600" fill="#1a1a2e">② 智能体编排层 · Orchestration Layer（41 节点 + 5 检查点 + 1 物理硬筛）</text>')

    stages = [
        ("stage_1_research", "Research 调研 (10 节点)", "url(#research)", 0, [
            ("topic_refine", "主题精炼"),
            ("subquery_decompose", "子问题分解"),
            ("cp", "🛂"),
            ("topic_confirm", "主题确认"),
            ("paper_fetch", "三源抓取"),
            ("paper_filter", "5维筛选"),
            ("paper_ingest", "PDF 解析"),
            ("material_extraction", "材料抽取"),
            ("cross_validate", "交叉验证"),
            ("research_gap", "Gap 识别"),
        ]),
        ("stage_2_ideation", "Ideation 构思 (5 节点)", "url(#ideation)", 1, [
            ("brainstorm", "思路生成"),
            ("cp", "🛂"),
            ("idea_discuss", "思路讨论"),
            ("idea_validate", "三维度验证"),
            ("claim_draft", "Claim 草拟"),
        ]),
        ("stage_3_design", "Design 设计 (6 节点)", "url(#design)", 2, [
            ("atom_decompose", "原子分解"),
            ("method_formalize", "方法形式化"),
            ("cp", "🛂"),
            ("method_review", "方法审稿"),
            ("claim_evidence_link", "Claim↔证据"),
            ("method_artifact", "Artifact"),
        ]),
        ("stage_4_experiment", "Experiment 实验 (9 节点)", "url(#experiment)", 3, [
            ("experiment_config", "实验配置"),
            ("code_generate", "代码生成"),
            ("code_review", "代码审查"),
            ("cp", "🛂"),
            ("experiment_run", "沙盒执行"),
            ("experiment_review", "结果审查"),
            ("anomaly_check", "异常检测"),
            ("claim_verify", "Claim 验证"),
            ("outcome_assess", "成败评估"),
        ]),
        ("stage_5_writing", "Writing 写作 (7 节点)", "url(#writing)", 4, [
            ("provenance_check", "溯源硬校验"),
            ("cp", "🛂"),
            ("style_learn", "风格学习"),
            ("outline", "大纲"),
            ("section_draft", "章节填充"),
            ("review", "模拟审稿"),
            ("revise", "终稿"),
        ]),
    ]

    # 节点布局参数
    # Stage 1 (Research) 有 10 节点，最密：右侧可用空间 = 1660 - 230 - 20 = 1410px
    # 10 节点 × 130 + 9 间距 × 6 = 1300 + 54 = 1354 < 1410 ✓
    node_w = 130
    node_h = 56
    nodes_x_start = 230
    node_gap = 6
    for stage_id, stage_label, fill, idx, nodes in stages:
        sy = layer2_y + 28 + idx * stage_h
        # 阶段标题（左侧独立标签框）
        parts.append(f'<rect x="55" y="{sy}" width="160" height="68" rx="6" fill="{fill}" stroke="#444"/>')
        # 阶段标题文字（拆 2 行）
        label_main, label_sub = stage_label.split(" (", 1)
        label_sub = "(" + label_sub
        parts.append(f'<text x="135" y="{sy+25}" text-anchor="middle" font-size="13" font-weight="700" fill="white">{esc(label_main)}</text>')
        parts.append(f'<text x="135" y="{sy+48}" text-anchor="middle" font-size="11" fill="white">{esc(label_sub)}</text>')

        # 节点
        for j, (nid, label) in enumerate(nodes):
            nx = nodes_x_start + j * (node_w + node_gap)
            ny = sy
            if nid == "cp":
                # 检查点：用纯文本标记 "CP" + 中文 "人工检查点"，避免依赖 emoji 字体
                parts.append(f'<rect x="{nx}" y="{ny}" width="{node_w}" height="{node_h}" rx="5" fill="#ffe5e5" stroke="#d44" stroke-width="1.5" stroke-dasharray="3,2"/>')
                parts.append(f'<text x="{nx+node_w//2}" y="{ny+24}" text-anchor="middle" font-size="16" font-weight="700" fill="#d44">CP</text>')
                parts.append(f'<text x="{nx+node_w//2}" y="{ny+40}" text-anchor="middle" font-size="9" fill="#d44">人工检查点</text>')
            else:
                parts.append(f'<rect x="{nx}" y="{ny}" width="{node_w}" height="{node_h}" rx="5" fill="{fill}" stroke="#444" stroke-width="0.5"/>')
                # 节点名：如果超过 14 字符自动缩小字号
                name_fs = 11 if len(nid) <= 14 else 9 if len(nid) <= 18 else 8
                parts.append(f'<text x="{nx+node_w//2}" y="{ny+22}" text-anchor="middle" font-size="{name_fs}" font-weight="700" fill="white">{esc(nid)}</text>')
                # label：超长也缩小
                label_fs = 10 if len(label) <= 8 else 8
                parts.append(f'<text x="{nx+node_w//2}" y="{ny+42}" text-anchor="middle" font-size="{label_fs}" fill="white">{esc(label)}</text>')
            # 节点间连线
            if j < len(nodes) - 1:
                next_x = nx + node_w
                mid_y = ny + node_h // 2
                parts.append(f'<line x1="{next_x+1}" y1="{mid_y}" x2="{next_x+node_gap-1}" y2="{mid_y}" stroke="#888" stroke-width="1" marker-end="url(#arrow)"/>')

    # ========== Discovery（横向独立模块，单独一行）==========
    # 放在 Stage 5 之下，独立横条
    discovery_y = layer2_y + 28 + 5 * stage_h + 5
    parts.append(f'<rect x="40" y="{discovery_y-5}" width="{W-80}" height="100" rx="8" fill="#fff0f5" stroke="#ff6a00" stroke-width="1"/>')
    parts.append(f'<text x="60" y="{discovery_y+10}" font-size="12" font-weight="700" fill="#ff6a00">③ 独立子系统 · Discovery 构效关系发现 (6 节点 + 物理硬筛)</text>')
    disc_nodes = [
        ("hypothesis_seed", "假设种子", "url(#discovery)"),
        ("search_space", "搜索空间", "url(#discovery)"),
        ("llm_guided_search", "MCTS+LLM 搜索", "url(#discovery)"),
        ("physics_hard_filter", "★ 物理硬筛", "filter"),
        ("discovery_validate", "双路 CV (MP+规则)", "url(#discovery)"),
        ("discovery_report", "5 维评分报告", "url(#discovery)"),
    ]
    for j, (nid, label, fill) in enumerate(disc_nodes):
        nx = nodes_x_start + j * (node_w + node_gap)
        ny = discovery_y + 22
        if fill == "filter":
            parts.append(f'<rect x="{nx}" y="{ny}" width="{node_w}" height="{node_h}" rx="5" fill="#fff3cd" stroke="#c8a415" stroke-width="2"/>')
            # 物理硬筛：去掉 🛡 emoji（部分字体不识别），用 ★ 标记 + 黄色调
            filter_fs = 9 if len("physics_hard_filter") > 14 else 11
            parts.append(f'<text x="{nx+node_w//2}" y="{ny+22}" text-anchor="middle" font-size="{filter_fs}" font-weight="700" fill="#7a5e00">★ {esc(nid)}</text>')
            parts.append(f'<text x="{nx+node_w//2}" y="{ny+42}" text-anchor="middle" font-size="9" fill="#7a5e00">118元素价态+18e</text>')
        else:
            parts.append(f'<rect x="{nx}" y="{ny}" width="{node_w}" height="{node_h}" rx="5" fill="{fill}" stroke="#444"/>')
            # discovery 节点名：自动适配字号
            name_fs = 11 if len(nid) <= 14 else 9 if len(nid) <= 18 else 8
            parts.append(f'<text x="{nx+node_w//2}" y="{ny+22}" text-anchor="middle" font-size="{name_fs}" font-weight="700" fill="white">{esc(nid)}</text>')
            label_fs = 10 if len(label) <= 8 else 8
            parts.append(f'<text x="{nx+node_w//2}" y="{ny+42}" text-anchor="middle" font-size="{label_fs}" fill="white">{esc(label)}</text>')
        # 节点间连线
        if j < len(disc_nodes) - 1:
            next_x = nx + node_w
            mid_y = ny + node_h // 2
            parts.append(f'<line x1="{next_x+1}" y1="{mid_y}" x2="{next_x+node_gap-1}" y2="{mid_y}" stroke="#888" stroke-width="1" marker-end="url(#arrow)"/>')

    # ========== 阶段间反馈弧（合并到 Layer2 之后一段）==========
    feedback_y = discovery_y + 110
    parts.append(f'<rect x="40" y="{feedback_y}" width="{W-80}" height="80" rx="8" fill="#fffbf0" stroke="#c8a415" stroke-dasharray="3,3"/>')
    parts.append(f'<text x="60" y="{feedback_y+18}" font-size="12" font-weight="700" fill="#7a5e00">★ 跨阶段反馈弧：Research Gap → Discovery 假设种子 → Experiment 验证 → Writing 引用</text>')
    feedbacks = [
        ("Research Gap", "→ Discovery 假设种子", "#c8a415", 80),
        ("Discovery 新发现", "→ Experiment 验证", "#c8a415", 480),
        ("Experiment 结果", "→ Writing 引用", "#2575fc", 880),
        ("Provenance 校验", "→ 证据链 (manual/retrieved)", "#6a11cb", 1280),
    ]
    for label_main, label_arrow, color, x in feedbacks:
        parts.append(f'<rect x="{x}" y="{feedback_y+28}" width="350" height="40" rx="5" fill="white" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<text x="{x+15}" y="{feedback_y+50}" font-size="12" font-weight="600" fill="{color}">{esc(label_main)}</text>')
        parts.append(f'<text x="{x+180}" y="{feedback_y+50}" font-size="11" fill="#444">{esc(label_arrow)}</text>')

    # ========== ④ 工具与 LLM 层 ==========
    tools_y = feedback_y + 100
    parts.append(f'<rect x="40" y="{tools_y}" width="{W-80}" height="100" rx="8" fill="#fffaf0" stroke="#c8a415"/>')
    parts.append(f'<text x="60" y="{tools_y+18}" font-size="13" font-weight="600" fill="#1a1a2e">④ 工具与 LLM 层 · Tooling &amp; LLM Layer</text>')
    tools = [
        ("任务路由", "config/tasks.yaml\n8 任务 / 6 Provider"),
        ("结构化输出", "function calling\nJSON schema"),
        ("向量检索", "ChromaVectorStore\n3 类查询接口"),
        ("物理一致性", "checker.py\n5 项检查"),
        ("5 维评分", "DiscoveryReliabilityScorer"),
        ("材料 CV", "MP + 规则 + OQMD\n双路降级"),
        ("冲突裁决", "IF 60% + 分区 20%\n+ 文献新度 20%"),
        ("指标聚合", "metrics.py\n9 类系统级指标"),
    ]
    tool_w = (W - 120) // 8
    for i, (name, desc) in enumerate(tools):
        x = 60 + i * tool_w
        parts.append(f'<rect x="{x}" y="{tools_y+30}" width="{tool_w-10}" height="60" rx="5" fill="white" stroke="#c8a415"/>')
        # 名称字号自适应：>6 字符用 10px
        name_fs = 11 if len(name) <= 6 else 10
        parts.append(f'<text x="{x+(tool_w-10)//2}" y="{tools_y+46}" text-anchor="middle" font-size="{name_fs}" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        for k, line in enumerate(esc(desc).split("\n")):
            line_fs = 9 if len(line) <= 18 else 8
            parts.append(f'<text x="{x+(tool_w-10)//2}" y="{tools_y+60+k*12}" text-anchor="middle" font-size="{line_fs}" fill="#666">{line}</text>')

    # ========== ⑤ 数据与知识层 ==========
    data_y = tools_y + 110
    parts.append(f'<rect x="40" y="{data_y}" width="{W-80}" height="100" rx="8" fill="#f0fff4" stroke="#3a7"/>')
    parts.append(f'<text x="60" y="{data_y+18}" font-size="13" font-weight="600" fill="#1a1a2e">⑤ 数据与知识层 · Data &amp; Knowledge Layer</text>')
    data_items = [
        ("KV 表", "save_kv/get_kv\n31 关键字段"),
        ("Paper", "保存/查询/去重\n外部 ID 索引"),
        ("Material + Property", "材料 + 性能 + 合成方法\nShannon 离子半径"),
        ("Claim / Idea / Gap", "结构化实体\n赛题要求"),
        ("Conflict", "结构化冲突实体\nIF/分区/新度裁决"),
        ("Evidence Log", "manual/retrieved\n按阶段落库"),
    ]
    data_w = (W - 120) // 6
    for i, (name, desc) in enumerate(data_items):
        x = 60 + i * data_w
        parts.append(f'<rect x="{x}" y="{data_y+30}" width="{data_w-10}" height="60" rx="5" fill="white" stroke="#3a7"/>')
        name_fs = 11 if len(name) <= 10 else 9
        parts.append(f'<text x="{x+(data_w-10)//2}" y="{data_y+46}" text-anchor="middle" font-size="{name_fs}" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        for k, line in enumerate(esc(desc).split("\n")):
            line_fs = 9 if len(line) <= 14 else 8
            parts.append(f'<text x="{x+(data_w-10)//2}" y="{data_y+60+k*12}" text-anchor="middle" font-size="{line_fs}" fill="#666">{line}</text>')

    # ========== ⑥ 外部数据源 + 系统级指标 ==========
    ext_y = data_y + 110
    parts.append(f'<rect x="40" y="{ext_y}" width="{W-80}" height="100" rx="8" fill="#fff0f0" stroke="#c84"/>')
    parts.append(f'<text x="60" y="{ext_y+18}" font-size="13" font-weight="600" fill="#1a1a2e">⑥ 外部数据源层 · External Data Sources（统一登记 · 许可证透明 · 三路降级）</text>')
    sources = [
        ("arXiv", "公开"),
        ("Semantic Scholar", "公开"),
        ("Sciverse", "学术 token"),
        ("Materials Project", "X-API-KEY"),
        ("OQMD", "公开"),
        ("MinerU", "PDF 解析"),
        ("NOMAD", "公开"),
        ("LLM (6 Provider)", "OpenAI/Ant/DS/MM/vLLM"),
    ]
    src_w = (W - 120) // 8
    for i, (name, desc) in enumerate(sources):
        x = 60 + i * src_w
        parts.append(f'<rect x="{x}" y="{ext_y+30}" width="{src_w-10}" height="60" rx="5" fill="white" stroke="#c84"/>')
        # 外部数据源：Semantic Scholar (16 字符) / Materials Project (17) 容易溢出
        src_fs = 11 if len(name) <= 10 else 9
        parts.append(f'<text x="{x+(src_w-10)//2}" y="{ext_y+48}" text-anchor="middle" font-size="{src_fs}" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        parts.append(f'<text x="{x+(src_w-10)//2}" y="{ext_y+72}" text-anchor="middle" font-size="9" fill="#666">{esc(desc)}</text>')

    # ========== ⑦ 系统级指标面板 ==========
    metrics_y = ext_y + 110
    parts.append(f'<rect x="40" y="{metrics_y}" width="{W-80}" height="80" rx="8" fill="#f5f0ff" stroke="#7a4cb8"/>')
    parts.append(f'<text x="60" y="{metrics_y+18}" font-size="13" font-weight="600" fill="#1a1a2e">⑦ 系统级指标面板 · System Metrics Panel（赛题 §4.2 强支撑 · 9 类指标自动聚合）</text>')
    metrics = [
        ("节点完成率", "41 节点"),
        ("KV 覆盖率", "31 字段"),
        ("文献抓取率", "3 源"),
        ("5 维评分", "P25/中位数/P75"),
        ("Gap 质量", "4 维度"),
        ("CV 一致性", "MP+规则"),
        ("证据链", "按阶段"),
        ("降级触发率", "三路"),
        ("流水线效率", "耗时/LLM"),
    ]
    met_w = (W - 120) // 9
    for i, (name, desc) in enumerate(metrics):
        x = 60 + i * met_w
        parts.append(f'<rect x="{x}" y="{metrics_y+28}" width="{met_w-10}" height="44" rx="5" fill="white" stroke="#7a4cb8"/>')
        met_fs = 10 if len(name) <= 6 else 9
        parts.append(f'<text x="{x+(met_w-10)//2}" y="{metrics_y+45}" text-anchor="middle" font-size="{met_fs}" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        parts.append(f'<text x="{x+(met_w-10)//2}" y="{metrics_y+62}" text-anchor="middle" font-size="8" fill="#666">{esc(desc)}</text>')

    # ========== 图例 + 关键统计 ==========
    legend_y = metrics_y + 95
    parts.append(f'<text x="60" y="{legend_y}" font-size="12" font-weight="700" fill="#1a1a2e">图例</text>')
    legend_items = [
        ("普通节点", "#4e54c8"),
        ("物理硬筛", "#c8a415"),
        ("人工检查点", "#d44"),
        ("跨阶段反馈", "#ff6a00"),
        ("工具层", "#c8a415"),
        ("数据层", "#3a7"),
        ("外部数据源", "#c84"),
        ("系统级指标", "#7a4cb8"),
    ]
    leg_w = (W - 120) // 8
    for i, (label, color) in enumerate(legend_items):
        x = 60 + i * leg_w
        parts.append(f'<rect x="{x}" y="{legend_y+8}" width="14" height="14" rx="2" fill="{color}" stroke="#666"/>')
        parts.append(f'<text x="{x+22}" y="{legend_y+19}" font-size="10">{esc(label)}</text>')

    # 关键统计：每条拆成两行（确保不超宽），统一字号 11
    
    fact_y = legend_y+ 20
    

    # ---------- 拼装 ----------
    H = fact_y + 10
    body = "\n".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, \'Segoe UI Emoji\', \'Apple Color Emoji\', \'Noto Sans CJK SC\', \'Microsoft YaHei\', sans-serif">'
        f'<rect width="{W}" height="{H}" fill="#fafbfc"/>'
        f'{defs}'
        f'{body}'
        '</svg>'
    )


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    svg = make_svg()
    OUT.write_text(svg, encoding="utf-8")
    print(f"已生成：{OUT} ({OUT.stat().st_size:,} bytes, {svg.count(chr(10))} lines)")


if __name__ == "__main__":
    main()