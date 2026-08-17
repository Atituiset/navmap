#include "msg_ids.h"
#include "handlers.h"

typedef struct {
    int msg_id;
    msg_handler_t handler;
} MsgEntry;

/* 普通宏表：含 #ifdef 条件表项与 (cast)fn 强转 handler */
static const MsgEntry g_msgTable[] = {
    { MSG_1001, sess_handle_invite },
#ifdef FEATURE_IMS
    { MSG_1002, sess_handle_bye },
#endif
    { MSG_1003, (msg_handler_t)sess_handle_refer },
};
