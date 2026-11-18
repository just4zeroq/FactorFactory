"""V5 应用层 · CLI 壳（09-app-storage.md §2）。

应用层只有壳：不含业务逻辑、不含口径参数。
核心五条在 S0 冻结：run / station / explain / inspect / stations。
业务命令（doctor/factors/promote...）随 S5–S7 补齐。
"""

__all__: list[str] = []  # CLI 入口从 v5.app.cli 直接导入，避免 -m 双加载告警
