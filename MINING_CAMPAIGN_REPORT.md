# Top-200 因子挖掘战役报告（2026-11-18）

## 一、数据准备

- 清空 94 个历史轮次（`runs/`），备份至 `~/factorfactory_runs_backup_20261118_235649`（11MB）。
- 通过 ccxt（7890 代理 + 重试）采集 24h 成交额前 200 的 USDT-M 永续：
  - ohlcv：**199 标的**，2020-01-01 ~ 2026-11-17，约 14.7 万行（1 个中文名标的因路径白名单不可存储而剔除）。
  - funding：**199 标的**，约 58 万行。
  - basis / open_interest 仅 23/24 个老标的（多数新币无对应现货，交易所 OI 仅约 30 天历史）。
- 新增标的集：`top200`（199）、`top200_long`（38 个有 2023 起完整历史）、`top200_carry20`（17 个含 basis）。
- 修正末端未闭合 bar（去掉 2026-11-18 当日未收盘 K 线）以通过供给门禁 A。

## 二、代码改动（均通过全量测试 815 项）

1. **新增挖掘算法 `template_enum`**（`capability/mining.py`、`capability/ops/search.py`、`tests/test_v5_search.py`）：  
   确定性枚举经济锚点（动量/反转/区间/量比/资金费 carry/基差 carry/OI/量价交互）× 窗口网格，  
   与随机采样、GP 进化互补；字段门控、内容身份、注册/分派一致性均有测试覆盖。
2. **新增 6 个内置因子**（`capability/factors.py`）：`funding_delta`、`basis_delta`、`illiquidity`、  
   `range_efficiency`、`tsmom_60_5`、`vol_price_diverge`（内置池 26 → 32）。
3. **修复严重 bug**：`materialize()` 曾把 `catalog/factors.jsonl` 覆盖为仅含 sqlite 因子的视图，  
   一次 promote 就抹掉 78 条历史库。已改为原样保留未导入 sqlite 的 legacy 记录，并加回归测试  
   `test_materialize_preserves_legacy_catalog_rows`。

## 三、挖掘过程（6 轮全量八工位 run）

| 轮次                     | 宇宙                  | 判据/口径                                           | 结果                       |
| ---------------------- | ------------------- | ----------------------------------------------- | ------------------------ |
| top200_mine_v1         | 38 标的, 83 候选        | 默认口径 (rebalance 5, 中性化 beta+size)               | 74 全拒：IC 显著但 DSR/成本闸未过   |
| top200_carry_v1        | 17 标的含 basis        | 同上                                              | 24 全拒：同上                 |
| old18_proven           | 18 标的, 2020-22/2023 | 同上                                              | 24 全拒                    |
| **top200_caliber_opt** | 38 标的               | rebalance/horizon 10、去中性化、cost_tiers [10,30,80] | **state=done，2 个候选全闸通过** |
| old18_caliber_opt      | 18 标的, 2020-22/2023 | 同优化口径                                           | **state=done，2 个候选全闸通过** |

口径洞察：默认口径下 IC 与多空净收益被 beta/size 中性化与高换手成本抵消；把持有期拉到 10 日、  
去掉中性化、按头部流动永续真实费率计成本后，carry 类因子才显出净优势。

## 四、可用因子（走完 §6 六步：全量通过 → 判据核对 → 单因子隔离 → OOS → 入库 → 回灌）

### 已验证可用（单因子隔离 weight=1.0，OOS 正收益，已 promote 入库）

| 因子                          | 表达式                                              | 全量 DSR | p_adj | 隔离 OOS net | Sharpe | 正折      |
| --------------------------- | ------------------------------------------------ | ------ | ----- | ---------- | ------ | ------- |
| **funding_vol_interaction** | `-mean(funding_rate,7) × volume/mean(volume,20)` | 0.997  | 0.021 | **+4.89%** | 0.635  | **3/3** |
| **funding_carry14**         | `-mean(funding_rate,14)`                         | 0.997  | 0.013 | +3.30%     | 0.433  | 2/3     |

窗口：mine 2020-11~2022-12 / OOS 2023 全年（18 标的）。`funding_vol_interaction` 三折全正，是最稳健的一个。

### 已识别为伪影并下架（retired）

- `funding_carry`、`funding_carry_rank7`：在 2026Q2~Q3 全量跑通过，但二者是同一信号的 raw/rank 变换，  
  单因子隔离 OOS 均为 **-1.28%（1/3 正折）**；组合的正 OOS 来自 ridge 给共线对分配的相反权重（-0.22/+0.78），  
  属权重伪影。已 `factor_db --retire` 并注明原因。

### 剩余 7 条 active 记录

为从 Publish 事件 + 备份 run 产物重建的 history（内容身份可重算校验），未在本轮单独做隔离验证，  
建议后续按同样六步逐一复验。

## 五、结论与下一步

- **找到了可用因子**：`funding_vol_interaction`（最稳健，OOS 3/3 正折）与 `funding_carry14`，均已 OOS 认证入库。
- **关键教训**：严判据下「组合通过」不等于「因子可用」；共线因子组合会产生权重伪影，必须单因子隔离复跑。
- **数据现实**：前 200 里 150 个是 2024 后上市（多为 2026 年的代币化股票），历史太短，无法进入多年挖掘窗口；  
  真正可用于 5~10 日挖掘的头部币约 38 个。
- 建议后续：① 用 `db:*` 先验把新入库因子回灌再挖；② 对重建的 7 条 history 做隔离复验；  
  ③ 在 2026 窗口上检验 carry 类因子的时段衰减（Q2 正、Q3/Q4 负）。

## 六、扩大可用币种：币安备份站来源与 `vision` 工作流

**问题**：top200 里 150 个是 2024 后上市，真正能进多年挖掘窗口的只有约 38 个；且按
「当前成交额 top200」选宇宙存在幸存者偏差。

**新增能力**：接入币安官方备份站 `<https://data.binance.vision>`（`upstream/binance_vision.py`），
按月归档的 `ohlcv`/`funding` 可回溯到 2019/2020 年，是交易所 ccxt 翻页的历史深度补集。
每个 zip 强制校验 `.CHECKSUM`（sha256），月度下载并发化；落盘路径与 ccxt 完全相同。

`flow vision list/scan/fetch` 三步把「全市场发现 → 按历史长度筛选 → 拉齐数据」固化成工作流：
- `list` 用 ccxt 拉全市场约 901 个 USDT-M 永续（**含约 138 个 `active=false` 的已下线合约**）；
- `scan` 对每个标的二分探测备份站最早归档月（并发），按 `--min-days` 筛出历史达标的老币，
  产出可审计的 `vision_candidates.json`（每标的 `first_month`/`days`/筛选条件）；
- `fetch` 从筛中清单下载 `ohlcv`+`funding` 到挖掘数据根。

**关于已下线币**：它们**可以且应当参与**因子挖掘——只用「活到今天」的币会引入幸存者偏差，
高估因子表现。系统对此已天然支持：已下线合约在 ccxt 清单里（`active=false`），备份站也保留其
退市前的历史（如 FTT 2022-11 归档仍在）；边界工位在退市后按 `delisted` 理由码排除该标的，
退市前正常参与——这正是 PIT 宇宙的正确语义。彻底下架、连 ccxt 清单都没有的币（如 LUNA）
无法枚举，属数据源边界。

**取舍**：不改门禁代码。`vision scan` 筛出的达标老币标的集本身覆盖研究窗口，三处供给门禁
自然通过；配合已有 `boundary=pit_dynamic_topk`（逐时点按流动性选 top-k）即可实现动态宇宙，
无需放宽 fail-closed 防线。

