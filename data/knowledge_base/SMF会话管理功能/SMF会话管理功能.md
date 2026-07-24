# SMF会话管理功能

SMF（Session Management Function，会话管理功能）是5G核心网中的关键控制面网络功能，负责管理PDU会话的完整生命周期，包括会话的建立、修改和释放。SMF是连接控制面策略与用户面执行的枢纽，通过与多个NF交互实现端到端的会话管理。

## SMF在5GC中的角色

SMF在5GC中承担以下核心职责：

- **PDU会话生命周期管理**：建立、修改、释放PDU会话
- **UE IP地址管理**：为UE分配、续约、释放IP地址或IPv6前缀
- **UPF选择与控制**：根据S-NSSAI、DNN和拓扑信息选择UPF，通过PFCP控制其行为
- **DN（Data Network）接入控制**：管理UE到外部数据网络的连接

一个SMF可以同时服务多个PDU会话。在网络切片部署中，不同切片可能使用不同的SMF实例。

## 会话管理功能详述

### PDU会话建立

UE通过AMF向SMF发送PDU Session Establishment Request，SMF执行以下操作：

1. 从**UDM**获取用户的签约数据（允许的DNN、S-NSSAI、SSC模式等）。
2. 选择合适的**UPF**，基于DNN、S-NSSAI、UPF负载和位置策略进行决策。
3. 为UE分配**IP地址/前缀**（IPv4通过DHCPv4或静态配置，IPv6通过无状态地址自动配置SLAAC或DHCPv6）。
4. 通过**PFCP Session Establishment**向UPF下发包检测规则（PDR）、转发规则（FAR）、QoS执行规则（QER）等。
5. 将会话建立结果通过AMF返回给UE。

### 会话修改

SMF或UE均可触发会话修改，典型场景包括：

- QoS Flow的增删或参数变更（如GFBR调整）
- 添加/移除UL CL（Uplink Classifier）实现业务分流
- 切换场景下的UPF重定位
- ARP参数更新

### 会话释放

会话释放可由UE发起、SMF发起（如策略变更、签约到期）或AN发起（如无线连接丢失）。SMF负责撤销PFCP会话、回收IP地址、通知PCF和计费系统。

## SMF与UPF的PFCP交互

SMF通过**N4接口**（PFCP协议，定义于3GPP TS 29.244）控制UPF。关键交互包括：

| PFCP操作 | 说明 |
|---------|------|
| **PFCP Association Setup** | SMF与UPF建立控制面关联，交换节点能力和配置 |
| **PFCP Session Establishment** | 为新PDU会话在UPF上创建会话上下文，下发PDR/FAR/QER/URR规则 |
| **PFCP Session Modification** | 修改已有会话的规则（增删QoS Flow、调整路由等） |
| **PFCP Session Deletion** | 删除会话上下文，释放UPF资源 |
| **PFCP Session Report** | UPF向SMF上报事件（如用量报告、应用检测事件、下行数据通知） |

## SMF与PCF的策略交互

SMF通过**N7接口**与PCF交互，获取PDU会话级别的策略和计费控制信息：

- SMF在PDU会话建立时向PCF发起**SM Policy Association**。
- PCF下发**PCC规则**，包含QoS参数（5QI、ARP、GFBR/MFBR）、门控策略、计费策略和应用检测规则。
- 在会话生命周期内，PCF可随时通过N7接口更新策略（如AF触发的事件、签约变更）。
- SMF将PCC规则转换为对应的PFCP规则下发给UPF执行。

## SMF与计费系统交互

SMF负责与计费系统（CHF）交互，支持**在线计费（Online Charging）**和**离线计费（Offline Charging）**：

- **N40接口（Nchf）**：SMF与CHF之间的计费接口。
- 离线计费：SMF收集用量数据，生成CDF（Charging Data Function）记录。
- 在线计费：SMF向CHF请求配额，在配额耗尽前申请续约或终止会话。
- 计费数据基于UPF通过PFCP Session Report上报的流量统计信息。

## SMF与其他NF的交互

| 接口 | 对端NF | 用途 |
|------|-------|------|
| **N1**（经由AMF） | UE | NAS会话管理信令 |
| **N11** | AMF | 会话管理消息转发 |
| **N4** | UPF | PFCP会话控制 |
| **N7** | PCF | 策略与计费控制 |
| **N10** | UDM | 获取签约数据和会话管理订阅 |
| **N40** | CHF | 在线/离线计费 |
| **Nsmf**（服务化接口） | 其他NF | 服务化接口，供AMF、NSSF等调用 |

## 参考规范

- **3GPP TS 23.501**：System Architecture for the 5G System — SMF功能定义（Section 6.2.2）
- **3GPP TS 23.502**：Procedures for the 5G System — Session Management procedures
- **3GPP TS 29.502**：Nsmf接口（SMF Service Based Interface）信令规范
- **3GPP TS 29.244**：N4接口（SMF-UPF）PFCP协议规范
- **3GPP TS 29.512**：N7接口（SMF-PCF）策略控制信令
