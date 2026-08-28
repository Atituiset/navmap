/* 多维裸函数指针数组：msg 二维分发表（call hierarchy 盲区形态）。 */

typedef int (*fp_handler_t)(int);

static int h_a0(int x) { return x; }
static int h_a1(int x) { return x + 1; }
static int h_b0(int x) { return x + 10; }
static int h_b1(int x) { return x + 11; }

/* [group][msg] 二维表 */
static const fp_handler_t FP_MATRIX_TBL[2][2] = {
    { h_a0, h_a1 },
    { h_b0, h_b1 },
};
