from typing import Dict

from fastapi import Request

I18N: Dict[str, Dict[str, str]] = {
    "en": {
        "title": "Admin UI",
        "auto_refresh": "Auto-refresh 30s",
        "refresh_in": "Page refresh in",
        "tab_overview": "strategy monitor",
        "lang_en": "EN",
        "lang_zh": "中文",
        "login_title": "Admin Sign In",
        "username": "Username",
        "password": "Password",
        "login": "Login",
        "logout": "Logout",
        "login_failed": "Invalid username or password.",
        "signed_in_as": "Signed in as",
        "summary_total": "Tracked strategies",
        "summary_running": "Running",
        "summary_stopped": "Stopped",
        "summary_errored": "Errored",
        "last_update": "Last update",
        "status": "Status",
        "mode": "Mode",
        "symbol": "Symbol",
        "accounts": "Accounts",
        "command_id": "Command ID",
        "started_at": "Started",
        "updated_at": "Updated",
        "details": "Details",
        "raw_payload": "Raw payload",
        "topic_live_updates": "Live updates topic",
        "no_data": "No strategy updates received yet.",
        "running_only": "Currently running strategies",
        "all_strategies": "All tracked strategies",
    },
    "zh": {
        "title": "管理界面",
        "auto_refresh": "每 30 秒自动刷新",
        "refresh_in": "页面刷新倒计时",
        "tab_overview": "策略监控",
        "lang_en": "EN",
        "lang_zh": "中文",
        "login_title": "管理员登录",
        "username": "用户名",
        "password": "密码",
        "login": "登录",
        "logout": "退出登录",
        "login_failed": "用户名或密码错误。",
        "signed_in_as": "当前用户",
        "summary_total": "追踪中的策略",
        "summary_running": "运行中",
        "summary_stopped": "已停止",
        "summary_errored": "异常",
        "last_update": "最近更新",
        "status": "状态",
        "mode": "模式",
        "symbol": "标的",
        "accounts": "账户",
        "command_id": "命令 ID",
        "started_at": "开始时间",
        "updated_at": "更新时间",
        "details": "详情",
        "raw_payload": "原始消息",
        "topic_live_updates": "实时更新主题",
        "no_data": "暂未收到策略更新。",
        "running_only": "当前运行中的策略",
        "all_strategies": "全部已追踪策略",
    },
}

SUPPORTED_LANGS = ("en", "zh")


def resolve_lang(request: Request) -> str:
    q = request.query_params.get("lang")
    if q in SUPPORTED_LANGS:
        return q
    c = request.cookies.get("lang")
    if c in SUPPORTED_LANGS:
        return c
    return "en"


def t(lang: str, key: str) -> str:
    lang_map = I18N.get(lang, I18N["en"])
    return lang_map.get(key, I18N["en"].get(key, key))
