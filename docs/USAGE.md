# 使用说明（USAGE）

面向"第一次用这个 Skill 交差"的人：从原文到可复核的双语对照，每一步做什么、看到什么、出问题怎么办。

## 1. 适用与不适用

**适用**：英文（或中英混排）科研文献 → 中文，要求术语统一、段落对齐、数字/引用/公式不错、结果能回指原文。
**不适用**：只要大意摘要；原文不是文献；目标是润色中文原创稿。

## 2. 环境

```bash
python3 --version          # 需要 >= 3.9
pip install -r requirements.txt   # 可选：装 pymupdf 以启用 PDF 首选后端
```

- 核心流程**只用标准库**，`pymupdf` 装不上也能跑（自动回退 `pdftotext`）；
- 没有任何联网调用、不下载模型。

## 3. 目录契约

| 角色 | 评测环境 | 本地模拟 |
| --- | --- | --- |
| 输入 | `/input`（`case.json`、`task.md`、原文） | `--input /path/to/input` |
| 中间态 | `/app/work` | `--work /path/to/work` |
| 交付物 | `/app/output` | `--output /path/to/output` |

默认值就是评测契约，**评测时所有命令都可以不带参数**。

`case.json` 至少要有 `case_id`；`output_contract` 的字段名会被原样回填进 `result.json`，所以不要自己发明字段名。

## 4. 一次完整运行

```bash
S=/skills/weekly-skill                      # SKILL.md 所在目录
python3 $S/scripts/run.py all               # ① 体检 → 抽取 → 分段 → 术语候选
```

停在"术语表待填"是**设计如此**。接着：

```bash
# ② 填 work/glossary.tsv 的 zh 列（不打算锁定的行把 status 改成 skip）
python3 $S/scripts/glossary.py lock --seed-only   # 急用：只锁种子表已给译名的术语
python3 $S/scripts/packet.py build                # ③ 生成翻译包

# ④ 逐包翻译：读 work/packets/packet_XXXX.md，把结果写成 work/translation/packet_XXXX.out.jsonl
python3 $S/scripts/packet.py status               # 检查还剩哪些包

python3 $S/scripts/check.py --merge               # ⑤ 守恒校验（非 0 退出 = 必须改）
python3 $S/scripts/render.py                      # ⑥ 出 result.json 与双语对照
```

也可以一条命令收尾：`python3 $S/scripts/run.py finish`（= `check.py --merge` + `render.py`）。

## 5. 产物解读

| 文件 | 谁看 | 说明 |
| --- | --- | --- |
| `output/result.json` | 评测方/程序 | `case_id`、`status`、`output_contract` 全部字段、证据、不确定性、变更记录 |
| `output/bilingual.md` | 人 | 每段：段号 · 页码 · 类型 · 原文 · 译文 · 译法依据 |
| `output/translation.zh.md` | 人 | 纯中文阅读版 |
| `output/glossary.tsv` | 人/复用者 | 本次实际锁定的术语表（含首现段号） |
| `output/verify_report.json` | 人/程序 | 各指标 + 每一条问题 |
| `output/pending_review.md` | 人 | 需要确认的清单，按段落分组 |
| `output/coverage.json` | 人/程序 | 页级字符对账，证明没漏段 |
| `output/run_manifest.json` | 人 | 输入哈希、版本、产物清单，用于复跑核对 |

`status` 三态：`success`（全译且校验通过）/ `partial`（有缺失或校验未过，必看 `uncertainty`）/ `unable`（无法完成，原因在 `uncertainty`）。

## 6. 术语表维护

列含义：

| 列 | 说明 |
| --- | --- |
| `term_id` | 稳定编号，报告里用它指代术语 |
| `src` | 原文术语（大小写不敏感；含大写字母的缩写按大小写匹配） |
| `zh` | **锁定译名**，全篇只允许这一个 |
| `zh_alt` | 允许的同义写法，`\|` 分隔，例如 `带隙\|能隙` |
| `status` | `locked`（默认）/ `keep`（保留原文，如 `MoS2`）/ `skip`（不校验） |
| `first_seg` / `first_page` | 首现位置，供回指 |
| `context` | 首现上下文，判断译名时的依据 |

改词流程：改 `glossary.tsv` → 重新 `glossary.py lock` → 重新 `packet.py build` → 重译受影响段落 → `check.py --merge`。
**不要**直接改 `glossary.lock.tsv`：它的 sha256 记录在 `glossary.lock.json` 里，改了就与记录不一致。

换学科：把 `resources/glossary_seed.tsv` 换成你领域的词表（`src<TAB>zh<TAB>category<TAB>note`），重跑 `glossary.py suggest`。

## 7. 修复循环

`check.py` 非 0 退出时，`verify_report.json` 的 `issues[]` 会给出 `seg_id + type + detail`：

| type | 含义 | 怎么修 |
| --- | --- | --- |
| `missing` | 该段没译文 | 补 `work/translation/*.out.jsonl` |
| `not_chinese` | 译文不含中文 | 真的翻译，不要照抄英文 |
| `too_short` | 译文短得离谱 | 检查是否只译了半句 |
| `length_ratio` | 译/原字符比 < 0.12 或 > 1.3 | 漏译或扩写 |
| `number_missing` | 数字没保留 | 按 `expected` 逐个补回 |
| `number_added` | 多了原文没有的数字 | 删掉或核对是否误加 |
| `unit_missing` | 单位丢了 | 补单位，注意 `cm-1` 不要写成 `cm` |
| `citation_missing` | `[12]` 之类丢了 | 补回编号组 |
| `figure_missing` | `Fig. 3a` 编号丢了 | 用「图 3a」但编号必须一致 |
| `formula_lost` | 公式符号没了 | 保留符号，不要用文字替代公式 |
| `negation_lost` | 否定/限定没落中文 | 补「未/仅/除…外」等 |
| `glossary` | 该段没用锁定译名 | 改成 `expected_zh` |

修完只需重跑 `check.py --merge`（不必重抽原文）。

## 8. 异常手册

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `preflight` 报 `scanned_pages` | 该页是图片 | `extract.py --render-pages 3,4` 导出 PNG → 用视觉读图转写该页 → 手工补进 `work/segments_manual.jsonl`；写进 `uncertainty` |
| `blocking: pdf_encrypted` | 加密 PDF | 停止，`status=unable` |
| `extract` 报 `no_pdf_extractor` | 无 PyMuPDF 且无 pdftotext | `pip install pymupdf`；不行就用自身的读文件能力产出 `work/blocks.jsonl`（字段：`page/pid/text/size/bold/math/col/bbox`），并从 `segment.py` 继续 |
| `coverage_pass=false` | 有页没抽全 | 看 `pages_below_threshold`，对照 PDF 该页，必要时手工补段 |
| 段落太大（>2000 字） | 该 PDF 段落无额外行距 | `--max-segments` 调小不影响段内切分；确需细切时在 `segments.jsonl` 手工拆段并保持编号连续 |
| 段落太碎（一句话一段） | 行距/宽度信号被误判 | 检查 `work/blocks.jsonl` 的 `bbox` 是否为空（回退后端会没有 bbox） |
| 双栏顺序错 | 有跨栏大图/大表 | 在 `blocks.jsonl` 里把该块 `kind` 改为 `image` 后重跑 `segment.py` |
| 表格内容当正文翻译 | 表格是文本层 | 把该段 `kind` 改成 `table_like`，译文保留表头与数值，逐行对齐 |
| `output_contract` 字段不认识 | 契约里有本 Skill 未覆盖的字段 | `result.json` 里写 `null` + `contract_notes` 说明，不要编造 |
| 输出目录不可写 | 权限 | 换 `--output`，并在交付说明写实际路径 |

## 9. 重复运行与幂等

- 所有中间产物都可重算；`extract.py`/`segment.py` 重跑会覆盖，不会累积；
- `work/translation/*.out.jsonl` 不会被覆盖，可断点续译；
- `check.py --merge` 以 `id` 去重，后写的覆盖先写的；
- 同一输入重跑 `finish` 两次，`result.json` 除 `generated_at` 外应完全一致（自检项之一）。
