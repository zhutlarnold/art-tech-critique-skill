# 历史登记与查重

读取用户现有登记优先。空表用于新项目；`assets/approved-cases.seed.json`仅在用户明确继续这些案例或要求导入时使用。本包种子只有这两篇已认可开发案例，不覆盖所有社团历史。作品ID跨栏目复用，新增文章使用唯一 `analysis_id`，把旧问题与旧结论保留在 `analyses` 对象中。

## 需要比较的层次

1. 对象：作者、已知别名、作品/版本/年份；同名不同版本先核对，Untitled不能自动合并。
2. 材料：已知来源URL、同源稿件、图片文件指纹及实际画面。不同文件指纹不证明图片内容新颖，来源复用也不等于稿件抄袭。
3. 文章：核心问题、主要结论、批判对象、机制、形式策略、证据、生活例子和伦理问题。比较主字段及所有 `analyses`，不同作品可能重复同一套论证。
4. 研究计划：已有来源能解决的问题不反复从零检索；按证据缺口查询，复核时说明理由；阶段性总结已知、未确认、下一项缺口。

先写比较表“旧问题与结论／本篇问题与结论／推理变化／新增证据与艺术分析／决定”。可给继续、补证、比较、续写、暂缓的判断，不因有重复就抹掉记录。历史缺问题或全文时明确无法完成哪层比较，不能用未命中代替查重成功。

## 数据结构

`schema_version: 1`，数组为 `cases`、`sources`、`searches`、`claims`、`images`。

- case 必需：`id`、`title`、`kind`、`artist`、`year`、`aliases`、`stage`、`publication`。artist/year未知用null；kind为artwork/product/artist_reference/format_reference。
- case 可有主字段 question/takeaway；逐篇记录在 `analyses`，键为唯一ID，值含 skill_name、angle、question、takeaway、stage、artistic_strategy、daily_anchor、discussion_question、claim_ids等。
- source 必需 id/url/source_type；已确认同源才用 evidence_family_id，访问情况另记。
- claim 必需 id/case_id/type/text/sources/premises/publishable；type为fact/artist_intent/interpretation/unverified。事实与意图要有来源，解读要有可追溯前提，未确认不能发布为已知事实；locator定位图像或文献。
- image 使用 source_url、case_id、sha256、role、caption等；没有真实文件不编造SHA。
- search 记实际日期、query、purpose、结果、访问失败或复查理由，不填猜测的检索总量。

stage 为 mentioned/researched/drafted/delivered/published，表示实际观察到的阶段；认可用 adoption 另记。publication 为 `{ "status": "unknown", "evidence": [] }`，确认发布才用 published_confirmed及证据。交付与用户认可不推导已发帖。

## 命令与安全更新

命令相对 Skill 文件夹。Python 3.10+，只用标准库。README有完整命令示例。

`init`创建且不覆盖；`check`验证结构/引用与前提；`assess`验证候选并只读预警；`merge`使用add或update，先验证整体结果，备份、写锁、指纹核对和原子更新。不删除活跃写锁，不用整文件覆盖共享表。

完整候选可写：

```json
{"case":{"id":"candidate","title":"作品名","kind":"artwork","artist":null,"year":null,"aliases":[],"stage":"mentioned","publication":{"status":"unknown","evidence":[]},"question":"本次问题","takeaway":null},"sources":[],"claims":[],"images":[]}
```

已有作品增加文章：

```json
{"cases":[{"id":"已有作品ID","analyses":{"新的analysis_id":{"skill_name":"art-tech-critique","angle":"艺术批判工业与科技","question":"本篇问题","takeaway":"本篇结论","stage":"delivered","artistic_strategy":"本篇形式分析","daily_anchor":"生活联系","discussion_question":"留给读者的问题","claim_ids":[]}}}}]}
```

这是update，脚本递归保留其他文章对象。只有继续同一篇才更新它的 analysis_id。

## 脚本预警的范围

已知题名/别名与作者/年份用于身份和版本提示，不猜作者简称与新翻译。来源URL只去除已知追踪参数，保留有意义的版本信息。图像比较SHA与来源地址。

论点比较检查主字段及 `analyses` 中每篇的 question/takeaway，中文相邻两字与英文词的集合Jaccard阈值为0.45。这是启发式关键词预警，不是统计显著性、抄袭比例或语义认证。对缺失问题/结论的历史明确报告；低分、无风险或缺字段均不能证明新颖。

形式策略、生活类比、讨论问题和换词后的同义论证仍需阅读全文人工/模型复核。check只能验证已登记引用关系，不能证明来源真的支持主张。包内检查不检查全网，未完成历史比较时保留缺口。
