"""产出文件分类契约：_is_code 闸门 / _is_doc_file 文档判定 / Godot 适配。

分类影响 net_loc / TCER / 文档行 三个核心指标，回归代价高，逐类锁定。
"""
import pytest

from tcer.core.loc import _is_code, _is_doc_file, _is_test_file


@pytest.mark.parametrize("path", [
    # 通用代码
    "src/main.py", "lib/app.ts", "a/b.rs", "x.go", "y.java", "z.cpp",
    "game.lua", "app.dart", "query.sql", "S.scala", "web/app.vue",
    # 着色器 / 游戏
    "fx/blur.hlsl", "fx/sky.glsl", "water.shader",
    # 脚本
    "build.ps1", "run.bat", "deploy.zsh",
    # 无后缀知名文件
    "Makefile", "Dockerfile", "ci/Jenkinsfile", "CMakeLists.txt",
    # 配置
    "config.yaml", "settings.toml", "app.ini", "data.xml",
    # 策划文本
    "design/plot.md", "notes.txt", "data/items.csv",
])
def test_is_code_true(path):
    assert _is_code(path), path


@pytest.mark.parametrize("path", [
    "image.png", "video.mp4", "font.ttf", "archive.zip",
    "model.glb", "audio.ogg", "sprite.webp",
    "report.docx", "sheet.xlsx",   # Office 二进制刻意不计（行模型无法作用）
    "binary", "noext",
    # Godot 二进制格式：行模型无法作用，刻意排除
    "scenes/Main.scn", "res/pack.res", "game.pck", "locale/zh.translation",
])
def test_is_code_false(path):
    assert not _is_code(path), path


# --- Godot 专项 ---
@pytest.mark.parametrize("path", [
    "player/Player.gd",            # GDScript
    "fx/water.gdshader",           # 着色器
    "fx/common.gdshaderinc",       # 着色器 include
    "scenes/Main.tscn",            # 场景（文本化，策划主要产出）
    "themes/ui.tres",              # 资源
    "project.godot",               # 工程配置
    "assets/hero.png.import",      # 导入元数据
    "ui/default.theme",
    "addons/thing/thing.gdextension",   # Godot 4 扩展配置
    "player/Player.gd.uid",             # 4.4+ sidecar
    "scenes/Exported.escn",
    "legacy/native.gdns", "legacy/lib.gdnlib",
    "MyGame.csproj", "MyGame.sln",      # C#/Mono 工程
])
def test_godot_files_are_productive(path):
    assert _is_code(path), path


@pytest.mark.parametrize("path,expect", [
    ("scenes/Main.tscn", False),   # 场景是配置产出，不算文档
    ("player/Player.gd", False),
    ("design/gdd.md", True),       # 策划案是文档
])
def test_godot_doc_split(path, expect):
    assert _is_doc_file(path) is expect, path


# --- 文档判定 ---
@pytest.mark.parametrize("path,expect", [
    ("README.md", True), ("docs/guide.rst", True), ("CHANGELOG", True),
    ("notes.mdx", True), ("paper.tex", True),
    ("data/items.csv", False),          # 数据不算文档（在闸门里但非散文）
    ("CMakeLists.txt", False),          # 代码语义 .txt，排除
    ("requirements.txt", False),
    ("requirements-dev.txt", False),
    ("robots.txt", False),
    ("misc/notes.txt", True),           # 普通 .txt 仍是文档
])
def test_is_doc(path, expect):
    assert _is_doc_file(path) is expect, path


def test_test_file_detection():
    assert _is_test_file("/repo/tests/test_loc.py")   # /tests/ 目录
    assert _is_test_file("/repo/src/test_foo.py")     # pytest 前缀惯例
    assert _is_test_file("/repo/src/foo_test.py")     # 后缀惯例
    assert _is_test_file("/repo/src/foo.test.ts")
    assert not _is_test_file("/repo/src/main.py")
    assert not _is_test_file("/repo/src/contest.py")  # 不误伤普通文件
