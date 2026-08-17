/* g_sysConfig 的跨文件使用者：一个读者 + 一个复合赋值写者（设计文档 §5.5）。 */

extern int g_sysConfig;

int cfg_is_set(void) { return g_sysConfig > 0; }

void cfg_boost(void) { g_sysConfig += 3; }
