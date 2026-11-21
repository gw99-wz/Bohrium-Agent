# 最小调用示例（示例，不是正式答案）

本目录给出一组**最小可运行示例**：从一段英文文本到一份完整的双语对照交付物。

> ⚠️ 这里的输入是本仓库自写的示例文本，输出是示意结果（每个 `.md` 顶部都有「示例输出」标注）。
> 它用来展示接口、段号约定与产物格式，**不是任何正式测试案例的答案，也不冒充正式结果**。

## 输入

| 文件 | 作用 |
| --- | --- |
| `case/input/material.md` | 待译文本（5 段：标题、摘要标题、摘要、结果标题、结果段） |
| `case/input/case.json` | 案例描述：`case_id`、源文件、`output_contract` 五个字段 |
| `case/input/task.md` | 任务要求：段落对照、术语统一、保留数字与引用、不确定要标注 |

## 调用

```bash
# 生成示例（会重建 case/work 与 output）
python examples/build_example.py
```

等价的逐步调用：

```bash
python scripts/run.py all      --input examples/case/input --work examples/case/work --output examples/output
python scripts/glossary.py lock --seed-only --input examples/case/input --work examples/case/work --output examples/output
python scripts/packet.py build --input examples/case/input --work examples/case/work --output examples/output
# 把 work/packets/packet_0001.md 翻好写进 work/translation/packet_0001.out.jsonl
python scripts/check.py --merge --input examples/case/input --work examples/case/work --output examples/output
python scripts/render.py        --input examples/case/input --work examples/case/work --output examples/output
```

## 输出（`examples/output/`）

| 文件 | 看什么 |
| --- | --- |
| `bilingual.md` | 段号 · 页码 · 类型 · 原文 · 译文 · 译法依据 —— **格式重点看这个** |
| `translation.zh.md` | 纯中文阅读版 |
| `result.json` | `case_id` + `status` + `output_contract` 五个字段全部填好 + `evidence` 段级定位 |
| `glossary.tsv` | 本次锁定的术语（含首现段号） |
| `verify_report.json` | 六项指标全为 1.0，`errors = 0` |
| `pending_review.md` | 无待确认项时的样子 |

`result.json` 里 `output_contract` 的五个键（`translation` / `bilingual` / `glossary` / `evidence` / `status`）与 `case.json` 声明完全对应——这是评测方检查的第一件事。

## 想换成自己的论文

```bash
mkdir -p /tmp/myc/input && cp your.pdf /tmp/myc/input/material.pdf
# case.json 至少写 case_id 与 output_contract
python scripts/run.py all --input /tmp/myc/input --work /tmp/myc/work --output /tmp/myc/output
```

之后按 `docs/USAGE.md` 第 4 节推进即可。
