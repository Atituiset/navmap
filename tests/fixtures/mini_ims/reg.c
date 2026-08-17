#include "msg_ids.h"
#include "handlers.h"

/* 注册式分发（设计文档 §5.3 fixture）：运行期初始化函数中逐条注册。
   覆盖三种形态：普通注册 / #ifdef 包裹 / (cast)fn 强转 handler。 */
void reg_init(void)
{
    MsgReg(MSG_1001, sess_handle_invite);
#ifdef FEATURE_IMS
    MsgReg(MSG_1002, sess_handle_bye);
#endif
    MsgReg(MSG_1003, (msg_handler_t)sess_handle_refer);
}
