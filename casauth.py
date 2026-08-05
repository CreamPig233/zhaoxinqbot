# webvpn_login_simple.py

import base64
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES
import hashlib

CAS_BASE_URL = "https://pass.neu.edu.cn/tpass"
WEBVPN_ENTRY_URL = "https://webvpn.neu.edu.cn"
DEFAULT_SERVICE = "https://personal.neu.edu.cn/mydata/common/auth_callback?redirect_url=https%3A%2F%2Fpersonal.neu.edu.cn%2Fmydata%2Fpage%2F"

RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnjA28DLKXZzxbKmo9/1W"
    "kVLf1mr+wtLXLXt6sC4WiBCtsbzF5ewm7ARZeAdS3iZtqlYPn6IcUoOw42H8nAK/"
    "tfFcIb6dZ1K0atn0U39oWCGPzYuKtLJeMuNZiDXVuAXtojrckOjLW9B3gUnaNGLu"
    "Ix0fYe66l0o9WjU2cGLNZQfiIxs2h00z1EA9IdSnVxiVQWSD+lsP3JZXh2TT287l"
    "a4Y4603SQNKTK/QvXfcmccwTEd1IW6HwGxD6QrkInBiHisKWxmveN7UDSaQRZ/J9"
    "7G0YC32pD38WT53izXeK0p/kU/X37VP555um1wVWFvPIuc9I7gMP1+hq5a+X6c++"
    "tQIDAQAB"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WebVPNLoginError(Exception):
    pass


class SMSRequiredError(WebVPNLoginError):
    pass


def _hidden_fields(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    return {
        item.get("name"): item.get("value", "")
        for item in soup.find_all("input", type="hidden")
        if item.get("name")
    }


def _form_action(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    form = soup.select_one("form#loginForm") or soup.select_one("form[action]")
    action = form.get("action", "") if form else ""
    return urljoin(page_url, action or page_url)


def _public_key(html: str) -> str:
    patterns = [
        r'(?:var|const|let)\s+publicKeyStr\s*=\s*["\']([A-Za-z0-9+/=]+)["\']',
        r'(?:var|const|let)\s+publicKey\s*=\s*["\']([A-Za-z0-9+/=]+)["\']',
        r'<input[^>]*id=["\']publicKey["\'][^>]*value=["\']([A-Za-z0-9+/=]+)["\']',
        r'<input[^>]*value=["\']([A-Za-z0-9+/=]+)["\'][^>]*id=["\']publicKey["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match and len(match.group(1)) > 100:
            return match.group(1)

    return RSA_PUBLIC_KEY_B64


def _rsa_encrypt(userid: str, password: str, key_b64: str) -> str:
    key = RSA.import_key(base64.b64decode(key_b64))
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt((userid + password).encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _error_message(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for selector in ["#errormsg", ".error", "#errormsghide", ".alert"]:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)
    return "未知错误"


def _has_sms_challenge(html: str) -> bool:
    return bool(
        re.search(
            r"phone\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][^'\"]+['\"]\s*\)",
            html,
        )
    )


def _is_login_page(url: str) -> bool:
    return "webvpn.neu.edu.cn" in url and "/tpass/login" in url


def webvpn_login(
    userid: str,
    password: str,
    services: str = DEFAULT_SERVICE,
    timeout: int = 15,
    verify_ssl: bool = True,
) -> requests.Session:
    """
    WebVPN 账号密码登录。

    Args:
        userid: 学号
        password: 明文密码
        services: 登录后访问的目标服务

    Returns:
        已登录的 requests.Session

    Raises:
        SMSRequiredError: 触发短信验证码
        WebVPNLoginError: 登录失败
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    page = session.get(
        WEBVPN_ENTRY_URL,
        timeout=timeout,
        verify=verify_ssl,
        allow_redirects=True,
    )
    page.raise_for_status()

    if not _is_login_page(page.url):
        session.get(services, timeout=timeout, verify=verify_ssl, allow_redirects=True)
        return session

    hidden = _hidden_fields(page.text)
    key_b64 = _public_key(page.text)
    post_url = _form_action(page.text, page.url)

    data = {
        "un": userid,
        "pd": password,
        "rsa": _rsa_encrypt(userid, password, key_b64),
        "ul": str(len(userid)),
        "pl": str(len(password)),
        "lt": hidden.get("lt", ""),
        "execution": hidden.get("execution", "e1s1"),
        "_eventId": "submit",
    }

    resp = session.post(
        post_url,
        data=data,
        timeout=timeout,
        verify=verify_ssl,
        allow_redirects=True,
    )
    resp.raise_for_status()

    if _has_sms_challenge(resp.text):
        raise SMSRequiredError("当前登录触发短信验证码验证")

    if _is_login_page(resp.url):
        raise WebVPNLoginError(f"登录失败: {_error_message(resp.text)}")

    service_resp = session.get(
        services,
        timeout=timeout,
        verify=verify_ssl,
        allow_redirects=True,
    )
    service_resp.raise_for_status()

    if _is_login_page(service_resp.url):
        raise WebVPNLoginError("登录后访问目标服务仍跳回认证页")

    return session



def normal_login(
    userid: str,
    password: str,
    services: str = DEFAULT_SERVICE
) -> requests.Session:
    app_ua = "Mozilla/5.0 (Linux; Android 12; BVL-AN00 Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Safari/537.36 uni-app Html5Plus/1.0 (Immersed/24.0)"
    def _generate_fingerprint():
        src = f"{userid}_Login"
        md5 = hashlib.md5()
        md5.update(src.encode('utf-8'))
        fingerprint = md5.hexdigest()
        return fingerprint

    fingerprint = _generate_fingerprint()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": app_ua,
        "X-Mobile-Device-UUID": fingerprint,
    }

    
    data = {
        "username": userid,
        "password": password,
        "device_id": fingerprint,
    }

    resp = requests.post(
        "https://personal.neu.edu.cn/prize/Front/Oauth/User/login_sms",
        headers=headers,
        data=data,
        allow_redirects=False,
        timeout=10,
    )
    resp.raise_for_status()

    body = resp.json()
    
    if body["code"] == 1 and str(body.get("result", "")).startswith("TGT"):
        tgt =  body["result"]
    elif body["code"] == 3:
        raise SMSRequiredError("当前登录触发短信验证码验证")
    else:
        raise Exception('智慧东大统一登录失败')
    
    session = requests.Session()
    
    session.cookies.set("CASTGC", tgt)
    session.headers.update(HEADERS)
    session.get(services)
    return session

def turn_url_webvpn(url: str) -> str:
    protocol, url = url.split("://")
    urlroot, urlpath = url.split("/", 1)
    
    cipher = AES.new(
        b'b0A58a69394ce73@',
        AES.MODE_CFB,
        b'b0A58a69394ce73@',
        segment_size=128)
    cipher_text = cipher.encrypt(urlroot.ljust(len(urlroot)//16*16+16, '\0').encode())

    res = f'https://webvpn.neu.edu.cn/{protocol}/62304135386136393339346365373340' \
        + cipher_text[:len(urlroot)].hex() + "/" + urlpath
    return res