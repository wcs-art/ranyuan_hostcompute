# EV1 睡眠监测上位机

燃元科技 EV1 睡眠监测设备的上位机软件。采集 EEG 4 通道 + fNIRS 10 通道（760/850 nm 双波长）数据，
实时显示、记录，并导出为 CSV / EDF / BDF / SNIRF / XDF。

波形区顶部可切换“14 通道分图”和“多通道叠加”。叠加页将 EEG 绘在左侧 μV 轴、
fNIRS 与 EEG 共用时间轴；标定浓度数据使用右侧 mM 轴，当前固件的 01/02
帧使用原始 ADC 码轴。下方通道勾选框控制两页的可见曲线。
叠加页纵轴默认固定，点“Zoom · 适配当前波形”可按当前时间窗的可见通道调整一次比例，
之后仍保持固定；“恢复默认比例”回到 EEG ±100 μV、fNIRS ±0.5 mM。

底部选择“二进制串口”并点击连接时，上位机会通过所选 COM 口依次发送
`EEG OUTPUT UART`、`EEG UART ON`，确认回复后开始读取 100 字节帧。
配置文件不再预设旧机器的 COM 编号。启动后点“刷新端口”，选择实际的控制串口；
只有明确通过 `--com=COMx` 或配置文件指定端口时才会在启动阶段自动连接。
若固件返回充电禁传，上位机会发送 `CHARGE OFF`；只有收到 `result=0`
才重试开启 EEG。此操作在采集期间关闭充电使能，不代表主动控制放电。
若返回 `result=8`，请求尚未实际应用，界面会报错而不会显示连接成功。
选择“EEG UDP (ESP8285)”时仍需选择控制串口；点击连接后，上位机通过该 COM 口
发送 `EEG OUTPUT UDP`，确认固件接受后释放串口并开始无线握手。先用固件的
`WIFI STATUS` 确认 ESP IP，在界面填写该 IP 和 UDP 端口 `5000`。
电脑需接入同一个 Wi-Fi；固件联网较慢时，链路状态会显示等待测试包。
上位机需要 `psutil`，会选择与 ESP 同网段且已连接的无线网卡；未连接无线时会报错，
不会改走以太网。它会用本机自动分配的
端口发送 `HELLO`，收齐 10 个测试包后发送 `START`，再按与串口相同的 `fw100`
布局校验 CRC 并显示 4 路 EEG；底部链路标签显示握手进度、帧数和错误计数。
没有 fNIRS 采集板时，固件帧内 fNIRS 字段仍为零。若固件从 ISON 自动回退到
Redmi K60 Ultra，ESP 的 IP 会变化，电脑也需切到对应网络并重新输入 IP。

当前固件的 100 字节帧支持 fNIRS 版本 `01`（CH1～CH6 单板暗场）和
`02`（十个 S-D 光学槽）。两版的四路 EEG 均持续解析。界面 fNIRS 两档切换为
“原始码·波长1/2”，只在帧标志 bit0 表示新采样时更新读数；其余 EEG 帧保持最近
一次 fNIRS 值。`01` 的槽 7～10 和未启动座子的全零填充不画为有效通道。
这些曲线是 AFE 差分 ADC 码，尚未完成 LED 波长、光强和 MBLL 标定，不能解读为
ΔHbO₂、ΔHbR 或血氧浓度。

## 运行环境
- Python 3.9.13

### 源码运行
```
.venv\Scripts\python.exe -m pip install psutil
.venv\Scripts\python.exe main.py
```

### 打包
```
.venv\Scripts\python.exe -m PyInstaller packaging/HostComputer6.spec --noconfirm
```
### ev1/ 分层模块（25 个）

| 子目录 | 类别 | 模块 | 职责 |
|---|---|---|---|
| `ev1/protocol/` | 协议/帧/契约 | contract、device_frame、frame_slicer、core_events、commands | 数据契约、CRC 与帧解析、切帧、事件总线、命令编码 |
| `ev1/algorithm/` | 算法（纯函数） | derive、fnirs、quality、band_power、imu | 光强→OD→ΔHbO/ΔHbR（MBLL）、暗噪声与质量、频带功率、姿态解算 |
| `ev1/source/` | 数据源适配 | source、text_source | 三态数据源（sim/real/dry）、文本源解析 |
| `ev1/transport/` | 通信收发 | receivers、tx | 串口/UDP/TCP 接收、下行命令发送 |
| `ev1/sync/` | 时钟/多设备 | lsl_outlet、lsl_sync、device_manager | LSL 输出与时钟同步、多设备状态管理 |
| `ev1/storage/` | 存储导出 | exporters、xdf_writer | CSV/EDF/BDF/SNIRF/XDF 导出、XDF 真实时钟校正 |
| `ev1/ui/` | 界面 | main_window、monitor_14ch、panels、replay_panel、test_panel、verify_center | PyQt5 主窗口、14 通道波形显示与放大、回放/测试/验证面板 |



