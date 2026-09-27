# ps-legacy —— 冻结的旧实现（不要使用）

这里是 xtcli 的**上一代 PowerShell 实现**，仅作为测试的 **oracle** 保留：

* `tests/test_parity_ps.py` 会在同一个工程上分别跑本实现与 `lib/xtcli.ps1`，
  逐字段比较抽取结果，用来证明 Python 版没有在迁移中改变行为；
* 缺了本目录或 `pwsh` 时，该测试会自动跳过，其余测试不受影响。

**注意**：

1. 里面的命令名（`stm32-init-pj` / `pj-build` 等）是**历史命名**，已在当前版本中
   统一为 `xtcli-init-pj` / `xtcli-build-pj` / `xtcli-burn-pj`（见根目录 README）。
   请使用根目录 `bin/`（由 `setup.ps1` 生成）里的命令，不要用本目录的 shim。
2. 本目录**不再维护**：新功能、新芯片、新架构一律只加在 `src/xtcli/`（Python 实现）。
3. 保留它是为了"行为可对照"这一条测试价值，不是为了回退使用。
