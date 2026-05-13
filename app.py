import customtkinter as ctk
import threading
import speech_recognition as sr
from llama_cpp import Llama
import os
import subprocess
import socket
import sys
import json
import urllib.request
import urllib.parse
import ssl
import time
import queue
import re
from datetime import datetime, timedelta
import uuid

LOG_FILE = "/home/phablet/Documents/local_ai_v1/debug_log.txt"
try:
    with open(LOG_FILE, "a") as f:
        f.write("--- NEW SESSION ---\n")
    sys.stdout = open(LOG_FILE, "a")
    sys.stderr = sys.stdout
except Exception:
    pass

class LocalAIApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PI Assistant")
        ctk.set_window_scaling(1.5)
        ctk.set_widget_scaling(1.5)
        self.attributes("-zoomed", True)
        ctk.set_appearance_mode("dark")

        self.base_dir = "/home/phablet/Documents/local_ai_v1"
        self.llm_model_path = f"{self.base_dir}/models/LFM2.5-1.2B-Thinking-Q4_K_M.gguf"
        self.piper_executable = f"{self.base_dir}/piper/piper"

        self.llm = None
        self.is_recording = False
        self.parecord_process = None
        self.mic_wav_file = None
        self.current_tab = "chat"

        self.languages = [
            {"label": "EN", "stt": "en-US", "tts": "en_US-amy-medium.onnx", "vosk_model": "vosk-model-small-en-us-0.15"},
            {"label": "ID", "stt": "id-ID", "tts": "id_ID-news_tts-medium.onnx", "vosk_model": "vosk-model-small-id-0.45"},
        ]
        self.lang_index = 0
        self.stt_models = {}

        self.character_name = "Maria"
        self.brain_file = f"{self.base_dir}/brain.json"
        self.memory = {"facts": [], "tasks": [], "meetings": [], "quotations": [], "projects": []}
        self.chat_history = []
        self.load_brain()

        self.tts_queue = queue.Queue()
        threading.Thread(target=self.tts_worker, daemon=True).start()
        self.is_online = False

        self.setup_ui()
        self.update_network_status()

        self.append_to_chat("System", "Loading AI model...")
        threading.Thread(target=self.load_model, daemon=True).start()

        self.reminder_check_active = True
        threading.Thread(target=self.reminder_loop, daemon=True).start()

    # ======== LOGGING ========

    def log_debug(self, msg):
        print(f"[DEBUG] {msg}")
        try:
            self.after(0, self._insert_debug_log, str(msg))
        except Exception:
            pass

    def _insert_debug_log(self, text):
        try:
            self.debug_box.configure(state="normal")
            self.debug_box.insert("end", f"{text}\n")
            self.debug_box.configure(state="disabled")
            self.debug_box.yview("end")
        except Exception:
            pass

    # ======== BRAIN (DATA STORAGE) ========

    def load_brain(self):
        try:
            if os.path.exists(self.brain_file):
                with open(self.brain_file, "r") as f:
                    loaded = json.load(f)
                    self.memory["facts"] = loaded.get("facts", [])
                    self.memory["tasks"] = loaded.get("tasks", [])
                    self.memory["meetings"] = loaded.get("meetings", [])
                    self.memory["quotations"] = loaded.get("quotations", [])
                    self.memory["projects"] = loaded.get("projects", [])
                self.log_debug("Brain loaded successfully.")
        except Exception as e:
            self.log_debug(f"Brain load: {e}")

    def save_brain(self):
        try:
            with open(self.brain_file, "w") as f:
                json.dump(self.memory, f, indent=2)
            self.log_debug("Brain saved.")
        except Exception as e:
            self.log_debug(f"Brain save: {e}")

    # ======== NETWORK ========

    def check_internet(self):
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2)
            return True
        except OSError:
            return False

    def update_network_status(self):
        def _check():
            self.is_online = self.check_internet()
            self.after(0, lambda: self.net_status_label.configure(
                text="[Online]" if self.is_online else "[Offline]",
                text_color="#00FF00" if self.is_online else "#AAAAAA"))
        threading.Thread(target=_check, daemon=True).start()
        self.after(15000, self.update_network_status)

    # ======== REMINDER LOOP ========

    def reminder_loop(self):
        while self.reminder_check_active:
            try:
                now = time.time()
                tasks = self.memory.get("tasks", [])
                remaining = []
                triggered = False
                for t in tasks:
                    if now >= t.get("time", 0):
                        memo = t.get("memo", "Reminder!")
                        msg = f"REMINDER: {memo}"
                        self.log_debug(msg)
                        self.after(0, self.append_to_chat, "System", f"[REMINDER] {memo}")
                        wav = self._generate_audio_file(f"Hey, reminder: {memo}")
                        if wav:
                            self.tts_queue.put(wav)
                        triggered = True
                    else:
                        remaining.append(t)
                if triggered:
                    self.memory["tasks"] = remaining
                    self.save_brain()

                meetings = self.memory.get("meetings", [])
                remaining_m = []
                for m in meetings:
                    m_date = m.get("date", "")
                    m_time = m.get("time", "00:00")
                    try:
                        dt_str = f"{m_date} {m_time}"
                        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                        if now >= dt.timestamp() and m.get("status") == "upcoming":
                            m["status"] = "past"
                            msg = f"Meeting now: {m['title']}"
                            self.after(0, self.append_to_chat, "System", f"[MEETING] {msg}")
                            wav = self._generate_audio_file(f"You have a meeting: {m['title']}")
                            if wav:
                                self.tts_queue.put(wav)
                            self.save_brain()
                    except Exception:
                        pass
                    remaining_m.append(m)
                self.memory["meetings"] = remaining_m

                for p in self.memory.get("projects", []):
                    deadline = p.get("deadline", "")
                    if deadline and p.get("status") == "active":
                        try:
                            dl = datetime.strptime(deadline, "%Y-%m-%d")
                            remaining_days = (dl - datetime.now()).days
                            if 0 <= remaining_days <= 1 and not p.get("deadline_warned"):
                                msg = f"Project '{p['name']}' deadline is TOMORROW!"
                                self.after(0, self.append_to_chat, "System", f"[DEADLINE] {msg}")
                                p["deadline_warned"] = True
                                self.save_brain()
                        except Exception:
                            pass

                for q in self.memory.get("quotations", []):
                    due = q.get("due_date", "")
                    if due and q.get("status") == "pending":
                        try:
                            dd = datetime.strptime(due, "%Y-%m-%d")
                            remaining_days = (dd - datetime.now()).days
                            if 0 <= remaining_days <= 2 and not q.get("due_warned"):
                                client = q.get("client", "Unknown")
                                msg = f"Quotation for {client} due in {remaining_days} days!"
                                self.after(0, self.append_to_chat, "System", f"[QUOTATION] {msg}")
                                q["due_warned"] = True
                                self.save_brain()
                        except Exception:
                            pass
            except Exception as e:
                self.log_debug(f"Reminder loop error: {e}")
            time.sleep(30)

    # ======== SEARCH ========

    def search_internet(self, query):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            search_url = "https://en.wikipedia.org/w/api.php?action=opensearch&search=" + urllib.parse.quote(query) + "&limit=1&format=json"
            req = urllib.request.Request(search_url, headers={
                'User-Agent': 'Mozilla/5.0 (Linux; Android 10)',
                'Accept': 'application/json'
            })
            res = json.loads(urllib.request.urlopen(req, timeout=5, context=ctx).read())
            if res and len(res) > 1 and res[1]:
                title = res[1][0]
                summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
                req2 = urllib.request.Request(summary_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 10)',
                    'Accept': 'application/json'
                })
                res2 = json.loads(urllib.request.urlopen(req2, timeout=5, context=ctx).read())
                if 'extract' in res2:
                    return "Wikipedia: " + res2['extract']
            return "No internet search results."
        except Exception as e:
            self.log_debug("Search fail: " + str(e))
            return ""

    # ======== MODEL ========

    def load_model(self):
        try:
            if os.path.exists(self.llm_model_path):
                self.llm = Llama(model_path=self.llm_model_path, n_ctx=8192, n_threads=8)
                self.after(0, self.on_model_loaded)
                self.log_debug("Model loaded.")
            else:
                self.after(0, self.set_ai_status, "Error: Model Missing", "#8B0000")
                self.log_debug(f"Missing model at {self.llm_model_path}")
        except Exception as e:
            self.after(0, self.set_ai_status, "Error Loading", "#8B0000")
            self.log_debug(f"LLM init error: {e}")

    def on_model_loaded(self):
        self.set_ai_status("Idle", "#AAAAAA")
        self.append_to_chat(self.character_name, "I'm ready! I can help manage your schedule, quotations, projects and more.\n")

    def set_ai_status(self, status, color="#AAAAAA"):
        try:
            self.ai_status_label.configure(text=f"Status: {status}", text_color=color)
        except Exception:
            pass

    # ======== UI SETUP ========

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        my_font = ("Inter", 16)

        # Header
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.header_frame.grid_columnconfigure(1, weight=1)

        self.net_status_label = ctk.CTkLabel(self.header_frame, text="Checking...", font=("Inter", 14))
        self.net_status_label.grid(row=0, column=0, sticky="w")

        self.ai_status_label = ctk.CTkLabel(self.header_frame, text="Status: Loading...", font=my_font, text_color="#FFA500")
        self.ai_status_label.grid(row=0, column=1)

        self.close_btn = ctk.CTkButton(self.header_frame, text="Close", width=70, height=40, font=my_font, fg_color="#8B0000", command=self.exit_app)
        self.close_btn.grid(row=0, column=2, sticky="e")

        # Tab Navigation
        self.tab_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.tab_frame.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        self.tab_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        tab_style = {"height": 45, "font": ("Inter", 14, "bold")}
        self.tab_chat_btn = ctk.CTkButton(self.tab_frame, text="Chat", **tab_style, fg_color="#2B5797", command=lambda: self.switch_tab("chat"))
        self.tab_chat_btn.grid(row=0, column=0, padx=2, sticky="ew")

        self.tab_dash_btn = ctk.CTkButton(self.tab_frame, text="Dashboard", **tab_style, command=lambda: self.switch_tab("dashboard"))
        self.tab_dash_btn.grid(row=0, column=1, padx=2, sticky="ew")

        self.tab_sched_btn = ctk.CTkButton(self.tab_frame, text="Schedule", **tab_style, command=lambda: self.switch_tab("schedule"))
        self.tab_sched_btn.grid(row=0, column=2, padx=2, sticky="ew")

        self.tab_quote_btn = ctk.CTkButton(self.tab_frame, text="Quotes", **tab_style, command=lambda: self.switch_tab("quotations"))
        self.tab_quote_btn.grid(row=0, column=3, padx=2, sticky="ew")

        self.tab_proj_btn = ctk.CTkButton(self.tab_frame, text="Projects", **tab_style, command=lambda: self.switch_tab("projects"))
        self.tab_proj_btn.grid(row=0, column=4, padx=2, sticky="ew")

        # Content area (stacked frames)
        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.grid(row=2, column=0, padx=5, pady=5, sticky="nsew")
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_rowconfigure(0, weight=1)

        # --- CHAT TAB ---
        self.chat_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.chat_frame.grid_columnconfigure(0, weight=1)
        self.chat_frame.grid_rowconfigure(0, weight=1)
        self.chat_frame.grid_rowconfigure(1, weight=0)
        self.chat_frame.grid_rowconfigure(2, weight=0)

        self.chat_box = ctk.CTkTextbox(self.chat_frame, state="disabled", wrap="word", font=("Ubuntu", 17))
        self.chat_box.grid(row=0, column=0, padx=3, pady=3, sticky="nsew")

        input_frame = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        input_frame.grid(row=1, column=0, padx=3, pady=3, sticky="ew")
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_rowconfigure(0, weight=0)
        input_frame.grid_rowconfigure(1, weight=0)

        entry_send = ctk.CTkFrame(input_frame, fg_color="transparent")
        entry_send.grid(row=0, column=0, sticky="ew")
        entry_send.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(entry_send, placeholder_text="Message...", font=my_font, height=40)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda e: self.send_message())
        self.entry.bind("<FocusIn>", lambda e: self.auto_show_keyboard())
        self.entry.bind("<KeyPress>", lambda e: self.reset_kb_timer())

        send_btn = ctk.CTkButton(entry_send, text="Send", width=60, height=40, font=("Inter", 14), command=self.send_message)
        send_btn.grid(row=0, column=1, padx=(3, 0))

        # Floating round mic button (above send button area)
        self.mic_frame = ctk.CTkFrame(entry_send, fg_color="transparent", width=0, height=0)
        self.mic_frame.grid(row=0, column=2, padx=(3, 0))
        self.voice_btn = ctk.CTkButton(self.mic_frame, text="Mic", width=44, height=44,
                                       font=("Inter", 11, "bold"), corner_radius=22, fg_color="#1F6AA5",
                                       hover_color="#144870")
        self.voice_btn.bind("<ButtonPress-1>", self.on_mic_press)
        self.voice_btn.bind("<ButtonRelease-1>", self.on_mic_release)
        self.voice_btn.pack()

        btn_frame = ctk.CTkFrame(input_frame, fg_color="transparent")
        btn_frame.grid(row=1, column=0, pady=(3, 0), sticky="ew")
        btn_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.lang_btn = ctk.CTkButton(btn_frame, text="EN", height=35, font=("Inter", 13), command=self.toggle_language)
        self.lang_btn.grid(row=0, column=0, padx=1, sticky="ew")
        ctk.CTkButton(btn_frame, text="KB", height=35, font=("Inter", 13), command=self.toggle_keyboard).grid(row=0, column=1, padx=1, sticky="ew")
        ctk.CTkButton(btn_frame, text="Clear", height=35, font=("Inter", 13), command=self.clear_chat).grid(row=0, column=2, padx=1, sticky="ew")

        # On-screen keyboard
        self.osk_frame_ui = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        self.setup_button_osk()
        self.osk_visible = False
        self.kb_timer_id = None

        # --- DASHBOARD TAB ---
        self.dash_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.dash_frame.grid_columnconfigure(0, weight=1)
        self.dash_frame.grid_rowconfigure(0, weight=1)
        self.dash_text = ctk.CTkTextbox(self.dash_frame, state="disabled", wrap="word", font=("Ubuntu", 16))
        self.dash_text.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        ctk.CTkButton(self.dash_frame, text="Refresh Dashboard", height=40, font=my_font, command=self.refresh_dashboard).grid(row=1, column=0, pady=5)

        # --- SCHEDULE TAB ---
        self.sched_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.sched_frame.grid_columnconfigure(0, weight=1)
        self.sched_frame.grid_rowconfigure(0, weight=0)
        self.sched_frame.grid_rowconfigure(1, weight=1)
        self.sched_frame.grid_rowconfigure(2, weight=0)

        sched_form = ctk.CTkFrame(self.sched_frame, fg_color="transparent")
        sched_form.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        sched_form.grid_columnconfigure((1, 3, 5), weight=1)

        ctk.CTkLabel(sched_form, text="Title:", font=my_font).grid(row=0, column=0, sticky="w", padx=2)
        self.sched_title = ctk.CTkEntry(sched_form, font=my_font, height=35)
        self.sched_title.grid(row=0, column=1, columnspan=2, sticky="ew", padx=2)

        ctk.CTkLabel(sched_form, text="Date:", font=my_font).grid(row=1, column=0, sticky="w", padx=2)
        self.sched_date = ctk.CTkEntry(sched_form, font=my_font, height=35, placeholder_text="YYYY-MM-DD")
        self.sched_date.grid(row=1, column=1, sticky="ew", padx=2)
        # Set default date to tomorrow
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.sched_date.insert(0, tomorrow)

        ctk.CTkLabel(sched_form, text="Time:", font=my_font).grid(row=1, column=2, sticky="w", padx=2)
        self.sched_time = ctk.CTkEntry(sched_form, font=my_font, height=35, placeholder_text="HH:MM")
        self.sched_time.grid(row=1, column=3, sticky="ew", padx=2)
        self.sched_time.insert(0, "10:00")

        ctk.CTkLabel(sched_form, text="Desc:", font=my_font).grid(row=2, column=0, sticky="w", padx=2)
        self.sched_desc = ctk.CTkEntry(sched_form, font=my_font, height=35)
        self.sched_desc.grid(row=2, column=1, columnspan=3, sticky="ew", padx=2)

        ctk.CTkButton(sched_form, text="+ Add Meeting", height=35, font=my_font, command=self.add_meeting_from_ui).grid(row=3, column=0, columnspan=5, pady=5, sticky="ew")

        self.sched_list = ctk.CTkTextbox(self.sched_frame, state="disabled", wrap="word", font=("Ubuntu", 15))
        self.sched_list.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

        ctk.CTkButton(self.sched_frame, text="Refresh Schedule", height=35, font=my_font, command=self.refresh_schedule).grid(row=2, column=0, pady=5)

        # --- QUOTATIONS TAB ---
        self.quote_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.quote_frame.grid_columnconfigure(0, weight=1)
        self.quote_frame.grid_rowconfigure(0, weight=0)
        self.quote_frame.grid_rowconfigure(1, weight=1)
        self.quote_frame.grid_rowconfigure(2, weight=0)

        q_form = ctk.CTkFrame(self.quote_frame, fg_color="transparent")
        q_form.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        q_form.grid_columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(q_form, text="Client:", font=my_font).grid(row=0, column=0, sticky="w", padx=2)
        self.q_client = ctk.CTkEntry(q_form, font=my_font, height=35)
        self.q_client.grid(row=0, column=1, sticky="ew", padx=2)

        ctk.CTkLabel(q_form, text="Amount:", font=my_font).grid(row=0, column=2, sticky="w", padx=2)
        self.q_amount = ctk.CTkEntry(q_form, font=my_font, height=35, placeholder_text="0")
        self.q_amount.grid(row=0, column=3, sticky="ew", padx=2)

        ctk.CTkLabel(q_form, text="Due Date:", font=my_font).grid(row=1, column=0, sticky="w", padx=2)
        self.q_due = ctk.CTkEntry(q_form, font=my_font, height=35, placeholder_text="YYYY-MM-DD")
        self.q_due.grid(row=1, column=1, sticky="ew", padx=2)
        self.q_due.insert(0, (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"))

        ctk.CTkLabel(q_form, text="Desc:", font=my_font).grid(row=1, column=2, sticky="w", padx=2)
        self.q_desc = ctk.CTkEntry(q_form, font=my_font, height=35)
        self.q_desc.grid(row=1, column=3, sticky="ew", padx=2)

        ctk.CTkButton(q_form, text="+ Add Quotation", height=35, font=my_font, command=self.add_quotation_from_ui).grid(row=2, column=0, columnspan=5, pady=5, sticky="ew")

        self.quote_list = ctk.CTkTextbox(self.quote_frame, state="disabled", wrap="word", font=("Ubuntu", 15))
        self.quote_list.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

        ctk.CTkButton(self.quote_frame, text="Refresh Quotations", height=35, font=my_font, command=self.refresh_quotations).grid(row=2, column=0, pady=5)

        # --- PROJECTS TAB ---
        self.proj_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.proj_frame.grid_columnconfigure(0, weight=1)
        self.proj_frame.grid_rowconfigure(0, weight=0)
        self.proj_frame.grid_rowconfigure(1, weight=1)
        self.proj_frame.grid_rowconfigure(2, weight=0)

        p_form = ctk.CTkFrame(self.proj_frame, fg_color="transparent")
        p_form.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        p_form.grid_columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(p_form, text="Project:", font=my_font).grid(row=0, column=0, sticky="w", padx=2)
        self.p_name = ctk.CTkEntry(p_form, font=my_font, height=35)
        self.p_name.grid(row=0, column=1, sticky="ew", padx=2)

        ctk.CTkLabel(p_form, text="Deadline:", font=my_font).grid(row=0, column=2, sticky="w", padx=2)
        self.p_deadline = ctk.CTkEntry(p_form, font=my_font, height=35, placeholder_text="YYYY-MM-DD")
        self.p_deadline.grid(row=0, column=3, sticky="ew", padx=2)

        ctk.CTkLabel(p_form, text="Tasks (comma):", font=my_font).grid(row=1, column=0, sticky="w", padx=2)
        self.p_tasks = ctk.CTkEntry(p_form, font=my_font, height=35, placeholder_text="Task1, Task2, ...")
        self.p_tasks.grid(row=1, column=1, columnspan=3, sticky="ew", padx=2)

        ctk.CTkButton(p_form, text="+ Add Project", height=35, font=my_font, command=self.add_project_from_ui).grid(row=2, column=0, columnspan=5, pady=5, sticky="ew")

        self.proj_list = ctk.CTkTextbox(self.proj_frame, state="disabled", wrap="word", font=("Ubuntu", 15))
        self.proj_list.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

        ctk.CTkButton(self.proj_frame, text="Refresh Projects", height=35, font=my_font, command=self.refresh_projects).grid(row=2, column=0, pady=5)

        # --- DEBUG PANEL ---
        self.debug_frame = ctk.CTkFrame(self, height=120)
        self.debug_frame.grid(row=4, column=0, sticky="ew", padx=10, pady=5)
        self.debug_frame.grid_columnconfigure(0, weight=1)
        self.debug_box = ctk.CTkTextbox(self.debug_frame, state="disabled", wrap="word", font=("Inter", 12), text_color="#00ff00", fg_color="#121212", height=120)
        self.debug_box.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # Show chat by default
        self.switch_tab("chat")

    def setup_button_osk(self):
        self.keys = [['q','w','e','r','t','y','u','i','o','p'],
                     ['a','s','d','f','g','h','j','k','l'],
                     ['z','x','c','v','b','n','m','Back'],
                     ['Space','Clear']]
        btn_font = ("Inter", 16, "bold")
        self.osk_frame_ui.grid_columnconfigure(0, weight=1)
        for r, row in enumerate(self.keys):
            self.osk_frame_ui.grid_rowconfigure(r, weight=1)
            row_frame = ctk.CTkFrame(self.osk_frame_ui, fg_color="transparent")
            row_frame.grid(row=r, column=0, sticky="ew", pady=2)
            for c, key in enumerate(row):
                row_frame.grid_columnconfigure(c, weight=1)
                btn = ctk.CTkButton(row_frame, text=key, font=btn_font, height=45,
                                    command=lambda k=key: self.osk_press(k))
                btn.grid(row=0, column=c, padx=2, sticky="ew")

    def osk_press(self, key):
        cur = self.entry.get()
        if key == "Back":
            if len(cur) > 0:
                self.entry.delete(len(cur)-1, "end")
        elif key == "Clear":
            self.entry.delete(0, "end")
        elif key == "Send":
            self.send_message()
            return
        elif key == "Space":
            self.entry.insert(len(cur), " ")
        else:
            self.entry.insert(len(cur), key)
        self.reset_kb_timer()

    def toggle_keyboard(self):
        if self.osk_visible:
            self.hide_keyboard()
        else:
            self.show_keyboard()

    def show_keyboard(self):
        if not self.osk_visible:
            self.osk_frame_ui.grid(row=2, column=0, sticky="ew", padx=3, pady=3)
            self.osk_visible = True
            self.reset_kb_timer()

    def hide_keyboard(self):
        if self.osk_visible:
            self.osk_frame_ui.grid_forget()
            self.osk_visible = False
            if self.kb_timer_id:
                self.after_cancel(self.kb_timer_id)
                self.kb_timer_id = None

    def auto_show_keyboard(self, event=None):
        if not self.osk_visible:
            self.show_keyboard()

    def reset_kb_timer(self, event=None):
        if self.kb_timer_id:
            self.after_cancel(self.kb_timer_id)
        if self.osk_visible:
            self.kb_timer_id = self.after(60000, self.hide_keyboard)

    def toggle_language(self):
        self.lang_index = (self.lang_index + 1) % len(self.languages)
        l = self.languages[self.lang_index]
        self.lang_btn.configure(text=f"Lang: {l['label']}")
        self.append_to_chat("System", f"Language Switched: {l['label']}\n")

    # ======== TAB SWITCHING ========

    def switch_tab(self, tab):
        self.current_tab = tab
        for f in [self.chat_frame, self.dash_frame, self.sched_frame, self.quote_frame, self.proj_frame]:
            f.grid_forget()
        btns = [self.tab_chat_btn, self.tab_dash_btn, self.tab_sched_btn, self.tab_quote_btn, self.tab_proj_btn]
        for b in btns:
            b.configure(fg_color="#1F538D")
        targets = {
            "chat": (self.chat_frame, self.tab_chat_btn),
            "dashboard": (self.dash_frame, self.tab_dash_btn),
            "schedule": (self.sched_frame, self.tab_sched_btn),
            "quotations": (self.quote_frame, self.tab_quote_btn),
            "projects": (self.proj_frame, self.tab_proj_btn),
        }
        self.hide_keyboard()
        frame, btn = targets.get(tab, (self.chat_frame, self.tab_chat_btn))
        frame.grid(row=0, column=0, sticky="nsew")
        btn.configure(fg_color="#2B5797")
        if tab == "dashboard":
            self.refresh_dashboard()
        elif tab == "schedule":
            self.refresh_schedule()
        elif tab == "quotations":
            self.refresh_quotations()
        elif tab == "projects":
            self.refresh_projects()

    # ======== CHAT (CORE) ========

    def replace_emojis(self, text):
        if not text:
            return text
        text = "".join(c for c in text if ord(c) <= 0xFFFF)
        return text

    def append_to_chat(self, sender, message):
        message = self.replace_emojis(message)
        try:
            self.chat_box.configure(state="normal")
            if message:
                self.chat_box.insert("end", f"{sender}: {message}\n\n")
            else:
                self.chat_box.insert("end", f"{sender}: ")
            self.chat_box.configure(state="disabled")
            self.chat_box.yview("end")
        except Exception:
            pass

    def stream_append_token(self, token):
        token = self.replace_emojis(token)
        try:
            self.chat_box.configure(state="normal")
            self.chat_box.insert("end", token)
            self.chat_box.configure(state="disabled")
            self.chat_box.yview("end")
        except Exception:
            pass

    def clear_chat(self):
        try:
            self.chat_box.configure(state="normal")
            self.chat_box.delete("1.0", "end")
            self.chat_box.configure(state="disabled")
            self.log_debug("Chat cleared.")
        except Exception:
            pass

    def get_local_time_str(self):
        offset = time.timezone if (time.localtime().tm_isdst == 0) else time.altzone
        if offset == 0:
            local_dt = datetime.utcnow() + timedelta(hours=7)
        else:
            local_dt = datetime.now()
        return local_dt.strftime("%A, %B %d, %Y %I:%M %p")

    # ======== COMMAND PARSING ========

    def parse_command(self, prompt):
        lower = prompt.lower().strip()

        # --- SCHEDULE MEETING ---
        m = re.search(r"(?:schedule|add|create|set up)\s+(?:a\s+)?(?:meeting|appointment|schedule|call)\s+(?:with\s+)?(.+?)(?:\s+on\s+|\s+at\s+|\s+tomorrow|\s+next\s+\w+|\s*$)", lower)
        if m:
            title = m.group(1).strip()
            date_match = re.search(r"on\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", lower)
            time_match = re.search(r"at\s+(\d{1,2}):?(\d{2})\s*(am|pm)?", lower)
            tomorrow_match = "tomorrow" in lower
            if tomorrow_match:
                dt = datetime.now() + timedelta(days=1)
                date_str = dt.strftime("%Y-%m-%d")
            elif date_match:
                d = date_match.group(1)
                if "/" in d:
                    parts = d.split("/")
                    if len(parts) == 3:
                        date_str = f"{parts[2] if len(parts[2])==4 else '2026'}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                    else:
                        date_str = f"2026-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                else:
                    date_str = d
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")
            time_str = "10:00"
            if time_match:
                h = int(time_match.group(1))
                m_min = time_match.group(2)
                ampm = time_match.group(3)
                if ampm:
                    if ampm.lower() == "pm" and h < 12:
                        h += 12
                    elif ampm.lower() == "am" and h == 12:
                        h = 0
                time_str = f"{h:02d}:{m_min}"
            meeting = {
                "id": str(uuid.uuid4())[:8],
                "title": title.capitalize(),
                "date": date_str,
                "time": time_str,
                "description": "",
                "attendees": [],
                "status": "upcoming",
                "summary": ""
            }
            self.memory.setdefault("meetings", []).append(meeting)
            self.save_brain()
            reply = f"Meeting '{title.capitalize()}' scheduled on {date_str} at {time_str}."
            self.append_to_chat(self.character_name, reply)
            wav = self._generate_audio_file(reply)
            if wav:
                self.tts_queue.put(wav)
            self.log_debug(f"Meeting added: {title} on {date_str} at {time_str}")
            return True

        # --- ADD QUOTATION ---
        m = re.search(r"(?:add|create|new)\s+(?:a\s+)?(?:quotation|quote|quotation)\s+(?:for\s+|from\s+)?(.+?)(?:\s+for\s+|\s+of\s+|\s+worth\s+|\s+amount\s+|\s*$)", lower)
        if m and ("quotation" in lower or "quote" in lower):
            client = m.group(1).strip().title()
            amount_match = re.search(r"(?:for|of|worth|amount)\s*(?:rp|idr|\$|usd)?\s*([\d,]+\.?\d*)", lower)
            due_match = re.search(r"(?:due|deadline|by)\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", lower)
            amount = 0
            if amount_match:
                amount = float(amount_match.group(1).replace(",", ""))
            due_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
            if due_match:
                d = due_match.group(1)
                if "/" in d:
                    parts = d.split("/")
                    if len(parts) == 3:
                        due_date = f"{parts[2] if len(parts[2])==4 else '2026'}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                    else:
                        due_date = f"2026-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                else:
                    due_date = d
            quotation = {
                "id": str(uuid.uuid4())[:8],
                "client": client,
                "amount": amount,
                "currency": "IDR",
                "due_date": due_date,
                "description": "",
                "status": "pending"
            }
            self.memory.setdefault("quotations", []).append(quotation)
            self.save_brain()
            amount_str = f"Rp {amount:,.0f}" if amount else "an amount"
            reply = f"Quotation for {client} added for {amount_str}, due {due_date}."
            self.append_to_chat(self.character_name, reply)
            wav = self._generate_audio_file(reply)
            if wav:
                self.tts_queue.put(wav)
            self.log_debug(f"Quotation added: {client} - {amount}")
            return True

        # --- ADD PROJECT ---
        m = re.search(r"(?:add|create|new|start)\s+(?:a\s+)?(?:project|task)\s+(?:called\s+|named\s+)?(.+?)(?:\s+deadline\s+|\s+due\s+|\s*$)", lower)
        if m:
            name = m.group(1).strip().title()
            deadline_match = re.search(r"(?:deadline|due|by)\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", lower)
            deadline = ""
            if deadline_match:
                d = deadline_match.group(1)
                if "/" in d:
                    parts = d.split("/")
                    if len(parts) == 3:
                        deadline = f"{parts[2] if len(parts[2])==4 else '2026'}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                    else:
                        deadline = f"2026-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                else:
                    deadline = d
            project = {
                "id": str(uuid.uuid4())[:8],
                "name": name,
                "deadline": deadline,
                "status": "active",
                "tasks": [],
                "deadline_warned": False
            }
            self.memory.setdefault("projects", []).append(project)
            self.save_brain()
            dl_str = f" due {deadline}" if deadline else ""
            reply = f"Project '{name}' created{dl_str}."
            self.append_to_chat(self.character_name, reply)
            wav = self._generate_audio_file(reply)
            if wav:
                self.tts_queue.put(wav)
            self.log_debug(f"Project added: {name}")
            return True

        # --- LIST TODAY / SCHEDULE ---
        if lower in ["what's my schedule", "what is my schedule", "show my schedule", "list meetings", "my schedule", "schedule"]:
            self.show_schedule_in_chat()
            return True

        # --- LIST QUOTATIONS ---
        if lower in ["list quotations", "show quotations", "my quotations", "quotes", "quotation"]:
            self.show_quotations_in_chat()
            return True

        # --- LIST PROJECTS ---
        if lower in ["list projects", "show projects", "my projects", "projects"]:
            self.show_projects_in_chat()
            return True

        # --- SHOW DASHBOARD ---
        if lower in ["dashboard", "summary", "overview", "show dashboard"]:
            self.show_dashboard_in_chat()
            return True

        # --- MARK QUOTATION DONE ---
        m = re.search(r"(?:mark|set)\s+(?:quotation|quote)\s+(.+?)\s+(?:as\s+)?(done|completed|paid|cancelled)", lower)
        if m:
            client = m.group(1).strip().lower()
            new_status = m.group(2)
            status_map = {"done": "completed", "completed": "completed", "paid": "completed", "cancelled": "cancelled"}
            for q in self.memory.get("quotations", []):
                if q["client"].lower().startswith(client) or client in q["client"].lower():
                    q["status"] = status_map.get(new_status, "completed")
                    self.save_brain()
                    reply = f"Quotation for {q['client']} marked as {q['status']}."
                    self.append_to_chat(self.character_name, reply)
                    wav = self._generate_audio_file(reply)
                    if wav:
                        self.tts_queue.put(wav)
                    return True
            return False

        # --- MARK PROJECT COMPLETE ---
        m = re.search(r"(?:mark|set)\s+project\s+(.+?)\s+(?:as\s+)?(done|completed|finished|cancelled)", lower)
        if m:
            name = m.group(1).strip().lower()
            new_status = m.group(2)
            status_map = {"done": "completed", "completed": "completed", "finished": "completed", "cancelled": "cancelled"}
            for p in self.memory.get("projects", []):
                if p["name"].lower().startswith(name) or name in p["name"].lower():
                    p["status"] = status_map.get(new_status, "completed")
                    self.save_brain()
                    reply = f"Project '{p['name']}' marked as {p['status']}."
                    self.append_to_chat(self.character_name, reply)
                    return True
            return False

        return False

    def show_schedule_in_chat(self):
        meetings = self.memory.get("meetings", [])
        upcoming = [m for m in meetings if m.get("status") == "upcoming"]
        if not upcoming:
            self.append_to_chat(self.character_name, "No upcoming meetings.")
            return
        lines = ["Here's your schedule:"]
        for m in sorted(upcoming, key=lambda x: x.get("date", "") + x.get("time", "")):
            lines.append(f"  - {m['date']} at {m['time']}: {m['title']}")
        self.append_to_chat(self.character_name, "\n".join(lines))

    def show_quotations_in_chat(self):
        quotations = self.memory.get("quotations", [])
        pending = [q for q in quotations if q.get("status") == "pending"]
        if not pending:
            self.append_to_chat(self.character_name, "No pending quotations.")
            return
        lines = ["Pending quotations:"]
        for q in pending:
            amt = f"Rp {q['amount']:,.0f}" if q.get("amount") else "No amount"
            lines.append(f"  - {q['client']}: {amt}, due {q['due_date']}")
        self.append_to_chat(self.character_name, "\n".join(lines))

    def show_projects_in_chat(self):
        projects = self.memory.get("projects", [])
        active = [p for p in projects if p.get("status") == "active"]
        if not active:
            self.append_to_chat(self.character_name, "No active projects.")
            return
        lines = ["Active projects:"]
        for p in active:
            dl = f", deadline: {p['deadline']}" if p.get("deadline") else ""
            lines.append(f"  - {p['name']}{dl}")
        self.append_to_chat(self.character_name, "\n".join(lines))

    def show_dashboard_in_chat(self):
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        meetings = self.memory.get("meetings", [])
        quotations = self.memory.get("quotations", [])
        projects = self.memory.get("projects", [])

        today_meetings = [m for m in meetings if m.get("date") == today_str and m.get("status") == "upcoming"]
        pending_quotes = [q for q in quotations if q.get("status") == "pending"]
        active_projects = [p for p in projects if p.get("status") == "active"]

        lines = [f"Good {self.character_name}! Here's your overview:"]
        lines.append(f"\nToday ({today_str}):")
        if today_meetings:
            for m in today_meetings:
                lines.append(f"  - {m['time']}: {m['title']}")
        else:
            lines.append("  No meetings today")

        if pending_quotes:
            lines.append(f"\nPending quotations ({len(pending_quotes)}):")
            for q in pending_quotes:
                amt = f"Rp {q['amount']:,.0f}" if q.get("amount") else "No amount"
                lines.append(f"  - {q['client']}: {amt} (due {q['due_date']})")

        if active_projects:
            lines.append(f"\nActive projects ({len(active_projects)}):")
            for p in active_projects:
                dl = f" deadline: {p['deadline']}" if p.get("deadline") else ""
                lines.append(f"  - {p['name']}{dl}")

        self.append_to_chat(self.character_name, "\n".join(lines))

    # ======== SEND MESSAGE ========

    def send_message(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.append_to_chat("You", text)
        self.set_ai_status("Processing...", "#00BFFF")
        self.hide_keyboard()

        if self.current_tab != "chat":
            self.switch_tab("chat")

        threading.Thread(target=self.generate_response, args=(text,), daemon=True).start()

    # ======== RESPONSE GENERATION ========

    def generate_response(self, prompt):
        lower_prompt = prompt.lower()
        self.log_debug(f">> User said: {prompt[:80]}...")

        # Check reminders
        remind_match = re.search(r"remind me in (\d+)\s*minutes?\s*(?:to\s+)?(.*)", lower_prompt)
        if remind_match:
            try:
                mins = int(remind_match.group(1))
                memo = remind_match.group(2).strip() or "Timer finished!"
                remind_time = time.time() + (mins * 60)
                self.memory.setdefault("tasks", []).append({"time": remind_time, "memo": memo})
                self.save_brain()
                reply = f"Okay, I will remind you to '{memo}' in {mins} minutes."
                self.after(0, self.append_to_chat, self.character_name, reply)
                self.log_debug(f"Task set: {mins}m for '{memo}'")
                wav = self._generate_audio_file(reply)
                if wav:
                    self.tts_queue.put(wav)
                self.after(0, self.set_ai_status, "Idle", "#AAAAAA")
                return
            except Exception as e:
                self.log_debug(f"Reminder logic error: {e}")

        # Remember facts
        if lower_prompt.startswith("remember that ") or lower_prompt.startswith("remember "):
            fact = lower_prompt.replace("remember that ", "", 1).replace("remember ", "", 1)
            self.memory.setdefault("facts", []).append(fact)
            self.save_brain()
            self.log_debug(f"Brain saved: {fact}")

        # Check for assistant commands first
        self.after(0, self.append_to_chat, self.character_name, "")

        # Try to parse as a command
        if self.parse_command(prompt):
            self.after(0, self.set_ai_status, "Idle", "#AAAAAA")
            return

        # LLM generation
        facts = self.memory.get("facts", [])
        internet_info = ""
        if any(kw in lower_prompt for kw in ["search", "find", "look up", "what is", "who is", "how to", "explain", "info", "about", "weather", "news"]):
            if self.is_online:
                self.log_debug("Searching Wikipedia...")
                self.after(0, self.set_ai_status, "Searching Web...", "#FFD700")
                internet_info = self.search_internet(prompt)

        current_time_str = self.get_local_time_str()
        sys_prompt = f"You are {self.character_name}, a helpful personal assistant. You manage schedules, quotations, projects, and reminders for the user. Answer conversationally. Current time: {current_time_str}."

        mem_str = "\n".join(facts)
        if mem_str:
            sys_prompt += f"\nMemories:\n{mem_str}"

        # Add context about upcoming events
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_meetings = [m for m in self.memory.get("meetings", []) if m.get("date") == today_str and m.get("status") == "upcoming"]
        if today_meetings:
            sys_prompt += "\nToday's meetings:\n"
            for m in today_meetings:
                sys_prompt += f"- {m['time']}: {m['title']}\n"

        full_prompt = f"<|im_start|>system\n{sys_prompt}<|im_end|>\n<|im_start|>user\nContext: {internet_info}\nQuestion: {prompt}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n"
        try:
            self.log_debug("LLM generating tokens...")
            t_start = time.time()
            stream = self.llm(full_prompt, max_tokens=2048, stop=["<|im_end|>", "<|im_start|>"], temperature=0.6, repeat_penalty=1.1, stream=True)

            token_count = 0
            in_thinking = False
            token_buffer = ""
            answer_buffer = ""

            for output in stream:
                token = output['choices'][0]['text']
                token_buffer += token
                token_count += 1

                if not in_thinking:
                    if "<think>" in token_buffer:
                        parts = token_buffer.split("<think>", 1)
                        if parts[0]:
                            answer_buffer += parts[0]
                            self.after(0, self.stream_append_token, parts[0])
                        in_thinking = True
                        token_buffer = parts[1]
                    elif not any(token_buffer.endswith(p) for p in ["<", "<t", "<th", "<thi", "<thin", "<think"]):
                        answer_buffer += token_buffer
                        self.after(0, self.stream_append_token, token_buffer)
                        token_buffer = ""
                else:
                    if "</think>" in token_buffer:
                        parts = token_buffer.split("</think>", 1)
                        in_thinking = False
                        token_buffer = parts[1]
                    else:
                        token_buffer = token_buffer[-100:]

            if token_buffer:
                if in_thinking and "</think>" in token_buffer:
                    parts = token_buffer.split("</think>", 1)
                    token_buffer = parts[1] if len(parts) > 1 else ""
                    in_thinking = False
                if not in_thinking and token_buffer:
                    answer_buffer += token_buffer
                    self.after(0, self.stream_append_token, token_buffer)

            self.after(0, self.stream_append_token, "\n\n")

            if answer_buffer.strip():
                wav = self._generate_audio_file(answer_buffer.strip()[:500])
                if wav:
                    self.tts_queue.put(wav)

            elapsed = time.time() - t_start
            tps = token_count / elapsed if elapsed > 0 else 0
            self.log_debug(f"Done: {token_count} tokens in {elapsed:.1f}s ({tps:.1f} tok/s)")
        except Exception as e:
            self.log_debug(f"LLM fail: {e}")
            self.after(0, self.stream_append_token, f"[Error: {e}]\n\n")
        finally:
            self.after(0, self.set_ai_status, "Idle", "#AAAAAA")

    # ======== MEETING MANAGEMENT ========

    def add_meeting_from_ui(self):
        title = self.sched_title.get().strip()
        date_str = self.sched_date.get().strip()
        time_str = self.sched_time.get().strip()
        desc = self.sched_desc.get().strip()
        if not title or not date_str:
            self.log_debug("Meeting form incomplete")
            return
        meeting = {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "date": date_str,
            "time": time_str if time_str else "10:00",
            "description": desc,
            "attendees": [],
            "status": "upcoming",
            "summary": ""
        }
        self.memory.setdefault("meetings", []).append(meeting)
        self.save_brain()
        self.sched_title.delete(0, "end")
        self.sched_desc.delete(0, "end")
        self.refresh_schedule()
        self.log_debug(f"Meeting added via UI: {title}")

    def refresh_schedule(self):
        meetings = self.memory.get("meetings", [])
        self.sched_list.configure(state="normal")
        self.sched_list.delete("1.0", "end")
        if not meetings:
            self.sched_list.insert("end", "No meetings scheduled.\n")
        else:
            upcoming = [m for m in meetings if m.get("status") == "upcoming"]
            past = [m for m in meetings if m.get("status") != "upcoming"]
            if upcoming:
                self.sched_list.insert("end", "=== UPCOMING ===\n\n", "bold")
                for m in sorted(upcoming, key=lambda x: x.get("date", "") + x.get("time", "")):
                    self.sched_list.insert("end", f"{m['date']} {m['time']} - {m['title']}\n")
                    if m.get("description"):
                        self.sched_list.insert("end", f"  {m['description']}\n")
                    self.sched_list.insert("end", "\n")
            if past:
                self.sched_list.insert("end", "=== PAST ===\n\n", "bold")
                for m in sorted(past, key=lambda x: x.get("date", "") + x.get("time", ""), reverse=True)[:5]:
                    self.sched_list.insert("end", f"{m['date']} {m['time']} - {m['title']} [{m.get('status', 'past')}]\n")
                    if m.get("summary"):
                        self.sched_list.insert("end", f"  Summary: {m['summary'][:100]}...\n")
                    self.sched_list.insert("end", "\n")
        self.sched_list.configure(state="disabled")

    # ======== QUOTATION MANAGEMENT ========

    def add_quotation_from_ui(self):
        client = self.q_client.get().strip()
        amount_str = self.q_amount.get().strip()
        due_date = self.q_due.get().strip()
        desc = self.q_desc.get().strip()
        if not client:
            self.log_debug("Quotation form incomplete")
            return
        amount = float(amount_str) if amount_str else 0
        quotation = {
            "id": str(uuid.uuid4())[:8],
            "client": client,
            "amount": amount,
            "currency": "IDR",
            "due_date": due_date if due_date else (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "description": desc,
            "status": "pending"
        }
        self.memory.setdefault("quotations", []).append(quotation)
        self.save_brain()
        self.q_client.delete(0, "end")
        self.q_amount.delete(0, "end")
        self.q_desc.delete(0, "end")
        self.refresh_quotations()
        self.log_debug(f"Quotation added via UI: {client}")

    def refresh_quotations(self):
        quotations = self.memory.get("quotations", [])
        self.quote_list.configure(state="normal")
        self.quote_list.delete("1.0", "end")
        if not quotations:
            self.quote_list.insert("end", "No quotations.\n")
        else:
            pending = [q for q in quotations if q.get("status") == "pending"]
            completed = [q for q in quotations if q.get("status") != "pending"]
            if pending:
                self.quote_list.insert("end", "=== PENDING ===\n\n")
                for q in sorted(pending, key=lambda x: x.get("due_date", "")):
                    amt = f"Rp {q['amount']:,.0f}" if q.get("amount") else "No amount"
                    self.quote_list.insert("end", f"{q['client']} - {amt}\n  Due: {q['due_date']}\n")
                    if q.get("description"):
                        self.quote_list.insert("end", f"  {q['description']}\n")
                    self.quote_list.insert("end", "\n")
            if completed:
                self.quote_list.insert("end", "=== COMPLETED ===\n\n")
                for q in completed[:5]:
                    amt = f"Rp {q['amount']:,.0f}" if q.get("amount") else "No amount"
                    self.quote_list.insert("end", f"{q['client']} - {amt} [{q['status']}]\n")
        self.quote_list.configure(state="disabled")

    # ======== PROJECT MANAGEMENT ========

    def add_project_from_ui(self):
        name = self.p_name.get().strip()
        deadline = self.p_deadline.get().strip()
        tasks_str = self.p_tasks.get().strip()
        if not name:
            self.log_debug("Project form incomplete")
            return
        tasks = []
        if tasks_str:
            for t in tasks_str.split(","):
                t = t.strip()
                if t:
                    tasks.append({"name": t, "deadline": "", "done": False})
        project = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "deadline": deadline,
            "status": "active",
            "tasks": tasks,
            "deadline_warned": False
        }
        self.memory.setdefault("projects", []).append(project)
        self.save_brain()
        self.p_name.delete(0, "end")
        self.p_tasks.delete(0, "end")
        self.refresh_projects()
        self.log_debug(f"Project added via UI: {name}")

    def refresh_projects(self):
        projects = self.memory.get("projects", [])
        self.proj_list.configure(state="normal")
        self.proj_list.delete("1.0", "end")
        if not projects:
            self.proj_list.insert("end", "No projects.\n")
        else:
            active = [p for p in projects if p.get("status") == "active"]
            completed = [p for p in projects if p.get("status") != "active"]
            if active:
                self.proj_list.insert("end", "=== ACTIVE ===\n\n")
                for p in active:
                    dl = f" (deadline: {p['deadline']})" if p.get("deadline") else ""
                    self.proj_list.insert("end", f"{p['name']}{dl}\n")
                    for t in p.get("tasks", []):
                        chk = "✓" if t.get("done") else "○"
                        self.proj_list.insert("end", f"  {chk} {t['name']}\n")
                    self.proj_list.insert("end", "\n")
            if completed:
                self.proj_list.insert("end", "=== COMPLETED ===\n\n")
                for p in completed[:5]:
                    self.proj_list.insert("end", f"{p['name']} [{p['status']}]\n")
        self.proj_list.configure(state="disabled")

    # ======== DASHBOARD ========

    def refresh_dashboard(self):
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        meetings = self.memory.get("meetings", [])
        quotations = self.memory.get("quotations", [])
        projects = self.memory.get("projects", [])

        self.dash_text.configure(state="normal")
        self.dash_text.delete("1.0", "end")

        self.dash_text.insert("end", f"=== DASHBOARD ===\n")
        self.dash_text.insert("end", f"{self.get_local_time_str()}\n\n")

        # Meetings
        today_meetings = [m for m in meetings if m.get("date") == today_str and m.get("status") == "upcoming"]
        upcoming = [m for m in meetings if m.get("status") == "upcoming"]
        self.dash_text.insert("end", f"Today's Meetings: {len(today_meetings)}\n")
        if today_meetings:
            for m in today_meetings:
                self.dash_text.insert("end", f"  {m['time']} - {m['title']}\n")
        self.dash_text.insert("end", f"Upcoming: {len(upcoming)}\n\n")

        # Quotations
        pending_q = [q for q in quotations if q.get("status") == "pending"]
        self.dash_text.insert("end", f"Pending Quotations: {len(pending_q)}\n")
        urgent_q = [q for q in pending_q if q.get("due_date", "").startswith(today_str[:7])]
        if urgent_q:
            self.dash_text.insert("end", "  DUE THIS MONTH:\n")
            for q in urgent_q:
                amt = f"Rp {q['amount']:,.0f}" if q.get("amount") else "?"
                self.dash_text.insert("end", f"  - {q['client']}: {amt} (due {q['due_date']})\n")
        self.dash_text.insert("end", "\n")

        # Projects
        active_p = [p for p in projects if p.get("status") == "active"]
        self.dash_text.insert("end", f"Active Projects: {len(active_p)}\n")
        for p in active_p:
            dl = f" (deadline: {p['deadline']})" if p.get("deadline") else ""
            done_tasks = sum(1 for t in p.get("tasks", []) if t.get("done"))
            total_tasks = len(p.get("tasks", []))
            task_str = f" [{done_tasks}/{total_tasks} tasks]" if total_tasks else ""
            self.dash_text.insert("end", f"  - {p['name']}{dl}{task_str}\n")

        self.dash_text.configure(state="disabled")

    # ======== VOICE ========

    def on_mic_press(self, event):
        if self.is_recording:
            return
        self.is_recording = True
        self.voice_btn.configure(text="REC", fg_color="red", hover_color="#CC0000")
        self.set_ai_status("Recording...", "#FF0000")
        self.mic_wav_file = f"/tmp/voice_{time.time()}.wav"
        self.parecord_process = subprocess.Popen(["parecord", "--format=s16le", "--rate=16000", "--channels=1", self.mic_wav_file], stderr=subprocess.DEVNULL)
        self.log_debug("Started recording (Push-to-talk)")

    def on_mic_release(self, event):
        if not self.is_recording:
            return
        self.is_recording = False
        self.voice_btn.configure(text="Mic", fg_color="#1F6AA5", hover_color="#144870")
        self.set_ai_status("Processing audio...", "#FF4500")
        if self.parecord_process:
            self.parecord_process.terminate()
            self.parecord_process.wait()
            self.parecord_process = None
        threading.Thread(target=self.process_audio_file, args=(self.mic_wav_file,), daemon=True).start()

    def process_audio_file(self, wav_file):
        if not os.path.exists(wav_file):
            self.log_debug("WAV file not found!")
            self.set_ai_status("Idle", "#AAAAAA")
            return
        try:
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_file) as s:
                audio = recognizer.record(s)
            self.after(0, self.set_ai_status, "Transcribing...", "#00BFFF")
            text = ""
            if self.is_online:
                try:
                    text = recognizer.recognize_google(audio, language=self.languages[self.lang_index]['stt'])
                except Exception as g_e:
                    self.log_debug(f"Google STT fail, offline fallback: {g_e}")
                    text = self.recognize_offline(audio)
            else:
                text = self.recognize_offline(audio)
            if text:
                self.after(0, lambda: self.entry.insert(0, text))
                self.after(0, self.send_message)
            else:
                self.after(0, self.append_to_chat, "System", "[Voice could not be recognized]")
        except Exception as e:
            self.log_debug(f"Mic process error: {e}")
        finally:
            self.after(0, self.set_ai_status, "Idle", "#AAAAAA")
            try:
                os.remove(wav_file)
            except Exception:
                pass

    def recognize_offline(self, audio_data):
        try:
            from vosk import Model, KaldiRecognizer
        except Exception as e:
            self.log_debug(f"Offline STT load failed: {e}")
            return ""
        l = self.languages[self.lang_index]
        path = f"{self.base_dir}/models/stt/{l['vosk_model']}"
        if not os.path.exists(path):
            return ""
        try:
            if path not in self.stt_models:
                self.stt_models[path] = Model(path)
            rec = KaldiRecognizer(self.stt_models[path], 16000)
            if rec.AcceptWaveform(audio_data.get_raw_data(convert_rate=16000, convert_width=2)):
                return json.loads(rec.Result())["text"]
            return json.loads(rec.FinalResult())["text"]
        except Exception:
            return ""

    # ======== TTS ========

    def tts_worker(self):
        while True:
            wav_file = self.tts_queue.get()
            if wav_file and os.path.exists(wav_file):
                try:
                    subprocess.run(f"paplay {wav_file} || aplay {wav_file}", shell=True, stderr=subprocess.DEVNULL)
                    os.remove(wav_file)
                except Exception as e:
                    self.log_debug(f"TTS playback error: {e}")
            self.tts_queue.task_done()

    def _generate_audio_file(self, text):
        l = self.languages[self.lang_index]
        path = f"{self.base_dir}/models/{l['tts']}"
        if not os.path.exists(path):
            path = f"{self.base_dir}/models/en_US-amy-medium.onnx"
        try:
            audio_file = f"/tmp/res_{time.time()}.wav"
            p = subprocess.Popen([self.piper_executable, "--model", path, "--length_scale", "0.85", "--sentence_silence", "0.1", "--output_file", audio_file], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.communicate(input=text.encode('utf-8'))
            if os.path.exists(audio_file):
                return audio_file
        except Exception as e:
            self.log_debug(f"TTS gen error: {e}")
        return None

    # ======== EXIT ========

    def exit_app(self):
        self.reminder_check_active = False
        try:
            sys.stdout.close()
        except Exception:
            pass
        self.destroy()
        os._exit(0)

if __name__ == "__main__":
    LocalAIApp().mainloop()
