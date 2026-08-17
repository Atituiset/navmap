/* 嵌套初始化表 fixture：表项首字段本身是 {...} 聚合，
 * clang_Cursor_Evaluate 对 INIT_LIST_EXPR 会 segfault（libclang 20.1.0 实测，
 * u-boot cmd/ethsw.c 复现）——回归防护：eval_int 按 kind 白名单过滤。 */
#include "handlers.h"

typedef struct {
    int ids[2];
    msg_handler_t handler;
} nested_t;

static const nested_t g_nestedTable[] = {
    { {1, 2}, sess_handle_invite },
};
