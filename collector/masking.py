"""证件号码遮蔽。

普通列表、控制台输出和日志统一经过 :func:`mask_text`，保留长数字串的后三位
以便资料员核对，其余以 ``*`` 代替。原件内容从不进入日志或状态输出。
"""
from __future__ import annotations

import logging
import re

# 连续四位及以上数字按证件号/联系方式处理；过短的数字（如材料号中的 201）不遮蔽。
_DIGIT_RUN = re.compile(r"\d{4,}")


def mask_text(value: str) -> str:
    """遮蔽文本中的长数字串，仅保留最后三位。"""
    if not value:
        return value

    def _replace(match: re.Match[str]) -> str:
        run = match.group(0)
        return "*" * (len(run) - 3) + run[-3:]

    return _DIGIT_RUN.sub(_replace, value)


class MaskingFilter(logging.Filter):
    """日志过滤器：对消息与字符串参数统一做证件号遮蔽。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_text(str(record.msg))
        if record.args:
            record.args = tuple(
                mask_text(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True
