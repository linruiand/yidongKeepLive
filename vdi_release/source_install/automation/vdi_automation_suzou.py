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
"""

import os
import sys
import time
import json
import json as py_json
import random
import logging
import urllib.request
import traceback
import subprocess
from enum import Enum

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
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

# --- CDP HELPER ---
class CDPSession:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=5)
        self.msg_id = 0
        
    def send(self, method, params=None):
        self.msg_id += 1
        message = { "id": self.msg_id, "method": method, "params": params or {} }
        try:
            self.ws.send(json.dumps(message))
            while True:
                resp = self.ws.recv()
                data = json.loads(resp)
                if data.get("id") == self.msg_id:
                    if "error" in data:
                        return None 
                    return data.get("result")
        except Exception as e:
            return None

    def evaluate(self, expression):
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        if not res: return None
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
        try: self.ws.close()
        except: pass

# --- STATES ---
class State(Enum):
    UNKNOWN = 0       # 初始状态或未定义页面
    LOGIN = 1         # 登录界面 (#/login)
    DESKTOP_LIST = 2  # 云电脑列表主界面 (#/home)
    CONNECTING = 3    # 正在建立桌面连接（加载中）
    IN_SESSION = 4    # 已成功进入桌面会话
    ZOMBIE = 5        # 客户端卡死或无响应状态
    WAIT = 6          # 触发风控或冲突后的冷却等待

# --- MAIN CONTROLLER ---
class VDIStateMachine:
    def __init__(self):
        self.reload_config()
        self.cdp_url = "http://localhost:9222"
        self.session = None
        self.last_keepalive = time.time()
        self.state = State.UNKNOWN
        self.last_state = None
        self.state_start_time = time.time()
        self.last_action_time = 0  # 追踪最后一次尝试操作的时间

    def reload_config(self):
        self.config = load_config()
        self.username = self.config.get('phone', '')
        self.password = self.config.get('password', '')
        self.login_method = self.config.get('login_method', 'password')
        self.connect_index = int(self.config.get('connect_index', 0))
        self.min_int = int(self.config.get('keepalive_min_seconds', 120))
        self.max_int = int(self.config.get('keepalive_max_seconds', 300))
        self.keepalive_method = self.config.get('keepalive_method', 'mouse_move')
        self.conflict_wait = int(self.config.get('conflict_wait_seconds', 300))
        self.keepalive_interval = random.randint(self.min_int, self.max_int)
        self.last_conflict_log = 0

    def get_cdp_session(self):
        """Get or refresh CDP session"""
        if self.session:
            # Check if alive via stealthy heartbeat
            if self.session.is_alive():
                return self.session
            else:
                logger.warning("CDP Session lost. Reconnecting...")
                self.session.close()
                self.session = None

        # Connect new
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=3) as f:
                pages = json.load(f)
                logger.info(f"CDP Poll: Found {len(pages)} targets")
                ws_url = next((p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page'), None)
                if ws_url:
                    self.session = CDPSession(ws_url)
                    return self.session
        except Exception as e:
            logger.error(f"CDP Connect Error: {e}")
            pass
        return None

    def is_process_running(self, name):
        try:
            # Use pgrep with exact match if possible, or refined grep
            # We want to match /usr/bin/name but not /usr/bin/nameServiceAgent
            p1 = subprocess.Popen(["ps", "-eo", "comm,args"], stdout=subprocess.PIPE)
            output = p1.communicate()[0].decode().strip()
            
            if not output:
                return False
                
            lines = output.split('\n')
            found = False
            for line in lines:
                parts = line.split(None, 1)
                comm = parts[0]
                args = parts[1] if len(parts) > 1 else ""
                
                # Check if the command name is an exact match
                # or if the base name of the first argument (executable path) is an exact match
                if comm == name:
                    found = True
                elif f"/{name} " in args or args.endswith(f"/{name}"):
                    # Specifically exclude common helper patterns
                    if "AutoStart" not in args and "ServiceAgent" not in args and "UapAgent" not in args:
                        found = True
                
                if found:
                    # Check for Zombie in 'ps aux' if we found it
                    check_zombie = subprocess.check_output(["ps", "-C", comm, "-o", "state="]).decode()
                    if 'Z' in check_zombie:
                         return "ZOMBIE"
                    return True
            return False
        except:
            return False

    def click_at_selector(self, selector, text_hint=None):
        """Find element coordinates (optionally filtering by text) and perform a physical click"""
        s = self.get_cdp_session()
        if not s: return False
        
        target_selector = py_json.dumps(selector)
        target_hint = py_json.dumps(text_hint) if text_hint else "null"
        
        js_find = f"""
            (function() {{
                try {{
                    let hint = ({target_hint} || "").replace(/\\s+/g, "").toLowerCase();
                    let selector = {target_selector};
                    let elements = Array.from(document.querySelectorAll(selector === "*" ? "button, span, div, a, .btn, .el-button" : selector));
                    
                    let matches = elements.filter(e => {{
                        let text = (e.innerText || "").replace(/\\s+/g, "").toLowerCase();
                        if (!hint) return true;
                        if (!text.includes(hint)) return false;
                        let r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.width < 1000;
                    }});
                    
                    if (matches.length === 0) return null;
                    
                    // Smallest area first to hit the button, not container
                    matches.sort((a, b) => {{
                        let ra = a.getBoundingClientRect();
                        let rb = b.getBoundingClientRect();
                        return (ra.width * ra.height) - (rb.width * rb.height);
                    }});
                    
                    let el = matches[0];
                    let r = el.getBoundingClientRect();
                    // Handle both old (left/top) and new (x/y) DOMRect APIs
                    let x = (r.x !== undefined ? r.x : r.left) + r.width/2;
                    let y = (r.y !== undefined ? r.y : r.top) + r.height/2;
                    return {{
                        x: x, 
                        y: y,
                        tag: el.tagName,
                        cls: el.className,
                        rect_debug: {{
                            x: r.x,
                            y: r.y,
                            left: r.left,
                            top: r.top,
                            width: r.width,
                            height: r.height
                        }}
                    }};
                }} catch(e) {{ return null; }}
            }})()
        """
        res = s.evaluate(js_find)
        logger.info(f"[DEBUG] click_at_selector({selector}, {text_hint}) -> {res}")
        if not res or not isinstance(res, dict) or 'x' not in res:
             return False
        
        x, y = res['x'], res['y']
        logger.info(f"[CDP] Physical click at {selector} ({x:.1f}, {y:.1f})")
        
        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        time.sleep(0.05)
        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True

    def paste_at_selector(self, selector, value):
        """Focus via click, Clear, and Type character-by-character (Hardware Simulation) - TWICE for reliability"""
        
        def _perform_input():
            # 1. Focus
            if not self.click_at_selector(selector):
                return False
            
            s = self.get_cdp_session()
            if not s: return False
            
            # Wait for focus to settle
            time.sleep(0.5)
            
            # 2. Clear Field (Ctrl+A -> Backspace)
            # Ctrl+A
            s.send("Input.dispatchKeyEvent", {"type": "keyDown", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a", "code": "KeyA"})
            s.send("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a", "code": "KeyA"})
            time.sleep(0.1)
            # Backspace
            s.send("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace"})
            s.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace"})
            time.sleep(0.1)

            # 3. Type Character by Character
            val_str = str(value)
            for char in val_str:
                # Simulate key press event flow
                s.send("Input.dispatchKeyEvent", {
                    "type": "char",
                    "text": char,
                    "unmodifiedText": char
                })
                # Small random jitter between keystrokes (human-like)
                time.sleep(random.uniform(0.01, 0.05))
            
            # 4. Trigger 'input' event manually just in case
            time.sleep(0.2)
            s.evaluate(f"let el = document.querySelector('{selector}'); if(el) {{ el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
            return True

        # First Pass (Might be partial due to lag)
        if not _perform_input(): return False
        
        # Second Pass (Overwrite to ensure correctness)
        time.sleep(0.5)
        return _perform_input()

    # --- SENSE (State Detection) 建议应该倒过来看状态---
    def detect_state(self):
        # 1. Highest Priority: Native Session Active (uSmartView)
        proc_status = self.is_process_running("uSmartView")
        if proc_status == "ZOMBIE": return State.ZOMBIE
        if proc_status is True: return State.IN_SESSION

        # 2. Get CDP Session
        s = self.get_cdp_session()
        if not s: return State.UNKNOWN

        try:
            # Check for unique VISIBLE DOM elements
            dom_info = s.evaluate("""
                (function() {
                    function isVisible(el) {
                        if (!el) return false;
                        let style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        let rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }
                    let res = { login: false, list: false, conflict: false };
                    if (isVisible(document.querySelector('.system-login'))) res.login = true;
                    if (isVisible(document.querySelector('.desktopList')) || isVisible(document.querySelector('.comName'))) res.list = true;
                    if (document.body.innerText.includes('其他设备') || document.body.innerText.includes('被挤')) res.conflict = true;
                    return res;
                })()
            """)
            
            if not dom_info: return State.UNKNOWN

            if dom_info['conflict']:
                logger.warning("[SENSE] Conflict detected via DOM text")
                return State.WAIT
            
            if dom_info['list']:
                return State.DESKTOP_LIST
            
            if dom_info['login']:
                return State.LOGIN

            # Fallback for connecting state (VISIBLE loading mask)
            if s.evaluate("""
                (function() {
                    let el = document.querySelector('.el-loading-mask');
                    return !!(el && window.getComputedStyle(el).display !== 'none');
                })()
            """):
                return State.CONNECTING

        except Exception as e:
            logger.error(f"[SENSE] Error during detection: {e}")
            
        return State.UNKNOWN

    # --- ACT (State Handlers) ---
    def monitor_state(self, current_state):
        duration = time.time() - self.state_start_time
        s = self.get_cdp_session()
        if not s: return

        # --- 0. Global Prompt / Guide Handlers (Minimal Intrusion) ---
        
        # A. Skip Guide / Tips if present
        if s.evaluate("""
            (function() {
                // 1. Specific class known from VDI tips
                let el = document.querySelector('.animationBtnPass');
                if (el && el.getBoundingClientRect().width > 0) return true;
                
                // 2. Search for common 'Skip' or 'Got it' text in visible elements
                let skipText = ['跳过', '知道了', '我知道了'];
                let skip = Array.from(document.querySelectorAll('button, span, .btn, .link')).find(e => 
                    skipText.some(t => e.innerText.includes(t)) && 
                    e.getBoundingClientRect().width > 0
                );
                return !!skip;
            })()
        """):
            logger.info("[ACT] Guidance/Tip detected. Clicking to skip...")
            if not self.click_at_selector(".animationBtnPass"):
                self.click_at_selector("button", text_hint="跳过") or \
                self.click_at_selector("span", text_hint="跳过") or \
                self.click_at_selector("button", text_hint="知道了") or \
                self.click_at_selector(".btn", text_hint="我知道了")
            time.sleep(1)
            return

        # B. Handle Agreement Dialog (Prompt: "I Agree")
        if s.evaluate("""
            (function() {
                let btn = document.querySelector('.sureBtn');
                return !!(btn && btn.getBoundingClientRect().width > 0);
            })()
        """):
            logger.info("[ACT] Agreement dialog (Prompt) detected. Clicking '.sureBtn'...")
            self.click_at_selector(".sureBtn")
            time.sleep(1)
            return

        # C. Handle Generic Confirmation Dialog (Prompt: "Sure" / "OK")
        if s.evaluate("""
            (function() {
                let targets = ['确定', '确认', '好的', '我知道了', '知道了'];
                let btn = Array.from(document.querySelectorAll('button, .el-button, .el-dialog__footer button, .btn')).find(e => 
                    targets.some(t => (e.innerText || "").trim() === t) && 
                    e.getBoundingClientRect().width > 0
                );
                return !!btn;
            })()
        """):
            logger.info("[ACT] Confirmation dialog detected. Clicking...")
            for t in ['确定', '确认', '我知道了', '知道了']:
                if self.click_at_selector("*", text_hint=t):
                    time.sleep(1)
                    return

        if current_state == State.WAIT:
            # User Requested: Wait Configured Time if squeezed
            now = time.time()
            if int(duration) > 0 and (now - self.last_conflict_log >= 60):
                logger.warning(f"[ACT] CONFLICT WAIT: Giving user time... ({duration//60:.0f}/{self.conflict_wait//60:.0f} mins)")
                self.last_conflict_log = now
            
            if duration > self.conflict_wait:
                logger.info("[ACT] WAIT OVER -> Refreshing to check status")
                if s: s.reload()
                self.last_conflict_log = 0
            return

        if current_state == State.LOGIN:
            now = time.time()
            if duration > 5 and (now - self.last_action_time) > 8:
                self.reload_config()
                logger.info(f"[ACT] LOGIN View: Processing {self.login_method} login...")
                self.last_action_time = now

                # 1. Force Password Login Mode
                # If "发送验证码" is visible or password input is missing, switch to password mode.
                wrong_mode = s.evaluate("""
                    (function() {
                        let has_code_text = document.body.innerText.includes("发送验证码");
                        let password_input = document.querySelector('input[type="password"]');
                        return has_code_text || !password_input;
                    })()
                """)
                if wrong_mode:
                    logger.info("[ACT] Not in password login mode. Aggressively switching...")
                    # The password button is usually '.password' or has tooltip '密码'
                    # Try to find a VISIBLE password button first
                    clicked = False
                    
                    # Method 1: Try clicking visible .password button
                    result = s.evaluate("""
                        (function() {
                            let btn = document.querySelector('button.password');
                            if (btn) {
                                let r = btn.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                                }
                            }
                            return null;
                        })()
                    """)
                    
                    if result and isinstance(result, dict) and result.get('x', 0) > 0:
                        logger.info(f"[ACT] Found visible .password button at ({result['x']}, {result['y']})")
                        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                        time.sleep(0.05)
                        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                        clicked = True
                    else:
                        # Method 2: Use known coordinates from DOM inspection (805, 760 is typical position)
                        logger.info("[ACT] Using fallback coordinates for password button (805, 760)...")
                        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 828, "y": 760, "button": "left", "clickCount": 1})
                        time.sleep(0.05)
                        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 828, "y": 760, "button": "left", "clickCount": 1})
                        clicked = True
                    
                    if clicked:
                        logger.info("[ACT] Password mode button clicked")
                    
                    time.sleep(2)
                    return # Loop again to re-check State in the new mode

                # 2. Check/Tick Privacy Policy Checkbox
                is_unchecked = s.evaluate("""
                    (function() {
                        // Look for the checkbox image. Usually next to "我已阅读并同意"
                        let btn = document.querySelector('.item3 button') || document.querySelector('.fp-nocheck');
                        if (!btn) {
                            // Try finding by text
                           let container = Array.from(document.querySelectorAll('div, span'))
                                                .find(e => (e.innerText || "").includes("我已阅读并同意") && e.getBoundingClientRect().width > 0);
                           if (container) btn = container.querySelector('button') || container.querySelector('img');
                        }
                        if (!btn) return false;
                        
                        // Check if it's already checked by looking at classes or image src
                        let html = btn.outerHTML || "";
                        let is_already_checked = html.includes('checked') && !html.includes('nocheck');
                        if (is_already_checked) return false;
                        
                        let img = btn.querySelector('img') || (btn.tagName === 'IMG' ? btn : null);
                        return !!(img && (img.src.includes('NoCheck') || img.src.includes('nocheck')));
                    })()
                """)
                if is_unchecked:
                    logger.info("[ACT] Privacy policy checkbox is UNCHECKED. Ticking it...")
                    # Click the checkbox button specifically
                    self.click_at_selector(".item3 button") or self.click_at_selector(".fp-nocheck") or self.click_at_selector(".item3 img")
                    time.sleep(0.5)

                # 2.5 Ensure 'Auto Login' and 'Remember Password' are ticked
                for target in ['自动登录', '记住密码']:
                    try:
                        # 1. Check status
                        status = s.evaluate(f"""
                            (function() {{
                                let el = Array.from(document.querySelectorAll('span, button'))
                                              .find(e => (e.innerText || "").includes("{target}"));
                                if (!el) return null;
                                let parent = el.tagName === 'BUTTON' ? el : el.parentElement;
                                let img = parent.querySelector('img');
                                let unchecked = img && img.src.includes('NoCheck');
                                return {{ unchecked: !!unchecked }};
                            }})()
                        """)
                        
                        if status and status.get('unchecked'):
                            logger.info(f"[ACT] Ticking '{target}'...")
                            # 2. Click it
                            self.click_at_selector("*", text_hint=target)
                            time.sleep(1.5)  # Wait longer for dialog to appear
                            
                            # 3. Handle the confirmation dialog that appears after clicking
                            # The dialog is a security warning with "确认" button (class: sureBtn)
                            logger.info(f"[ACT] Looking for confirmation dialog after ticking '{target}'...")
                            
                            confirmed = False
                            
                            # Strategy 1: Look for the specific .sureBtn button (most reliable)
                            result = s.evaluate("""
                                (function() {
                                    let btn = document.querySelector('.sureBtn, .el-button--primary.sureBtn');
                                    if (btn) {
                                        let r = btn.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) {
                                            return {x: r.x + r.width/2, y: r.y + r.height/2, text: btn.innerText};
                                        }
                                    }
                                    return null;
                                })()
                            """)
                            
                            if result and isinstance(result, dict) and result.get('x', 0) > 0:
                                logger.info(f"[ACT] Found .sureBtn at ({result['x']}, {result['y']}) with text '{result.get('text', '')}'")
                                s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                                time.sleep(0.05)
                                s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                                confirmed = True
                                logger.info(f"[ACT] Confirmed '{target}' by clicking .sureBtn")
                                time.sleep(0.5)
                            else:
                                # Strategy 2: Look for any button with text "确认" in dialogs
                                for confirm_text in ['确认', '确定', '我知道了']:
                                    result = s.evaluate(f"""
                                        (function() {{
                                            let btns = Array.from(document.querySelectorAll('button, .btn'));
                                            let btn = btns.find(b => {{
                                                let text = (b.innerText || "").trim();
                                                let r = b.getBoundingClientRect();
                                                return text === "{confirm_text}" && r.width > 0 && r.height > 0;
                                            }});
                                            if (btn) {{
                                                let r = btn.getBoundingClientRect();
                                                return {{x: r.x + r.width/2, y: r.y + r.height/2}};
                                            }}
                                            return null;
                                        }})()
                                    """)
                                    
                                    if result and isinstance(result, dict) and result.get('x', 0) > 0:
                                        logger.info(f"[ACT] Found dialog button '{confirm_text}' at ({result['x']}, {result['y']})")
                                        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                                        time.sleep(0.05)
                                        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": result['x'], "y": result['y'], "button": "left", "clickCount": 1})
                                        confirmed = True
                                        logger.info(f"[ACT] Confirmed '{target}' with '{confirm_text}'")
                                        time.sleep(0.5)
                                        break
                            
                            if not confirmed:
                                logger.warning(f"[ACT] Could not find confirmation button for '{target}'")
                    except Exception as e:
                        logger.error(f"Error handling {target}: {e}")

                # 3. Enter Credentials (Focus -> Ctrl+A -> Type)
                user_ok = self.paste_at_selector(".inputName input", self.username) or \
                          self.paste_at_selector("input[placeholder*='账号']", self.username)
                
                # Wait between fields to ensure UI processes the first input
                time.sleep(1)

                pass_ok = self.paste_at_selector(".inputCode input", self.password) or \
                          self.paste_at_selector("input[placeholder*='密码']", self.password)
                
                logger.info(f"Form Fill -> User: {user_ok}, Pass: {pass_ok}")

                if user_ok and pass_ok:
                    # 4. Submit
                    logger.info("[ACT] Submitting login request...")
                    if self.click_at_selector(".inputLoginText") or \
                       self.click_at_selector(".input31") or \
                       self.click_at_selector("button.el-button--primary", text_hint="登录"):
                        logger.info("[ACT] Login submitted. Waiting transition...")
                        time.sleep(5)

        elif current_state == State.DESKTOP_LIST:
            now = time.time()
            if duration > 5 and (now - self.last_action_time) > 10:
                logger.info(f"[ACT] LIST: Connecting to desktop index {self.connect_index}...")
                self.last_action_time = now
                
                # Connect logic based on DesktopList assets and physical center click
                js_find_btn = f"""
                    (function() {{
                        let items = document.querySelectorAll('.comName, .h-item, .comAction');
                        if (items.length > {self.connect_index}) {{
                             let item = items[{self.connect_index}];
                             let btn = item.innerText.includes('连接') ? item : item.querySelector('button');
                             if (!btn) return null;
                             let rect = btn.getBoundingClientRect();
                             return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
                        }}
                        return null;
                    }})()
                """
                pos = s.evaluate(js_find_btn)
                if pos:
                    x, y = pos['x'], pos['y']
                    s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                    s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    logger.info(f"[ACT] Connection triggered at ({x}, {y})")
                else:
                    logger.warning("[ACT] Could not find Connect button for specified index.")

        elif current_state == State.CONNECTING:
             if int(duration) % 5 == 0:
                 logger.info(f"[ACT] CONNECTING: Waiting for VDI Launch... ({duration:.0f}s)")
             # Watchdog: If connecting > 60s, maybe the app hung?
             if duration > 60:
                 logger.warning("[ACT] CONNECTING timeout -> Reloading UI")
                 if s: s.reload()

        elif current_state == State.IN_SESSION:
            # Keep Alive via CDP (No PyAutoGUI)
            # 必须模拟鼠标移动，否则 VDI 客户端会判定为闲置并断开
            now = time.time()
            if now - self.last_keepalive > self.keepalive_interval:
                try:
                    s = self.get_cdp_session()
                    if s:
                        # 产生一个随机的“拟人”坐标
                        # 在 200-600 像素的中间安全区域抖动，避免意外点到边角的退出或关闭按钮
                        rx, ry = random.randint(200, 600), random.randint(200, 600)
                        s.send("Input.dispatchMouseEvent", {
                            "type": "mouseMoved",
                            "x": rx,
                            "y": ry
                        })
                        logger.info(f"[ACT] IN_SESSION: Mouse Jiggle to ({rx}, {ry}) to keep alive.")
                except Exception as e:
                    logger.error(f"Heartbeat Jiggle Failed: {e}")
                
                self.last_keepalive = now
                self.keepalive_interval = random.randint(self.min_int, self.max_int)

        elif current_state == State.UNKNOWN:
             if duration > 30:
                 logger.error("[ACT] UNKNOWN STUCK (>30s) -> FORCE RELOAD")
                 if s: s.reload()

        elif current_state == State.ZOMBIE:
            logger.error("[ACT] ZOMBIE PROCESS -> KILLING")
            subprocess.call(["pkill", "-9", "-f", "uSmartView"])

    # --- LOOP ---
    def run(self):
        logger.info(">>> VDI FSM Bot Started (Router-Aware)")
        while True:
            try:
                # 1. Sense
                new_state = self.detect_state()
                
                # 2. State Transition
                if new_state != self.state:
                    logger.info(f"TRANSITION: {self.state.name} -> {new_state.name}")
                    self.state = new_state
                    self.state_start_time = time.time()
                    self.last_action_time = 0 # 状态切换时重置操作计时器
                
                # 3. Act
                self.monitor_state(new_state)
                
                # 4. Tick
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
