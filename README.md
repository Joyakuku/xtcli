# xtcli —— 多芯片嵌入式工程 CLI

对一个**已经存在**的嵌入式工程做 **初始化 → 构建 → 烧录 → 体检**，
不替换工程原有的构建体系，只在必要时补齐缺的东西。

```text
xtcli-init-pj   [选项]     初始化到"可编译"（必要时试编译一次）
xtcli-build-pj  [选项]     构建
xtcli-burn-pj   [选项]     烧录（+ 独立回读校验）
xtcli-doctor    [选项]     体检环境 / 工程，给确定性结论
```

> **命令名不带芯片前缀**：后端由工程本身自动识别（`.cproject` / `*.ioc` / `*.ewp` /
> `*.uvprojx` / `Makefile` / ESP-IDF `CMakeLists.txt`），所以同一套命令处理
> STM32 / GD32 / AT32 / APM32 / CH32V / RP2040 / nRF / SAM 与 ESP32。
> 要强制指定后端加 `-Target`（如 `-Target stm32`、`-Target espidf`）。

**文档**

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [docs/使用手册.md](docs/使用手册.md) | 使用者 | 安装、上手流程、全部命令与选项、工程形态矩阵、烧录、故障排查、会写哪些文件 |
| [docs/维护手册.md](docs/维护手册.md) | 维护者 | 架构与不变量、加芯片/加后端/加架构的步骤、受管写入契约、测试闸门、发布检查、环境维护、历史教训 |
| 本文 | 所有人 | 定位、设计原则、快速开始、命令速查、能力与边界摘要 |

---

## 设计原则（优先级从高到低）

1. **宁可拒绝，不产出可疑固件。** 拿不准的一律给确定性拒绝 + 可执行建议，
   而不是"先编出来再说"。凡是"能编但错"的方向（错误的 `-mcpu`、超出真机的 `.ld`、
   用 ARM 工具链编 RISC-V）都被显式拦住。
2. **安全接管。** 写进工程的每个文件都登记在受管清单里，覆盖前先备份成 `*.xtcli-bak`；
   工程自带的东西永不静默覆盖。
3. **换台机器也能跑。** 核心只用 CPython 标准库（`dependencies = []`），
   用项目 venv 包裹环境；工具链按前缀在 `E:\env` 下自动发现。
4. **广度优先。** 加芯片 = 加一个 `data/devices/*.json`，不加命令、不改代码。

## 支持范围（摘要）

| 维度 | 现状 |
| --- | --- |
| 芯片数据 | **8 张表 / 68 个系列键**：STM32（20 系列）、GD32（13）、AT32（11）、APM32（7）、nRF（7）、SAM（7）、RP2040（2）、CH32V（1） |
| 架构 | ARM Cortex-M0/M0+/M3/M4(F)/M7(F)/M23/M33(F) + **RISC-V（RV32EC / RV32IMAC）** |
| 工程形态 | CubeIDE `.cproject`、CubeMX `.ioc`、IAR `.ewp`+`.icf`、Keil `.uvprojx`、自带 Makefile、ESP-IDF CMake |
| 烧录 | openocd（探针 × target）、pyOCD（CMSIS-Pack）、ESP32 esptool/JTAG |
| 运行时依赖 | 无（可选开发工具 pytest/ruff/mypy 与可选 `.venv-pyocd` 均不影响核心） |

**明确未做**：IAR `.icf` 的忠实翻译、Keil 器件/分组解析、PlatformIO 与独立 CMake 后端、
烧录的真机验证（手头无 ST-Link/ESP32/CH32V 硬件）。详见使用手册的"未验证 / 不负责"。

---

## 快速开始

```powershell
# 1) 建/修项目 venv（独立 CPython，拒绝 conda 底座）
xtcli-setup                 # 需要 pyOCD 时: xtcli-setup -Pyocd
                            # 干净重建:     xtcli-setup -Force

# 2) cd 到工程目录（或它的子目录），先体检一次
cd E:\path\to\myproject
xtcli-doctor                # 看清工具链、芯片参数、内存布局、探针

# 3) 初始化 → 构建 → 烧录（同一套命令，芯片自动识别）
xtcli-init-pj               # CubeIDE / IAR / Keil / CubeMX / ESP-IDF 都走这条
xtcli-build-pj
xtcli-burn-pj

# ESP32 的 UART 烧录要串口；强制指定后端用 -Target
xtcli-burn-pj -Port COM7
xtcli-build-pj -Target espidf
```

工具链搜索顺序：`XTCLI_ROOTS` 环境变量 → `xtcli.json` 的 `roots` → 内置默认根
（`E:\env`、`E:\env\msys64`）→ `PATH`。本机已装：

| 架构 | 稳定别名 | 前缀 |
| --- | --- | --- |
| ARM Cortex-M | `E:\env\Arm\cortex-m` | `arm-none-eabi-` |
| RISC-V | `E:\env\RiscV\riscv-none-elf` | `riscv-none-elf-` |
| ESP32 | `E:\env\esp\v6.0.1\esp-idf`（+ `C:\Espressif\tools`） | — |

---

## 命令速查

| 入口名（共 6 个，等价于同一套 CLI） | 说明 |
| --- | --- |
| `xtcli-init-pj` / `xtcli-build-pj` / `xtcli-burn-pj` | `xtcli init/build/burn`；后端按工程自动识别，强制用 `-Target` |
| `xtcli-doctor` | `xtcli doctor`（只读体检） |
| `xtcli <动词>` | 同上（动词：`init` / `build` / `burn` / `doctor`） |
| `xtcli-setup` | 运行 `setup.ps1`（建/修 venv，可选 `-Pyocd` / `-Force`） |

常用选项（**完整表在使用手册**）：`-Target` `-Root` `-Config` `-Clean` `-NoBuild`
`-NoStubs` `-MakeTarget` `-Interface` `-OcdTarget` `-ProbeSerial` `-Bin` `-Address`
`-Elf` `-Probe` `-FlashType` `-Flasher` `-Cpu` `-Fpu` `-Port` `-Refresh` `-Quiet`
`-LogFile` `-h/--help` `-v/--version`。

**退出码**（脚本可直接依赖）：
`0 成功 · 1 失败 · 2 用法 · 3 环境 · 4 工程 · 5 不支持 · 6 构建 · 7 烧录`。
关键区分：**探针没插是 3 而不是 7** —— 那不该让 CI 以为固件坏了。

---

## 工具会写哪些文件

| 位置 | 内容 |
| --- | --- |
| `<工程>/xtcli/` | `config.mk`、`rules.mk`、`Makefile`（工具自己的构建入口）、`stubs/`、`.owned.json`（受管清单）、`idf-env.json`（ESP-IDF）、兜底 `.ld` |
| `<工程>/compile_commands.json` | 给 clangd/IDE 的编译数据库（已存在时先备份） |
| `<工程>/Makefile` | **仅当工程没有 Makefile 时**补一个转发壳（带 `xtcli:generated` 标记，便于下次识别） |
| 工程缺失的关键件 | 例如没有 `.ld` 时从模板库复制一份；没有启动文件时不猜，直接拒绝 |

覆盖规则：不是 xtcli 生成的、或生成后被改过 → 先备份 `*.xtcli-bak` 再写并告警；
是 xtcli 生成的且未改动 → 直接重写。想收回控制权，把自己的文件放回去即可。

---

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t tests   # 285 项
.\.venv\Scripts\python.exe -m ruff check src tests
```

测试里有两类"闸门"必须保持绿：

* **体积黄金值**：三个真实 STM32 工程的 `text/data/bss` 逐字节一致
  （`demo` 88076/124/25928、`freertos_hal_template` 16904/96/6488、`1_LED` 5088/12/1068）；
* **架构不变量**：零第三方依赖、分层、单模块 ≤700 行、data/assets 单一入口、命令面冻结。

改接口或加后端前，请先读 [docs/维护手册.md](docs/维护手册.md)。

---

## 目录与第三方文件

```text
README.md  docs/  setup.ps1  pyproject.toml
src/xtcli/            源码（31 个模块）
src/xtcli/assets/     rules.mk 模板、运行时桩、链接脚本模板
data/devices/         8 张芯片数据表（加芯片只改这里）
tests/                23 个 test_*.py
ps-legacy/            冻结的旧 PowerShell 实现（tests/test_parity_ps.py 用它当 oracle）
```

**第三方文件与许可归属**（重要）：

`src/xtcli/assets/stm32/` 下的三个文件是 **STM32CubeIDE 自动生成**的，文件头带
`Copyright (c) STMicroelectronics` 声明，按 ST 的软件许可条款使用与分发：

| 文件 | 来源 |
| --- | --- |
| `stm32/ld/STM32F103RCTX_FLASH.ld` | STM32CubeIDE 为 STM32F103RCTx 生成的链接脚本 |
| `stm32/syscalls.c`、`stm32/sysmem.c` | STM32CubeIDE 生成的 newlib 系统调用/内存桩 |

它们**不属于本项目的著作权范围**，本项目自身的许可不覆盖它们；
如果你的工程自带这些文件，xtcli 也不会用模板覆盖（见"工具会写哪些文件"）。

其余文件（`src/`、`data/`、`tests/`、`docs/`、`ps-legacy/`、`setup.ps1`）为项目自有内容。
