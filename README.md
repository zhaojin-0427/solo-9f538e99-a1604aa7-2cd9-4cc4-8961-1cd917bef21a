# SNP 指纹匿名样本核对系统

外包测序结果回库前，用 6 个 SNP 位点指纹把匿名样本号对应回院内编号的 Flask + SQLite 网页工具。

## 业务规则

- 每份 CSV 含 **1～12 个唯一样本号**，两份文件各含相同的 **6 个 SNP 位点**，位点值为 `0`/`1`/`2` 或空（缺失），两侧样本数相同。
- 两样本可配对的前提：**两侧均非空的位点 ≥ 3 个，且不一致位点数 ≤ 1**。
- 在可配对构成的二分图上求**覆盖全部样本的一对一完美匹配**，使全部分配的不一致位点总数最小（匈牙利算法，O(n³)，n ≤ 12）。
- 逐样本列出其在**所有最优匹配**中出现过的全部候选（逐边强制 + 重算最优值枚举），不只报任意一个解。
- 数据员按外部证据**锁定**候选后，系统以强制边重算其余样本的候选；导致无解或使总代价高于原始最优值的选择一律拒绝。
- 仍有歧义时**禁止下载**；全部样本候选唯一后才提供编号对照 CSV。
- 无解时展示具体原因（哪个样本无可配对象 / 不存在覆盖全部样本的匹配）。

## 并发与持久化

- SQLite（WAL 模式）保存上传数据、锁定关系和修订号 `revision`。
- 锁定/解锁采用**乐观并发控制**：页面带所见修订号提交，服务端在单个 `BEGIN IMMEDIATE` 事务内校验，修订号过期返回 409，旧页面无法覆盖他人判断。
- 校验不通过即回滚，**保存失败不改变任何已有判断**，修订号也不自增。
- 数据与锁定落盘，**进程重启后输入和锁定全部保留**。

## 用 Docker Compose 启动

```bash
docker compose up --build -d
# 打开 http://localhost:8000
```

数据库位于命名卷 `snp_data`（容器内 `/app/data/snp.db`）。生产环境请通过环境变量
`APP_SECRET_KEY` 覆盖默认密钥。

停止与清理：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 连数据卷一起删除
```

## 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py               # 开发服务器
# 或：gunicorn --bind 0.0.0.0:8000 wsgi:app
```

## 自测

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
```

测试覆盖：

- `tests/test_matching.py`：配对规则边界、匈牙利算法、唯一/歧义候选、
  锁定消歧、锁定破坏最优值/导致无解、无解原因，并对 **60 组随机数据**
  与暴力枚举所有完美匹配的结果逐项对照；
- `tests/test_csvio.py`：编码、BOM、非法值、重复样本号、位点列数、行列数、
  跨文件位点/样本数/编号空间校验；
- `tests/test_web.py`：上传、歧义页、锁定消歧全流程、过期修订号 409、
  破坏最优值的锁定被拒且状态不变、重复占用右侧编号被拒、无解下载门禁、
  重启后数据与锁定保留。

## 手动试用数据

`examples/` 下提供三组 CSV：

| 文件对 | 场景 |
| --- | --- |
| `ambiguous_left/right.csv` | A01/A02 指纹相同产生 2 选 1 歧义，锁定后才能下载（含缺失值） |
| `unique_left/right.csv` | 全部唯一，可直接下载对照 CSV |
| `no_solution_left/right.csv` | A02 无任何可配院内样本，展示无解原因 |

## 目录结构

```
app/
  __init__.py     Flask 路由（上传/详情/锁定/解锁/下载，乐观锁事务）
  matching.py     纯算法引擎：位点比较、匈牙利最小权匹配、候选枚举、锁定
  csvio.py        CSV 解析与校验
  storage.py      SQLite 存储层（WAL、IMMEDIATE 事务、修订号）
  templates/      Jinja2 页面
tests/            pytest 单元与端到端测试
examples/         三组示例 CSV
wsgi.py           gunicorn 入口
Dockerfile / docker-compose.yml / requirements.txt
```
