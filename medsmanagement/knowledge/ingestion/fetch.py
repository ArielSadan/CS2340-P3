import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
 
import requests
 
from ..conf import get_setting
from .errors import BlockedByHost, FetchFailed, FileTooLarge, InvalidURL, PrivatePage, UnsafeURL
 
LOGIN_MESSAGE = (
    "This page is private or requires a login. If you have access, "
    "download it and upload it as a file instead."
)
FORBIDDEN_MESSAGE = (
    "This page is private, requires a login, or the site doesn't allow "
    "automated downloads. If you can open it, save it and upload it as a file instead."
)
BLOCKED_MESSAGE = (
    "This site can't be reached from our server (it isn't on our hosting "
    "provider's allowlist). Download the page and upload it as a file instead."
)
UNSAFE_MESSAGE = "Private or internal network addresses can't be ingested."
 
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "igshid"}
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")
 
LOGIN_HOSTS = {
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "login.yahoo.com", "sso.gatech.edu", "login.gatech.edu",
}
LOGIN_HOST_SUFFIXES = (".okta.com", ".auth0.com", ".onelogin.com")
LOGIN_PATH = re.compile(
    r"(^|/)(login|log-in|log_in|signin|sign-in|sign_in|logon|sso|oauth2?|servicelogin|"
    r"session/new|users/sign_in|account/login|accounts/login|auth/login|cas/login)"
    r"(/|$|\.|\?)",
    re.I,
)
 
REDIRECT_CODES = {301, 302, 303, 307, 308}
 
 
@dataclass
class FetchedPage:
    url: str            # final URL after redirects
    content_type: str   # e.g. "text/html"
    body: bytes
    encoding: str | None
 
 
# ---------------------------------------------------------------- URLs
 
def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        raise InvalidURL("Empty URL.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidURL("Only http:// and https:// links can be ingested.")
    if parts.username or parts.password:
        raise InvalidURL("Links with a username or password inside them aren't allowed.")
    host = parts.hostname
    if not host or " " in host:
        raise InvalidURL("That doesn't look like a web address.")
    try:
        port = parts.port
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise InvalidURL("That doesn't look like a web address.")
 
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
 
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode([
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ])
    return urlunsplit((scheme, netloc, path, query, ""))
 
 
def _check_ip(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global is False for private, loopback, link-local (incl. cloud
    # metadata 169.254.169.254), shared, reserved and documentation ranges.
    if not ip.is_global or ip.is_multicast:
        raise UnsafeURL(UNSAFE_MESSAGE)
 
 
def ensure_public_host(host, resolver=socket.getaddrinfo):
    """Raise UnsafeURL if the host is (or resolves to) a private address."""
    host = (host or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(INTERNAL_SUFFIXES) or "." not in host and ":" not in host:
        raise UnsafeURL(UNSAFE_MESSAGE)
    try:
        _check_ip(ipaddress.ip_address(host))
        return
    except ValueError:
        pass  # not an IP literal; resolve the name
    try:
        infos = resolver(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        # Can't resolve here (e.g. DNS goes through the hosting proxy).
        # The request itself will fail with a readable error if the name
        # doesn't exist.
        return
    for info in infos:
        try:
            _check_ip(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
 
 
def looks_like_login_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in LOGIN_HOSTS or host.endswith(LOGIN_HOST_SUFFIXES):
        return True
    return bool(LOGIN_PATH.search(parts.path or "/"))
 
 
# ---------------------------------------------------------------- download
 
def _read_limited(response, max_bytes):
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise FileTooLarge(f"This page is larger than the {max_bytes // (1024 * 1024)} MB limit.")
    chunks, total = [], 0
    for chunk in response.iter_content(65536):
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLarge(f"This page is larger than the {max_bytes // (1024 * 1024)} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)
 
 
def _is_hosting_proxy_block(response):
    # For http:// links, PythonAnywhere's proxy answers with its own 403
    # page instead of the site's. (For https:// links the block shows up
    # as a requests ProxyError, handled in fetch_url.)
    try:
        snippet = response.raw.read(4096, decode_content=True) or b""
    except Exception:
        return False
    text = snippet.decode("utf-8", "ignore").lower()
    return "pythonanywhere" in text and ("whitelist" in text or "allowlist" in text)
 
 
def fetch_url(url, session=None, resolver=socket.getaddrinfo):
    """Download `url` (already normalized). Follows redirects manually so
    every hop is re-checked for private addresses and login pages."""
    session = session or requests.Session()
    headers = {
        "User-Agent": get_setting("URL_USER_AGENT"),
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    }
    timeout = (get_setting("URL_CONNECT_TIMEOUT"), get_setting("URL_READ_TIMEOUT"))
    max_bytes = get_setting("KNOWLEDGE_MAX_UPLOAD_MB") * 1024 * 1024
 
    current = url
    for _hop in range(get_setting("URL_MAX_REDIRECTS") + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise InvalidURL("The page redirected to a link we can't follow.")
        ensure_public_host(parts.hostname, resolver)
        if looks_like_login_url(current):
            raise PrivatePage(LOGIN_MESSAGE)
 
        try:
            response = session.get(
                current, headers=headers, timeout=timeout, allow_redirects=False, stream=True
            )
        except requests.exceptions.ProxyError as exc:
            raise BlockedByHost(BLOCKED_MESSAGE) from exc
        except requests.exceptions.SSLError as exc:
            raise FetchFailed("Couldn't make a secure connection to this site.") from exc
        except requests.exceptions.Timeout as exc:
            raise FetchFailed("The site took too long to respond.") from exc
        except requests.exceptions.ConnectionError as exc:
            raise FetchFailed("Couldn't connect to this site. Check the address.") from exc
        except requests.exceptions.RequestException as exc:
            raise FetchFailed("Couldn't download this page.") from exc
 
        try:
            status = response.status_code
            if status in REDIRECT_CODES and response.headers.get("Location"):
                current = urljoin(current, response.headers["Location"])
                continue
            if status == 401:
                raise PrivatePage(LOGIN_MESSAGE)
            if status == 403:
                if _is_hosting_proxy_block(response):
                    raise BlockedByHost(BLOCKED_MESSAGE)
                raise PrivatePage(FORBIDDEN_MESSAGE)
            if status in (404, 410):
                raise FetchFailed("Page not found, or it may be private.")
            if status == 429:
                raise FetchFailed("The site is limiting requests right now. Try again later.")
            if status >= 500:
                raise FetchFailed(f"The site had a server error ({status}). Try again later.")
            if status >= 400:
                raise FetchFailed(f"The site returned an error ({status}).")
 
            body = _read_limited(response, max_bytes)
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            encoding = None
            if "charset=" in response.headers.get("Content-Type", "").lower():
                encoding = response.encoding
            return FetchedPage(current, content_type, body, encoding)
        except requests.exceptions.RequestException as exc:
            raise FetchFailed("The download was interrupted. Try again.") from exc
        finally:
            response.close()
 
    raise FetchFailed("This page redirects too many times.")