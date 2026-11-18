"""错误分层与错误码（S0 契约层）。

分层不按模块划分，而按**谁该修**划分。这样调用方只看退出码就知道该找谁：

===========  =======  ======  ==========================================
类            前缀     退出码   含义
===========  =======  ======  ==========================================
用法与配置     E1xxx    2       用户输入不合法，改配置即可
契约违反       E2xxx    3       系统自身不变量被破坏，按 bug 处理
执行失败       E3xxx    1       输入合法但这一步没成功
供给失败       E4xxx    4       事实层不可用或不适用
存储与 IO      E5xxx    1       落盘、权限、并发
===========  =======  ======  ==========================================

三条硬规则（违反任何一条都等于把故障变成谜题）：

1. **不得捕获后继续**。当前可降级的错误数为零，任何 `Error` 都必须向上传播。
2. **不得换码**。底层抛 `E4002`，上层不得包装成 `E1001`；分类是排查的第一信息。
3. **不得只记日志不返回**。日志是旁路，返回值是契约，二者都要有。

`layer` 不作为独立字段存储，而是由 `code` 唯一决定。存两份必然漂移，且漂移
之后没有任何检查点能发现"这个错误被归错了类"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .exit_codes import ExitCode

# --------------------------------------------------------------------------
# 分层
# --------------------------------------------------------------------------


class ErrorLayer(str, Enum):
    """错误所属的层，决定退出码。"""

    USAGE = "usage"
    CONTRACT = "contract"
    EXECUTION = "execution"
    SUPPLY = "supply"
    STORAGE = "storage"


_LAYER_BY_PREFIX: Mapping[str, ErrorLayer] = MappingProxyType(
    {
        "E1": ErrorLayer.USAGE,
        "E2": ErrorLayer.CONTRACT,
        "E3": ErrorLayer.EXECUTION,
        "E4": ErrorLayer.SUPPLY,
        "E5": ErrorLayer.STORAGE,
    }
)

_EXIT_BY_LAYER: Mapping[ErrorLayer, ExitCode] = MappingProxyType(
    {
        ErrorLayer.USAGE: ExitCode.USAGE,
        ErrorLayer.CONTRACT: ExitCode.CONTRACT,
        ErrorLayer.EXECUTION: ExitCode.EXECUTION,
        ErrorLayer.SUPPLY: ExitCode.SUPPLY,
        ErrorLayer.STORAGE: ExitCode.EXECUTION,
    }
)

# --------------------------------------------------------------------------
# 用法与配置 E1xxx（退出码 2）
# --------------------------------------------------------------------------

E1001 = "E1001"  # 配置路径不存在于 schema
E1002 = "E1002"  # 实现名不在能力目录
E1003 = "E1003"  # 参数含 schema 之外的字段
E1004 = "E1004"  # 参数类型、范围或枚举不符
E1005 = "E1005"  # 构成性参数出现在覆盖层且未放行
E1006 = "E1006"  # 严格模式未注入裁决者
E1007 = "E1007"  # origins 未覆盖全部叶子
E1008 = "E1008"  # 缺必填参数，或出现未解析占位
E1009 = "E1009"  # 构成性参数改动但试验族未声明；或数据版本变化后在原计划上重跑搜索（I59）
E1010 = "E1010"  # 扫参未按预注册枚举（自动遍历）
E1011 = "E1011"  # 默认实现指向占位或演示实现
E1012 = "E1012"  # 实现参数与其声明的拟合/变换阶段冲突
E1013 = "E1013"  # 经济口径不自洽（判据口径与回测口径不一致）
E1014 = "E1014"  # 算法声明的字段超出已固化供给版本的覆盖
E1015 = "E1015"  # 算法声明含禁用算子，或与全局算子白名单冲突
E1016 = "E1016"  # 有状态算法拟合窗口非 mine_only，或拟合结果不可重建
E1017 = "E1017"  # 多算法配额总和超计划上限，或实际产出超上限
E1018 = "E1018"  # 组合域成员口径不一致、缺可用时间、纳入规则含表现类条件，或跨执行域成员未显式声明（I60）
E1019 = "E1019"  # 组合拟合窗口起点早于域内最晚可用时间
E1020 = "E1020"  # 工位参数里出现 caliber 已定义的字段
E1021 = "E1021"  # 组合三段窗口非法：评估与拟合重叠、隔离段缺失、评估越出搜索区间
E1022 = "E1022"  # rebalance < horizon 但未声明重叠处理，或 width_policy 缺失

# --------------------------------------------------------------------------
# 契约违反 E2xxx（退出码 3）
# --------------------------------------------------------------------------

E2001 = "E2001"  # 下层尝试调用上层，或能力层取得禁止的入口
E2002 = "E2002"  # 指纹输入含 NaN / Infinity / 非法路径
E2003 = "E2003"  # 结构缺必填字段
E2004 = "E2004"  # 禁止内容：Evidence 出结论、日志含凭据、Parcel 被就地修改
E2005 = "E2005"  # 不可变字段被改写
E2006 = "E2006"  # 检测到第二套执行路径或第二套裁判入口
E2007 = "E2007"  # 设施被绕过：某层自行取时钟、随机源或写日志文件
E2008 = "E2008"  # 调用了未在 composes 中声明的能力，或组合深度超过两层
E2009 = "E2009"  # 自适应算法被并发执行（全局随机状态不可重入）

# --------------------------------------------------------------------------
# 执行失败 E3xxx（退出码 1）
# --------------------------------------------------------------------------

E3001 = "E3001"  # 上游产物缺失
E3002 = "E3002"  # 上游指纹与 lineage 不符
E3003 = "E3003"  # 产物为空或无法重建；含截面丢弃时点占比超 20%（I61），不得降级为成功
E3004 = "E3004"  # 工位实现抛出未预期异常
E3005 = "E3005"  # 状态转移非法
E3006 = "E3006"  # 违反一次性事件语义

# --------------------------------------------------------------------------
# 供给失败 E4xxx（退出码 4）
# --------------------------------------------------------------------------

E4001 = "E4001"  # 别名指向不存在的版本
E4002 = "E4002"  # 版本覆盖区间、标的集或新鲜度不满足本次时间轴（门禁 B）
E4003 = "E4003"  # 时间末端未闭合
E4004 = "E4004"  # 校验和不符
E4005 = "E4005"  # 门禁 A 未通过的版本被引用
E4006 = "E4006"  # 维护动作发生在编译之后或被单步触发
E4007 = "E4007"  # 时间索引重复或非单调

# --------------------------------------------------------------------------
# 存储与 IO E5xxx（退出码 1）
# --------------------------------------------------------------------------

E5001 = "E5001"  # 目录不可写或权限不足
E5002 = "E5002"  # 租约冲突，已有写者
E5003 = "E5003"  # 原子发布失败（暂存未完成）
E5004 = "E5004"  # 写入被拒绝：共享仓、内容寻址冲突、或超出本执行目录
E5005 = "E5005"  # 日志与产物指纹不一致


ALL_ERROR_CODES: tuple[str, ...] = (
    E1001, E1002, E1003, E1004, E1005, E1006, E1007, E1008, E1009, E1010,
    E1011, E1012, E1013, E1014, E1015, E1016, E1017, E1018, E1019, E1020,
    E1021, E1022,
    E2001, E2002, E2003, E2004, E2005, E2006, E2007, E2008, E2009,
    E3001, E3002, E3003, E3004, E3005, E3006,
    E4001, E4002, E4003, E4004, E4005, E4006, E4007,
    E5001, E5002, E5003, E5004, E5005,
)
"""全部已定义错误码。不在这个集合里的码一律拒绝构造。

不做"前缀对就放行"的宽松校验：那样拼错的 `E1023` 会悄悄通过，而它对应的
手工 `raise` 永远不会被任何人读到。
"""


def layer_for(code: str) -> ErrorLayer:
    """由错误码推出所属层。未知码抛 `ValueError`（fail-closed）。"""
    if code not in ALL_ERROR_CODES:
        raise ValueError(
            f"未定义的错误码 {code!r}；新增错误码必须同时加进 "
            f"v5/contracts/errors.py 的 ALL_ERROR_CODES 与 "
            f"docs/architecture/v5/s0/01-data-contracts.md §4.2"
        )
    return _LAYER_BY_PREFIX[code[:2]]


def exit_code_for(code: str) -> ExitCode:
    """错误码 → 进程退出码。"""
    return _EXIT_BY_LAYER[layer_for(code)]


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Error:
    """一个可序列化的错误。

    `layer` 与 `exit_code` 是从 `code` 推导的只读属性，不接受独立赋值——
    见模块 docstring。
    """

    code: str
    message: str
    path: str | None = None
    hint: str | None = None
    context: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False
    )
    retryable: bool = False

    def __post_init__(self) -> None:
        layer_for(self.code)  # 未知码在构造期就炸，不留到序列化之后
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Error.message 必须是非空字符串")
        if self.context is not None:
            # 结构化上下文进入跨层结构后不得再被就地修改。
            object.__setattr__(self, "context", MappingProxyType(dict(self.context)))

    @property
    def layer(self) -> ErrorLayer:
        return layer_for(self.code)

    @property
    def exit_code(self) -> ExitCode:
        return exit_code_for(self.code)

    def to_dict(self) -> dict[str, Any]:
        """序列化。可选字段缺失时**不补 null**（null 与缺失不等价）。"""
        out: dict[str, Any] = {
            "code": self.code,
            "layer": self.layer.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.path is not None:
            out["path"] = self.path
        if self.hint is not None:
            out["hint"] = self.hint
        if self.context is not None:
            out["context"] = dict(self.context)
        return out


class FlowError(Exception):
    """携带 `Error` 的异常。上层只允许原样传播，不允许换码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        hint: str | None = None,
        context: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        self.error = Error(
            code=code,
            message=message,
            path=path,
            hint=hint,
            context=context,
            retryable=retryable,
        )
        detail = f"[{code}] {message}"
        if path:
            detail += f" (at {path})"
        super().__init__(detail)

    @property
    def code(self) -> str:
        return self.error.code

    @property
    def exit_code(self) -> ExitCode:
        return self.error.exit_code


__all__ = [
    "ALL_ERROR_CODES",
    "Error",
    "ErrorLayer",
    "FlowError",
    "exit_code_for",
    "layer_for",
]
