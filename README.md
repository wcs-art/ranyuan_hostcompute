# EV1 睡眠监测上位机

燃元科技 EV1 睡眠监测设备的上位机软件。采集 EEG 4 通道 + fNIRS 10 通道（760/850 nm 双波长）数据，
实时显示、记录，并导出为 CSV / EDF / BDF / SNIRF / XDF。

## 运行环境
- Python 3.9.13（线上编译加密系统同版本；代码已适配，注解不依赖 3.10 语法）

### 源码运行
```
.venv\Scripts\python.exe main.py
```

### 打包
```
.venv\Scripts\python.exe -m PyInstaller packaging/HostComputer6.spec --noconfirm
```



