#!/usr/bin/env python3
"""
PyReverseLink Client
C2/Edge Computing 反向连接客户端 - 主动连接 Server 并暴露完整 Python 访问权限

使用方法:
    python main.py --server your-server.com --port 18862 --password YOUR_SECRET
    
    # 指定 CA 证书 (用于验证服务器身份):
    python main.py --server your-server.com --password YOUR_SECRET --ca-cert /path/to/ca.crt
"""

import argparse
import logging
import ssl
import socket
import time
import os
import platform
import threading
from pathlib import Path
from typing import Dict, Any, Optional

import rpyc
from rpyc.core.service import SlaveService

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("C2Client")


class C2SlaveService(SlaveService):
    """
    扩展的 SlaveService
    除了标准的 SlaveService 功能外，还提供一些便捷方法
    支持持久化上下文
    """
    
    # 类级别的持久化上下文存储
    _contexts: Dict[str, dict] = {}
    _default_context: dict = {}
    
    def exposed_get_info(self) -> dict:
        """返回客户端系统信息"""
        return {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cwd": os.getcwd(),
            "user": os.getenv("USER") or os.getenv("USERNAME"),
        }
    
    def exposed_execute(self, code: str) -> Any:
        """
        执行 Python 代码 (独立命名空间，不保持状态)
        
        Args:
            code: Python 代码字符串
            
        Returns:
            执行结果 (最后一个表达式的值)
        """
        exec_globals = {"__builtins__": __builtins__}
        exec_locals = {}
        
        # 先尝试作为表达式求值
        try:
            result = eval(code, exec_globals, exec_locals)
            return result
        except SyntaxError:
            # 如果不是表达式，则作为语句执行
            exec(code, exec_globals, exec_locals)
            return exec_locals.get("result", None)
    
    def exposed_execute_in_context(self, code: str, context_id: str = None) -> Any:
        """
        在持久化上下文中执行代码 (变量会保持)
        
        Args:
            code: Python 代码字符串
            context_id: 上下文ID，None 使用默认上下文
            
        Returns:
            执行结果
        """
        if context_id is None:
            ns = self._default_context
        else:
            if context_id not in self._contexts:
                self._contexts[context_id] = {"__builtins__": __builtins__}
            ns = self._contexts[context_id]
        
        if "__builtins__" not in ns:
            ns["__builtins__"] = __builtins__
        
        # 先尝试作为表达式求值
        try:
            result = eval(code, ns, ns)
            return result
        except SyntaxError:
            # 如果不是表达式，则作为语句执行
            exec(code, ns, ns)
            return ns.get("result", None)
    
    def exposed_get_context_var(self, name: str, context_id: str = None) -> Any:
        """获取上下文中的变量值"""
        if context_id is None:
            ns = self._default_context
        else:
            ns = self._contexts.get(context_id, {})
        return ns.get(name)
    
    def exposed_set_context_var(self, name: str, value: Any, context_id: str = None):
        """设置上下文中的变量"""
        if context_id is None:
            ns = self._default_context
        else:
            if context_id not in self._contexts:
                self._contexts[context_id] = {"__builtins__": __builtins__}
            ns = self._contexts[context_id]
        ns[name] = value
    
    def exposed_list_context_vars(self, context_id: str = None) -> list:
        """列出上下文中的所有变量名"""
        if context_id is None:
            ns = self._default_context
        else:
            ns = self._contexts.get(context_id, {})
        return [k for k in ns.keys() if not k.startswith('_')]
    
    def exposed_clear_context(self, context_id: str = None):
        """清空上下文"""
        if context_id is None:
            self._default_context.clear()
        elif context_id in self._contexts:
            del self._contexts[context_id]
    
    def exposed_shell(self, command: str) -> dict:
        """
        执行 shell 命令
        
        Args:
            command: Shell 命令字符串
            
        Returns:
            {"returncode": int, "stdout": str, "stderr": str}
        """
        import subprocess
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    
    def exposed_ping(self) -> float:
        """心跳响应，返回当前时间戳"""
        return time.time()


def get_client_info() -> dict:
    """获取客户端信息用于注册"""
    return {
        "hostname": platform.node(),
        "platform": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }


def create_ssl_context(ca_certfile: str = None, verify: bool = True) -> ssl.SSLContext:
    """
    创建 SSL 上下文
    
    Args:
        ca_certfile: CA 证书文件 (用于验证服务器)
        verify: 是否验证服务器证书
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    
    if ca_certfile and Path(ca_certfile).exists():
        context.load_verify_locations(ca_certfile)
        context.verify_mode = ssl.CERT_REQUIRED
    elif verify:
        context.load_default_certs()
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    
    return context


class HeartbeatThread(threading.Thread):
    """心跳保活线程"""
    
    def __init__(self, conn: rpyc.Connection, interval: int = 30):
        super().__init__(daemon=True)
        self._conn = conn
        self._interval = interval
        self._stop_event = threading.Event()
    
    def run(self):
        """定期发送心跳"""
        while not self._stop_event.is_set():
            try:
                if self._conn.closed:
                    break
                # 发送一个简单的 ping 请求保持连接活跃
                self._conn.ping()
                logger.debug("❤️ 心跳发送成功")
            except Exception as e:
                logger.warning(f"心跳失败: {e}")
                break
            
            self._stop_event.wait(self._interval)
    
    def stop(self):
        """停止心跳线程"""
        self._stop_event.set()


def connect_and_authenticate(host: str, port: int, password: str,
                              ssl_context: ssl.SSLContext, 
                              server_hostname: str = None) -> rpyc.Connection:
    """
    连接到服务器并进行认证
    
    Returns:
        认证成功的连接对象
        
    Raises:
        ConnectionError: 连接或认证失败
    """
    config = {
        "allow_all_attrs": True,
        "allow_public_attrs": True,
        "allow_pickle": True,
        "sync_request_timeout": 30,
    }
    
    logger.info(f"正在连接到 {host}:{port}...")
    
    # 创建原始 socket 连接
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    
    # 使用 SSL 上下文包装 socket
    ssl_sock = ssl_context.wrap_socket(sock, server_hostname=server_hostname or host)
    
    logger.info("SSL 握手完成，正在建立 RPyC 连接...")
    
    # 使用已包装的 SSL socket 创建 RPyC 连接
    from rpyc.utils.factory import connect_stream
    from rpyc.core.stream import SocketStream
    
    stream = SocketStream(ssl_sock)
    conn = connect_stream(stream, service=C2SlaveService, config=config)
    
    logger.info("SSL 连接已建立，正在认证...")
    
    client_info = get_client_info()
    result = conn.root.authenticate(password, client_info)
    
    if not result["success"]:
        conn.close()
        raise ConnectionError(f"认证失败: {result['message']}")
    
    logger.info(f"认证成功! 客户端ID: {result['client_id']}")
    return conn


def run_client(host: str, port: int, password: str,
               ca_certfile: str = None, no_verify: bool = False,
               reconnect: bool = True, reconnect_interval: int = 5,
               heartbeat_interval: int = 30):
    """
    运行客户端主循环
    
    Args:
        host: 服务器地址
        port: 服务器端口
        password: 认证密码
        ca_certfile: CA 证书文件
        no_verify: 是否跳过证书验证
        reconnect: 断线后是否自动重连
        reconnect_interval: 重连间隔 (秒)
        heartbeat_interval: 心跳间隔 (秒)
    """
    ssl_context = create_ssl_context(ca_certfile, verify=not no_verify)
    
    while True:
        heartbeat: Optional[HeartbeatThread] = None
        
        try:
            conn = connect_and_authenticate(
                host=host,
                port=port,
                password=password,
                ssl_context=ssl_context,
                server_hostname=host
            )
            
            # 启动心跳线程
            heartbeat = HeartbeatThread(conn, interval=heartbeat_interval)
            heartbeat.start()
            logger.info(f"已连接，心跳间隔: {heartbeat_interval}秒")
            
            # 保持连接活跃并处理请求
            # 使用 serve_all() 来处理来自服务器的 RPC 请求
            try:
                conn.serve_all()
            except (KeyboardInterrupt, EOFError):
                logger.info("连接主动关闭")
            
            logger.warning("连接已断开")
            
        except ConnectionRefusedError:
            logger.error(f"无法连接到 {host}:{port}，服务器可能未运行")
        except ConnectionError as e:
            logger.error(str(e))
        except ssl.SSLError as e:
            logger.error(f"SSL 错误: {e}")
        except KeyboardInterrupt:
            logger.info("用户中断连接")
            if not reconnect:
                break
            logger.info(f"{reconnect_interval} 秒后重连...")
            time.sleep(reconnect_interval)
            continue
        except Exception as e:
            logger.exception(f"未预期的错误: {e}")
        finally:
            if heartbeat:
                heartbeat.stop()
        
        if not reconnect:
            break
        
        logger.info(f"{reconnect_interval} 秒后重连...")
        time.sleep(reconnect_interval)


def main():
    parser = argparse.ArgumentParser(description="PyReverseLink C2 客户端")
    parser.add_argument("--server", required=True, help="服务器地址")
    parser.add_argument("--port", type=int, default=18862, help="服务器端口")
    parser.add_argument("--password", required=True, help="认证密码")
    parser.add_argument("--ca-cert", default=None, help="CA 证书 (用于验证服务器)")
    parser.add_argument("--no-verify", action="store_true", 
                        help="不验证服务器证书 (仅用于测试!)")
    parser.add_argument("--no-reconnect", action="store_true",
                        help="断线后不自动重连")
    parser.add_argument("--reconnect-interval", type=int, default=5,
                        help="重连间隔 (秒)")
    parser.add_argument("--heartbeat-interval", type=int, default=30,
                        help="心跳间隔 (秒)")
    
    args = parser.parse_args()
    
    if args.no_verify:
        logger.warning("⚠️  警告: 已禁用证书验证，仅用于测试环境!")
    
    run_client(
        host=args.server,
        port=args.port,
        password=args.password,
        ca_certfile=args.ca_cert,
        no_verify=args.no_verify,
        reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
        heartbeat_interval=args.heartbeat_interval
    )


if __name__ == "__main__":
    main()
