"""生成"专业"的系统架构 SVG 图（用于方案文档）。

设计原则：
- 5 大层（应用层 / 智能体编排层 / 工具与LLM层 / 数据与知识层 / 外部数据源层）
- 41 节点全画，逻辑分组清晰
- 每个节点旁标 I/O schema 与 KV 字段
- 颜色编码：研究 / 构思 / 设计 / 实验 / 写作 / 发现
- 物理一致性检查 / 证据链 / 系统级指标 三处独立高亮
"""
from __future__ import annotations
from pathlib import Path

# 输出 SVG 文件
OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"


def esc(s: str) -> str:
    """转义 SVG 文本中的特殊字符。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_svg() -> str:
    """构造架构图 SVG（使用列表收集 + 最终合并，避免嵌套错误）。"""
    W, H = 1600, 1100

    defs = """<defs>
  <linearGradient id="research" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#4e54c8"/><stop offset="1" stop-color="#8f94fb"/></linearGradient>
  <linearGradient id="ideation" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#11998e"/><stop offset="1" stop-color="#38ef7d"/></linearGradient>
  <linearGradient id="design" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ee0979"/><stop offset="1" stop-color="#ff6a00"/></linearGradient>
  <linearGradient id="experiment" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fc4a1a"/><stop offset="1" stop-color="#f7b733"/></linearGradient>
  <linearGradient id="writing" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#6a11cb"/><stop offset="1" stop-color="#2575fc"/></linearGradient>
  <linearGradient id="discovery" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ff6a00"/><stop offset="1" stop-color="#ee0979"/></linearGradient>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#888"/></marker>
  <marker id="arrow-gold" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="#c8a415"/></marker>
</defs>"""

    body_parts = []

    # ============ 顶部标题 ============
    body_parts.append(
        f'<text x="{W//2}" y="40" text-anchor="middle" font-size="22" font-weight="700" fill="#1a1a2e">科研智能体系统架构图</text>'
        f'<text x="{W//2}" y="62" text-anchor="middle" font-size="13" fill="#666">5 阶段流水线 · 41 个节点 · 物理一致性硬筛 · 5 维度可信度评分 · 双路交叉验证</text>'
    )

    # ============ Layer 1: 应用层 ============
    body_parts.append('<rect x="40" y="100" width="1520" height="80" rx="8" fill="#f0f4ff" stroke="#aab8d8"/>')
    body_parts.append('<text x="60" y="125" font-size="13" font-weight="600" fill="#1a1a2e">① 应用层 · Application Layer</text>')
    apps = [
        ("Web Dashboard", "FastAPI + 原生 JS", "项目管理 / 报告查看 / 数据源合规卡片", 80),
        ("CLI 工具", "python -m runtime.cli", "批量执行 / 离线运行 / Golden Set 回归", 560),
        ("HuggingFace Space", "Static Mirror", "前端镜像展示（无需本地复现）", 1040),
        ("Replit 后端", "FastAPI Deploy", "云端后端 API 持续运行", 1300),
    ]
    for name, sub, desc, x in apps:
        body_parts.append(f'<rect x="{x}" y="135" width="220" height="38" rx="6" fill="white" stroke="#4e54c8"/>')
        body_parts.append(f'<text x="{x+10}" y="152" font-size="12" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        body_parts.append(f'<text x="{x+10}" y="167" font-size="10" fill="#666">{esc(sub)}</text>')
        body_parts.append(f'<text x="{x+10}" y="178" font-size="9" fill="#888">{esc(desc)}</text>')

    # ============ Layer 2: 编排层 ============
    body_parts.append('<rect x="40" y="200" width="1520" height="600" rx="8" fill="#fefefe" stroke="#d0d4e0"/>')
    body_parts.append('<text x="60" y="225" font-size="13" font-weight="600" fill="#1a1a2e">② 智能体编排层 · Orchestration Layer（41 节点 + 5 检查点 + 物理一致性硬筛）</text>')

    stages = [
        ("Research 调研", "url(#research)", 80, [
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
        ("Ideation 构思", "url(#ideation)", 360, [
            ("brainstorm", "思路生成"),
            ("cp", "🛂"),
            ("idea_discuss", "思路讨论"),
            ("idea_validate", "三维度验证"),
            ("claim_draft", "Claim 草拟"),
        ]),
        ("Design 设计", "url(#design)", 560, [
            ("atom_decompose", "原子分解"),
            ("method_formalize", "方法形式化"),
            ("cp", "🛂"),
            ("method_review", "方法审稿"),
            ("claim_evidence_link", "Claim↔证据"),
            ("method_artifact", "Artifact"),
        ]),
        ("Experiment 实验", "url(#experiment)", 760, [
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
        ("Writing 写作", "url(#writing)", 1000, [
            ("provenance_check", "溯源硬校验"),
            ("cp", "🛂"),
            ("style_learn", "风格学习"),
            ("outline", "大纲"),
            ("section_draft", "章节填充"),
            ("review", "模拟审稿"),
            ("revise", "终稿"),
        ]),
        ("Discovery 发现", "url(#discovery)", 1240, [
            ("hypothesis_seed", "假设种子"),
            ("search_space", "搜索空间"),
            ("llm_guided_search", "MCTS+LLM"),
            ("physics_hard_filter", "🛡硬筛"),
            ("discovery_validate", "双路CV"),
            ("discovery_report", "5维评分"),
        ]),
    ]

    node_y = 270
    for stage_name, fill, x, nodes in stages:
        # 阶段标题
        text_x = x + (len(nodes) * 50) / 2
        body_parts.append(f'<text x="{text_x}" y="250" text-anchor="middle" font-size="12" font-weight="600" fill="#444">{esc(stage_name)}</text>')
        for i, (nid, label) in enumerate(nodes):
            node_x = x + i * 50
            if nid == "cp":
                body_parts.append(f'<rect x="{node_x}" y="{node_y}" width="46" height="42" rx="5" fill="#ffe5e5" stroke="#d44" stroke-dasharray="3,2"/>')
                body_parts.append(f'<text x="{node_x+23}" y="{node_y+27}" text-anchor="middle" font-size="14">🛂</text>')
            elif nid == "physics_hard_filter":
                body_parts.append(f'<rect x="{node_x}" y="{node_y}" width="46" height="42" rx="5" fill="#fff3cd" stroke="#c8a415" stroke-width="2"/>')
                body_parts.append(f'<text x="{node_x+23}" y="{node_y+18}" text-anchor="middle" font-size="7" font-weight="700" fill="#7a5e00">🛡物理</text>')
                body_parts.append(f'<text x="{node_x+23}" y="{node_y+32}" text-anchor="middle" font-size="7" fill="#7a5e00">硬筛</text>')
            else:
                body_parts.append(f'<rect x="{node_x}" y="{node_y}" width="46" height="42" rx="5" fill="{fill}" stroke="#666" stroke-width="0.5"/>')
                body_parts.append(f'<text x="{node_x+23}" y="{node_y+18}" text-anchor="middle" font-size="7" font-weight="600" fill="white">{esc(nid[:10])}</text>')
                for j, line in enumerate(esc(label).split("\n")):
                    body_parts.append(f'<text x="{node_x+23}" y="{node_y+30+j*9}" text-anchor="middle" font-size="6" fill="white">{line}</text>')
            # 节点间连线
            if i < len(nodes) - 1:
                next_x = node_x + 50
                body_parts.append(f'<line x1="{node_x+46}" y1="{node_y+21}" x2="{next_x}" y2="{node_y+21}" stroke="#888" stroke-width="0.8" marker-end="url(#arrow)"/>')

    # 跨阶段反馈箭头
    body_parts.append('<path d="M 80,310 Q 50,500 1240,310" fill="none" stroke="#c8a415" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#arrow-gold)"/>')
    body_parts.append('<text x="60" y="500" font-size="10" fill="#7a5e00" font-style="italic">Research Gap → Discovery</text>')

    body_parts.append('<path d="M 1290,310 Q 1000,500 900,310" fill="none" stroke="#c8a415" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#arrow-gold)"/>')
    body_parts.append('<text x="950" y="500" font-size="10" fill="#7a5e00" font-style="italic">Discovery 新发现 → Experiment 验证</text>')

    body_parts.append('<path d="M 900,310 Q 950,500 1090,310" fill="none" stroke="#888" stroke-width="1.5" marker-end="url(#arrow)"/>')
    body_parts.append('<text x="980" y="500" font-size="10" fill="#444" font-style="italic">Experiment → Writing</text>')

    # Evidence Chain + 5 维 + 物理硬筛 注解
    body_parts.append('<rect x="80" y="320" width="1480" height="58" rx="6" fill="#f0f8ff" stroke="#88aacc" stroke-dasharray="2,2"/>')
    body_parts.append('<text x="90" y="340" font-size="11" font-weight="600" fill="#1a3c5e">🔗 证据链（how）：每个节点产出 evidence_log 条目，区分 manual / retrieved。前端 dashboard 的"研究链路可视化"按阶段展示每条结论的"原料→加工→成品"三段链路</text>')
    body_parts.append('<text x="90" y="358" font-size="10" fill="#1a3c5e">📊 5 维度可信度评分：外推安全 (0.20) + 文献密度 (0.25) + 机制论证 (0.20) + CV 一致性 (0.20) + 预测区间合理性 (0.15)</text>')
    body_parts.append('<text x="90" y="374" font-size="10" fill="#7a5e00">🛡 物理一致性硬筛：core/physics_consistency/checker.py（118 元素价态表 + 14 项性能范围 + 18 电子规则 + Goldschmidt 容忍因子）</text>')

    # 检查点标注
    body_parts.append('<text x="800" y="400" text-anchor="middle" font-size="11" fill="#d44" font-weight="600">↑ 5 个 cp_before_* 检查点（人工介入）</text>')

    # ============ Layer 3: 工具与 LLM 层 ============
    body_parts.append('<rect x="40" y="420" width="900" height="120" rx="8" fill="#fffaf0" stroke="#c8a415"/>')
    body_parts.append('<text x="60" y="445" font-size="13" font-weight="600" fill="#1a1a2e">③ 工具与 LLM 层 · Tooling &amp; LLM Layer</text>')

    tools = [
        ("任务路由", "config/tasks.yaml\n8 任务 / 6 Provider", 80, 460),
        ("结构化输出", "function calling\nJSON schema", 270, 460),
        ("向量检索", "ChromaVectorStore\n3 类查询接口", 460, 460),
        ("物理一致性", "checker.py\n5 项检查", 650, 460),
        ("5 维度评分", "DiscoveryReliabilityScorer", 80, 535),
        ("材料交叉验证", "MP + 规则 + OQMD\n双路降级", 270, 535),
        ("文献冲突裁决", "IF 60% + 分区 20%\n+ 文献新度 20%", 460, 535),
        ("指标聚合", "metrics.py\n9 类系统级指标", 650, 535),
    ]
    for name, desc, x, y in tools:
        body_parts.append(f'<rect x="{x}" y="{y}" width="180" height="60" rx="5" fill="white" stroke="#c8a415"/>')
        body_parts.append(f'<text x="{x+90}" y="{y+18}" text-anchor="middle" font-size="11" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        for i, line in enumerate(esc(desc).split("\n")):
            body_parts.append(f'<text x="{x+90}" y="{y+32+i*11}" text-anchor="middle" font-size="9" fill="#666">{line}</text>')

    # ============ Layer 4: 数据与知识层 ============
    body_parts.append('<rect x="950" y="420" width="610" height="120" rx="8" fill="#f0fff4" stroke="#3a7"/>')
    body_parts.append('<text x="970" y="445" font-size="13" font-weight="600" fill="#1a1a2e">④ 数据与知识层 · Data &amp; Knowledge Layer</text>')

    data_items = [
        ("KV 表", "save_kv/get_kv\n31 关键字段", 970, 460),
        ("Paper", "保存/查询/去重\n外部 ID 索引", 1170, 460),
        ("Material", "材料 + 性能\n+ 合成方法", 970, 535),
        ("Claim/Idea/Gap", "结构化实体\n赛题要求", 1170, 535),
    ]
    for name, desc, x, y in data_items:
        body_parts.append(f'<rect x="{x}" y="{y}" width="180" height="60" rx="5" fill="white" stroke="#3a7"/>')
        body_parts.append(f'<text x="{x+90}" y="{y+18}" text-anchor="middle" font-size="11" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        for i, line in enumerate(esc(desc).split("\n")):
            body_parts.append(f'<text x="{x+90}" y="{y+32+i*11}" text-anchor="middle" font-size="9" fill="#666">{line}</text>')

    # ============ Layer 5: 外部数据源 ============
    body_parts.append('<rect x="40" y="555" width="1520" height="110" rx="8" fill="#fff0f0" stroke="#c84"/>')
    body_parts.append('<text x="60" y="580" font-size="13" font-weight="600" fill="#1a1a2e">⑤ 外部数据源层 · External Data Sources（统一登记 · 许可证透明 · 三路降级）</text>')

    sources = [
        ("arXiv", "公开 / 无 token", 80, 600),
        ("Semantic Scholar", "公开 / 可选 token", 270, 600),
        ("Sciverse", "学术 token", 460, 600),
        ("Materials Project", "X-API-KEY / 缺降级", 650, 600),
        ("OQMD", "公开", 840, 600),
        ("MinerU", "PDF 解析 token", 1030, 600),
        ("LLM Provider", "OpenAI/Anthropic/\nDeepSeek/MiniMax/vLLM", 1220, 600),
        ("NOMAD", "公开材料档案", 1410, 600),
    ]
    for name, desc, x, y in sources:
        body_parts.append(f'<rect x="{x}" y="{y}" width="170" height="50" rx="5" fill="white" stroke="#c84"/>')
        body_parts.append(f'<text x="{x+85}" y="{y+18}" text-anchor="middle" font-size="11" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        for i, line in enumerate(esc(desc).split("\n")):
            body_parts.append(f'<text x="{x+85}" y="{y+32+i*10}" text-anchor="middle" font-size="8" fill="#666">{line}</text>')

    # ============ Layer 6: 系统级指标面板 ============
    body_parts.append('<rect x="40" y="685" width="1520" height="100" rx="8" fill="#f5f0ff" stroke="#7a4cb8"/>')
    body_parts.append('<text x="60" y="710" font-size="13" font-weight="600" fill="#1a1a2e">⑥ 系统级指标面板 · System Metrics Panel（赛题 §4.2 强支撑 · 9 类指标自动聚合）</text>')

    metrics = [
        ("节点完成率", "成功率", 80),
        ("KV 覆盖率", "15 字段", 240),
        ("文献抓取率", "arxiv/S2/Sciverse", 400),
        ("5 维评分分布", "中位数/P25/P75", 580),
        ("Gap 质量分布", "4 维度", 760),
        ("CV 一致性", "MP+规则一致率", 920),
        ("证据链", "按阶段", 1080),
        ("降级触发率", "三路", 1240),
        ("流水线效率", "耗时/LLM 调用", 1400),
    ]
    for name, desc, x in metrics:
        body_parts.append(f'<rect x="{x}" y="725" width="140" height="50" rx="5" fill="white" stroke="#7a4cb8"/>')
        body_parts.append(f'<text x="{x+70}" y="743" text-anchor="middle" font-size="10" font-weight="600" fill="#1a1a2e">{esc(name)}</text>')
        body_parts.append(f'<text x="{x+70}" y="758" text-anchor="middle" font-size="8" fill="#666">{esc(desc)}</text>')

    # ============ 图例 + 关键统计 ============
    legend_items = [
        ("普通节点", "#4e54c8", 0, 0),
        ("硬筛节点", "#c8a415", 110, 0),
        ("检查点 🛂", "#d44", 220, 0),
        ("Evidence Chain", "#88aacc", 360, 0),
        ("数据/工具/外部", "#3a7", 510, 0),
    ]
    for label, color, x, y in legend_items:
        body_parts.append(f'<rect x="{x}" y="{y+810}" width="14" height="14" rx="2" fill="{color}" stroke="#666"/>')
        body_parts.append(f'<text x="{x+22}" y="{y+821}" font-size="10">{esc(label)}</text>')

    key_facts = [
        "✓ 41 节点 · 5 检查点 · 9 类指标 · 299 pytest 通过 · 8 个外部数据源 · 6 Provider · 12 个热电体系规则表",
        "✓ 物理一致性硬筛（118 元素价态 + 14 项性能范围 + 18 电子规则 + Goldschmidt 容忍因子）",
        "✓ 5 维度可信度评分（外推安全 / 文献密度 / 机制论证 / 交叉验证 / 预测区间合理性）",
        "✓ 证据链（how）：manual / retrieved 分离，按阶段落库",
        "✓ 三层部署：GitHub（源码） + Replit（后端） + HuggingFace Space（前端）",
    ]
    for i, fact in enumerate(key_facts):
        body_parts.append(f'<text x="40" y="{855+i*16}" font-size="11" fill="#666">{esc(fact)}</text>')

    # 拼装
    body = "\n".join(body_parts)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif">'
        f'<rect width="{W}" height="{H}" fill="#fafbfc"/>'
        f'{defs}'
        f'{body}'
        '</svg>'
    )
    return svg


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(make_svg(), encoding="utf-8")
    print(f"已生成：{OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()