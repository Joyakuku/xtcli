# 变更记录（CHANGELOG）

本文件记录 **1.0.0 起**的版本变更。更早的实现（PowerShell 版 `ps-legacy/`）已在 1.0.0
中删除，其历史留在 git 历史里。

约定：**命令面（命令名 / 动词 / 选项）与退出码是冻结契约** —— 任何改动都必须同步
`src/xtcli/commands.py`、`tests/test_architecture.py`、`setup.ps1`、两份手册。

---

## [1.0.0] —— 首个正式版本

命令面就此冻结，并做了三件"把工具变稳"的事：**全局开关成套化**、**ABI 哨兵**、
**产物新鲜度判定**；顺带删掉了全部历史包袱。

### 修复：`--specs=nano.specs` 只在链接行 → 编译/链接两套 ABI（致命）

* 现象：STM32F103 工程 `2_LED_freeRtos` 编译、烧录、"体积黄金值"比对全绿，但板上
  **两个 LED 全灭** —— `vTaskStartScheduler()` 死在 `configASSERT`。
* 根因：`--specs=nano.specs` 只写在 `LDFLAGS`，于是**编译**按标准 newlib 头
  （`sizeof(struct _reent)` = 512 B）、**链接**用 libc_nano（76 B）；
  `configUSE_NEWLIB_REENTRANT=1` 时每个 TCB 白背 436 B → 4 个任务 + 定时器队列的堆需求
  从约 3.0 KB 涨到 4792 B，超出 `configTOTAL_HEAP_SIZE` = 4096 B。
* 为什么"体积一致"没抓到：FreeRTOS 堆是 `.bss` 里的定长数组 `ucHeap[4096]`，TCB 变大
  不改变 `.text/.data/.bss` 任何一项。`.text` 只差 88–104 B（立即数与 memset 变小）。
* 修法（三道防线）：
  1. `config.mk` 新增 `RUNTIME_LIB` / `SPECS` / `CSTD` 三个全局开关（进指纹），
     `rules.mk` 把 `$(SPECS)` 成套注入 `CFLAGS/CXXFLAGS/ASFLAGS/LDFLAGS`；
  2. `rules.mk` 解析期自检（`XT_REQUIRE_IN` + `XT_FLAG_SELFCHECK`）与 `make show-flags`；
  3. 构建后 **ABI 哨兵**：`assets/abi-probe.c` 断言编译期 `sizeof(struct _reent)` == 链接
     产物里 `_impure_data` 的实际大小（`make abi-check`），不一致则构建以退出码 6 失败。

### 新增：产物新鲜度与受管指纹的确定性判定

* 所有对象依赖 `xtcli/config.mk`、`xtcli/rules.mk`（`XT_PARAM_FILES`）：参数一变就全量重编，
  不再出现"只重链接、固件里混着两套参数"的假成功；
* `backends/gcc_common.stale_objects()`：构建前扫描 `build/*.o`（只认由工程源文件编出来的
  对象），比参数文件旧的直接判失败；
* `artifact_is_stale()`：烧录前判断固件是否比源文件旧（缺失或陈旧则自动重新构建，
  `-NoBuild` 时明确告警）；
* 找不到"本工程名"的产物时不再静默跳过哨兵：会提示 `out_dir` 里的外来产物并建议重新
  `xtcli-init`（工程改名场景）；`config.mk` 没有指纹时同样明确要求重新初始化。

### 变更：命令链重构（`commands.py` 成为唯一出处）

| 命令 | 展开 |
| --- | --- |
| `xtcli-init` | `init` |
| `xtcli-build` | `build` |
| `xtcli-build-all` | `build -Clean` |
| `xtcli-burn` | `burn`（默认 openocd 链） |
| `xtcli-burn-openocd` | `burn -Flasher openocd` |
| `xtcli-burn-flash` | `burn -Flasher pyocd`（CMSIS-Pack flash 算法链） |
| `xtcli-doctor` | `doctor` |

* 新增 `src/xtcli/commands.py`（`Chain` / `CHAINS` / `by_name` / `shim_names` / `expand`）：
  shim、帮助、文档里的命令名全部由它派生；`tests/test_commands.py` 守四者一致；
* 新增选项 `-NoAbiCheck`（跳过 ABI 哨兵，仅供排障）。

### 破坏性变更（升级步骤）

1. **不再有任何命令别名。** 历史命令名（`xtcli-*-pj`、`stm32-*-pj`、`esp32-*-pj`、`pj-*`）
   会被 `setup.ps1` 从 `bin/`、`bin-msys/` **删除**；请改用上表 7 个名字。
2. **删除 `ps-legacy/` 与 `tests/test_parity_ps.py`**，不再维护 PowerShell 实现。
3. **不再读取历史格式的 `xtcli.json`**：`schema_version` 必须等于当前值，否则整份缓存
   丢弃重建（只影响首次运行的速度，不影响正确性）。
4. 升级动作：`xtcli-setup`（清旧 shim + 重建 shim）→ 在每个工程里重新 `xtcli-init`
   （重新生成 `xtcli/{Makefile,config.mk,rules.mk}` 与 `xtcli/abi-probe.c`）→ `xtcli-build-all`。

### 清理

* 工程侧：删除工具在 `software/` 各工程里留下的 `xtcli/`、`build/`、`Debug/`、
  `compile_commands.json` 等生成物（用户自有的 `Makefile` 一律保留）；
* 工具侧：删除历史模板兼容分支（骨架开关探测、旧指纹专用告警）、别名机制与
  `renamed_to` 提示链；
* 版本号：`0.2.0` → `1.0.0`（`pyproject.toml` 与 `xtcli.__version__` 单一出处）。

### 测试基线

`python -m unittest discover -s tests -t tests` → **321 项**（5 skipped：硬件/IDF/pyOCD 门控），
`ruff check src tests` 全绿。体积黄金值在 1.0.0 重新记录（`--specs` 成套后 `.text` 变化）：
`demo` 87972/124/25928、`freertos_hal_template` 16816/96/6488、`1_LED` 5088/12/1068。
