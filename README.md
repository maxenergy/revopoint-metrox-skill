# Revopoint MetroX 3D 扫描仪全流程使用手册 & AI 辅助扫描 / 逆向 CAD Skills

本仓库包含从 Revopoint 官方 YouTube 频道（`@Revopoint3D`）及 **MetroX 官方完整教学播放列表 (`PLN8UlQmwKrZISjkUyKt-H1pmSlsqwSLMw`)** 20 部教程视频中深度提炼的 **中文全流程使用手册**，以及两套专为 AI 编程助手（Antigravity / Claude Code / Cursor 等）打造的 **Agent Skills**：

1. 📖 **[Revopoint-MetroX-使用手册与技巧大全.md](./Revopoint-MetroX-使用手册与技巧大全.md)**：涵盖 MetroX / MetroX Pro / MetroY 全系列硬件对比、4 大扫描模式决策矩阵、标志点与磁力标志块套件（金字塔悬空法、四柱标志杆立体笼、标志点胶带）实战技法、窄薄件/透明件/黑亮金属件无喷粉扫描秘籍、点云后处理与“前中后 5 点手动对齐”拼接铁律、CPU/GPU 性能实测天梯表及五向标定排障指南。
2. 🧠 **Skill 1：[`revopoint-metrox`](./skills/revopoint-metrox/SKILL.md)**：Revopoint MetroX 3D 扫描专家知识库与参数推荐 Skill。
3. 🤖 **Skill 2：[`revo-metro-cad-copilot`](./skills/revo-metro-cad-copilot/SKILL.md)**：`RevoMetro.exe` 桌面程序 GUI 自动化控制器、实时 `rvScan5.log` 扫描追踪诊断副驾驶，以及基于 FreeCAD OpenCASCADE 内核的 **3D 扫描点云/网格自动清洗 → 逆向参数化 STEP/IGES 3D CAD 实体 → DXF/PDF/SVG/PNG 工程三视图蓝图生成器**。

---

## 📂 仓库目录结构

```text
revopoint-metrox-skill/
├── README.md                                  # 项目说明与快速上手指南
├── Revopoint-MetroX-使用手册与技巧大全.md       # 20 部官方视频提炼的完整使用手册与实战技巧大全
├── skills/                                    # 标准 Agent Skills 目录（可直接复制到全局 skills 目录）
│   ├── revopoint-metrox/
│   │   ├── SKILL.md                           # MetroX 操作指南与参数决策矩阵 Skill
│   │   └── references/
│   │       └── metrox-complete-guide.md       # 20 部视频索引、硬件对比与 6 大复杂工件实战拆解
│   └── revo-metro-cad-copilot/
│       ├── SKILL.md                           # RevoMetro.exe 自动化控制与逆向 CAD 出图 Skill
│       └── scripts/
│           ├── revo_controller.py             # RevoMetro.exe 进程/配置/快捷键控制与 rvScan5.log 实时诊断
│           └── scan_to_cad.py                 # 点云清洗、PCA 对齐、STEP/IGES 实体逆向与 DXF/PDF 三视图生成
└── .agents/
    └── skills/                                # 工作区级自动加载 Skills 镜像
```

---

## 🚀 快速安装与使用

### 1. 安装 Skills 到本地 AI 助手
克隆本仓库后，可以直接在工作区内使用（已内置 `.agents/skills/`），或将 `skills/` 下的两个文件夹复制到全局 Skills 目录：

```powershell
git clone https://github.com/maxenergy/revopoint-metrox-skill.git
cd revopoint-metrox-skill

# 复制到全局 Skills 目录（Windows PowerShell）
Copy-Item -Path ".\skills\*" -Destination "$HOME\.gemini\config\skills\" -Recurse -Force
```

### 2. 控制 `RevoMetro.exe` 与实时诊断扫描日志 (`revo_controller.py`)
```powershell
# 查看 RevoMetro.exe 运行状态、扫描仪硬件型号、GPU 性能评级、日志追踪丢失诊断与本地工程列表
python skills/revo-metro-cad-copilot/scripts/revo_controller.py status

# 启动 RevoMetro.exe 或直接打开指定的 .revo / .ply / .stl / .obj 文件
python skills/revo-metro-cad-copilot/scripts/revo_controller.py launch --project "C:\path\to\project.revo"

# 实时分析 rvScan5.log 日志（诊断 0x10002 标记点不足、0x10006 特征丢失，并获取 AI 推荐融合点距）
python skills/revo-metro-cad-copilot/scripts/revo_controller.py analyze-log --lines 500

# 发送 GUI 快捷键控制扫描（开始/暂停扫描、保存、反选删除外围标志块等）
python skills/revo-metro-cad-copilot/scripts/revo_controller.py send-key --action toggle_scan
python skills/revo-metro-cad-copilot/scripts/revo_controller.py send-key --action delete_inverted
```

### 3. 将 3D 扫描工程一键转为 3D CAD 实体与 2D/3D 工程图纸 (`scan_to_cad.py`)
> 依赖环境：本机安装 [FreeCAD 1.0/1.1+](https://www.freecad.org/)（脚本会自动调用 FreeCAD 内置的 Python、OpenCASCADE `Part`、`ReverseEngineering`、`Mesh`、`SciPy` 与 `Matplotlib`）。

```powershell
# 自动读取最新的 Revo Metro 扫描工程，执行 RANSAC 去底面、孤立噪点剔除、PCA 正交对齐，
# 并输出：
#   1. 清理后的高精度 3D 网格 (.stl, .ply, .obj)
#   2. 逆向参数化 3D CAD 实体 (.step, .iges, _parametric_cad.py)
#   3. 2D/3D 工程三视图与多高度截面蓝图 (.dxf, _blueprint.pdf, _blueprint.svg, _blueprint.png, _dimensions.json)
python skills/revo-metro-cad-copilot/scripts/scan_to_cad.py --latest-project --remove-base --out-dir "./output"
```
