# pairwise-shot-cut-detect

## 领域

游戏、图形与多媒体

## 主题

镜头切换检测

## 说明

双阈值判定、显示序检测与跨文件时间码。

初始状态下 `av/shot_cut_detect.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
