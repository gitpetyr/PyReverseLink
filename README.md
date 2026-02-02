# PyReverseLink

C2/边缘计算反向连接框架 - 使用 RPyC 实现安全的远程调用

## 快速开始

### 1. 生成 SSL 证书

```python
from server.main import generate_self_signed_cert

# 生成证书到 certs/ 目录
cert, key, ca = generate_self_signed_cert("your-server.com")
# 将 ca.crt 分发给客户端
```

### 2. 服务端使用

```python
from server.main import C2Server

# 创建服务器
server = C2Server(
    password="your_secret_password",
    port=18862,
    certfile="certs/server.crt",
    keyfile="certs/server.key"
)

# 设置回调
server.on_client_connect = lambda c: print(f"客户端连接: {c.id}")

# 启动 (非阻塞)
server.start()

# 等待客户端连接
client = server.wait_for_client(timeout=60)

# 直接调用远程 Python!
print(client.os.getcwd())                    # 获取远程工作目录
print(client.platform.node())                # 获取远程主机名
result = client.subprocess.run(["ls", "-la"], capture_output=True)

# 执行任意代码
client("import numpy as np; result = np.array([1,2,3])")
```

### 3. 客户端使用

```bash
# 在远端机器运行
python client/main.py \
    --server your-server.com \
    --port 18862 \
    --password your_secret_password \
    --ca-cert certs/ca.crt
```

## 核心 API

### `C2Server`

| 方法 | 说明 |
|------|------|
| `start(blocking=False)` | 启动服务器 |
| `stop()` | 停止服务器 |
| `clients` | 已连接客户端ID列表 |
| `get_client(id)` | 获取客户端代理 |
| `wait_for_client(timeout)` | 等待客户端连接 |

### `RemoteClient`

| 用法 | 说明 |
|------|------|
| `client.os` / `client.sys` | 获取远程模块 |
| `client("code")` | 执行远程代码 (独立命名空间) |
| `client.shell("ls")` | 执行 shell 命令 |
| `client.get_info()` | 获取系统信息 |
| `client.ctx("code")` | 在默认上下文执行 (变量持久化) |
| `client.ctx["x"]` | 获取/设置上下文变量 |
| `client.context("name")` | 创建命名上下文 |

### 持久化上下文示例

```python
# 方式1: 使用默认上下文
remote.ctx("x = 1")
remote.ctx("y = 2")
remote.ctx("result = x + y")
print(remote.ctx["result"])  # 3

# 方式2: 创建命名上下文
session = remote.context("my_session")
session("import numpy as np")
session("data = np.array([1,2,3])")
print(session["data"])
```

## 心跳保活

客户端内置心跳机制，默认每 30 秒发送一次心跳：

```bash
python client/main.py --server host --password secret --heartbeat-interval 60
```

## 安全说明

⚠️ **警告**: 此框架授予服务器对客户端的**完全控制权限**，仅用于：
- 内部边缘计算管理
- 合法的 C2 应用场景
- 受信任网络环境