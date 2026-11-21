> **示例输出**：本文件由 `examples/build_example.py` 用 `examples/case/input/material.md`（仓库自写的示例文本）生成，用于说明接口、段号与对齐格式，**不是任何正式案例的答案**。

# 双语对照（段落级）

- 案例：`example-mini`
- 源文件：material.md
- 段数：5　已译：5
- 术语一致率：1.0　数字准确率：1.0　引用准确率：1.0
- 校验结论：通过

> 段号由 `scripts/segment.py` 分配，与 `segments.jsonl` 一一对应；`pN` 为原文页码。


## §Title

**S0001 · p1 · title**

> A minimal fixture for the paper-translate pipeline

一份用于演示最小调用流程的样例文本

*译法依据：标题按陈述句译出*


## §Abstract

**S0002 · p1 · heading**

> Abstract

摘要

*译法依据：Abstract 作“摘要”*

**S0003 · p1 · paragraph**

> We measured the optical response of a two-dimensional crystal with a thickness of 3 layers. The band gap was found to be 1.29 eV, which is 0.6 eV larger than the bulk value [12]. Photoluminescence was not observed at 10 K in samples grown on a silicon substrate (Fig. 2a).

我们测量了厚度为 3 层的二维晶体的光学响应。测得带隙为 1.29 eV，比体材料的数值大 0.6 eV [12]。在硅衬底上生长的样品中，10 K 下未观察到光致发光（图 2a）。

*译法依据：two-dimensional→二维、band gap→带隙、photoluminescence→光致发光，均为锁定译名；Fig. 2a→图 2a*


## §Results

**S0004 · p1 · heading**

> Results

结果

*译法依据：Results 作“结果”*

**S0005 · p1 · paragraph**

> Raman spectroscopy showed a linewidth of 2.4 cm-1 for the monolayer, and the sample was only stable below 350 K. In contrast to earlier work [7, 8], we did not detect any measurable exciton binding energy shift.

拉曼光谱给出的单层线宽为 2.4 cm-1，并且样品仅在 350 K 以下稳定。与早期工作 [7, 8] 相比，我们未检测到任何可测量的激子结合能移动。

*译法依据：only below→仅在……以下；in contrast to→与……相比；did not detect→未检测到*
