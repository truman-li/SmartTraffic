## 安装
请位于项目根目录下开始：

1. 创建环境

```bash
conda create -n traffic python=3.11 -y
conda activate traffic
```

2. 安装核心依赖

```bash
pip install -r requirements.txt
```

## 配置API KEY

在项目根目录 `.env` 配置统一密钥（推荐）：

```env
API_KEY=你的密钥
```

如需单独配置 GraphRAG，也可在 `Module-3/.env` 中设置：

```env
GRAPHRAG_API_KEY=你的密钥
```

## 启动

使用启动脚本启动：

```bash
bash start_service.sh
```

或者自定义启动端口：

```bash
bash start_service.sh --port 9000
```

当您需要重置整个系统时，请使用：
```bash
bash start_service.sh --reset
```
