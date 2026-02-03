#!/usr/bin/env python3
"""
PyReverseLink Server
C2/Edge Computing 管理中心 - 作为库使用

使用示例:
    from server import C2Server
    
    # 启动服务器
    server = C2Server(password="your_secret", port=18862)
    server.start()  # 非阻塞，在后台线程运行
    
    # 等待客户端连接...
    
    # 列出所有已连接的客户端
    clients = server.clients
    
    # 获取一个客户端的远程代理
    remote = server.get_client("client_id")
    
    # 直接调用远程 Python 函数/对象
    remote.os.getcwd()                    # 获取远程工作目录
    remote.subprocess.run(["ls", "-la"])  # 执行远程命令
    remote("import numpy; result = numpy.array([1,2,3])")  # 执行代码
"""

import logging
import ssl
import threading
import hashlib
import hmac
import time
from typing import Dict, Optional, Any, Callable, List
from pathlib import Path

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.utils.authenticators import SSLAuthenticator
from rpyc.utils.helpers import classpartial

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("C2Server")


class RemoteContext:
    """
    远程持久化执行上下文
    变量在多次调用间保持
    
    用法:
        ctx = remote.context("my_session")
        ctx("x = 1")
        ctx("y = 2")
        ctx("result = x + y")
        print(ctx["result"])  # 3
    """
    def __init__(self, client: 'RemoteClient', context_id: str = None):
        self._client = client
        self._context_id = context_id
    
    def __call__(self, code: str) -> Any:
        """在此上下文中执行代码"""
        return self._client._conn.root.execute_in_context(code, self._context_id)
    
    def __getitem__(self, name: str) -> Any:
        """获取上下文中的变量: ctx["x"]"""
        return self._client._conn.root.get_context_var(name, self._context_id)
    
    def __setitem__(self, name: str, value: Any):
        """设置上下文中的变量: ctx["x"] = 1"""
        self._client._conn.root.set_context_var(name, value, self._context_id)
    
    def vars(self) -> list:
        """列出上下文中的所有变量名"""
        return self._client._conn.root.list_context_vars(self._context_id)
    
    def clear(self):
        """清空上下文"""
        self._client._conn.root.clear_context(self._context_id)


class RemoteClient:
    """
    远程客户端代理
    提供简洁的接口调用远程 Python 功能
    
    两种执行模式:
    1. 独立模式: remote("code") - 每次独立执行，不保留状态
    2. 上下文模式: remote.ctx("code") 或 remote.context()("code") - 变量持久化
    """
    def __init__(self, client_id: str, conn: rpyc.Connection, info: dict = None):
        self._client_id = client_id
        self._conn = conn
        self._info = info or {}
        self._default_context = RemoteContext(self, None)
    
    @property
    def id(self) -> str:
        """客户端ID"""
        return self._client_id
    
    @property
    def info(self) -> dict:
        """客户端信息"""
        return self._info
    
    @property
    def connected(self) -> bool:
        """是否仍然连接"""
        return self._conn and not self._conn.closed
    
    @property
    def root(self) -> Any:
        """
        获取远程 root 对象 (SlaveService)
        可用于调用 exposed_* 方法
        """
        return self._conn.root
    
    @property
    def ctx(self) -> RemoteContext:
        """
        默认持久化上下文 (快捷访问)
        
        用法:
            remote.ctx("x = 1")
            remote.ctx("print(x)")  # 能访问到 x
            remote.ctx["x"]         # 获取变量值
        """
        return self._default_context
    
    def context(self, context_id: str = None) -> RemoteContext:
        """
        创建或获取命名上下文
        
        Args:
            context_id: 上下文ID，None 返回默认上下文
            
        用法:
            ctx = remote.context("session1")
            ctx("x = 1")
            ctx("y = x + 1")
            print(ctx["y"])  # 2
        """
        if context_id is None:
            return self._default_context
        return RemoteContext(self, context_id)
    
    def __call__(self, code: str) -> Any:
        """
        执行远程 Python 代码 (独立命名空间，不保持状态)
        
        用法:
            result = remote("1 + 1")
        """
        return self._conn.root.execute(code)
    
    def __getattr__(self, name: str):
        """
        获取远程模块
        
        用法:
            remote.os.getcwd()
            remote.subprocess.run(["ls"])
        """
        if name.startswith('_'):
            raise AttributeError(name)
        return self._conn.root.getmodule(name)
    
    def shell(self, command: str) -> dict:
        """
        执行远程 shell 命令
        
        Returns:
            {"returncode": int, "stdout": str, "stderr": str}
        """
        return self._conn.root.shell(command)
    
    def get_info(self) -> dict:
        """获取远程系统信息"""
        return self._conn.root.get_info()
    
    def ping(self) -> float:
        """Ping 客户端，返回远程时间戳"""
        return self._conn.root.ping()
    
    def eval(self, expr: str) -> Any:
        """在远程执行 Python 表达式并返回结果 (使用 builtins.eval)"""
        return self._conn.modules["builtins"].eval(expr)
    
    def exec(self, code: str) -> RemoteContext:
        """
        在默认上下文中执行代码 (变量会持久化)
        
        用法:
            remote.exec("x = 1")
            remote.exec("print(x)")  # 能访问到 x
        
        Returns:
            默认上下文对象
        """
        self._default_context(code)
        return self._default_context
    
    def __enter__(self):
        """支持 with 语句"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出 with 语句时不做任何操作 (保持连接)"""
        return False



class C2ServerService(rpyc.Service):
    """
    C2 服务端 Service - 内部使用
    """
    ALIASES = ["C2Server"]
    
    def __init__(self, password_hash: bytes, on_client_registered: Callable = None, 
                 on_client_disconnected: Callable = None):
        self._password_hash = password_hash
        self._authenticated = False
        self._client_id = None
        self._conn = None
        self._on_client_registered = on_client_registered
        self._on_client_disconnected = on_client_disconnected
    
    def on_connect(self, conn):
        self._conn = conn
        try:
            peer = conn._channel.stream.sock.getpeername()
            logger.info(f"新连接来自: {peer}")
        except:
            pass
    
    def on_disconnect(self, conn):
        if self._client_id and self._on_client_disconnected:
            self._on_client_disconnected(self._client_id)
        logger.info(f"连接断开: {self._client_id or 'unauthenticated'}")
    
    def exposed_authenticate(self, password: str, client_info: dict = None) -> dict:
        """客户端认证接口"""
        password_hash = hashlib.sha256(password.encode()).digest()
        if not hmac.compare_digest(password_hash, self._password_hash):
            logger.warning(f"认证失败: 密码错误")
            return {"success": False, "client_id": None, "message": "认证失败：密码错误"}
        
        try:
            peer = self._conn._channel.stream.sock.getpeername()
        except:
            peer = ("unknown", 0)
        
        self._client_id = f"{client_info.get('hostname', 'unknown')}@{peer[0]}:{peer[1]}"
        self._authenticated = True
        
        info = client_info or {}
        info["peer"] = peer
        
        if self._on_client_registered:
            self._on_client_registered(self._client_id, self._conn, info)
        
        logger.info(f"认证成功: {self._client_id}")
        return {"success": True, "client_id": self._client_id, "message": "认证成功"}


class C2Server:
    """
    C2 控制服务器
    
    用法:
        server = C2Server(password="secret", port=18862)
        server.start()
        
        # 获取客户端
        for client_id in server.clients:
            remote = server.get_client(client_id)
            print(remote.os.getcwd())
    """
    
    def __init__(self, password: str, 
                 host: str = "0.0.0.0", 
                 port: int = 18862,
                 certfile: str = None,
                 keyfile: str = None,
                 ca_certfile: str = None):
        """
        初始化 C2 服务器
        
        Args:
            password: 客户端认证密码
            host: 监听地址
            port: 监听端口
            certfile: SSL 证书文件 (必需)
            keyfile: SSL 私钥文件 (必需)
            ca_certfile: CA 证书 (可选，用于双向认证)
        """
        self._password_hash = hashlib.sha256(password.encode()).digest()
        self._host = host
        self._port = port
        self._certfile = certfile
        self._keyfile = keyfile
        self._ca_certfile = ca_certfile
        
        self._clients: Dict[str, RemoteClient] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadedServer] = None
        self._server_thread: Optional[threading.Thread] = None
        
        # 回调函数
        self.on_client_connect: Optional[Callable[[RemoteClient], None]] = None
        self.on_client_disconnect: Optional[Callable[[str], None]] = None
    
    def _register_client(self, client_id: str, conn: rpyc.Connection, info: dict):
        """内部回调: 注册客户端"""
        remote = RemoteClient(client_id, conn, info)
        with self._lock:
            self._clients[client_id] = remote
        
        if self.on_client_connect:
            try:
                self.on_client_connect(remote)
            except Exception as e:
                logger.error(f"on_client_connect 回调错误: {e}")
    
    def _unregister_client(self, client_id: str):
        """内部回调: 注销客户端"""
        with self._lock:
            if client_id in self._clients:
                del self._clients[client_id]
        
        if self.on_client_disconnect:
            try:
                self.on_client_disconnect(client_id)
            except Exception as e:
                logger.error(f"on_client_disconnect 回调错误: {e}")
    
    @property
    def clients(self) -> List[str]:
        """获取所有已连接客户端ID列表"""
        with self._lock:
            return list(self._clients.keys())
    
    def get_client(self, client_id: str) -> Optional[RemoteClient]:
        """
        获取客户端代理对象
        
        Args:
            client_id: 客户端ID
            
        Returns:
            RemoteClient 代理对象，可直接调用远程函数
        """
        with self._lock:
            return self._clients.get(client_id)
    
    def get_all_clients(self) -> Dict[str, RemoteClient]:
        """获取所有客户端代理对象"""
        with self._lock:
            return dict(self._clients)
    
    def wait_for_client(self, timeout: float = None) -> Optional[RemoteClient]:
        """
        等待第一个客户端连接
        
        Args:
            timeout: 超时秒数，None 表示无限等待
            
        Returns:
            第一个连接的客户端，超时返回 None
        """
        start = time.time()
        while True:
            with self._lock:
                if self._clients:
                    return next(iter(self._clients.values()))
            
            if timeout and (time.time() - start) >= timeout:
                return None
            
            time.sleep(0.1)
    
    def start(self, blocking: bool = False):
        """
        启动服务器
        
        Args:
            blocking: 是否阻塞当前线程
        """
        if not self._certfile or not self._keyfile:
            raise ValueError("必须提供 SSL 证书 (certfile) 和私钥 (keyfile)")
        
        if not Path(self._certfile).exists():
            raise FileNotFoundError(f"证书文件不存在: {self._certfile}")
        if not Path(self._keyfile).exists():
            raise FileNotFoundError(f"私钥文件不存在: {self._keyfile}")
        
        # 创建 SSL 认证器
        authenticator = SSLAuthenticator(
            keyfile=self._keyfile,
            certfile=self._certfile,
            ca_certs=self._ca_certfile,
            cert_reqs=ssl.CERT_NONE
        )
        
        # 创建服务工厂
        service = classpartial(
            C2ServerService,
            self._password_hash,
            on_client_registered=self._register_client,
            on_client_disconnected=self._unregister_client
        )
        
        # RPyC 配置
        protocol_config = {
            "allow_all_attrs": True,
            "allow_public_attrs": True,
            "allow_pickle": True,
            "sync_request_timeout": 30,
        }
        
        self._server = ThreadedServer(
            service,
            hostname=self._host,
            port=self._port,
            authenticator=authenticator,
            protocol_config=protocol_config
        )
        
        logger.info(f"C2 服务器启动于 {self._host}:{self._port} (SSL)")
        
        if blocking:
            self._server.start()
        else:
            self._server_thread = threading.Thread(target=self._server.start, daemon=True)
            self._server_thread.start()
    
    def stop(self):
        """停止服务器"""
        if self._server:
            self._server.close()
            logger.info("服务器已停止")
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, *args):
        self.stop()


# 便捷函数: 生成自签名证书
def generate_self_signed_cert(hostname: str = "localhost", 
                               output_dir: str = "certs") -> tuple:
    """
    生成自签名证书 (需要 openssl 命令)
    
    Args:
        hostname: 服务器域名/IP
        output_dir: 输出目录
        
    Returns:
        (certfile, keyfile, ca_certfile) 路径元组
    """
    import subprocess
    import os
    
    out = Path(output_dir)
    out.mkdir(exist_ok=True)
    
    ca_key = out / "ca.key"
    ca_crt = out / "ca.crt"
    srv_key = out / "server.key"
    srv_crt = out / "server.crt"
    
    # 生成 CA 密钥
    logger.info("生成 CA 密钥...")
    result = subprocess.run([
        "openssl", "genrsa", "-out", str(ca_key), "4096"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"生成 CA 密钥失败: {result.stderr}")
    
    # 创建 CA 配置文件 (避免 Windows 下 -subj 参数问题)
    ca_conf = out / "ca.conf"
    ca_conf.write_text("""[req]
distinguished_name = req_distinguished_name
prompt = no

[req_distinguished_name]
CN = PyReverseLink CA
""")
    
    logger.info("生成 CA 证书...")
    result = subprocess.run([
        "openssl", "req", "-new", "-x509", "-days", "365",
        "-key", str(ca_key), "-out", str(ca_crt),
        "-config", str(ca_conf)
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"生成 CA 证书失败: {result.stderr}")
    
    ca_conf.unlink()  # 清理配置文件
    
    # 生成服务器密钥
    logger.info("生成服务器密钥...")
    result = subprocess.run([
        "openssl", "genrsa", "-out", str(srv_key), "4096"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"生成服务器密钥失败: {result.stderr}")
    
    # 创建服务器配置文件
    srv_conf = out / "server.conf"
    srv_conf.write_text(f"""[req]
distinguished_name = req_distinguished_name
prompt = no

[req_distinguished_name]
CN = {hostname}
""")
    
    # 生成服务器 CSR
    logger.info("生成服务器 CSR...")
    csr_path = out / "server.csr"
    result = subprocess.run([
        "openssl", "req", "-new", "-key", str(srv_key),
        "-out", str(csr_path), "-config", str(srv_conf)
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"生成服务器 CSR 失败: {result.stderr}")
    
    srv_conf.unlink()  # 清理配置文件
    
    # 创建扩展配置
    ext_path = out / "server.ext"
    ext_path.write_text(f"""authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = {hostname}
DNS.2 = localhost
IP.1 = 127.0.0.1
""")
    
    # 签发服务器证书
    logger.info("签发服务器证书...")
    result = subprocess.run([
        "openssl", "x509", "-req", "-in", str(csr_path),
        "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(srv_crt),
        "-days", "365", "-extfile", str(ext_path)
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"签发服务器证书失败: {result.stderr}")
    
    # 清理临时文件
    csr_path.unlink()
    ext_path.unlink()
    (out / "ca.srl").unlink(missing_ok=True)
    
    logger.info(f"✓ 证书已生成到 {output_dir}/")
    return str(srv_crt), str(srv_key), str(ca_crt)


# 命令行入口 (可选)
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="PyReverseLink C2 服务器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18862)
    parser.add_argument("--password", required=True)
    parser.add_argument("--cert", default="certs/server.crt")
    parser.add_argument("--key", default="certs/server.key")
    parser.add_argument("--gen-cert", action="store_true", help="生成自签名证书")
    
    args = parser.parse_args()
    
    if args.gen_cert:
        cert, key, ca = generate_self_signed_cert()
        print(f"证书已生成: {cert}, {key}")
        print(f"CA证书 (分发给客户端): {ca}")
        import sys
        sys.exit(0)
    
    server = C2Server(
        password=args.password,
        host=args.host,
        port=args.port,
        certfile=args.cert,
        keyfile=args.key
    )
    
    def on_connect(client):
        print(f"✓ 客户端已连接: {client.id}")
    
    server.on_client_connect = on_connect
    server.start(blocking=True)
