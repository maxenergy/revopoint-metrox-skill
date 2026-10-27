---
name: revo-metro-cad-copilot
description: AI-assisted 3D scanning copilot, GUI/config/log controller for RevoMetro.exe (C:\Program Files\Revo Metro\RevoMetro.exe), and automated Scan-to-CAD / 2D-3D Engineering Drawing generator. Activate this skill whenever the user wants to launch, control, configure, or monitor RevoMetro.exe, perform AI-assisted 3D scanning, diagnose tracking loss or scan logs, or convert 3D scan data (.revo, .ply, .stl, .obj) into cleaned 3D meshes, reverse-engineered parametric 3D CAD solids (.step, .iges, Python CAD scripts), or 2D/3D engineering blueprints (.dxf, .svg, .pdf, .png). Also activate it when the user wants the hands-free pipeline "scan finished → fuse/mesh in RevoMetro → drawings → slice for Bambu Lab H2D / A2L 3D printers (.3mf / .gcode)".
---

# Revo Metro AI 辅助扫描控制与 3D 逆向工程制图 Skill (`revo-metro-cad-copilot`)

本 Skill 提供对 **`C:\Program Files\Revo Metro\RevoMetro.exe`** 的全流程自动化控制、实时日志与视觉领航诊断，以及基于 **FreeCAD 1.1 (OpenCASCADE 内核) + SciPy + Matplotlib** 的点云/网格自动逆向建模与 2D/3D 工程图纸生成能力。

如果任务涉及 MetroX / MetroX Pro / MetroY 扫描仪的物理布点策略、四模式选型或复杂材质（黑亮件、窄薄件、透明件）经验法则，请同时参考同仓库下的姊妹 Skill：
- [`revopoint-metrox`](../revopoint-metrox/SKILL.md)

---

## 1. 核心架构与脚本工具箱

本 Skill 内置以下可直接执行的自动化脚本（位于 `scripts/` 目录）：

| 脚本路径 | 核心职责 | 运行环境 |
| :--- | :--- | :--- |
| [`scripts/revo_controller.py`](scripts/revo_controller.py) | **RevoMetro.exe 控制与 AI 扫描领航器**：负责进程启停、直接打开 `.revo`/`.ply` 文件、读写 `revoscan.cfg` 与 `.revo` 扫描参数、发送 Win32 全局快捷键与窗口坐标点击、实时截取软件窗口画面、解析 `rvScan5.log` 诊断丢帧与标志点不足。 | 系统 Python（零第三方依赖，直接调用 Win32 API） |
| [`scripts/scan_to_cad.py`](scripts/scan_to_cad.py) | **点云/网格逆向 CAD 与工程图纸生成器**：自动从 `.revo` 工程或 `.ply`/`.stl`/`.obj` 读取三维数据，完成统计滤波、RANSAC 桌面切除、连通域孤立块剔除、PCA 基准面摆正，并一次性生成 **清理后 3D 网格 (`STL`/`PLY`/`OBJ`)** + **参数化 3D CAD 实体 (`STEP`/`IGES`/`.py`)** + **2D/3D 工程三视图与截面图 (`DXF`/`PDF`/`SVG`/`PNG`/`JSON`)**。 | 自动切换至 `C:\Users\rogers\Applications\FreeCAD\bin\python.exe`（内置 OpenCASCADE + NumPy + SciPy + Matplotlib） |
| [`scripts/revo_pipeline.py`](scripts/revo_pipeline.py) | **全自动流水线**：经命名管道让运行中的 RevoMetro 打开工程，经 Frida 注入的 Qt 反射点击 One-click Edit「Apply」完成融合+网格，跟踪 `rvScan5.log` 等待完成，再串联 `scan_to_cad.py` 与 `slice_print.py`。 | 系统 Python + `rea` CLI（内置 Frida） |
| [`scripts/frida/qt_bridge.js`](scripts/frida/qt_bridge.js) | 注入 RevoMetro 的 Qt5 控制脚本：枚举全部 QWidget（objectName/类名/文字/可见/可用），用 `QMetaObject::invokeMethod(..., Qt::QueuedConnection)` 在 GUI 线程上点击按钮。 | 由 `revo_pipeline.py` 调用 |
| [`scripts/slice_print.py`](scripts/slice_print.py) | **拓竹 H2D / A2L 切片**：展开 Bambu 系统预设的 `inherits` 继承链，调用 OrcaSlicer CLI 输出 `.3mf` + `.gcode`。 | 系统 Python + `C:\Program Files\OrcaSlicer\orca-slicer.exe` |

---

## 2. 工作流一：控制 `RevoMetro.exe` 完成 AI 辅助扫描

### 步骤 1：巡检软件状态、连接硬件与现有工程 (`status`)
在开始操作前，先运行 `status` 查看 `RevoMetro.exe` 是否在运行、当前连接的扫描仪型号与序列号（如 `MetroX Pro`）、GPU 加速配置、最近一次扫描日志诊断以及 `%APPDATA%\RevoMetro\Projects` 下的所有工程与扫描节点：
```powershell
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" status
```

### 步骤 2：预配置扫描参数、双轴转盘与融合点距 (`configure`)
根据被扫工件特征（材质、尺寸、是否使用转盘），在启动扫描前通过 `configure` 直接写入 `revoscan.cfg` 及 `.revo` 工程配置：
```powershell
# 示例：设置点云融合点距为 0.30mm，双轴转盘单圈 70s、间隔 15°，第1圈固定 0° 水平基准 + 正负倾角
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" configure `
  --fuse-pitch 0.30 `
  --turntable-speed 70 `
  --turntable-step 15 `
  --turntable-angles "0,15,-15,25,-25"
```
- **针对具体 `.revo` 工程修改扫描模式与曝光**：
  - `--scan-mode`: `0` = 全场蓝光结构光 (`Full-field`)，`1` = 多线蓝色激光 (`Cross Lines` / `Parallel Lines`)
  - `--scan-object`: `0` = 普通材质 (`General`)，`1` = 黑色材质 (`Black`)，`2` = 金属/光泽反光材质 (`Metallic Shiny`)
  - `--depth-exposure`: `0` = 自动曝光，`1~10` = 手动深度曝光档位（黑件推荐 `3~5`）

### 步骤 3：启动或唤醒 `RevoMetro.exe` 并加载目标工程 (`launch`)
`RevoMetro.exe` 支持通过命令行直接传入 `.revo` 工程文件或 `.ply`/`.stl`/`.obj` 模型路径：
```powershell
# 启动 RevoMetro（若已运行则自动置顶唤醒窗口）
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" launch

# 启动并直接打开指定 .revo 工程
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" launch `
  --project "C:\Users\rogers\AppData\Roaming\RevoMetro\Projects\Project10262026204924\Project10262026204924.revo"
```

### 步骤 4：GUI 快捷键自动化与窗口交互 (`action`)
通过 `action` 子命令向 `RevoMetro.exe` 发送原生快捷键或相对坐标点击：

| 动作指令 (`command`) | 对应快捷键 | 功能说明 |
| :--- | :--- | :--- |
| `start_pause` | `Space` | **开始扫描 / 暂停扫描**（最核心高频控制指令） |
| `cancel` | `Esc` | **取消当前扫描** 或 **取消点云选区** |
| `new_project` | `Ctrl+N` | 新建扫描工程 |
| `open_project` | `Ctrl+O` | 打开已有扫描工程 |
| `import_model` | `Ctrl+I` | 导入外部三维模型 (`.revo`/`.ply`/`.obj`/`.stl`) |
| `save` | `Ctrl+S` | 保存当前工程 |
| `select_all` | `Ctrl+A` | 全选点云/网格 |
| `deselect` | `Ctrl+D` | 取消选择 |
| `invert_selection` | `Ctrl+Shift+I` | 反选当前点云/网格选区 |
| `delete_invert` | `Ctrl+Shift+Del` | **一键反选删除**（配合连通域选中主模型后，一键清除周围所有标志块和杂点！） |
| `delete` | `Delete` | 删除当前选中的噪点或标志块 |
| `undo` / `redo` | `Ctrl+Z` / `Ctrl+Y` | 撤销 / 重做上一步编辑操作 |
| `fullscreen_scan` | `Shift+M` | 切换扫描预览窗口全屏显示 |
| `model_screenshot` | `Ctrl+Shift+M` | 保存当前 3D 模型视图截屏 |
| `toggle_light` | `Ctrl+L` | 切换 3D 视图光照方向 |
| `click_ratio --rx X --ry Y` | 鼠标左键点击 | 点击窗口内归一化坐标 `(rx, ry)`（范围 `0.0 ~ 1.0`），用于点击顶部工具栏按钮（如一键编辑、融合、孤立点、网格化、导出） |

```powershell
# 示例：触发 Space 键开始或暂停扫描
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" action start_pause

# 示例：选中主模型后执行 Ctrl+Shift+Del 反选删除周围所有杂点
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" action delete_invert
```

### 步骤 5：AI 实时视觉截屏与日志领航诊断 (`screenshot` & `diagnose`)
在用户手持扫描或自动转盘扫描过程中，AI 可随时截取窗口画面并分析 `rvScan5.log` 底层状态码：
```powershell
# 1. 截取 RevoMetro 实时界面，供 AI 使用 read_file 工具查看曝光（红=过曝，蓝=欠曝，灰=正常）与距离绿区
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" screenshot --out "d:\source\revopoint\output\revo_live.png"

# 2. 采样监控 rvScan5.log 日志，自动诊断是否出现标志点不足 (0x10002 / getLastRegState=6) 或特征丢失 (0x10006 / getLastRegState=10)
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_controller.py" diagnose --watch 2
```
- **日志底层状态码速查表**：
  - `WorkMode`: `0` = 主页 (`StartPage`)，`1` = 扫描界面 (`ScanPanel`)，`2` = 模型编辑后处理界面 (`EditModel`)
  - `ScanState`: `0` = 未扫描 (`Idle`)，`1` = 预览就绪 (`Preview`)，`2` = 正在采集 (`Scanning`)，`3` = 已暂停 (`Paused`)
  - `sigScanStatusMessage`:
    - `0x10001`: 正常追踪拼帧；
    - `0x10002` (`getLastRegState is 6`): **标志点不足（Insufficient Markers）** -> 立即提示用户放慢转角移动速度、水平持握扫边缘、开启 `High Speed` 模式（28°~30° 识点角）或补放磁力标志块；
    - `0x10006` (`getLastRegState is 10`): **几何特征不足（Feature Tracking Loss）** -> 提示用户在对称件/薄边件旁放置 2~3 个不规则参考小物件，或切回激光标志点模式。

---

## 3. 工作流二：从扫描数据一键生成 3D 网格、参数化 CAD 实体与 2D/3D 工程图纸

无需在 GUI 中繁琐手动点击导出，`scripts/scan_to_cad.py` 可直接读取 `%APPDATA%\RevoMetro\Projects\<工程名>` 下的扫描节点数据（自动优先选择已融合的 `fuse_mesh.ply`，若尚未融合则直接读取 `raw_preview.ply` 原始点云并自动完成滤波与曲面重建），也可直接处理任意 `.ply` / `.stl` / `.obj` 文件。

### 执行命令示例
```powershell
# 方式 A：直接指定 Revo Metro 工程名称（自动寻找最优扫描节点并切除底部转盘平面）
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\scan_to_cad.py" `
  --project "Project10262026204924" `
  --remove-base `
  --out-dir "d:\source\revopoint\output"

# 方式 B：指定工程名称 + 特定扫描节点名称 (如 1026_01)
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\scan_to_cad.py" `
  --project "Project10262026204924" `
  --node "1026_01" `
  --remove-base `
  --out-dir "d:\source\revopoint\output"

# 方式 C：直接传入任意 .ply / .stl / .obj 文件路径
python "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\scan_to_cad.py" `
  --input "C:\path\to\model.ply" `
  --name "MyWorkpiece" `
  --out-dir "d:\source\revopoint\output"
```

### 自动生成的全套交付物清单（保存在 `--out-dir` 下）
每次运行 `scan_to_cad.py` 会自动完成 **点云去噪 -> RANSAC 底面切除 -> 剔除孤立标志点圆片簇（外径 ≤ `--marker-max-mm`，默认 12mm，且厚度 < 0.6mm；`--keep-markers` 关闭）-> 连通域剔除标志块 -> PCA 主轴摆正（底面贴齐 `Z=0`，长轴对齐 `X` 轴） -> 特征提取与尺寸拟合**，并输出以下 **10 份标准工程文件**：

1. **高精度清理与摆正后的 3D 网格文件（用于 3D 打印 / 逆向对比）**：
   - `<name>_cleaned.stl`（标准二进制 STL 网格）
   - `<name>_cleaned.ply`（标准 PLY 网格）
   - `<name>_cleaned.obj`（标准 Wavefront OBJ 网格）
2. **逆向参数化 3D CAD 实体模型（用于 SolidWorks / AutoCAD / FreeCAD / Fusion 360 编辑）**：
   - `<name>_cad.step`（ISO 10303-21 标准 **STEP B-Rep 实体模型**，包含外轮廓拉伸/回转体及自动识别并布尔切除的圆孔特征）
   - `<name>_cad.iges`（标准 **IGES 曲面/实体模型**）
   - `<name>_parametric_cad.py`（**可编辑的 Python 参数化 CAD 脚本**：顶部以清晰变量列出 `LENGTH_X_MM`、`WIDTH_Y_MM`、`HEIGHT_Z_MM`、`OUTER_PROFILE_XY` 顶点坐标与 `HOLES` 孔位参数。用户或 AI 修改任意尺寸数值后重新运行该脚本，即可生成精确规整后的 STEP/IGES 图纸！）
3. **2D / 3D 工程三视图与横截面图纸（用于加工制造与尺寸检验）**：
   - `<name>_drawing.dxf`（**AutoCAD 兼容 DXF 工程图**：分层包含 `TOP_OUTER_PROFILE` 俯视外轮廓、`SECTION_Z_20/50/80` 多高度截面线、`TOP_HOLES` 圆孔、`FRONT_VIEW` 主视图、`SIDE_VIEW` 左视图）
   - `<name>_blueprint.png` / `<name>_blueprint.pdf` / `<name>_blueprint.svg`（**六宫格标准工程蓝图**：包含带尺寸标注的主视图、左视图、带孔径与坐标引线标注的俯视图、20%/50%/80% 多高度截面叠加图、3D 等轴测渲染图以及标准工程图标题栏）
   - `<name>_dimensions.json`（结构化尺寸与几何特征报告 JSON）

---

## 4. AI 定制修改 CAD 图纸的闭环工作流

当用户提出诸如 *“把扫描出来的零件长度规整到 120.0 mm、厚度改为 10.0 mm，或者把中间的孔径改为标准的 M8 (Ø8.5 mm)”* 时：
1. 先运行 `scan_to_cad.py` 生成初始的 `<name>_dimensions.json`、`<name>_blueprint.png` 与 `<name>_parametric_cad.py`。
2. 使用 `read_file` 查看 `<name>_blueprint.png` 与 `<name>_dimensions.json`，向用户汇报实测扫描尺寸与识别到的孔位/轮廓特征。
3. 使用 `edit_file` 直接修改 `<name>_parametric_cad.py` 顶部的参数常量（如 `HEIGHT_Z_MM`、`HOLES` 或 `OUTER_PROFILE_XY`），然后用 `"C:\Users\rogers\Applications\FreeCAD\bin\python.exe" <name>_parametric_cad.py` 重新生成精确规整后的 `.step` 与 `.iges` 实体模型！

---

## 5. 工作流三：扫描完成 → 自动出图 → 拓竹 H2D / A2L 切片（`revo_pipeline.py`）

### 一条命令跑完全程
```powershell
$P = "d:\source\revopoint\.agents\skills\revo-metro-cad-copilot\scripts\revo_pipeline.py"

# 对最新工程（或 --project 指定）：一键融合+网格 → 去标志点/出图 → 同时为 H2D 与 A2L 切片
python $P auto --project "Project10262026204924" --printer h2d a2l --remove-base

# 节点已有 fuse_mesh.ply 时默认跳过 RevoMetro 处理；--force 强制重新一键处理
# --print-source cad 改为打印逆向 STEP 实体（默认打印清洗后的扫描网格 STL）
# --process "0.16mm Standard @BBL H2D" / --filament "Bambu PETG HF @BBL H2D" 覆盖预设
# --printer（不跟值）跳过切片

# 守护模式：等待 RevoMetro 扫描保存（日志 "Processor saveTo out"）后自动执行 auto
python $P watch --printer h2d --once
```
输出位于 `<out-dir>\<工程名>_<节点名>\`：全部图纸文件、`print\<name>_cleaned_<printer>.3mf`、`print\<name>_cleaned_<printer>_plate_1.gcode`，以及汇总的 `<name>_pipeline_report.json`（含尺寸、清洗统计、切片结果）。

### 单步调试命令
```powershell
python $P widgets                       # 列出当前可见控件 (objectName / 类 / 可用 / 文字)
python $P widgets --name pcFuseBtn --all
python $P click --text Home --cls QToolButton
python $P open --project Project_Example
python $P process --project Project10262026204924 --force   # 只做一键融合+网格

python slice_print.py model.stl --printer a2l --out-dir out\print
python slice_print.py x --printer h2d --list-presets         # 列出可用工艺/耗材预设
```

### 逆向得到的 RevoMetro 5.8.8.460 控制面（实测 + Ghidra 反编译）
- **单实例 IPC**：第二实例把首个位置参数以 UTF-8 写入 `QLocalServer "revoscan5_localserver"`（Windows 命名管道 `\.\pipe\revoscan5_localserver`）。`StartPageWidget::openDocument` 只在**首页可见且无模态窗口**时打开，所以在工程内要先点 `Home`。
- **控件定位**：UIA 的 AutomationId 为空，按钮靠 Qt objectName/文字定位。功能栏：`fastProcessFuncBtn`（一键处理）、`pcFuseBtn`、`meshConstructBtn`、`meshHoleFillBtn`、`meshSimplifyBtn`、`exportBtn`、`thirdBtn`（第三方软件）、`BatchButton`。同名控件有 `RvFunctionItem` 容器和 `SwitchButton` 按钮两个，点击时用 `--cls SwitchButton`。
- **一键处理**：`editPage`（`QuickEditPage`）里的 `Apply`（`RvDefaultButton`）按推荐参数完成融合 → 网格 → 去噪，作用于工程打开时加载的节点（`.revo` 中 `nodes` 的最后一个，即最新扫描）。
- **完成信号**：`[QuickEdit] one-click edit end` → `mesh change, save to : <node>\fuse_mesh.ply`；`property.rvproj` 写入 `fuse_file` / `mesh_file`。失败标记：`RVE_SCAN_FUSE_FAILED`、`RVE_SCAN_MESH_FAILED`、`fuseProcess error`。
- **第三方软件按钮**：先导出模型，再 `ShellExecuteW("open", <exe>, <模型路径>)`。exe 路径来自注册表（`RevoDesign Location`、`SOFTWARE\Revopoint\Revo Measure`、`SOFTWARE\Geomagic`）。本流水线没有利用这条路（需要改 HKLM）。

### 切片预设说明
- OrcaSlicer 2.4.2 自带 H2D 预设，没有 A2L。A2L 的机器预设取自 Bambu Studio 缓存（`%APPDATA%\BambuStudio\system\BBL`），工艺/耗材默认用 Orca 的 `@BBL A1` 预设（Bambu Studio 的工艺值会被 Orca 校验拒绝，例如 `tree_support_wall_count=-1`）。拿到 Orca 官方 A2L 预设后，应换成原生预设。
- OrcaSlicer CLI 退出码：`-17` 工艺与机器不兼容，`-18` 参数超出范围；`--export-3mf` 的文件名相对于 `--outputdir`。

### 前置条件与限制
- 扫描本身仍需人工完成；流水线从"扫描已保存"开始。
- 需要 `rea` CLI（Frida）以当前用户权限 attach 到 RevoMetro；没装 `rea` 时会退回 `npx -y rea-agents@6.4.0`。
- 从本工具的 shell 里启动的 RevoMetro 会随 shell 的 Job 对象一起被回收，所以 `ensure_running` 通过 `explorer.exe` 启动它。
- 逆向 STEP 是"轮廓拉伸 + 竖直通孔"的简化实体，自由曲面零件请打印 `--print-source mesh`（默认）。
