# ADD APN - V100R023C20 scope variant

适用范围：CloudCore / V100R023C20 / PGW-C
来源依据：cloud_core_coldstart_md/02_commands/05_add_apn.md

## 命令功能

ADD APN 用于创建 APN 对象，并定义 APN 名称、地址类型、关联地址池、DNS 模板和缺省策略。

## 参数说明

| 参数标识 | 参数名称 | 参数说明 |
|---|---|---|
| APNNAME | APN 名称 | 必选参数。指定 APN 标识。 |
| PDNTYPE | PDN 地址类型 | 必选参数。可选 IPV4、IPV6、IPV4V6。 |
| POOLNAME | 地址池名称 | 可选参数。用于绑定本地地址池。 |
| DNSPROFILE | DNS 模板 | 条件必选参数。企业专网 APN 必须指定 DNS 模板。 |
| DESC | 描述 | 可选参数。用于维护备注。 |

## 注意事项

> V100R023C20 中，企业专网 APN 若未指定 DNSPROFILE，终端可能无法解析专网域名。

## 使用实例

```mml
ADD APN:APNNAME="enterprise",PDNTYPE=IPV4V6,POOLNAME="pool_enterprise",DNSPROFILE="dns_enterprise";
```

