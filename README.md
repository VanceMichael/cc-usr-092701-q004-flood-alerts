# 山洪预警闭环处置

本项目保存山洪预警闭环处置所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

防汛指挥员、水文站人员、乡镇网格员、上级值班人员

## 事实资料

- 水利部与中国气象局联合发布黄色山洪灾害气象预警
- 预警涉及湖北西北部、四川东北部、重庆北部和陕西南部等地
- 部门要求实时监测、防汛预警和转移避险

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.flood_alerts.context fixtures/context.json
```
