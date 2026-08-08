"""自动化验证脚本包（不上传 git，本地使用）。

子脚本清单：
- validate_arch         架构质量（语法/导入/接口/类型）
- validate_pipeline     端到端流水线（5阶段 + discovery）
- validate_outputs      产出质量（Claim/mechanism/报告）
- validate_competition  赛题三·方向三评分项覆盖度
- validate_api          Web API 接口完整性
- run_all               总入口 + 质量报告生成

设计原则：
- 不修改源代码（只读 + dry_run）
- 失败快速暴露（清晰错误信息）
- 产出可读报告（终端 + JSON）
"""