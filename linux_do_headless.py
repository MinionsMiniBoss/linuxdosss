# -*- coding: utf-8 -*-
"""
================================================================================
Linux.do 论坛自动浏览脚本 (无头版 / Headless)
================================================================================

适用场景：
    - GitHub Actions 定时任务
    - 服务器后台运行
    - 无 GUI 环境

功能：
    - 自动登录（Cookie 优先 / 用户名 + 密码兜底）
    - 自动浏览多个板块
    - 随机点赞帖子
    - 防风控机制（随机间隔）
    - 支持代理

================================================================================
使用方法
================================================================================

方式一：命令行参数
    python linux_do_headless.py --username 你的用户名 --password 你的密码

方式二：环境变量（推荐用于 GitHub Actions）
    export LINUXDO_USERNAME="你的用户名"
    export LINUXDO_PASSWORD="你的密码"
    python linux_do_headless.py

方式三：Cookie 登录（推荐 Actions，可规避 hCaptcha）
    export LINUXDO_COOKIE="_t=xxx; _forum_session=yyy"
    python linux_do_headless.py
    # 或
    python linux_do_headless.py --cookie "_t=xxx; _forum_session=yyy"

可选参数：
    --cookie        Cookie 字符串（优先于账号密码）
    --proxy         代理地址，如 127.0.0.1:7890 或 http://127.0.0.1:7890
    --topics        浏览帖子数量，默认 30
    --like-rate     点赞概率，0-100，默认 30
    --headless      是否无头模式，默认 true
    --debug         调试模式，显示更多日志

示例：
    # 基本使用
    python linux_do_headless.py -u myuser -p mypass

    # Cookie 登录 + 代理（GitHub Actions 推荐）
    python linux_do_headless.py --cookie "$_t=...; _forum_session=..." --proxy http://127.0.0.1:7890

    # 指定浏览数量和点赞率
    python linux_do_headless.py -u myuser -p mypass --topics 50 --like-rate 20

    # 使用代理
    python linux_do_headless.py -u myuser -p mypass --proxy 127.0.0.1:7897

================================================================================
GitHub Actions 配置
================================================================================

1. Fork 本仓库到你的账号，并设为私有

2. 添加 Secrets（Settings -> Secrets and variables -> Actions）：
   - LINUXDO_COOKIE: 浏览器 Cookie（推荐，可规避 hCaptcha）
   - LINUXDO_USERNAME / LINUXDO_PASSWORD: 账号密码（Cookie 失效时兜底）
   - CLASH_CONFIG_URL: Clash/mihomo 订阅地址（可选，降低 GitHub IP 风控）
   - SMTP_* : 邮件通知相关（可选）

3. 启用 Actions（Actions -> I understand my workflows, go ahead and enable them）

4. 定时任务会自动运行，也可以手动触发（Actions -> Run workflow）

Cookie 获取方法：
    1. 浏览器登录 linux.do
    2. F12 -> Application/存储 -> Cookies -> https://linux.do
    3. 复制关键 Cookie（至少需要 _t，建议带上 _forum_session）
    4. 拼成: _t=xxx; _forum_session=yyy
    也可导出为 JSON 数组格式

================================================================================
注意事项
================================================================================

1. 请合理设置运行频率，避免对服务器造成压力
2. 建议每天运行 1-2 次，每次浏览 30-50 个帖子
3. GitHub Actions 私有仓库每月有 2000 分钟免费额度
4. 单次运行时间建议控制在 30 分钟以内
5. Cookie 会过期，失效后需重新从浏览器导出并更新 Secret

================================================================================
"""

import os
import sys
import json
import random
import time
import argparse
from datetime import datetime

# 延迟导入 DrissionPage：--help / Cookie 解析等不需要浏览器依赖
ChromiumPage = None
ChromiumOptions = None


def _ensure_drissionpage():
    """按需导入 DrissionPage，避免仅解析参数时强制依赖"""
    global ChromiumPage, ChromiumOptions
    if ChromiumPage is not None:
        return
    try:
        from DrissionPage import ChromiumPage as _Page, ChromiumOptions as _Options

        ChromiumPage = _Page
        ChromiumOptions = _Options
    except ImportError:
        print("错误: 请先安装 DrissionPage")
        print("运行: pip install DrissionPage")
        sys.exit(1)


# ============================================================================
# 配置
# ============================================================================

# 板块配置（可根据需要调整 enabled 字段）
CATEGORIES = [
    {"name": "开发调优", "url": "/c/develop/4", "enabled": True},
    {"name": "国产替代", "url": "/c/domestic/98", "enabled": True},
    {"name": "资源荟萃", "url": "/c/resource/14", "enabled": True},
    {"name": "网盘资源", "url": "/c/resource/cloud-asset/94", "enabled": True},
    {"name": "文档共建", "url": "/c/wiki/42", "enabled": True},
    {"name": "积分乐园", "url": "/c/credit/106", "enabled": False},  # 默认禁用
    {"name": "非我莫属", "url": "/c/job/27", "enabled": True},
    {"name": "读书成诗", "url": "/c/reading/32", "enabled": True},
    {"name": "扬帆起航", "url": "/c/startup/46", "enabled": False},  # 默认禁用
    {"name": "前沿快讯", "url": "/c/news/34", "enabled": True},
    {"name": "网络记忆", "url": "/c/feeds/92", "enabled": True},
    {"name": "福利羊毛", "url": "/c/welfare/36", "enabled": True},
    {"name": "搞七捻三", "url": "/c/gossip/11", "enabled": True},
    {"name": "社区孵化", "url": "/c/incubator/102", "enabled": False},  # 默认禁用
    {"name": "虫洞广场", "url": "/c/square/110", "enabled": True},
    {"name": "运营反馈", "url": "/c/feedback/2", "enabled": False},  # 默认禁用
]

# 默认配置
DEFAULT_CONFIG = {
    "base_url": "https://linux.do",
    "like_rate": 0.3,  # 点赞概率 30%
    "scroll_min": 3,  # 最小滚动次数
    "scroll_max": 8,  # 最大滚动次数
    "wait_min": 1,  # 最小等待时间（秒）
    "wait_max": 3,  # 最大等待时间（秒）
}


# ============================================================================
# 日志工具
# ============================================================================


class Logger:
    """简单的日志工具"""

    def __init__(self, debug=False):
        self.debug_mode = debug

    def _timestamp(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def info(self, msg):
        print(f"[{self._timestamp()}] [INFO] {msg}")

    def success(self, msg):
        print(f"[{self._timestamp()}] [OK] {msg}")

    def warning(self, msg):
        print(f"[{self._timestamp()}] [WARN] {msg}")

    def error(self, msg):
        print(f"[{self._timestamp()}] [ERROR] {msg}")

    def debug(self, msg):
        if self.debug_mode:
            print(f"[{self._timestamp()}] [DEBUG] {msg}")


# ============================================================================
# 核心类
# ============================================================================


class LinuxDoBot:
    """Linux.do 自动浏览机器人（无头版）"""

    def __init__(self, username=None, password=None, cookie=None, config=None, logger=None):
        """
        初始化机器人

        Args:
            username: Linux.do 用户名（Cookie 登录时可选）
            password: Linux.do 密码（Cookie 登录时可选）
            cookie: Cookie 字符串或 JSON（优先使用）
            config: 配置字典，可选
            logger: 日志工具，可选
        """
        self.username = username
        self.password = password
        self.cookie = cookie
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.log = logger or Logger()
        self.page = None
        self.stats = {
            "topics": 0,  # 浏览帖子数
            "likes": 0,  # 点赞数
            "floors": 0,  # 爬楼数
            "login_method": None,  # cookie / password
        }

    def _random_delay(self, min_sec=None, max_sec=None, reason=""):
        """随机延迟（防风控）"""
        min_sec = min_sec or self.config["wait_min"]
        max_sec = max_sec or self.config["wait_max"]
        delay = random.uniform(min_sec, max_sec)
        if reason:
            self.log.debug(f"等待 {delay:.1f}s ({reason})")
        time.sleep(delay)

    def start_browser(self, headless=True, proxy=None):
        """
        启动浏览器

        Args:
            headless: 是否无头模式
            proxy: 代理地址，如 "127.0.0.1:7897"

        Returns:
            bool: 是否成功
        """
        self.log.info("启动浏览器...")
        _ensure_drissionpage()

        try:
            options = ChromiumOptions()

            # 无头模式
            if headless:
                options.set_argument("--headless=new")
                self.log.info("无头模式已启用")

            # 代理设置（支持 host:port 或 http(s)://host:port）
            if proxy:
                proxy = self._normalize_proxy(proxy)
                options.set_proxy(proxy)
                self.log.info(f"代理已设置: {proxy}")

            # 反自动化检测
            options.set_argument("--disable-blink-features=AutomationControlled")
            options.set_argument("--no-sandbox")
            options.set_argument("--disable-dev-shm-usage")
            options.set_argument("--disable-gpu")
            options.set_argument("--window-size=1920,1080")
            options.set_argument("--lang=zh-CN")

            # 设置 User-Agent
            options.set_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            self.page = ChromiumPage(options)
            self.log.success("浏览器启动成功")
            return True

        except Exception as e:
            self.log.error(f"浏览器启动失败: {e}")
            return False

    @staticmethod
    def _normalize_proxy(proxy):
        """规范化代理地址，确保带协议前缀"""
        proxy = (proxy or "").strip()
        if not proxy:
            return proxy
        if "://" not in proxy:
            return f"http://{proxy}"
        return proxy

    def login(self):
        """
        登录 Linux.do

        优先级：
            1. Cookie 登录（可规避 GitHub IP 触发的 hCaptcha）
            2. 用户名 + 密码登录（兜底）

        Returns:
            bool: 是否成功
        """
        self.log.info("开始登录...")

        # 优先 Cookie 登录
        if self.cookie:
            if self.login_with_cookie(self.cookie):
                self.stats["login_method"] = "cookie"
                return True
            self.log.warning("Cookie 登录失败，尝试账号密码登录...")

        # 账号密码登录
        if self.username and self.password:
            if self.login_with_password():
                self.stats["login_method"] = "password"
                return True
            return False

        if self.cookie:
            self.log.error("Cookie 登录失败，且未提供用户名/密码")
        else:
            self.log.error("未提供 Cookie 或 用户名/密码，无法登录")
        return False

    def login_with_cookie(self, cookie_raw):
        """
        使用 Cookie 登录

        支持格式：
            1. 浏览器复制字符串: _t=xxx; _forum_session=yyy
            2. JSON 数组: [{"name":"_t","value":"xxx","domain":".linux.do"}, ...]
            3. JSON 对象: {"_t":"xxx","_forum_session":"yyy"}

        Args:
            cookie_raw: Cookie 原始字符串

        Returns:
            bool: 是否成功
        """
        self.log.info("尝试 Cookie 登录...")

        try:
            cookies = self._parse_cookies(cookie_raw)
            if not cookies:
                self.log.error("Cookie 解析结果为空")
                return False

            cookie_names = [c.get("name") for c in cookies if c.get("name")]
            self.log.debug(f"解析到 {len(cookies)} 个 Cookie: {', '.join(cookie_names)}")

            # 必须先访问域名，再写入 Cookie
            self.page.get(self.config["base_url"])
            self._random_delay(1, 2, "首页预热")

            # 写入 Cookie（兼容不同 DrissionPage 版本 API）
            self._set_cookies(cookies)
            self._random_delay(0.5, 1, "写入 Cookie 后")

            # 刷新验证登录状态
            if self._check_login():
                self.log.success("Cookie 登录成功")
                return True

            self.log.error("Cookie 登录失败，Cookie 可能已过期或无效")
            return False

        except Exception as e:
            self.log.error(f"Cookie 登录过程出错: {e}")
            return False

    def _parse_cookies(self, cookie_raw):
        """解析 Cookie 为 DrissionPage 可用的列表"""
        raw = (cookie_raw or "").strip()
        if not raw:
            return []

        domain = ".linux.do"
        cookies = []

        # JSON 格式
        if raw.startswith("[") or raw.startswith("{"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                self.log.error(f"Cookie JSON 解析失败: {e}")
                return []

            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name") or item.get("Name")
                    value = item.get("value") if "value" in item else item.get("Value")
                    if not name or value is None:
                        continue
                    cookie = {
                        "name": str(name),
                        "value": str(value),
                        "domain": item.get("domain") or item.get("Domain") or domain,
                        "path": item.get("path") or item.get("Path") or "/",
                    }
                    cookies.append(cookie)
                return cookies

            if isinstance(data, dict):
                # 可能是 {name: value} 或 单个 cookie 对象
                if "name" in data and "value" in data:
                    return [{
                        "name": str(data["name"]),
                        "value": str(data["value"]),
                        "domain": data.get("domain") or domain,
                        "path": data.get("path") or "/",
                    }]
                for name, value in data.items():
                    if value is None:
                        continue
                    cookies.append({
                        "name": str(name),
                        "value": str(value),
                        "domain": domain,
                        "path": "/",
                    })
                return cookies

            self.log.error("不支持的 Cookie JSON 结构")
            return []

        # 浏览器复制的 name=value; name2=value2 格式
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            })

        return cookies

    def _set_cookies(self, cookies):
        """写入 Cookie，兼容不同 DrissionPage API"""
        # 优先使用 set.cookies
        try:
            self.page.set.cookies(cookies)
            return
        except Exception as e:
            self.log.debug(f"set.cookies 失败，尝试备用方式: {e}")

        # 备用：逐个 set.cookies / set.cookie
        for cookie in cookies:
            try:
                if hasattr(self.page.set, "cookies"):
                    self.page.set.cookies(cookie)
                elif hasattr(self.page.set, "cookie"):
                    self.page.set.cookie(cookie)
                else:
                    # 最后手段：通过 JS document.cookie（仅当前域 path）
                    name = cookie["name"]
                    value = cookie["value"]
                    path = cookie.get("path", "/")
                    self.page.run_js(
                        f'document.cookie = "{name}={value}; path={path}; domain=.linux.do";'
                    )
            except Exception as e:
                self.log.debug(f"写入 Cookie {cookie.get('name')} 失败: {e}")

    def login_with_password(self):
        """
        使用用户名 + 密码登录

        Returns:
            bool: 是否成功
        """
        self.log.info("尝试账号密码登录...")

        try:
            # 访问登录页面
            login_url = f"{self.config['base_url']}/login"
            self.page.get(login_url)
            self._random_delay(2, 4, "页面加载")

            # 输入用户名
            self.log.debug("输入用户名...")
            username_input = self.page.ele("#login-account-name", timeout=10)
            if not username_input:
                self.log.error("未找到用户名输入框")
                return False
            username_input.clear()
            username_input.input(self.username)
            self._random_delay(0.5, 1, "输入用户名后")

            # 输入密码
            self.log.debug("输入密码...")
            password_input = self.page.ele("#login-account-password", timeout=5)
            if not password_input:
                self.log.error("未找到密码输入框")
                return False
            password_input.clear()
            password_input.input(self.password)
            self._random_delay(0.5, 1, "输入密码后")

            # 点击登录按钮
            self.log.debug("点击登录按钮...")
            login_btn = self.page.ele("#login-button", timeout=5)
            if not login_btn:
                self.log.error("未找到登录按钮")
                return False
            login_btn.click()

            # 等待登录完成
            self._random_delay(3, 5, "等待登录")

            # 检测 hCaptcha
            if self._has_hcaptcha():
                self.log.error("检测到 hCaptcha 验证码，账号密码登录无法在 Actions 中完成")
                self.log.error("请改用 Cookie 登录：设置 Secret LINUXDO_COOKIE")
                return False

            # 验证登录状态
            if self._check_login():
                self.log.success("账号密码登录成功")
                return True
            else:
                self.log.error("登录失败，请检查用户名和密码，或改用 Cookie 登录")
                return False

        except Exception as e:
            self.log.error(f"账号密码登录过程出错: {e}")
            return False

    def _has_hcaptcha(self):
        """检测页面是否出现 hCaptcha"""
        try:
            # iframe / 常见 hCaptcha 元素
            if self.page.ele("iframe[src*='hcaptcha']", timeout=1):
                return True
            if self.page.ele(".h-captcha", timeout=1):
                return True
            if self.page.ele("#h-captcha", timeout=1):
                return True
            # 页面文本兜底
            html = self.page.html or ""
            return "hcaptcha" in html.lower()
        except Exception:
            return False

    def _check_login(self):
        """检查是否已登录"""
        try:
            # 访问首页
            self.page.get(self.config["base_url"])
            self._random_delay(2, 3)

            # 检查用户头像元素
            user_ele = self.page.ele("#current-user", timeout=5)
            return user_ele is not None
        except Exception:
            return False

    def get_topics(self, category):
        """
        获取板块帖子列表

        Args:
            category: 板块配置字典

        Returns:
            list: 帖子列表
        """
        url = self.config["base_url"] + category["url"]
        self.log.info(f"进入板块: {category['name']}")

        try:
            self.page.get(url)
            self._random_delay(2, 4, "板块加载")

            # 使用 JS 获取帖子列表
            topics = self.page.run_js("""
            function getTopics() {
                const rows = document.querySelectorAll('tr.topic-list-item');
                const topics = [];
                rows.forEach(row => {
                    const link = row.querySelector('a.title.raw-link.raw-topic-link');
                    if (link) {
                        const href = link.getAttribute('href');
                        const title = link.textContent.trim();
                        // 跳过置顶帖
                        if (href && title && !row.classList.contains('pinned')) {
                            topics.push({
                                url: href,
                                title: title.substring(0, 50)
                            });
                        }
                    }
                });
                return topics;
            }
            return getTopics();
            """)

            self.log.debug(f"找到 {len(topics or [])} 个帖子")
            return topics or []

        except Exception as e:
            self.log.error(f"获取帖子列表失败: {e}")
            return []

    def browse_topic(self, topic):
        """
        浏览单个帖子

        Args:
            topic: 帖子信息字典

        Returns:
            bool: 是否成功
        """
        url = topic["url"]
        if url.startswith("/"):
            url = self.config["base_url"] + url

        title = (
            topic["title"][:30] + "..." if len(topic["title"]) > 30 else topic["title"]
        )
        self.log.info(f"浏览: {title}")

        try:
            self.page.get(url)
            self._random_delay(2, 3, "帖子加载")

            # 滚动阅读
            scroll_count = random.randint(
                self.config["scroll_min"], self.config["scroll_max"]
            )

            for i in range(scroll_count):
                # 随机滚动距离
                distance = random.randint(300, 800)
                self.page.run_js(f"window.scrollBy(0, {distance})")
                self._random_delay(1, 2.5, f"滚动 {i + 1}/{scroll_count}")

                # 检查是否到底部
                at_bottom = self.page.run_js("""
                return (window.innerHeight + window.scrollY) >= document.body.offsetHeight - 100;
                """)
                if at_bottom:
                    self.log.debug("已到达页面底部")
                    break

            self.stats["topics"] += 1
            self.stats["floors"] += scroll_count

            # 随机点赞
            if random.random() < self.config["like_rate"]:
                self._do_like()

            return True

        except Exception as e:
            self.log.error(f"浏览帖子失败: {e}")
            return False

    def _do_like(self):
        """点赞主帖"""
        try:
            result = self.page.run_js("""
            function clickLike() {
                const buttons = document.querySelectorAll('button.btn-toggle-reaction-like');
                if (buttons.length > 0) {
                    const btn = buttons[0];
                    if (!btn.classList.contains('has-like') && !btn.classList.contains('my-likes')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
            return clickLike();
            """)

            if result:
                self.stats["likes"] += 1
                self.log.success("点赞成功")
                self._random_delay(0.5, 1.5, "点赞后")

        except Exception as e:
            self.log.debug(f"点赞失败: {e}")

    def run(self, target_topics=30, headless=True, proxy=None):
        """
        运行自动浏览任务

        Args:
            target_topics: 目标浏览帖子数
            headless: 是否无头模式
            proxy: 代理地址

        Returns:
            dict: 统计结果
        """
        self.log.info("=" * 60)
        self.log.info("Linux.do 自动浏览任务开始")
        self.log.info(f"目标: 浏览 {target_topics} 个帖子")
        if self.cookie:
            self.log.info("登录方式: Cookie 优先")
        elif self.username:
            self.log.info("登录方式: 账号密码")
        self.log.info("=" * 60)

        start_time = time.time()

        try:
            # 启动浏览器
            if not self.start_browser(headless=headless, proxy=proxy):
                return self.stats

            # 登录（Cookie 优先，账号密码兜底）
            if not self.login():
                return self.stats

            # 获取启用的板块
            enabled_categories = [c for c in CATEGORIES if c.get("enabled", True)]
            random.shuffle(enabled_categories)

            self.log.info(f"将浏览 {len(enabled_categories)} 个板块")

            # 开始浏览
            while self.stats["topics"] < target_topics:
                for category in enabled_categories:
                    if self.stats["topics"] >= target_topics:
                        break

                    # 获取帖子列表
                    topics = self.get_topics(category)
                    if not topics:
                        continue

                    # 随机选择几个帖子
                    count = min(random.randint(2, 5), len(topics))
                    selected = random.sample(topics, count)

                    for topic in selected:
                        if self.stats["topics"] >= target_topics:
                            break

                        self.browse_topic(topic)
                        self._random_delay(reason="切换帖子")

                # 如果一轮结束还没达到目标，重新打乱板块顺序
                random.shuffle(enabled_categories)

        except KeyboardInterrupt:
            self.log.warning("用户中断")

        except Exception as e:
            self.log.error(f"运行出错: {e}")

        finally:
            # 关闭浏览器
            if self.page:
                try:
                    self.page.quit()
                except:
                    pass

        # 统计结果
        elapsed = time.time() - start_time
        elapsed_min = int(elapsed / 60)
        elapsed_sec = int(elapsed % 60)

        self.log.info("=" * 60)
        self.log.info("任务完成")
        self.log.info(f"用时: {elapsed_min}分{elapsed_sec}秒")
        if self.stats.get("login_method"):
            self.log.info(f"登录方式: {self.stats['login_method']}")
        self.log.info(f"浏览帖子: {self.stats['topics']}")
        self.log.info(f"点赞数: {self.stats['likes']}")
        self.log.info(f"滚动次数: {self.stats['floors']}")
        self.log.info("=" * 60)

        return self.stats


# ============================================================================
# 命令行入口
# ============================================================================


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Linux.do 论坛自动浏览脚本（无头版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python linux_do_headless.py -u myuser -p mypass
  python linux_do_headless.py --cookie "_t=xxx; _forum_session=yyy"
  python linux_do_headless.py --cookie "$LINUXDO_COOKIE" --proxy http://127.0.0.1:7890
  python linux_do_headless.py -u myuser -p mypass --topics 50
  python linux_do_headless.py -u myuser -p mypass --proxy 127.0.0.1:7897

环境变量:
  LINUXDO_COOKIE    Cookie（推荐，可规避 hCaptcha）
  LINUXDO_USERNAME  用户名（Cookie 失效时兜底）
  LINUXDO_PASSWORD  密码（Cookie 失效时兜底）
  LINUXDO_PROXY     代理地址（可选）
        """,
    )

    parser.add_argument(
        "-u", "--username", help="Linux.do 用户名（或设置环境变量 LINUXDO_USERNAME）"
    )
    parser.add_argument(
        "-p", "--password", help="Linux.do 密码（或设置环境变量 LINUXDO_PASSWORD）"
    )
    parser.add_argument(
        "--cookie",
        help="Cookie 字符串/JSON（或设置环境变量 LINUXDO_COOKIE）。优先于账号密码",
    )
    parser.add_argument(
        "--proxy", help="代理地址，如 127.0.0.1:7890 或 http://127.0.0.1:7890"
    )
    parser.add_argument("--topics", type=int, default=30, help="浏览帖子数量，默认 30")
    parser.add_argument(
        "--like-rate", type=int, default=30, help="点赞概率（0-100），默认 30"
    )
    parser.add_argument(
        "--no-headless", action="store_true", help="禁用无头模式（显示浏览器窗口）"
    )
    parser.add_argument("--debug", action="store_true", help="调试模式")

    return parser.parse_args()


def main():
    """主函数"""
    args = parse_args()

    # 获取登录凭证与代理（优先命令行参数，其次环境变量）
    username = args.username or os.environ.get("LINUXDO_USERNAME")
    password = args.password or os.environ.get("LINUXDO_PASSWORD")
    cookie = args.cookie or os.environ.get("LINUXDO_COOKIE")
    proxy = args.proxy or os.environ.get("LINUXDO_PROXY")

    # 验证必要参数：Cookie 或 用户名+密码 至少一种
    has_cookie = bool(cookie and cookie.strip())
    has_password = bool(username and password)
    if not has_cookie and not has_password:
        print("错误: 请提供 Cookie，或提供用户名和密码")
        print()
        print("方式一: Cookie 登录（推荐，可规避 hCaptcha）")
        print('  python linux_do_headless.py --cookie "_t=xxx; _forum_session=yyy"')
        print("  或: export LINUXDO_COOKIE='_t=xxx; _forum_session=yyy'")
        print()
        print("方式二: 账号密码登录")
        print("  python linux_do_headless.py -u 用户名 -p 密码")
        print("  或: export LINUXDO_USERNAME='用户名' && export LINUXDO_PASSWORD='密码'")
        sys.exit(1)

    # 创建日志工具
    logger = Logger(debug=args.debug)

    # 配置
    config = {
        "like_rate": args.like_rate / 100,  # 转换为小数
    }

    # 创建机器人并运行
    bot = LinuxDoBot(
        username=username,
        password=password,
        cookie=cookie,
        config=config,
        logger=logger,
    )

    stats = bot.run(
        target_topics=args.topics, headless=not args.no_headless, proxy=proxy
    )

    # 返回状态码
    sys.exit(0 if stats["topics"] > 0 else 1)


if __name__ == "__main__":
    main()
