# ADD APN - V100R023C10 baseline

适用范围：CloudCore / V100R023C10 / PGW-C
来源依据：cloud_core_coldstart_md/02_commands/05_add_apn.md

## 命令功能

ADD APN 用于创建 APN 对象，并定义 APN 名称、地址类型、关联地址池和缺省策略。

## 注意事项

- APN 名称需与签约数据保持一致。
- 若 APN 由 PGW-U 本地分配地址，需同时准备地址池和 Sxb 控制能力。
- 变更生产 APN 前应确认不影响现网签约和 DNS。

## 参数说明

| 参数标识 | 参数名称 | 参数说明 |
|---|---|---|
| APNNAME | APN 名称 | 必选参数。指定 APN 标识。 |
| PDNTYPE | PDN 地址类型 | 必选参数。可选 IPV4、IPV6、IPV4V6。 |
| POOLNAME | 地址池名称 | 可选参数。用于绑定本地地址池。 |
| DNSPROFILE | DNS 模板 | 可选参数。指定下发到用户侧的 DNS 信息。 |
| DESC | 描述 | 可选参数。用于维护备注。 |

## 使用实例

```mml
ADD APN:APNNAME="internet",PDNTYPE=IPV4,POOLNAME="pool_ipv4",DNSPROFILE="dns_public";
ADD APN:APNNAME="ims",PDNTYPE=IPV4V6,POOLNAME="pool_dual",DNSPROFILE="dns_ims";
```

