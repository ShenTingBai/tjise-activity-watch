# deploy/legacy/

已停用的部署脚本，保留备查：

| 脚本 | 原用途 | 为什么停用 |
|------|--------|-----------|
| `install_nginx_route.py` | 在旧机 nginx 上加 `<你的域名>/0f4b…` → `127.0.0.1:18080` 的 HTTPS 路由 | 旧机已清理；新机没有对外 443 |
| `install_nginx_http_route.py` | 同上，HTTP 版（用于证书签发前的跳转） | 同上 |

手机一键触发的现状见 `docs/OPERATIONS.md` 第 9 节：链接已失效，恢复需要在新机部署 HTTPS 反代。
