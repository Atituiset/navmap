#include "msg_ids.h"
#include "handlers.h"

typedef struct {
    int msg_id;
    msg_handler_t handler;
} XMsgEntry;

/* X-Macro 表：元素 location 应指向 msg.def */
const XMsgEntry g_xmsgTable[] = {
#define X(id, fn) { id, fn },
#include "msg.def"
#undef X
};
