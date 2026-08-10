#!/usr/bin/env bash
# TCER server 运维 SOP（服务 tcer-server）。
#
# 子命令：status / setup / start / stop / restart / update
#   进程托管：systemd 单元 tcer-server.service（与 tools-hub / svnhub 等全站服务一致）。
#     —— 不再用 nohup/pidfile 自托管：自托管的子进程活在 opshub 的执行通道 / cgroup 里，
#        通道一断（页面关 / WS 断）daemon 就被 SIGHUP 连坐、restart 还会卡到超时；
#        交给 systemd 后 daemon 归自己的 cgroup+会话，opshub 怎么动都不连坐，
#        restart 立即返回，且开机自启、崩溃自拉。
#   setup   环境自检（TCER server 纯 stdlib，零第三方依赖，无需 venv/pip）
#   update  svn up 拉取代码后跑一次 setup 自检；不会自动重启（内网走 svn，不用 git）
#
# 目标运行环境：Linux 服务器 + Python ≥3.11（server 只用标准库）。
# 部署布局要求：本脚本放“部署根”，其下 server/ 与 tcer/ 为兄弟目录
#   （server/backend/db.py 靠 parents[2] 定位部署根来 import tcer.core）。
#
# systemd 单元见 ops-hub/deploy/tcer-server.service；start/stop/restart/status 需要
# 免密 systemctl，精确命令已登记在 ops-hub/deploy/sudoers.opshub（OPSHUB_SYSTEMCTL）。
set -uo pipefail

SERVICE_ID="tcer-server"
UNIT="tcer-server"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 可调参数（环境自检用）--------------------------------------------- #
PYTHON="${PYTHON:-python3}"                 # ≥3.11；纯 stdlib，无需 venv
SERVER_ENTRY="$ROOT/server/backend/server.py"

# 运行参数（HOST / PORT / SECRET / DB）由 systemd 从部署根 .env 注入（见 .env.example）；
# 这里加载仅为 status 的端口展示与 setup 自检，不影响 systemd 托管的进程。
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
# ----------------------------------------------------------------------- #

cmd_setup() {
  # 纯 stdlib，无 pip 依赖：只校验 Python 版本与入口/依赖布局，并冒烟 import。
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "找不到 $PYTHON，请安装 Python ≥3.11" >&2; return 1
  fi
  "$PYTHON" -c 'import sys; assert sys.version_info >= (3,11), sys.version; print("Python", sys.version.split()[0], "OK")' || return 1
  [ -f "$SERVER_ENTRY" ] || { echo "找不到入口: $SERVER_ENTRY" >&2; return 1; }
  # server 依赖同级 tcer.core（db.py 靠 parents[2] 定位）——校验部署布局。
  [ -f "$ROOT/tcer/core/metrics.py" ] || { echo "缺少 tcer/core/metrics.py（tcer 须与 server 同级）" >&2; return 1; }
  [ -f "$ROOT/tcer/config/model_pricing.json" ] || { echo "缺少 tcer/config/model_pricing.json" >&2; return 1; }
  # 冒烟：确认 db.py 能真正 import 到 tcer.core（而非静默降级到 None）。
  ( cd "$ROOT" && "$PYTHON" -c "import sys;sys.path.insert(0,'server/backend'); import db; assert db.tcer_metrics is not None and db.pricing is not None, 'tcer.core 未加载（降级）'; print('tcer.core 加载 OK')" ) || return 1
  echo "setup 自检通过。"
}

cmd_start()   { sudo -n /usr/bin/systemctl start   "$UNIT"; }
cmd_stop()    { sudo -n /usr/bin/systemctl stop    "$UNIT"; }
cmd_restart() { sudo -n /usr/bin/systemctl restart "$UNIT"; }

cmd_status() {
  # systemctl status 是只读 Bus 查询，普通用户即可执行，无需 sudo
  # （与全站 sop-tools-hub.sh 一致；只有 start/stop/restart 改状态才走 sudo）。
  # 不走 sudo 还顺带绕开 sudoers 参数逐字匹配问题：白名单里 `status tcer-server`
  # 无通配符，带上 `--no-pager -l` 就不命中 → sudo -n 会回退要密码而失败。
  # --no-pager 避免挂在分页器上等输入。
  # 退出码：0=active；3=单元存在但未运行（inactive/failed）。对“查看状态”而言 3
  # 也是成功查询，必须归一为 0——否则脚本在 set -e/pipefail 下以 3 退出，opshub
  # runner 把非零码一律判 failed（runner.ts），会把“服务已停”误渲染成执行失败。
  # 其余码（4=unknown unit 等）照常冒泡上报。
  local rc=0
  /usr/bin/systemctl status "$UNIT" --no-pager -l || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] && return 0
  return "$rc"
}

cmd_update() {
  # 内网 svn 工作副本（vx-tools 仓库子目录，检出到部署根）；不走 git。
  # 与全站服务一致统一用 `svn up`（见 ops-hub deploy/ADD-NEW-SERVICE.md）。
  ( cd "$ROOT" && svn up ) || { echo "svn up 失败" >&2; exit 1; }
  cmd_setup
  echo "更新完成。服务未自动重启，确认无误后再执行 restart。"
}

case "${1:-status}" in
  status)  cmd_status ;;
  setup)   cmd_setup ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  update)  cmd_update ;;
  *) echo "用法: $0 {status|setup|start|stop|restart|update}" >&2; exit 2 ;;
esac