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

/* pjsip 实际形态：mod_evsub 非 const、按位初始化（成员顺序 = 声明顺序）。
 * 注：libclang 20 对"全指定初始化器 + 全 fnptr 成员"的这种组合不生成
 * init cursor（CI 实测），按位形态稳定且更贴近 pjsip 真实代码。 */
static ModDesc g_modDesc = {
    "ims-mod",
    sess_handle_invite,
    sess_handle_bye,
    NULL,
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
