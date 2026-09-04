/* 宏调用生成表项：X-Macro 表项文本是宏调用形态（u-boot U_BOOT_SUBCMD_MKENT 同构）。
 * msg_id 兜底逻辑须从首实参（剥引号）取出。
 */
#include "handlers.h"

typedef struct {
    const char *name;
    msg_handler_t handler;
} CmdEntry;

#define CMD_MKENT(name, handler) { #name, handler }

static const CmdEntry g_cmdMkTable[] = {
    CMD_MKENT(MSG_1001, sess_handle_invite),
    CMD_MKENT(MSG_1002, sess_handle_bye),
};
