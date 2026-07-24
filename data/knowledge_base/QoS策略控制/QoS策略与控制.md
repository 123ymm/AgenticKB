# QoS策略与控制

5G系统引入了全新的QoS（Quality of Service）框架，以QoS Flow为核心粒度，取代了4G EPS中基于EPS Bearer的QoS模型。这一设计使5G能够支持更精细的服务质量区分和更灵活的策略控制。

## 5G QoS模型

### QoS Flow与QFI

5G中最小的QoS区分粒度是**QoS Flow**。每个QoS Flow由一个**QFI（QoS Flow Identifier）**唯一标识，QFI的取值范围为0~63（其中0~127中非标准化值可用于扩展）。一个PDU会话内可以包含多个QoS Flow，每个Flow承载不同QoS要求的数据流。

QoS Flow分为两类：

| 类型 | 说明 |
|------|------|
| **GBR（Guaranteed Bit Rate）** | 保证比特速率，网络为该Flow预留资源。包括GBR和Delay-Critical GBR两种子类型 |
| **Non-GBR** | 不保证比特速率，共享网络资源，按尽力而为（Best Effort）方式处理 |

### 5QI（5G QoS Identifier）

**5QI**是一个标量值，作为索引指向一组预定义的QoS特征参数。标准化的5QI值与QoS特征之间是一对一映射关系，定义在3GPP TS 23.501 Table 5.7.4-1中。

5QI对应的关键QoS特征包括：

- **资源类型**（Resource Type）：GBR / Non-GBR / Delay-Critical GBR
- **优先级**（Priority Level）：数值越小优先级越高
- **包时延预算**（Packet Delay Budget，PDB）：UE与UPF N6终结点之间的时延上限
- **包错误率**（Packet Error Rate，PER）：允许的最大丢包率

## 标准化5QI值示例

以下为TS 23.501 Table 5.7.4-1中的典型标准化5QI值：

### GBR类型

| 5QI | 默认优先级 | 包时延预算 | 包错误率 | 典型业务 |
|-----|-----------|-----------|---------|---------|
| 1 | 20 | 100 ms | 10^-2 | 会话类语音（Conversational Voice） |
| 2 | 40 | 150 ms | 10^-3 | 会话类视频（实时流媒体） |
| 3 | 30 | 50 ms | 10^-3 | 实时游戏、V2X消息、AR/VR |
| 4 | 50 | 300 ms | 10^-6 | 非会话类视频（缓冲流媒体） |
| 67 | 15 | 100 ms | 10^-3 | 任务关键型视频 |

### Non-GBR类型

| 5QI | 默认优先级 | 包时延预算 | 包错误率 | 典型业务 |
|-----|-----------|-----------|---------|---------|
| 5 | 10 | 100 ms | 10^-6 | IMS信令 |
| 6 | 60 | 300 ms | 10^-6 | 缓冲流媒体、TCP业务（WWW、邮件、FTP） |
| 7 | 70 | 100 ms | 10^-3 | 语音、实时视频、交互式游戏 |
| 8 | 80 | 300 ms | 10^-6 | 缓冲流媒体、TCP业务 |
| 9 | 90 | 300 ms | 10^-6 | 默认QoS Flow（Default Bearer） |

### Delay-Critical GBR类型

| 5QI | 默认优先级 | 包时延预算 | 包错误率 | 默认最大数据突发量 | 典型业务 |
|-----|-----------|-----------|---------|------------------|---------|
| 82 | 19 | 10 ms | 10^-4 | 255 bytes | 离散自动化 |
| 84 | 24 | 30 ms | 10^-5 | 1354 bytes | 智能交通系统 |
| 85 | 21 | 5 ms | 10^-5 | 255 bytes | 配电（高压）、远程驾驶 |

值域128~254为运营商自定义（非标准化）5QI。

## ARP（Allocation and Retention Priority）

**ARP**用于指示QoS Flow在资源争用时的优先级，包含三个参数：

- **优先级**（Priority）：1~15，数值越小优先级越高
- **抢占能力**（Pre-emption Capability）：该Flow是否可以抢占低优先级资源
- **抢占脆弱性**（Pre-emption Vulnerability）：该Flow的资源是否可被高优先级Flow抢占

ARP主要用于接纳控制决策，不直接影响数据包转发时的调度优先级（调度优先级由5QI中的Priority Level决定）。

## PCF策略控制与PCC规则

**PCF（Policy Control Function）** 是5GC中的策略控制节点，通过N7接口向SMF下发**PCC（Policy and Charging Control）规则**。PCC规则定义于3GPP TS 23.503，主要包含：

- **QoS参数**：5QI、ARP、GFBR/MFBR（对于GBR Flow）
- **门控状态**（Gate Status）：Open/Close，控制数据流是否允许通过
- **计费相关参数**：计费键值（Charging Key）、计费方法
- **应用检测规则**：指示UPF执行应用检测并上报
- **流量描述**：SDF（Service Data Flow）模板，用于匹配特定数据流

SMF收到PCC规则后，将其转换为PFCP规则（PDR、FAR、QER、URR）下发给UPF执行。

## 参考规范

- **3GPP TS 23.501**：System Architecture for the 5G System — QoS模型（Section 5.7）、标准化5QI表（Table 5.7.4-1）
- **3GPP TS 23.503**：Policy and Charging Control Framework for the 5G System — PCC规则定义
- **3GPP TS 29.512**：N7接口（SMF-PCF）策略控制信令
