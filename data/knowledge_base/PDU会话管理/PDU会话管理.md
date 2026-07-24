# PDU会话管理

PDU（Protocol Data Unit）会话是5G系统中UE与数据网络（Data Network）之间建立的数据连接通道。一个PDU会话为UE提供了一个或多个QoS流（QoS Flow），承载不同质量要求的数据业务。PDU会话是5GS中最核心的会话管理概念。

## PDU会话类型

3GPP TS 23.501定义了以下PDU会话类型：

| 会话类型 | 说明 |
|---------|------|
| **IPv4** | UE分配IPv4地址，承载IPv4数据包 |
| **IPv6** | UE分配IPv6前缀，承载IPv6数据包 |
| **IPv4v6** | 双栈模式，同时分配IPv4地址和IPv6前缀 |
| **Ethernet** | 透明承载以太网帧，不分配IP地址，适用于企业专线等场景 |
| **Unstructured** | 透明承载非结构化数据，适用于物联网点对点通信 |

PDU会话类型由UE在发起会话建立请求时指定，SMF根据签约数据和网络策略决定是否接受。

## PDU会话生命周期

### 会话建立（Establishment）

1. UE通过AN向AMF发送PDU Session Establishment Request。
2. AMF选择合适的SMF，将会话请求转发给SMF。
3. SMF从UDM获取签约数据，选择UPF，通过PFCP建立会话。
4. SMF为UE分配IP地址/前缀，通过AMF将建立结果返回给UE。
5. 用户面通路建立完成，UE可以收发数据。

### 会话修改（Modification）

SMF或UE均可触发会话修改，用于：

- 增删QoS Flow
- 调整QoS参数（如GFBR、MFBR）
- 增加/移除UL CL或分支点
- 修改Session-AMBR

### 会话释放（Release）

会话可由UE、SMF或AN触发释放。释放时SMF撤销PFCP会话，回收IP地址，通知相关NF。

## SSC模式（Session and Service Continuity）

SSC模式决定PDU会话在锚点UPF发生变化时（如UE移动到新区域）的连续性策略：

| SSC模式 | 行为 | 适用场景 |
|---------|------|---------|
| **SSC Mode 1** | 锚点UPF保持不变，无论UE如何移动都维持同一个PDU会话。网络通过建立到该锚点的数据转发隧道保证连续性 | 固定IP需求、IMS语音、需要IP地址不变的业务 |
| **SSC Mode 2** | 当锚点需要变更时，网络释放旧PDU会话，指示UE重新建立新会话（可能使用新锚点）。存在短暂中断 | 允许短暂中断、对IP连续性无要求的业务 |
| **SSC Mode 3** | 网络先建立新PDU会话（Make-before-break），再释放旧会话，保证切换期间数据不丢失。UE短暂保持两个会话 | 需要无缝切换、对中断敏感的业务 |

根据TS 23.501 Section 5.6.9.3，SSC模式的选择由SMF根据签约数据（UDM中配置的允许SSC模式列表和默认模式）结合UE请求确定。所有UE必须支持SSC Mode 1，SSC Mode 2和3为可选支持。

## DNN（Data Network Name）

**DNN（Data Network Name）** 是5G中标识目标数据网络的参数，功能上等同于4G中的**APN（Access Point Name）**。UE在建立PDU会话时携带DNN，SMF根据DNN选择对应的UPF和数据网络接口。

- DNN由两部分组成：`<Network Identifier>.<Operator Identifier>`
- 网络标识符标识外部数据网络（如"internet"、"ims"）
- 运营商标识符可选，格式为`mnc<MNC>.mcc<MCC>.3gppnetwork.org`
- 当UE未提供DNN时，SMF使用UDM中签约的默认DNN

## 参考规范

- **3GPP TS 23.501**：System Architecture for the 5G System — PDU Session（Section 5.6.9）、SSC Mode Selection（Section 5.6.9.3）
- **3GPP TS 23.502**：Procedures for the 5G System — PDU Session Management procedures
- **3GPP TS 24.501**：NAS protocol for 5GS — PDU Session Establishment信令
