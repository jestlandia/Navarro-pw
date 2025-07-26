import sys
import re
import os
import json
import time
import atexit
import random
import platform
import argparse
import subprocess
import pytesseract
from PIL import Image
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Callable, Tuple, List, Optional
from enum import Enum
from collections import defaultdict
from functools import wraps
from playwright.sync_api import sync_playwright, Browser, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
from agents import UA, USER_AGENTS
try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    RICH = True
except ImportError:
    RICH = False



TIMEOUT = 8
RATE_LIMIT_FILE = Path.home() / ".navarro_rate_limits.json"

class CheckResult(Enum):
    """Result types for platform checks"""
    FOUND = "found"
    NOT_FOUND = "not_found"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNKNOWN_ERROR = "unknown_error"

class RateLimiter:
    """Rate limiter with persistence"""
    def __init__(self):
        # Fixed: Create datetime objects directly, not strings
        self.limits = defaultdict(lambda: {"count": 0, "reset_time": datetime.now()})
        self.delays = defaultdict(lambda: 0.5)  # Base delay per platform
        self.last_request = defaultdict(lambda: datetime.now())
        self.load_limits()
    
    def load_limits(self):
        """Load saved rate limits from disk"""
        if RATE_LIMIT_FILE.exists():
            try:
                with open(RATE_LIMIT_FILE, 'r') as f:
                    saved_data = json.load(f)
                    # Convert ISO format strings back to datetime objects with error handling
                    for platform, limit_data in saved_data.get('limits', {}).items():
                        try:
                            reset_time = datetime.fromisoformat(limit_data.get("reset_time", datetime.now().isoformat()))
                        except (ValueError, TypeError):
                            reset_time = datetime.now()
                        
                        self.limits[platform] = {
                            "count": limit_data.get("count", 0),
                            "reset_time": reset_time
                        }
                    self.delays.update(saved_data.get('delays', {}))
            except Exception:
                pass
    
    def save_limits(self):
        """Save rate limits to disk"""
        try:
            # Convert datetime objects to ISO format for JSON serialization
            limits_to_save = {}
            for platform, limit_data in self.limits.items():
                limits_to_save[platform] = {
                    "count": limit_data["count"],
                    "reset_time": limit_data["reset_time"].isoformat() if isinstance(limit_data["reset_time"], datetime) else limit_data["reset_time"]
                }
            
            with open(RATE_LIMIT_FILE, 'w') as f:
                json.dump({
                    'limits': limits_to_save,
                    'delays': dict(self.delays)
                }, f, indent=2)
        except Exception:
            pass
    
    def should_wait(self, platform: str) -> float:
        """Calculate wait time for platform"""
        now = datetime.now()
        
        reset_time = self.limits[platform]["reset_time"]
        if isinstance(reset_time, str):
            try:
                reset_time = datetime.fromisoformat(reset_time)
            except (ValueError, TypeError):
                reset_time = now
        
        if reset_time > now:
            return (reset_time - now).total_seconds()
        
        time_since_last = (now - self.last_request[platform]).total_seconds()
        if time_since_last < self.delays[platform]:
            return self.delays[platform] - time_since_last
        
        return 0
    
    def record_request(self, platform: str, was_rate_limited: bool = False):
        """Record a request and update delays"""
        now = datetime.now()
        self.last_request[platform] = now
        if was_rate_limited:
            self.delays[platform] = min(self.delays[platform] * 2, 30)
            self.limits[platform]["reset_time"] = now + timedelta(seconds=60)
        else:
            self.delays[platform] = max(self.delays[platform] * 0.9, 0.5)
        self.save_limits()

class PlaywrightSessionManager:
    """Manage persistent browser contexts with Playwright"""
    def __init__(self):
        self.playwright = sync_playwright().start()
        browser_type = random.choice([self.playwright.chromium, self.playwright.firefox])
        self.browser: Browser = browser_type.launch(headless=True)
        self.contexts: Dict[str, BrowserContext] = {}
        self._user_agent_index = 0

    def get_context(self, platform: str) -> BrowserContext:
        """Get or create a browser context for a platform"""
        if platform not in self.contexts:
            user_agent = self._get_next_user_agent()
            context = self.browser.new_context(user_agent=user_agent)
            self.contexts[platform] = context
        return self.contexts[platform]

    def get_page(self, platform: str):
        """Get a new page from the platform's browser context"""
        context = self.get_context(platform)
        return context.new_page()

    def _get_next_user_agent(self) -> str:
        """Rotate through user agents"""
        ua = USER_AGENTS[self._user_agent_index % len(USER_AGENTS)]
        self._user_agent_index += 1
        return ua

    def close_all(self):
        """Close all contexts and the browser"""
        for context in self.contexts.values():
            context.close()
        self.browser.close()
        self.playwright.stop()
        self.contexts.clear()


# Global instances
rate_limiter = RateLimiter()
session_manager = PlaywrightSessionManager()

# Register cleanup on exit
atexit.register(session_manager.close_all)

def handle_request_errors(func):
    """Decorator to handle common Playwright errors and return appropriate CheckResult"""
    @wraps(func)
    def wrapper(username):
        try:
            return func(username)
        except PlaywrightTimeoutError:
            return CheckResult.TIMEOUT
        except PlaywrightError:
            return CheckResult.NETWORK_ERROR
        except Exception:
            return CheckResult.UNKNOWN_ERROR
    return wrapper

def check_rate_limit(content: str) -> bool:
    """Check if page content indicates rate limiting"""
    if not content:
        return False
    content = content.lower()
    rate_limit_patterns = [
        "rate limit exceeded",
        "too many requests",
        "429 too many requests",
        "you have triggered a rate limit",
        "please wait before retrying",
        "retry later",
        "temporarily blocked",
    ]
    return any(pattern in content for pattern in rate_limit_patterns)

#----------------------------#
#    SOCIAL MEDIA QUERIES    #
#----------------------------#
@handle_request_errors
def github(username: str) -> CheckResult:
    page = session_manager.get_page("github")
    url = f"https://github.com/{username}"
    try:
        page.goto(url, timeout=TIMEOUT * 1000)
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("github", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("github")
        not_found_markers = [
            "Not Found",
            "Profile not found",
            'alt="404 “This is not the web page you are looking for”"',
            "User not found",
            "Sorry! We couldn't find",
            "Oops! We couldn't find",
        ]
        if any(marker in content for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        return CheckResult.FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("github", was_rate_limited=True)
        return CheckResult.RATE_LIMITED
    finally:
        page.close()

@handle_request_errors
def discord(username: str) -> CheckResult:
    page = session_manager.get_page("discord")
    url = "https://discord.com/register"
    try:
        page.goto(url, timeout=TIMEOUT * 1000)
        page.fill('input[name="username"]', username)
        delay = random.uniform(1800, 3000)
        page.wait_for_timeout(delay)
        page.wait_for_selector('div.defaultColor__4bd52')
        text = page.inner_text('div.defaultColor__4bd52')
        if "Username is unavailable." in text:
            return CheckResult.FOUND
        else:
            return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("discord", was_rate_limited=True)
        return CheckResult.RATE_LIMITED
    finally:
        page.close()

@handle_request_errors
def pastebin(username) -> CheckResult:
    page = session_manager.get_page("pastebin")
    url = f"https://pastebin.com/u/{username}"
    try:
        page.goto(url, timeout=TIMEOUT * 1000)
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("pastebin", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("pastebin")
        if "<title>Pastebin.com - Not Found (#404)</title>" in content:
            return CheckResult.NOT_FOUND
        return CheckResult.FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("pastebin", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def telegram(username) -> CheckResult:
    page = session_manager.get_page("telegram")
    url = f"https://t.me/{username}"
    try:
        page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        content = page.content()
        final_url = page.url
        if check_rate_limit(content):
            rate_limiter.record_request("telegram", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("telegram")
        if final_url.startswith("https://telegram.org"):
            return CheckResult.NOT_FOUND
        content_lower = content.lower()
        if '"@type":"person"' in content_lower or '"@type":"organization"' in content_lower:
            return CheckResult.FOUND
        if 'og:image' in content_lower and 'cdn' in content_lower:
            if 'telegram_logo' not in content_lower and 'default' not in content_lower:
                return CheckResult.FOUND
        has_title = 'property="og:title"' in content_lower or 'name="twitter:title"' in content_lower
        has_unique_title = f'<meta property="og:title" content="telegram: contact @{username}"' not in content_lower ##doublecheck this rule
        has_description = 'property="og:description"' in content_lower or 'name="twitter:description"' in content_lower 
        if has_title and has_unique_title and has_description and username.lower() in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("telegram", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def pinterest(username) -> CheckResult:
    page = session_manager.get_page("pinterest")
    url = f"https://www.pinterest.com/{username}/"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("pinterest", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("pinterest")
        if response is None or response.status != 200:
            return CheckResult.NOT_FOUND
        text = content
        profile_markers = [
            '"@type":"Person"',
            '"profileOwner":',
            f'"username":"{username}"',
            '"pinterestapp:followers"',
        ]
        not_found_markers = [
            "User not found",
            "Sorry! We couldn't find",
            "Oops! We couldn't find",
        ]
        if any(marker in text for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if any(marker in text for marker in profile_markers):
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("pinterest", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def youtube(username) -> CheckResult:
    page = session_manager.get_page("youtube")
    urls = [
        f"https://www.youtube.com/@{username}",
        f"https://www.youtube.com/c/{username}",
        f"https://www.youtube.com/user/{username}",
    ]
    try:
        for url in urls:
            try:
                resp = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                final_url = page.url
                status = resp.status if resp else None

                if resp is None or status != 200:
                    continue
                content = page.content()
                content_lower = content.lower()
                if check_rate_limit(content):
                    rate_limiter.record_request("youtube", was_rate_limited=True)
                    return CheckResult.RATE_LIMITED
                not_found_markers = [
                    '{"error":{"code":404',
                    "this page isn't available",
                    "<title>404 not found</title>"
                ]
                if any(marker in content_lower for marker in not_found_markers):
                    continue
                channel_markers = [
                    '"channelid":"',
                    '"ownertext":',
                    '"subscribercounttext":',
                    '"@type":"channel"',
                    'subscribers',
                    f'@{username}'
                ]
                channel_detected = any(marker in content_lower for marker in channel_markers)
                if channel_detected:
                    rate_limiter.record_request("youtube")
                    return CheckResult.FOUND
            except PlaywrightTimeoutError:
                rate_limiter.record_request("youtube", was_rate_limited=True)
                return CheckResult.TIMEOUT
        rate_limiter.record_request("youtube")
        return CheckResult.NOT_FOUND
    finally:
        page.close()

@handle_request_errors
def vimeo(username) -> CheckResult:
    page = session_manager.get_page("vimeo")
    url = f"https://vimeo.com/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status == 404:
            rate_limiter.record_request("vimeo")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("vimeo", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("vimeo")
        content_lower = content.lower()
        not_found_markers = [
            "sorry, we couldn't find that page",
            "page not found",
            "this page is no longer available"
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if username.lower() in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("vimeo", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def keybase(username) -> CheckResult:
    page = session_manager.get_page("keybase")
    url = f"https://keybase.io/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("keybase")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("keybase", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("keybase")
        content_lower = content.lower()
        not_found_markers = [
            "user not found",
            "404",
            "no such user",
        ]
        profile_markers = [
            f'"username":"{username.lower()}"',
            '"proofs_summary"',
            '"stellar"',
            '"bitcoin"',
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if any(marker in content_lower for marker in profile_markers):
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("keybase", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def rutube(username) -> CheckResult:
    page = session_manager.get_page("rutube")
    url = f"https://rutube.com/u/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status == 404:
            rate_limiter.record_request("rutube")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("rutube", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("rutube")
        content_lower = content.lower()
        not_found_markers = [
            "sorry, we couldn't find that page",
            "page not found",
            "404",
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if username.lower() in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("rutube", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def deviantart(username) -> CheckResult:
    page = session_manager.get_page("deviantart")
    url = f"https://www.deviantart.com/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status == 404:
            rate_limiter.record_request("deviantart")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("deviantart", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("deviantart")
        content_lower = content.lower()
        not_found_markers = [
            "doesn't exist",
            "the page you're looking for",
            "deviantart: page not found",
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if username.lower() in content_lower or 'deviantart.com' in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("deviantart", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def vk(username) -> CheckResult:
    page = session_manager.get_page("vk")
    url = f"https://vk.com/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("vk")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("vk", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("vk")
        content_lower = content.lower()
        not_found_markers = [
            "profile not found",
            "страница удалена",
            "страница не найдена",
            "is unavailable",
            "has been deleted",
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if '<div class="page_name"' in content_lower or "wall_tab_all" in content_lower:
            return CheckResult.FOUND
        if username.lower() in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("vk", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def chessdotcom(username) -> CheckResult:
    page = session_manager.get_page("chessdotcom")
    url = f"https://www.chess.com/member/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("chessdotcom")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("chessdotcom", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("chessdotcom")
        content_lower = content.lower()
        if username.lower() in content_lower and "chess.com" in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("chessdotcom", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def medium(username) -> CheckResult:
    page = session_manager.get_page("medium")
    url = f"https://medium.com/@{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status == 404:
            rate_limiter.record_request("medium")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("medium", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("medium")
        content_lower = content.lower()
        not_found_markers = [
            "we couldn't find this page",
            "page not found",
            "404",
        ]
        profile_markers = [
            '"@type":"person"',
            '"creator":{"@type":"person"',
            f'"identifier":"@{username}"',
            '"userfollowbutton"',
        ]
        if any(marker in content_lower for marker in not_found_markers):
            return CheckResult.NOT_FOUND
        if any(marker in content_lower for marker in profile_markers):
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("medium", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def soundcloud(username) -> CheckResult:
    page = session_manager.get_page("soundcloud")
    url = f"https://soundcloud.com/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("soundcloud")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("soundcloud", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("soundcloud")
        content_lower = content.lower()
        if "soundcloud" in content_lower or username.lower() in content_lower:
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("soundcloud", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def spotify(username) -> CheckResult:
    page = session_manager.get_page("spotify")
    url = f"https://open.spotify.com/user/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("spotify")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("spotify", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("spotify")
        if username.lower() in content.lower():
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("spotify", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def bluesky(username) -> CheckResult:
    page = session_manager.get_page("bluesky")
    candidates = [f"{username}.bsky.social", username]
    try:
        for user in candidates:
            url = f"https://bsky.app/profile/{user}"
            response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            if response is None or response.status != 200:
                continue
            content = page.content()
            if check_rate_limit(content):
                rate_limiter.record_request("bluesky", was_rate_limited=True)
                return CheckResult.RATE_LIMITED
            if username.lower() in content.lower() or "posts" in content.lower():
                rate_limiter.record_request("bluesky")
                return CheckResult.FOUND
        rate_limiter.record_request("bluesky")
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("bluesky", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def mastodon(username) -> CheckResult:
    page = session_manager.get_page("mastodon")
    instances = ["mastodon.social", "hachyderm.io", "infosec.exchange"]
    try:
        for instance in instances:
            url = f"https://{instance}/@{username}"
            try:
                response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                if response is None or response.status != 200:
                    continue
                content = page.content()
                if check_rate_limit(content):
                    rate_limiter.record_request("mastodon", was_rate_limited=True)
                    return CheckResult.RATE_LIMITED
                if f"@{username.lower()}" in content.lower() or username.lower() in content.lower():
                    rate_limiter.record_request("mastodon")
                    return CheckResult.FOUND
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        rate_limiter.record_request("mastodon")
        return CheckResult.NOT_FOUND
    finally:
        page.close()

@handle_request_errors
def strava(username) -> CheckResult:
    page = session_manager.get_page("strava")
    url = f"https://www.strava.com/athletes/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("strava")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("strava", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        if username in content or "Athlete" in content:
            rate_limiter.record_request("strava")
            return CheckResult.FOUND
        rate_limiter.record_request("strava")
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("strava", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def threads(username) -> CheckResult:
    page = session_manager.get_page("threads")
    url = f"https://www.threads.net/@{username}"

    profile_markers = [
        '"user":{"pk"',
        '"profile_pic_url"',
        f'"username":"{username}"',
        '"thread_items"',
    ]

    not_found_markers = [
        "Sorry, this page isn't available",
        "User not found",
    ]

    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("threads")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("threads", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        for marker in not_found_markers:
            if marker in content:
                rate_limiter.record_request("threads")
                return CheckResult.NOT_FOUND

        if any(marker in content for marker in profile_markers):
            rate_limiter.record_request("threads")
            return CheckResult.FOUND

        rate_limiter.record_request("threads")
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("threads", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def snapchat(username) -> CheckResult:
    page = session_manager.get_page("snapchat")
    url = f"https://www.snapchat.com/add/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("snapchat")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("snapchat", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        if 'Snapcode' in content:
            rate_limiter.record_request("snapchat")
            return CheckResult.FOUND

        rate_limiter.record_request("snapchat")
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("snapchat", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def linkedin(username) -> CheckResult:
    page = session_manager.get_page("linkedin")
    url = f"https://www.linkedin.com/in/{username}"
    profile_markers = [
        '"profile":',
        '"publicIdentifier":"',
        '"firstName":"',
        '"lastName":"',
        '"headline":"',
    ]

    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("linkedin")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("linkedin", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        if any(marker in content for marker in profile_markers):
            rate_limiter.record_request("linkedin")
            return CheckResult.FOUND

        rate_limiter.record_request("linkedin")
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("linkedin", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def linktree(username) -> CheckResult:
    page = session_manager.get_page("linktree")
    url = f"https://linktr.ee/{username}"

    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("linktree")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("linktree", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        if "Sorry, this page isn't available" in content or "404" in content or "The page you’re looking for doesn’t exist." in content:
            rate_limiter.record_request("linktree")
            return CheckResult.NOT_FOUND

        if username.lower() in content.lower() or "linktr.ee" in content.lower():
            rate_limiter.record_request("linktree")
            return CheckResult.FOUND

        rate_limiter.record_request("linktree")
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("linktree", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def instagram(username) -> CheckResult:
    page = session_manager.get_page("instagram")
    url = f"https://www.instagram.com/{username}/"

    not_found_indicators = [
        "isn't available",
        "not available",
        "page isn't available", 
        "profile isn't available",
        "The link may be broken",
        "profile may have been removed",
        '"challengeType":"UNKNOWN"',
        '"viewer":null',
    ]

    user_data_patterns = [
        f'"username":"{username}"',
        f'"alternateName":"@{username}"',
        '"edge_followed_by":{"count":',
        '"profile_pic_url":"http',
        '"is_private":',
        '"media_count":',
    ]

    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            rate_limiter.record_request("instagram")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("instagram", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        if '"user":null' in content or re.search(r'"user":\s*{\s*}', content):
            rate_limiter.record_request("instagram")
            return CheckResult.NOT_FOUND

        content_lower = content.lower()
        for indicator in not_found_indicators:
            if indicator.lower() in content_lower:
                rate_limiter.record_request("instagram")
                return CheckResult.NOT_FOUND

        if "/accounts/login/" in page.url:
            rate_limiter.record_request("instagram")
            return CheckResult.NOT_FOUND

        if any(pattern in content for pattern in user_data_patterns):
            rate_limiter.record_request("instagram")
            return CheckResult.FOUND

        rate_limiter.record_request("instagram")
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("instagram", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def steam(username) -> CheckResult:
    page = session_manager.get_page("steam")
    url = f"https://steamcommunity.com/id/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None:
            rate_limiter.record_request("steam")
            return CheckResult.NOT_FOUND

        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("steam", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        rate_limiter.record_request("steam")

        if response.status == 404:
            return CheckResult.NOT_FOUND
        
        if "The specified profile could not be found" in content:
            return CheckResult.NOT_FOUND

        if 'class="profile_header_bg"' in content or 'steamcommunity.com/id/' in content:
            return CheckResult.FOUND
        
        if username.lower() in content.lower():
            return CheckResult.FOUND
        
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("steam", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def reddit(username) -> CheckResult:
    page = session_manager.get_page("reddit")
    url = f"https://www.reddit.com/user/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None:
            rate_limiter.record_request("reddit")
            return CheckResult.NOT_FOUND
        
        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("reddit", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        rate_limiter.record_request("reddit")

        if response.status == 200 and not re.search(r"nobody on Reddit goes by that name", content, re.IGNORECASE):
            return CheckResult.FOUND
        
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("reddit", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def gitlab(username) -> CheckResult:
    page = session_manager.get_page("gitlab")
    url = f"https://gitlab.com/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        if response is None:
            rate_limiter.record_request("gitlab")
            return CheckResult.NOT_FOUND
        
        content = page.content()

        if check_rate_limit(content):
            rate_limiter.record_request("gitlab", was_rate_limited=True)
            return CheckResult.RATE_LIMITED

        rate_limiter.record_request("gitlab")

        if response.status == 200 and re.search(r'<h1>[\w\-]+', content):
            return CheckResult.FOUND
        
        return CheckResult.NOT_FOUND

    except PlaywrightTimeoutError:
        rate_limiter.record_request("gitlab", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()

@handle_request_errors
def facebook(username: str) -> CheckResult:
    """
    Playwright-based check if a Facebook profile/page exists.
    Strategy:
    - Try Graph API (via page.goto + fetch) first.
    - For usernames with special chars or fallback, do direct page check.
    """

    page = session_manager.get_page("facebook")
    rate_limiter.record_request("facebook")

    def _graph_ok(slug: str) -> bool:
        graph_url = f"https://graph.facebook.com/{slug}/picture?type=normal&redirect=false"
        try:
            # Use Playwright to fetch the JSON from the Graph API
            response = page.request.get(graph_url, headers=UA, timeout=TIMEOUT * 1000)
            if response.status != 200:
                return False
            json_data = response.json()
            data = json_data.get("data")
            return (
                isinstance(data, dict)
                and data.get("url")
                and data.get("width")
                and "facebook.com" in data.get("url", "")
            )
        except Exception:
            return False

    def _direct_check(slug: str) -> bool:
        url = f"https://www.facebook.com/{slug}"
        try:
            response = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            if response is None or response.status != 200:
                return False
            text = page.content()

            # Negative indicators for profile not found
            definite_not_found_indicators = [
                "This content isn't available right now",
                "This page isn't available",
                "Page Not Found",
                "Content Not Found",
                "The page you requested cannot be displayed",
                "Sorry, this page isn't available",
                '"error":{"message":"Unsupported get request',
                '"error":{"message":"(#803)',
                '"error":{"message":"Invalid username',
                "profile unavailable",
                "Page not found",
            ]
            text_lower = text.lower()
            if any(indicator.lower() in text_lower for indicator in definite_not_found_indicators):
                return False

            # Basic Facebook page structure indicators
            basic_facebook_indicators = [
                'id="facebook"',
                'property="og:site_name" content="Facebook"',
                'name="twitter:site" content="@facebook"',
                "<title>",
                "www.facebook.com",
            ]
            has_basic_structure = any(indicator in text for indicator in basic_facebook_indicators)
            if not has_basic_structure:
                return False

            url_indicators = [
                f'facebook.com/{slug}',
                f'content="https://www.facebook.com/{slug}"',
                f'content="https://facebook.com/{slug}"',
            ]
            has_url_match = any(indicator in text for indicator in url_indicators)

            # More lenient check if username contains special chars
            if any(char in slug for char in ".-_"):
                return has_basic_structure
            else:
                return has_url_match or '"userID":"' in text or '"pageID":"' in text

        except PlaywrightTimeoutError:
            return False
        except Exception:
            return False

    # 1. Try Graph API with original username
    if _graph_ok(username):
        page.close()
        return CheckResult.FOUND

    # 2. Try Graph API with cleaned username (remove '.' and '-')
    cleaned = re.sub(r"[.\-]", "", username)
    if cleaned != username and _graph_ok(cleaned):
        page.close()
        return CheckResult.FOUND

    # 3. Fallback to direct check for usernames with special chars or always
    if any(char in username for char in ".-") or True:
        if _direct_check(username):
            page.close()
            return CheckResult.FOUND
        if cleaned != username and _direct_check(cleaned):
            page.close()
            return CheckResult.FOUND

    page.close()
    return CheckResult.NOT_FOUND

@handle_request_errors
def tiktok(username) -> CheckResult:
    page = session_manager.get_page("tiktok")
    url = f"https://www.tiktok.com/@{username}"
    try:
        response = page.goto(url, wait_until='load', timeout=TIMEOUT * 1000)
        if response is None:
            rate_limiter.record_request("tiktok")
            return CheckResult.NOT_FOUND
        if response.status != 200:
            rate_limiter.record_request("tiktok")
            return CheckResult.NOT_FOUND
        text = page.content()
        if check_rate_limit(text):
            rate_limiter.record_request("tiktok", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("tiktok")
        not_found_markers = [
            "Couldn't find this account",
            "Impossible de trouver ce compte",
            '<h1>404</h1>',
            '"statusCode":10202',
            "page not available",
        ]
        for marker in not_found_markers:
            if marker in text:
                return CheckResult.NOT_FOUND
        user_data_patterns = [
            f'"uniqueId":"{username}"',
            f'"@{username}"',
            '"__typename":"User"',
            '"followerCount":',
            '"videoCount":',
        ]
        if any(pattern in text for pattern in user_data_patterns):
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except Exception:
        rate_limiter.record_request("tiktok")
        return CheckResult.NOT_FOUND

@handle_request_errors
def twitch(username) -> CheckResult:
    page = session_manager.get_page("twitch")
    page.set_viewport_size({"width": 1920, "height": 1080})
    url = f"https://www.twitch.tv/{username}"
    try:
        response = page.goto(url, timeout=TIMEOUT * 1000, wait_until='load')
        if response is None:
            rate_limiter.record_request("twitch")
            return CheckResult.NOT_FOUND
        content = page.content()
        if check_rate_limit(content):
            rate_limiter.record_request("twitch", was_rate_limited=True)
            return CheckResult.RATE_LIMITED
        rate_limiter.record_request("twitch")
        time.sleep(3)
        screenshot_path = f"{username}_twitch_screenshot.png"
        page.screenshot(path=screenshot_path)
        image = Image.open(os.path.abspath(screenshot_path))
        text = pytesseract.image_to_string(image, lang='eng', config='--oem 3 --psm 6')
        try:
            os.remove(screenshot_path)
        except FileNotFoundError:
            print("File not found, could not delete.")
        definite_not_found_indicators = [
                "This content isn't available right now",
                "This page isn't available",
                "Page Not Found",
                "Content Not Found",
                "Sorry, this page isn't available",
                '"error":{"message":"Unsupported get request',
                '"error":{"message":"(#803)',
                '"error":{"message":"Invalid username',
                "profile unavailable",
                "Page not found",
                "Sorry. Unless you've got a time machine",
                "Sorry. Unless you've got a time machine, that content is unavailable.",
            ]
        definite_found_indicators = [
                "stream chat",
                "OFFLINE",
                f"{username.lower()} is offline",
                "This video is either unavailable or not supported in this browser.",
                "(Eror #4000)",
                "Home About Schedule Videos",
                f"{username} Viewers Also Watch"
            ]
        text_lower = text.lower()
        if any(indicator.lower() in text_lower for indicator in definite_not_found_indicators):
            return CheckResult.NOT_FOUND
        if any(indicator.lower() in text_lower for indicator in definite_found_indicators):
            return CheckResult.FOUND
        return CheckResult.NOT_FOUND
    except PlaywrightTimeoutError:
        rate_limiter.record_request("twitch", was_rate_limited=True)
        return CheckResult.TIMEOUT
    finally:
        page.close()


#----------------------------#
#          RESULTS           #
#----------------------------#

CHECKS: Dict[str, Callable[[str], CheckResult]] = {
    "Bluesky": bluesky,
    "Chess.com": chessdotcom,
    "DeviantArt": deviantart, 
    "Discord": discord,
    "Facebook": facebook,
    "GitHub": github,
    "GitLab": gitlab,
    "Instagram": instagram,
    "Keybase": keybase,
    "LinkedIn": linkedin,
    "Linktree": linktree,
    "Mastodon": mastodon,
    "Medium": medium,
    "Pastebin": pastebin,
    "Pinterest": pinterest,
    "Reddit": reddit,
    "Rutube": rutube,
    "Snapchat": snapchat,
    "SoundCloud": soundcloud,
    "Spotify": spotify,
    "Steam": steam,
    "Strava": strava,
    "Telegram": telegram,
    "Threads": threads,
    "TikTok": tiktok,
    "Twitch": twitch,
    "Vimeo": vimeo,
    "VK": vk,
    "YouTube": youtube,
}

profile_urls = {
    "Bluesky": lambda u: f"https://bsky.app/profile/{u}.bsky.social",
    "Chess.com": lambda u: f"https://www.chess.com/member/{u}",
    "DeviantArt": lambda u: f"https://www.deviantart.com/{u}",
    "Discord": lambda u: "https://discord.com/register",
    "GitHub": lambda u: f"https://github.com/{u}",
    "GitLab": lambda u: f"https://gitlab.com/{u}",
    "Instagram": lambda u: f"https://instagram.com/{u}",
    "Keybase": lambda u: f"https://keybase.io/{u}",
    "LinkedIn": lambda u: f"https://www.linkedin.com/in/{u}",
    "Linktree": lambda u: f"https://linktr.ee/{u}",
    "Mastodon": lambda u: f"https://mastodon.social/@{u}",
    "Medium": lambda u: f"https://medium.com/@{u}",
    "Pastebin": lambda u: f"https://pastebin.com/u/{u}",
    "Pinterest": lambda u: f"https://www.pinterest.com/{u}",
    "Reddit": lambda u: f"https://reddit.com/user/{u}",
    "Rutube": lambda u: f"https://rutube.ru/u/{u}",
    "Snapchat": lambda u: f"https://www.snapchat.com/add/{u}",
    "SoundCloud": lambda u: f"https://soundcloud.com/{u}",
    "Spotify": lambda u: f"https://open.spotify.com/user/{u}",
    "Steam": lambda u: f"https://steamcommunity.com/id/{u}",
    "Strava": lambda u: f"https://www.strava.com/athletes/{u}",
    "Telegram": lambda u: f"https://t.me/{u}",
    "Threads": lambda u: f"https://www.threads.net/@{u}",
    "TikTok": lambda u: f"https://www.tiktok.com/@{u}",
    "Twitch": lambda u: f"https://www.twitch.tv/{u}",
    "VK": lambda u: f"https://vk.com/{u}",
    "Vimeo": lambda u: f"https://vimeo.com/{u}",
    "YouTube": lambda u: f"https://www.youtube.com/@{u}",
}


def check_single_platform(platform: str, check_func: Callable, username: str) -> Tuple[str, CheckResult]:
    wait_time = rate_limiter.should_wait(platform.lower())
    if wait_time > 0:
        time.sleep(wait_time)
    result = check_func(username)
    return (platform, result)

def get_result_symbol(result: CheckResult) -> str:
    """Get the appropriate symbol for each result type"""
    symbols = {
        CheckResult.FOUND: "✅",
        CheckResult.NOT_FOUND: "❌",
        CheckResult.NETWORK_ERROR: "🔌",
        CheckResult.RATE_LIMITED: "⏳",
        CheckResult.TIMEOUT: "⏱️",
        CheckResult.UNKNOWN_ERROR: "❓"
    }
    return symbols.get(result, "❓")

def get_result_description(result: CheckResult) -> str:
    """Get human-readable description of the result"""
    descriptions = {
        CheckResult.FOUND: "Profile found",
        CheckResult.NOT_FOUND: "Profile not found",
        CheckResult.NETWORK_ERROR: "Network error",
        CheckResult.RATE_LIMITED: "Rate limited",
        CheckResult.TIMEOUT: "Timeout",
        CheckResult.UNKNOWN_ERROR: "Unknown error"
    }
    return descriptions.get(result, "Unknown")

def export_json(results: Dict[str, Dict[str, any]], filename: str):
    """Export results to JSON"""
    with open(filename, 'w', encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n💾 Results exported to {filename}")

def check_username(username: str) -> Dict:
    """Check a single username across all platforms"""
    results = {}
    total_platforms = len(CHECKS)
    current = 0
    
# Track statistics
    stats = {
        CheckResult.FOUND: 0,
        CheckResult.NOT_FOUND: 0,
        CheckResult.NETWORK_ERROR: 0,
        CheckResult.RATE_LIMITED: 0,
        CheckResult.TIMEOUT: 0,
        CheckResult.UNKNOWN_ERROR: 0
    }
    
    if RICH:
        console = Console()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            task = progress.add_task(f"[cyan]Checking {username}...", total=total_platforms)
            
            for plat, fn in CHECKS.items():
                platform_result, result = check_single_platform(plat, fn, username)
                results[plat] = result
                stats[result] += 1
                progress.update(task, advance=1, description=f"[cyan]Checking {username}... {plat} {get_result_symbol(result)}")
    else:
        for plat, fn in CHECKS.items():
            current += 1
            print(f"[{current}/{total_platforms}] Checking {plat}...", end=" ", flush=True)
            platform_result, result = check_single_platform(plat, fn, username)
            results[plat] = result
            stats[result] += 1
            print(get_result_symbol(result))
    
    return {
        "username": username,
        "results": results,
        "stats": stats,
        "timestamp": datetime.now()
    }

def display_results(username: str, results: Dict[str, CheckResult], stats: Dict):
    """Display results for a single username"""
    print(f"\n📊 Results Summary for '{username}':")
    print(f"✅ Found: {stats[CheckResult.FOUND]}")
    print(f"❌ Not Found: {stats[CheckResult.NOT_FOUND]}")
    if stats[CheckResult.NETWORK_ERROR] > 0:
        print(f"🔌 Network Errors: {stats[CheckResult.NETWORK_ERROR]}")
    if stats[CheckResult.RATE_LIMITED] > 0:
        print(f"⏳ Rate Limited: {stats[CheckResult.RATE_LIMITED]}")
    if stats[CheckResult.TIMEOUT] > 0:
        print(f"⏱️  Timeouts: {stats[CheckResult.TIMEOUT]}")
    if stats[CheckResult.UNKNOWN_ERROR] > 0:
        print(f"❓ Unknown Errors: {stats[CheckResult.UNKNOWN_ERROR]}")

    if RICH:
        console = Console()
        table = Table(title=f"Username: {username} | Found: {stats[CheckResult.FOUND]}/{len(CHECKS)}", show_lines=True)
        table.add_column("Platform", style="cyan", no_wrap=True)
        table.add_column("Status", style="green")
        table.add_column("Result", style="yellow")
        table.add_column("Profile URL", style="magenta")
        
        # Sort results
        def sort_key(item):
            plat, result = item
            if result == CheckResult.FOUND:
                return (0, plat)
            elif result == CheckResult.NOT_FOUND:
                return (2, plat)
            else:
                return (1, plat)
        
        sorted_results = sorted(results.items(), key=sort_key)
        
        for plat, result in sorted_results:
            url = profile_urls[plat](username) if plat in profile_urls else ""
            status = get_result_symbol(result)
            result_desc = get_result_description(result)
            table.add_row(plat, status, result_desc, url if result == CheckResult.FOUND else "")
        console.print(table)
    else:
        print("\n" + "="*60)
        print("DETAILED RESULTS:")
        print("="*60)
        
        found_profiles = [(plat, result) for plat, result in results.items() if result == CheckResult.FOUND]
        error_profiles = [(plat, result) for plat, result in results.items() if result not in [CheckResult.FOUND, CheckResult.NOT_FOUND]]
        not_found_profiles = [(plat, result) for plat, result in results.items() if result == CheckResult.NOT_FOUND]
        
        if found_profiles:
            print("\n✅ PROFILES FOUND:")
            for plat, _ in found_profiles:
                print(f"[+] {plat:12} : {profile_urls[plat](username)}")
        
        if error_profiles:
            print(f"\n⚠️  ERRORS ({len(error_profiles)}):")
            for plat, result in error_profiles:
                print(f"[!] {plat:12} : {get_result_description(result)}")
        
        if not_found_profiles:
            print(f"\n❌ NOT FOUND ({len(not_found_profiles)}):")
            for plat, _ in not_found_profiles:
                print(f"[-] {plat:12} : No profile detected")


def main():
    parser = argparse.ArgumentParser(description="OSINT username checker")
    parser.add_argument("username", nargs="?", help="Username to search")
    parser.add_argument("--list", "-l", help="File containing list of usernames (one per line)")
    parser.add_argument("--export", "-e", help="Export results to JSON file")
    
    args = parser.parse_args()
    
    if not args.username and not args.list:
        parser.print_help()
        sys.exit(1)
    
    print("\n🔍 Enhanced OSINT Username Checker")
    print("📝 Note: X/Twitter is currently unavailable - no reliable detection method")
        
    all_results = {}
    
    # Get list of usernames to check
    usernames = []
    if args.list:
        try:
            with open(args.list, 'r', encoding="utf-8") as f:
                usernames = [line.strip().lstrip("@") for line in f if line.strip()]
            print(f"📋 Loaded {len(usernames)} usernames from {args.list}")
        except FileNotFoundError:
            print(f"❌ Error: File '{args.list}' not found")
            sys.exit(1)
    else:
        usernames = [args.username.strip().lstrip("@")]
    
    # Check each username
    for idx, username in enumerate(usernames):
        if len(usernames) > 1:
            print(f"\nChecking username: {username}")
            # Add delay between usernames to avoid IP-based rate limiting
            if idx > 0:
                delay = random.uniform(2, 5)  # Random delay between 2-5 seconds
                print(f"⏳ Waiting {delay:.1f} seconds before next username...")
                time.sleep(delay)
        
        result = check_username(username)
        all_results[username] = result
        
        display_results(
            username,
            result["results"],
            result["stats"]
        )
    
    # Export results if requested
    if args.export:
        export_data = {}
        for username, data in all_results.items():
            export_data[username] = {
                "timestamp": data["timestamp"].isoformat(),
                "stats": {k.value: v for k, v in data["stats"].items()},
                "results": {plat: result.value for plat, result in data["results"].items()},
                "found_profiles": {
                    plat: profile_urls[plat](username)
                    for plat, result in data["results"].items()
                    if result == CheckResult.FOUND and plat in profile_urls
                }
            }
        export_json(export_data, args.export)
    
    print("\n💡 Tips:")
    print("- Manually verify positive results for accuracy")
    print("- The things you own end up owning you")


if __name__ == "__main__":
    main()