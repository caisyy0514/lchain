import os

# macOS 上开了 Clash 等系统代理时，httpx 会把 localhost 请求也转发给代理导致 502，
# 在任何 HTTP 客户端创建之前强制本地地址直连。
for _k in ("NO_PROXY", "no_proxy"):
    _cur = [x.strip() for x in os.environ.get(_k, "").split(",") if x.strip()]
    os.environ[_k] = ",".join(dict.fromkeys(_cur + ["localhost", "127.0.0.1", "0.0.0.0", "::1"]))
