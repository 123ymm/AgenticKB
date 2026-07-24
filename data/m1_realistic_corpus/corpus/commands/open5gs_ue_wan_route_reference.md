# Open5GS UE 外网连通路由配置命令

来源：

- SRC-OPEN5GS-QUICKSTART: https://open5gs.org/open5gs/docs/guide/01-quickstart/

## 命令功能

这些 Linux 命令用于在 Open5GS 实验环境中打开主机转发能力，并为 UE 地址段添加 NAT 规则，使 UE 流量能够通过 PGWU/UPF 访问外部网络。

## 命令格式

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv6.conf.all.forwarding=1
sudo iptables -t nat -A POSTROUTING -s <UE_IPV4_CIDR> ! -o ogstun -j MASQUERADE
sudo ip6tables -t nat -A POSTROUTING -s <UE_IPV6_CIDR> ! -o ogstun -j MASQUERADE
sudo ufw status
sudo ufw disable
```

## 参数说明

| 参数名 | 类型 | 取值范围 | 缺省值 | 说明 |
|---|---|---|---|---|
| UE_IPV4_CIDR | IPv4 CIDR | 实验环境 UE 地址池 | 10.45.0.0/16 | 需要做 IPv4 NAT 的 UE 地址段。 |
| UE_IPV6_CIDR | IPv6 CIDR | 实验环境 UE 地址池 | 2001:db8:cafe::/48 | 需要做 IPv6 NAT 的 UE 地址段。 |
| ogstun | 接口名 | Open5GS tunnel interface | ogstun | Open5GS 用户面隧道接口。 |

## 视图

Linux shell，需要 sudo 权限。

## 使用指南

1. 先确认 UE 地址池与 Open5GS 配置一致。
2. 开启 IPv4/IPv6 forwarding。
3. 添加 NAT 规则，让 UE 源地址流量出公网时完成地址转换。
4. 检查防火墙策略，避免默认规则阻断 UE 流量。

## 使用实例

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv6.conf.all.forwarding=1
sudo iptables -t nat -A POSTROUTING -s 10.45.0.0/16 ! -o ogstun -j MASQUERADE
sudo ip6tables -t nat -A POSTROUTING -s 2001:db8:cafe::/48 ! -o ogstun -j MASQUERADE
sudo ufw status
```

## 注意事项

- 这些命令适用于实验环境或测试主机，不应直接复制到生产核心网。
- 如果主机上启用了防火墙，应先确认安全策略，而不是简单关闭防火墙。
- 多主机场景还需要额外限制 UE 流量访问核心网控制面网元。

