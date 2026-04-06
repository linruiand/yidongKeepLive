#!/usr/bin/env python3
"""
VDI Automation: State Machine (FSM) Implementation
--------------------------------------------------
Architecture: Game Loop / FSM
States:
 1. LOGIN:     Login inputs are visible.
 2. LIST:      Desktop list visible, 'Connect' button enabled.
 3. CONNECTING: 'Connect' button disabled OR Native Helper running but Viewer missing.
 4. SESSION:   VDI Viewer process (uSmartView) is running.
 5. UNKNOWN:   Loading or error state.

This version:
- Keeps your original FSM/login/update/guide/conflict logic and logging style.
- Adds:
  * SAFE home popup dismiss (close popup overlay only; never click window close button)
  * Dynamic desktop count + click nth "连接" on the left side
  * Serial switching in 20H mode: close after 60s -> switch next desktop after 10s -> reload -> repeat
"""

import os
import sys
import time
import json
import json as py_json
import random
import logging
import urllib.request
import subprocess
from enum import Enum

# Setup Logging (UNCHANGED)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/supervisor/automation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("VDI_FSM")

import websocket


# --- CONFIG LOADING ---
def load_config(path='/config/credentials.conf'):
    config = {}
    if not os.path.exists(path):
        return config
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip().strip('"').strip("'")
    return config


def _parse_bool(val, default=False):
    """解析配置里的布尔值，兼容 true/false/1/0/yes/no 等形式"""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


# --- CDP HELPER ---
class CDPSession:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=5)
        self.msg_id = 0

    def send(self, method, params=None):
        self.msg_id += 1
        message = {"id": self.msg_id, "method": method, "params": params or {}}
        try:
            self.ws.send(json.dumps(message))
            while True:
                resp = self.ws.recv()
                data = json.loads(resp)
                if data.get("id") == self.msg_id:
                    if "error" in data:
                        return None
                    return data.get("result")
        except Exception:
            return None

    def evaluate(self, expression):
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        if not res:
            return None
        return res.get("result", {}).get("value")

    def reload(self):
        self.send("Page.reload")

    def is_alive(self):
        """Stealthy heartbeat check using a browser-level command (no JS injection)"""
        try:
            res = self.send("Browser.getVersion")
            if res:
                logger.info(f"Stealth Check Response: {res}")
            return res is not None
        except:
            return False

    def close(self):
        try:
            self.ws.close()
        except:
            pass


# --- STATES ---
class State(Enum):
    UNKNOWN = 0  # 初始状态或未定义页面
    LOGIN = 1  # 登录界面 (#/login)
    DESKTOP_LIST = 2  # 云电脑列表主界面 (#/home)
    CONNECTING = 3  # 正在建立桌面连接（加载中）
    IN_SESSION = 4  # 已成功进入桌面会话
    ZOMBIE = 5  # 客户端卡死或无响应状态
    WAIT = 6  # 冲突等待
    UPDATING = 7  # 发现新版本弹窗状态
    GUIDE = 8  # 新手引导页状态


class VDIStateMachine:
    SERIAL_SWITCH_WAIT_SECONDS = 10

    def __init__(self):
        self.reload_config()
        self.cdp_url = "http://localhost:9222"
        self.session = None
        self.last_keepalive = time.time()
        self.state = State.UNKNOWN
        self.last_state = None
        self.state_start_time = time.time()
        self.last_action_time = 0
        self.last_connecting_log = 0
        self.last_conflict_log = 0

        # 20H cycle
        self._cycle_phase = "IDLE"  # IDLE | COUNTDOWN_TO_CLOSE | WAIT_RECONNECT
        self._cycle_deadline_ts = 0
        self._cycle_last_log_ts = 0

        # Dynamic desktops + serial
        self.runtime_indices = []
        self.runtime_ptr = 0
        self.after_close_wait_until = 0
        self._dyn_last_refresh_ts = 0
        self._dyn_last_count = None

    def reload_config(self):
        self.config = load_config()
        self.username = self.config.get('phone', '')
        self.password = self.config.get('password', '')
        self.login_method = self.config.get('login_method', 'password')
        self.connect_index = int(self.config.get('connect_index', 0))  # kept but not used in dynamic mode
        self.min_int = int(self.config.get('keepalive_min_seconds', 120))
        self.max_int = int(self.config.get('keepalive_max_seconds', 300))
        self.keepalive_method = self.config.get('keepalive_method', 'mouse_move')
        self.conflict_wait = int(self.config.get('conflict_wait_seconds', 300))
        self.keepalive_interval = random.randint(self.min_int, self.max_int)
        self.is_20hour = _parse_bool(self.config.get('is_20hour', False), default=False)
        self.sleep_20hour = int(self.config.get('sleep_20hour', 720))

    def get_cdp_session(self):
        if self.session:
            if self.session.is_alive():
                return self.session
            logger.warning("CDP Session lost. Reconnecting...")
            self.session.close()
            self.session = None

        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=3) as f:
                pages = json.load(f)
                logger.info(f"CDP Poll: Found {len(pages)} targets")
                ws_url = next((p['webSocketDebuggerUrl'] for p in pages if p.get('type') == 'page'), None)
                if ws_url:
                    self.session = CDPSession(ws_url)
                    return self.session
        except Exception as e:
            logger.error(f"CDP Connect Error: {e}")
        return None

    def is_process_running(self, name):
        try:
            output = subprocess.check_output(["ps", "aux"]).decode()
            if name not in output:
                return False
            for line in output.split('\n'):
                if name in line:
                    parts = line.split()
                    if len(parts) > 7 and 'Z' in parts[7]:
                        return "ZOMBIE"
            return True
        except:
            return False

    def click_at_selector(self, selector, text_hint=None):
        s = self.get_cdp_session()
        if not s:
            return False

        target_selector = py_json.dumps(selector)
        target_hint = py_json.dumps(text_hint) if text_hint else "null"

        js_find = f"""
            (function() {{
                try {{
                    let el;
                    if ({target_hint} !== null) {{
                        el = Array.from(document.querySelectorAll({target_selector}))
                                  .find(e => (e.innerText||'').includes({target_hint}));
                    }} else {{
                        el = document.querySelector({target_selector});
                    }}
                    if (!el) return null;
                    let rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return null;
                    return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
                }} catch(e) {{ return null; }}
            }})()
        """
        pos = s.evaluate(js_find)
        if pos and 'x' in pos and 'y' in pos:
            x, y = pos['x'], pos['y']
            s.send("Input.dispatchMouseEvent",
                   {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent",
                   {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        return False

    def click_id(self, s, element_id):
        js_find = f"""
            (function() {{
                let el = document.getElementById('{element_id}');
                if (!el) return null;
                let rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return null;
                return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
            }})()
        """
        pos = s.evaluate(js_find)
        if pos and 'x' in pos and 'y' in pos:
            x, y = pos['x'], pos['y']
            s.send("Input.dispatchMouseEvent",
                   {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent",
                   {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        return False

    # --- UPDATE ---
    def check_update_dialog(self):
        s = self.get_cdp_session()
        if not s:
            return False
        js_check = """
            (function() {
                let el = document.querySelector('.refresh-dialog');
                if (!el) return false;
                let rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
            })()
        """
        return s.evaluate(js_check)

    def get_upgrade_url(self):
        try:
            api_url = "https://soho.komect.com/cube/h5/user/download/urls/v2/1"
            headers = {
                'Referer': 'https://soho.komect.com/clientDownload',
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9'
            }
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                resp_data = json.loads(response.read().decode())
                for client in resp_data.get('data', []):
                    if client.get('clientType') == 'pc':
                        for dl in client.get('downloadList', []):
                            if dl.get('clientLabel') == 'linux':
                                for info in dl.get('downloadInfo', []):
                                    if "UOS" in info.get('name', ''):
                                        for sub in info.get('subInfo', []):
                                            if "x86_64" in sub.get('subName', ''):
                                                url = sub.get('subUrl')
                                                if url:
                                                    logger.info(f"[UPDATE] Found latest Web URL: {url}")
                                                    return url
        except Exception as e:
            logger.error(f"[UPDATE] Scrape URL from Web API failed: {e}")
        return None

    def perform_manual_update(self):
        url = self.get_upgrade_url()
        if not url:
            logger.warning("[UPDATE] Could not find upgrade URL in DOM.")
            return False

        target_deb = "/tmp/vdi_update_manual.deb"
        logger.info(f"[UPDATE] Found URL: {url}. Starting manual download...")
        try:
            curl_cmd = ["curl", "-L", "--retry", "5", "--retry-delay", "5", "--connect-timeout", "30", "-C", "-", url,
                        "-o", target_deb]
            subprocess.run(curl_cmd, check=True)
            logger.info(f"[UPDATE] Downloaded successfully to {target_deb}. Verifying integrity...")

            try:
                subprocess.run(["dpkg-deb", "-I", target_deb], check=True, stdout=subprocess.DEVNULL)
                logger.info("[UPDATE] Package integrity check passed.")
            except subprocess.CalledProcessError:
                logger.error("[UPDATE] DEB file is corrupted. Deleting it to prevent resume errors.")
                if os.path.exists(target_deb):
                    os.remove(target_deb)
                return False

            logger.info("[UPDATE] Installing via dpkg (ignoring initial dependency errors)...")
            subprocess.run(["dpkg", "-i", target_deb], check=False)

            logger.info("[UPDATE] Fixing broken dependencies via apt-get...")
            try:
                subprocess.run(["apt-get", "update"], check=False)
                subprocess.run(["apt-get", "install", "-f", "-y"], check=True)
                logger.info("[UPDATE] Dependency fixation successful.")
            except Exception as e:
                logger.warning(f"[UPDATE] Dependency fix had issues (non-fatal if package works): {e}")

            logger.info("[UPDATE] Installation process complete. Restarting services...")
            subprocess.run(["supervisorctl", "restart", "all"], check=True)
            return True
        except Exception as e:
            logger.error(f"[UPDATE] Manual update failed: {e}")
            return False

    # --- INPUT ---
    def paste_at_selector(self, selector, text):
        if self.click_at_selector(selector):
            s = self.get_cdp_session()
            time.sleep(1)
            s.send("Input.dispatchKeyEvent", {"type": "keyDown", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a",
                                             "code": "KeyA"})
            s.send("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a",
                                             "code": "KeyA"})
            time.sleep(0.1)
            s.send("Input.insertText", {"text": text})
            return True
        return False

    # --- POPUP DISMISS (SAFE) ---
    def _dismiss_home_popup_if_any(self, s):
        """
        Safe dismiss:
        - Prefer known popup close class: .banner-pop-close (from your logs)
        - Otherwise: only click close buttons INSIDE visible modal roots/overlays
        - Never click window top-right controls (y < 40)
        """
        js = r"""
        (function(){
            function isVisible(el){
                if(!el) return false;
                const r = el.getBoundingClientRect();
                if(r.width<=0||r.height<=0) return false;
                const st = window.getComputedStyle(el);
                if(!st || st.display==='none' || st.visibility==='hidden' || st.opacity==='0') return false;
                return true;
            }

            // 0) Hard prefer: banner popup close
            const bannerClose = document.querySelector(".banner-pop-close");
            if(bannerClose && isVisible(bannerClose)){
                const r = bannerClose.getBoundingClientRect();
                bannerClose.click();
                return {clicked:true, via:"banner-pop-close", x:r.x, y:r.y, cls:bannerClose.className};
            }

            // 1) Need modal/overlay root to exist (avoid clicking window controls)
            const modalRoots = []
              .concat(Array.from(document.querySelectorAll(".el-dialog, .el-message-box")))
              .concat(Array.from(document.querySelectorAll(".el-overlay, .el-overlay-dialog")))
              .concat(Array.from(document.querySelectorAll(".banner-pop, .banner-pop-wrap, .banner-pop-mask, .banner-pop-overlay, .banner-pop-container")))
              .concat(Array.from(document.querySelectorAll("[class*='mask'], [class*='modal'], [class*='dialog'], [class*='popup']")));

            const roots = modalRoots.filter(isVisible);
            if(roots.length === 0){
                return {clicked:false, reason:"no-modal-root"};
            }

            // 2) Click close inside roots only
            for(const root of roots){
                const btns = []
                  .concat(Array.from(root.querySelectorAll(".el-dialog__headerbtn, .el-message-box__headerbtn")))
                  .concat(Array.from(root.querySelectorAll(".el-icon-close, .close, .btn-close, [aria-label*='关闭'], [title*='关闭']")))
                  .concat(Array.from(root.querySelectorAll("button, span, i, div, a")));

                for(const el of btns){
                    if(!isVisible(el)) continue;

                    const txt = ((el.innerText||"").trim());
                    const aria = (el.getAttribute("aria-label")||"").toLowerCase();
                    const title = (el.getAttribute("title")||"").toLowerCase();
                    const cls = (el.className||"").toString().toLowerCase();

                    const looksLikeClose =
                      cls.includes("close") ||
                      aria.includes("close") || aria.includes("关闭") ||
                      title.includes("close") || title.includes("关闭") ||
                      txt === "x" || txt === "X" || txt === "×";

                    if(!looksLikeClose) continue;

                    const r = el.getBoundingClientRect();
                    if(r.y < 40) continue; // safety: window controls area

                    el.click();
                    return {clicked:true, via:"modal-inner-close", x:r.x, y:r.y, txt:txt, cls:el.className};
                }
            }

            return {clicked:false, reason:"modal-root-found-but-no-close"};
        })()
        """
        try:
            res = s.evaluate(js)
            if res and res.get("clicked"):
                logger.info(f"[ACT] Home popup dismissed: {res}")
                return True
        except Exception as e:
            logger.error(f"[ACT] Home popup dismiss error: {e}")
        return False

    # --- CONFLICT ---
    def check_conflict_state(self, s):
        js_get_dialog = """
            (function() {
                let box = document.querySelector('.el-message-box, .el-message');
                return box ? box.innerText : null;
            })()
        """
        dialog_text = s.evaluate(js_get_dialog)
        if not dialog_text:
            return None
        keywords = ["其他设备上登录", "已分配", "已回收"]
        if any(kw in dialog_text for kw in keywords):
            logger.warning(f"[SENSE] Conflict detected in Dialog: {dialog_text.strip()} -> WAIT")
            return State.WAIT
        return None

    # --- PAGE SENSE ---
    def check_desktop_list_state(self, s, url):
        if "home" not in (url or ""):
            return None
        is_disabled = s.evaluate("document.querySelector('.btn-link') && document.querySelector('.btn-link').disabled")
        if is_disabled:
            return State.CONNECTING
        return State.DESKTOP_LIST

    def check_login_page_state(self, s, url):
        if "login" not in (url or ""):
            return None
        login_view = s.evaluate("""
            (function() {
                let h6 = document.querySelector('.lf-name h6');
                if (h6) return h6.innerText;
                let activeTab = document.querySelector('.lf-tabs .active');
                if (activeTab) return activeTab.innerText;
                return 'Unknown Login';
            })()
        """)
        if login_view:
            logger.info(f"[SENSE] Login Page Active: {login_view.strip()}")
        return State.LOGIN

    def check_session_state(self):
        proc_status = self.is_process_running("uSmartView")
        if proc_status == "ZOMBIE":
            return State.ZOMBIE
        if proc_status is True:
            return State.IN_SESSION
        return None

    def check_update_state(self):
        if self.check_update_dialog():
            return State.UPDATING
        return None

    def check_guide_state(self):
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=1) as f:
                pages = json.load(f)
                if any("bootguidor.html" in p.get('url', '') for p in pages):
                    return State.GUIDE
        except Exception as e:
            logger.error(f"[SENSE] Guide Detection Error (CDP Poll): {e}")
        return None

    def detect_state(self):
        res = self.check_guide_state()
        if res:
            return res

        res = self.check_session_state()
        if res:
            return res

        res = self.check_update_state()
        if res:
            return res

        s = self.get_cdp_session()
        if not s:
            return State.UNKNOWN

        try:
            current_url = s.evaluate("window.location.href")

            res = self.check_conflict_state(s)
            if res:
                return res

            res = self.check_desktop_list_state(s, current_url)
            if res:
                return res

            res = self.check_login_page_state(s, current_url)
            if res:
                return res

            if current_url and "error" in current_url:
                logger.warning("[SENSE] Error Page Detected")
                return State.UNKNOWN
        except Exception as e:
            logger.error(f"[SENSE] Error during detection: {e}")

        return State.UNKNOWN

    # --- WAIT ---
    def handle_wait_state(self, duration):
        now = time.time()
        if int(duration) > 0 and (now - self.last_conflict_log >= 60):
            logger.warning(
                f"[ACT] CONFLICT WAIT: Giving user time... ({duration // 60:.0f}/{self.conflict_wait // 60:.0f} mins)")
            self.last_conflict_log = now

        if duration > self.conflict_wait:
            logger.info("[ACT] WAIT OVER -> Refreshing to check status")
            s = self.get_cdp_session()
            if s:
                s.reload()
            self.last_conflict_log = 0

    # --- LOGIN (UNCHANGED LOGIC) ---
    def _ensure_correct_login_view(self, s):
        if self.login_method != "other":
            view_text = s.evaluate(
                "document.querySelector('.lf-name h6') ? document.querySelector('.lf-name h6').innerText : ''")
            target_text = "子账号登录" if self.login_method == "sub_account" else "账号名密码登录"
            if target_text not in (view_text or ""):
                logger.info(f"[ACT] Switching to {target_text} view...")
                switch_btn_text = "子账号登录" if self.login_method == "sub_account" else "账密登录"
                if self.click_at_selector(".lf-sub p", text_hint=switch_btn_text):
                    time.sleep(3)

    def _perform_login_action(self, s):
        user_ok = self.paste_at_selector("input[placeholder*='账号']", self.username)
        pass_ok = self.paste_at_selector("input[type='password']", self.password)
        logger.info(f"ok1:{user_ok} , ok2: {pass_ok}")

        if user_ok and pass_ok:
            is_checked = s.evaluate("document.querySelector('.el-checkbox').classList.contains('is-checked')")
            if not is_checked:
                self.click_at_selector(".el-checkbox__inner")
            time.sleep(1)
            self.click_at_selector("button.el-button--primary")
            logger.info("[ACT] Login submitted.")

    def handle_login_state(self, duration):
        now = time.time()
        if duration > 10 and (now - self.last_action_time) > 6:
            self.reload_config()
            logger.info(f"[ACT] LOGIN: Processing {self.login_method} login for {self.username}...")
            self.last_action_time = now
            s = self.get_cdp_session()
            if not s:
                return
            self._ensure_correct_login_view(s)
            self._perform_login_action(s)

    # --- UPDATING ---
    def handle_updating_state(self):
        now = time.time()
        if (now - self.last_action_time) > 10:
            self.last_action_time = now
            logger.info("[ACT] UPDATING: Update dialog found. Triggering manual update flow...")
            self.perform_manual_update()

    # --- Dynamic desktop click helpers ---
    def _count_connect_buttons_left(self, s):
        js = r"""
        (function(){
            function isVisible(el){
                if(!el) return false;
                const r = el.getBoundingClientRect();
                if(r.width<=0||r.height<=0) return false;
                const st = window.getComputedStyle(el);
                if(!st || st.display==='none' || st.visibility==='hidden' || st.opacity==='0') return false;
                return true;
            }
            const vw = window.innerWidth || 0;
            const btns = Array.from(document.querySelectorAll('button.btn-link, .btn-link'))
              .filter(el => ((el.innerText||'').trim() === '连接'))
              .filter(isVisible)
              .filter(el => el.getBoundingClientRect().x < vw * 0.6);
            return btns.length;
        })()
        """
        val = s.evaluate(js)
        try:
            return int(val or 0)
        except:
            return 0

    def _refresh_runtime_indices(self, s, force=False):
        now = time.time()
        if (not force) and self.runtime_indices and (now - self._dyn_last_refresh_ts < 30):
            return
        self._dyn_last_refresh_ts = now

        count = self._count_connect_buttons_left(s)
        if count <= 0:
            if not self.runtime_indices:
                logger.warning("[DYN] No connect buttons found yet.")
            return

        if self._dyn_last_count != count or not self.runtime_indices:
            self._dyn_last_count = count
            self.runtime_indices = list(range(count))
            if self.runtime_ptr >= len(self.runtime_indices):
                self.runtime_ptr = 0
            logger.info(
                f"[DYN] Detected desktops={count}, runtime_indices={self.runtime_indices}, runtime_ptr={self.runtime_ptr}")

    def click_nth_connect_button(self, n):
        s = self.get_cdp_session()
        if not s:
            return False

        js = f"""
        (function(){{
            function isVisible(el){{
                if(!el) return false;
                const r = el.getBoundingClientRect();
                if(r.width<=0||r.height<=0) return false;
                const st = window.getComputedStyle(el);
                if(!st || st.display==='none' || st.visibility==='hidden' || st.opacity==='0') return false;
                return true;
            }}
            const vw = window.innerWidth || 0;
            const btns = Array.from(document.querySelectorAll('button.btn-link, .btn-link'))
              .filter(el => ((el.innerText||'').trim() === '连接'))
              .filter(isVisible)
              .filter(el => el.getBoundingClientRect().x < vw * 0.6);

            if(btns.length === 0) return null;
            const idx = {int(n)};
            if(idx < 0 || idx >= btns.length) return {{count: btns.length, x:null, y:null}};

            const el = btns[idx];
            const r = el.getBoundingClientRect();
            const disabled = (el.tagName === 'BUTTON') ? !!el.disabled : false;
            return {{count: btns.length, x: r.x + r.width/2, y: r.y + r.height/2, disabled: disabled}};
        }})()
        """
        pos = s.evaluate(js)
        if not pos:
            return False
        if pos.get("x") is None:
            logger.warning(f"[ACT] connect buttons count={pos.get('count')} but idx={n} out of range")
            return False
        if pos.get("disabled") is True:
            logger.warning(f"[ACT] connect button #{n} disabled")
            return False

        x, y = pos["x"], pos["y"]
        s.send("Input.dispatchMouseEvent",
               {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        s.send("Input.dispatchMouseEvent",
               {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        logger.info(f"[ACT] Clicked desktop-area '连接' button #{n} at ({x:.0f}, {y:.0f}).")
        return True

    # --- DESKTOP LIST ---
    def handle_desktop_list_state(self, duration):
        if self.is_20hour and self._cycle_phase == "WAIT_RECONNECT":
            return

        if self.after_close_wait_until and time.time() < self.after_close_wait_until:
            return

        now = time.time()
        # keep original cadence: only attempt actions every ~10s (after entering list)
        if duration > 5 and (now - self.last_action_time) > 10:
            s = self.get_cdp_session()
            if not s:
                return

            # First: dismiss popup if any (SAFE)
            if self._dismiss_home_popup_if_any(s):
                self.last_action_time = now
                time.sleep(1.2)
                return

            # Then: refresh dynamic desktop indices
            self._refresh_runtime_indices(s, force=(not self.runtime_indices))
            if not self.runtime_indices:
                return

            logger.info(f"[ACT] LIST: Connecting to desktop runtime_ptr={self.runtime_ptr}/{len(self.runtime_indices)}...")
            self.last_action_time = now
            if self.click_nth_connect_button(self.runtime_ptr):
                logger.info("[ACT] Desktop link clicked.")

    # --- CONNECTING ---
    def handle_connecting_state(self, duration):
        now = time.time()
        if now - self.last_connecting_log >= 5:
            logger.info(f"[ACT] CONNECTING: Waiting for VDI Launch... ({duration:.0f}s)")
            self.last_connecting_log = now
        if duration > 60:
            logger.warning("[ACT] CONNECTING timeout -> Reloading UI")
            s = self.get_cdp_session()
            if s:
                s.reload()

    # --- GUIDE ---
    def handle_guide_state(self):
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=2) as f:
                pages = json.load(f)
                guide_p = next((p for p in pages if p.get('type') == 'page' and "bootguidor.html" in (p.get('url') or '')), None)

            if not guide_p or not guide_p.get('webSocketDebuggerUrl'):
                return

            tmp_s = CDPSession(guide_p['webSocketDebuggerUrl'])
            try:
                if self.click_id(tmp_s, "J_bootGuidorBtn"):
                    logger.info("[ACT] Guide page DISMISSED. Waiting for UI sync...")
                    time.sleep(1)
            finally:
                tmp_s.close()
        except Exception as e:
            logger.error(f"[ACT] Guide Clearing Failed: {e}")

    # --- 20H cycle ---
    def _cycle_log(self, msg, every_seconds=30):
        now = time.time()
        if now - self._cycle_last_log_ts >= every_seconds:
            logger.info(msg)
            self._cycle_last_log_ts = now

    def _cycle_reset(self):
        self._cycle_phase = "IDLE"
        self._cycle_deadline_ts = 0
        self._cycle_last_log_ts = 0

    def _cycle_reset_if_not_waiting(self):
        if self.is_20hour and self._cycle_phase == "WAIT_RECONNECT":
            return
        self._cycle_reset()

    def _cycle_schedule_close(self):
        self._cycle_phase = "COUNTDOWN_TO_CLOSE"
        self._cycle_deadline_ts = time.time() + 60
        self._cycle_last_log_ts = 0
        logger.info("[20H] Entered desktop session. Will close session in 60s.")

    def _cycle_schedule_reconnect(self):
        self._cycle_phase = "WAIT_RECONNECT"
        self._cycle_deadline_ts = time.time() + (self.sleep_20hour * 60)
        self._cycle_last_log_ts = 0
        logger.info(f"[20H] Session closed. Will reconnect after {self.sleep_20hour}min.")

    def _cycle_try_close_session(self):
        try:
            logger.warning("[20H] Closing session: pkill -f uSmartView")
            subprocess.call(["pkill", "-9", "-f", "uSmartView"])
        except Exception as e:
            logger.error(f"[20H] Close session failed: {e}")

    def _after_close_advance_or_sleep(self):
        if not self.runtime_indices:
            logger.info("[SERIAL] No runtime_indices, entering WAIT_RECONNECT.")
            self.runtime_ptr = 0
            self.runtime_indices = []
            self._cycle_schedule_reconnect()
            return

        if self.runtime_ptr + 1 < len(self.runtime_indices):
            self.runtime_ptr += 1
            self.after_close_wait_until = time.time() + self.SERIAL_SWITCH_WAIT_SECONDS
            logger.info(f"[SERIAL] Switching to next desktop (runtime_ptr={self.runtime_ptr}) after {self.SERIAL_SWITCH_WAIT_SECONDS}s.")
            self._cycle_reset()
            s = self.get_cdp_session()
            if s:
                logger.info("[SERIAL] Reloading UI to prepare next desktop connect...")
                s.reload()
            return

        logger.info("[SERIAL] All desktops finished. Entering WAIT_RECONNECT sleep window.")
        self.runtime_ptr = 0
        self.runtime_indices = []
        self._cycle_schedule_reconnect()

    # --- SESSION ---
    def _do_mouse_jiggle(self):
        try:
            s = self.get_cdp_session()
            if s:
                rx, ry = random.randint(200, 600), random.randint(200, 600)
                s.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rx, "y": ry})
                logger.info(f"[ACT] IN_SESSION: Mouse Jiggle to ({rx}, {ry}) to keep alive.")
        except Exception as e:
            logger.error(f"Heartbeat Jiggle Failed: {e}")

    def handle_in_session_state(self):
        now = time.time()

        if self.is_20hour:
            if self._cycle_phase == "IDLE":
                self._cycle_schedule_close()

            if self._cycle_phase == "COUNTDOWN_TO_CLOSE":
                remaining = self._cycle_deadline_ts - now
                if remaining <= 0:
                    self._cycle_try_close_session()
                    self._after_close_advance_or_sleep()
                    return
                self._cycle_log(f"[20H] In session. Closing in {remaining:.0f}s", every_seconds=15)
                return

            if self._cycle_phase == "WAIT_RECONNECT":
                self._cycle_log("[20H] Still in session while waiting reconnect. Retrying close...", every_seconds=30)
                self._cycle_try_close_session()
                return

        if now - self.last_keepalive > self.keepalive_interval:
            self._do_mouse_jiggle()
            self.last_keepalive = now
            self.keepalive_interval = random.randint(self.min_int, self.max_int)

    # --- UNKNOWN / ZOMBIE ---
    def handle_unknown_state(self, duration):
        if duration > 30:
            logger.error("[ACT] UNKNOWN STUCK (>30s) -> FORCE RELOAD")
            s = self.get_cdp_session()
            if s:
                s.reload()

    def handle_zombie_state(self):
        logger.error("[ACT] ZOMBIE PROCESS -> KILLING")
        subprocess.call(["pkill", "-9", "-f", "uSmartView"])

    # --- Monitor / loop ---
    def monitor_state(self, current_state):
        duration = time.time() - self.state_start_time

        if self.is_20hour and self._cycle_phase == "WAIT_RECONNECT" and current_state != State.IN_SESSION:
            return

        if current_state == State.WAIT:
            return self.handle_wait_state(duration)
        if current_state == State.LOGIN:
            return self.handle_login_state(duration)
        if current_state == State.UPDATING:
            return self.handle_updating_state()
        if current_state == State.DESKTOP_LIST:
            return self.handle_desktop_list_state(duration)
        if current_state == State.CONNECTING:
            return self.handle_connecting_state(duration)
        if current_state == State.IN_SESSION:
            return self.handle_in_session_state()
        if current_state == State.UNKNOWN:
            return self.handle_unknown_state(duration)
        if current_state == State.ZOMBIE:
            return self.handle_zombie_state()
        if current_state == State.GUIDE:
            return self.handle_guide_state()

    def run(self):
        logger.info(">>> VDI FSM Bot Started (Router-Aware)")
        while True:
            try:
                new_state = self.detect_state()

                if new_state != self.state:
                    logger.info(f"TRANSITION: {self.state.name} -> {new_state.name}")
                    if self.state == State.IN_SESSION and new_state != State.IN_SESSION:
                        self._cycle_reset_if_not_waiting()
                    self.state = new_state
                    self.state_start_time = time.time()
                    self.last_action_time = 0

                # 20H wait reconnect handler
                if self.is_20hour and self._cycle_phase == "WAIT_RECONNECT" and self.state != State.IN_SESSION:
                    now = time.time()
                    remaining = self._cycle_deadline_ts - now
                    if remaining <= 0:
                        logger.info("[20H] Reconnect window reached. Forcing UI reload to trigger reconnect flow...")
                        s = self.get_cdp_session()
                        if s:
                            s.reload()
                        self._cycle_reset()
                        self.runtime_ptr = 0
                        self.runtime_indices = []
                        self._dyn_last_count = None
                    else:
                        self._cycle_log(f"[20H] Waiting reconnect: {remaining:.0f}s remaining", every_seconds=30)

                self.monitor_state(new_state)
                time.sleep(2)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Loop Crash: {e}")
                time.sleep(5)
                self.session = None


if __name__ == "__main__":
    time.sleep(5)
    bot = VDIStateMachine()
    bot.run()
