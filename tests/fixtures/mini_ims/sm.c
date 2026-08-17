/* 状态机表 fixture：普通行 / #ifdef 行 / 指定初始化器纯迁移行 */
#include "handlers.h"
#include "sm_defs.h"

typedef struct {
    int state;
    int event;
    msg_handler_t handler;
    int next_state;
} sm_trans_t;

static const sm_trans_t g_smTable[] = {
    { ST_IDLE, EV_INVITE, sess_handle_invite, ST_RING },
#ifdef FEATURE_IMS
    { ST_RING, EV_CANCEL, sess_handle_bye, ST_IDLE },
#endif
    { .event = EV_BYE, .state = ST_TALK, .next_state = ST_IDLE },
};
