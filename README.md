# paper-translate

科研文献翻译 Skill：**把"术语稳、段落全、关系准、可核验"从口号变成能被程序检查的产物。**

翻译由 Agent 完成，切段、编号、术语锁定、数字守恒、对齐渲染、结果落盘由脚本完成；校验不通过时脚本以非零退出码强制返工，而不是在提示词里"要求注意"。

## 它具体防住什么

| 典型失败 | 机制 | 可检查的证据 |
| --- | --- | --- |
| 同一术语在标题/正文/图注里换了三种译法 | 术语表落盘 → sha256 冻结 → 逐段回填校验 | `glossary_check.json` 的 `consistency_rate` |
| 摘要被拆成 10 个碎段、句子被腰斩 | 按行距/短末行/标题标签重建段落，跨栏跨页续接 | `coverage.json` 的 `char_ratio` |
| 长文漏段、重复、错位 | 段号只由脚本分配，页级字符对账 | `coverage.json` 的 `pages_below_threshold` |
| 数字/单位/`[12]`/`Fig. 3a`/公式被改写 | 多重集守恒比对 | `verify_report.json` 的 `number_accuracy` 等 |
| 结果无法回指原文 | 每段带段号+页码+章节+block id | `bilingual.md`、`result.json.evidence` |

## 目录结构

```
SKILL.md                  入口：触发条件、七步流程、硬规则、异常处置、自检清单
scripts/
  common.py               路径/IO/case 装载
  preflight.py            材料体检（文本层、扫描页、契约字段）
  extract.py              版面感知抽取 + 段落重建
  segment.py              段号分配 + 跨页续接 + 字符对账
  glossary.py             术语抽取 / 冻结 / 回填校验
  packet.py               分批翻译包（上下文 + 本批术语）
  check.py                数字、单位、引用、公式、否定、长度守恒
  render.py               双语对照 + result.json（按 output_contract 回填）
  run.py                  一键编排与断点续跑
resources/
  glossary_seed.tsv       可替换的领域术语种子（默认二维材料/光谱学）
  style_zh.md             中文学术译写规范
  schema/result.schema.json
docs/{USAGE,METHOD,LIMITS}.md
examples/                 最小调用示例（输入 + 示意输出，标注"示例"）
tests/                    回归测试（含失败路径断言）
eval/                     与不带 Skill 的基线对照及指标脚本
```

## 快速开始（本地模拟评测契约）

```bash
mkdir -p /tmp/case/input /tmp/case/work /tmp/case/output
cp your.pdf /tmp/case/input/material.pdf
cat > /tmp/case/input/case.json <<'JSON'
{"case_id": "demo-001", "source": "material.pdf",
 "output_contract": {"translation": "全文译文", "bilingual": "段落对照",
                     "glossary": "术语表", "evidence": "段号与页码", "status": "运行状态"}}
JSON
printf '# 任务\n把原文全文翻译成中文，术语统一，保留数字与引用。\n' > /tmp/case/input/task.md

python3 scripts/run.py all --input /tmp/case/input --work /tmp/case/work --output /tmp/case/output
# 按提示填 work/glossary.tsv 的 zh 列，然后
python3 scripts/glossary.py lock --seed-only --input /tmp/case/input --work /tmp/case/work --output /tmp/case/output
python3 scripts/packet.py build --input /tmp/case/input --work /tmp/case/work --output /tmp/case/output
# 逐包翻译 work/packets/packet_*.md → work/translation/<packet>.out.jsonl
python3 scripts/check.py --merge --input /tmp/case/input --work /tmp/case/work --output /tmp/case/output
python3 scripts/render.py --input /tmp/case/input --work /tmp/case/work --output /tmp/case/output
```

评测环境里 `/input`、`/app/work`、`/app/output` 已是默认值，可直接：

```bash
python3 /skills/weekly-skill/scripts/run.py all
```

## 依赖

- Python ≥ 3.9，核心流程仅标准库；
- 可选 `pymupdf`（PDF 首选后端）、`pdftotext`（poppler-utils，回退后端）；
- `pip install -r requirements.txt` 会装 pymupdf，装不上也能跑（降级路径见 `docs/USAGE.md`）。

## 测试

```bash
python tests/test_pipeline.py     # 期望末行：ALL TESTS PASSED
```

覆盖：双栏阅读顺序、正常路径端到端、漏段与改数字必须被抓住、未知 `output_contract` 字段必须写 null 而不是编造。

## 指标

```bash
python eval/metrics.py eval/runs/<case>      # 术语一致率/数字保留率/引用保留率/段落覆盖
```

对照协议与结果见 `docs/METHOD.md`。

## 许可

MIT。术语种子表可自由替换为你的学科；替换后重跑 `glossary.py suggest` 即可复用整套流程。
