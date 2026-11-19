"""供给：把外部世界写成一组 parquet 文件（19-supply.md）。

只有两个模块：

- `store`：路径规则 + 读写 + **现算身份**（checksum / coverage / interval / gaps）。
  没有 manifest、没有版本目录、没有别名表——「用的是哪份数据」由 checksum 回答；
- `fetch`：四数据集采集（ccxt）。市场维度（spot/futures）与数据集的相容性
  在这里拦，且是连网之前就拦。备份站归档在 `binance_vision`，落盘契约相同。

这一层只读写 `<数据根>/<dataset>/<exchange>/<market>/`，不写 Ticket、不进状态机、
不被编译或单步触发。采集是流水线**之外**的动作：数据先存在，研究才可能开始。
"""
