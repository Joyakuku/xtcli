# =============================================================================
#  xtcli / templates / stm32 / rules.mk
#
#  通用 STM32 (arm-none-eabi) 构建规则 —— 工程无关, 全部由 config.mk 参数化。
#  由 xtcli 复制到 <project>/xtcli/rules.mk, 请勿手改副本。
#
#  config.mk 需要提供:
#     TARGET LDSCRIPT CPU FPU C_DEFS C_INCLUDES SRC_DIRS ASM_SRCS
#     LIBS LIBDIR OPT DBG OUT_DIR BUILD_DIR
# =============================================================================

# ----------------------------------------------------------------------------
# 工具链  (xtcli 会在命令行上覆盖, 这里只是裸跑时的兜底)
#
#  变量名绝对不能叫 GCC_ROOT！
#  GCC_ROOT 是 Arm GNU Toolchain 自己会读的环境变量, 用来定位内部子程序
#  (cc1 等)。而 GNU make 会把"命令行变量"自动导出到子进程环境, 于是
#      make GCC_ROOT=E:/env/Arm/cortex-m/bin
#  会让 arm-none-eabi-gcc 收到一个错误的 GCC_ROOT, 报:
#      cannot execute 'cc1': CreateProcess: No such file or directory
#  已实测: 不设该变量 → 正常; 设为工具链 bin 目录 → cc1 失败。
#  注意 makefile 里的 `GCC_ROOT ?=` 不会被导出(所以看起来"能用"), 但只要
#  有人按注释提示用命令行覆盖, 构建就会以一种极难排查的方式崩掉。
# ----------------------------------------------------------------------------
XT_TOOLCHAIN_BIN ?= E:/env/Arm/cortex-m/bin
PREFIX   := $(XT_TOOLCHAIN_BIN)/arm-none-eabi-
CC := $(PREFIX)gcc
AS := $(PREFIX)gcc
CP := $(PREFIX)objcopy
SZ := $(PREFIX)size
OD := $(PREFIX)objdump
NM := $(PREFIX)nm

OPENOCD     ?= openocd
OPENOCD_IF  ?= interface/stlink.cfg
OPENOCD_TGT ?= target/stm32f1x.cfg

TARGET    ?= firmware
OUT_DIR   ?= Debug
BUILD_DIR ?= build
OPT       ?= -O0
DBG       ?= -g3

# ----------------------------------------------------------------------------
# shell 环境探测
#   依据 $(SHELL) 判定, 而不是 $(wildcard /bin/sh)。
#   真正决定命令由谁执行的是 SHELL, 而且它的写法因 make 发行版而异:
#     MSYS/Git-Bash 的 make : SHELL = /bin/sh        (不含 "sh.exe")
#     mingw32-make 从 cmd 启 : SHELL = cmd.exe
#   所以取 basename 再比对白名单, 不要用 findstring sh.exe。
#   (历史教训: 判错会让 `mkdir -p build` 被 cmd.exe 执行, 于是凭空多出一个
#    名为 "-p" 的目录。)
# ----------------------------------------------------------------------------
XT_SHELL_NAME := $(notdir $(SHELL))
ifeq ($(OS),Windows_NT)
  ifeq ($(filter sh sh.exe bash bash.exe dash,$(XT_SHELL_NAME)),)
    XT_SHELL_POSIX := 0
  else
    XT_SHELL_POSIX := 1
  endif
else
  XT_SHELL_POSIX := 1
endif

ifeq ($(XT_SHELL_POSIX),1)
  MKDIR  = mkdir -p $(1)
  RM_DIR = rm -rf $(1)
  RM_F   = rm -f $(1)
else
  MKDIR  = if not exist "$(subst /,\,$(1))" mkdir "$(subst /,\,$(1))"
  RM_DIR = if exist "$(subst /,\,$(1))" rmdir /s /q "$(subst /,\,$(1))"
  RM_F   = if exist "$(subst /,\,$(1))" del /q /f "$(subst /,\,$(1))"
endif

# ----------------------------------------------------------------------------
# 源文件收集
#   rwildcard: 纯 make 内置函数递归展开, 不依赖 find, 各 shell 行为一致。
# ----------------------------------------------------------------------------
rwildcard = $(foreach d,$(wildcard $(1:=/*)),$(call rwildcard,$d,$2) $(filter $(subst *,%,$2),$d))

# 保序去重 (GNU make 没有内置 uniq; sort 会打乱链接顺序)
XT_UNIQ = $(if $(1),$(firstword $(1)) $(call XT_UNIQ,$(filter-out $(firstword $(1)),$(wordlist 2,$(words $(1)),$(1)))))

# 厂商模板目录/文件必须排除。两类都要处理:
#  1) 目录: CMSIS 的 Templates/ 下同时存在
#       - 全部型号的启动文件  startup_stm32f100xb.s ... startup_stm32f107xc.s
#       - 各内核模板          Core/Template/ARMv8-M/main_s.c, Core_A/Source/irq_ctrl_gic.c
#       - 重复的 system_stm32f1xx.c
#     递归收进来会重复定义 SystemInit 或直接编不过。
#  2) 文件: HAL 源码目录里的 ST 模板文件 (CubeIDE 默认排除, CubeMX 只拷启用模块
#     所以部分工程没有它们)。它们依赖 stm32f1xx_hal_conf.h 里未必开启的模块,
#     直接编会报 "unknown type name 'RTC_HandleTypeDef'" 之类。
XT_EXCLUDE := %/Templates/% %/Template/% %/Core_A/% %_template.c %_template.s

# 去重是必须的: config.mk 里的 ASM_SRCS 已显式指定启动文件, 而 SRC_DIRS 的
# 递归扫描会再找到同一个文件 —— 不去重就会把同一个 .o 链接两遍, 报
# "multiple definition of g_pfnVectors"。
#
# 两种取源方式:
#   C_SRCS_EXPLICIT 非空 -> 用显式文件清单 (来自 .ewp 等 IDE 工程, 精确)
#   否则                 -> 按 SRC_DIRS 递归扫描
ifneq ($(strip $(C_SRCS_EXPLICIT)),)
  C_SRCS := $(call XT_UNIQ,$(C_SRCS_EXPLICIT))
else
  C_SRCS := $(call XT_UNIQ,$(filter-out $(XT_EXCLUDE),$(foreach d,$(SRC_DIRS),$(call rwildcard,$d,*.c))))
endif
ASM_SRCS := $(call XT_UNIQ,$(strip $(ASM_SRCS) $(filter-out $(XT_EXCLUDE),$(foreach d,$(SRC_DIRS),$(call rwildcard,$d,*.s)))))

# 对象名拍平成单层, 保留目录信息以避免同名文件互相覆盖。
# 约定: 源文件名中不得出现双下划线, 否则下面的反查会还原出错误路径。
mangle = $(subst /,__,$(1))

OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(call mangle,$(C_SRCS)))
OBJS += $(patsubst %.s,$(BUILD_DIR)/%.o,$(call mangle,$(ASM_SRCS)))
DEPS := $(OBJS:.o=.d)

# ----------------------------------------------------------------------------
# 编译 / 链接选项
# ----------------------------------------------------------------------------
CFLAGS  = $(CPU) $(FPU) $(C_DEFS) $(C_INCLUDES) $(OPT) $(DBG) \
          -Wall -fmessage-length=0 -ffunction-sections -fdata-sections -MMD -MP

ASFLAGS = $(CPU) $(FPU) $(OPT) $(DBG) -Wall -fmessage-length=0 -x assembler-with-cpp

LDFLAGS = $(CPU) $(FPU) $(OPT) $(DBG) \
          -T$(LDSCRIPT) \
          -Wl,-Map=$(OUT_DIR)/$(TARGET).map \
          -Wl,--gc-sections \
          -Wl,--print-memory-usage \
          -static --specs=nano.specs \
          $(LIBDIR) $(LIBS)

# ----------------------------------------------------------------------------
# 规则
# ----------------------------------------------------------------------------
.PHONY: all clean size disasm dump flash flash-bin help

all: $(OUT_DIR)/$(TARGET).elf $(OUT_DIR)/$(TARGET).hex $(OUT_DIR)/$(TARGET).bin

.SECONDEXPANSION:
$(BUILD_DIR)/%.o: $$(subst __,/,$$*).c
	@$(call MKDIR,$(BUILD_DIR))
	@echo "  CC      $<"
	@$(CC) -c $(CFLAGS) $< -o $@

$(BUILD_DIR)/%.o: $$(subst __,/,$$*).s
	@$(call MKDIR,$(BUILD_DIR))
	@echo "  AS      $<"
	@$(AS) -c $(ASFLAGS) $< -o $@

$(OUT_DIR)/$(TARGET).elf: $(OBJS) $(LDSCRIPT)
	@$(call MKDIR,$(OUT_DIR))
	@echo "  LD      $@"
	@$(CC) $(OBJS) $(LDFLAGS) -o $@
	@$(SZ) $@

$(OUT_DIR)/%.hex: $(OUT_DIR)/%.elf
	@echo "  HEX     $@"
	@$(CP) -O ihex $< $@

$(OUT_DIR)/%.bin: $(OUT_DIR)/%.elf
	@echo "  BIN     $@"
	@$(CP) -O binary -S $< $@

size: $(OUT_DIR)/$(TARGET).elf
	@$(SZ) -A -x $<

disasm: $(OUT_DIR)/$(TARGET).elf
	@$(OD) -h -S $< > $(OUT_DIR)/$(TARGET).lst
	@echo "  -> $(OUT_DIR)/$(TARGET).lst"

dump: $(OUT_DIR)/$(TARGET).elf
	@$(OD) -t -j .isr_vector $<

flash: $(OUT_DIR)/$(TARGET).elf
	$(OPENOCD) -f $(OPENOCD_IF) -f $(OPENOCD_TGT) \
	  -c "program $(OUT_DIR)/$(TARGET).elf verify reset exit"

flash-bin: $(OUT_DIR)/$(TARGET).bin
	$(OPENOCD) -f $(OPENOCD_IF) -f $(OPENOCD_TGT) \
	  -c "program $(OUT_DIR)/$(TARGET).bin 0x08000000 verify reset exit"

clean:
	@echo "  CLEAN"
	@$(call RM_DIR,$(BUILD_DIR))
	@$(call RM_F,$(OUT_DIR)/$(TARGET).elf)
	@$(call RM_F,$(OUT_DIR)/$(TARGET).hex)
	@$(call RM_F,$(OUT_DIR)/$(TARGET).bin)
	@$(call RM_F,$(OUT_DIR)/$(TARGET).map)
	@$(call RM_F,$(OUT_DIR)/$(TARGET).lst)

help:
	@echo "make            构建 ($(OUT_DIR)/$(TARGET).elf .hex .bin)"
	@echo "make -j8        并行构建"
	@echo "make size       查看 FLASH/RAM 占用"
	@echo "make disasm     生成反汇编"
	@echo "make flash      OpenOCD 烧录"
	@echo "make clean      清理"

-include $(DEPS)
