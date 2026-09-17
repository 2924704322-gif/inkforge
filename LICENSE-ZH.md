# 许可说明（中文）

> 正式条款以 [`LICENSE`](LICENSE) 为准（**PolyForm Strict License 1.0.0**，SPDX 标识符
> `PolyForm-Strict-1.0.0`，官方原文：<https://polyformproject.org/licenses/strict/1.0.0>）。
> 本文件只是便于中文读者理解的说明；两者冲突时以 `LICENSE` 为准。

## 一句话

**源码可见，仅供非商业使用；不得再分发，也不得修改后发布。**

## 你可以做（无需另外申请）

- 自己阅读、学习、研究这份代码；
- 在自己机器上**原样**运行、实验、测试；
- 个人娱乐、业余爱好、学习用途；
- 慈善组织 / 学校等教育机构 / 公立科研机构 / 公共安全与卫生机构 / 环保组织 / 政府机构的
  非商业使用（不论经费来源）；
- 法律允许范围内的"合理使用"（fair use）。

## 你不能做

| 禁止 | 对应条款 |
|---|---|
| **二传 / 再分发**：拷贝、上传到任何仓库或网盘、打包进产品、转给他人 | Copyright License：许可仅覆盖「**分发软件以外**」的一切行为 |
| **商用**：用于盈利产品、付费服务、公司内部生产环境、外包交付等任何有商业预期的场景 | Noncommercial Purposes：**只有**非商业目的属于被许可目的 |
| **修改 / 衍生**：改动代码后发布、基于它做新作品、再许可给第三方 | Copyright License：许可明确排除「**修改或制作基于本软件的新作品**」 |

> 换句话说：**"自己用"可以，"传出去"和"拿去卖"都不行，改完再发布也不行。**

## 想要别的授权

需要商用、二次开发、分发或集成到自己的产品时，请先取得**书面授权**——
在 GitHub 上开一个 Issue 说明用途即可（<https://github.com/2924704322-gif/inkforge/issues>）。

## 本仓库**不包含**的内容

为避免误会，明确说明本仓库**刻意不含**以下内容（均由 `.gitignore` 排除）：

- **任何小说作品数据**：`engine/data/` 整体不入库，包括 `novels/`（章节正文、大纲、人物卡、
  摘要、伏笔、会话）、`workspace/`、`materials/`、`learning/`、`skills/`、`custom_skills/`、`config/`。
  仓库里出现的"小说相关内容"只有 `engine/smoke_test.py` 等测试夹具中**自造的示例文本**，
  与任何真实作品无关。
- **任何密钥**：`.env`（`DEEPSEEK_API_KEY` / `EMBEDDING_API_KEY` 等）从不入库，
  仓库只提供 `engine/.env.example` 占位模板；`configs/models.yaml` 中的密钥一律写作
  `${ENV_VAR}`，由使用者在本机环境变量或 `.env` 中提供。
- 运行时派生数据与构建产物：`engine/data/runtime/`、`engine/smoke-reports/`、
  `apps/desktop/out/`、`node_modules/`。

## 免责

在法律允许的最大范围内，本软件按"现状"提供，不含任何担保；作者不对使用本软件产生的
任何后果承担责任（详见 `LICENSE` 的 *No Liability* 一节）。
