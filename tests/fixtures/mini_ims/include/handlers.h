#ifndef HANDLERS_H
#define HANDLERS_H

typedef void (*msg_handler_t)(int msg);

void sess_handle_invite(int msg);
void sess_handle_bye(int msg);
void sess_handle_refer(int msg);
void sess_handle_notify(int msg);
void oam_handle_stats(int msg);

#endif /* HANDLERS_H */
