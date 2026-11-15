"""URL 规范化、SURT 风格 urlkey、回环白名单与 robots.txt 解析。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from .errors import PolicyError

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str, base: str | None = None) -> str:
    """去 fragment、统一 scheme/host 大小写、补根路径并去掉默认端口。"""
    joined = urljoin(base, url) if base else url
    parsed = urlsplit(joined)
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise PolicyError("URL_SCHEME", "only http and https URLs are supported")
    if parsed.username or parsed.password:
        raise PolicyError("URL_USERINFO", "userinfo in URLs is not allowed")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise PolicyError("URL_INVALID", f"invalid URL: {url}") from exc
    if not host:
        raise PolicyError("URL_INVALID", f"URL has no host: {url}")
    host = host.lower().rstrip(".")
    if port is not None and port == _DEFAULT_PORTS[scheme]:
        port = None
    if ":" in host:
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    else:
        netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def urlkey(url: str) -> str:
    """生成 SURT 风格的排序键：主机标签反转 + 路径与 query，统一小写。"""
    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    if port is not None and port == _DEFAULT_PORTS[parsed.scheme]:
        port = None
    if ":" in host:
        reversed_host = f"[{host}]"
    else:
        reversed_host = ",".join(reversed(host.split(".")))
    port_part = f":{port}" if port is not None else ""
    path_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return f"{parsed.scheme}://{reversed_host}{port_part}){path_query}".lower()


def target_host(url: str) -> str:
    """返回规范化后的主机名，供白名单判断。"""
    parsed = urlsplit(normalize_url(url))
    return (parsed.hostname or "").lower().rstrip(".")


def is_loopback_target(url: str) -> bool:
    """判断 URL 是否只指向本机回环地址（不触发 DNS 查询）。"""
    try:
        return target_host(url) in ALLOWED_HOSTS
    except PolicyError:
        return False


def ensure_allowed_url(url: str) -> str:
    """在建立任何连接前拒绝非回环目标，返回规范化 URL。"""
    normalized = normalize_url(url)
    if target_host(normalized) not in ALLOWED_HOSTS:
        raise PolicyError("TARGET_NOT_LOOPBACK",
            f"target is outside the loopback allowlist: {target_host(normalized)}",
        )
    return normalized


def same_origin(first: str, second: str) -> bool:
    """判断两个 URL 是否同 origin（scheme + 主机 + 有效端口均相同）。"""
    left = urlsplit(normalize_url(first))
    right = urlsplit(normalize_url(second))

    def parts(parsed: SplitResult) -> tuple[str, str, int | None]:
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if port is None:
            port = _DEFAULT_PORTS.get(scheme)
        return scheme, host, port
    return parts(left) == parts(right)


def validate_bind_host(host: str) -> str:
    """服务绑定地址也必须是回环；防止误暴露到局域网。"""
    candidate = host.strip().lower()
    if candidate not in ALLOWED_HOSTS:
        raise PolicyError("BIND_NOT_LOOPBACK", "bind address must be 127.0.0.1, localhost or ::1")
    return candidate


@dataclass(frozen=True)
class RobotsRules:
    """最小 robots 规则集：按最长匹配选择，Allow 与 Disallow 等长时 Allow 胜。"""
    patterns: tuple[tuple[bool, str], ...]

    def allowed(self, path_and_query: str) -> bool:
        """返回路径是否允许抓取；无规则时一律允许。"""
        best_length = -1
        best_allow = True
        for allow, pattern in self.patterns:
            if not pattern:
                continue
            if _robots_match(pattern, path_and_query):
                length = len(pattern)
                if length > best_length or (length == best_length and allow and not best_allow):
                    best_length = length
                    best_allow = allow
        return best_allow


def _robots_match(pattern: str, text: str) -> bool:
    anchored = pattern.endswith("$")
    raw = pattern[:-1] if anchored else pattern
    regex = re.escape(raw).replace(r"\*", ".*")
    suffix = "$" if anchored else ""
    return re.match("^" + regex + suffix, text) is not None


def _agent_matches(agent: str, user_agent: str) -> bool:
    value = agent.strip().lower()
    return value == "*" or value in user_agent.lower()


def parse_robots(text: str, user_agent: str) -> RobotsRules:
    """解析 robots.txt 的 User-agent / Allow / Disallow 子集。"""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []

    def flush() -> None:
        nonlocal agents, rules
        if agents:
            groups.append((agents, rules))
        agents, rules = [], []
    for line in text.splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean or ":" not in clean:
            continue
        field, value = clean.split(":", 1)
        name = field.strip().lower()
        content = value.strip()
        if name == "user-agent":
            if rules:
                flush()
            agents.append(content)
        elif name in ("allow", "disallow") and agents:
            rules.append((name == "allow", content))
    flush()
    selected: list[tuple[bool, str]] = []
    for group_agents, group_rules in groups:
        if any(_agent_matches(agent, user_agent) for agent in group_agents if agent != "*"):
            selected.extend(group_rules)
    if not selected:
        for group_agents, group_rules in groups:
            if any(agent == "*" for agent in group_agents):
                selected.extend(group_rules)
    return RobotsRules(tuple(selected))


def parse_robots_sitemaps(text: str) -> tuple[str, ...]:
    """提取 robots.txt 中的 Sitemap 声明，按出现顺序保留原始声明 URL。

    Sitemap 行不属于任何 User-agent 组，可出现在文件任意位置；此处只做
    注释剥离与首尾空白清理，不做 URL 规范化，声明原文留给抓取层审计。
    """
    declared: list[str] = []
    for line in text.splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean or ":" not in clean:
            continue
        field, value = clean.split(":", 1)
        if field.strip().lower() == "sitemap":
            content = value.strip()
            if content:
                declared.append(content)
    return tuple(declared)


def robots_url_for(url: str) -> str:
    """返回站点级 robots.txt 地址。"""
    parsed = urlsplit(normalize_url(url))
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def path_and_query(url: str) -> str:
    """返回 robots 匹配使用的 path?query。"""
    parsed = urlsplit(normalize_url(url))
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")
