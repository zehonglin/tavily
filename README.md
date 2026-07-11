# Tavily Key 池网关 (Tavily Key-Pool Gateway)

一个轻量 HTTP 网关,在 `tvly` CLI 前面加了一层多 API key 池,每次调用自动
路由到剩余额度最多的那把 key。各 agent(openclaw、codex 等)依旧像以前一样
调 `tvly`,key 池集中在网关机器上,API key 永不离开网关主机。

## 架构

```
[agent 机器]                          [网关主机: openclaw 公网 ECS]
  SKILL.md  ->  `tvly search ...`       tavily-gateway :18790 (systemd)
                     |  (thin client)        |- 选剩余额度最多的 key
                     v                       |- 注入 TAVILY_API_KEY
                 POST /exec  ---------------->|- spawn `tvly <cmd> <args>`
                     |                       |- 把 stdout 流式回传
              tvly stdout 给 agent  <--------
```

- **网关** (`gateway/tavily_gateway.py`,仅用 Python 标准库):接收
  `POST /exec {cmd, args, stdin}`,选出剩余额度最多的 key(缓存 5 分钟),
  以该 key 为 `TAVILY_API_KEY` spawn 真正的 `tvly` CLI,把 CLI 的 stdout
  作为 HTTP 响应体流式回传。
- **Thin client** (`client/tvly`,仅用 Python 标准库):伪装成每台 agent
  机器上的 `tvly` 命令。解析 argv,按需读取 stdin,POST 到网关,把响应
  流式写到 stdout。无 key、无状态。
- **Skill**:不变。官方 skill(`SKILL.md`)放在每台 agent 上,agent 读它来决定何时调 `tvly`。

## 部署拓扑速查

| 组件 | 放哪 | 备注 |
|---|---|---|
| `gateway/tavily_gateway.py` + systemd unit | 网关主机 | 守护进程 |
| `keys.json`(密钥池)、真正的 `tvly` CLI | 网关主机 | **绝不出网关** |
| `client/tvly`(Windows 另加 `.cmd`/`.ps1`) | 每台 agent | 瘦客户端,无 key |
| `TAVILY_GATEWAY_URL` / `TAVILY_GATEWAY_TOKEN` | 每台 agent 的环境变量 | 指向网关 |
| 官方 skill(`SKILL.md`) | 每台 agent | agent 读的指令 |

原则:**密钥和真正的 tvly 只在网关主机**;agent 只有转发脚本。加/换 agent 机器,
只需拷瘦客户端 + 配两个环境变量,不涉及任何密钥分发。

## 为什么 spawn CLI(而不是直连 SDK / 官方 API)

CLI 已经封装了我们希望对 agent 保持完全一致的全部行为:参数解析、`--json`
输出格式、`tvly research` 背后的异步状态机。通过 spawn CLI,网关只用约
200 行代码就能搞定,且 agent 看到的行为与本地调用逐字节一致。当 tavily
发布新版 CLI 时,你只需在网关主机上升级 `tvly`,网关代码、thin client、
agent skill 都不需要改动。

## 部署网关

服务以专用非特权用户 `tavily` 运行(不再用 root),API key 文件和 spawn 出的
子进程都不再拥有 root 权限。

1. 建用户、建目录、拷代码:
   ```
   sudo useradd -r -s /usr/sbin/nologin tavily
   sudo mkdir -p /etc/tavily /var/lib/tavily /opt/tavily-gateway
   sudo cp gateway/tavily_gateway.py /opt/tavily-gateway/
   sudo chown -R tavily:tavily /var/lib/tavily
   ```
2. 确保 **tavily 用户**能在 PATH 中找到 `tvly`。推荐装到 `/usr/local/bin`
   (例如普通用户 `pipx install tavly-cli` 后把 wrapper 拷进 `/usr/local/bin`),
   或者在 service 里把 `TVLY_BIN` 指向绝对路径。
3. 把 key 放进 `/etc/tavily/keys.json`(JSON 字符串数组),权限收紧:
   ```
   sudo install -m 640 -o root -g tavily keys.json /etc/tavily/keys.json
   ```
4. 生成 token:`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`。
5. 把 `deploy/tavily-gateway.service` 拷到 `/etc/systemd/system/`,把
   `TAVILY_GATEWAY_TOKEN` 那一行改成第 4 步生成的值。
6. 启用并启动:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable --now tavily-gateway
   curl localhost:18790/healthz   # 应返回 {"ok": true, "keys": N}
   ```

## 在 agent 机器上安装 thin client

Linux:
```
sudo cp client/tvly /usr/local/bin/tvly
sudo chmod +x /usr/local/bin/tvly
# 指向网关(写入 /etc/profile.d 或对应 agent 服务的环境变量)
echo 'export TAVILY_GATEWAY_URL=http://网关主机:18790' | sudo tee /etc/profile.d/tavily.sh
echo 'export TAVILY_GATEWAY_TOKEN=你的TOKEN'            | sudo tee -a /etc/profile.d/tavly.sh
```

Windows(codex):Windows 不认 Linux 的 shebang,所以不能直接用 `client\tvly`
这个文件当命令,需要走包装器。仓库已带两个包装器,二选一:

- **`client\tvly.cmd`** —— cmd / 双击 / 传统 Windows 终端用
- **`client\tvly.ps1`** —— PowerShell 用(codex 在 Windows 上的 Bash 工具
  如果实际是 PowerShell,优先用这个)

做法:把 `tvly.cmd`(或 `tvly.ps1`)和 `tvly` 两个文件一起放到 PATH 上的
某个目录(例如 `C:\Users\你\bin\`),然后在用户环境变量里设置:

```
TAVILY_GATEWAY_URL=http://网关主机:18790
TAVILY_GATEWAY_TOKEN=你的TOKEN
TVLY_CLIENT_SCRIPT=C:\Users\你\bin\tvly     # 指向那个无扩展名的 python 脚本
```

如果 `tvly.cmd`/`tvly.ps1` 和 `tvly` 放在同一目录,`TVLY_CLIENT_SCRIPT`
可以不设(包装器会自动找同目录的 `tvly`)。

**注意:** Windows 上 stdin 默认是文本流,管道输入时 `\r\n` 会被转成 `\n`。
对搜索 query 没影响,但若管道传含 `\r\n` 的二进制内容会被改写。搜索场景
不受影响。

**安装/升级 skill:** 用官方方式装 Tavily skill,和 `tvly` 无关、也和瘦客户端无关,
随时可跑:`npx skills add https://github.com/tavily-ai/skills`。
它只把 `SKILL.md` 写到本机 agent 框架的 skill 目录(Claude Code 是 `~/.claude/skills/`)。
skill 运行时调 `tvly …`,瘦客户端透明转发,无需为 skill 改任何东西。

验证:`tvly search "hello" --json --max-results 1` 应返回与本地 CLI 完全
一致的 JSON。

## 更新方式

- **tavily 发布新版 CLI**:在网关主机升级 tvly 并重启:
  `/opt/tavily-gateway/venv/bin/pip install -U tavily-cli && systemctl restart tavily-gateway`
  (若你当初按 pipx 部署,则用 `pipx upgrade tavily-cli`)。网关代码、瘦客户端、skill 都不用改。
- **tavily 发布新版 skill**:每台 agent 跑 `npx skills add https://github.com/tavily-ai/skills`。
  只写本地 `SKILL.md`、不走 `tvly`,和瘦客户端互不影响,没有安装顺序问题。
- **改网关 token / 地址**:改网关 drop-in
  (`/etc/systemd/system/tavily-gateway.service.d/override.conf`)→
  `systemctl daemon-reload && systemctl restart tavily-gateway`;再同步到每台 agent
  (瘦客户端里的 baked 值,或 `TAVILY_GATEWAY_URL`/`TAVILY_GATEWAY_TOKEN` 环境变量)。

## 安全说明

- 网关对每个 `/exec` 请求用 `Authorization: Bearer <token>` 鉴权。请生成
  一个足够强的 token 并妥善保管。
- 网关默认绑定 `0.0.0.0`(跨机器访问需要)。生产环境建议放在 HTTPS 反向
  代理或防火墙规则后面。
- API key 只存在于网关主机上,agent 机器永远看不到 key。

## 配置项(环境变量)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TAVILY_GATEWAY_TOKEN` | (空) | thin client 必须携带的 bearer token,空表示不鉴权(不推荐) |
| `TAVILY_KEYS_FILE` | `/etc/tavily/keys.json` | key 池文件路径,JSON 字符串数组 |
| `TAVILY_USAGE_CACHE` | `/var/lib/tavily/usage_cache` | usage 缓存文件路径,首次运行自动创建 |
| `TAVILY_CACHE_TTL` | `300` | usage 缓存有效期(秒),过期前不重新查 `/usage` |
| `TAVILY_MAX_CONCURRENT` | `8` | 同时运行的 tvly 子进程上限,超限返回 429 |
| `TAVILY_EXEC_TIMEOUT` | `600` | 单次 exec 的 wall-clock 杀进程上限(秒),应 ≥ 客户端超时 |
| `TAVILY_KEY_COOLDOWN` | `60` | key 鉴权/额度失败后跳过该 key 的时长(秒) |
| `TAVILY_GATEWAY_HOST` | `0.0.0.0` | 监听地址 |
| `TAVILY_GATEWAY_PORT` | `18790` | 监听端口 |
| `TVLY_BIN` | `tvly` | 网关 spawn 的 tvly 可执行文件名/路径 |
| `TAVILY_GATEWAY_URL` | `http://127.0.0.1:18790` | (thin client)网关地址 |
| `TAVILY_GATEWAY_TOKEN` | (空) | (thin client)鉴权 token,需与网关一致 |
| `TAVILY_CLIENT_TIMEOUT` | `600` | (thin client)HTTP 超时(秒),research 长任务需要够长 |

## 监控与日志

- **日志**:网关把结构化日志写到 stderr(systemd 送进 journald,用
  `journalctl -u tavily-gateway` 查看)。每次 `/exec` 完成记一行
  `exec cmd=… exit=… dt=…s`;key 被冷却、spawn 失败等告警也落在这里。
- `GET /metrics`:Prometheus 文本格式,含 `tavily_exec_total`、
  `tavily_exec_errors`、`tavily_exec_in_flight`、`tavily_exec_seconds_sum`、
  `tavily_http_{4xx,5xx,429}_total`、`tavily_key_cooldowns`。需带 bearer token
  (与 `/exec` 一致),方便 Prometheus 抓取时统一鉴权。
- `GET /logs`:返回最近 200 条服务端日志(含 CLI 失败时的 stderr 尾部)。需
  bearer token。
- `GET /healthz`:存活探针,**无需鉴权**,返回 `{"ok": true, "keys": N}`。