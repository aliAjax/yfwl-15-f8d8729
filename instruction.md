# 漂移光变与跨批次归位

多批次巡天换过计时板，五条未标号曲线的局部时间轴带着不同的线性漂移。每个目标在当前批次可能留下零到两次真实凌日；更早的重复观测留在批次日志中，足以把各曲线放回同一时间基准。

## 输入

示例报文位于 `/app/data/transit_case.json`，由 `--input` 指向待校准时序文档。

```text
输入顶层键：schema_version, case_id, targets, light_curves, batch_logs, period_bounds_days
目标键：target_code, depth_ppm, duration_h, stellar_radius_rsun
曲线键：curve_code, batch_reference_d, time_d, flux
日志键：log_code, curve_code, repeat_code, batch_reference_d, recorded_center_d, recorded_depth_ppm, recorded_width_h, anchor_center_d
范围键：min, max
```

输入必须恰好有五个目标和五条未标号曲线。`target_code` 与 `curve_code` 分别在同层唯一。每条曲线有六十个严格递增的时间点及等长流量数组，并对应十二条日志：四个 `repeat_code`，每组三次跨批次重复观测。每条曲线恰有一个非空的 `anchor_center_d`，其余为 `null`。

局部时钟满足 `global = time_rate * recorded + time_offset_d + batch_reference_d`。`time_rate`、`time_offset_d` 和各曲线到目标的对应均未给出。`period_bounds_days` 的输入值固定为 12.0 和 16.0。输入不提供候选周期表。

缺少键即拒绝。额外键即拒绝。同名键不得在同一层重复。版本必须是数值一，布尔值不算数值。所有数值字段必须有限。

## 联合反演

先用每组重复日志消除未知事件时刻，以线性最小二乘恢复每条曲线的时钟率和相位偏移，不使用漂移网格。再把历史事件与当前事件映射到同一时间轴。

周期候选只从去漂移后的历史事件中心差除以整数一至二十四产生，并保留十二到十六天的值。

```text
支持排序：四组历史事件、当前全部凌日和全部 60 个点按相位距离进入窗口；窗口为 `duration_h / 48 + 0.012` 天，排序依据历史观测数、当前点数、凌日数、残差。
```

`support_margin` 是最佳与次佳不同周期的总支持差，必须为正。

目标对应按恢复深度和持续时间的全局一一匹配求得。当前无凌日的目标从重复日志恢复周期、深度和宽度；当前有一或两次凌日的目标同时用当前事件复核。把 `time_rate`、`time_offset_d` 与 `batch_reference_d` 代回后，`transit_centers_d` 给出全局中心；列表升序且长度等于 `transit_count`。

## 输出与历史

成功时两个输出流均留空。

```text
输入错误：标准输出为空；标准错误一行 `transit: `；状态二；无调用栈。
```

```text
报告顶层键：schema_version, case_id, catalog, status
目录项键：target_code, curve_code, time_rate, time_offset_d, transit_count, transit_centers_d, period_days, epoch_days, depth_ppm, duration_h, log_support, point_support, support_margin
```

`catalog` 按 `target_code` 升序排列，`status` 为 `calibrated`。`epoch_days` 是首个历史事件中心在周期内的相位，范围为 `[0, period_days)`。

历史文件是键集合为 `case_id`、`fingerprint`、`target_code`、`orbit_sha256` 的紧凑 JSONL。首次输入为五个目标各追加一行；同一输入重放时字节不变；同案号换输入时追加一套五行记录；失败输入保持旧字节。报告和历史均以一个换行结束，暂存后一次换名。

完成标准：样例返回零，报告含五条记录，历史含五行。整批反演应在八秒内返回；测试进程在十秒时接管终止。
