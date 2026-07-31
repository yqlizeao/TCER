"""应用内自更新:下载新版本二进制、替换当前可执行文件、重启。

仅发布版(PyInstaller 打包,``sys.frozen``)可用——源码运行无"自身可执行文件"
概念(由调用方判断,本模块假定发布版环境)。平台策略:

- **Windows**:运行中的 exe 被锁,不能直接覆盖。生成 ``_tcer_updater.bat`` 在独立
  进程里轮询删除旧 exe(等本进程退出释放句柄)→ ``move`` 新 exe → 重启 → 自删 bat。
  bat 用 mbcs 编码写(中文系统=GBK),兼容含中文/空格的路径。
- **macOS**:可直接覆盖运行中的二进制(当前进程继续用已加载的旧映像);替换后对新
  文件清 ``com.apple.quarantine``(下载带的 Gatekeeper 标记)、``chmod 755``,
  再启动新进程。

下载用 urllib 流式 + 进度回调;失败抛异常由 GUI 捕获。不做签名校验(项目选择不签名),
完整性依赖 GitHub Release 的 HTTPS。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def current_exe():
    """当前可执行文件路径(发布版 sys.executable)。"""
    return Path(sys.executable).resolve()


def download_target():
    """建议下载目标:exe 同目录(避免 Windows ``move`` 跨盘失败)。"""
    exe = current_exe()
    suffix = ".exe" if os.name == "nt" else ".new"
    return exe.parent / ("TCER.update" + suffix)


def asset_for_current_platform(release):
    """从 release['assets'] 选当前平台的 (name, url);找不到返回 None。"""
    is_mac = sys.platform == "darwin"
    is_win = os.name == "nt"
    for name, url in release.get("assets", []):
        low = name.lower()
        if is_win and low.endswith(".exe"):
            return name, url
        if is_mac and "macos" in low and "arm64" in low:
            return name, url
    return None


def download(url, dest, progress_cb=None, chunk=1 << 15):
    """流式下载 url 到 dest;progress_cb(done_bytes, total_bytes|None) 可选。"""
    # GitHub release asset 经 objects CDN 分发,默认/短 UA 易被 403;
    # 用浏览器样 UA + Accept 是官方推荐的 asset 下载方式。
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; TCER-Updater)",
        "Accept": "application/octet-stream",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total else None
        done = 0
        with open(dest, "wb") as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, total)


def apply_and_restart(new_binary):
    """用 new_binary 替换当前可执行文件并启动新进程。

    调用方在本函数返回后应**立即退出主程序**(root.destroy + sys.exit),让旧 exe
    释放句柄(Windows)或让新进程接管。
    """
    exe = current_exe()
    new_binary = Path(new_binary)
    if os.name == "nt":
        _windows_replace(exe, new_binary)
    elif sys.platform == "darwin":
        _mac_replace(exe, new_binary)
    else:
        raise RuntimeError(f"auto-update unsupported on {sys.platform}")


def _windows_replace(exe, new_binary):
    bat = exe.parent / "_tcer_updater.bat"
    # mbcs = Windows ANSI 代码页(中文系统为 GBK),兼容含中文/空格的路径。
    bat.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "EXE={exe}"\r\n'
        f'set "NEW={new_binary}"\r\n'
        ":wait\r\n"
        'del "%EXE%" >nul 2>nul\r\n'
        'if exist "%EXE%" (\r\n'
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        'move /Y "%NEW%" "%EXE%" >nul 2>nul\r\n'
        'start "" "%EXE%"\r\n'
        'del "%~f0" >nul 2>nul\r\n',
        encoding="mbcs",
    )
    # 独立进程跑 bat;本程序随后退出释放 exe 句柄
    subprocess.Popen(["cmd", "/c", str(bat)], close_fds=True)


def _mac_replace(exe, new_binary):
    # mac 允许覆盖运行中的二进制(当前进程继续用已加载的旧映像)
    shutil.copy2(new_binary, exe)
    os.chmod(exe, 0o755)
    # 清掉下载文件带的 quarantine 标记,免得重启时被 Gatekeeper 拦
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(exe)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    subprocess.Popen([str(exe)], close_fds=True)
