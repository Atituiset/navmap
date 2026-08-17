#ifndef OAM_TABLE_H
#define OAM_TABLE_H

#include "msg_ids.h"
#include "handlers.h"

typedef struct {
    int msg_id;
    msg_handler_t handler;
} OamEntry;

/* 头文件中的表：无自身 TU 条目，需借包含它的 .c TU 参数解析（设计 §5.2 末尾） */
static const OamEntry g_oamTable[] = {
    { MSG_1002, oam_handle_stats },
};

#endif /* OAM_TABLE_H */
