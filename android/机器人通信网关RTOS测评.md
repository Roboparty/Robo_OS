# 机器人通信网关 Phase 1 测评方案：STM32 硬件覆盖 + Zephyr + RK3588

- 更新日期：2026-11-12
- 状态：硬件资料核对与测评计划，尚无本方案的板级性能实测结论
- 代码核对：2026-11-12 对照 `cannectivity/README.md`、`app/boards/*` 与 `soc/roboparty/stm32g5x`；本节只补充仓内架构/构建状态，不改写原测评结论与实测记录。
- MCU：STM32 MCU；主机：RK3588 Linux；固件：Zephyr + cannectivity
- 目标：至少 2 路原生 CAN-FD，仲裁段 1 Mbps、数据段 5 Mbps、64 B 帧、多通道并发
- 原则：**硬件支持就进入候选池，软件由项目适配；Zephyr 有无节点、驱动是否现成，不作为硬件淘汰理由。**

---

## 1. 范围与判断方法

Phase 1 比较芯片及其网关实现，不比较 RTOS，不按现成开发板限制芯片覆盖。开发板只是验证载体，实际封装、引脚和外部 PHY 仍须核实。

分别记录三个结论，不能相互代替：

| 结论 | 判断依据 |
|---|---|
| 硬件候选 | 芯片至少 2 路 FDCAN、有 USB device、目标封装可以同时引出接口 |
| 软件适配状态 | 未检查 / 需新增 SoC / 需补驱动 / 已构建 / 已板测，绑定实际 commit |
| 性能达标 | 指定时钟、PHY、固件和主机下的吞吐、延迟、丢帧、恢复与稳定性实测 |

支持 CAN-FD 不自动证明 5 Mbps 链路已可用。核对 FDCAN 内核时钟、位时序、采样点、TDC、器件勘误和收发器延迟后，再进行 1/5 Mbps 验证。不能用 CPU 主频代替 FDCAN 内核时钟。

USB FS/HS 都可入围。USB FS 达不到项目负载时，应保留其无丢帧能力边界，并判定它是否满足最终用途，不能把“允许入围”写成“保证满载通过”。

---

## 2. STM32 硬件覆盖表

### 2.1 读表口径

- 表内容量沿用 ST 的 KB/MB 标注；KB 按 1024 B 计算。Flash 写“上限”时，不表示所有容量后缀都具备该容量。
- SRAM 列明确是否含 CCM/TCM/备份域；Cache、USB FIFO、CAN message RAM 不重复加到应用 SRAM。
- FDCAN 是芯片硬件实例数，不是 Zephyr 当前 DTS 节点数。封装可能限制实际可引出的通道数。
- HS-E：USB HS 控制器需要外接 ULPI HS PHY；仅连接内部 FS PHY 时仍是 12 Mbps。
- HS-I：有内置 HS PHY；仍须满足供电、时钟与封装条件，并以主机实际枚举速度验证。
- 本表覆盖已核对的产品线，不宣称逐一验证了全部订货后缀；确定 BOM 时必须追加完整料号、封装、数据手册修订号和勘误版本。

### 2.2 G0 / G4 / G5 / C5 / U3

| 产品线 | 内核 / 最高主频 | 片内程序 Flash | SRAM | FDCAN | USB device | 入围与主要约束 |
|---|---|---:|---:|---:|---|---|
| STM32G0B1 / G0C1 | M0+ / 64 MHz | 最高 512 KB | 144 KB | 2 | FS | 入围；核对双 CAN 与 USB 同时引脚分配、CPU 中断负载 [S1] |
| STM32G471 | M4F / 170 MHz | 最高 512 KB | 112 KB：96 + 16 CCM | 2 | FS | 入围；原表漏项，RM0440 明列 FDCAN1/2 [S2] |
| STM32G491 / G4A1 | M4F / 170 MHz | 最高 512 KB | 112 KB：96 + 16 CCM | 2 | FS | 入围；小 FIFO 与 FS 聚合能力要测 [S2] |
| STM32G473 / G474 / G483 / G484 | M4F / 170 MHz | 最高 512 KB | 128 KB：96 + 32 CCM | 3 | FS | 入围；硬件明确为 3 路，不再写“可能支持”；各封装核对引出 [S2] |
| STM32G554 / G564 | M33F / 280 MHz | 最高 512 KB | 112 KB ECC：32 + 64 + 16 | 3 | FS | 入围；新系列，补 SoC/驱动亦可，关注实际供货与 SDK 适配 [S3] |
| STM32G553 / G563 | M33F / 280 MHz | 最高 512 KB | 112 KB ECC | 最高 3 | FS，核对封装 | 列入跟踪候选；所查 G553CE/G563RE 产品页为 Proposal，不能当作现货量产选项 [S3] |
| STM32C532 / C542 | M33F / 144 MHz | 最高 256 KB | 64 KB | 2 | FS | 入围；应用 RAM 最紧的一档，DFU 分区需单独预算 [S4] |
| STM32C593 / C5A3 | M33F / 144 MHz | 最高 1 MB | 256 KB | 2 | FS | 入围；另有 Ethernet/外存扩展，仍先测 USB 网关 [S4] |
| STM32U3B5 / U3C5 | M33F / 96 MHz | 最高 2 MB | 640 KB | 2 | FS | 入围；不能把整个 U3 系列归为单路；低功耗与唤醒延迟分别评估 [S5] |

**G5 专项说明**：ST 的发布公告日期为 2026-11-04，原文“STM32 没有 G5 系列”错误，予以撤销。G554/G564 的 112 KB 已包含 32 KB routine-booster SRAM，不能再加一次成为 144 KB。G534/G544 是另一档：60 KB ECC SRAM（20 + 40）、最高 256 KB Flash、1 路 FDCAN，不满足本方案双路门槛。G553/G563 的产品状态单列，不以系列发布新闻替代具体料号供货确认。[S3]

### 2.3 H5

H5 均为 Cortex-M33，最高 250 MHz。下表主 SRAM 不含独立备份域；不得把整个 H5 系列统一写成 USB FS，也不得把所有 H5E/F 都写成 HS。[S6]

| 产品线 | 片内程序 Flash | 主 SRAM | FDCAN | USB device | 入围与主要约束 |
|---|---:|---:|---:|---|---|
| STM32H523 / H533 | 最高 512 KB | 272 KB | 2 | FS | 入围；H523 另有较小 Flash 档 |
| STM32H543 / H553 | 最高 1 MB | 304 KB | 2 | FS | 入围；原表漏项 |
| STM32H563 / H573 | 最高 2 MB | 640 KB | 2 | FS | 入围；H563 另有 1 MB 档 |
| STM32H5E4 / H5F4 | 最高 4 MB | 1536 KB | 3 | FS | 入围；大 RAM 不改变 USB FS 吞吐上限 |
| STM32H5E5 / H5F5 | 最高 4 MB | 1536 KB | 3 | HS-I + FS | 入围；内置 HS PHY，可作为三路 HS 验证对象 |

H562 为 640 KB SRAM、最高 2 MB Flash、USB FS，但**只有 1 路 FDCAN**；H503 为 32 KB SRAM、128 KB Flash、1 路 FDCAN。二者留在覆盖记录中，不进入原生双路候选池。[S6]

### 2.4 H7 / N6

| 产品线 | 内核 / 最高主频 | 片内程序 Flash | SRAM 标称总量 | FDCAN | USB device | 入围与主要约束 |
|---|---|---:|---:|---:|---|---|
| STM32H723 / H733 / H725 / H735 | M7 / 550 MHz | 最高 1 MB | 564 KB，含 4 KB 备份 | 3 | HS-E | 入围；H733 同样是硬件候选；留出 ULPI 引脚 [S7] |
| STM32H730 | M7 / 550 MHz | 128 KB | 564 KB，含 4 KB 备份 | 3 | HS-E | 入围；小 Flash，外存/裁剪/升级布局须明确 [S7] |
| STM32H742 | M7 / 480 MHz | 最高 2 MB | 692 KB，含 4 KB 备份 | 2 | HS-E + FS | 入围；不能套 H743 的 SRAM 容量 [S8] |
| STM32H743 / H753 | M7 / 480 MHz | 最高 2 MB | 1060 KB，含 4 KB 备份，常宣传约 1 MB | 2 | HS-E + FS | 入围；注意 DMA 可达性、Cache 一致性 [S8] |
| STM32H750 | M7 / 480 MHz | 128 KB | 1060 KB：864 + 192 TCM + 4 备份 | 2 | HS-E + FS | 入围；不要用未保证的片内 Flash 地址 [S8] |
| STM32H745 / H747 / H755 / H757 | M7 480 + M4 240 MHz | 最高 2 MB | 1060 KB：864 + 192 TCM + 4 备份 | 2 | HS-E + FS | 入围；共享系统 RAM，双核不增加 FDCAN 数，先单核网关 [S9] |
| STM32H7A3 / H7B3 | M7 / 280 MHz | 最高 2 MB | 1380 KB：1184 + 192 TCM + 4 备份 | 2 | HS-E | 入围；原文“无 FDCAN”错误；小封装 ULPI 有限制 [S10] |
| STM32H7B0 | M7 / 280 MHz | 128 KB | 1380 KB：1184 + 192 TCM + 4 备份 | 2 | HS-E | 入围；大 RAM、小 Flash，需设计启动/升级存储 [S10] |
| STM32H7R3 / H7R7 / H7S3 / H7S7 | M7 / 600 MHz | 64 KB bootflash | 620 KB | 2 | HS-I + FS | 入围；通常需外部程序存储，记录 XIP/搬入 RAM 的延迟差异 [S11] |
| STM32N645 / N647 / N655 / N657 | M55 / 800 MHz | 无内置用户程序 Flash | 4.2 MB 系统 SRAM + 8 KB 备份 | 最高 3，部分封装为 2 | HS-I，最高 2 个控制器 | 入围；外部启动存储、封装和电源成本单列，不因无 Zephyr 节点排除 [S12] |

### 2.5 其余系列与不满足条件的产品

| 系列 / 产品 | 硬件判断 | 本阶段处理 |
|---|---|---|
| G431 / G441 | 1 路 FDCAN，32 KB SRAM | 不满足原生双路 |
| G534 / G544 | 1 路 FDCAN，60 KB SRAM | 不满足原生双路，不能与 G55x/G56x 合并 [S3] |
| C552 / C562 | 1 路 FDCAN，128 KB SRAM | 不满足原生双路 [S4] |
| C531 / C551 / C591 | 无 FDCAN | 排除；不能把 C5 全系列都算双路 [S4] |
| C092 | 1 路 FDCAN | 不满足双路，但原文“整个 C0 无 FDCAN”错误 [S13] |
| U375 / U385 | 1 路 FDCAN，256 KB SRAM | 不满足双路；与 U3B5/U3C5 分开 [S5] |
| U5 已核对产品线 | 1 路 FDCAN | 不满足双路；大 SRAM 或 USB HS 不会补出第二路 [S14] |
| L552 / L562 | 1 路 FDCAN | 不满足双路 |
| 其余 C0/G0、U0、L0/L1/L4、F0/F1/F2/F3/F4/F7、WB/WBA/WL | 未纳入原生双路 FDCAN 候选；部分只有经典 CAN 或没有 CAN | 按具体料号硬件手册复核，不用 DTS 缺失作为理由 |
| STM32MP1 / MP2 / MP13 等 MPU | 不属于本阶段 STM32 MCU 范围 | 即使有 CAN-FD 也另立 MPU 方案 |

外接 SPI CAN-FD 控制器属于硬件扩展路线，不能称作“软件兼容出第二路”。本阶段以 MCU 原生双路为基线，避免把外置控制器延迟混进同一芯片对比。

---

## 3. SRAM、Flash 和缓冲预算

### 3.1 SRAM 必须分开记账

| 内存类别 | 用途 | 评估要求 |
|---|---|---|
| 通用 SRAM / AXI SRAM | Zephyr 对象、栈、USB/CAN 软件队列、日志 | 给出每个 bank 的大小、地址、DMA 可达性及剩余量 |
| CCM / ITCM / DTCM | 低延迟代码或数据 | 已包含在上表总量中；可达性随总线和别名地址变化，不一概认为 DMA 可用或不可用 |
| 备份 SRAM / 寄存器 | 掉电域状态、故障记录 | 不作为正常收发大队列容量 |
| FDCAN message RAM | 硬件过滤器、RX FIFO、TX FIFO、事件 | 专用内存；不能拿通用 SRAM 任意扩大硬件 FIFO |
| USB PMA / FIFO | 端点硬件缓冲 | 不等于应用 SRAM；队列仍可能在主 SRAM 占另一份 |
| Cache / 外部 RAM | 加速 / 扩容 | Cache 不算可分配 SRAM；外部 RAM 单列容量及最坏访问延迟 |

几个具体例子：

- G473/474/483/484：80 KB SRAM1 + 16 KB SRAM2 + 32 KB CCM = 128 KB；G471/G491/G4A1 的 CCM 为 16 KB，总计 112 KB。[S2]
- G554/G564：32 KB SRAM1 + 64 KB SRAM2 + 16 KB SRAM3 = 112 KB，已包含 routine booster。[S3]
- H742：384 KB AXI + 32 KB SRAM1 + 16 KB SRAM2 + 64 KB SRAM4 + 64 KB ITCM + 128 KB DTCM + 4 KB 备份 = 692 KB；H743 对应 AXI 为 512 KB、SRAM1/2 各 128 KB、另有 32 KB SRAM3，总计 1060 KB。以 DS12110 的内存表为准。[S8]
- H72x/73x：标称 564 KB 中含 TCM/系统 RAM 重映射区和 4 KB 备份；重映射前后不能重复统计。[S7]

### 3.2 软件缓冲预算示例

以下是**规划假设，不是现有固件实测占用**：每个软件帧槽按 96 B 预留（64 B payload + 元数据/对齐）；实际必须用构建中的结构大小和队列实现重算。

| 配置 | 计算 | 仅帧槽占用 |
|---|---|---:|
| 2 路，每路 RX/TX 各 64 槽 | 2 × 2 × 64 × 96 | 24 KB |
| 2 路，每路 RX/TX 各 128 槽 | 2 × 2 × 128 × 96 | 48 KB |
| 3 路，每路 RX/TX 各 128 槽 | 3 × 2 × 128 × 96 | 72 KB |

此外还要计入 USB 在途请求、echo 跟踪、RTOS 内核对象、线程/中断栈、日志和升级缓冲。共用帧池时按实际所有权计数，避免重复计入，也避免遗漏复制产生的第二份。

因此 C532/C542 的 64 KB SRAM 不能直接套 48 KB 帧队列配置；G4/G5 的 112/128 KB 也必须算余量。大 SRAM 能吸收短突发，不能解决长期输入速率高于 USB 输出速率的问题。给出队列高水位和最长停顿吸收时间，按“可用槽数 / 实测输入 fps”估算并验证。

### 3.3 CAN message RAM 与 Flash

不再笼统写“H5/H7 全部共享且任意分配”。各系列采用的 FDCAN 实现不同：

- H742/H743 等及 H7A3/B0：共享 10 KB message RAM；多实例区域必须不重叠。[S8][S10]
- N6：最高三实例共享 10 KB message RAM。[S12]
- H7R/S 的 R 系数据手册记载 2 KB message RAM，不能套用传统 H7 的 10 KB 配置。[S11]
- G0/G4、H5、G5、C5/U3：逐目标核对固定分区或可配置分区、每路 FIFO 深度、过滤器数；不能复制 H743 的配置推断容量。具体数值在目标 RM/驱动核对后填入实现记录。
- 在适用的 M_CAN RAM 布局中，64 B 数据元素还含 8 B 头，共 72 B；过滤器、TX event 等另占空间。[S15]

Flash 预算必须列出 bootloader、主镜像、备用镜像、元数据/交换区、NVS 和升级中断恢复策略。双 bank Flash 不等于自动回滚。H730/H750/H7B0 的 128 KB 和 H7R/S 的 64 KB bootflash 要单独处理；N6 需外部非易失存储。允许裁剪或外存方案，不因缺现成 MCUboot 配置淘汰芯片。

---

## 4. 软件适配路线

硬件入围后再评估工作量，所有候选使用同一套目标功能。尚未对每颗芯片执行 Zephyr 构建；九个参考板已有离线构建记录（见下），但都不能标记“overlay 即可”或“已板测”。

| 适配层 | 必须完成 |
|---|---|
| SoC / 启动 | 启动代码、CMSIS/HAL、链接布局、时钟、电源域、Cache/MPU、TrustZone（适用时） |
| DTS / pinctrl | FDCAN 全实例的 reg/IRQ/clock/reset、引脚复用、USB/PHY、每路 message RAM |
| CAN 驱动 | FD+BRS、1/5 Mbps 位时序、TDC、过滤器、并发收发、TX 完成、时间戳、错误状态 |
| USB 驱动 | device 栈、FS/HS PHY、端点配置、DMA、缓冲所有权、拔插和取消请求 |
| cannectivity | channel0/1，三路型号追加 channel2；gs_usb 请求、echo、时间戳、状态和溢出报告 |
| 存储 / 升级 | 内外 Flash、NVS、DFU、镜像验证、断电恢复和回滚 |
| 维护 | 固定 Zephyr/cannectivity/HAL/SDK 版本，记录补丁与上游状态 |

仓内双路参考为 [candlelightfd_stm32g0b1xx_dual.overlay](cannectivity/app/boards/candlelightfd_stm32g0b1xx_dual.overlay)，已声明 fdcan1/2 两个通道。2026-11-12 源码核对：`cannectivity/app/boards/` 另有 `nucleo_g0b1re`、`nucleo_g474re`（fdcan1/2/3）、`nucleo_g491re`、`nucleo_g564re`（fdcan1/2/3）、`nucleo_h533re`、`nucleo_h563zi`、`nucleo_c542rc`、`nucleo_c5a3zg`、`nucleo_u3c5zi_q` 九个 STM32 Nucleo 参考 overlay；[cannectivity/README.md](cannectivity/README.md) 记录 2026-11-12 对九块板完成离线构建与生成配置检查，链接 RAM 19,768–23,188 B，未进行 USB 枚举、CAN 流量或吞吐实测。这些仍只证明参考配置与构建，不证明当前目标板卡已完成压测。先复用相邻 SoC 与驱动，再补缺口；软件兼容范围允许新增 SoC、驱动与板级支持。

对于 G5 这类新系列，仓内已有实验性树外移植 [soc/roboparty/stm32g5x](cannectivity/soc/roboparty/stm32g5x) 与 [boards/roboparty/nucleo_g564re](cannectivity/boards/roboparty/nucleo_g564re)。README 明确其目标为 STM32G564RE、TZEN=0、固定 64 MHz CPU / 80 MHz FDCAN，无 bootloader 时钟继承、电源管理或 Flash 写驱动；其他 G5 SKU 尚不是构建目标。仍需逐项核对所选 SDK/HAL 接口与 Zephyr STM32 驱动的对接，不能假设与 G4 寄存器、时钟和 HAL 完全相同。

---

## 5. 测评标准

### 5.1 分阶段验收

1. **硬件入围**：原生双路 FDCAN、USB device、接口同时引出，目标位时序与器件限制可满足。
2. **功能基线**：Zephyr 启动、双路 gs_usb 枚举、FD+BRS 收发、错误上报、时间戳与重连；三路芯片增加三路测试。
3. **性能验收**：在约定负载下达到丢帧、延迟与资源标准。
4. **产品化验收**：24 h 稳定性、升级回滚、量产烧录、许可证及维护闭环。

尚未适配的芯片记为“待适配”，不能在第一阶段因软件缺失淘汰。软件实现完成后若仍无法满足目标，则附实测依据评估成本或停止该路线。

### 5.2 测试矩阵

| 测试 | 条件 | 必须输出 |
|---|---|---|
| CAN 基线 | 每路 1/5 Mbps，FD+BRS，64 B，分别 TX/RX | 实际时钟、位时序、采样点、TDC、fps、错误计数 |
| 多路并发 | 两路同时；三路芯片另测三路 | 各路 fps、payload Mbps、总线占用率、丢帧/乱序 |
| USB 聚合 | CAN→USB、USB→CAN、混合方向分别测 | 实际枚举速率、USB 字节率、echo 开销、端点队列高水位 |
| 延迟 | 空载、典型负载、最大无丢帧负载 | p50/p99/max，定义起止点、时钟来源和同步误差 |
| 实时性 | GPIO + 逻辑分析仪，连续 30 min | IRQ 响应、处理时间、调度抖动；不与 USB RTT 混为一项 |
| 错误恢复 | 隔离台架上的 bus-off、错误注入、拔插 | 状态上报、恢复时间、队列清理、是否需重启 |
| 稳定性 | 24 h soak，固定负载 | 总帧数、丢帧、重启、RAM/栈高水位、温度与功耗 |
| 升级 | 正常升级、各关键阶段断电 | 镜像校验、恢复结果、回滚耗时 |
| 资源 | release 构建 + 运行态 | Flash 分区、各 SRAM bank 占用、栈余量、CPU 利用率 |

CAN 单总线本身不是全双工；“双向测试”是该总线上双方竞争仲裁。数据段 5 Mbps 也不等于每路持续 5 Mbps 的有效 payload。必须计入仲裁、CRC、填充、帧间隔以及 USB/gs_usb 开销，不能简单用 2 × 5 与 USB FS 12 Mbps 比较。

RK3588 使用 SocketCAN、can-utils、python-can、usbmon；确认 CONFIG_CAN、CONFIG_CAN_RAW、CONFIG_CAN_GS_USB 与实际 CAN-FD 支持。cannelloni 网络转发作为独立场景，不能混入基础 USB 网关延迟。

### 5.3 通过标准与评分

- 双路 1/5 Mbps、64 B，在预先冻结的目标负载下连续 30 min 无丢帧；以序号与收发计数核对，另查硬件溢出。
- 扫描负载，分别给出每种方向的最大持续无丢帧 fps；低于业务目标即不能宣称性能通过。
- 业务目标 fps、允许的 p99/max 延迟与抖动阈值目前**待定义**；冻结前只报告基线，不输出综合“通过”。
- 24 h 无意外复位、无持续内存增长；升级断电可恢复至有效镜像。
- bus-off 自动恢复只在隔离测评台架验证；实际机器人使用受控恢复策略。

原有加权维度保留：硬件资源 20%、多通道 CAN-FD 25%、实时性 15%、工具链/DFU/CI 10%、协议兼容 10%、适配成本 10%、许可/安全/维护 10%。每项 0–3 分，加权结果范围 0–3。**评分用于排序，不替代硬指标；未测项标 N/A，不按 0 分或满分处理。**

---

## 6. 封装、PHY 与硬件成本

- 为每个候选完整料号检查 USB、两组/三组 CAN TX/RX、SWD、晶振、boot、外存和 ULPI 的同时引出；开发板只引一路不代表芯片仅一路。
- G0B1 的 PA11/PA12 复用冲突通过选择有效备用 CAN 引脚解决，必须核对封装；“只用 FDCAN2”不能满足原生双路目标。
- 传统 H7 的 HS-E 必须把外部 PHY、晶振/时钟、供电、布线和引脚成本计入；USB-C 连接器不代表 HS。
- 每路采用满足 5 Mbps 与传播延迟要求的 CAN-FD 收发器；隔离器及隔离电源的延迟/成本单列。
- 终端、TVS/ESD、共模器件、线缆与接插件按信号完整性验证选择，记录独立地和屏蔽连接方式。
- 记录芯片价格的渠道/日期/数量、生命周期、供货状态、PCB 面积、实测功耗和温升，不能只比较裸芯片售价。

---

## 7. 落地顺序

1. 全部硬件候选保留；为有条件获取的型号建立完整料号、封装、DS/RM/勘误与软件版本记录。
2. 并行规划不同能力档的代表：G0/G4 的 FS 基线、G554/G564 新平台、H5 的不同 SRAM 档、H7 的外置 HS PHY、H5E5/F5 或 H7R/S 的内置 HS PHY；C5、U3、N6 同样保留适配路线。
3. 先完成时钟、引脚、内存预算及最小固件构建，再开始板级功能测试。
4. 跑通双路后再扩三路；复用同一 RK3588、对端、线缆、负载和测量方法。
5. 用资源/吞吐/延迟/成本结果选型；适配进度、规格声明、已构建和已板测分列，不能混写。

---

## 8. Phase 2 与 RT-Thread 预留

Phase 1 固定 Zephyr；HPM、NXP、Renesas 及 RT-Thread 留到后续。保持主机 SocketCAN 测试可复用，协议语义、硬件适配、板级配置和测试分层。

cannectivity 使用 Zephyr 的 USB/CAN/设备模型，跨 RTOS 时 USB 控制请求、批量传输、CAN API、并发、时间戳与升级路径仍需适配，不能承诺“只加芯片配置、不动其他代码”。RT-Thread 的工期须在目标 BSP 核实后估算。

---

## 9. 资料与证据

以下为本次查询的 ST 官方来源；网页会更新。冻结具体目标时记录下载文件修订号与哈希。资料证明规格，不证明本方案已完成硬件验证。

- [S1] [STM32G0B1 产品资料](https://www.st.com/en/microcontrollers-microprocessors/stm32g0b1cb.html)：144 KB SRAM、双 FDCAN；G0C1 需按完整料号复核容量/封装。
- [S2] [RM0440 Rev 9](https://www.st.com/resource/en/reference_manual/dm00355726-stm32g4-series-reference-manual-stmicroelectronics.pdf)：Table 2（产品差异、SRAM、FDCAN、USB）。
- [S3] [G5 发布公告](https://newsroom.st.com/media-center/press-item.html/stmicroelectronics-reveals-advanced-microcontrollers-for-real-time-power-control-in-ai-datacenters-and-robotics-p4797.html)、[G5 产品概览](https://www.st.com/resource/en/product_presentation/microcontrollers-stm32g5-series-product-overview.pdf)、[G554RE](https://www.st.com/en/microcontrollers-microprocessors/stm32g554re.html)、[G534RC](https://www.st.com/en/microcontrollers-microprocessors/stm32g534rc.html)、[G544RC](https://www.st.com/en/microcontrollers-microprocessors/stm32g544rc.html)、[G553CE](https://www.st.com/en/microcontrollers-microprocessors/stm32g553ce.html)、[G563RE](https://www.st.com/en/microcontrollers-microprocessors/stm32g563re.html)。
- [S4] [C532RC 产品资料](https://www.st.com/en/microcontrollers-microprocessors/stm32c532rc.html)、[C5 系列介绍](https://www.st.com/content/ccc/resource/training/technical/product_training/group1/dd/a8/7b/67/4d/f3/4f/85/stm32c5-series-introduction/files/stm32c5-series-introduction.pdf/jcr%3Acontent/translations/en.stm32c5-series-introduction.pdf)、[C593 数据手册](https://www.st.com/resource/en/datasheet/stm32c593rg.pdf)。
- [S5] [U3 系列产品表](https://www.st.com/en/microcontrollers-microprocessors/stm32u3-series.html)、[U3C5ZI 产品资料](https://www.st.com/en/microcontrollers-microprocessors/stm32u3c5zi.html)。
- [S6] [H5 系列产品表](https://www.st.com/en/microcontrollers-microprocessors/stm32h5-series.html)、[H562/H563 数据手册](https://www.st.com/resource/en/datasheet/stm32h562ig.pdf)。
- [S7] [H725 数据手册 DS13311](https://www.st.com/resource/en/datasheet/stm32h725rg.pdf)、[H733ZG](https://www.st.com/en/microcontrollers-microprocessors/stm32h733zg.html)、[H730AB](https://www.st.com/en/microcontrollers-microprocessors/stm32h730ab.html)。
- [S8] [H742/H743 数据手册 DS12110 Rev 11](https://www.st.com/resource/en/datasheet/stm32h742zi.pdf)：SRAM 表及 FDCAN/USB 章节；[H750 DS12556 Rev 8](https://www.st.com/resource/en/datasheet/stm32h750vb.pdf)；[RM0433](https://www.st.com/resource/en/reference_manual/dm00314099-stm32h742-stm32h743753-and-stm32h750-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)。
- [S9] [H7 系列产品概览](https://www.st.com/resource/en/product_presentation/microcontrollers_stm32h7_series_product_overview.pdf)、[H745 DS12923 Rev 3](https://www.st.com/resource/en/datasheet/stm32h745zi.pdf)。
- [S10] [H7A3 DS13195 Rev 8](https://www.st.com/resource/en/datasheet/stm32h7a3zi.pdf) §3.44/3.45；[H7B0 DS13196 Rev 7](https://www.st.com/resource/en/datasheet/stm32h7b0vb.pdf) §3.46/3.47；[H7B3 数据手册](https://www.st.com/resource/en/datasheet/stm32h7b3zi.pdf)。
- [S11] [H7R3/R7 DS14360 Rev 6](https://www.st.com/resource/en/datasheet/stm32h7r3l8.pdf)：FDCAN、USB 及存储章节；[H7R3/S3 产品线](https://www.st.com/en/microcontrollers-microprocessors/stm32h7r3-7s3.html)。
- [S12] [N6 DS14791 Rev 11](https://www.st.com/resource/en/datasheet/stm32n657l0.pdf)：产品/封装差异表及 §3.50–3.52。
- [S13] [C0 RM0490](https://www.st.com/resource/en/reference_manual/rm0490-stm32c0-series-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)：C092 FDCAN 章节。
- [S14] [U5 迁移指南 AN5766](https://www.st.com/content/st_com/en/technical-documents/an5766-migrating-within-stm32u5-series-microcontrollers-stmicroelectronics_asset/chunk-001-topic-an5766-hardware-migration-guide.html)。
- [S15] [AN5348：STM32 FDCAN 实现与 message RAM](https://www.st.com/content/st_com/en/technical-documents/an5348-introduction-to-fdcan-peripherals-for-stm32-mcus-stmicroelectronics_asset/chunk-002-topic-an5348-implementation-can-fd.html)；其中 superset 描述不能直接推广到全部型号。
