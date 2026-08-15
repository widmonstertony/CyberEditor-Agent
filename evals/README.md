# Editorial benchmark / 人工参考剪辑评测

This directory defines a small, repeatable benchmark for comparing an AI edit
with a timeline approved by a human editor. It runs without loading a model,
media decoder, or DaVinci Resolve.

本目录提供一个小而可重复的 benchmark，用人工剪辑师认可的时间线评估 AI
时间线。评测只读取 JSON，不会启动模型、解码视频或连接 DaVinci Resolve。

## What it measures / 衡量内容

- **Source-selection precision, recall, and F1** use the set of unique
  `source_id` values selected by each edit. When `source_id` is absent, the
  evaluator falls back to `asset_id` or the basename of a media-path field.
- **Boundary MAE** pairs clips from the same source one-to-one, then averages
  the absolute in-point and out-point errors in seconds.
- **Order consistency** is the share of matched clip pairs that appear in the
  same relative order as the human edit (1.0 is perfect; 0.0 is fully inverted).
- **Duplicate-shot ratio** flags later same-source selections whose source-time
  overlap (IoU) reaches 0.8 by default. **Adjacent-same-source ratio** reports
  how often neighboring timeline clips come from the same source.
- **Target-duration deviation** compares the AI clip-duration sum with
  `target_duration_sec`; if omitted, the reference clip-duration sum is used.

- **素材选择 precision / recall / F1** 按唯一 `source_id` 集合计算。缺少
  `source_id` 时，依次回退到 `asset_id` 或媒体路径的文件名。
- **边界 MAE** 将同源片段一对一匹配，计算入点和出点的平均绝对秒数误差。
- **顺序一致性** 统计匹配片段之间的相对顺序有多少与人工版相同；1.0 为完全一致。
- **重复镜头比例** 默认将同源且源时间 IoU 不低于 0.8 的后续片段视为重复；
  **相邻同源比例** 衡量相邻时间线片段连续使用同一素材的频率。
- **目标时长偏差** 优先对比 `target_duration_sec`；未填写时使用人工片段时长之和。

These metrics are regression signals, not an automatic claim of “human-level”
editing. Story clarity, emotional intent, music appropriateness, color, and
performance choice still need a blinded human review rubric.

这些指标用于发现版本回退，不能自动证明“达到人类剪辑水平”。故事清晰度、情绪
意图、配乐适配、调色和表演选择仍需由盲测人工评分。

## Files / 文件

- `human_reference.schema.json`: JSON Schema for reference timelines.
- `human_reference.example.json`: synthetic example only; it contains no user media.
- `src/editorial_eval.py`: standard-library evaluator and CLI.

## Create a benchmark / 创建 benchmark

1. Freeze one legally usable test footage set and its stable source IDs.
2. Ask a human editor to make an approved cut without seeing the AI output.
3. Export the human decisions using this schema. Prefer stable `source_id`
   values from the project manifest instead of machine-specific absolute paths.
4. Run every candidate pipeline version on exactly the same extraction data,
   brief, model settings, and seed, then keep each JSON report with its commit.

1. 固定一套可合法使用的测试素材以及稳定的素材 ID。
2. 请人工剪辑师在看不到 AI 成片的情况下完成认可版本。
3. 按本 schema 导出人工决策；优先使用项目清单中的稳定 `source_id`，不要依赖
   某台电脑的绝对路径。
4. 对每个候选版本使用完全相同的提取结果、创作要求、模型配置和随机种子，并将
   报告与对应 commit 一起保存。

Run from the repository root / 在仓库根目录运行：

```powershell
.venv\Scripts\python.exe -m src.editorial_eval `
  --ai data\ui-run\timeline_cuts.json `
  --reference evals\human_reference.example.json `
  --output data\ui-run\editorial_eval.json
```

For a stricter repeated-shot definition, change
`--duplicate-iou-threshold` (range 0–1). Invalid or missing fields produce a
bilingual actionable error and process exit code `2`.

若需调整重复镜头判定，可设置 `--duplicate-iou-threshold`（范围 0–1）。字段
缺失或无效时，CLI 会输出中英双语可操作错误，并以退出码 `2` 结束。
