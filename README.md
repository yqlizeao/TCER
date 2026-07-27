![Fable 5 Verified](img/Fable-5_Verified.png)

# TCER

> **Token-to-Code Efficiency Ratio** — 度量 AI 编程效率的离线分析工具

基于 Claude Code、Codex、OpenCode与 Grok本地会话数据，多维度量化「每消耗多少 Token、产出多少有效代码」。

Tkinter 桌面界面，纯离线运行。需要 Python ≥3.11 标准库，零依赖，免安装。

![主界面](img/主界面.png)

## 快速开始

**推荐方式**：双击启动脚本

- Windows：`launch.bat`
- macOS：`launch.command`

**命令行**：

```bash
python -m tcer
```


## 特性

**近百项指标，6 组分类**：鼠标悬停即有中文解释。

![指标分类](img/指标分类.png)

**综合效率指数排名**：多维合成评分，一眼看出哪次会话效率最高。

![综合效率指数排名](img/综合效率指数排名.png)

**趋势分析**：按时间维度追踪效率变化。

![趋势](img/趋势.png)

**模型对比**：多模型并排横评，一眼看清各模型的性价比与产出特征。

![模型对比](img/模型对比.png)

**逐模型详情**：四色堆叠条展示每种模型的 Token 构成。

![子窗口-模型使用详情](img/子窗口-模型使用详情.png)

**会话对比**：选 2~3 个会话并排对比全部指标，标注每行最优值。
![子窗口-子窗口-会话对比](img/子窗口-会话对比.png)

**HTML 报告导出**：导出菜单支持项目级 / 会话级自包含单文件 HTML 报告（深色主题、可排序会话表、CTEI 排名条、模型对比表），零依赖、可直接分享；另有 Markdown / JSON / CSV。

## 文档

- [指标公式与计算步骤](doc/metrics.md)
- [JSONL 数据格式](doc/data-format.md)
- [架构与工程规范](doc/architecture.md)
- [项目规格](CLAUDE.md)

## 许可

MIT
