# Contributing / 贡献指南

感谢改进 CyberEditor-Agent。

## 基本规则

1. 不得把真实媒体、台词、模型权重、令牌或 Resolve 工程提交到仓库。
2. 所有重型 AI 功能必须留在独立子进程中；禁止在 `main.py` 导入 GPU 库。
3. 新增或修改核心函数时，请保留中英双语 Docstring。
4. 错误必须可操作：说明缺少什么、用户下一步应做什么。
5. 新依赖必须说明必要性，优先使用标准库。

## 本地检查

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py src tests
```

## Pull Request

- 用简洁标题说明行为变化。
- 描述硬件/Windows/Resolve/Ollama 测试环境。
- 涉及显存策略时，说明如何验证阶段没有并发。
- 不要用仅对单机成立的绝对路径。

---

Thank you for contributing. Keep GPU-heavy work inside isolated child
processes, add bilingual docstrings to core functions, include actionable error
messages, and run the standard-library test suite before opening a PR.
