/* 全局变量读写 fixture（设计文档 §5.5）：g_sysConfig 的定义与普通写者。 */

int g_sysConfig = 0;

void cfg_apply(int v) { g_sysConfig = v; }

void cfg_bump(void) { g_sysConfig++; }
