/* ABI 探针 —— 由 xtcli 生成/维护, 请勿手改 (重新生成: xtcli-init)
 *
 * 用途: 让"编译期看到的库结构体布局"与"真正链接进来的库对象"不一致时**编译期报错**。
 *
 * 背景 (实测事故): 旧 rules.mk 把 --specs=nano.specs 只写在 LDFLAGS —— 链接的是
 * libc_nano (struct _reent = 76 B), 而编译按标准 newlib 头 (512 B)。FreeRTOS 的每个
 * TCB 内嵌一份 struct _reent, 于是每个任务白多背 436 B; 4 个任务 + 定时器队列把
 * 4096 B 的堆顶爆, 板子完全没反应 —— 而编译、烧录、以及"黄金体积"比对全都是绿的
 * (FreeRTOS 堆是 .bss 里的定长数组, TCB 变大不改任何 section 一个字节)。
 *
 * XT_ABI_EXPECT_REENT: 由 xtcli 从链接产物的 .map 里读出 _impure_data 的字节数后传入;
 * 0 = 跳过检查。两侧必须是同一个对象类型 —— 否则任何通过 _impure_ptr 交换指针的
 * libc 调用都会按错误的偏移读写。
 */
#ifndef XT_ABI_EXPECT_REENT
#define XT_ABI_EXPECT_REENT 0
#endif

/* 用 __has_include 判断有没有 newlib 的 <reent.h> (不能靠 __NEWLIB__:
   那个宏要等第一个 libc 头被包含之后才存在)。 */
#if defined(__has_include)
#  if __has_include(<reent.h>)
#    include <reent.h>
#    define XT_ABI_HAS_REENT 1
#  endif
#endif
#ifndef XT_ABI_HAS_REENT
#define XT_ABI_HAS_REENT 0
#endif

#if XT_ABI_HAS_REENT && (XT_ABI_EXPECT_REENT != 0)
typedef char xt_abi_probe_reent_size_must_equal_the_linked_libc[
    (sizeof(struct _reent) == (unsigned long) XT_ABI_EXPECT_REENT) ? 1 : -1];
#endif

/* 空翻译单元在部分告警级别下会抱怨; 放一个无害声明避免噪声 */
typedef int xt_abi_probe_ok;
