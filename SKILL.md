---
name: paper-translate
description: Use when a task asks to translate a scientific paper (PDF, DOCX, Markdown or plain text) into Chinese, to keep terminology identical across title, abstract, body, figure captions and references, to produce paragraph-aligned bilingual output that can be checked back against the source, or to verify an existing translation for numbers, units, formulas, citation markers and negation. It segments the document, freezes a terminology table, batches translation with context, and refuses to render until conservation checks pass.
---

# 文献翻译：可核验的段落级双语对照

这是一个**可执行**的文献翻译流水线，不是一个提示词集合。翻译本身由你（Agent）完成，但**切段、编号、术语锁定、数字守恒、对齐渲染、结果落盘**由脚本完成，并用非零退出码强制你返工。

它解决四个具体失败：

| 失败 | 本 Skill 的做法 | 证据 |
| --- | --- | --- |
| 同一术语前后译法不一致 | 术语表落盘 → 冻结哈希 → 逐段回填校验 | `work/glossary_check.json` 一致率 |
| 长文漏段、重复、跨页错位 | 段号只由脚本分配；页级字符对账 | `work/coverage.json` 的 `char_ratio` |
| 数字/单位/引用/公式被改写 | 多重集守恒比对，不一致即报错 | `work/verify_report.json` |
| 译文无法回指原文 | 每段带 `段号 + 页码 + 章节 + block id` | `output/bilingual.md`、`result.json.evidence` |

## 何时使用

- 输入里有需要翻译的论文全文、明确段落区间、摘要/图表题注；
- 要求术语统一、双语对照、段号可回指、结果可复核；
- 要求把结果写成结构化 `result.json` 交给评测方。

## 何时不要用

- 只要一句话大意或摘要式改写（不需要段级对齐）；
- 原文根本不是文献（合同、说明书）——脚本能跑，但术语种子表不适用；
- 目标是"润色中文原创稿"，没有原文可核验。

## 运行契约

| 项 | 位置 | 说明 |
| --- | --- | --- |
| 入参 | `<INPUT>/case.json`、`<INPUT>/task.md` | 材料路径、`case_id`、`output_contract` 都在 case.json 里，**必须运行时读取，不得假设** |
| 原文 | `<INPUT>/` 下的 pdf/docx/md/txt | 以 case.json 提到的文件优先，否则取最大的 PDF |
| 中间态 | `<WORK>`（默认 `/app/work`） | 可重跑、可断点续跑 |
| 交付物 | `<OUTPUT>`（默认 `/app/output`） | `result.json` + 双语对照 + 术语表 + 校验报告 |

默认目录已按评测契约设好，一般不需要传参；本地模拟时用 `--input/--work/--output` 覆盖。

```bash
S=/skills/weekly-skill                        # 本 SKILL.md 所在目录（本地运行时换成实际路径）
python3 "$S/scripts/run.py" all                # 只有 python 命令时换成 python
```

## 快速路径（想少动手时）

```bash
python3 "$S/scripts/run.py" all                       # 抽段 + 术语候选（会停在术语表）
python3 "$S/scripts/glossary.py" lock --seed-only      # 只锁种子表已给译名的术语（约 8–15 条）
python3 "$S/scripts/packet.py" build                   # 出翻译包
# ……逐包翻译，写入 work/translation/<packet>.out.jsonl……
python3 "$S/scripts/run.py" finish                     # 校验 + 出 result.json
```

时间允许时不要用 `--seed-only`：打开 `work/glossary.tsv`，把**出现次数最多的 20–40 条**术语填上 `zh` 再 `lock`，术语一致性分数会明显更稳。

## 七步流程

每一步都只做一件事，失败会返回 `{"ok": false, "error": ...}` 并以非零码退出——照 `error` 字段修，不要跳过。

### 1. preflight：先看清材料

```bash
python3 "$S/scripts/preflight.py"
```

产出 `work/preflight.json`：页数、每页字符数、扫描页、是否加密、语言方向、`output_contract` 字段名。
**判断点**：`status` 为 `blocked` 时不要继续，按 `blocking` 字段在 `result.json` 里如实写 `unable`。

### 2. extract：版面感知抽取

```bash
python3 "$S/scripts/extract.py"
```

按行重建段落：行距比 > 1.35 断段、短末行断段、标题标签断段；双栏按"宽块切带 + 带内分栏"排序；图片/公式/题注/参考文献分区。
**判断点**：`kinds` 里 `caption`、`reference` 是否为 0。为 0 说明版式没识别出来，先看 `work/blocks.jsonl` 再决定是否人工修。

### 3. segment：段号与对账

```bash
python3 "$S/scripts/segment.py"
```

分配 `S0001…`，剔除页眉页码，把跨栏/跨页断开的段落接回去，输出 `work/coverage.json` 的页级字符对账。
**判断点**：`coverage_pass` 必须为 `true`；`pages_below_threshold` 非空说明有页漏抽。

某页确实抽不出来时，可以自己补 `work/segments_manual.jsonl`（每行一条）：

```json
{"page": 4, "text": "该页正文……", "kind": "paragraph", "section": "Results"}
```

`segment.py` 会把它追加成正式段落（`source=manual`），照样编号、照样计入字符对账、照样参与后续校验。

### 4. glossary：术语表（本 Skill 的核心）

```bash
python3 "$S/scripts/glossary.py" suggest     # 抽候选 + 用种子表预填
# 打开 work/glossary.tsv，填 zh 列；不打算锁定的行把 status 改成 skip
python3 "$S/scripts/glossary.py" lock         # 冻结并记录 sha256
```

TSV 列：`term_id / src / zh / zh_alt / category / count / first_seg / first_page / status / note / context`。
- `zh`：锁定译名，全篇只允许这一个（同义写法填 `zh_alt`，用 `|` 分隔）；
- `status`：`locked`（默认）/ `keep`（保留原文，如 `MoS2`）/ `skip`（管不了，不校验）；
- 想快速试跑可用 `lock --seed-only`：只锁种子表已给出译名的术语，其余跳过。

**判断点**：锁定后任何对 `glossary.tsv` 的改动都会与 `glossary.lock.json` 的 sha256 不符，必须重新 `lock`。

### 5. packet：分批翻译包

```bash
python3 "$S/scripts/packet.py" build
```

按语义边界打包（默认 4500 字符 / 12 段，不切断段落），每个包带：本批命中的锁定术语、上一段结尾、下一段开头、章节与页码。
逐包读 `work/packets/packet_XXXX.md` 翻译，**结果写到** `work/translation/packet_XXXX.out.jsonl`，每行：

```json
{"id": "S0007", "zh": "……译文……", "basis": "可选：译法依据", "flags": []}
```

```bash
python3 "$S/scripts/packet.py" status   # 看还剩哪些包
```

任务只要求某一段落区间（例如"只译摘要与第 2 节"）时：

```bash
python3 "$S/scripts/packet.py" build --only S0001-S0012,S0020
```

范围会写进 `work/scope.json`，`check.py` 与 `render.py` 只对该范围负责，`result.json` 的 `evidence` 也只覆盖这些段——**不要**用"全文已译"的方式掩盖没译的部分。

### 6. check：守恒校验（必须过）

```bash
python3 "$S/scripts/check.py" --merge
```

校验项：段落完整性、术语回填、数字多重集、单位、`[n]` 引用、Fig./Table/Eq. 编号、公式符号、否定/限定词、长度异常。
退出码非 0 = 有 error 项，**必须改译文后重跑**，不能带着 error 交付。
`work/pending_review.md` 是给人看的确认清单。

### 7. render：出交付物

```bash
python3 "$S/scripts/render.py"
```

产出 `output/`：

| 文件 | 内容 |
| --- | --- |
| `result.json` | `case_id` + `status` + `output_contract` 全部字段 + 证据 + 不确定性 |
| `bilingual.md` | 段号 · 页码 · 章节 · 原文 · 译文 · 译法依据 |
| `translation.zh.md` | 纯中文阅读版 |
| `glossary.tsv` | 本次实际锁定的术语表 |
| `verify_report.json` / `pending_review.md` | 机器校验结果 / 人工确认清单 |
| `run_manifest.json` | 输入哈希、版本、产物清单（可复跑） |

## 翻译时的硬规则

1. **段号只来自脚本**：不改号、不合并、不新增；一段一行，段内不换行。
2. **锁定术语原样使用**：`zh` 列写什么就用什么，不要因为语感换词；确需同义写法，写进 `zh_alt` 后再 `lock`。
3. **数字、单位、公式、编号原样保留**：`1.29 eV`、`cm-1`、`[12]`、`Fig. 3a`、`Eq. (2)`、`N = 1, 2, ... 6` 一个都不许改；中文语境可用「图 3a」，但编号必须一致。
4. **否定与限定不许丢**：`not observed`、`only below`、`in contrast to` 要落到中文的「未观察到 / 仅在…以下 / 与…相反」。
5. **指代要落到实处**：`this`、`the latter`、`the former` 依据给定的上文还原，不许写成"这/该"含糊带过。
6. **译不准备注，不许含混**：拿不准就照译，并在 `flags` 里加 `["uncertain"]`、`basis` 写明理由；`check.py` 会把它汇总进 `pending_review.md`。
7. **不扩写、不缩写、不解释**：不添加原文没有的内容，也不省略从句。
8. **参考文献段**：照录编号与作者-年份，不逐条翻译标题（除任务明确要求）。

## 异常处置

| 情况 | 识别方式 | 处置 |
| --- | --- | --- |
| 扫描页/无文字层 | `preflight` 的 `scanned_pages` | `extract.py --render-pages 3,4` 导出 PNG，用视觉读图转写该页，追加到 `work/segments_manual.jsonl`；无法转写则在 `result.json.uncertainty` 写明具体页码，`status` 记 `partial` |
| PDF 加密 | `preflight.blocking` 含 `pdf_encrypted` | 停止，`status=unable`，原因写清楚 |
| 无 PyMuPDF 且无 pdftotext | `extract` 返回 `no_pdf_extractor` | 先试 `pip install pymupdf`；装不上就用你自己的读文件能力取出文本，写成 `work/blocks.jsonl`（字段见 `docs/METHOD.md`）后从第 3 步继续 |
| 公式/表格是图片 | `kinds.formula == 0` 但题注提到公式 | 保留题注与编号，译文里写「（公式为图片，未转写）」并在 uncertainty 记录 |
| 参考文献解析失败 | `kinds.reference == 0` | 把参考文献区当单段保留，标注"未逐条结构化" |
| 中英混排 | `preflight.language.cjk_ratio` 介于 0.2–0.8 | 只译英文段，中文原样保留（`status=keep`），不要回译 |
| 超长文献 | 包数 > 15 | 逐包推进，中断后用 `packet.py status` 续跑；不要一次把所有原文塞进上下文 |
| `output_contract` 有未知字段 | `preflight.contract.fields` | 写 `null` 并在 `contract_notes` / `uncertainty` 说明，**绝不编造** |
| 输出目录不可写 | `render` 报错 | 换 `--output` 到可写目录，并在交付说明里写明实际路径 |

## 提交前自检

- [ ] `run.py status` 显示 `packets.missing` 为空，`verdict` 为 `true`
- [ ] `output/result.json` 有 `case_id`，`output_contract` 中每个字段都存在（不可填的是 `null` 且有说明）
- [ ] `bilingual.md` 的段号连续、无重号、无跳号
- [ ] `verify_report.json` 里 `errors == 0`；`pending_review.md` 每条都有对应处置
- [ ] 没有编造的页码、文献、实验数据；未译处显式写【未译】
- [ ] 重跑一遍 `run.py finish` 结果不变（幂等）

## 依赖与版本

- Python ≥ 3.9，**核心流程只用标准库**；
- 可选：`pymupdf`（PDF 抽取首选）、`pdftotext`（poppler-utils，PyMuPDF 缺失时回退）；
- 无第三方网络依赖，不下载任何模型。
- 版本：1.0.0；详细说明见 `docs/USAGE.md`、`docs/METHOD.md`、`docs/LIMITS.md`，最小调用示例见 `examples/`。
