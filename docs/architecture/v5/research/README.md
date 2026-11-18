# research · 研究与专题分析

> V5 开发过程中的专题记录。**属历史留痕**：单篇结论可能已被后续轮次覆盖，引用前先对照 `../reviews/summary/` 与当前代码。

| 文件 | 内容 | 与现状的关系 |
|---|---|---|
| `v5_e2e_analysis_20261117.md` | 端到端严格判据复跑：2 pass / 59 reject / 1 watch，组合 3 成员，Certify OOS net_return 16.42%。 | 历史实测；判据与口径此后又修过多处 |
| `v5_mining_industry_gap_20261118.md` | 挖矿算法业界差距调研：可选算法、单因子六步链路。 | 仍作选型参考 |
| `v5_supply_market_20261117.md` | 供给市场维度（spot/futures）改造记录：路径、护栏、门禁。 | 结论已进 `../19-supply.md` |
| `v5_s3_m6_executor_capability_20261117.md` | S3-M6 工位→能力桥接入记录：六通道、对象表、data 落 Ref。 | 已落地，见 `../../../../v5/orchestration/capability_bridge.py` |
| `v5_s7_combine_and_single_commands_20261118.md` | S7 组合域（三合成器/corr_max/Löwdin）与三条单命令接入记录。 | 已落地，见 `v5/capability/ops/combine.py`、`v5/app/cli/replay.py` |

## 相关设计篇目

- 加密适配与组件边界：`../10-crypto-fit.md`
- 挖矿算法接口：`../02-mining-interface.md`、`../20-mining-algorithms.md`
- 供给链：`../19-supply.md`
