# TCER v1.0.10

本次更新完整重做产出文件分类，新增对 Godot 游戏开发的支持，并修复图表数值的科学计数法显示。

## 新功能

### 产出文件识别全面适配
代码产出与质量（G4）指标依赖「什么是代码、什么是文档」的准确判定，本次大幅扩展：

- **代码后缀 28 → 80+**：补齐 Lua、Dart、R、Julia、Elixir、Haskell、Kotlin Script、PowerShell、zsh/fish、scss/sass/less、Astro、Jinja/Handlebars/ERB 模板、hlsl/glsl/wgsl 着色器、cmake/gradle/bazel 构建脚本、.proto、.ipynb 等。
- **无后缀知名文件**：Makefile、Dockerfile、Jenkinsfile、Justfile、CMakeLists.txt 等此前全部漏计，现已识别为产出。
- **文档判定修正**：`CMakeLists.txt` / `requirements.txt` / `robots.txt` 等「后缀像文档、实为工程文件」不再被误判为文档行；文档类型补齐 `.mdx` / `.markdown` / `.asciidoc` 及 CHANGELOG / LICENSE / CONTRIBUTING。

### Godot 游戏开发支持
针对使用 Godot 开发小游戏的场景，完整适配 16 种 Godot 文件类型，策划的产出效率现可被 TCER 衡量：

- **脚本/着色器**（计为代码）：`.gd`（GDScript）、`.gdshader`、`.gdshaderinc`。
- **场景/资源/工程**（计为配置产出）：`.tscn`（场景）、`.tres`（资源）、`.escn`、`project.godot`、`.import`、`.uid`（4.4+ sidecar）、`.theme`、`.gdextension`。
- **老版本与 C#**：Godot 3 的 `.gdns`/`.gdnlib`，Mono 工程的 `.csproj`/`.sln`。
- Godot 二进制格式（`.scn`/`.res`/`.pck`/`.translation`）刻意不计——文本行模型无法对二进制产生行增量。
- 策划案（`.md` GDD）仍归为文档行，场景/资源归为配置产出，代码/文档分野对 Godot 项目语义正确。

## 改进

- **图表数值禁科学计数法**：修复图表 tooltip 兜底格式对极小值（如 |v|<1e-4 的成本/返工率）输出 `1.2e-05` 的问题，统一定点显示。
- **测试文件识别**：补齐 `test_foo.py` 前缀式命名（pytest 主流惯例），此前仅识别 `foo_test.py` 后缀式。
- 新增文件分类契约测试（约 50 条），覆盖代码/非产出/Godot/文档/测试五组，防分类回归。

---

**完整变更**：`v1.0.9...v1.0.10`
