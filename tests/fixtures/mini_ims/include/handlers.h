#ifndef HANDLERS_H
#define HANDLERS_H

typedef void (*msg_handler_t)(int msg);

void sess_handle_invite(int msg);
void sess_handle_bye(int msg);
void sess_handle_refer(int msg);
void sess_handle_notify(int msg);
void oam_handle_stats(int msg);

/* 注册式分发 API（registry fixture；实现由运行期提供，此处仅需声明） */
void MsgReg(int msg_id, msg_handler_t fn);

#endif /* HANDLERS_H */
