"""从官方示例启动本部署支持的 SenseVoice CPU 服务。"""

import server

if __name__ == "__main__":
    server.MODEL_CONFIGS = {"sensevoice": server.MODEL_CONFIGS["sensevoice"]}
    server.main()
