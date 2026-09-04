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

/* 结构体注册形态（pjsip/freeDiameter 同构）：
   注册 API 收 &mod / &mod.member，回调在结构体初始化器的 fnptr 成员里。 */
typedef struct {
    const char *name;
    void (*on_rx_request)(int);
    void (*on_rx_response)(int);
    void (*on_tx_request)(int);
} ModDesc;

typedef struct {
    int priority;
    ModDesc mod;   /* &m.mod.member 嵌套形态 */
} ModWrap;

/* pjsip 实际形态：mod_evsub 非 const。const + 全常量初始化在 libclang 20
 * 下不生成 init cursor（已知限制，见 README）。 */
static ModDesc g_modDesc = {
    .name = "ims-mod",
    .on_rx_request = sess_handle_invite,
    .on_rx_response = sess_handle_bye,
    .on_tx_request = NULL,
};

static const ModWrap g_modWrap = {
    .priority = 1,
    .mod = {
        .name = "wrap",
        .on_rx_request = sess_handle_refer,
        .on_rx_response = sess_handle_notify,
    },
};

void ModReg(const ModDesc *desc);
void WrapReg(const ModWrap *wrap);

void reg_struct_init(void)
{
    ModReg(&g_modDesc);
    WrapReg(&g_modWrap.mod);
}
