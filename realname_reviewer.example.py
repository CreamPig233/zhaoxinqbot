"""实名自动审核器示例文件。

使用方法：
1. 将本文件复制为 realname_reviewer.py。
2. 在 realname_reviewer.py 中实现 review_application(application)。
3. 保持 config.yaml 中 realname.auto_review.module_path 指向 realname_reviewer.py。

realname_reviewer.py 已加入 .gitignore，适合放置本地名单、接口密钥或临时审核逻辑。
"""

from typing import Any


def review_application(application: dict[str, Any]) -> dict[str, str]:
    """审核一条实名申请，并返回 approve、reject 或 timeout。

    application 中会包含 application_id、user_id、identity、raw_text、
    created_at、source_event 等字段。identity 默认包含：
    - name：姓名
    - id：学号（可能包含字母，长度不固定）

    可返回的形式包括：
    - {"status": "approve", "reason": "名单匹配", "college": "学院名称"}
    - {"status": "reject", "reason": "学号不存在"}
    - {"status": "timeout", "reason": "移交人工审核"}
    """
    
    # return {"status": "approve", "reason": "成功原因", "college": "学院名称"} # 实名自动核验成功
    # return {"status": "reject", "reason": "失败原因"} # 实名自动核验失败
    # return {"status": "timeout", "reason": "异常原因"} # 实名自动核验异常，移交人工审核
    
    identity = application.get("identity", {})
    xingming = str(identity.get("name", "")).strip()
    xuehao = str(identity.get("id", "")).strip()


    return {"status": "timeout", "reason": "示例审核器未接入真实名单，移交人工审核"}
