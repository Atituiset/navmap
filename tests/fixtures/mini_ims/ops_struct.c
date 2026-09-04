/* 单 ops-struct 分发形态（curl Curl_protocol 同构）：
 * 非数组结构体、多个函数指针成员、按位 + 指定初始化器混合填充、ZERO_NULL 占位。
 */

typedef struct {
    int (*setup_connection)(int);
    int (*do_it)(int);
    int (*done)(int);
    int (*connect_it)(int);
    int (*disconnect)(int);
} OpsProto;

int ops_setup_full(int);
int ops_do_full(int);
int ops_connect_full(int);
int ops_disconnect_full(int);
int ops_do_designated(int);
int ops_connect_designated(int);

#define ZERO_NULL ((void *)0)

/* 按位初始化：成员与声明顺序对齐，ZERO_NULL 占位 */
const OpsProto g_opsProtoFull = {
    ops_setup_full,
    ops_do_full,
    ZERO_NULL,
    ops_connect_full,
    ops_disconnect_full,
};

/* 指定初始化器：乱序、只填部分成员 */
static OpsProto g_opsProtoDesignated = {
    .do_it = ops_do_designated,
    .connect_it = ops_connect_designated,
};
