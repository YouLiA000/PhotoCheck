# PhotoCheck · 照片隐私体检成品说明

更新日期：2026-11-13

## 一句话

PhotoCheck 是一个本地照片隐私体检与脱敏原型：检测明确的文字、二维码、人脸和
拍摄元数据，按用户决定生成不覆盖原图的脱敏副本，并对当前计划区域和最终导出
文件进行复检。

## 当前完整链路

```text
JPG / PNG
→ 方向矫正与元数据检测
→ A 检测器
→ Decision
→ RedactionPlan（含手动补框）
→ solid / blur / mosaic 处置
→ 清理整图源元数据并重编码
→ 逐区域复检
→ 整图重跑
→ 输出图片 + JSON/TXT 报告
```

二维码始终强制 `solid`。高斯模糊已从约短边 1/4 提升到短边 3/4 的奇数核，
但仍必须由复检决定是否通过；不能用“已经模糊”代替验证。

## 包内结构

```text
PhotoCheck_App/
├─ 启动.bat
├─ 自检.bat
├─ 使用说明.txt
├─ 项目说明.md
├─ BUILD_INFO.json
├─ requirements.txt
├─ docs/
│  └─ 功能规格卡_v1.4.md
├─ runtime/
│  ├─ python.exe
│  ├─ msvcr120.dll
│  ├─ Lib/site-packages/
│  └─ exiftool/
└─ app/
   ├─ main.py
   ├─ server.py
   ├─ service.py
   ├─ vendor/photocheck/
   └─ web/
```

发布包不得包含 `work/`、上传副本、处理产物、`__pycache__`、`.pyc`、
`.pytest_cache` 或测试目录。

## 当前验证

- C 契约/数据/界面/评测/运维测试：223 passed。
- A 检测引擎：232 passed，源码哈希 `DD15E484…`。
- B 运行时、契约和 A+B 集成：82 passed。
- 本地 Web 应用：28 passed。
- dev 真值：38 张、84 个对象；A+B 导出预测 TP 84 / FN 0 / FP 0。
- 43 图 B 全链：41 张可解析，32 passed、9 not_applicable，0 残留、
  0 计划外检出；2 张为预期无效 JPEG。
- lock：23 张图、49 个封存文件，字节校验通过。

这些数字主要来自合成 dev，不能外推为实拍泛化能力。人脸尚无合规验收集，
二维码纸面实拍仍需补样。

## 状态与边界

- `passed` 只表示本次计划区域未发现可复检残留；不是“绝对安全”。
- 不联网、不上传、不调用 API 或大模型。
- 复检看不见首次就漏掉的对象，因此仍支持手动补框和整图重扫作为兜底。
- `pending` 表示检测器不可用或缺少可验证语义；`failed` 表示仍检出残留。
- 便携包内的 ExifTool 通过显式参数和 `PHOTOCHECK_EXIFTOOL` 双重定位；
  `msvcr120.dll` 随包提供，自检会把 pyzbar 降级视为失败而非 OK。

## 启动

1. 解压到可写目录。
2. 双击 `启动.bat`。
3. 浏览器打开本机地址。
4. 使用完毕关闭命令行窗口；如需清理，删除 `work/`。
