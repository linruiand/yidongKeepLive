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
    WAIT = 6          # 冲突等待
    UPDATING = 7      # 发现新版本弹窗状态
    GUIDE = 8         # 新手引导页状态

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
        self.last_action_time = 0    # 追踪最后一次尝试操作的时间
        self.last_connecting_log = 0 # 追踪 CONNECTING 状态的最后一次日志时间
        self.last_healthy_time = time.time() # 新增：看门狗，记录最后一次正常业务状态的时间

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
            output = subprocess.check_output(["ps", "aux"]).decode()
            if name not in output:
                return False
            for line in output.split('\n'):
                if name in line:
                    parts = line.split()
                    if len(parts) > 7:
                        stat = parts[7]
                        if 'Z' in stat:
                            return "ZOMBIE"
            return True
        except:
            return False

    def click_at_selector(self, selector, text_hint=None):
        """Find element coordinates and perform a physical click via CDP"""
        s = self.get_cdp_session()
        if not s: return False
        

        target_selector = py_json.dumps(selector)
        target_hint = py_json.dumps(text_hint) if text_hint else "null"
        
        # JS to find element and get its center coordinates with visibility check
        js_find = f"""
            (function() {{
                try {{
                    let el;
                    if ({target_hint} !== null) {{
                        el = Array.from(document.querySelectorAll({target_selector}))
                                  .find(e => e.innerText.includes({target_hint}));
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
            s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        return False

    def click_id(self, s, element_id):
        """在指定 session 中通过 ID 执行物理点击"""
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
            s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        return False

    # --- UPDATE HANDLING ---
    def check_update_dialog(self):
        """1. 检测当前是否有更新的弹窗 (必须存在且可见)"""
        s = self.get_cdp_session()
        if not s: return False
        # 增加可见性判定：检查元素是否存在且 offsetParent 不为 null (即在渲染树中可见)
        js_check = """
            (function() {
                let el = document.querySelector('.refresh-dialog');
                if (!el) return false;
                let rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none';
            })()
        """
        return s.evaluate(js_check)

    # def get_upgrade_button_pos(self):
    #     """2. 查找 “立即更新” (按钮类名 .refresh-sure) 的中心位置坐标 (增加可见性校验)"""
    #     s = self.get_cdp_session()
    #     if not s: return None
    #     # JS 获取按钮中心坐标，并确保按钮当前是可见的
    #     js_find = """
    #         (function() {
    #             let el = document.querySelector('.refresh-sure');
    #             if (!el) return null;
    #             let rect = el.getBoundingClientRect();
    #             // 确保按钮有实际大小且不是隐藏状态
    #             if (rect.width === 0 || rect.height === 0 || window.getComputedStyle(el).display === 'none') return null;
    #             return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
    #         })()
    #     """
    #     return s.evaluate(js_find)

    def get_upgrade_url(self):
        """通过官网接口公开数据获取最新的 UOS x86_64 下载链接"""
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
                # 遍历数据结构寻找 pc -> linux -> UOS -> AMD64(x86_64)
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
        """2. 手动执行下载和更新指令 ()"""
        url = self.get_upgrade_url()
        if not url:
            logger.warning("[UPDATE] Could not find upgrade URL in DOM.")
            return False
            
        target_deb = "/tmp/vdi_update_manual.deb"
        logger.info(f"[UPDATE] Found URL: {url}. Starting manual download...")
        
        try:
            # A. 增强版下载：包含断点续传和自动重试
            curl_cmd = [
                "curl", "-L",
                "--retry", "5",               # 自动重试 5 次
                "--retry-delay", "5",         # 重试间隔 5 秒
                "--connect-timeout", "30",    # 连接超时
                "-C", "-",                    # 断点续传 (如果下载中断，下次从断点开始)
                url,
                "-o", target_deb
            ]
            subprocess.run(curl_cmd, check=True)
            logger.info(f"[UPDATE] Downloaded successfully to {target_deb}. Verifying integrity...")

            # B. 验证 .deb 文件完整性 (防止下载损坏或不全)
            try:
                # 如果文件头损坏或不完整，dpkg-deb 会报错
                subprocess.run(["dpkg-deb", "-I", target_deb], check=True, stdout=subprocess.DEVNULL)
                logger.info("[UPDATE] Package integrity check passed.")
            except subprocess.CalledProcessError:
                logger.error("[UPDATE] DEB file is corrupted. Deleting it to prevent resume errors.")
                if os.path.exists(target_deb): os.remove(target_deb)
                return False
            
            # C. 使用 dpkg 安装
            logger.info("[UPDATE] Installing via dpkg (ignoring initial dependency errors)...")
            # 这里设为 check=False，因为如果有缺依赖 dpkg 会返回错误码，我们需要后续用 apt 修复它
            subprocess.run(["dpkg", "-i", target_deb], check=False)

            # D. 自动修复依赖 (非常关键)
            logger.info("[UPDATE] Fixing broken dependencies via apt-get...")
            try:
                subprocess.run(["apt-get", "update"], check=False) # 尝试更新源索引
                subprocess.run(["apt-get", "install", "-f", "-y"], check=True)
                logger.info("[UPDATE] Dependency fixation successful.")
            except Exception as e:
                logger.warning(f"[UPDATE] Dependency fix had issues (non-fatal if package works): {e}")

            logger.info("[UPDATE] Installation process complete. Restarting services...")
            
            # E. 重启所有容器内服务，确保新版本生效并清除残留进程
            subprocess.run(["supervisorctl", "restart", "all"], check=True)
            return True
        except Exception as e:
            logger.error(f"[UPDATE] Manual update failed: {e}")
            return False

    def paste_at_selector(self, selector, text):
        """Focus element via click, Select All (Ctrl+A), and insert text via CDP (Overwrite mode)"""
        if self.click_at_selector(selector):
            s = self.get_cdp_session()
            time.sleep(1)
            # Select All (Ctrl+A) to ensure we overwrite existing content
            s.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "modifiers": 2, # Control
                "windowsVirtualKeyCode": 65, # A
                "key": "a",
                "code": "KeyA"
            })
            s.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "modifiers": 2,
                "windowsVirtualKeyCode": 65,
                "key": "a",
                "code": "KeyA"
            })
            time.sleep(0.1)
            s.send("Input.insertText", {"text": text})
            return True
        return False

    def check_conflict_state(self, s):
        """检查是否存在冲突/挤占状态 (精准识别弹窗内容)"""
        # 源码确认使用 Element Plus，弹窗类名主要为 .el-message-box
        js_get_dialog = """
            (function() {
                let box = document.querySelector('.el-message-box, .el-message');
                return box ? box.innerText : null;
            })()
        """
        dialog_text = s.evaluate(js_get_dialog)
        if not dialog_text:
            return None
            
        # 该云电脑已在其他设备上登录(A90020124)
        keywords = ["其他设备上登录", "已分配", "已回收"]
        if any(kw in dialog_text for kw in keywords):
            logger.warning(f"[SENSE] Conflict detected in Dialog: {dialog_text.strip()} -> WAIT")
            return State.WAIT
        return None

    def check_desktop_list_state(self, s, url):
        """判定是否在列表页 (#/home) 以及连接中状态"""
        if "home" not in url:
            return None
            
        # 检查主按钮状态判定是否在连接中
        is_disabled = s.evaluate("document.querySelector('.btn-link') && document.querySelector('.btn-link').disabled")
        if is_disabled:
            return State.CONNECTING
        return State.DESKTOP_LIST

    def check_login_page_state(self, s, url):
        """判定是否在登录页 (#/login)"""
        if "login" not in url:
            return None
            
        # 识别具体登录子视图供日志参考
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
        """1. 最优先判定：桌面会话是否已运行 (IN_SESSION)"""
        proc_status = self.is_process_running("uSmartView")
        if proc_status == "ZOMBIE":
            return State.ZOMBIE
        if proc_status is True:
            # 使用桌面中...
            return State.IN_SESSION
        return None

    def check_update_state(self):
        """2. 判断是否存在更新弹窗"""
        if self.check_update_dialog():
            return State.UPDATING
        return None

    # --- SENSE (State Detection) 建议应该倒过来看状态---
    def detect_state(self):
        # 0. 引导页判定 (优先级最高，因为它是交互遮挡，且可能与任何状态并存) 会和IN_SESSION 并存 优先判断
        res = self.check_guide_state()
        if res: return res

        # 1. 判定是否已经连接电脑桌面（出现桌面），会话进程状态
        res = self.check_session_state()
        if res: return res

        # 2. 判定更新弹窗状态
        res = self.check_update_state()
        if res: return res

        # 3. 获取浏览器会话进行 UI 判定
        s = self.get_cdp_session()
        if not s:
            return State.UNKNOWN

        try:
            current_url = s.evaluate("window.location.href")

            # 3. 判定是否冲突 (这种状态优先级较高)
            res = self.check_conflict_state(s)
            if res: return res

            # 4. 判定是否在列表页
            res = self.check_desktop_list_state(s, current_url)
            if res: return res

            # 5. 判定是否在登录页
            res = self.check_login_page_state(s, current_url)
            if res: return res

            if "error" in current_url:
                logger.warning("[SENSE] Error Page Detected")
                return State.UNKNOWN

        except Exception as e:
            logger.error(f"[SENSE] Error during detection: {e}")
            
        return State.UNKNOWN

    def check_guide_state(self):
        """全局判定：只要容器内存在引导页 Target，就判定为 GUIDE 状态"""
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=1) as f:
                pages = json.load(f)
                if any("bootguidor.html" in p.get('url', '') for p in pages):
                    return State.GUIDE
        except Exception as e:
            logger.error(f"[SENSE] Guide Detection Error (CDP Poll): {e}")
            pass
        return None

    def handle_wait_state(self, duration):
        """处理等待状态 (冲突挤占)：给予用户手动操作时间"""
        now = time.time()
        # 每 60 秒打印一次倒计时信息
        if int(duration) > 0 and (now - self.last_conflict_log >= 60):
            logger.warning(f"[ACT] CONFLICT WAIT: Giving user time... ({duration//60:.0f}/{self.conflict_wait//60:.0f} mins)")
            self.last_conflict_log = now
        
        # 超过配置的冲突等待时间后，刷新页面尝试恢复
        if duration > self.conflict_wait:
            logger.info("[ACT] WAIT OVER -> Refreshing to check status")
            s = self.get_cdp_session()
            if s: s.reload()
            self.last_conflict_log = 0

    def _ensure_correct_login_view(self, s):
        """子步骤：确保在正确的登录视图 (子账号 vs 账密)"""
        view_text = s.evaluate("document.querySelector('.lf-name h6') ? document.querySelector('.lf-name h6').innerText : ''")
        target_text = "子账号登录" if self.login_method == "sub_account" else "账号名密码登录"
        
        if target_text not in view_text:
            logger.info(f"[ACT] Switching to {target_text} view...")
            switch_btn_text = "子账号登录" if self.login_method == "sub_account" else "账密登录"
            if self.click_at_selector(".lf-sub p", text_hint=switch_btn_text):
                time.sleep(3) # 等待视图切换动画

    def _perform_login_action(self, s):
        """子步骤：执行填表、勾选协议并点击登录"""
        # 1. 物理模拟填表 (粘贴模式)
        user_ok = self.paste_at_selector("input[placeholder*='账号']", self.username)
        pass_ok = self.paste_at_selector("input[type='password']", self.password)
        logger.info(f"ok1:{user_ok} , ok2: {pass_ok}")
        
        if user_ok and pass_ok:
            # 2. 勾选协议
            is_checked = s.evaluate("document.querySelector('.el-checkbox').classList.contains('is-checked')")
            if not is_checked:
                self.click_at_selector(".el-checkbox__inner")
            
            # 3. 点击登录按钮
            time.sleep(1)
            self.click_at_selector("button.el-button--primary")
            logger.info("[ACT] Login submitted.")

    def handle_login_state(self, duration):
        """处理登录页逻辑：分步调用抽离的函数"""
        now = time.time()
        if duration > 10 and (now - self.last_action_time) > 6:
            self.reload_config() 
            logger.info(f"[ACT] LOGIN: Processing {self.login_method} login for {self.username}...")
            self.last_action_time = now
            
            s = self.get_cdp_session()
            if not s: return

            # 调用抽离的子步骤
            self._ensure_correct_login_view(s)
            self._perform_login_action(s)

    def handle_updating_state(self):
        """处理更新逻辑：触发手动更新脚本"""
        now = time.time()
        if (now - self.last_action_time) > 10:
            self.last_action_time = now
            logger.info("[ACT] UPDATING: Update dialog found. Triggering manual update flow...")
            self.perform_manual_update()

    def handle_desktop_list_state(self, duration):
        """处理列表页逻辑：点击连接指定索引的桌面"""
        now = time.time()
        if duration > 5 and (now - self.last_action_time) > 10:
            logger.info(f"[ACT] LIST: Connecting to desktop index {self.connect_index}...")
            self.last_action_time = now
            
            # 使用物理点击代替 JS 点击
            # 通过 nth-child 找到第 N 个可用按钮
            target_selector = f".h-item-wrap:nth-child({self.connect_index + 1}) .btn-link"
            if self.click_at_selector(target_selector):
                logger.info("[ACT] Desktop link clicked.")

    def handle_connecting_state(self, duration):
        """处理连接中状态：显示倒计时并实现超时自动重刷（Watchdog）"""
        # 用时间戳控制日志频率：精确每 5 秒打印一次，不依赖 % 运算
        now = time.time()
        if now - self.last_connecting_log >= 5:
            logger.info(f"[ACT] CONNECTING: Waiting for VDI Launch... ({duration:.0f}s)")
            self.last_connecting_log = now
        # 看门狗逻辑：如果连接超过 60 秒，可能前端卡死，强制刷新
        if duration > 60:
            logger.warning("[ACT] CONNECTING timeout -> Reloading UI")
            s = self.get_cdp_session()
            if s: s.reload()

    def _do_mouse_jiggle(self):
        """执行具体的鼠标随机移动动作"""
        try:
            s = self.get_cdp_session()
            if s:
                # 产生一个随机的“拟人”坐标，避免点到边角按钮
                rx, ry = random.randint(200, 600), random.randint(200, 600)
                s.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": rx,
                    "y": ry
                })
                logger.info(f"[ACT] IN_SESSION: Mouse Jiggle to ({rx}, {ry}) to keep alive.")
        except Exception as e:
            logger.error(f"Heartbeat Jiggle Failed: {e}")

    def handle_guide_state(self):
        """处理 GUIDE 状态：探测并关闭引导页"""
        try:
            # 1. 获取引导页 Target
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=2) as f:
                pages = json.load(f)
                guide_p = next((p for p in pages if p['type'] == 'page' and "bootguidor.html" in p['url']), None)
            
            if not guide_p or not guide_p.get('webSocketDebuggerUrl'):
                return

            # 2. 私有连接点击
            tmp_s = CDPSession(guide_p['webSocketDebuggerUrl'])
            try:
                if self.click_id(tmp_s, "J_bootGuidorBtn"):
                    logger.info("[ACT] Guide page DISMISSED. Waiting for UI sync...")
                    time.sleep(1) # 给 UI 一点消失的时间
            finally:
                tmp_s.close()
        except Exception as e:
            logger.error(f"[ACT] Guide Clearing Failed: {e}")
            pass

    def handle_in_session_state(self):
        """处理会话运行中状态"""
        # 执行鼠标随机移动（Jiggle）以防止超时断开
        now = time.time()
        if now - self.last_keepalive > self.keepalive_interval:
            self._do_mouse_jiggle()
            self.last_keepalive = now
            self.keepalive_interval = random.randint(self.min_int, self.max_int)

    def handle_unknown_state(self, duration):
        """处理挂起或加载长久未响应"""
        if duration > 30:
            logger.error("[ACT] UNKNOWN STUCK (>30s) -> FORCE RELOAD")
            s = self.get_cdp_session()
            if s: s.reload()

    def handle_zombie_state(self):
        """处理进程僵死"""
        logger.error("[ACT] ZOMBIE PROCESS -> KILLING")
        subprocess.call(["pkill", "-9", "-f", "uSmartView"])

    def force_system_reset(self):
        """连续多次未知状态，执行深度清理并强制退出以触发重启"""
        logger.error("[FATAL] UNKNOWN state persisted 5 times. Force killing VDI cluster for restart.")
        # 杀掉所有 CMCC 相关组件和可能的残留
        # pkill -9 -f "cmcc-jtydn|QoEAgent|usbredirect|chuanyun-redirect|bootCypc|uSmartView"
        kill_cmd = 'pkill -9 -f "cmcc-jtydn|QoEAgent|uSmartView|usbredirect|chuanyun-redirect|bootCypc"'
        subprocess.call(kill_cmd, shell=True)
        # 退出当前脚本，由 supervisor 负责重启
        sys.exit(1)

    # --- ACT (State Handlers) ---
    def monitor_state(self, current_state):
        duration = time.time() - self.state_start_time
        
        # --- 优雅的看门狗逻辑 ---
        # 只要不是在“未知”或“僵死”状态，就定义为“健康/有进展”，更新时间戳
        if current_state not in [State.UNKNOWN, State.ZOMBIE]:
            self.last_healthy_time = time.time()
        
        # 如果超过 120 秒（可调）没进入过任何健康状态，说明环境彻底卡死，触发重置
        if time.time() - self.last_healthy_time > 120:
            logger.error(f"[WATCHDOG] Unhealthy for {time.time() - self.last_healthy_time:.0f}s. Triggering reset.")
            self.force_system_reset()

        # --- 各状态具体处理 ---
        if current_state == State.WAIT:
            self.handle_wait_state(duration)
            return

        if current_state == State.LOGIN:
            self.handle_login_state(duration)
            return

        elif current_state == State.UPDATING:
            self.handle_updating_state()
            return

        elif current_state == State.DESKTOP_LIST:
            self.handle_desktop_list_state(duration)
            return

        elif current_state == State.CONNECTING:
            self.handle_connecting_state(duration)
            return

        elif current_state == State.IN_SESSION:
            self.handle_in_session_state()
            return

        elif current_state == State.UNKNOWN:
            self.handle_unknown_state(duration)
            return

        elif current_state == State.ZOMBIE:
            self.handle_zombie_state()
            return

        elif current_state == State.GUIDE:
            self.handle_guide_state()
            return

    # --- LOOP ---
    # 自动操作机器人，使用类似行为树的概念，不断检测和推进状态
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
                    # 记录机器人进入当前状态的那一刻时间。
                    self.state_start_time = time.time()
                    # 记录上一次执行物理动作
                    self.last_action_time = 0
                
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
