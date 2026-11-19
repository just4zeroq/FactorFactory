"""内置因子池：先验因子的唯一定义处（18-plan-schema.md `search.prior_pool`）。

计划里的 `prior_pool` 只写名字（`schema type=ref`：只接受引用标识，不接受内联
表达式）。名字到 AST 的解析必须发生在**编排侧**，并且只有这一处：

- 能力层（`ops/search.py`）只消费 `market_view.prior_candidates`，不读盘、不内联
  —— 否则「口径只有一个来源」在搜索工位就破了；
- 计划层不持有表达式 —— 否则同一因子在两个计划里能写成两份不同的内容身份。

三条校验在加载时全做，任何一条不满足就整体失败（`E1015`），不做「跳过这一条」：

1. 每个 `op` 必须在 `registry/operators.json` 里，且 `arity` 与 `args` 长度一致
   （带参算子的末位必须是 `{"const": int}`，与 `normalise_ast` 同一规则）；
2. 每个 `field` 必须在 `registry/fields.json` 里；
3. 窗口/滞后必须落在该算子的 `param_domain` 内 —— 越界会让 execute 阶段才报
   `E1003`，而那时已经烧掉了前三个工位的时间。

**为什么要有这张表**：没有先验池时，一批 GP 候选全被裁决拒绝，无法区分
「数据没有信号」和「搜索没找到」——两者在产物里长得一模一样。先验因子是
唯一的锚点：`mom60` 在 ACCEPTANCE 里就是已知答案（年化 SR≈0.675）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from capability.mining import normalise_ast
from capability.registry import default_registry, param_error
from contracts.errors import E1015, FlowError


def _op(name: str, *args: Any) -> dict[str, Any]:
    return {"op": name, "args": list(args)}


def _field(name: str) -> dict[str, Any]:
    return {"field": name}


def _const(value: int | float) -> dict[str, Any]:
    return {"const": value}


def _ret(name: str, window: int) -> dict[str, Any]:
    """`price / delay(price, window) - 1`，即 window 根 bar 的简单收益率。"""
    d = _op("ref", _field(name), _const(window))
    return _op("div", _op("sub", _field(name), d), d)


def _zscore(field: str, short: int, long: int) -> dict[str, Any]:
    """短期均值相对**自身长期分布**的标准分。

    `(mean(x,short) - mean(x,long)) / std(x,long)`。用自身历史而非截面做分母，
    是因为加密币的资金费中枢差异极大：BTC 常态 0.01%/8h、小市值币可长期
    0.05% 以上，同一个绝对水平在两者身上不是同一件事。
    """
    return _op(
        "div",
        _op("sub", _op("mean", _field(field), _const(short)),
            _op("mean", _field(field), _const(long))),
        _op("std", _field(field), _const(long)),
    )


# 名字 → 表达式。键是**唯一身份**：同一名字在库存里不允许有两种写法。
# 全部只使用已注册的 9 个字段与 34 个算子，不引入新原语。
BUILTIN_FACTORS: dict[str, dict[str, Any]] = {
    # --- 价格动量 / 反转 -------------------------------------------------
    "mom5": _ret("close", 5),
    "mom20": _ret("close", 20),
    "mom60": _ret("close", 60),
    # 反转＝动量的相反数（负号在值位置，不占窗口参数位）
    "rev5": _op("mul", _const(-1), _ret("close", 5)),
    "rev1": _op("mul", _const(-1), _ret("close", 1)),
    # --- 波动与形态 -------------------------------------------------------
    # 20 根日收益的时序标准差；收益用 delay(close,1) 构造，不引入 pct_change
    "rv20": _op("std", _ret("close", 1), _const(20)),
    # 日内振幅占收盘价
    "hl_range": _op("div", _op("sub", _field("high"), _field("low")), _field("close")),
    # 收盘价相对 VWAP 的偏离
    "vwap_gap": _op("div", _op("sub", _field("close"), _field("vwap")), _field("vwap")),
    # --- 量能 -------------------------------------------------------------
    "vol_ratio": _op("div", _field("volume"),
                     _op("mean", _field("volume"), _const(20))),
    # --- 衍生品截面（只在 futures 数据集可用）-----------------------------
    # 资金费是持有成本：多头在正资金费下净收益为负，故取相反数
    "funding_carry": _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7))),
    # 更长窗口的资金费carry：7 个 funding 事件约 2.3 天，2026Q2 的短窗口 IC
    # 衰减到 0；14/21 个事件覆盖 5~7 天，与 5 日前瞻收益的持有期更匹配，
    # 且在 2024H2/2025H1/2025H2/2026Q1/Q2/Q3Q4 分段 IC 上保持同号。
    "funding_carry14": _op("mul", _const(-1),
                           _op("mean", _field("funding_rate"), _const(14))),
    "funding_carry21": _op("mul", _const(-1),
                           _op("mean", _field("funding_rate"), _const(21))),
    # 低波动异象：截面 IC 为负说明高波动标的后续收益更差；取负号作为因子。
    # 它不依赖资金费数据，可作为独立的经济学锚点，也是 funding 失效时的对照。
    "low_vol20": _op("mul", _const(-1),
                     _op("std", _ret("close", 1), _const(20))),
    "low_vol60": _op("mul", _const(-1),
                     _op("std", _ret("close", 1), _const(60))),
    # 资金费carry + 低波动：两项各自截面标准化后等权，降低单一信号失效风险。
    "funding_lowvol14": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("mul", _const(-1),
                         _op("std", _ret("close", 1), _const(20)))),
    ),
    # 资金费的截面秩版本：抑制极端 funding 事件对分位组合的影响。
    "funding_carry_rank7": _op(
        "rank",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(7))),
    ),
    # 组合锚点：7 事件资金费carry 是主收益来源；量能比在 2026Q2/Q3Q4 的截面 IC
    # 为正，基差水平在 2026Q1/Q2 为正。两项各自截面标准化后与资金费等权，
    # 用不同经济学来源对冲单一 carry 的时段失效。
    "funding_volratio": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    # 长窗口版本：Q3/Q4 原始截面 IC 上 14/21 事件资金费强于 7 事件。
    "funding_volratio14": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    "funding_volratio21": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(21)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    # 交互项：负资金费只有在成交活跃时才更容易被纠正。原始截面 IC 在
    # 2024H2/2025H1/2025H2/2026Q1/Q2/Q3Q4 六段全部同号，优于加法组合。
    "funding_vol_interaction": _op(
        "mul",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(7))),
        _op("div", _field("volume"), _op("mean", _field("volume"), _const(20))),
    ),
    "funding14_vol_interaction": _op(
        "mul",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(14))),
        _op("div", _field("volume"), _op("mean", _field("volume"), _const(20))),
    ),
    "funding14_vol_interaction_scaled": _op(
        "mul",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    "funding_basis7": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("scale", _op("mean", _field("basis"), _const(7))),
    ),
    "funding_volratio_basis": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("add",
            _op("scale", _op("div", _field("volume"),
                             _op("mean", _field("volume"), _const(20)))),
            _op("scale", _op("mean", _field("basis"), _const(7)))),
    ),
    # 持仓量变化
    "oi_change20": _ret("open_interest", 20),
    # 基差（perp 相对现货溢价）的近期均值
    "basis_carry": _op("mean", _field("basis"), _const(7)),
    # --- S8 新增锚点（top200 挖掘）-------------------------------------
    # carry 的变化率比水平更贴近 5 日前瞻：基差/资金费的收敛速度反映套利压力。
    "funding_delta": _op("delta", _op("mean", _field("funding_rate"), _const(14)), _const(3)),
    "basis_delta": _op("delta", _op("mean", _field("basis"), _const(14)), _const(3)),
    # Amihud 非流动性：|收益|/成交额，低流动性溢价；取负号作因子。
    "illiquidity": _op(
        "mul", _const(-1),
        _op("div",
            _op("abs", _ret("close", 1)),
            _op("mean", _field("volume"), _const(20))),
    ),
    # 区间效率：(high-low)/close 与已实现波动的比值——信息含量高的波动更易延续。
    "range_efficiency": _op(
        "div",
        _op("div", _op("sub", _field("high"), _field("low")), _field("close")),
        _op("std", _ret("close", 1), _const(20)),
    ),
    # Kaufman 效率比率：|10日净位移| / 10日路径总长。取值 [0,1]，越高说明
    # 价格沿直线推进（趋势纯净），越低说明来回震荡。纯 OHLCV、跨标的池稳健：
    # 2020-2026 逐年截面 IC 均值 0.06 / IR 1.28 / 正率 86%（old18 与 38 标的池同号）。
    "ts_efficiency10": _op(
        "div",
        _op("abs", _op("delta", _field("close"), _const(10))),
        _op("sum", _op("abs", _ret("close", 1)), _const(10)),
    ),
    # 时序动量（跳过最近一周，12-1 经典构造），规避短期反转污染。
    "tsmom_60_5": _op(
        "div",
        _op("sub", _op("ref", _field("close"), _const(5)),
            _op("ref", _field("close"), _const(60))),
        _op("ref", _field("close"), _const(60)),
    ),
    # 量价背离：上涨缩量（价涨量缩）常预示反转；取 volume 与 ret 的负相关。
    "vol_price_diverge": _op(
        "mul", _const(-1),
        _op("corr", _field("volume"), _ret("close", 1), _const(20)),
    ),
    # --- 加密原生（v2 算子扩表后，逐个有实测）--------------------------
    # 口径：top200_long 38 标的、2020-01~2026-11 日线、fwd5 原始收益、未中性化，
    # 走生产求值路径（`capability.ops.evaluate` → qweave），逐年截面 rank IC。
    # 只收「IC 同号年份 >= 5/7 且与库内因子 |rho| <= 0.6」的；其余候选的实测
    # 与淘汰理由记在 `docs/23-operator-and-factor-expansion.md`，不要重测。
    #
    # Parkinson 波动率：sigma^2 = mean(ln(H/L)^2)/(4ln2)，4ln2 = 2.772588722239781。
    # 用日内高低价估计，比收盘对收盘少一半采样噪声；加密 7x24 无隔夜跳空，
    # 高低价几乎覆盖全天，估计量比股票上更贴合。取负号 = 低波动溢价。
    # 实测 IC +0.0887 / IR 0.283 / 7 年全正（min +0.055），是本库最强信号；
    # 与 rv20 的 rho=0.89——同一条低波动异象，换的是测量方式而不是缩放，
    # 故与 low_vol20 并存（先验池按需取一个，不必同时进）。
    "low_parkinson20": _op(
        "neg",
        _op("power",
            _op("div",
                _op("mean",
                    _op("power", _op("log", _op("div", _field("high"), _field("low"))),
                        _const(2.0)),
                    _const(20)),
                _const(2.772588722239781)),
            _const(0.5)),
    ),
    # 距 60 日最高价的接近度 ∈(0,1]：实测 IC +0.0414 / 7 年全正（min +0.001）。
    # 与 mom20 的 rho=0.51——动量问「涨了多少」，它问「离前高多远」，前高会随
    # 新高上移，所以它不会被 60 天前的单一基准价钉住。
    "near_high60": _op("div", _field("close"), _op("max", _field("high"), _const(60))),
    # 资金费的自身历史标准分：绝对水平跨币不可比（中枢可差 5 倍），异常度可比。
    # 实测 IC +0.0131 / 5 年正；与 funding_carry_rank7 的 rho=0.48，是新增信息
    # 而不是改名。资金费是多头持有成本，取负号与 funding_carry 同向。
    "funding_zscore7": _op("neg", _zscore("funding_rate", 7, 60)),
    # 20 日趋势的线性解释度 R^2 ∈[0,1]。实测 IC 只有 +0.0098（6/7 正），但
    # 与库内**所有**因子的 |rho| <= 0.12——它不是又一个动量，而是一个正交维度：
    # 位移一样大时，「走直线」比「来回震荡」更可延续。低 IC 高正交的因子在
    # 组合里的边际价值高于高 IC 高相关的因子。
    "trend_r2_20": _op("rsquare", _field("close"), _const(20)),

    # --- 从 qweave 三个股票因子库迁入（v3，逐个复算并去重）----------------
    # 口径与上一节相同，但多两道闸：
    #   (a) 必须能用现有 34 个算子表达，不为一个因子新增原语；
    #   (b) 与库内因子及彼此的 |rho| <= 0.6，同族只留 |IC| 最大的代表。
    # 450 个 → 字段可算 426 → 强信号（|IC|>=0.03、同号>=5/7）68 →
    # 算子可表达 45 → 与库内 rho<=0.6 共 21 → 族内去重 9。
    # 名字右边是 qweave 原名；IC 是 fwd5 截面 rank IC 的 2020~2026 均值。
    # FF 的 AST 与 qweave 表达式在同一面板上复算，IC 完全一致（见 docs/24 §5）。
    #
    # CORD10 取负：相关性本身是负 IC，入库后让「分数高 = 后续收益高」。
    # 收益涨、成交量同步放大时，后续收益偏弱——量价配合过度 = 拥挤。
    # IC +0.0547 / 7 年全正；与库内 max rho 0.48（vol_price_diverge）。
    "vol_ret_corr10": _op(
        "neg",
        _op("corr", _ret("close", 1),
            _op("log", _op("add",
                _op("div", _field("volume"), _op("ref", _field("volume"), _const(1))),
                _const(1.0))),
            _const(10)),
    ),
    # CORR10 取负：价格水平与成交量水平的相关，问的是「贵且放量」。
    # 与上一因子的区别是一个看变化率、一个看绝对水平（彼此 rho 0.58）。
    # IC +0.0531 / 7 年全正；库内 max rho 0.38。
    "vol_level_corr10": _op(
        "neg",
        _op("corr", _field("close"),
            _op("log", _op("add", _field("volume"), _const(1.0))), _const(10)),
    ),
    # 距 10 日低点的接近度 ∈(0,1]，越接近 1 表示离近期低点越远（已反弹）。
    # MIN5/10/20/30 是同一族（彼此 rho>=0.88），取与库内重叠最小的 10 日：
    # IC +0.0520 / 7 年全正；库内 max rho 0.56（rev5），30 日版本是 0.60。
    "near_low10": _op("div", _op("min", _field("low"), _const(10)), _field("close")),
    # 5 日收盘波动率取负。与 20 日版本不同，它量的是**即时**风险；
    # 与 hl_range 的 rho=0.60 恰好在闸值上，是入选因子里最边缘的一个。
    # IC +0.0515 / 7 年全正（7 年最小 +0.028）。
    "short_std5": _op(
        "neg", _op("div", _op("std", _field("close"), _const(5)), _field("close")),
    ),
    # 5 日最高价 / 收盘价取负 = 离短期高点越近分数越高。与 near_high60 是
    # 不同时间尺度（rho 0.51 与库内），MAX5/10 同族 0.79，只留 5 日。
    # IC +0.0494 / 7 年全正。
    "near_high5": _op(
        "neg", _op("div", _op("max", _field("high"), _const(5)), _field("close")),
    ),
    # gtja_alpha091：距 5 日高点的距离（截面 rank）× 量低相关的截面 rank，取负。
    # 两个 rank 都是截面算子，与纯时序因子天然正交。
    # IC +0.0463 / 7 年全正；库内 max rho 0.45（ts_efficiency10）。
    "vol_close5_gap": _op(
        "mul", _const(-1.0),
        _op("mul",
            _op("rank", _op("sub", _field("close"), _op("max", _field("close"), _const(5)))),
            _op("rank", _op("corr", _op("mean", _field("volume"), _const(40)),
                            _field("low"), _const(5)))),
    ),
    # gtja_alpha001：成交量变化的截面 rank 与日内收益截面 rank 的时序相关，取负。
    # 与上一因子不同：这里两个输入先各自截面 rank，相关再沿时间算。
    # IC +0.0395 / 7 年全正（最弱的一年仅 +0.0002，稳定性一般）；库内 rho 0.26。
    "vol_chg_ret_corr6": _op(
        "mul", _const(-1.0),
        _op("corr",
            _op("rank", _op("delta", _op("log", _field("volume")), _const(1))),
            _op("rank", _op("div", _op("sub", _field("close"), _field("open")),
                            _field("open"))),
            _const(6)),
    ),
    # alpha26 / gtja_alpha005 是同一个表达式（后者取负），只留一条：
    # volume 与 high 的时序 rank 相关，再取 3 日最大，取负。
    # IC +0.0355 / 7 年全正；库内 rho 0.23，是新信息但信号偏弱。
    "vol_high_rank_corr3": _op(
        "mul", _const(-1.0),
        _op("max",
            _op("corr", _op("ts_rank", _field("volume"), _const(5)),
                _op("ts_rank", _field("high"), _const(5)), _const(5)),
            _const(3)),
    ),
    # alpha55 / gtja_alpha176 互为相反数，留正 IC 的一向：
    # 价格在 12 日高低区间中的位置（截面 rank）与成交量 rank 的 6 日相关，取负。
    # IC +0.0321 / 7 年全正；库内 rho 0.19，与其余 8 个也都 <= 0.28。
    "vol_price_pos_corr6": _op(
        "mul", _const(-1.0),
        _op("corr",
            _op("rank", _op("div",
                _op("sub", _field("close"), _op("min", _field("low"), _const(12))),
                _op("sub", _op("max", _field("high"), _const(12)),
                    _op("min", _field("low"), _const(12))))),
            _op("rank", _field("volume")), _const(6)),
    ),

    # --- 加密原生补族（v4：按语义族选，不拿 IC 当准入）--------------------
    # 前面几批是「有实测才收」，这一批改口径：只要求两件事——
    #   (1) 在加密市场有清楚的经济含义（7x24、厚尾、资金费/持仓量为原生变量）；
    #   (2) 属于现有因子池没有的族，或首次启用此前闲置的算子。
    # IC 只作描述，不作门槛；是否可交易仍由 verdict 的 FDR/DSR/成本闸裁决。
    #
    # 事件时间族：ts_argmax 返回窗口内最大值的**起点索引**（0 基），
    # 故 59-idx 才是「距前高几天」。加密没有收盘集合竞价，前高/前低的时间
    # 距离比「离前多远」多一层信息：久盘不破前高常是趋势衰竭。
    "days_since_high60": _op("sub", _const(59), _op("ts_argmax", _field("high"), _const(60))),
    "days_since_low60": _op("sub", _const(59), _op("ts_argmin", _field("low"), _const(60))),
    # 高阶矩族：加密收益厚尾且左右不对称（上涨彩票、下跌踩踏），
    # 波动率只量二阶矩，偏度/峰度量的是尾部形状。MAD 则是不受极端值影响的
    # 稳健离散度，与 std 的口径不同。
    "ret_skew20": _op("ts_skew", _ret("close", 1), _const(20)),
    "ret_kurt20": _op("ts_kurt", _ret("close", 1), _const(20)),
    "ret_mad20": _op("ts_mad", _ret("close", 1), _const(20)),
    # 趋势的线性斜率（除以 close 变成每 bar 收益率），量的是「走得多快」；
    # trend_r2_20 量「走得多直」，mom20 量「走了多远」，三者不同族。
    "trend_slope20": _op("div", _op("slope", _field("close"), _const(20)), _field("close")),
    # 趋势残差取负：价格偏离 20 日线性趋势越远，短期回归的概率越大。
    # resi 算子此前没有任何内置因子使用。
    "trend_resi20": _op(
        "neg", _op("div", _op("resi", _field("close"), _const(20)), _field("close")),
    ),
    # 双 EMA 之差除以价格：与简单动量同样是趋势，但权重按指数衰减，
    # 对突然反转更钝、对持续慢牛更敏感，过滤掉日线级别的噪声。
    "ema_trend10_30": _op(
        "div",
        _op("sub", _op("ema", _field("close"), _const(10)),
            _op("ema", _field("close"), _const(30))),
        _field("close"),
    ),
    # 波动率的波动率取负：加密常见「平静期突然转震荡」，二阶矩本身也在变。
    # 高 vol-of-vol 意味着风险状态不稳，取负后分数高 = 状态稳定。
    "vol_of_vol20": _op(
        "neg", _op("std", _op("std", _ret("close", 1), _const(5)), _const(20)),
    ),
    # 上涨 bar 占比：sign(ret1) 的 20 日均值 ∈[-1,1]。与动量不同，它只看方向
    # 不看幅度——同样涨 10%，一根大阳线和十根小阳线在这里完全不同。
    "up_day_ratio20": _op("mean", _op("sign", _ret("close", 1)), _const(20)),
    # 资金费期限结构：长窗口均值减短窗口均值，量的是「远期持有成本相对
    # 近期的变化」。funding_carry* 量水平、funding_delta 量一阶差分，
    # 这一条量曲线陡峭度，是第三个独立维度。
    "funding_curve21_7": _op(
        "sub", _op("mean", _field("funding_rate"), _const(21)),
        _op("mean", _field("funding_rate"), _const(7)),
    ),
    # 持仓量的短周期变化（5 日），与已有的 oi_change20 是不同时间尺度；
    # 除以 5 日前水平才能在跨币之间有可比性。
    "oi_change5": _op(
        "div", _op("delta", _field("open_interest"), _const(5)),
        _op("ref", _field("open_interest"), _const(5)),
    ),
    # 持仓量与资金费的共动取负：两者同步上行 = 多头拥挤（加杠杆且付高价），
    # 是加密特有的拥挤度代理；股票因子库里没有对应变量。
    "oi_funding_corr10": _op(
        "neg", _op("corr", _field("open_interest"), _field("funding_rate"), _const(10)),
    ),
}

# 每个因子在语义上的最低数据集要求：只有 futures 才提供这两类数据。
# `require_market_dataset` 已经在采集侧拦一次，这里再拦一次是因为**先验池可以
# 跨计划复用**：同一份 prior_pool 在现货计划里会引用 funding_rate。
_FUTURES_ONLY_FIELDS = frozenset({"funding_rate", "open_interest", "basis"})


def available_names() -> tuple[str, ...]:
    return tuple(sorted(BUILTIN_FACTORS))


def _check_ast(node: Any, reg, path: str) -> set[str]:
    """递归校验 AST，返回其引用到的字段集。"""
    if not isinstance(node, Mapping):
        raise FlowError(E1015, f"{path} 节点必须是映射", path=path)
    keys = set(node)
    if keys == {"field"}:
        name = node["field"]
        if name not in reg.fields:
            raise FlowError(
                E1015, f"{path} 引用了未注册字段 {name!r}", path=path,
                hint=f"已注册: {sorted(reg.fields)}",
            )
        return {name}
    if keys == {"const"}:
        return set()
    if keys != {"op", "args"}:
        raise FlowError(E1015, f"{path} 键集合必须是 field/const/(op,args)", path=path)
    op_name = node["op"]
    spec = reg.operators.get(op_name)
    if spec is None:
        raise FlowError(
            E1015, f"{path} 引用了未注册算子 {op_name!r}", path=path,
            hint=f"已注册: {sorted(reg.operators)}",
        )
    args = node["args"]
    if not isinstance(args, list) or len(args) != int(spec["arity"]):
        raise FlowError(
            E1015,
            f"{path} 算子 {op_name} 需要 {spec['arity']} 个参数，得到 "
            f"{len(args) if isinstance(args, list) else '非列表'}",
            path=path,
        )
    fields: set[str] = set()
    for i, arg in enumerate(args):
        fields |= _check_ast(arg, reg, f"{path}.args[{i}]")
    # 参数位按 param_domain 声明顺序对应 AST 末尾的 const 节点：count 必须是
    # int，fraction（quantile 的 q）必须是 [0,1] 实数。只查末位会让双参数
    # 算子的 window 被 q 的域校验，或反过来放过错位的类型。
    domains = list((spec.get("param_domain") or {}).items())
    for j, (pname, domain) in enumerate(domains):
        node = args[len(args) - len(domains) + j]
        if not (isinstance(node, Mapping) and set(node) == {"const"}):
            raise FlowError(
                E1015, f"{path} 算子 {op_name} 的第 {j + 1} 个参数必须是常量",
                path=path,
            )
        err = param_error(op_name, pname, domain, node["const"])
        if err:
            raise FlowError(E1015, f"{path} {err}", path=path)
    return fields


def factor_ast(name: str) -> dict[str, Any]:
    """名字 → 已规范化的 AST。未知名字 → E1015，不做模糊匹配。"""
    if name not in BUILTIN_FACTORS:
        raise FlowError(
            E1015, f"先验因子未登记: {name!r}", path="search.prior_pool",
            hint=f"可用: {list(available_names())}",
        )
    return normalise_ast(BUILTIN_FACTORS[name])


def resolve_prior_pool(
    names: Sequence[str] | None,
    *,
    available_fields: Iterable[str] | None = None,
    catalog: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把计划里的名字列表解析成 `prior_candidates`，并显式报告被排除的条目。

    三种写法，都只接受**引用标识**（不内联表达式）：

    - `mom60` —— 内置因子池（本模块的 `BUILTIN_FACTORS`）；
    - `db:<name>` / `db:<factor_id>` —— 因子库里已晋升的因子；
    - `db:*` —— 库里全部 active 因子（跨 run 复用的主要入口）。

    返回 `(resolved, skipped)`。`skipped` 不是静默丢弃：每条都带 `name` 与
    `reason`，由 Search 产物原样带出（进 `fitted_state`），所以「先验池里有 5 个、
    实际进池 2 个」在产物里是可查的。

    两种排除原因：
    - `unknown`：名字既不在内置池也不在库里 → 这是**配置错误**，抛 E1015；
    - `field_unavailable`：因子引用了当前数据集没有的字段（例如现货集引用
      `funding_rate`）。这不是配置错误 —— 同一份 prior_pool 跨市场复用是正常需求。
    """
    if not names:
        return [], []
    reg = default_registry()
    have = None if available_fields is None else {str(f) for f in available_fields}
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()

    def _emit(name: str, ast: Any, label: str) -> None:
        ast = normalise_ast(ast)
        fields = _check_ast(ast, reg, f"factors.{label}")
        if have is not None:
            missing = sorted(fields - have)
            if missing:
                skipped.append({"name": name, "reason": "field_unavailable",
                                "detail": ",".join(missing)})
                return
        resolved.append({"name": name, "expression": ast,
                         "required_fields": sorted(fields)})

    for raw in names:
        name = str(raw)
        if name in seen:  # 同名重复：只进池一次，不重复计数
            continue
        seen.add(name)
        if name.startswith("db:"):
            if catalog is None:
                raise FlowError(
                    E1015, f"{name!r} 需要因子库，但调用方没有提供",
                    path="search.prior_pool",
                    hint="把 DataRoot/catalog 传进 resolve_prior_pool",
                )
            key = name[3:]
            rows = catalog.expressions(status="active")
            if key != "*":
                rows = [r for r in rows
                        if key in (r.get("name"), r.get("factor_id"))]
            if not rows:
                raise FlowError(
                    E1015, f"因子库里没有 {key!r}", path="search.prior_pool",
                    hint="先 flow promote 把通过裁决的候选入库",
                )
            for row in rows:
                _emit(str(row["name"]), row["expression"], str(row["name"]))
            continue
        _emit(name, factor_ast(name), name)
    return resolved, skipped


def validate_all() -> dict[str, Any]:
    """加载即自检：整张表一起过，任何一条不合法就整体失败（E1015）。"""
    reg = default_registry()
    for name in available_names():
        ast = normalise_ast(BUILTIN_FACTORS[name])
        _check_ast(ast, reg, f"factors.{name}")
    return {"count": len(BUILTIN_FACTORS), "names": list(available_names())}


__all__ = [
    "BUILTIN_FACTORS", "available_names", "factor_ast",
    "resolve_prior_pool", "validate_all",
]
