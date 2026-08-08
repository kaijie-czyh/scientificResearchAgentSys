# 自动化验证脚本（本地使用，不上传 git）

## 目的
持续验证产品质量，覆盖 **赛题三·方向三（材料方向）评分项**，帮助本地快速发现回归问题。

## 脚本清单

| 脚本 | 验证维度 | 耗时 |
|------|---------|------|
| `validate_arch.py` | 语法/导入/schema/子图/ContextKey | ~1.5s |
| `validate_pipeline.py` | 5 阶段 + discovery 端到端 + resume/blocked | ~10s |
| `validate_outputs.py` | Claim/mechanism/证据链/新颖性/报告 | ~7s |
| `validate_competition.py` | 赛题三·方向三评分项覆盖度（50% + 50%） | ~40s |
| `validate_api.py` | 路由/下载类型/多格式/前端调用 | ~2s |
| `run_all.py` | 总入口 + JSON 综合报告 | 全部运行 |

## 使用方法

```bash
# 全部验证
python tests/run_all.py

# 快速验证（仅架构 + API）
python tests/run_all.py --quick

# 单项验证
python tests/run_all.py --only validate_outputs

# 无颜色输出（适合日志捕获）
python tests/run_all.py --no-color

# 直接运行单项
python tests/validate_arch.py
python tests/validate_pipeline.py
python tests/validate_outputs.py
python tests/validate_competition.py
python tests/validate_api.py
```

## 输出

- **终端**：彩色分级报告（S/A/B/C/D）+ 各维度详细输出
- **JSON 报告**：`tests/last_full_report.json`（综合）+ `tests/last_quality_report.json`（产出质量）

## 评级

| 等级 | 通过率 | 含义 |
|------|--------|------|
| **S** | ≥90% | 冠军候选 |
| **A** | ≥80% | 强竞争力 |
| **B** | ≥70% | 良好 |
| **C** | ≥60% | 合格 |
| **D** | <60%  | 需改进 |

## 设计原则

1. **不修改源代码**（只读 + dry_run）
2. **失败快速暴露**（清晰的错误信息 + traceback）
3. **可重入**（可重复运行，KV 数据会更新）
4. **不影响生产**（使用 PID 作为 project_id，不污染真实项目）

## 添加新检查项

每个脚本是一个独立模块，按模式：

```python
def check_xxx(project_id: str, topic: str = "") -> dict[str, Any]:
    """检查项说明。"""
    print(_section("X. 检查项名"))
    # ... 检查逻辑
    return {"score": 0~100, ...}
```

在 `main()` 的 `checks` 列表中添加。

## 与 DEVLOG 联动

每次重大功能调整后，运行 `python tests/run_all.py` 并记录综合评级到 [DEVLOG.md](../DEVLOG.md)，便于追踪产品质量演进。