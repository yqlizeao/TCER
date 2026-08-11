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
    """建议下载目标。

    Windows: exe 同目录(避免 ``move`` 跨盘失败)，后缀 ``.exe``。
    macOS: 系统 temp 目录下的 ``.app.zip``（mac 改打 .app 包，下载的是 zip；
    不写进 .app 内部以免污染 bundle）。
    """
    exe = current_exe()
    if os.name == "nt":
        return exe.parent / "TCER.update.exe"
    import tempfile
    return Path(tempfile.gettempdir()) / "TCER.update.zip"


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


def _clean_launch_env():
    """去掉 PyInstaller onefile 内部环境变量后再启动新 exe / updater 脚本。

    Why: onefile bootloader 把 ``_PYI_APPLICATION_HOME_DIR`` 等变量注入环境;
    若继承给新 exe,它的 bootloader 会当成 child 复用**旧** _MEI(已被旧进程
    退出时清理),导致 ``python311.dll`` LoadLibrary 失败。清掉这些变量,新 exe
    才会正常解压到自己的新 _MEI。
    """
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("_MEI") or k.startswith("_PYI"))}


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
    # 独立进程跑 bat;本程序随后退出释放 exe 句柄。清掉 _PYI*/_MEI* 环境变量,
    # 免得 bat start 的新 exe 误复用旧 _MEI(python311.dll 找不到)。
    subprocess.Popen(["cmd", "/c", str(bat)], env=_clean_launch_env(), close_fds=True)


def _mac_replace(exe, new_archive):
    """mac 替换安装。按当前安装形态分流：

    - 当前 exe 在 ``.app`` bundle 内、且下载的是 ``.app.zip`` → 解压替换整个 bundle。
    - 否则（旧式裸二进制，fallback）→ ``copy2 + chmod``。
    """
    new_archive = Path(new_archive)
    in_bundle = (exe.parent.name == "MacOS"
                 and exe.parent.parent.name == "Contents")
    if in_bundle and new_archive.suffix == ".zip":
        _mac_replace_app_bundle(exe, new_archive)
        return
    # 旧式裸二进制（fallback）
    shutil.copy2(new_archive, exe)
    os.chmod(exe, 0o755)
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(exe)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    subprocess.Popen([str(exe)], env=_clean_launch_env(), close_fds=True)


def _mac_replace_app_bundle(exe, new_zip):
    """把下载的 ``.app.zip`` 解压、替换当前 ``.app`` bundle，再启动新实例。

    用 ``ditto -x`` 解（与 CI 的 ``ditto -c`` 配对，保留 unix 可执行权限）；旧
    ``.app`` rename 到 ``.old``（运行中的文件允许 rename，当前进程持旧映像继续
    到退出），新 ``.app`` move 到位，``xattr -dr`` 清整个 bundle 的 quarantine。
    残留 ``.old`` 由下次更新开头清理。
    """
    import tempfile
    # .../TCER.app/Contents/MacOS/TCER → .../TCER.app
    app_bundle = exe.parent.parent.parent
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ditto", "-x", "-k", str(new_zip), td],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        apps = list(Path(td).glob("*.app"))
        if not apps:
            raise RuntimeError("mac .app.zip 解压后未找到 .app bundle")
        new_app = apps[0]
        old = app_bundle.parent / (app_bundle.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        if app_bundle.exists():
            app_bundle.rename(old)      # 运行中允许 rename
        shutil.move(str(new_app), str(app_bundle))
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app_bundle)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    subprocess.Popen(["open", "-n", str(app_bundle)],
                     env=_clean_launch_env(), close_fds=True)
