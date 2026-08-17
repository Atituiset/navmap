#include "msg_ids.h"
#include "handlers.h"

/* 字段顺序与初始化顺序刻意不同：验证指定初始化器不按源码顺序假设 */
typedef struct {
    msg_handler_t handler;
    int msg_id;
} MsgEntry2;

const MsgEntry2 g_dispTable[] = {
    { .msg_id = MSG_1004, .handler = &sess_handle_notify },
    { .handler = sess_handle_bye, .msg_id = MSG_1001 },
};
