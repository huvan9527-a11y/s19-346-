# S19 · 20万 GitHub Actions 模拟仓

这个仓库用于验证 **S19 原始选股逻辑** 在真实时间轴下的模拟成交表现。

## 原则

- **S19 负责选股**：题材强度、连板高度、龙头/二龙头排序、承接过滤。
- **执行层只负责判断能否成交**：已经封死的票不假设能买到。
- 不把“炸板回封”作为 S19 的额外条件。
- 初始模拟资金：**200,000 元**。
- 默认单笔资金约 1/3 初始资金，最多同时 3 个仓位。
- 默认持有 5 个交易日，-6% 止损；参数见 `config.json`。

## 文件

- `src/s19_paper.py`：S19 GitHub Actions 模拟仓。
- `config.json`：策略/账户参数。
- `state/account.json`：现金和持仓状态。
- `logs/trades.csv`：模拟成交记录。
- `logs/equity.csv`：净值记录。
- `outputs/latest.md`：最新模拟仓摘要。
- `.github/workflows/s19-paper.yml`：自动运行任务。

## GitHub Actions

交易日上午按北京时间 09:45–10:55 每 5 分钟扫描；11:00 再做一次状态更新。

GitHub cron 可能有分钟级延迟，因此这个仓库只用于**模拟验证**，不用于未来真实的瞬时下单。

模拟仓会把 `state/`、`logs/` 和 `outputs/` 的变化自动 commit 回仓库。
