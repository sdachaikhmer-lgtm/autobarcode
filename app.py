import os
import re
import datetime
import csv
import shutil
import json
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance
import requests
from io import BytesIO
import concurrent.futures
import time
import threading
import subprocess
import sys
import hashlib
import mimetypes
import gspread

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/drive']

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")
CHIP_ON = "#2fa866"
CHIP_OFF = ("gray55", "gray45")
MODE_CONTAINER_COLOR = "#e08a1e"
MODE_VERIFY_COLOR = "#2fa866"

duplicate_lock = threading.Lock()
gdrive_lock = threading.Lock()
CONFIG_FILE = "app_config.json"
INDEX_FILE = "gdrive_index.json"  # Persistent Local Index Map for Drive File IDs
TARGET_PARENT_FOLDER_ID = "1sTeOcK79ytlePV0zF84zFKA2Q52t82pT"
GDRIVE_SUBFOLDERS = ["BarCodeAndStamp", "Container List", "Container Match Format (VGM)", "Main", "Part", "Report"]
# Local top-level folder name (lower-case) -> Drive subfolder it belongs to, when the names differ
GDRIVE_FOLDER_ALIASES = {"container match format": "container match format (vgm)"}
SYNC_WORKERS = 8
SYNC_INTERVAL_MIN, SYNC_INTERVAL_MAX = 1, 600
SYNC_INTERVAL_FINE_MAX = 30  # up to here the stepper moves 1 s at a time, above it 5 s at a time
RECONCILE_SECONDS = 600  # how often Drive is re-listed to repair files deleted/changed there

def save_config(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception:
        pass

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def load_gdrive_index():
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_gdrive_index(index_data):
    try:
        tmp_path = INDEX_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(index_data, f, indent=2)
        os.replace(tmp_path, INDEX_FILE)
    except Exception:
        pass

def draw_wrapped_text(draw, text, font, max_width):
    lines = []
    words = text.split(' ')
    current_line = ""
    for word in words:
        bbox = draw.textbbox((0, 0), word, font=font)
        if (bbox[2] - bbox[0]) > max_width:
            if current_line: lines.append(current_line); current_line = ""
            sub_word = ""
            for char in word:
                test_sub = sub_word + char
                if (draw.textbbox((0, 0), test_sub, font=font)[2] - draw.textbbox((0, 0), test_sub, font=font)[0]) <= max_width:
                    sub_word = test_sub
                else: lines.append(sub_word); sub_word = char
            if sub_word: current_line = sub_word
            continue
        test_line = current_line + " " + word if current_line else word
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_width: current_line = test_line
        else:
            if current_line: lines.append(current_line)
            current_line = word
    if current_line: lines.append(current_line)
    return lines

def is_file_ready(file_path):
    if not os.path.exists(file_path):
        return False
    try:
        size1 = os.path.getsize(file_path)
        if size1 <= 0:
            return False
        time.sleep(0.1)
        size2 = os.path.getsize(file_path)
        if size1 != size2:
            return False
        with open(file_path, 'ab'):
            pass
        return True
    except Exception:
        return True

def log_error_to_file(error_msg):
    try:
        with open("error_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}\n")
    except Exception:
        pass

def get_today_active_date_folder():
    today_str = datetime.datetime.now().strftime("%d-%b-%Y")
    return rf"D:\Scan\Main OutputFastOCR\OutputFastOCR_{today_str}"

def process_single_file(file_name, source_dir, output_dir, fixed_img, copy_only=False, seen_containers=None, first_seen_files=None, verify_mode=False, register_numbers=None):
    source_file_path = os.path.join(source_dir, file_name)
    output_file_path = os.path.join(output_dir, file_name)
    
    for _ in range(10):
        if is_file_ready(source_file_path):
            break
        time.sleep(0.3)
    else:
        return ("ignored", None)

    is_image = file_name.lower().endswith(('.jpg', '.jpeg', '.png'))
    if not is_image:
        try:
            if os.path.exists(source_file_path):
                if copy_only:
                    shutil.copy2(source_file_path, output_file_path)
                else:
                    shutil.move(source_file_path, output_file_path)
                return ("processed", f"Passed Non-Image File: {file_name}")
        except Exception as e:
            err_str = f"Error passing file {file_name}: {e}"
            log_error_to_file(err_str)
            return ("error", err_str)
        return ("ignored", None)

    def force_move_to_error():
        error_folder = os.path.join(output_dir, "Error_Files")
        os.makedirs(error_folder, exist_ok=True)
        error_path = os.path.join(error_folder, file_name)
        for _ in range(3):
            try:
                if os.path.exists(source_file_path):
                    shutil.move(source_file_path, error_path)
                    return True
            except Exception:
                try:
                    shutil.copy2(source_file_path, error_path)
                    if os.path.exists(source_file_path):
                        os.remove(source_file_path)
                    return True
                except Exception:
                    time.sleep(0.3)
        return False

    if "@" not in file_name:
        force_move_to_error()
        err_str = f"Moved Unidentified File to Error_Files (Missing '@'): {file_name}"
        log_error_to_file(err_str)
        return ("error", err_str)

    try:
        before_at, after_at = file_name.split("@", 1)
        containers_raw = after_at.strip().split('.')[0]
        containers = [c.strip() for c in containers_raw.split('-')]
        
        numeric_values = []
        for cont in containers:
            digits_only = "".join(filter(str.isdigit, cont))
            if digits_only:
                numeric_values.append(int(digits_only))

        container_unique_key = containers_raw.upper()

        if not numeric_values:
            force_move_to_error()
            err_str = f"Moved Unidentified File to Error_Files (No numbers): {file_name}"
            log_error_to_file(err_str)
            return ("error", err_str)

        if len(numeric_values) > 1:
            barcode_text = str(sum(numeric_values))
        else:
            barcode_text = str(numeric_values[0])

        if len(numeric_values) > 1:
            last_three = int(barcode_text[-3:]) if len(barcode_text) >= 3 else int(barcode_text)
            rotation_angle = float(last_three / 3.0)
        else:
            single_val_str = str(numeric_values[0])
            last_digit = int(single_val_str[-1])
            last_two_digits = int(single_val_str[-2:]) if len(single_val_str) >= 2 else int(single_val_str)
            
            if last_digit % 2 != 0:
                rotation_angle = float(last_two_digits + 190)
            else:
                rotation_angle = float(last_two_digits + 180)

        if rotation_angle > 360: 
            rotation_angle -= 360

        matched_main_filename = None
        registered_key = None  # duplicate-tracking key this call added, undone if we must retry later

        if verify_mode:
            match_e = re.search(r'E\s*(\d+)', file_name, re.IGNORECASE)
            incoming_reg = match_e.group(1) if match_e else None

            matched_reg = None
            if incoming_reg and register_numbers and incoming_reg in register_numbers:
                matched_reg = incoming_reg
                matched_main_filename = register_numbers[incoming_reg]
            else:
                all_file_numbers = re.findall(r'\d+', file_name)
                for num in all_file_numbers:
                    if register_numbers and num in register_numbers:
                        matched_reg = num
                        matched_main_filename = register_numbers[num]
                        break

            if not matched_reg or not matched_main_filename:
                mismatch_folder = os.path.join(output_dir, "Unverified_Registers")
                os.makedirs(mismatch_folder, exist_ok=True)
                mismatch_path = os.path.join(mismatch_folder, file_name)
                if copy_only:
                    shutil.copy2(source_file_path, mismatch_path)
                else:
                    shutil.move(source_file_path, mismatch_path)
                return ("unverified", f"Skipped (Verify Mismatch): Moved to Unverified_Registers | {file_name}")

            if seen_containers is not None and first_seen_files is not None:
                with duplicate_lock:
                    if matched_reg in seen_containers:
                        dup_folder = os.path.join(output_dir, "Duplicate_containers")
                        os.makedirs(dup_folder, exist_ok=True)
                        dup_file_path = os.path.join(dup_folder, file_name)
                        report_path = os.path.join(dup_folder, "duplicates_report.csv")
                        try:
                            file_exists = os.path.exists(report_path)
                            with open(report_path, mode='a', newline='', encoding='utf-8') as f:
                                writer = csv.writer(f)
                                if not file_exists: 
                                    writer.writerow(["Timestamp", "Duplicate File", "Register Number", "Original File"])
                                writer.writerow([
                                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                                    file_name, 
                                    matched_reg, 
                                    first_seen_files.get(matched_reg, "Unknown")
                                ])
                        except:
                            pass
                        if copy_only:
                            shutil.copy2(source_file_path, dup_file_path)
                        else:
                            shutil.move(source_file_path, dup_file_path)
                        return ("duplicate_container", f"Skipped Duplicate Register ({matched_reg}) | {file_name}")
                    else:
                        seen_containers.add(matched_reg)
                        registered_key = matched_reg
                        if matched_reg not in first_seen_files:
                            first_seen_files[matched_reg] = file_name

        else:
            if seen_containers is not None and first_seen_files is not None:
                with duplicate_lock:
                    if container_unique_key in seen_containers:
                        dup_folder = os.path.join(output_dir, "Duplicate_containers")
                        os.makedirs(dup_folder, exist_ok=True)
                        dup_file_path = os.path.join(dup_folder, file_name)
                        report_path = os.path.join(dup_folder, "duplicates_report.csv")
                        try:
                            file_exists = os.path.exists(report_path)
                            with open(report_path, mode='a', newline='', encoding='utf-8') as f:
                                writer = csv.writer(f)
                                if not file_exists: 
                                    writer.writerow(["Timestamp", "Duplicate File", "Container Identifier", "Original File"])
                                writer.writerow([
                                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
                                    file_name, 
                                    container_unique_key, 
                                    first_seen_files.get(container_unique_key, "Unknown")
                                ])
                        except:
                            pass
                        if copy_only:
                            shutil.copy2(source_file_path, dup_file_path)
                        else:
                            shutil.move(source_file_path, dup_file_path)
                        return ("duplicate_container", f"Skipped Duplicate Container ({container_unique_key}) | {file_name}")
                    else:
                        seen_containers.add(container_unique_key)
                        registered_key = container_unique_key
                        if container_unique_key not in first_seen_files:
                            first_seen_files[container_unique_key] = file_name

        output_file_path = os.path.join(output_dir, file_name)
        if os.path.exists(output_file_path):
            if not copy_only and os.path.exists(source_file_path):
                try:
                    os.remove(source_file_path)
                except:
                    pass
            return ("output_existing", None)

        with Image.open(source_file_path) as img:
            base_img = img.convert("RGBA").copy()
            
        base_width, base_height = base_img.size
        
        barcode_url = f"https://barcodeapi.org/api/code128/{requests.utils.quote(barcode_text)}"
        response = None
        for attempt in range(3):
            try:
                response = requests.get(barcode_url, timeout=10)
                if response.status_code == 200 or (400 <= response.status_code < 500 and response.status_code != 429):
                    break
            except requests.RequestException:
                response = None
            time.sleep(1 + attempt)

        if response is None or response.status_code == 429 or response.status_code >= 500:
            # Temporary network/API problem: leave the file where it is and try again later
            if registered_key is not None:
                with duplicate_lock:
                    seen_containers.discard(registered_key)
                    if first_seen_files.get(registered_key) == file_name:
                        first_seen_files.pop(registered_key, None)
            retry_msg = f"Barcode service unavailable, will retry: {file_name}"
            log_error_to_file(retry_msg)
            return ("retry", retry_msg)
        
        if response.status_code != 200:
            force_move_to_error()
            err_str = f"Error with {file_name}: API error code {response.status_code}"
            log_error_to_file(err_str)
            return ("error", err_str)

        barcode_img = Image.open(BytesIO(response.content)).convert("RGBA")
        barcode_img = barcode_img.resize((1000, 250))
        
        gray_barcode = ImageOps.grayscale(barcode_img)
        green_badge = Image.new("RGBA", barcode_img.size, (0, 150, 0, 255))
        barcode_img = Image.composite(green_badge, Image.new("RGBA", barcode_img.size, (255, 255, 255, 0)), ImageOps.invert(gray_barcode))

        spacing = 35  
        draw = ImageDraw.Draw(base_img)
        
        try:
            font = ImageFont.truetype("arial.ttf", 54)
        except IOError:
            font = ImageFont.load_default()

        if fixed_img:
            rotated_stamp = fixed_img.rotate(rotation_angle, expand=True, resample=Image.BICUBIC)
            fx_width, fx_height = rotated_stamp.size
            fx_center_x = (base_width - fx_width) // 2
            fx_center_y = (base_height - fx_height) // 2
            
            base_img.paste(rotated_stamp, (fx_center_x, fx_center_y), rotated_stamp)
            current_time_str = datetime.datetime.now().strftime("%A, %B %d, %Y at %H:%M:%S")
            
            time_bbox = draw.textbbox((0, 0), current_time_str, font=font)
            time_width = time_bbox[2] - time_bbox[0]
            time_height = time_bbox[3] - time_bbox[1]
            
            max_text_width = base_width - 200
            text_to_print = file_name
                
            filename_lines = draw_wrapped_text(draw, text_to_print, font, max_text_width)
            
            line_height = time_height + 10
            timestamp_y = fx_center_y - time_height - spacing
            
            bc_width, bc_height = barcode_img.size
            bc_x = (base_width - bc_width) // 2
            bc_y = fx_center_y + fx_height + spacing
            base_img.paste(barcode_img, (bc_x, bc_y), barcode_img)

            timestamp_bottom_y = int(base_height * 0.85)
            start_filename_y = timestamp_bottom_y + time_height + 35
            
            current_y = start_filename_y
            for line in filename_lines:
                line_bbox = draw.textbbox((0, 0), line, font=font)
                line_width = line_bbox[2] - line_bbox[0]
                line_x = (base_width - line_width) // 2
                draw.text((line_x, current_y), line, fill="red", font=font)
                current_y += line_height

            time_x = (base_width - time_width) // 2
            draw.text((time_x, timestamp_y), current_time_str, fill="red", font=font)

        base_img.convert("RGB").save(output_file_path, "JPEG", quality=90)
        base_img.close()
        
        if not copy_only and os.path.exists(source_file_path):
            try:
                archive_dir = os.path.join(source_dir, "Processed_Backup")
                os.makedirs(archive_dir, exist_ok=True)
                shutil.move(source_file_path, os.path.join(archive_dir, file_name))
            except:
                try:
                    os.remove(source_file_path)
                except:
                    pass
        
        action_label = "Copied & Processed" if copy_only else "Success & Moved"
        return ("processed", f"{action_label}: {file_name} | Barcode Sum: {barcode_text} | Angle: {rotation_angle:.1f}°")
        
    except Exception as e:
        force_move_to_error()
        err_str = f"Moved Stuck/Corrupted File to Error_Files: {file_name} | Reason: {e}"
        log_error_to_file(err_str)
        return ("error", err_str)

class LogBox(ctk.CTkTextbox):
    """Activity log that colours each line by what it says and can filter by kind."""
    TAG_COLORS = {"error": "#e5484d", "warn": "#d98e04", "upload": "#3b8ed0", "ok": "#2fa866"}
    MAX_LINES = 5000

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        for tag, color in self.TAG_COLORS.items():
            self.tag_config(tag, foreground=color)
        self.tag_config("info")

    @staticmethod
    def classify(text):
        low = text.lower()
        if "no errors" in low:
            return "info"
        if "[gdrive sync]" in low:
            return "upload" if "failed (will retry): 0" in low else "warn"
        if "error" in low or "failed" in low or "moved unidentified" in low or "moved stuck" in low:
            return "error"
        if "retry" in low or "skipped" in low or "duplicate" in low or "unavailable" in low:
            return "warn"
        if "success" in low or "copied" in low or "passed non-image" in low:
            return "ok"
        return "info"

    def insert(self, index, text, tags=None):
        if text.strip():
            text = datetime.datetime.now().strftime("[%H:%M:%S] ") + text
        super().insert(index, text, tags or self.classify(text))
        if int(self.index("end-1c").split(".")[0]) > self.MAX_LINES:
            self.delete("1.0", f"{self.MAX_LINES // 5}.0")

    def apply_filter(self, mode):
        visible = {"All": {"error", "warn", "upload", "ok", "info"}, "Errors": {"error", "warn"}, "Uploads": {"upload"}}[mode]
        for tag in ("error", "warn", "upload", "ok", "info"):
            self.tag_config(tag, elide=tag not in visible)


class BarcodeApp:
    def run_full_automated_sequence(self):
        def log_msg(msg):
            print(msg)
            self.ui_log(msg)

        log_msg("=== Starting Automated Sequence ===")
        try:
            log_msg("[Step 1/5] Running Auto_Create Folder...")
            self.auto_create_barcode_stamp_folder(silent=True)

            log_msg("[Step 2/5] Running Auto-Copy (list_of_container)...")
            source_dir = self.source_entry.get().strip()
            output_dir = self.output_entry.get().strip()
            if source_dir and output_dir and os.path.exists(source_dir):
                target_list_dir = os.path.join(output_dir, "list_of_container")
                os.makedirs(target_list_dir, exist_ok=True)
                for file_name in os.listdir(source_dir):
                    src_file_path = os.path.join(source_dir, file_name)
                    dest_file_path = os.path.join(target_list_dir, file_name)
                    if os.path.isfile(src_file_path) and not os.path.exists(dest_file_path):
                        if is_file_ready(src_file_path):
                            try:
                                shutil.copy2(src_file_path, dest_file_path)
                            except Exception:
                                pass

            log_msg("[Step 3/5] Running Auto-Create Gdrive Folder & Log IDs...")
            target_path = self.gdrive_src_entry.get().strip()
            parent_id = TARGET_PARENT_FOLDER_ID
            headers = self.get_oauth_headers()
            folder_base_name = os.path.basename(os.path.normpath(target_path))
            date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
            date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")
            c_name = f"CustomsDocs_{date_str}"
            
            gdrive_folder_id = self.get_or_create_folder_id(headers, parent_id, c_name)
            subfolder_ids_map = {}
            if gdrive_folder_id:
                for sub in ["BarCodeAndStamp", "Container List", "Container Match Format (VGM)", "Main", "Part", "Report"]:
                    sub_id = self.get_or_create_folder_id(headers, gdrive_folder_id, sub)
                    if sub_id:
                        subfolder_ids_map[sub] = sub_id
                self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, subfolder_ids_map)

            log_msg("[Step 4/5] Running Initial Sync CustomsDocs...")
            if gdrive_folder_id:
                self.sync_folder_by_id(target_path, gdrive_folder_id, subfolder_ids_map, headers)

            log_msg("[Step 5/5] Starting Auto-Watch & Process...")
            target_output_dir = self.output_entry.get().strip()
            if target_output_dir and os.path.exists(source_dir):
                os.makedirs(target_output_dir, exist_ok=True)
                os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
                fixed_img = self.load_stamp_image()
                all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                valid_files = [f for f in all_files if "@" in f]
                other_files = [f for f in all_files if "@" not in f]
                container_files = valid_files + other_files
                
                if container_files:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                        futures = [
                            executor.submit(process_single_file, file_name, source_dir, target_output_dir, fixed_img, copy_only=False, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set)
                            for file_name in container_files
                        ]
                        concurrent.futures.wait(futures)

            log_msg("=== Automated Sequence Completed Successfully ===")
        except Exception as e:
            err_str = f"ERROR during automated sequence: {e}"
            log_msg(err_str)
            log_error_to_file(err_str)

    def __init__(self, root):
        self.root = root
        self.root.title("Barcode & Stamp Control Center")
        self.root.geometry("1180x780")
        self.root.minsize(980, 640)

        self.is_watching = False
        self.is_copy_processing = False
        self.is_list_copy_looping = False
        self.is_gdrive_folder_sync_looping = False
        self.current_font_size = 12
        self.session_output_dir = ""
        self.is_log_expanded = False

        self.verify_mode_active = False
        self.register_numbers_set = {}

        self.seen_containers = set()
        self.first_seen_files = {}
        self.processed_source_files = set()

        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_files_move = 0
        self.count_errors = 0
        self.progress_total = 1

        self.config_data = load_config()
        self.gdrive_index_map = load_gdrive_index()  # Load persistent file ID cache
        self.index_lock = threading.Lock()
        self.index_dirty = False
        self.sync_lock = threading.Lock()  # one mirror pass at a time (startup sequence and loop would otherwise race)
        self.children_lock = threading.Lock()
        self.drive_children = {}  # folder id -> files already on Drive (listed once per session)
        self.verified_folder_ids = set()
        self.retry_after = {}  # local path -> earliest time to retry a failed upload
        self.gd_headers = {}
        self.gd_headers_time = 0.0
        self.gd_ctx = None
        self.file_retry_after = {}  # file name -> earliest time to retry after a temporary barcode failure
        self.last_upload_text = "no uploads yet"
        try:
            self.sync_interval = max(SYNC_INTERVAL_MIN, min(SYNC_INTERVAL_MAX, int(self.config_data.get("sync_interval", 10))))
        except (TypeError, ValueError):
            self.sync_interval = 10
        self.interval_repeat_job = None

        today_base_folder = get_today_active_date_folder()
        default_stamp_path = self.config_data.get("stamp_path", r"D:\barcodestame\new 2 stamp RB.png")

        self.pages = {}
        self.nav_buttons = {}
        self.switches = {}
        self.chips = {}
        self.stat_labels = {}

        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self.build_sidebar()

        content = ctk.CTkFrame(self.root, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=20, pady=18)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        self.pages["Dashboard"] = self.build_dashboard_page(content)
        self.pages["Paths"] = self.build_paths_page(content, today_base_folder, default_stamp_path)
        self.pages["Drive"] = self.build_drive_page(content, today_base_folder)
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
        self.show_page("Dashboard")

        self.update_output_file_count()
        self.current_base_folder = today_base_folder
        self.root.after(60000, self.roll_over_date_folders)

    # ---------------------------------------------------------------- UI building
    def build_sidebar(self):
        side = ctk.CTkFrame(self.root, width=210, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(side, text="Barcode & Stamp", font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, padx=20, pady=(26, 0), sticky="w")
        ctk.CTkLabel(side, text="Control Center", text_color=("gray40", "gray60")).grid(row=1, column=0, padx=20, pady=(0, 22), sticky="w")

        for i, name in enumerate(("Dashboard", "Paths", "Drive")):
            btn = ctk.CTkButton(side, text=name, anchor="w", height=40, corner_radius=8, fg_color="transparent",
                                text_color=("gray10", "gray90"), hover_color=("gray78", "gray28"),
                                command=lambda n=name: self.show_page(n))
            btn.grid(row=2 + i, column=0, padx=14, pady=3, sticky="ew")
            self.nav_buttons[name] = btn

        self.auto_sequence_btn = ctk.CTkButton(
            side, text="Run full 5-step automation", height=42, font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: threading.Thread(target=self.run_full_automated_sequence, daemon=True).start())
        self.auto_sequence_btn.grid(row=6, column=0, padx=14, pady=(0, 12), sticky="ew")

        self.theme_switch = ctk.CTkSegmentedButton(side, values=["Dark", "Light"], command=lambda v: ctk.set_appearance_mode(v))
        self.theme_switch.set("Dark")
        self.theme_switch.grid(row=7, column=0, padx=14, pady=(0, 20), sticky="ew")

    def show_page(self, name):
        self.pages[name].tkraise()
        for n, btn in self.nav_buttons.items():
            btn.configure(fg_color=("gray78", "gray25") if n == name else "transparent")

    def build_dashboard_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        self.top_area = ctk.CTkFrame(page, fg_color="transparent")
        self.top_area.grid(row=0, column=0, sticky="ew")
        self.top_area.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.top_area, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(header, text="Dashboard", font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        for key, text in (("list", "List copy"), ("drive", "Drive sync"), ("watch", "Watching")):
            chip = ctk.CTkLabel(header, text=f"●  {text}", text_color=CHIP_OFF, font=ctk.CTkFont(size=13, weight="bold"))
            chip.pack(side="right", padx=(16, 0))
            self.chips[key] = chip

        cards = ctk.CTkFrame(self.top_area, fg_color="transparent")
        cards.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for col, (key, title) in enumerate((("processed", "Processed"), ("stamped", "Already stamped"), ("errors", "Errors"), ("files", "Files in output"))):
            cards.grid_columnconfigure(col, weight=1, uniform="stat")
            card = ctk.CTkFrame(cards, corner_radius=12)
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 3 else 6))
            ctk.CTkLabel(card, text=title, text_color=("gray40", "gray60")).pack(anchor="w", padx=16, pady=(12, 0))
            value = ctk.CTkLabel(card, text="0", font=ctk.CTkFont(size=30, weight="bold"))
            value.pack(anchor="w", padx=16, pady=(0, 12))
            self.stat_labels[key] = value

        self.sync_status_lbl = ctk.CTkLabel(self.top_area, text="Drive sync: waiting for first pass", anchor="w", text_color=("gray35", "gray65"))
        self.sync_status_lbl.grid(row=2, column=0, sticky="ew", padx=4)
        self.progress_bar = ctk.CTkProgressBar(self.top_area, height=8)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=3, column=0, sticky="ew", pady=(6, 12))

        controls = ctk.CTkFrame(self.top_area, corner_radius=12)
        controls.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        controls.grid_columnconfigure((0, 1), weight=1)
        specs = (
            ("watch", "Auto-watch & process (moves source)", self.toggle_watch),
            ("copy", "Auto-copy & process (keeps source)", self.toggle_copy_processing),
            ("list", "Auto-copy list_of_container", self.toggle_list_copy_loop),
            ("drive", "Auto-sync CustomsDocs to Drive", self.toggle_gdrive_folder_sync_loop),
        )
        for i, (key, text, command) in enumerate(specs):
            switch = ctk.CTkSwitch(controls, text=text, command=command, font=ctk.CTkFont(size=13))
            switch.grid(row=i // 2, column=i % 2, sticky="w", padx=20, pady=(14 if i < 2 else 6, 6))
            self.switches[key] = switch

        mode_row = ctk.CTkFrame(controls, fg_color="transparent")
        mode_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(6, 14))
        self.mode_lbl = ctk.CTkLabel(mode_row, text="Mode: Container-Only", text_color=MODE_CONTAINER_COLOR, font=ctk.CTkFont(size=13, weight="bold"))
        self.mode_lbl.pack(side="left")
        self.mode_toggle_btn = ctk.CTkButton(mode_row, text="Switch to Verify Mode", width=200, command=self.toggle_processing_mode)
        self.mode_toggle_btn.pack(side="right")

        log_card = ctk.CTkFrame(page, corner_radius=12)
        log_card.grid(row=1, column=0, sticky="nsew")
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(log_card, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        ctk.CTkLabel(bar, text="Activity log", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.zoom_in_btn = ctk.CTkButton(bar, text="A+", width=36, command=self.zoom_in)
        self.zoom_out_btn = ctk.CTkButton(bar, text="A-", width=36, command=self.zoom_out)
        self.expand_log_btn = ctk.CTkButton(bar, text="Expand log", width=100, fg_color=("gray70", "gray30"), hover_color=("gray62", "gray38"), text_color=("gray10", "gray95"), command=self.toggle_expand_log)
        self.jump_error_btn = ctk.CTkButton(bar, text="Jump to error", width=110, fg_color="#c0392b", hover_color="#a93226", command=self.jump_to_error_log)
        self.reset_btn = ctk.CTkButton(bar, text="Clear", width=70, fg_color=("gray70", "gray30"), hover_color=("gray62", "gray38"), text_color=("gray10", "gray95"), command=self.reset_history)
        for widget in (self.zoom_in_btn, self.zoom_out_btn, self.expand_log_btn, self.jump_error_btn, self.reset_btn):
            widget.pack(side="right", padx=(6, 0))
        self.log_filter = ctk.CTkSegmentedButton(bar, values=["All", "Errors", "Uploads"], command=lambda mode: self.log_box.apply_filter(mode))
        self.log_filter.set("All")
        self.log_filter.pack(side="right", padx=(0, 14))

        self.log_box = LogBox(log_card, font=("Consolas", self.current_font_size), wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        return page

    def path_row(self, parent, title, initial, browse_command):
        ctk.CTkLabel(parent, text=title, anchor="w", font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=20, pady=(16, 3))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=20)
        entry = ctk.CTkEntry(row, height=36, font=("Segoe UI", self.current_font_size))
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.insert(0, initial)
        button = ctk.CTkButton(row, text="Browse", width=90, height=36, command=browse_command)
        button.pack(side="right")
        return entry, button

    def build_paths_page(self, parent, today_base_folder, default_stamp_path):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(page, text="Paths", font=ctk.CTkFont(size=26, weight="bold")).grid(row=0, column=0, sticky="w", pady=(0, 12))
        card = ctk.CTkFrame(page, corner_radius=12)
        card.grid(row=1, column=0, sticky="ew")
        self.verify_entry, self.btn_verify_src = self.path_row(card, "Verify source (Main folder with 'M' filenames)", os.path.join(today_base_folder, "Main"), self.select_verify_source)
        self.source_entry, self.btn_src = self.path_row(card, "Source folder (Container List)", os.path.join(today_base_folder, "Container List"), self.select_source)
        self.fixed_entry, self.btn_fx = self.path_row(card, "Stamp image (.png)", default_stamp_path, self.select_fixed_file)
        self.output_entry, self.btn_out = self.path_row(card, "Output directory", os.path.join(today_base_folder, "BarcodeandStamp"), self.select_output)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(18, 18))
        self.auto_create_out_btn = ctk.CTkButton(actions, text="Create output folder", command=lambda: self.auto_create_barcode_stamp_folder(silent=False))
        self.auto_create_out_btn.pack(side="left", padx=(0, 8))
        self.open_folder_btn = ctk.CTkButton(actions, text="Open output folder", fg_color=("gray70", "gray30"), hover_color=("gray62", "gray38"), text_color=("gray10", "gray95"), command=self.open_current_output_folder)
        self.open_folder_btn.pack(side="left")
        return page

    def build_drive_page(self, parent, today_base_folder):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(page, text="Google Drive", font=ctk.CTkFont(size=26, weight="bold")).grid(row=0, column=0, sticky="w", pady=(0, 12))
        card = ctk.CTkFrame(page, corner_radius=12)
        card.grid(row=1, column=0, sticky="ew")
        self.gdrive_src_entry, self.btn_gd_src = self.path_row(card, "Local date folder to mirror (switches to the new day automatically)", today_base_folder, self.select_gdrive_source)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(18, 6))
        self.auto_create_gdrive_btn = ctk.CTkButton(actions, text="Create Drive folders & log IDs", command=self.manual_create_gdrive_folders)
        self.auto_create_gdrive_btn.pack(side="left", padx=(0, 20))
        ctk.CTkLabel(actions, text="CSV re-check interval").pack(side="left", padx=(0, 8))
        minus_btn = ctk.CTkButton(actions, text="−", width=36, height=34, font=ctk.CTkFont(size=18, weight="bold"))
        self.sync_interval_lbl = ctk.CTkLabel(actions, text=f"{self.sync_interval} s", width=64, font=ctk.CTkFont(size=14, weight="bold"))
        plus_btn = ctk.CTkButton(actions, text="+", width=36, height=34, font=ctk.CTkFont(size=18, weight="bold"))
        for btn, delta in ((minus_btn, -1), (plus_btn, 1)):
            # click = one step, hold = keeps stepping (no typing needed)
            btn.bind("<ButtonPress-1>", lambda e, d=delta: self.start_interval_repeat(d))
            btn.bind("<ButtonRelease-1>", self.stop_interval_repeat)
        minus_btn.pack(side="left")
        self.sync_interval_lbl.pack(side="left")
        plus_btn.pack(side="left")

        ctk.CTkLabel(card, justify="left", anchor="w", wraplength=760, text_color=("gray35", "gray65"),
                     text="New images and files upload within about a second. CSV files (Report and others) are re-checked at the interval above "
                          "and re-uploaded only when they changed. Every 10 minutes Drive is re-listed to repair anything deleted or changed there."
                     ).pack(fill="x", padx=20, pady=(4, 18))
        return page

    # ---------------------------------------------------------------- UI helpers used by the logic
    def set_toggle_ui(self, key, on):
        """Reflect a running/stopped loop on its switch and header chip (safe to call from any thread)."""
        def apply():
            switch = self.switches.get(key)
            if switch:
                switch.select() if on else switch.deselect()
            chip = self.chips.get(key)
            if chip:
                chip.configure(text_color=CHIP_ON if on else CHIP_OFF)
        self.ui(apply)

    def set_progress(self, done, total=None):
        if total is not None:
            self.progress_total = max(total, 1)
        self.progress_bar.set(min(done / self.progress_total, 1.0))

    def roll_over_date_folders(self):
        """After midnight, move the date-based folder paths on to today's folder (custom paths are left alone)."""
        try:
            new_base = get_today_active_date_folder()
            old_base = self.current_base_folder
            if new_base != old_base:
                for entry in (self.verify_entry, self.source_entry, self.output_entry, self.gdrive_src_entry):
                    value = entry.get().strip()
                    if value.startswith(old_base):
                        entry.delete(0, tk.END)
                        entry.insert(0, new_base + value[len(old_base):])
                self.current_base_folder = new_base
                self.log_box.insert(tk.END, f"New day detected - folders switched to: {new_base}\n")
                self.log_box.see(tk.END)
                self.update_output_file_count()
        except Exception as e:
            log_error_to_file(f"Date rollover error: {e}")
        self.root.after(60000, self.roll_over_date_folders)

    def get_oauth_headers(self):
        creds = None
        token_path = "token.json"
        client_secrets_path = r"D:\barcodestame\credentials.json"
        
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        return {"Authorization": f"Bearer {creds.token}"}

    def save_current_paths(self):
        cfg = {
            "verify_path": self.verify_entry.get().strip(),
            "source_path": self.source_entry.get().strip(),
            "stamp_path": self.fixed_entry.get().strip(),
            "output_path": self.output_entry.get().strip(),
            "gdrive_src": self.gdrive_src_entry.get().strip(),
            "sync_interval": self.sync_interval
        }
        save_config(cfg)

    def select_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_entry.delete(0, tk.END)
            self.source_entry.insert(0, folder)
            self.save_current_paths()

    def select_verify_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.verify_entry.delete(0, tk.END)
            self.verify_entry.insert(0, folder)
            self.save_current_paths()

    def select_gdrive_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.gdrive_src_entry.delete(0, tk.END)
            self.gdrive_src_entry.insert(0, folder)
            self.save_current_paths()

    def select_fixed_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png;*.jpg;*.jpeg")])
        if file_path:
            self.fixed_entry.delete(0, tk.END)
            self.fixed_entry.insert(0, file_path)
            self.save_current_paths()

    def select_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, folder)
            self.save_current_paths()

    def update_output_file_count(self):
        out_dir = self.output_entry.get().strip()
        if out_dir and os.path.exists(out_dir):
            try:
                files_list = [f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))]
                self.count_files_move = len(files_list)
            except Exception:
                self.count_files_move = 0
        else:
            self.count_files_move = 0
        self.update_stats_display()

    def open_current_output_folder(self):
        target_dir = self.output_entry.get()
        if target_dir and os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
            subprocess.Popen(f'explorer "{os.path.abspath(target_dir)}"')
        else:
            messagebox.showinfo("Folder Notice", "The output folder path does not exist yet.")

    def toggle_processing_mode(self):
        if not self.verify_mode_active:
            self.toggle_processing_mode_silent()
            self.verify_mode_active = True
            self.mode_lbl.configure(text="Mode: Verify (Main 'M' register match active)", text_color=MODE_VERIFY_COLOR)
            self.mode_toggle_btn.configure(text="Switch to Container-Only Mode", fg_color="#c0392b", hover_color="#a93226")
            self.log_box.insert(tk.END, f"Verify Mode activated. Loaded {len(self.register_numbers_set)} register numbers from Main folder files containing 'M'.\n")
            self.log_box.see(tk.END)
        else:
            self.verify_mode_active = False
            self.register_numbers_set.clear()
            self.mode_lbl.configure(text="Mode: Container-Only", text_color=MODE_CONTAINER_COLOR)
            self.mode_toggle_btn.configure(text="Switch to Verify Mode", fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"], hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])
            self.log_box.insert(tk.END, "Switched back to Container-Only Mode.\n")
            self.log_box.see(tk.END)

    def toggle_processing_mode_silent(self):
        verify_path = self.verify_entry.get().strip()
        loaded_regs = {} 
        try:
            if verify_path and os.path.exists(verify_path):
                if os.path.isdir(verify_path):
                    for root_dir, _, files in os.walk(verify_path):
                        for file in files:
                            if 'M' in file.upper():
                                match_e = re.search(r'E\s*(\d+)', file, re.IGNORECASE)
                                if match_e:
                                    reg_num = match_e.group(1)
                                    loaded_regs[reg_num] = file
                elif os.path.isfile(verify_path):
                    if 'M' in verify_path.upper():
                        match_e = re.search(r'E\s*(\d+)', verify_path, re.IGNORECASE)
                        if match_e:
                            reg_num = match_e.group(1)
                            loaded_regs[reg_num] = os.path.basename(verify_path)
            self.register_numbers_set = loaded_regs
        except Exception:
            pass

    def toggle_list_copy_loop(self):
        if not self.is_list_copy_looping:
            self.is_list_copy_looping = True
            self.set_toggle_ui("list", True)
            self.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' started...\n")
            self.log_box.see(tk.END)
            threading.Thread(target=self.list_copy_loop_worker, daemon=True).start()
        else:
            self.is_list_copy_looping = False
            self.set_toggle_ui("list", False)
            self.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' stopped.\n")
            self.log_box.see(tk.END)

    def list_copy_loop_worker(self):
        while self.is_list_copy_looping:
            try:
                source_dir = self.source_entry.get().strip()
                output_dir = self.output_entry.get().strip()
                
                if source_dir and output_dir and os.path.exists(source_dir):
                    target_list_dir = os.path.join(output_dir, "list_of_container")
                    os.makedirs(target_list_dir, exist_ok=True)
                    files = os.listdir(source_dir)
                    for file_name in files:
                        if not self.is_list_copy_looping:
                            break
                        src_file_path = os.path.join(source_dir, file_name)
                        dest_file_path = os.path.join(target_list_dir, file_name)
                        
                        if os.path.isfile(src_file_path):
                            try:
                                dst_stat = os.stat(dest_file_path)
                                src_stat = os.stat(src_file_path)
                                if dst_stat.st_size == src_stat.st_size and int(dst_stat.st_mtime) == int(src_stat.st_mtime):
                                    continue  # already copied and unchanged
                            except OSError:
                                pass
                            if is_file_ready(src_file_path):
                                try:
                                    shutil.copy2(src_file_path, dest_file_path)
                                except Exception:
                                    pass
            except Exception:
                pass
            time.sleep(0.5)

    def drive_request(self, method, url, headers, extra_headers=None, retries=5, **kwargs):
        """Drive call with timeout, token refresh on 401 and backoff on rate-limit/5xx. None if the network keeps failing."""
        kwargs.setdefault("timeout", 60)
        res = None
        for attempt in range(retries):
            try:
                res = requests.request(method, url, headers={**headers, **(extra_headers or {})}, **kwargs)
            except requests.RequestException:
                res = None
                time.sleep(min(2 ** attempt, 20))
                continue
            if res.status_code == 401:
                try:
                    headers.update(self.get_oauth_headers())
                except Exception as e:
                    log_error_to_file(f"Token refresh failed: {e}")
                continue
            if res.status_code in (429, 500, 502, 503, 504) or (res.status_code == 403 and "ateLimit" in res.text):
                time.sleep(min(2 ** attempt, 30))
                continue
            return res
        return res

    def save_index(self):
        with self.index_lock:
            save_gdrive_index(dict(self.gdrive_index_map))

    def get_or_create_folder_id(self, headers, parent_id, folder_name):
        cache_key = f"folder_{parent_id}_{folder_name}"
        files_url = "https://www.googleapis.com/drive/v3/files"
        with gdrive_lock:
            cached_id = self.gdrive_index_map.get(cache_key)
            if cached_id and cached_id in self.verified_folder_ids:
                return cached_id
            if cached_id:
                test_res = self.drive_request("GET", f"{files_url}/{cached_id}", headers, params={"supportsAllDrives": True, "fields": "id,trashed"})
                if test_res is None or test_res.status_code not in (200, 404):
                    return None  # cannot verify right now; creating a copy here would duplicate the folder
                if test_res.status_code == 200 and not test_res.json().get("trashed", False):
                    self.verified_folder_ids.add(cached_id)
                    return cached_id

            safe_name = folder_name.replace("\\", "\\\\").replace("'", "\\'")
            params = {
                "q": f"'{parent_id}' in parents and name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                "pageSize": 5,
                "fields": "files(id, name)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True
            }
            res = self.drive_request("GET", files_url, headers, params=params)
            if res is None or res.status_code != 200:
                return None
            found = res.json().get("files", [])
            if found:
                fid = found[0]["id"]
            else:
                meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
                r_create = self.drive_request("POST", files_url, headers, json=meta, params={"supportsAllDrives": True})
                if r_create is None or r_create.status_code != 200:
                    log_error_to_file(f"API Error creating '{folder_name}' ({getattr(r_create, 'status_code', 'no response')}): {getattr(r_create, 'text', '')}")
                    return None
                fid = r_create.json().get("id")
            with self.index_lock:
                self.gdrive_index_map[cache_key] = fid
            self.verified_folder_ids.add(fid)
            self.save_index()
            return fid

    def log_folder_structure_to_sheet(self, date_label, sub_main_name, sub_main_id, subfolder_ids_map):
        token_path = "token.json"
        if not os.path.exists(token_path):
            return
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key("1EDogJjSd9qJd8Y8oUgPtkBPH2kKBbABRDm3raem-eUY")
            sheet = spreadsheet.get_worksheet(0)
            
            main_id = subfolder_ids_map.get("Main", "")
            part_id = subfolder_ids_map.get("Part", "")
            container_list_id = subfolder_ids_map.get("Container List", "")
            barcode_stamp_id = subfolder_ids_map.get("BarCodeAndStamp", "")
            report_id = subfolder_ids_map.get("Report", "")
            vgm_id = subfolder_ids_map.get("Container Match Format (VGM)", "")

            row_data = [
                date_label, 
                sub_main_name, 
                sub_main_id, 
                main_id, 
                part_id, 
                container_list_id, 
                vgm_id, 
                barcode_stamp_id, 
                report_id
            ]

            try:
                cell = sheet.find(sub_main_name)
                if cell:
                    sheet.update(range_name=f'A{cell.row}:I{cell.row}', values=[row_data])
                else:
                    sheet.append_row(row_data)
            except gspread.exceptions.CellNotFound:
                sheet.append_row(row_data)
        except Exception as e:
            log_error_to_file(f"Error logging folder IDs to Google Sheet: {e}")

    def manual_create_gdrive_folders(self):
        threading.Thread(target=self.create_gdrive_folders_worker, daemon=True).start()

    def create_gdrive_folders_worker(self):
        target_path = self.gdrive_src_entry.get().strip()
        parent_id = TARGET_PARENT_FOLDER_ID

        try:
            headers = self.get_oauth_headers()
            folder_base_name = os.path.basename(os.path.normpath(target_path))
            date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
            date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")

            c_name = f"CustomsDocs_{date_str}"
            subfolders_to_create = [
                "BarCodeAndStamp",
                "Container List",
                "Container Match Format (VGM)",
                "Main",
                "Part",
                "Report"
            ]
            
            gdrive_folder_id = self.get_or_create_folder_id(headers, parent_id, c_name)
            if gdrive_folder_id:
                subfolder_id_map = {}
                for sub in subfolders_to_create:
                    sub_id = self.get_or_create_folder_id(headers, gdrive_folder_id, sub)
                    if sub_id:
                        subfolder_id_map[sub] = sub_id

                self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, subfolder_id_map)
                self.sync_folder_by_id(target_path, gdrive_folder_id, subfolder_id_map, headers)
            else:
                log_error_to_file(f"-> ERROR: Could not create folder {c_name}")
        except Exception as e:
            log_error_to_file(f"Failed to create GDrive folder:\n{e}")

    def toggle_gdrive_folder_sync_loop(self):
        if not self.is_gdrive_folder_sync_looping:
            self.is_gdrive_folder_sync_looping = True
            self.set_toggle_ui("drive", True)
            self.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop started (Using Local Persistent Index Map)...\n")
            self.log_box.see(tk.END)
            threading.Thread(target=self.gdrive_folder_sync_loop_worker, daemon=True).start()
        else:
            self.is_gdrive_folder_sync_looping = False
            self.set_toggle_ui("drive", False)
            self.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop stopped.\n")
            self.log_box.see(tk.END)

    def ui(self, fn):
        """Run fn on the Tk main thread (Tk widgets must not be touched from worker threads)."""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def ui_log(self, msg):
        self.ui(lambda: (self.log_box.insert(tk.END, msg + "\n"), self.log_box.see(tk.END)))

    def update_sync_status(self, stats):
        try:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            if stats["new"] or stats["updated"]:
                self.last_upload_text = f"last upload {now} (new {stats['new']}, updated {stats['updated']})"
            text = f"Drive sync: checked {now}   |   {self.last_upload_text}   |   failed (retrying): {stats['failed']}"
            self.ui(lambda: self.sync_status_lbl.configure(text=text))
        except Exception:
            pass

    def get_sync_interval(self):
        return float(self.sync_interval)

    def change_sync_interval(self, delta):
        step = 5 if (self.sync_interval > SYNC_INTERVAL_FINE_MAX or (self.sync_interval == SYNC_INTERVAL_FINE_MAX and delta > 0)) else 1
        self.sync_interval = max(SYNC_INTERVAL_MIN, min(SYNC_INTERVAL_MAX, self.sync_interval + delta * step))
        self.sync_interval_lbl.configure(text=f"{self.sync_interval} s")

    def start_interval_repeat(self, delta):
        self.stop_interval_repeat()
        self.change_sync_interval(delta)
        self.interval_repeat_job = self.root.after(400, lambda: self.interval_repeat(delta))

    def interval_repeat(self, delta):
        self.change_sync_interval(delta)
        self.interval_repeat_job = self.root.after(90, lambda: self.interval_repeat(delta))

    def stop_interval_repeat(self, _event=None):
        if self.interval_repeat_job:
            self.root.after_cancel(self.interval_repeat_job)
            self.interval_repeat_job = None
            self.save_current_paths()

    def prepare_gdrive_context(self):
        """Resolve today's CustomsDocs folder + subfolder ids once and reuse them until the date changes."""
        target_path = self.gdrive_src_entry.get().strip()
        if not os.path.exists(target_path):
            return None
        now = time.time()
        if not self.gd_headers or now - self.gd_headers_time > 1200:
            self.gd_headers.update(self.get_oauth_headers())
            self.gd_headers_time = now

        folder_base_name = os.path.basename(os.path.normpath(target_path))
        date_match = re.search(r'(\d{2}-\w{3}-\d{4})', folder_base_name)
        date_str = date_match.group(1) if date_match else datetime.datetime.now().strftime("%d-%b-%Y")
        c_name = f"CustomsDocs_{date_str}"

        ctx = self.gd_ctx
        if not ctx or ctx["c_name"] != c_name:
            gdrive_folder_id = self.get_or_create_folder_id(self.gd_headers, TARGET_PARENT_FOLDER_ID, c_name)
            if not gdrive_folder_id:
                return None
            sub_map = {}
            for sub in GDRIVE_SUBFOLDERS:
                sub_id = self.get_or_create_folder_id(self.gd_headers, gdrive_folder_id, sub)
                if sub_id:
                    sub_map[sub] = sub_id
            if len(sub_map) < len(GDRIVE_SUBFOLDERS):
                return None
            self.log_folder_structure_to_sheet(date_str, c_name, gdrive_folder_id, sub_map)
            ctx = self.gd_ctx = {"c_name": c_name, "id": gdrive_folder_id, "map": sub_map}
        return target_path, ctx["id"], ctx["map"], self.gd_headers

    def reconcile_with_drive(self):
        """Forget cached signatures so the next pass re-lists Drive and repairs anything deleted or changed there."""
        with self.sync_lock:
            with self.index_lock:
                for key in [k for k in self.gdrive_index_map if k.startswith("sig_")]:
                    del self.gdrive_index_map[key]
            with self.children_lock:
                self.drive_children.clear()

    def gdrive_folder_sync_loop_worker(self):
        # Images/other files are mirrored on every tick; CSVs (Report etc.) are re-checked every "Report Interval" seconds.
        last_csv_pass = 0.0
        last_reconcile = time.time()
        while self.is_gdrive_folder_sync_looping:
            try:
                if time.time() - last_reconcile >= RECONCILE_SECONDS:
                    self.reconcile_with_drive()
                    last_reconcile = time.time()
                ctx = self.prepare_gdrive_context()
                if ctx:
                    target_path, gdrive_folder_id, sub_map, headers = ctx
                    include_csv = time.time() - last_csv_pass >= self.get_sync_interval()
                    stats = self.sync_folder_by_id(target_path, gdrive_folder_id, sub_map, headers, include_csv=include_csv)
                    if include_csv:
                        last_csv_pass = time.time()
                    if stats["new"] or stats["updated"] or stats["failed"]:
                        self.ui_log(f"[GDrive Sync] new: {stats['new']} | updated: {stats['updated']} | failed (will retry): {stats['failed']} | unchanged: {stats['unchanged']}")
            except Exception as e:
                log_error_to_file(f"GDrive Sync Loop Error: {e}")
                time.sleep(2)
            for _ in range(4):
                if not self.is_gdrive_folder_sync_looping:
                    break
                time.sleep(0.25)

    def resolve_top_level_id(self, folder_name, subfolder_id_map):
        """Map a local top-level folder to its Drive subfolder, ignoring case and known renames."""
        key = folder_name.strip().lower()
        key = GDRIVE_FOLDER_ALIASES.get(key, key)
        for name, fid in subfolder_id_map.items():
            if name.lower() == key:
                return fid
        return None

    def get_drive_children(self, headers, folder_id):
        """Non-folder files already on Drive in folder_id, keyed by lower-case name (listed once per session)."""
        with self.children_lock:
            cached = self.drive_children.get(folder_id)
            if cached is not None:
                return cached
            children = {}
            page_token = None
            while True:
                params = {
                    "q": f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                    "fields": "nextPageToken, files(id, name, size, md5Checksum)",
                    "pageSize": 1000,
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True
                }
                if page_token:
                    params["pageToken"] = page_token
                res = self.drive_request("GET", "https://www.googleapis.com/drive/v3/files", headers, params=params)
                if res is None or res.status_code != 200:
                    return None
                data = res.json()
                for f in data.get("files", []):
                    children.setdefault(f["name"].strip().lower(), f)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            self.drive_children[folder_id] = children
            return children

    def read_file_bytes(self, path):
        for _ in range(5):
            try:
                with open(path, 'rb') as f:
                    return f.read()
            except FileNotFoundError:
                return None
            except OSError:
                time.sleep(0.3)
        return None

    def collect_sync_jobs(self, local_dir, root_gdrive_id, subfolder_id_map, headers, include_csv):
        """Walk the local tree, creating missing Drive folders, and return (path, name, drive_parent_id, is_csv) jobs."""
        jobs = []
        dir_ids = {local_dir: root_gdrive_id}
        for dirpath, dirnames, filenames in os.walk(local_dir):
            pid = dir_ids[dirpath]
            for d in list(dirnames):
                sub_id = self.resolve_top_level_id(d, subfolder_id_map) if dirpath == local_dir else None
                if not sub_id:
                    sub_id = self.get_or_create_folder_id(headers, pid, d)
                if sub_id:
                    dir_ids[os.path.join(dirpath, d)] = sub_id
                else:
                    dirnames.remove(d)  # folder could not be created now; retried next pass
            for f in filenames:
                if f.endswith(".sync_snapshot.tmp"):
                    continue
                is_csv = f.lower().endswith(".csv")
                if is_csv and not include_csv:
                    continue
                jobs.append((os.path.join(dirpath, f), f, pid, is_csv))
        jobs.sort(key=lambda j: not j[3])  # CSVs first so reports are never queued behind thousands of images
        return jobs

    def sync_one_file(self, path, name, pid, is_csv, headers):
        """Make one Drive file identical to the local one. Returns new/updated/unchanged/skipped/failed."""
        if self.retry_after.get(path, 0) > time.time():
            return "skipped"
        try:
            st = os.stat(path)
        except OSError:
            return "skipped"
        sig = f"{st.st_size}:{st.st_mtime_ns}"
        clean = name.strip().lower()
        id_key = f"file_{pid}_{clean}"
        sig_key = f"sig_{pid}_{clean}"

        with self.index_lock:
            file_id = self.gdrive_index_map.get(id_key)
            old_sig = self.gdrive_index_map.get(sig_key)
        if file_id and old_sig == sig:
            return "unchanged"
        if not is_csv and time.time() - st.st_mtime < 1.5:
            return "skipped"  # still being written; picked up on the next pass

        def remember(fid):
            with self.index_lock:
                self.gdrive_index_map[id_key] = fid
                self.gdrive_index_map[sig_key] = sig
                self.index_dirty = True

        content = None
        children = None
        if not file_id or old_sig is None:
            children = self.get_drive_children(headers, pid)
            if children is None:
                return "failed"
            remote = children.get(clean)
            file_id = remote["id"] if remote else None  # live listing wins over a stale index entry
            if remote:
                if is_csv:
                    content = self.read_file_bytes(path)
                    same = content is not None and remote.get("md5Checksum") == hashlib.md5(content).hexdigest()
                else:
                    same = remote.get("size") == str(st.st_size)
                if same:
                    remember(file_id)
                    return "unchanged"

        if content is None:
            content = self.read_file_bytes(path)
        if content is None:
            if not os.path.exists(path):
                return "skipped"
            self.retry_after[path] = time.time() + 60
            log_error_to_file(f"Could not read for upload (locked?): {path}")
            return "failed"

        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        result = "updated" if file_id else "new"

        if file_id:
            # Drive rejects a 'parents' field on update, so send only the content.
            res = self.drive_request("PATCH", f"https://www.googleapis.com/upload/drive/v3/files/{file_id}", headers,
                                     extra_headers={"Content-Type": mime}, params={"uploadType": "media", "supportsAllDrives": "true"}, data=content)
            if res is not None and res.status_code == 404:
                file_id = None
                result = "new"
            elif res is None or res.status_code != 200:
                self.retry_after[path] = time.time() + 60
                log_error_to_file(f"Drive update failed for {path}: {getattr(res, 'status_code', 'no response')} {getattr(res, 'text', '')[:300]}")
                return "failed"

        if not file_id:
            boundary = 'foo_bar_baz'
            metadata_part = json.dumps({"name": name, "parents": [pid]})
            body = (
                f"--{boundary}\r\n"
                f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{metadata_part}\r\n"
                f"--{boundary}\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode('utf-8') + content + f"\r\n--{boundary}--".encode('utf-8')
            res = self.drive_request("POST", "https://www.googleapis.com/upload/drive/v3/files", headers,
                                     extra_headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                                     params={"uploadType": "multipart", "supportsAllDrives": "true"}, data=body)
            if res is None or res.status_code != 200:
                self.retry_after[path] = time.time() + 60
                log_error_to_file(f"Drive create failed for {path}: {getattr(res, 'status_code', 'no response')} {getattr(res, 'text', '')[:300]}")
                return "failed"
            file_id = res.json().get("id")
            if children is not None:
                children[clean] = {"id": file_id, "name": name}

        remember(file_id)
        self.retry_after.pop(path, None)
        return result

    def sync_folder_by_id(self, local_dir, root_gdrive_id, subfolder_id_map, headers, include_csv=True):
        """Mirror local_dir onto Drive. Only new/changed files are uploaded; failures are retried on the next pass."""
        stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
        with self.sync_lock:
            try:
                if not os.path.isdir(local_dir):
                    return stats
                jobs = self.collect_sync_jobs(local_dir, root_gdrive_id, subfolder_id_map, headers, include_csv)
                stats_lock = threading.Lock()

                def run(job):
                    path, name, pid, is_csv = job
                    try:
                        result = self.sync_one_file(path, name, pid, is_csv, headers)
                    except Exception as e:
                        log_error_to_file(f"Upload/Update exception on {path}: {e}")
                        result = "failed"
                    with stats_lock:
                        stats[result] += 1

                with concurrent.futures.ThreadPoolExecutor(max_workers=SYNC_WORKERS) as executor:
                    list(executor.map(run, jobs))
                if self.index_dirty:
                    self.index_dirty = False
                    self.save_index()
                self.update_sync_status(stats)
            except Exception as e:
                log_error_to_file(f"Sync error on {local_dir}: {e}")
        return stats
 
    def update_stats_display(self):
        values = {"processed": self.count_processed, "stamped": self.count_already_stamped, "errors": self.count_errors, "files": self.count_files_move}
        for key, value in values.items():
            self.stat_labels[key].configure(text=f"{value:,}")
        self.stat_labels["errors"].configure(text_color="#e5484d" if self.count_errors else ("gray10", "gray90"))

    def jump_to_error_log(self):
        log_content = self.log_box.get("1.0", tk.END)
        lines = log_content.split('\n')
        self.log_box.tag_remove("highlight", "1.0", tk.END)
        for idx, line in enumerate(lines):
            if "Error" in line:
                line_number = idx + 1
                self.log_box.see(f"{line_number}.0")
                self.log_box.tag_add("highlight", f"{line_number}.0", f"{line_number}.end")
                self.log_box.tag_config("highlight", background="yellow", foreground="black")
                return
        self.log_box.insert(tk.END, "No errors found in current log.\n")
        self.log_box.see(tk.END)

    def toggle_expand_log(self):
        self.is_log_expanded = not self.is_log_expanded
        if self.is_log_expanded:
            self.top_area.grid_remove()
            self.expand_log_btn.configure(text="Restore view")
        else:
            self.top_area.grid()
            self.expand_log_btn.configure(text="Expand log")

    def auto_create_barcode_stamp_folder(self, silent=True):
        target_dir = self.output_entry.get().strip()
        if target_dir:
            try:
                os.makedirs(target_dir, exist_ok=True)
                os.makedirs(os.path.join(target_dir, "Error_Files"), exist_ok=True)
                if not silent:
                    messagebox.showinfo("Success", f"Output folder created successfully:\n{target_dir}")
            except Exception as e:
                log_error_to_file(f"Folder creation error: {e}")
                if not silent:
                    messagebox.showerror("Error", f"Could not create folder:\n{e}")

    def reset_history(self):
        self.count_processed = 0
        self.count_already_stamped = 0
        self.count_errors = 0
        self.seen_containers.clear()
        self.first_seen_files.clear()
        self.processed_source_files.clear()
        self.set_progress(0, 1)
        self.update_output_file_count()
        self.log_box.delete("1.0", tk.END)
        self.log_box.insert(tk.END, "Log cleared and stats reset.\n")
        self.log_box.see(tk.END)

    def load_stamp_image(self):
        fixed_file = self.fixed_entry.get()
        if fixed_file and os.path.exists(fixed_file):
            try:
                img_raw = Image.open(fixed_file).convert("RGBA")
                datas = img_raw.getdata()
                new_data = []
                for item in datas:
                    if (item[0] > 200 and item[1] > 200 and item[2] > 200) or (abs(item[0] - item[1]) < 10 and abs(item[1] - item[2]) < 10 and item[0] > 150):
                        new_data.append((255, 255, 255, 0))
                    else:
                        new_data.append(item)
                img_raw.putdata(new_data)
                enhancer = ImageEnhance.Color(img_raw)
                img_raw = enhancer.enhance(3.5)
                contrast_enhancer = ImageEnhance.Contrast(img_raw)
                img_raw = contrast_enhancer.enhance(3.0)
                img_raw.thumbnail((500, 500))
                return img_raw
            except Exception as e:
                log_error_to_file(f"Stamp image load error: {e}")
        return None

    def toggle_watch(self):
        if not self.is_watching:
            target_output_dir = self.output_entry.get().strip()
            source = self.source_entry.get().strip()
            if not source or not target_output_dir:
                messagebox.showerror("Missing Information", "Please specify Source folder and Output directory.")
                self.set_toggle_ui("watch", False)
                return
            
            os.makedirs(target_output_dir, exist_ok=True)
            os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
            self.session_output_dir = target_output_dir
            
            self.is_watching = True
            self.set_toggle_ui("watch", True)
            self.log_box.insert(tk.END, f"Auto-Watch started. Output: {self.session_output_dir}\n")
            self.log_box.see(tk.END)
            
            self.toggle_processing_mode_silent()
            threading.Thread(target=self.watch_folder_loop, daemon=True).start()
        else:
            self.is_watching = False
            self.set_toggle_ui("watch", False)
            self.log_box.insert(tk.END, f"Auto-Watch mode stopped.\n")
            self.log_box.see(tk.END)

    def watch_folder_loop(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_watching:
            try:
                source_dir = self.source_entry.get().strip()  # re-read so a date rollover is picked up
                self.session_output_dir = self.output_entry.get().strip() or self.session_output_dir
                self.toggle_processing_mode_silent()
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                    
                    valid_files = [f for f in all_files if f not in self.processed_source_files and self.file_retry_after.get(f, 0) <= time.time() and "@" in f]
                    other_files = [f for f in all_files if f not in self.processed_source_files and self.file_retry_after.get(f, 0) <= time.time() and "@" not in f]
                    container_files = valid_files + other_files
                    
                    if container_files:
                        total_files_batch = len(container_files)
                        self.ui(lambda n=total_files_batch: self.set_progress(0, n))

                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=False, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            completed_count = 0
                            for future in concurrent.futures.as_completed(futures):
                                file_name = futures[future]
                                res_status, result_msg = future.result()
                                if res_status == "retry":
                                    self.file_retry_after[file_name] = time.time() + 30
                                else:
                                    self.processed_source_files.add(file_name)
                                if res_status in ["processed", "duplicate_container"]:
                                    self.count_processed += 1
                                    if res_status == "processed":
                                        self.count_already_stamped += 1
                                elif res_status == "error":
                                    self.count_errors += 1
                                if result_msg:
                                    self.ui_log(result_msg)
                                completed_count += 1
                                self.ui(lambda v=completed_count: self.set_progress(v))
                                self.ui(self.update_output_file_count)
            except Exception as e:
                log_error_to_file(f"Watch folder loop error: {e}")
            time.sleep(0.5)

    def toggle_copy_processing(self):
        if not self.is_copy_processing:
            target_output_dir = self.output_entry.get().strip()
            source = self.source_entry.get().strip()
            if not source or not target_output_dir:
                messagebox.showerror("Error", "Please specify Source folder and Output directory.")
                self.set_toggle_ui("copy", False)
                return

            os.makedirs(target_output_dir, exist_ok=True)
            self.session_output_dir = target_output_dir
            self.is_copy_processing = True
            self.set_toggle_ui("copy", True)
            self.log_box.insert(tk.END, f"Auto-Copy & Process started (Keep Source). Output: {target_output_dir}\n")
            self.log_box.see(tk.END)

            self.toggle_processing_mode_silent()
            threading.Thread(target=self.copy_processing_loop_worker, daemon=True).start()
        else:
            self.is_copy_processing = False
            self.set_toggle_ui("copy", False)
            self.log_box.insert(tk.END, f"Auto-Copy & Process stopped.\n")
            self.log_box.see(tk.END)

    def copy_processing_loop_worker(self):
        source_dir = self.source_entry.get()
        fixed_img = self.load_stamp_image()

        while self.is_copy_processing:
            try:
                source_dir = self.source_entry.get().strip()  # re-read so a date rollover is picked up
                self.session_output_dir = self.output_entry.get().strip() or self.session_output_dir
                self.toggle_processing_mode_silent()
                if os.path.exists(source_dir):
                    os.makedirs(self.session_output_dir, exist_ok=True)
                    all_files = [f for f in os.listdir(source_dir) if os.path.isfile(os.path.join(source_dir, f))]
                    
                    valid_files = [f for f in all_files if f not in self.processed_source_files and self.file_retry_after.get(f, 0) <= time.time() and "@" in f]
                    other_files = [f for f in all_files if f not in self.processed_source_files and self.file_retry_after.get(f, 0) <= time.time() and "@" not in f]
                    container_files = valid_files + other_files
                    
                    if container_files:
                        total_files_batch = len(container_files)
                        self.ui(lambda n=total_files_batch: self.set_progress(0, n))

                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                            futures = {
                                executor.submit(process_single_file, file_name, source_dir, self.session_output_dir, fixed_img, copy_only=True, seen_containers=self.seen_containers, first_seen_files=self.first_seen_files, verify_mode=self.verify_mode_active, register_numbers=self.register_numbers_set): file_name 
                                for file_name in container_files
                            }
                            completed_count = 0
                            for future in concurrent.futures.as_completed(futures):
                                file_name = futures[future]
                                res_status, result_msg = future.result()
                                if res_status == "retry":
                                    self.file_retry_after[file_name] = time.time() + 30
                                else:
                                    self.processed_source_files.add(file_name)
                                if res_status in ["processed", "duplicate_container"]:
                                    self.count_processed += 1
                                    if res_status == "processed":
                                        self.count_already_stamped += 1
                                elif res_status == "error":
                                    self.count_errors += 1
                                if result_msg:
                                    self.ui_log(result_msg)
                                completed_count += 1
                                self.ui(lambda v=completed_count: self.set_progress(v))
                                self.ui(self.update_output_file_count)
            except Exception as e:
                log_error_to_file(f"Copy loop error: {e}")
            time.sleep(0.5)

    def zoom_in(self):
        if self.current_font_size < 22:
            self.current_font_size += 2
            self.update_font_sizes()

    def zoom_out(self):
        if self.current_font_size > 8:
            self.current_font_size -= 2
            self.update_font_sizes()

    def update_font_sizes(self):
        f_norm = ("Segoe UI", self.current_font_size)
        f_log = ("Consolas", self.current_font_size)
        for ent in [self.source_entry, self.fixed_entry, self.output_entry, self.verify_entry, self.gdrive_src_entry]:
            ent.configure(font=f_norm)
        self.log_box.configure(font=f_log)

def run_fully_automatic_startup(app_instance):
    threading.Thread(target=app_instance.run_full_automated_sequence, daemon=True).start()
    
    def trigger_loops():
        if not app_instance.is_watching:
            target_output_dir = app_instance.output_entry.get().strip()
            source = app_instance.source_entry.get().strip()
            if source and target_output_dir:
                os.makedirs(target_output_dir, exist_ok=True)
                os.makedirs(os.path.join(target_output_dir, "Error_Files"), exist_ok=True)
                app_instance.session_output_dir = target_output_dir
                app_instance.is_watching = True
                app_instance.set_toggle_ui("watch", True)
                app_instance.log_box.insert(tk.END, f"Auto-Watch started. Output: {app_instance.session_output_dir}\n")
                app_instance.log_box.see(tk.END)
                app_instance.toggle_processing_mode_silent()
                threading.Thread(target=app_instance.watch_folder_loop, daemon=True).start()

        if not app_instance.is_list_copy_looping:
            app_instance.is_list_copy_looping = True
            app_instance.set_toggle_ui("list", True)
            app_instance.log_box.insert(tk.END, "Auto-Copy loop for 'list_of_container' started...\n")
            app_instance.log_box.see(tk.END)
            threading.Thread(target=app_instance.list_copy_loop_worker, daemon=True).start()

        if not app_instance.is_gdrive_folder_sync_looping:
            app_instance.is_gdrive_folder_sync_looping = True
            app_instance.set_toggle_ui("drive", True)
            app_instance.log_box.insert(tk.END, "Auto-Sync CustomsDocs loop started...\n")
            app_instance.log_box.see(tk.END)
            threading.Thread(target=app_instance.gdrive_folder_sync_loop_worker, daemon=True).start()

    app_instance.root.after(100, trigger_loops)

if __name__ == "__main__":
    root = ctk.CTk()
    app = BarcodeApp(root)
    threading.Thread(target=run_fully_automatic_startup, args=(app,), daemon=True).start()
    root.mainloop()