<!-- evaluator user prompt 模板：按 _build_prompt 引用的片段名切片 -->
<!-- 占位实现：能让模块 import 通过、_build_prompt 可装配出合法 prompt -->
<!-- 真正业务文案应由相关负责人填写 -->

<!-- @section skeleton -->
{system_prompt}

【Origin Query】
{origin_query}

【最近 {window} 轮证据】
{recent_evidence}{generated_files_section}{rubric_section}

【前置检测输出】
先据最新一轮姿态设置 task_declared_complete：执行中(尚未交付)→ false；已交付或模棱两可 → true。
只判姿态、不判对错；仅在有明确"执行中"信号时才判 false，其余一律 true。
若 task_declared_complete=false（执行中）：rubric_checks 返回空数组 []，不做逐条评分（completion 由系统置空）；
仅当 task_declared_complete=true（已交付）时，才按下方【验收 Rubric】逐条判 0/1。

<!-- @section generated_files -->
【产物文件指针 (review_subdir={review_subdir})】
{generated_file_lines}

<!-- @section oracle -->
【Oracle Ground Truth】
{oracle_json}

<!-- @section rubric -->
{oracle_section}【验收 Rubric】
{criteria}

请按上述 rubric 逐条判 0/1，rubric_checks 必须覆盖全部 id。

<!-- @section no_rubric -->
本 query 未提供冻结 rubric，rubric_checks 必须返回空数组 []，
请按评估铁律自由裁定 inclination / improvements / violations / citations / reason。
