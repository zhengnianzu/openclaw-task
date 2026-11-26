import os
import sys
import json
import argparse
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel


INDEX_FILENAME = "index.jsonl"
TRAJECTORY_DIR = Path("logs_proxy/traj")


def check_date_range(file: Path, date_start: str, date_end: str):
    """判断日志是否符合筛选日期"""
    file_date = file.name.split('_')[0]
    return True if date_start <= file_date <= date_end else False


def find_first_key_value(obj, target_key, value_type):
    """
    深度优先递归遍历 JSON 对象（dict/list），
    返回第一个键为 target_key 且符合 value_type 类型的值，
    若未找到则返回 None。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == target_key and isinstance(value, value_type):
                if value_type == str:
                    return value
                elif value_type == int and value > 0:
                    return value
            if isinstance(value, (dict, list)):
                result = find_first_key_value(value, target_key, value_type)
                if result is not None:
                    return result
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                result = find_first_key_value(item, target_key, value_type)
                if result is not None:
                    return result
    return None


class DataItem(BaseModel):
    """对应 /statistic 返回的 data 列表中的单个记录"""
    model: str
    date_start: str
    date_end: str
    status: Literal['success', 'error']
    count: int = 0
    input_token_num: int = 0
    output_token_num: int = 0


class SummaryItem(BaseModel):
    """对应 /statistic 返回的 summary 列表中的单个汇总项"""
    count: int = 0
    status: Literal['success', 'error']
    total_input: int = 0
    total_output: int = 0


def find_files(filter_suffix="*-res.json"):
    """扫描旧版按请求保存的响应文件。"""
    for _dir in os.listdir():
        if os.path.isdir(_dir) and _dir.startswith("logs_"):
            for file in Path(_dir).rglob(filter_suffix):
                yield file


def _load_index_file(index_path: str) -> Optional[list]:
    """从 index.jsonl 加载条目。文件不存在时返回 None。"""
    if not os.path.exists(index_path):
        return None
    entries = []
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                pass
    return entries


def _collect_index_files() -> list[Path]:
    """收集网关索引和 Lite 旧版的索引。"""
    index_files: list[Path] = []
    gateway_index = TRAJECTORY_DIR / INDEX_FILENAME
    if gateway_index.is_file():
        index_files.append(gateway_index)
    for d in os.listdir():
        if os.path.isdir(d) and d.startswith("logs_"):
            idx = Path(d) / INDEX_FILENAME
            if idx.is_file():
                index_files.append(idx)
    return index_files


def _matches_model(model: str, model_filter: str) -> bool:
    """按逗号分隔的模型名过滤，忽略大小写。"""
    return not model_filter or model.casefold() in {name.strip().casefold() for name in model_filter.split(",")}


def _aggregate_index_entries(entries, model_filter, date_start, date_end, status, model_count_data):
    """将 index 条目聚合到 model_count_data 字典中。"""
    for entry in entries:
        if entry.get("request_kind") in ("probe", "health"):
            continue
        ts = entry.get("ts", "")
        entry_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts.isdigit() else ts[:10]
        if not (date_start <= entry_date <= date_end):
            continue

        _model = entry.get("model", "") or ""
        if not _model:
            continue

        if not _matches_model(_model, model_filter):
            continue

        tok_in = entry.get("tok_in", 0) or 0
        tok_out = entry.get("tok_out", 0) or 0

        # 新索引用 HTTP 和流完成状态记录成败，旧索引沿用原有口径。
        is_success = bool(entry.get("success", False)) and ("status_code" in entry or tok_out > 0)

        if _model not in model_count_data:
            model_count_data[_model] = {
                "success": DataItem(model=_model, date_start=date_start, date_end=date_end, status="success"),
                "error": DataItem(model=_model, date_start=date_start, date_end=date_end, status="error"),
            }

        if status in ["全部", "成功"] and is_success:
            model_count_data[_model]["success"].count += 1
            model_count_data[_model]["success"].input_token_num += tok_in
            model_count_data[_model]["success"].output_token_num += tok_out
        elif status in ["全部", "失败"] and not is_success:
            model_count_data[_model]["error"].count += 1
            model_count_data[_model]["error"].input_token_num += tok_in


def statistic_tokens(model: str = '', date_start: str = '', date_end: str = '', status: str = '全部', **kwargs) -> dict:
    """
    统计token数。优先从 index.jsonl 快速加载，不存在时降级扫描 res 文件。
    :param model: 过滤模型，忽略大小写，多个模型用,拼接
    :param date_start: 过滤日期-开启，格式YYYY-MM-DD
    :param date_end: 过滤日期-结束，格式YYYY-MM-DD
    :param status: 过滤状态: 全部、成功、失败
    :return: dict
    """
    if not date_start:
        date_start = '2000-01-01'
    if not date_end:
        date_end = '9999-12-31'

    model_count_data = dict()
    res_data = list()

    # ---- 快速路径：index.jsonl ----
    index_files = _collect_index_files()
    if index_files:
        for index_file in index_files:
            entries = _load_index_file(str(index_file))
            if entries is not None:
                _aggregate_index_entries(entries, model, date_start, date_end, status, model_count_data)
    else:
        # ---- 降级路径：扫描所有 res 文件 ----
        for file in find_files():
            if not check_date_range(file, date_start, date_end):
                continue
            with open(file, 'r', encoding='utf8') as f:
                raw_data = f.read()
            try:
                data = json.loads(raw_data)
            except Exception as e:
                print(f"{file} json load error: {e}", file=sys.stderr)
                continue
            _model = find_first_key_value(data, 'model', str)
            if not isinstance(_model, str):
                continue
            if not _matches_model(_model, model):
                continue

            if _model not in model_count_data:
                model_count_data[_model] = {
                    "success": DataItem(model=_model, date_start=date_start, date_end=date_end, status="success"),
                    "error": DataItem(model=_model, date_start=date_start, date_end=date_end, status="error"),
                }

            token_key_in = ['input_tokens', 'prompt_tokens']
            for key_in in token_key_in:
                token_in = find_first_key_value(data, key_in, int)
                if token_in:
                    break
            else:
                token_in = 0

            token_key_out = ['output_tokens', 'completion_tokens']
            for key_out in token_key_out:
                token_out = find_first_key_value(data, key_out, int)
                if token_out:
                    break
            else:
                token_out = 0

            if status in ["全部", "成功"] and token_out > 0:
                model_count_data[_model]["success"].count += 1
                model_count_data[_model]["success"].input_token_num += token_in
                model_count_data[_model]["success"].output_token_num += token_out
            elif status in ["全部", "失败"] and token_out == 0:
                model_count_data[_model]["error"].count += 1
                model_count_data[_model]["error"].input_token_num += token_in

    for key, val in model_count_data.items():
        if val['success'].count > 0:
            res_data.append(val['success'].model_dump())
        if val['error'].count > 0:
            res_data.append(val['error'].model_dump())

    summary_success = SummaryItem(status="success")
    summary_error = SummaryItem(status="error")
    for data in res_data:
        if data['status'] == 'success':
            summary_success.count += data['count']
            summary_success.total_input += data['input_token_num']
            summary_success.total_output += data['output_token_num']
        elif data['status'] == 'error':
            summary_error.count += data['count']
            summary_error.total_input += data['input_token_num']
            summary_error.total_output += data['output_token_num']

    res = {"data": res_data, "summary": [summary_success.model_dump(), summary_error.model_dump()]}

    print_log = f"""
================================================================================
API 调用统计摘要
================================================================================

成功调用次数: {summary_success.count}
失败调用次数: {summary_error.count}
总输入 Tokens: {summary_success.total_input}
总输出 Tokens: {summary_success.total_output}
总 Tokens: {summary_success.total_input + summary_success.total_output}

按模型统计:
--------------------------------------------------------------------------------
"""
    for key, val in model_count_data.items():
        if val['success'].count > 0:
            _count = val['success'].count
            _input_token = val['success'].input_token_num
            _output_token = val['success'].output_token_num
            _all_token = _input_token + _output_token
            print_log += f"""{key}: 调用 {_count} 次, 输入 {_input_token} , 输出 {_output_token}, 总计 {_all_token} tokens"""
            print_log += '\n'
        if val['error'].count > 0:
            print_log += f"{key}: 失败 {val['error'].count} 次, 输入 {val['error'].input_token_num} tokens\n"
    print_log += "--------------------------------------------------------------------------------"

    print(print_log)
    return res


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", "-m", type=str, default="", help="过滤模型，忽略大小写，多个模型用,拼接")
    parser.add_argument("--date_start", "-s", type=str, default="", help="过滤日期-开启，格式YYYY-MM-DD")
    parser.add_argument("--date_end", "-e", type=str, default="", help="过滤日期-结束，格式YYYY-MM-DD")
    parser.add_argument("--status", "-t", type=str, default="全部", help="过滤状态: 全部、成功、失败")
    args = parser.parse_args()

    statistic_tokens(**args.__dict__)
