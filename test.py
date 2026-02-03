import time
from server.main import C2Server

# Arguments: password, host, port, certfile, keyfile
server = C2Server(
    password="114514",
    host="127.0.0.1",
    port=18862,
    certfile="server/certs/server.crt",
    keyfile="server/certs/server.key"
)

server.start()

print("等待客户端连接...")

while True:
    if server.clients:
        for client_id in server.clients:
            with server.get_client(client_id) as remote:
                # 获取远程 camoufox 模块中的 Camoufox 类
                camoufox_module = remote.camoufox.sync_api
                Camoufox = camoufox_module.Camoufox
                
                with Camoufox() as browser:
                    page = browser.new_page()
                    page.goto("https://www.baidu.com")
                    time.sleep(10)
    time.sleep(2)
