/* 裸函数指针数组（无结构体包装）：msg_id = 数组下标。
   对应 dispatch.py 的 bare_fnptr 分支。 */

typedef int (*fp_handler_t)(int);

static int alpha_handler(int x) { return x + 1; }
static int beta_handler(int x) { return x + 2; }

static const fp_handler_t FP_HANDLER_TBL[4] = {
    alpha_handler,
    beta_handler,
    alpha_handler,
    beta_handler,
};
